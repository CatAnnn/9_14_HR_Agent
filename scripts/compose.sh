#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
env_file="${project_root}/backend/config/.env"

if [[ ! -r "${env_file}" ]]; then
  echo "Missing configuration file: ${env_file}" >&2
  exit 2
fi

mode="$(
  awk '
    /^[[:space:]]*MODEL_PROVIDER_MODE[[:space:]]*=/ {
      sub(/^[^=]*=[[:space:]]*/, "")
      value = $0
    }
    END { print value }
  ' "${env_file}"
)"
mode="${mode%$'\r'}"
mode="${mode#\"}"
mode="${mode%\"}"
mode="${mode#\'}"
mode="${mode%\'}"

asr_mode="$(
  awk '
    /^[[:space:]]*ASR_PROVIDER_MODE[[:space:]]*=/ {
      sub(/^[^=]*=[[:space:]]*/, "")
      value = $0
    }
    END { print value }
  ' "${env_file}"
)"
asr_mode="${asr_mode%$'\r'}"
asr_mode="${asr_mode#\"}"
asr_mode="${asr_mode%\"}"
asr_mode="${asr_mode#\'}"
asr_mode="${asr_mode%\'}"
asr_mode="${asr_mode:-local}"

tts_enabled="$(
  awk '
    /^[[:space:]]*TTS_ENABLED[[:space:]]*=/ {
      sub(/^[^=]*=[[:space:]]*/, "")
      value = $0
    }
    END { print value }
  ' "${env_file}"
)"
tts_enabled="${tts_enabled%$'\r'}"
tts_enabled="${tts_enabled#\"}"
tts_enabled="${tts_enabled%\"}"
tts_enabled="${tts_enabled#\'}"
tts_enabled="${tts_enabled%\'}"
tts_enabled="${tts_enabled:-true}"
tts_enabled="${tts_enabled,,}"

case "${mode}" in
  local | platform) ;;
  *)
    echo "MODEL_PROVIDER_MODE must be local or platform in ${env_file}." >&2
    exit 2
    ;;
esac

case "${asr_mode}" in
  local | bosch | browser) ;;
  *)
    echo "ASR_PROVIDER_MODE must be local, bosch, or browser in ${env_file}." >&2
    exit 2
    ;;
esac

case "${tts_enabled}" in
  true | false) ;;
  *)
    echo "TTS_ENABLED must be true or false in ${env_file}." >&2
    exit 2
    ;;
esac

project_name="06-emotion-main"
compose_working_dir="${project_root}"
compose_config_files="${project_root}/docker-compose.yml"

if [[ -n "${COMPOSE_PROJECT_NAME:-}" && "${COMPOSE_PROJECT_NAME:-}" != "${project_name}" ]]; then
  echo "Refusing Compose command: COMPOSE_PROJECT_NAME must be ${project_name}." >&2
  exit 2
fi

# The wrapper preflights one exact Compose scope. Do not allow trailing caller
# options to redirect the eventual mutation to a different project or file.
for argument in "$@"; do
  case "${argument}" in
    -p | --project-name | -f | --file | --project-directory | --env-file | \
      -p?* | -f?* | --project-name=* | --file=* | --project-directory=* | --env-file=*)
      echo "Refusing Compose command: project, file, directory, and env-file overrides are not allowed." >&2
      exit 2
      ;;
  esac
done

export COMPOSE_PROJECT_NAME="${project_name}"

# Docker socket ownership is host-specific and can change after daemon or
# workspace migrations. Resolve the supplementary group at every wrapper run
# so the non-root autoscaler retains only the exact access it needs.
docker_socket_gid="${DOCKER_SOCKET_GID:-}"
if [[ -S /var/run/docker.sock ]]; then
  docker_socket_gid="$(stat -c '%g' /var/run/docker.sock)"
fi
docker_socket_gid="${docker_socket_gid:-999}"
if [[ ! "${docker_socket_gid}" =~ ^[0-9]+$ ]]; then
  echo "Unable to resolve a numeric Docker socket GID." >&2
  exit 2
fi
export DOCKER_SOCKET_GID="${docker_socket_gid}"

compose=(
  docker compose
  --project-name "${project_name}"
  --project-directory "${compose_working_dir}"
  --env-file "${env_file}"
  -f "${compose_config_files}"
)

