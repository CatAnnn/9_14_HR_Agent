from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import time
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit
import uuid

import requests
import websocket

from loadtests.accounts import LoadTestAccount, configured_accounts


@dataclass(slots=True)
class AsrSessionResult:
    account_index: int
    session_id: str = ""
    recording_id: str = ""
    accepted: bool = False
    preview_available: bool = False
    partial_count: int = 0
    terminal_type: str = ""
    terminal_code: str = ""
    ready_ms: float | None = None
    first_partial_ms: float | None = None
    capture_stopped_ms: float | None = None
    final_ms: float | None = None
    event_types: list[str] = field(default_factory=list)
    degradation_codes: list[str] = field(default_factory=list)
    error: str = ""


def _websocket_url(base_url: str, session_id: str) -> str:
    parsed = urlsplit(base_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    path = (parsed.path.rstrip("/") + "/api/v1/asr/realtime").replace("//", "/")
    query = urlencode(
        {
            "session_id": session_id,
            "client_started_at_ms": int(time.time() * 1000),
        }
    )
    return urlunsplit((scheme, parsed.netloc, path, query, ""))


def _receive_event(connection: websocket.WebSocket, timeout_seconds: float) -> dict[str, Any] | None:
    connection.settimeout(timeout_seconds)
    try:
        message = connection.recv()
    except websocket.WebSocketTimeoutException:
        return None
    if isinstance(message, bytes):
        return None
    payload = json.loads(message)
    return payload if isinstance(payload, dict) else None


def _record_event(result: AsrSessionResult, event: dict[str, Any], started_at: float) -> None:
    event_type = str(event.get("type") or "")
    if not event_type:
        return
    event_session_id = str(event.get("session_id") or result.session_id)
    if event_session_id != result.session_id:
        raise RuntimeError("cross-session ASR event detected")
    result.event_types.append(event_type)
    elapsed_ms = (time.perf_counter() - started_at) * 1000.0
    if event_type == "recording_ready":
        result.accepted = True
        result.ready_ms = elapsed_ms
        result.recording_id = str(event.get("recording_id") or "")
    elif event_type == "partial":
        result.preview_available = True
        result.partial_count += 1
        if result.first_partial_ms is None and str(event.get("text") or "").strip():
            result.first_partial_ms = elapsed_ms
    elif event_type == "preview_unavailable":
        result.degradation_codes.append(str(event.get("code") or "preview_unavailable"))
    elif event_type == "capture_stopped":
        result.capture_stopped_ms = elapsed_ms
    elif event_type == "final":
        result.terminal_type = "final"
        result.terminal_code = str(event.get("code") or "final")
        result.final_ms = elapsed_ms
    elif event_type == "error":
        result.terminal_type = "error"
        result.terminal_code = str(event.get("code") or "error")


def _prepare_session(base_url: str, account: LoadTestAccount, run_id: str) -> tuple[requests.Session, str]:
    session = requests.Session()
    headers = {
        "X-Load-Test-Run-ID": run_id,
        "X-Load-Test-User": f"asr-{account.index:04d}",
    }
    response = session.post(
        f"{base_url.rstrip('/')}/api/v1/auth/login",
        json={"email": account.email, "password": account.password},
        headers=headers,
        timeout=(20, 60),
    )
    response.raise_for_status()
    response = session.post(
        f"{base_url.rstrip('/')}/api/v1/sessions",
        headers=headers,
        timeout=(20, 60),
    )
    response.raise_for_status()
    session_id = str(response.json().get("session_id") or "")
    if not session_id:
        raise RuntimeError("ASR setup omitted workflow session id")
    return session, session_id


def _run_asr_session(
    *,
    base_url: str,
    account: LoadTestAccount,
    run_id: str,
    pcm: bytes,
    sample_rate: int,
    chunk_ms: int,
    timeout_seconds: float,
    start_barrier: threading.Barrier,
) -> AsrSessionResult:
    result = AsrSessionResult(account_index=account.index)
    http_session: requests.Session | None = None
    connection: websocket.WebSocket | None = None
    started_at = time.perf_counter()
    try:
        http_session, result.session_id = _prepare_session(base_url, account, run_id)
        cookie = "; ".join(
            f"{item.name}={item.value}" for item in http_session.cookies
        )
        start_barrier.wait(timeout=60)
        connection = websocket.create_connection(
            _websocket_url(base_url, result.session_id),
            cookie=cookie,
            header=[f"X-Load-Test-Run-ID: {run_id}"],
            timeout=20,
            enable_multithread=True,
        )
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline and not result.accepted and not result.terminal_type:
            event = _receive_event(connection, 0.5)
            if event:
                _record_event(result, event, started_at)
        if not result.accepted:
            if not result.terminal_type:
                raise RuntimeError("ASR did not return recording_ready or a clear capacity error")
            return result

        bytes_per_chunk = max(2, sample_rate * 2 * chunk_ms // 1000)
        for offset in range(0, len(pcm), bytes_per_chunk):
            connection.send_binary(pcm[offset : offset + bytes_per_chunk])
            interval_deadline = time.monotonic() + chunk_ms / 1000.0
            while time.monotonic() < interval_deadline:
                event = _receive_event(connection, min(0.05, interval_deadline - time.monotonic()))
                if event:
                    _record_event(result, event, started_at)
        connection.send(
            json.dumps(
                {"type": "stop", "client_stopped_at_ms": int(time.time() * 1000)}
            )
        )
        while time.monotonic() < deadline and not result.terminal_type:
            event = _receive_event(connection, 0.5)
            if event:
                _record_event(result, event, started_at)
        if not result.terminal_type:
            raise RuntimeError("ASR did not return final or explicit error before timeout")
    except BaseException as exc:
        result.error = f"{type(exc).__name__}: {exc}"[:500]
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        if http_session is not None:
            try:
                http_session.post(
                    f"{base_url.rstrip('/')}/api/v1/auth/logout",
                    headers={"X-Load-Test-Run-ID": run_id},
                    timeout=(5, 15),
                )
            except Exception:
                pass
            http_session.close()
    return result


def evaluate_asr_results(
    results: list[AsrSessionResult],
    *,
    concurrency: int,
    expect_preview: bool,
) -> list[str]:
    failures: list[str] = []
    if any(result.error for result in results):
        failures.append("one or more ASR clients failed without a protocol-level result")
    recording_ids = [result.recording_id for result in results if result.recording_id]
    if len(recording_ids) != len(set(recording_ids)):
        failures.append("ASR recording IDs were reused across sessions")
    for result in results:
        if not result.accepted:
            failures.append(f"account {result.account_index} was rejected instead of queued")
            continue
        try:
            ready = result.event_types.index("recording_ready")
            stopped = result.event_types.index("capture_stopped")
        except ValueError:
            failures.append(f"account {result.account_index} omitted a required ASR event")
            continue
        if stopped <= ready:
            failures.append(f"account {result.account_index} returned ASR events out of order")
        if result.terminal_type != "final":
            failures.append(f"account {result.account_index} did not complete final transcription")
        elif result.event_types.index("final") <= stopped:
            failures.append(f"account {result.account_index} returned final before capture_stopped")
        if result.terminal_code == "capture_capacity_exceeded":
            failures.append(f"account {result.account_index} received a capture capacity rejection")
        if "preview_capacity_exceeded" in result.degradation_codes:
            failures.append(f"account {result.account_index} received a preview capacity rejection")
    if expect_preview:
        preview_count = sum(result.partial_count > 0 for result in results)
        if preview_count != concurrency:
            failures.append(f"expected {concurrency} preview streams, observed {preview_count}")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run isolated realtime ASR capacity tests.")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--pcm-file", type=Path, required=True)
    parser.add_argument("--accounts-file", type=Path, default=Path("loadtests/data/accounts.jsonl"))
    parser.add_argument(
        "--concurrency",
        type=int,
        choices=(8, 16, 20, 24, 32, 40, 64),
        required=True,
    )
    parser.add_argument("--sample-rate", type=int, default=16000)
    parser.add_argument("--chunk-ms", type=int, default=100)
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    parser.add_argument("--expect-preview", action="store_true")
    parser.add_argument("--artifact-root", type=Path, default=Path("loadtests/results"))
    parser.add_argument("--run-id")
    args = parser.parse_args(argv)
    expect_preview = args.expect_preview or args.concurrency <= 24
    pcm = args.pcm_file.read_bytes()
    if not pcm or len(pcm) % 2:
        parser.error("PCM fixture must be non-empty signed 16-bit mono audio")
    accounts = configured_accounts(args.accounts_file)
    if len(accounts) < args.concurrency:
        parser.error("account pool is smaller than requested ASR concurrency")

    run_id = args.run_id or f"asr-{args.concurrency}-{uuid.uuid4().hex[:10]}"
    barrier = threading.Barrier(args.concurrency)
    results: list[AsrSessionResult] = []
    started_at = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = [
            executor.submit(
                _run_asr_session,
                base_url=args.base_url,
                account=account,
                run_id=run_id,
                pcm=pcm,
                sample_rate=args.sample_rate,
                chunk_ms=args.chunk_ms,
                timeout_seconds=args.timeout_seconds,
                start_barrier=barrier,
            )
            for account in accounts[: args.concurrency]
        ]
        for future in as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda item: item.account_index)
    failures = evaluate_asr_results(
        results,
        concurrency=args.concurrency,
        expect_preview=expect_preview,
    )
    payload = {
        "format_version": 1,
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "concurrency": args.concurrency,
        "duration_seconds": round(time.perf_counter() - started_at, 3),
        "expect_preview": expect_preview,
        "model_instances": 2,
        "model_inference_capacity": 22,
        "model_session_capacity": 32,
        "queue_expected": args.concurrency > 22,
        "accepted": sum(result.accepted for result in results),
        "preview_streams": sum(result.partial_count > 0 for result in results),
        "explicit_degradations": sum(
            result.terminal_code == "capture_capacity_exceeded"
            or "preview_capacity_exceeded" in result.degradation_codes
            for result in results
        ),
        "acceptance": {"passed": not failures, "failures": failures},
        "sessions": [asdict(result) for result in results],
    }
    output = args.artifact_root / run_id / "asr" / f"concurrency-{args.concurrency}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"artifact": str(output), "passed": not failures}))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
