# 06_emotion 400-user load tests

This directory is an isolated performance-test project. It never points at the
normal PostgreSQL or Redis volumes, and the default model mode is deterministic
`stub`. The text workflow ceiling is 400 simultaneous users. Realtime ASR is a
separate capacity test.

## Safety rules

- Use a unique `LOADTEST_RUN_ID` for every distributed run.
- Keep `LOADTEST_MODEL_MODE=stub` unless Model Farm capacity and cost are approved.
- A live run requires both `LOADTEST_ALLOW_LIVE_MODELS=true` and a positive
  `LOADTEST_MAX_COST_CNY`; each workflow reserves its estimated cost atomically.
- Never point this project at a production database. Environment validation
  rejects database names without `loadtest` and non-loadtest host names.
- Do not run the final 400-user acceptance from the application host. Use at
  least two load-generator machines and at least eight workers, with no
  worker serving more than 50 users.
- The final staircase, spike, and soak runs require a system metrics JSON file.
  Missing metrics fail acceptance rather than silently passing.

## Isolated environment

Create an untracked environment file and replace both passwords:

```bash
cp loadtests/.env.example loadtests/.env
```

Validate the Compose model without starting anything:

```bash
docker compose --env-file loadtests/.env \
  -f loadtests/compose.yml config --quiet
```

Build and start the isolated application stack. The one-shot migration,
synthetic 11-scope KB seed, and 480-account seed must all complete before the
four backend replicas become healthy:

```bash
docker compose --env-file loadtests/.env \
  -f loadtests/compose.yml up -d --build gateway
```

The stack uses only these named volumes:

```text
06_emotion_loadtest_postgres_v1
06_emotion_loadtest_redis_v1
06_emotion_loadtest_runtime_v1
```

The local gateway is `http://127.0.0.1:7210`. Production ports and volumes are
not referenced.

## Stub workflow runs

The stub reproduces OpenAI-compatible streaming, JSON Schema responses,
Embedding, Reranker, and the existing five-way structured-output retry race.
It does not reduce Prompt, Schema, candidate, or response sizes in the backend.

Set a fresh run ID and select one shape in `loadtests/.env`:

```text
LOADTEST_RUN_ID=loadtest-400-stub-20260812-001
LOADTEST_SHAPE=smoke|staircase|spike|soak
```

Start the master and eight local development workers:

```bash
docker compose --env-file loadtests/.env \
  -f loadtests/compose.yml --profile loadgen up --build \
  --scale locust_worker=8 locust_master locust_worker
```

Shapes are fixed as follows:

- `smoke`: one user, stops after one complete journey.
- `staircase`: `1/10/30/60/100/200/300/400`; the 400-user stage lasts at
  least 30 minutes, then all accepted workflows drain.
- `spike`: 50 to 400 in 60 seconds, then 15 minutes at 400.
- `soak`: 400 stub users for 60 minutes.

Artifacts are written below `loadtests/results/<run-id>/`: Locust HTML, CSV,
full-history CSV, JSON summary, capped failure samples, and a manifest.
Acceptance calculates network and complete-workflow error rates separately;
successful SSE timing events cannot dilute either rate.

## Final distributed run

On the stack host, bind only to a protected load-test VLAN and firewall both
ports to the two generator machines:

```text
LOADTEST_MASTER_BIND_IP=<private-stack-ip>
LOADTEST_INFRA_BIND_IP=<private-stack-ip>
LOADTEST_GATEWAY_BIND_IP=<private-stack-ip>
```

Start only the master load generator on that host, or run the master on one of
the dedicated generator machines. Each external worker needs the same image,
run ID, 480-account configuration, and Redis password:

```bash
docker run --rm --network host \
  -e LOADTEST_RUN_ID=<shared-run-id> \
  -e LOADTEST_SHAPE=staircase \
  -e LOADTEST_EXPECTED_WORKERS=8 \
  -e LOADTEST_MODEL_MODE=stub \
  -e LOADTEST_ACCOUNT_PASSWORD='<account-password>' \
  -e LOADTEST_REDIS_PASSWORD='<redis-password>' \
  -e LOADTEST_COORDINATION_REDIS_URL='redis://:<redis-password>@<stack-ip>:7238/1' \
  06-emotion-loadtest-locust:2.44.4 \
  --worker --master-host <master-ip> --host http://<stack-ip>:7210
```

Run four workers on each of two independent machines. Verify each generator
stays below 70% CPU. Locust workers use Redis leases so no account can be used
by two virtual users at once.

## System metrics gate

Export the run-correlated SigNoz/PostgreSQL measurements into
`loadtests/metrics/system-metrics.json`, following
`loadtests/system-metrics.example.json`. Values must cover the same
`X-Load-Test-Run-ID` interval. For final acceptance set:

```text
LOADTEST_REQUIRE_SYSTEM_METRICS=true
LOADTEST_SYSTEM_METRICS_FILE=/metrics/system-metrics.json
```

The gate checks event-loop P95, database wait P95, pool exhaustion, deadlocks,
queue drain, 2.5x throughput versus one backend, and the 1-user/30-user P95
comparison.

## AIPerf endpoint matrices

Build the pinned AIPerf image:

```bash
docker build -f loadtests/AIPerf.Dockerfile -t 06-emotion-aiperf .
```

Stub examples, using the same run ID:

```bash
docker run --rm --network 06-emotion-loadtest_default \
  -e LOADTEST_RUN_ID=<run-id> \
  -v "$PWD/loadtests/results:/results" \
  06-emotion-aiperf --target llm --base-url http://model_stub:8099 \
  --model loadtest-chat --artifact-root /results

docker run --rm --network 06-emotion-loadtest_default \
  -e LOADTEST_RUN_ID=<run-id> \
  -v "$PWD/loadtests/results:/results" \
  06-emotion-aiperf --target embedding --base-url http://model_stub:8099 \
  --model loadtest-embedding --artifact-root /results

docker run --rm --network 06-emotion-loadtest_default \
  -e LOADTEST_RUN_ID=<run-id> \
  -v "$PWD/loadtests/results:/results" \
  06-emotion-aiperf --target reranker --base-url http://model_stub:8099 \
  --model loadtest-reranker --artifact-root /results
```

The exact matrices are:

```text
LLM       1/8/16/32/64/96/128/192/256/400
Embedding 1/16/32/64/98/128/196/256/400
Reranker  1/8/16/24/32/46/64/92/128/196
```

For Bosch nested responses, run `loadtests.bosch_adapter` in front of the live
endpoint and point AIPerf at the adapter. The adapter unwraps nested JSON/SSE
without changing production clients. Live AIPerf also requires
`--allow-live-models --max-cost-cny <approved-limit>` and a configured pricing
entry for the LLM model. Priced Embedding/Reranker endpoints additionally
require `--estimated-cost-cny-per-request`; use `--cost-free-endpoint` only
when the platform owner has confirmed that the endpoint is not billed.

The Bosch adapter accepts `BOSCH_ADAPTER_API_KEY`, with optional
`BOSCH_ADAPTER_API_KEY_HEADER` and `BOSCH_ADAPTER_AUTH_SCHEME`. Secrets are
forwarded to the upstream endpoint and are never written to run manifests.

## ASR capacity tests

ASR tests target an isolated deployment where realtime ASR is enabled. Supply
a fixed, licensed 16-kHz mono signed-16-bit PCM speech sample. Each connection
logs in with a different test account and creates a different workflow session.

For the local-model load-test stack, generate the deterministic fixture and
start the two pinned Qwen3-ASR services before running the four tiers:

```bash
./loadtests/prepare_asr_fixture.sh

LOADTEST_ASR_ENABLED=true \
LOADTEST_LOCAL_MODEL_PINNED_SERVICES=qwen3_asr \
docker compose --env-file loadtests/.env -p 06-emotion-loadtest \
  -f loadtests/compose.yml --profile asr-local up -d qwen3_asr qwen3_asr_secondary backend gateway

for concurrency in 8 16 20 24; do
  LOADTEST_ASR_CONCURRENCY="${concurrency}" \
  LOADTEST_RUN_ID="asr-${concurrency}-$(date -u +%Y%m%d-%H%M%S)" \
  docker compose --env-file loadtests/.env -p 06-emotion-loadtest \
    -f loadtests/compose.yml --profile asr-local --profile asr-loadgen \
    run --rm --no-deps asr_load
done
```

Each model processes at most 11 simultaneous inference sequences, for a
combined execution capacity of 22, while each instance keeps at most 16 bounded
preview sessions. The 24-way tier therefore verifies that session admission is
independent from the GPU batch. Whether inference actually queues depends on
chunk arrival alignment and must be confirmed from the resource observer's
`queue_depth` samples rather than inferred from concurrency alone. To target a
separate HTTPS deployment instead, use the lower-level commands:

```bash
python -m loadtests.asr_load --base-url https://<test-host>:7443 \
  --pcm-file loadtests/data/audio/asr-sample.pcm --concurrency 8 \
  --expect-preview --run-id <run-id>

python -m loadtests.asr_load --base-url https://<test-host>:7443 \
  --pcm-file loadtests/data/audio/asr-sample.pcm --concurrency 16 \
  --expect-preview --run-id <run-id>

python -m loadtests.asr_load --base-url https://<test-host>:7443 \
  --pcm-file loadtests/data/audio/asr-sample.pcm --concurrency 20 \
  --expect-preview --run-id <run-id>

python -m loadtests.asr_load --base-url https://<test-host>:7443 \
  --pcm-file loadtests/data/audio/asr-sample.pcm --concurrency 24 \
  --expect-preview --run-id <run-id>
```

The 8/16/20/24-way tests require a real partial stream and a final result from
every session. Recording IDs, session IDs, cookies, audio, and event streams are
checked for isolation. Tests above the combined 32-session bound must separately
configure and verify the full-recording fallback path.

## Aggregate and fail CI

After all required artifacts share one run directory:

```bash
python -m loadtests.aggregate_results \
  --run-dir loadtests/results/<run-id> \
  --require-aiperf --require-asr
```

The command writes the unified `run-manifest.json` and exits nonzero on any
Locust, system-metric, AIPerf, or ASR failure. Component regressions remain in
their existing benchmark scripts; they are not accepted as a substitute for
this end-to-end result.