# A full `compose up` reconciles the service replica count from the static
# Compose file. Preserve an already-expanded backend pool so a routine rebuild
# cannot abruptly remove capacity while people are using the application.
backend_min_replicas=2
backend_max_replicas=8
reconcile_lease_key="hragent:backend-autoscaler:reconcile:${project_name}"
reconcile_lease_seconds="$(
  awk '
    /^[[:space:]]*BACKEND_AUTOSCALER_RECONCILE_LEASE_SECONDS[[:space:]]*=/ {
      sub(/^[^=]*=[[:space:]]*/, "")
      value = $0
    }
    END { print value }
  ' "${env_file}"
)"
reconcile_lease_seconds="${reconcile_lease_seconds%$'\r'}"
reconcile_lease_seconds="${reconcile_lease_seconds#\"}"
reconcile_lease_seconds="${reconcile_lease_seconds%\"}"
reconcile_lease_seconds="${reconcile_lease_seconds#\'}"
reconcile_lease_seconds="${reconcile_lease_seconds%\'}"
reconcile_lease_seconds="${reconcile_lease_seconds:-1500}"
if ! reconcile_lease_ttl_ms="$(
  awk -v value="${reconcile_lease_seconds}" '
    BEGIN {
      if (value !~ /^[0-9]+([.][0-9]+)?$/ || value <= 0) {
        exit 1
      }
      printf "%.0f", value * 1000
    }
  '
)"; then
  echo "BACKEND_AUTOSCALER_RECONCILE_LEASE_SECONDS must be positive in ${env_file}." >&2
  exit 2
fi

# These wrapper-only knobs keep contention handling bounded and make the
# heartbeat responsive to signals. Every individual sleep remains <= 2s.
reconcile_lock_max_attempts="${HR_AGENT_COMPOSE_LOCK_MAX_ATTEMPTS:-180}"
reconcile_lock_poll_seconds="${HR_AGENT_COMPOSE_LOCK_POLL_SECONDS:-1}"
reconcile_lock_heartbeat_seconds="${HR_AGENT_COMPOSE_LOCK_HEARTBEAT_SECONDS:-2}"
backend_restore_probe_seconds="${HR_AGENT_COMPOSE_BACKEND_RESTORE_PROBE_SECONDS:-1}"
backend_restore_wait_seconds="${HR_AGENT_COMPOSE_BACKEND_RESTORE_WAIT_SECONDS:-60}"
if [[ ! "${reconcile_lock_max_attempts}" =~ ^[1-9][0-9]*$ ]] || \
   ! awk -v value="${reconcile_lock_poll_seconds}" \
      'BEGIN { exit !(value ~ /^[0-9]+([.][0-9]+)?$/ && value > 0 && value <= 2) }' || \
   ! awk -v value="${reconcile_lock_heartbeat_seconds}" \
      'BEGIN { exit !(value ~ /^[0-9]+([.][0-9]+)?$/ && value > 0 && value <= 2) }' || \
   ! awk -v value="${backend_restore_probe_seconds}" \
      'BEGIN { exit !(value ~ /^[0-9]+([.][0-9]+)?$/ && value > 0 && value <= 2) }' || \
   ! awk -v value="${backend_restore_wait_seconds}" \
      'BEGIN { exit !(value ~ /^[0-9]+([.][0-9]+)?$/ && value > 0 && value <= 300) }'; then
  echo "Invalid Compose reconciliation lock timing configuration." >&2
  exit 2
fi
dry_run=false
up_index=-1
for ((index = 1; index <= $#; index++)); do
  argument="${!index}"
  if [[ "${argument}" == "--dry-run" ]]; then
    dry_run=true
  elif [[ "${argument}" == "up" && ${up_index} -lt 0 ]]; then
    up_index=$((index - 1))
  fi
done

has_requested_services=false
backend_requested=false
explicit_backend_scale=false
if ((up_index >= 0)); then
  consume_value=false
  previous_argument=""
  arguments=("$@")
  for ((index = up_index + 1; index < ${#arguments[@]}; index++)); do
    argument="${arguments[index]}"
    if [[ "${consume_value}" == "true" ]]; then
      if [[ "${previous_argument}" == "--scale" && "${argument}" == backend=* ]]; then
        explicit_backend_scale=true
      fi
      consume_value=false
      continue
    fi
    case "${argument}" in
      --scale=backend=*) explicit_backend_scale=true ;;
      --attach | --exit-code-from | --no-attach | --pull | --scale | --timeout | --wait-timeout | -t)
        previous_argument="${argument}"
        consume_value=true
        ;;
      --attach=* | --exit-code-from=* | --no-attach=* | --pull=* | --scale=* | --timeout=* | --wait-timeout=*) ;;
      -*) ;;
      *)
        has_requested_services=true
        if [[ "${argument}" == "backend" || \
              "${argument}" == "frontend" || \
              "${argument}" == "backend_autoscaler" ]]; then
          backend_requested=true
        fi
        ;;
    esac
  done
fi

reconciles_backend=false
if ((up_index >= 0)) && \
   { [[ "${has_requested_services}" == "false" ]] || [[ "${backend_requested}" == "true" ]]; }; then
  reconciles_backend=true
fi

backend_pool_replicas=0
running_backend_replicas=0
ready_backend_replicas=0
backend_running_replica_numbers=()
backend_running_container_ids=()
running_redis_container_ids=()
running_autoscaler_container_ids=()

inspect_exact_project_scope() {
  local project_container_output=""
  local inspect_format=""
  local inspect_line=""
  local container_id=""
  local container_project=""
  local container_working_dir=""
  local container_config_files=""
  local container_service=""
  local container_running=""
  local container_number=""
  local container_oneoff=""
  local container_health=""
  local project_container_ids=()
  local -A backend_numbers_seen=()

  backend_pool_replicas=0
  running_backend_replicas=0
  ready_backend_replicas=0
  backend_running_replica_numbers=()
  backend_running_container_ids=()
  running_redis_container_ids=()
  running_autoscaler_container_ids=()
  if ! project_container_output="$(
    docker ps -aq --filter "label=com.docker.compose.project=${project_name}"
  )"; then
    echo "Refusing Compose up: could not inspect project ${project_name}." >&2
    return 3
  fi

  if [[ -n "${project_container_output}" ]]; then
    mapfile -t project_container_ids <<<"${project_container_output}"
  fi
  inspect_format='{{printf "%s\x1f%s\x1f%s\x1f%s\x1f%t\x1f%s\x1f%s" (index .Config.Labels "com.docker.compose.project") (index .Config.Labels "com.docker.compose.project.working_dir") (index .Config.Labels "com.docker.compose.project.config_files") (index .Config.Labels "com.docker.compose.service") .State.Running (index .Config.Labels "com.docker.compose.container-number") (index .Config.Labels "com.docker.compose.oneoff")}}{{if .State.Health}}{{printf "\x1f%s" .State.Health.Status}}{{else}}{{printf "\x1fnone"}}{{end}}'

  for container_id in "${project_container_ids[@]}"; do
    [[ -n "${container_id}" ]] || continue
    if ! inspect_line="$(docker inspect --format "${inspect_format}" "${container_id}")"; then
      echo "Refusing Compose up: could not inspect project container ${container_id}." >&2
      return 3
    fi
    IFS=$'\x1f' read -r container_project container_working_dir container_config_files container_service container_running container_number container_oneoff container_health <<<"${inspect_line}"

    if [[ "${container_project}" != "${project_name}" || \
          "${container_working_dir}" != "${compose_working_dir}" || \
          "${container_config_files}" != "${compose_config_files}" ]]; then
      echo "Refusing Compose up: container ${container_id} uses project ${project_name} with mismatched working_dir/config_files labels." >&2
      return 3
    fi

    if [[ "${container_service}" == "backend" && "${container_oneoff,,}" == "false" ]]; then
      # Preserve stopped/created warm-pool members as well as running replicas.
      # Otherwise Compose removes them and the autoscaler immediately recreates
      # them with ever-increasing replica ordinals.
      if [[ ! "${container_number}" =~ ^[1-9][0-9]*$ ]] || \
         [[ -n "${backend_numbers_seen[${container_number}]+x}" ]]; then
        echo "Refusing Compose up: backend container ${container_id} has an invalid or duplicate replica number." >&2
        return 3
      fi
      backend_numbers_seen["${container_number}"]=1
      backend_pool_replicas=$((backend_pool_replicas + 1))
      if [[ "${container_running}" == "true" ]]; then
        running_backend_replicas=$((running_backend_replicas + 1))
        backend_running_replica_numbers+=("${container_number}")
        backend_running_container_ids+=("${container_id}")
        if [[ "${container_health,,}" == "healthy" ]]; then
          ready_backend_replicas=$((ready_backend_replicas + 1))
        fi
      fi
    elif [[ "${container_service}" == "redis" && "${container_running}" == "true" ]]; then
      running_redis_container_ids+=("${container_id}")
    elif [[ "${container_service}" == "backend_autoscaler" && "${container_running}" == "true" ]]; then
      running_autoscaler_container_ids+=("${container_id}")
    fi
  done
}

backend_activity_is_safe_to_stop() {
  local container_id=$1
  local probe_timeout_seconds=$2
  local probe_result=""

  if ! probe_result="$(
    timeout --signal=TERM --kill-after=1s "${probe_timeout_seconds}s" \
      docker exec "${container_id}" python3 -c '
import json
import sys
import urllib.request

request_timeout_seconds = max(0.1, min(5.0, float(sys.argv[2])))
with urllib.request.urlopen(
    "http://127.0.0.1:7111/api/v1/health/autoscaling",
    timeout=request_timeout_seconds,
) as response:
    payload = json.load(response)

active_http = int(payload["active_http_requests"])
active_websockets = int(payload["active_websockets"])
idle_for_seconds = float(payload["idle_for_seconds"])
minimum_idle_seconds = float(sys.argv[1])
print(
    "safe"
    if active_http == 0
    and active_websockets == 0
    and idle_for_seconds >= minimum_idle_seconds
    else "busy"
)
' 30 "${probe_timeout_seconds}"
  )"; then
    return 1
  fi
  [[ "${probe_result}" == "safe" ]]
}

backend_restore_now_seconds() {
  local now=""

  if ! now="$(date +%s.%N)" || \
     [[ ! "${now}" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
    return 1
  fi
  printf '%s\n' "${now}"
}

backend_restore_remaining_seconds() {
  local deadline=$1
  local now=""
  if [[ ! "${deadline}" =~ ^[0-9]+([.][0-9]+)?$ ]] || \
     ! now="$(backend_restore_now_seconds)"; then
    return 1
  fi
  awk -v deadline="${deadline}" -v now="${now}" '
    BEGIN {
      remaining = deadline - now
      if (remaining <= 0) {
        exit 1
      }
      printf "%.6f", remaining
    }
  '
}

backend_activity_is_safe_before_deadline() {
  local container_id=$1
  local deadline=$2
  local remaining_seconds=""
  remaining_seconds="$(backend_restore_remaining_seconds "${deadline}")" || return 1
  backend_activity_is_safe_to_stop "${container_id}" "${remaining_seconds}"
}

sleep_before_backend_restore_deadline() {
  local deadline=$1
  local remaining_seconds=""
  local sleep_seconds=""
  remaining_seconds="$(backend_restore_remaining_seconds "${deadline}")" || return 1
  sleep_seconds="$(
    awk -v remaining="${remaining_seconds}" \
        -v interval="${backend_restore_probe_seconds}" '
      BEGIN { printf "%.6f", remaining < interval ? remaining : interval }
    '
  )"
  sleep "${sleep_seconds}"
}

wait_for_backend_activity_to_be_safe() {
  local container_id=$1
  local deadline=$2

  while backend_restore_remaining_seconds "${deadline}" >/dev/null; do
    if ! kill -0 "${reconcile_heartbeat_pid}" 2>/dev/null || \
       ! renew_reconcile_lease_once; then
      return 1
    fi
    if backend_activity_is_safe_before_deadline "${container_id}" "${deadline}"; then
      return 0
    fi
    sleep_before_backend_restore_deadline "${deadline}" || return 1
  done
  return 1
}

restore_backend_running_state() {
  local target_running_replicas=$1
  shift
  local preserved_number=""
  local replica_number=""
  local container_id=""
  local index=0
  local candidate_index=0
  local candidate_present=false
  local excess_running=0
  local restore_idle_deadline=""
  local restore_started_at=""
  local stop_finished_at=""
  local stop_started_at=""
  local -A preserved_numbers=()
  local restore_candidates=()
  local sorted_restore_candidates=()

  for preserved_number in "$@"; do
    preserved_numbers["${preserved_number}"]=1
  done

  inspect_exact_project_scope || return $?
  if ((backend_pool_replicas != desired_backend_replicas)); then
    echo "Refusing backend state restoration: Compose returned ${backend_pool_replicas} pool members; expected ${desired_backend_replicas}." >&2
    return 3
  fi
  if ((running_backend_replicas < target_running_replicas)); then
    echo "Refusing backend state restoration: only ${running_backend_replicas} replicas are running; expected at least ${target_running_replicas}." >&2
    return 3
  fi

  excess_running=$((running_backend_replicas - target_running_replicas))
  if ((excess_running == 0)); then
    return 0
  fi

  for ((index = 0; index < ${#backend_running_replica_numbers[@]}; index++)); do
    replica_number=${backend_running_replica_numbers[index]}
    container_id=${backend_running_container_ids[index]}
    if [[ -z "${preserved_numbers[${replica_number}]+x}" ]]; then
      restore_candidates+=("${replica_number}"$'\t'"${container_id}")
    fi
  done
  if ((${#restore_candidates[@]} < excess_running)); then
    echo "Refusing backend state restoration: preserved replica identities changed during Compose up." >&2
    return 3
  fi

  mapfile -t sorted_restore_candidates < <(
    printf '%s\n' "${restore_candidates[@]}" | sort -t $'\t' -k1,1nr
  )
  if ! restore_started_at="$(backend_restore_now_seconds)" || \
     ! restore_idle_deadline="$(
       awk -v now="${restore_started_at}" \
           -v wait="${backend_restore_wait_seconds}" '
         BEGIN {
           deadline = now + wait
           if (deadline <= now) {
             exit 1
           }
           printf "%.6f", deadline
         }
       '
     )" || \
     [[ ! "${restore_idle_deadline}" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
    echo "Refusing backend state restoration: could not establish a bounded idle-wait deadline." >&2
    return 3
  fi
  for ((index = 0; index < excess_running; index++)); do
    IFS=$'\t' read -r replica_number container_id <<<"${sorted_restore_candidates[index]}"
    if ! wait_for_backend_activity_to_be_safe \
      "${container_id}" "${restore_idle_deadline}"; then
      echo "Refusing to stop backend replica ${replica_number}: lease ownership or zero-work state could not be proven." >&2
      return 3
    fi
    if ! sleep_before_backend_restore_deadline "${restore_idle_deadline}"; then
      echo "Refusing to stop backend replica ${replica_number}: the shared idle-wait deadline expired." >&2
      return 3
    fi

    if ! kill -0 "${reconcile_heartbeat_pid}" 2>/dev/null || \
       ! renew_reconcile_lease_once || \
       ! backend_activity_is_safe_before_deadline \
         "${container_id}" "${restore_idle_deadline}"; then
      echo "Refusing to stop backend replica ${replica_number}: its state changed during the idle probe window." >&2
      return 3
    fi

    inspect_exact_project_scope || return $?
    if ((running_backend_replicas <= target_running_replicas)) || \
       ((ready_backend_replicas <= target_running_replicas)); then
      echo "Refusing to stop backend replica ${replica_number}: the running or healthy capacity floor was reached." >&2
      return 3
    fi
    candidate_present=false
    for ((candidate_index = 0; candidate_index < ${#backend_running_replica_numbers[@]}; candidate_index++)); do
      if [[ "${backend_running_replica_numbers[candidate_index]}" == "${replica_number}" && \
            "${backend_running_container_ids[candidate_index]}" == "${container_id}" ]]; then
        candidate_present=true
        break
      fi
    done
    if [[ "${candidate_present}" != "true" ]] || \
       ! kill -0 "${reconcile_heartbeat_pid}" 2>/dev/null || \
       ! renew_reconcile_lease_once || \
       ! backend_activity_is_safe_before_deadline \
         "${container_id}" "${restore_idle_deadline}"; then
      echo "Refusing to stop backend replica ${replica_number}: final identity, lease, or activity validation failed." >&2
      return 3
    fi
    if ! stop_started_at="$(backend_restore_now_seconds)"; then
      echo "Refusing to stop backend replica ${replica_number}: could not establish the stop deadline." >&2
      return 3
    fi
    if ! docker stop --time 300 "${container_id}" >/dev/null; then
      echo "Failed to restore stopped backend replica ${replica_number}." >&2
      return 3
    fi
    if ! stop_finished_at="$(backend_restore_now_seconds)"; then
      echo "Backend replica ${replica_number} stopped, but stop duration could not be verified; no further replicas will be changed." >&2
      return 3
    fi
    # A graceful container stop has its own timeout. Do not let that time eat
    # the shared budget used to prove that later replicas are idle.
    if ! restore_idle_deadline="$(
      awk -v deadline="${restore_idle_deadline}" \
          -v started="${stop_started_at}" \
          -v finished="${stop_finished_at}" '
        BEGIN {
          elapsed = finished - started
          if (elapsed < 0 || elapsed > 301) {
            exit 1
          }
          printf "%.6f", deadline + elapsed
        }
      '
    )" || \
       [[ ! "${restore_idle_deadline}" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
      echo "Backend replica ${replica_number} stopped, but the bounded idle-wait deadline could not be restored; no further replicas will be changed." >&2
      return 3
    fi
    if ! kill -0 "${reconcile_heartbeat_pid}" 2>/dev/null || \
       ! renew_reconcile_lease_once; then
      echo "Backend replica ${replica_number} stopped, but the reconciliation lease was then lost; no further replicas will be changed." >&2
      return 3
    fi
  done

  inspect_exact_project_scope || return $?
  if ((running_backend_replicas != target_running_replicas)) || \
     ((ready_backend_replicas < target_running_replicas)); then
    echo "Backend state restoration ended with running=${running_backend_replicas}, healthy=${ready_backend_replicas}; expected at least ${target_running_replicas} healthy running replicas." >&2
    return 3
  fi
}

if ((up_index >= 0)); then
  inspect_exact_project_scope || exit $?
fi

final_arguments=("$@")
apply_implicit_backend_scale=false
if ((up_index >= 0)) && \
   [[ "${explicit_backend_scale}" == "false" ]] && \
   { [[ "${has_requested_services}" == "false" ]] || [[ "${backend_requested}" == "true" ]]; }; then
  apply_implicit_backend_scale=true
fi

reconcile_lease_held=false
reconcile_lease_token=""
reconcile_lease_redis_container_id=""
reconcile_lease_autoscaler_container_id=""
reconcile_heartbeat_pid=""
compose_pid=""

release_reconcile_lease() {
  local release_result=""

  if [[ -n "${reconcile_heartbeat_pid}" ]]; then
    kill "${reconcile_heartbeat_pid}" 2>/dev/null || true
    wait "${reconcile_heartbeat_pid}" 2>/dev/null || true
    reconcile_heartbeat_pid=""
  fi

  if [[ "${reconcile_lease_held}" == "true" ]]; then
    if ! release_result="$(
      docker exec "${reconcile_lease_redis_container_id}" redis-cli --raw EVAL \
        "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end return 0" \
        1 "${reconcile_lease_key}" "${reconcile_lease_token}"
    )"; then
      echo "Warning: failed to release the Compose reconciliation lease safely." >&2
    elif [[ "${release_result}" != "1" ]]; then
      echo "Warning: Compose reconciliation lease ownership changed before cleanup; it was not deleted." >&2
    fi
    reconcile_lease_held=false
  fi
}

cleanup_reconcile_lease() {
  local status=$?
  trap - EXIT INT TERM HUP
  set +e
  if [[ -n "${compose_pid}" ]] && kill -0 "${compose_pid}" 2>/dev/null; then
    kill -TERM "${compose_pid}" 2>/dev/null || true
    wait "${compose_pid}" 2>/dev/null || true
    compose_pid=""
  fi
  release_reconcile_lease
  exit "${status}"
}

renew_reconcile_lease_once() {
  local renew_result=""
  if ! renew_result="$(
    docker exec "${reconcile_lease_redis_container_id}" redis-cli --raw EVAL \
      "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('PEXPIRE', KEYS[1], ARGV[2]) end return 0" \
      1 "${reconcile_lease_key}" "${reconcile_lease_token}" "${reconcile_lease_ttl_ms}"
  )"; then
    return 1
  fi
  [[ "${renew_result}" == "1" ]]
}

renew_reconcile_lease() {
  while true; do
    sleep "${reconcile_lock_heartbeat_seconds}"
    renew_reconcile_lease_once || return 1
  done
}

initial_running_redis_replicas=${#running_redis_container_ids[@]}
initial_running_autoscaler_replicas=${#running_autoscaler_container_ids[@]}
if [[ "${reconciles_backend}" == "true" && "${dry_run}" == "false" ]]; then
  if ((initial_running_autoscaler_replicas > 1)); then
    echo "Refusing Compose up: expected at most one running, exact-scope backend autoscaler." >&2
    exit 3
  fi
  if ((initial_running_redis_replicas > 1)) || \
     ((initial_running_autoscaler_replicas > 0 && initial_running_redis_replicas != 1)); then
    echo "Refusing Compose up: a running autoscaler requires exactly one running, exact-scope Redis container." >&2
    exit 3
  fi

  # With no running Redis and no autoscaler this is a genuine first start, so
  # no shared lock exists yet. Once Redis exists, always lock—even if the
  # autoscaler was not running during the first scan—so it cannot start and
  # mutate backend capacity between preflight and Compose.
  if ((initial_running_redis_replicas == 1)); then
    reconcile_lease_redis_container_id="${running_redis_container_ids[0]}"
    if ((initial_running_autoscaler_replicas == 1)); then
      reconcile_lease_autoscaler_container_id="${running_autoscaler_container_ids[0]}"
    fi
    reconcile_lease_token="${project_name}:compose:$$:${RANDOM}:$(date +%s%N)"
    for ((attempt = 1; attempt <= reconcile_lock_max_attempts; attempt++)); do
      if ! acquire_result="$(
        docker exec "${reconcile_lease_redis_container_id}" redis-cli --raw SET \
          "${reconcile_lease_key}" "${reconcile_lease_token}" NX PX "${reconcile_lease_ttl_ms}"
      )"; then
        echo "Refusing Compose up: Redis reconciliation lease is unavailable." >&2
        exit 3
      fi
      if [[ "${acquire_result}" == "OK" ]]; then
        reconcile_lease_held=true
        break
      fi
      if ((attempt < reconcile_lock_max_attempts)); then
        sleep "${reconcile_lock_poll_seconds}"
      fi
    done

    if [[ "${reconcile_lease_held}" != "true" ]]; then
      echo "Refusing Compose up: reconciliation lease remained contended." >&2
      exit 3
    fi

    trap cleanup_reconcile_lease EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    trap 'exit 129' HUP

    # The first scan only identifies where to acquire the shared lease.
    # Capacity is selected from this second exact-scope scan while locked.
    inspect_exact_project_scope || exit $?
    locked_running_redis_replicas=${#running_redis_container_ids[@]}
    locked_running_autoscaler_replicas=${#running_autoscaler_container_ids[@]}
    if ((locked_running_redis_replicas != 1)) || \
       [[ "${running_redis_container_ids[0]}" != "${reconcile_lease_redis_container_id}" ]]; then
      echo "Refusing Compose up: Redis topology changed after acquiring the reconciliation lease." >&2
      exit 3
    fi
    if ((locked_running_autoscaler_replicas != initial_running_autoscaler_replicas)); then
      echo "Refusing Compose up: backend autoscaler topology changed after acquiring the reconciliation lease." >&2
      exit 3
    fi
    if ((locked_running_autoscaler_replicas == 1)) && \
       [[ "${running_autoscaler_container_ids[0]}" != "${reconcile_lease_autoscaler_container_id}" ]]; then
      echo "Refusing Compose up: backend autoscaler identity changed after acquiring the reconciliation lease." >&2
      exit 3
    fi
    if ! renew_reconcile_lease_once; then
      echo "Refusing Compose up: reconciliation lease ownership was lost during the locked preflight." >&2
      exit 3
    fi

    renew_reconcile_lease &
    reconcile_heartbeat_pid=$!
  fi
fi

restore_backend_running_replicas=false
target_running_backend_replicas=0
preserved_backend_running_replica_numbers=()
if [[ "${apply_implicit_backend_scale}" == "true" ]]; then
  target_running_backend_replicas=${running_backend_replicas}
  if ((target_running_backend_replicas < backend_min_replicas)); then
    target_running_backend_replicas=${backend_min_replicas}
  elif ((target_running_backend_replicas > backend_max_replicas)); then
    target_running_backend_replicas=${backend_max_replicas}
  fi

  # An established autoscaled pool is protected by the shared lease. Preserve
  # every warm member during Compose up, then restore the pre-deploy running set
  # before releasing that lease. On a genuine unlocked recovery, retain the old
  # running-count behavior rather than starting stopped replicas without a lock.
  desired_backend_replicas=${target_running_backend_replicas}
  if [[ "${reconcile_lease_held}" == "true" ]]; then
    if ((backend_pool_replicas > backend_max_replicas)); then
      echo "Refusing Compose up: backend pool size ${backend_pool_replicas} exceeds the configured maximum ${backend_max_replicas}." >&2
      exit 3
    fi
    desired_backend_replicas=${backend_pool_replicas}
    if ((desired_backend_replicas < backend_min_replicas)); then
      desired_backend_replicas=${backend_min_replicas}
    fi
    if ((desired_backend_replicas > target_running_backend_replicas)); then
      restore_backend_running_replicas=true
      preserved_backend_running_replica_numbers=("${backend_running_replica_numbers[@]}")
    fi
  fi
  final_arguments+=(--scale "backend=${desired_backend_replicas}")
fi

if [[ "${mode}" == "platform" ]]; then
  if [[ "${dry_run}" == "false" ]]; then
    for argument in "$@"; do
      if [[ "${argument}" == "up" ]]; then
        "${compose[@]}" --profile local rm \
          --stop --force qwen_embedding qwen_reranker
        break
      fi
    done
  fi
fi

if [[ "${asr_mode}" != "local" ]]; then
  if [[ "${dry_run}" == "false" ]]; then
    for argument in "$@"; do
      if [[ "${argument}" == "up" ]]; then
        "${compose[@]}" --profile asr-local rm --stop --force \
          qwen3_asr qwen3_asr_secondary
        break
      fi
    done
  fi
fi

if [[ "${tts_enabled}" == "false" ]]; then
  if [[ "${dry_run}" == "false" ]]; then
    for argument in "$@"; do
      if [[ "${argument}" == "up" ]]; then
        "${compose[@]}" --profile tts rm --stop --force fish_tts
        break
      fi
    done
  fi
fi

if [[ "${reconcile_lease_held}" != "true" ]]; then
  exec "${compose[@]}" "${final_arguments[@]}"
fi

# Do not exec while holding the lease: the wrapper must keep renewing it and
# must perform a compare-and-delete release on every exit path.
set +e
"${compose[@]}" "${final_arguments[@]}" &
compose_pid=$!
heartbeat_failed=false
while kill -0 "${compose_pid}" 2>/dev/null; do
  if ! kill -0 "${reconcile_heartbeat_pid}" 2>/dev/null; then
    heartbeat_failed=true
    kill -TERM "${compose_pid}" 2>/dev/null || true
    break
  fi
  sleep 0.25
done
wait "${compose_pid}"
compose_status=$?
compose_pid=""

if [[ "${heartbeat_failed}" == "true" && ${compose_status} -eq 0 ]]; then
  echo "Compose reconciliation lease renewal failed while Compose was running." >&2
  exit 3
fi

if ((compose_status == 0)); then
  if [[ "${restore_backend_running_replicas}" == "true" ]]; then
    if ! kill -0 "${reconcile_heartbeat_pid}" 2>/dev/null || \
       ! renew_reconcile_lease_once; then
      echo "Refusing backend state restoration because the reconciliation lease was lost." >&2
      exit 3
    fi
    if ! restore_backend_running_state \
      "${target_running_backend_replicas}" \
      "${preserved_backend_running_replica_numbers[@]}"; then
      exit 3
    fi
  fi

  # Stop the asynchronous renewer and perform one final synchronous
  # compare-PEXPIRE. This closes the boundary where Compose and the heartbeat
  # can finish at nearly the same time and the loop observes Compose first.
  kill "${reconcile_heartbeat_pid}" 2>/dev/null || true
  wait "${reconcile_heartbeat_pid}" 2>/dev/null || true
  reconcile_heartbeat_pid=""
  if ! renew_reconcile_lease_once; then
    echo "Compose completed, but reconciliation lease ownership was lost before success could be accepted." >&2
    exit 3
  fi
fi
exit "${compose_status}"
