from __future__ import annotations

import asyncio
import io
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, WebSocketException
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.api.dependencies import get_session_service
from backend.api.routes import asr as asr_route
from backend.core.auth_dependency import get_current_user
from backend.core.session_context import set_current_auth_user_id
from backend.main import create_app
from backend.repositories.session_repository import SessionRepository
from backend.schemas.auth import RegisterRequest
from backend.schemas.state import SessionState
from backend.services.auth_service import AuthService
from backend.services.auth_session_service import AuthSessionService
from backend.services.password_service import PasswordPolicyError, PasswordService
from backend.services.rate_limit_service import RateLimitService
from backend.services.realtime_asr_service import (
    AsrConfigurationError,
    AsrPreviewCapacityError,
    RealtimeAsrProxy,
)
from backend.services.whitelist_service import WhitelistService


class _Cursor:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class _WhitelistConnection:
    def __init__(self, row=None):
        self.row = row

    def execute(self, *_args, **_kwargs):
        return _Cursor(self.row)


class _WhitelistRepo:
    def __init__(self, row=None):
        self.row = row

    @contextmanager
    def connection(self):
        yield _WhitelistConnection(self.row)


def test_password_hash_uses_argon2id_and_enforces_policy():
    pytest.importorskip("argon2")
    service = PasswordService()
    password = "Bosch-HR-Agent-Password-2026"

    first = service.hash_password(password)
    second = service.hash_password(password)

    assert first.startswith("$argon2id$")
    assert first != second
    assert service.verify_password(password, first) is True
    assert service.verify_password("wrong-password", first) is False
    with pytest.raises(PasswordPolicyError):
        service.hash_password("short")


def test_email_is_normalized_and_rate_rules_are_parsed():
    payload = RegisterRequest(
        email=" User.Name@BOSCH.COM ",
        password="12345678",
    )
    assert payload.email == "user.name@bosch.com"
    assert RateLimitService._parse_rule("7/minute", 1, 1).limit == 7
    assert RateLimitService._parse_rule("2/hour", 1, 1).window_seconds == 3600


def test_auth_domain_requires_an_exactly_configured_domain():
    service = object.__new__(AuthService)
    service.settings = SimpleNamespace(
        auth_allowed_domain_set={"bosch.com", "etas.com"},
    )

    assert service._is_allowed_domain("employee@etas.com") is True
    assert service._is_allowed_domain("employee@sub.etas.com") is False
    assert service._is_allowed_domain("employee@fakeetas.com") is False

    service.settings.auth_allowed_domain_set = set()
    assert service._is_allowed_domain("employee@bosch.com") is False


def test_zero_active_session_quota_disables_the_atomic_login_cap():
    captured: dict[str, object] = {}

    class Redis:
        @staticmethod
        def eval(script, *arguments):
            captured["script"] = script
            captured["arguments"] = arguments
            return 1

    service = AuthSessionService(
        SimpleNamespace(
            auth_max_active_sessions=0,
            auth_session_idle_timeout_seconds=1800,
        )
    )

    assert service._atomic_create_session(Redis(), "session-id", 1000.0, {}) is True
    assert "max_sessions > 0" in str(captured["script"])
    assert captured["arguments"][3] == "0"


def test_database_whitelist_decision_overrides_environment_default():
    settings = SimpleNamespace(
        auth_whitelist_enabled=True,
        auth_allowed_email_set={"user@bosch.com"},
        auth_admin_email_set=set(),
    )
    disabled = WhitelistService(
        repo=_WhitelistRepo({"enabled": False}),
        settings=settings,
    )
    missing = WhitelistService(repo=_WhitelistRepo(None), settings=settings)

    assert disabled.is_allowed("user@bosch.com") is False
    assert missing.is_allowed("USER@bosch.com") is True
    assert missing.is_allowed("other@bosch.com") is False


class _SessionConnection:
    def __init__(self, row):
        self.row = row

    def execute(self, *_args, **_kwargs):
        return _Cursor(self.row)


class _SessionRepo:
    def __init__(self, row):
        self.row = row

    @contextmanager
    def connection(self):
        yield _SessionConnection(self.row)


def test_session_repository_hides_sessions_owned_by_another_user():
    state = SessionState(session_id="session-1")
    repo = SessionRepository(
        repository=_SessionRepo(
            {
                "owner_user_id": "owner-a",
                "state_json": state.model_dump(mode="json"),
            }
        )
    )
    try:
        set_current_auth_user_id("owner-a")
        assert repo.get("session-1").session_id == "session-1"
        set_current_auth_user_id("owner-b")
        with pytest.raises(KeyError):
            repo.get("session-1")
    finally:
        set_current_auth_user_id(None)


def test_asr_transcript_extraction_supports_openai_and_nested_payloads():
    assert asr_route._extract_transcript({"text": "直接文本"}) == "直接文本"
    assert asr_route._extract_transcript(
        {"choices": [{"message": {"content": "嵌套文本"}}]}
    ) == "嵌套文本"


@pytest.mark.asyncio
async def test_asr_upload_reuses_model_auth_and_returns_transcript(monkeypatch):
    long_transcript = "经理语音内容" * 4_096
    long_audio = b"audio" * 4_096
    settings = SimpleNamespace(
        asr_enabled=True,
        asr_provider_mode="local",
        asr_local_language_auto_detect=True,
        asr_http_url="https://model.example/audio/transcriptions",
        asr_max_file_bytes=0,
        effective_asr_api_key="secret",
        asr_http_model="whisper",
        asr_language="zh",
        asr_timeout_seconds=30,
    )
    captured: dict = {}

    class _Auth:
        async def async_headers(self, api_key):
            captured["api_key"] = api_key
            return {"Authorization": "Bearer secret"}

    class _Response:
        status_code = 200

        @staticmethod
        def json():
            return {"text": long_transcript}

    class _Client:
        def __init__(self, **kwargs):
            captured["client_kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, **kwargs):
            captured["url"] = url
            captured["request"] = kwargs
            stream = kwargs["files"]["file"][1]
            captured["uploaded_audio"] = stream.read()
            stream.seek(0)
            return _Response()

    class _Upload:
        filename = "speech.webm"
        content_type = "audio/webm"
        file = io.BytesIO(long_audio)

    monkeypatch.setattr(asr_route, "get_settings", lambda: settings)
    monkeypatch.setattr(asr_route, "ModelAPIAuth", _Auth)
    shared_client = _Client()
    monkeypatch.setattr(
        asr_route,
        "get_shared_async_client",
        lambda pool: shared_client,
    )

    result = await asr_route.asr_transcribe(
        file=_Upload(),
        session_id="session-1",
        language="zh",
    )

    assert result.text == long_transcript
    assert captured["uploaded_audio"] == long_audio
    assert captured["api_key"] == "secret"
    assert captured["url"] == settings.asr_http_url
    assert captured["request"]["data"]["model"] == "whisper"
    assert "language" not in captured["request"]["data"]
    assert captured["request"]["headers"]["Authorization"] == "Bearer secret"


@pytest.mark.asyncio
async def test_asr_upload_keeps_optional_positive_safety_cap(monkeypatch):
    settings = SimpleNamespace(
        asr_enabled=True,
        asr_http_url="https://model.example/audio/transcriptions",
        asr_max_file_bytes=4,
        effective_asr_api_key="secret",
        asr_http_model="whisper",
        asr_language="zh",
        asr_timeout_seconds=30,
    )

    class _Upload:
        filename = "speech.webm"
        content_type = "audio/webm"
        file = io.BytesIO(b"audio")

    monkeypatch.setattr(asr_route, "get_settings", lambda: settings)

    with pytest.raises(HTTPException) as exc_info:
        await asr_route.asr_transcribe(file=_Upload())

    assert exc_info.value.status_code == 413


@pytest.mark.asyncio
async def test_local_qwen_streaming_converts_pcm_and_emits_preview_only(monkeypatch):
    captured: dict[str, object] = {"requests": [], "closed": False, "leases": []}

    class _RuntimeRecycler:
        @contextmanager
        def lease(self, service_name):
            captured["leases"].append(("enter", service_name))
            try:
                yield
            finally:
                captured["leases"].append(("exit", service_name))

    class _Response:
        def __init__(self, payload):
            self.payload = payload
            self.status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class _Client:
        def __init__(self, **kwargs):
            captured["client_kwargs"] = kwargs

        async def post(self, path, **kwargs):
            captured["requests"].append((path, kwargs))
            if path.endswith("/api/start"):
                return _Response({"session_id": "local-session"})
            if path.endswith("/api/chunk"):
                return _Response({"language": "Chinese", "text": "正在进行实时转写"})
            return _Response({"language": "Chinese", "text": "实时转写已经完成"})

        async def aclose(self):
            captured["closed"] = True

    settings = SimpleNamespace(
        asr_enabled=True,
        asr_realtime_enabled=True,
        asr_local_streaming_url="http://qwen3_asr:7118",
        asr_local_chunk_ms=1000,
        asr_local_request_timeout_seconds=30,
        asr_connect_timeout_seconds=15,
        asr_sample_rate=16000,
        asr_local_stream_window_seconds=30,
        asr_language="zh",
    )
    client = _Client()
    proxy = RealtimeAsrProxy(
        settings,
        runtime_recycler=_RuntimeRecycler(),
        client=client,
        language="en",
    )
    proxy.validate()
    await proxy.connect()
    assert captured["leases"] == [("enter", "qwen3_asr")]
    events = []

    async def capture(event):
        events.append(event)

    receiver = asyncio.create_task(proxy.receive_loop(capture))
    await proxy.append_audio(b"\x00\x00" * 16000)
    for _ in range(100):
        if events:
            break
        await asyncio.sleep(0.001)
    final_payload = await proxy.finish()
    await receiver

    requests = captured["requests"]
    assert [request[0] for request in requests] == [
        "http://qwen3_asr:7118/api/start",
        "http://qwen3_asr:7118/api/chunk",
        "http://qwen3_asr:7118/api/finish",
    ]
    assert requests[0][1]["params"] == {"language": "en"}
    chunk_request = requests[1][1]
    assert chunk_request["params"] == {"session_id": "local-session"}
    assert chunk_request["headers"]["Content-Type"] == "application/octet-stream"
    assert len(chunk_request["content"]) == 16000 * 4
    assert events == [
        {
            "type": "partial",
            "text": "正在进行实时转写",
            "preview": "正在进行实时转写",
        },
    ]
    assert final_payload["text"] == "实时转写已经完成"
    assert captured["closed"] is False
    assert captured["leases"] == [
        ("enter", "qwen3_asr"),
        ("exit", "qwen3_asr"),
    ]


@pytest.mark.asyncio
async def test_local_qwen_streaming_uses_lossless_pcm_with_shared_client():
    captured: dict[str, object] = {"requests": [], "closed": False, "leases": []}

    class _RuntimeRecycler:
        @contextmanager
        def lease(self, service_name):
            captured["leases"].append(("enter", service_name))
            try:
                yield
            finally:
                captured["leases"].append(("exit", service_name))

    class _Response:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class _SharedClient:
        async def post(self, url, **kwargs):
            captured["requests"].append((url, kwargs))
            if url.endswith("/api/start"):
                return _Response(
                    {
                        "session_id": "pcm-session",
                        "audio_format": "pcm_s16le",
                    }
                )
            if url.endswith("/api/chunk"):
                return _Response({"language": "Chinese", "text": "无损实时转写"})
            return _Response({"cancelled": True})

        async def aclose(self):
            captured["closed"] = True

    settings = SimpleNamespace(
        asr_enabled=True,
        asr_realtime_enabled=True,
        asr_local_streaming_url="http://qwen3_asr:7118",
        asr_local_chunk_ms=500,
        asr_local_request_timeout_seconds=30,
        asr_connect_timeout_seconds=15,
        asr_sample_rate=16000,
        asr_local_stream_window_seconds=12,
        asr_language="zh",
    )
    pcm = b"\x00\x80\xff\x7f" * 4000
    proxy = RealtimeAsrProxy(
        settings,
        runtime_recycler=_RuntimeRecycler(),
        client=_SharedClient(),
    )

    await proxy.connect()
    await proxy.append_audio(pcm)
    for _ in range(100):
        if proxy.last_text:
            break
        await asyncio.sleep(0.001)
    await proxy.close()

    requests = captured["requests"]
    assert [request[0] for request in requests] == [
        "http://qwen3_asr:7118/api/start",
        "http://qwen3_asr:7118/api/chunk",
        "http://qwen3_asr:7118/api/cancel",
    ]
    assert requests[0][1]["params"] == {"language": "zh"}
    chunk_request = requests[1][1]
    assert chunk_request["content"] == pcm
    assert chunk_request["headers"]["Content-Type"] == "audio/pcm"
    assert captured["closed"] is False
    assert captured["leases"] == [
        ("enter", "qwen3_asr"),
        ("exit", "qwen3_asr"),
    ]


@pytest.mark.asyncio
async def test_local_qwen_replays_audio_buffered_before_admission():
    requests: list[tuple[str, dict]] = []

    class _RuntimeRecycler:
        @contextmanager
        def lease(self, _service_name):
            yield

    class _Response:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class _Client:
        async def post(self, url, **kwargs):
            requests.append((url, kwargs))
            if url.endswith("/api/start"):
                return _Response(
                    {"session_id": "buffered-session", "audio_format": "pcm_s16le"}
                )
            if url.endswith("/api/chunk"):
                return _Response({"text": "启动音频", "speech_detected": True})
            return _Response({"cancelled": True})

    settings = SimpleNamespace(
        asr_enabled=True,
        asr_realtime_enabled=True,
        asr_local_streaming_url="http://qwen3_asr:7118",
        asr_local_chunk_ms=500,
        asr_local_request_timeout_seconds=30,
        asr_connect_timeout_seconds=15,
        asr_sample_rate=16000,
        asr_local_stream_window_seconds=12,
        asr_language="zh",
    )
    pcm = b"\x01\x02" * 8000
    proxy = RealtimeAsrProxy(
        settings,
        runtime_recycler=_RuntimeRecycler(),
        client=_Client(),
    )

    await proxy.append_audio(pcm)
    await proxy.connect()
    for _ in range(100):
        if any(url.endswith("/api/chunk") for url, _kwargs in requests):
            break
        await asyncio.sleep(0.001)
    await proxy.close()

    chunk = next(kwargs for url, kwargs in requests if url.endswith("/api/chunk"))
    assert chunk["content"] == pcm
    assert chunk["headers"]["Content-Type"] == "audio/pcm"


@pytest.mark.asyncio
async def test_local_qwen_admission_timeout_is_bounded_and_releases_lease():
    starts: list[str] = []
    leases: list[str] = []

    class _RuntimeRecycler:
        @contextmanager
        def lease(self, _service_name):
            leases.append("enter")
            try:
                yield
            finally:
                leases.append("exit")

    class _Response:
        def __init__(self, payload, *, status_code=200):
            self.payload = payload
            self.status_code = status_code

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class _Client:
        async def get(self, _url, **_kwargs):
            return _Response(
                {
                    "status": "ok",
                    "active_sessions": 16,
                    "max_sessions": 16,
                    "queue_depth": 6,
                    "max_batch_size": 10,
                }
            )

        async def post(self, url, **_kwargs):
            starts.append(url)
            return _Response({}, status_code=429)

    settings = SimpleNamespace(
        asr_enabled=True,
        asr_realtime_enabled=True,
        asr_local_streaming_url="http://qwen3_asr:7118",
        asr_local_streaming_urls=(
            "http://qwen3_asr:7118,http://qwen3_asr_secondary:7118"
        ),
        asr_local_chunk_ms=500,
        asr_local_request_timeout_seconds=30,
        asr_connect_timeout_seconds=15,
        asr_local_admission_timeout_seconds=0.1,
        asr_local_routing_probe_timeout_seconds=0.05,
        asr_sample_rate=16000,
        asr_local_stream_window_seconds=12,
        asr_language="zh",
    )
    proxy = RealtimeAsrProxy(
        settings,
        runtime_recycler=_RuntimeRecycler(),
        client=_Client(),
        routing_key="bounded-admission",
    )

    started_at = asyncio.get_running_loop().time()
    with pytest.raises(AsrPreviewCapacityError, match="完整录音转写"):
        await asyncio.wait_for(proxy.connect(), timeout=1.0)
    elapsed = asyncio.get_running_loop().time() - started_at

    assert elapsed < 1.0
    assert 2 <= len(starts) <= 4
    assert leases == ["enter", "exit"]
    assert proxy._worker_task is None
    assert proxy.failed is True


@pytest.mark.asyncio
async def test_local_qwen_admission_deadline_bounds_stalled_start_requests():
    leases: list[str] = []
    start_attempts = 0

    class _RuntimeRecycler:
        @contextmanager
        def lease(self, _service_name):
            leases.append("enter")
            try:
                yield
            finally:
                leases.append("exit")

    class _Client:
        async def post(self, url, **_kwargs):
            nonlocal start_attempts
            assert url.endswith("/api/start")
            start_attempts += 1
            await asyncio.sleep(10)

    settings = SimpleNamespace(
        asr_enabled=True,
        asr_realtime_enabled=True,
        asr_local_streaming_url="http://qwen3_asr:7118",
        asr_local_streaming_urls=(
            "http://qwen3_asr:7118,http://qwen3_asr_secondary:7118"
        ),
        asr_local_chunk_ms=500,
        asr_local_request_timeout_seconds=0,
        asr_connect_timeout_seconds=15,
        asr_local_admission_timeout_seconds=0.1,
        asr_sample_rate=16000,
        asr_local_stream_window_seconds=12,
        asr_language="zh",
    )
    proxy = RealtimeAsrProxy(
        settings,
        runtime_recycler=_RuntimeRecycler(),
        client=_Client(),
        routing_key="stalled-admission",
    )

    with pytest.raises(AsrPreviewCapacityError, match="完整录音转写"):
        await asyncio.wait_for(proxy.connect(), timeout=1.0)
    await proxy.append_audio(b"\x01\x02" * 8000)

    assert start_attempts == 2
    assert leases == ["enter", "exit"]
    assert proxy.failed is True
    assert proxy._local_pcm_buffer == b""


@pytest.mark.asyncio
async def test_local_qwen_replays_audio_received_while_waiting_for_capacity():
    first_round_done = asyncio.Event()
    chunk_sent = asyncio.Event()
    start_attempts = 0
    chunks: list[bytes] = []

    class _RuntimeRecycler:
        @contextmanager
        def lease(self, _service_name):
            yield

    class _Response:
        def __init__(self, payload, *, status_code=200):
            self.payload = payload
            self.status_code = status_code

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class _Client:
        async def post(self, url, **kwargs):
            nonlocal start_attempts
            if url.endswith("/api/start"):
                start_attempts += 1
                if start_attempts <= 2:
                    if start_attempts == 2:
                        first_round_done.set()
                    return _Response({}, status_code=429)
                return _Response(
                    {"session_id": "released-session", "audio_format": "pcm_s16le"}
                )
            if url.endswith("/api/chunk"):
                chunks.append(kwargs["content"])
                chunk_sent.set()
                return _Response({"text": "补送成功", "speech_detected": True})
            return _Response({"cancelled": True})

    settings = SimpleNamespace(
        asr_enabled=True,
        asr_realtime_enabled=True,
        asr_local_streaming_url="http://qwen3_asr:7118",
        asr_local_streaming_urls=(
            "http://qwen3_asr:7118,http://qwen3_asr_secondary:7118"
        ),
        asr_local_chunk_ms=500,
        asr_local_request_timeout_seconds=0,
        asr_connect_timeout_seconds=15,
        asr_local_admission_timeout_seconds=0.5,
        asr_sample_rate=16000,
        asr_local_stream_window_seconds=12,
        asr_language="zh",
    )
    proxy = RealtimeAsrProxy(
        settings,
        runtime_recycler=_RuntimeRecycler(),
        client=_Client(),
        routing_key="capacity-released",
    )
    first = b"\x01\x02" * 3000
    second = b"\x03\x04" * 5000

    connect_task = asyncio.create_task(proxy.connect())
    await asyncio.wait_for(first_round_done.wait(), timeout=0.5)
    await proxy.append_audio(first)
    await proxy.append_audio(second)
    await asyncio.wait_for(connect_task, timeout=1.0)
    await asyncio.wait_for(chunk_sent.wait(), timeout=1.0)
    await proxy.close()

    assert start_attempts == 3
    assert chunks == [first + second]


@pytest.mark.asyncio
async def test_local_qwen_routes_new_session_to_lower_queue_pressure():
    primary = "http://qwen3_asr:7118"
    secondary = "http://qwen3_asr_secondary:7118"
    posts: list[str] = []

    class _RuntimeRecycler:
        @contextmanager
        def lease(self, _service_name):
            yield

    class _Response:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class _Client:
        async def get(self, url, **_kwargs):
            if url == f"{primary}/health":
                return _Response(
                    {
                        "status": "ok",
                        "active_sessions": 12,
                        "max_sessions": 16,
                        "queue_depth": 5,
                        "max_batch_size": 10,
                    }
                )
            return _Response(
                {
                    "status": "ok",
                    "active_sessions": 4,
                    "max_sessions": 16,
                    "queue_depth": 1,
                    "max_batch_size": 10,
                }
            )

        async def post(self, url, **_kwargs):
            posts.append(url)
            if url.endswith("/api/start"):
                return _Response({"session_id": "load-aware-session"})
            return _Response({"text": "最终文本", "speech_detected": True})

    settings = SimpleNamespace(
        asr_enabled=True,
        asr_realtime_enabled=True,
        asr_local_streaming_url=primary,
        asr_local_streaming_urls=f"{primary},{secondary}",
        asr_local_chunk_ms=500,
        asr_local_request_timeout_seconds=30,
        asr_connect_timeout_seconds=15,
        asr_sample_rate=16000,
        asr_local_stream_window_seconds=12,
        asr_local_final_timeout_seconds=20,
        asr_language="zh",
    )
    proxy = RealtimeAsrProxy(
        settings,
        runtime_recycler=_RuntimeRecycler(),
        client=_Client(),
        routing_key="hash-is-only-a-tie-break",
    )

    await proxy.connect()
    await proxy.finish()

    assert posts == [
        f"{secondary}/api/start",
        f"{secondary}/api/finish",
    ]


@pytest.mark.asyncio
async def test_local_qwen_streaming_forwards_empty_correction_revision():
    responses = iter(
        [
            {"text": "嗯嗯", "revision": 1, "speech_detected": True},
            {"text": "", "revision": 2, "speech_detected": True},
        ]
    )

    class _Response:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class _Client:
        async def post(self, _url, **_kwargs):
            return _Response(next(responses))

    settings = SimpleNamespace(
        asr_sample_rate=16000,
        asr_local_chunk_ms=500,
        asr_local_stream_window_seconds=12,
        asr_local_streaming_url="http://qwen3_asr:7118",
    )
    proxy = RealtimeAsrProxy(settings, client=_Client())
    proxy._local_session_id = "correction-session"
    proxy._local_timeout = None

    await proxy._send_local_chunk(b"\x00\x00" * 8000)
    await proxy._send_local_chunk(b"\x00\x00" * 8000)

    first = await proxy._local_events.get()
    correction = await proxy._local_events.get()
    assert first["text"] == "嗯嗯"
    assert correction["text"] == ""
    assert correction["revision"] == 2
    assert proxy.last_text == ""
    assert proxy.speech_detected is True


@pytest.mark.asyncio
async def test_local_qwen_streaming_rejects_text_without_speech_evidence():
    class _Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "text": "《爱的初体验》是张学友演唱的一首歌曲",
                "stable_text": "《爱的初体验》是张学友演唱的一首歌曲",
                "revision": 1,
                "speech_detected": False,
            }

    class _Client:
        async def post(self, _url, **_kwargs):
            return _Response()

    settings = SimpleNamespace(
        asr_sample_rate=16000,
        asr_local_chunk_ms=500,
        asr_local_stream_window_seconds=12,
        asr_local_streaming_url="http://qwen3_asr:7118",
    )
    proxy = RealtimeAsrProxy(settings, client=_Client())
    proxy._local_session_id = "no-speech-session"
    proxy._local_timeout = None

    await proxy._send_local_chunk(b"\x00\x00" * 8000)

    assert proxy._local_events.empty()
    assert proxy.last_text == ""
    assert proxy.speech_detected is False


@pytest.mark.asyncio
async def test_local_stream_with_dropped_startup_audio_cannot_be_finalized():
    settings = SimpleNamespace(
        asr_enabled=True,
        asr_realtime_enabled=True,
        asr_local_streaming_url="http://qwen3_asr:7118",
        asr_local_chunk_ms=500,
        asr_sample_rate=16000,
        asr_local_stream_window_seconds=5,
    )
    proxy = RealtimeAsrProxy(settings)

    await proxy.append_audio(b"\x00\x00" * (6 * 16000))

    assert proxy.failed is True
    with pytest.raises(AsrConfigurationError, match="不可用于最终定稿"):
        await proxy.finish()


def test_realtime_asr_requires_local_streaming_endpoint():
    settings = SimpleNamespace(
        asr_enabled=True,
        asr_realtime_enabled=True,
        asr_local_streaming_url="",
    )

    with pytest.raises(AsrConfigurationError, match="ASR_LOCAL_STREAMING_URL"):
        RealtimeAsrProxy(settings).validate()


def test_realtime_websocket_route_has_user_auth_dependency():
    route = next(item for item in asr_route.router.routes if getattr(item, "path", "") == "/asr/realtime")
    dependency_calls = [dependency.call for dependency in route.dependant.dependencies]

    assert get_current_user in dependency_calls
    assert get_session_service in dependency_calls


def test_realtime_websocket_rejects_missing_or_unowned_workflow_session():
    class _WebSocket:
        def __init__(self, session_id: str | None):
            self.query_params = {"session_id": session_id} if session_id else {}

    class _SessionService:
        @staticmethod
        def get_session(_session_id):
            raise KeyError("not found")

    with pytest.raises(WebSocketException) as missing:
        asr_route._require_owned_workflow_session(_WebSocket(None), _SessionService())
    assert missing.value.code == 1008

    with pytest.raises(WebSocketException) as unowned:
        asr_route._require_owned_workflow_session(
            _WebSocket("session-owned-by-another-user"),
            _SessionService(),
        )
    assert unowned.value.code == 1008
    assert unowned.value.reason == "Workflow session not found"


@pytest.mark.parametrize(
    ("session_locale", "expected_language"),
    [("zh-CN", "zh"), ("en", "en"), ("de", "de"), ("ja", "ja")],
)
def test_realtime_websocket_maps_session_locale_to_asr_language(
    session_locale,
    expected_language,
):
    websocket = SimpleNamespace(
        query_params={
            "session_id": "workflow-session-locale",
            "locale": "ignored-client-locale",
            "language": "ignored-client-language",
        }
    )
    session_service = SimpleNamespace(
        get_session=lambda _session_id: SimpleNamespace(locale=session_locale)
    )

    assert asr_route._require_owned_workflow_session(
        websocket,
        session_service,
    ) == ("workflow-session-locale", expected_language)


def test_realtime_websocket_rejects_unsupported_session_locale():
    websocket = SimpleNamespace(
        query_params={"session_id": "workflow-session-locale"}
    )
    session_service = SimpleNamespace(
        get_session=lambda _session_id: SimpleNamespace(locale="en-US")
    )

    with pytest.raises(WebSocketException) as unsupported:
        asr_route._require_owned_workflow_session(websocket, session_service)

    assert unsupported.value.code == 1008
    assert unsupported.value.reason == "Workflow session locale is not supported"


@pytest.mark.parametrize(
    ("language", "default", "expected"),
    [
        ("zh-CN", "en", "zh"),
        ("zh_cn", "en", "zh"),
        ("zh", "en", "zh"),
        ("en", "zh", "en"),
        ("en-US", "zh", "en"),
        ("en_GB", "zh", "en"),
        ("de", "zh", "de"),
        ("de-DE", "zh", "de"),
        ("de_AT", "zh", "de"),
        ("ja", "zh", "ja"),
        ("ja-JP", "zh", "ja"),
        ("unsupported", "en-US", "en"),
        ("unsupported", "unsupported-default", "zh"),
        (None, "zh-CN", "zh"),
    ],
)
def test_http_asr_normalizes_supported_language_aliases(
    language,
    default,
    expected,
):
    assert asr_route._normalize_asr_language(
        language,
        default=default,
    ) == expected


@pytest.mark.parametrize(
    ("provider_mode", "enabled", "language", "expected"),
    [
        ("local", True, "zh", ""),
        ("local", True, "en", ""),
        ("local", True, "de", ""),
        ("local", True, "ja", ""),
        ("local", True, "fr", ""),
        ("local", True, "ko", ""),
        ("local", False, "zh", "zh"),
        ("local", False, "de", "de"),
        ("bosch", True, "zh", "zh"),
        ("browser", True, "en", "en"),
    ],
)
def test_local_asr_auto_detects_all_model_supported_languages(
    provider_mode,
    enabled,
    language,
    expected,
):
    settings = SimpleNamespace(
        asr_provider_mode=provider_mode,
        asr_local_language_auto_detect=enabled,
    )

    assert asr_route._asr_model_language(language, settings) == expected


def test_realtime_websocket_rejects_unauthenticated_handshake():
    client = TestClient(create_app())
    with pytest.raises(WebSocketDisconnect) as denied:
        with client.websocket_connect("/api/v1/asr/realtime"):
            pass

    assert denied.value.code == 1008
    assert denied.value.reason == "Not authenticated"


@pytest.mark.asyncio
async def test_realtime_websocket_cancel_skips_final_transcription(monkeypatch):
    events: list[dict] = []
    discarded = asyncio.Event()

    class _WebSocket:
        query_params = {"session_id": "workflow-session-cancel"}

        def __init__(self):
            self.messages = [
                {
                    "type": "websocket.receive",
                    "text": '{"type":"cancel"}',
                }
            ]
            self.close_code = None

        async def accept(self):
            return None

        async def receive(self):
            return self.messages.pop(0)

        async def send_text(self, value):
            events.append(__import__("json").loads(value))

        async def close(self, code):
            self.close_code = code

    class _Recording:
        recording_id = "recording-cancel"
        bytes_written = 0
        duration_seconds = 0.0

        def __init__(self, _settings, *, owner_key, session_id):
            assert owner_key == "user@bosch.com"
            assert session_id == "workflow-session-cancel"

        def open(self):
            return None

        def append(self, _value):
            raise AssertionError("cancel test must not append audio")

        async def close_input(self):
            raise AssertionError("cancel must skip final transcription")

        async def discard(self):
            discarded.set()

    class _Preview:
        authoritative_final = True

        def __init__(self, _settings, *, client, language):
            assert client is preview_client
            assert language == ""
            self.is_connected = False
            self.last_text = ""

        async def connect(self):
            self.is_connected = True

        async def receive_loop(self, _callback):
            await asyncio.Event().wait()

        async def append_audio(self, _value):
            return None

        async def close(self):
            self.is_connected = False

    class _SessionService:
        @staticmethod
        def get_session(session_id):
            assert session_id == "workflow-session-cancel"
            return SimpleNamespace(session_id=session_id, locale="zh-CN")

    settings = SimpleNamespace(
        asr_capture_max_connections=32,
        asr_provider_mode="local",
        asr_realtime_model="qwen3-asr-local",
    )
    preview_client = object()
    monkeypatch.setattr(asr_route, "get_settings", lambda: settings)
    monkeypatch.setattr(
        asr_route,
        "get_asr_readiness",
        lambda *_args, **_kwargs: asyncio.sleep(0, result={"status": "ready"}),
    )
    monkeypatch.setattr(asr_route, "PcmRecording", _Recording)

    def create_preview(
        received_settings,
        *,
        local_client,
        speech_client,
        routing_key,
        language,
    ):
        return _Preview(
            received_settings,
            client=local_client,
            language=language,
        )

    monkeypatch.setattr(
        asr_route,
        "create_realtime_asr_provider",
        create_preview,
    )
    monkeypatch.setattr(
        asr_route,
        "get_shared_async_client",
        lambda _pool="model_farm": preview_client,
    )
    for metric_name in (
        "change_asr_active",
        "record_asr_audio_bytes",
        "record_asr_duration",
        "record_asr_event",
    ):
        monkeypatch.setattr(asr_route, metric_name, lambda *_args, **_kwargs: None)

    asr_route._capture_slots = None
    websocket = _WebSocket()
    await asr_route.asr_realtime(
        websocket,
        current_user=SimpleNamespace(email="user@bosch.com"),
        session_service=_SessionService(),
    )

    assert discarded.is_set()
    assert websocket.close_code == 1000
    event_types = [event["type"] for event in events]
    assert "recording_ready" in event_types
    assert not {"capture_stopped", "finalizing", "final"}.intersection(event_types)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("local_result", "expected_final"),
    [
        ("本地高质量终稿", "本地高质量终稿"),
        (None, "远程权威文本"),
        ("capacity", "远程权威文本"),
        ("bosch_hedged", "Bosch尾段终稿"),
        ("", None),
    ],
)
async def test_realtime_websocket_records_to_disk_path_and_only_final_is_authoritative(
    monkeypatch,
    local_result,
    expected_final,
):
    events: list[dict] = []
    appended: list[bytes] = []
    fallback_started = asyncio.Event()
    fallback_cancelled = asyncio.Event()

    class _WebSocket:
        def __init__(self):
            self.query_params = {
                "session_id": "workflow-session-1",
                "locale": "zh-CN",
                "language": "zh",
            }
            self.messages = [
                {"type": "websocket.receive", "bytes": b"\x00\x00" * 16000},
                {
                    "type": "websocket.receive",
                    "text": '{"type":"stop","client_stopped_at_ms":1}',
                },
            ]
            self.accepted = False
            self.close_code = None

        async def accept(self):
            self.accepted = True

        async def receive(self):
            return self.messages.pop(0)

        async def send_text(self, value):
            events.append(__import__("json").loads(value))

        async def send_json(self, value):
            events.append(value)

        async def close(self, code):
            self.close_code = code

    recording_identity: dict[str, str] = {}

    class _Recording:
        def __init__(self, _settings, *, owner_key, session_id):
            recording_identity.update(owner_key=owner_key, session_id=session_id)
            self.recording_id = "recording-1"
            self.bytes_written = 0
            self.duration_seconds = 0.0

        def open(self):
            return None

        def append(self, value):
            appended.append(value)
            self.bytes_written += len(value)
            self.duration_seconds = self.bytes_written / 32000

        async def close_input(self):
            return None

        def has_speech(self):
            return local_result != ""

        async def discard(self):
            return None

    preview_client = object()

    class _Preview:
        authoritative_final = True

        def __init__(self, _settings, *, client, language):
            assert client is preview_client
            assert language == (
                "en" if _settings.asr_provider_mode == "bosch" else ""
            )
            self.is_connected = False
            self.last_text = "临时预览"
            self.failed = False
            self.closed = asyncio.Event()

        async def connect(self):
            if local_result == "capacity":
                await asyncio.sleep(0.05)
                raise asr_route.AsrPreviewCapacityError("preview pool is full")
            self.is_connected = True

        async def receive_loop(self, callback):
            if local_result == "capacity":
                await self.closed.wait()
                return
            await callback(
                {
                    "type": "partial",
                    "text": "临时预览",
                    "preview": "临时预览",
                }
            )
            await self.closed.wait()

        async def append_audio(self, _value):
            return None

        async def finish(self):
            assert any(event.get("type") == "capture_stopped" for event in events)
            if local_result == "bosch_hedged":
                await asyncio.sleep(0.02)
            if local_result is None:
                self.failed = True
                raise asr_route.AsrConfigurationError("stream failed")
            result = "Bosch尾段终稿" if local_result == "bosch_hedged" else local_result
            self.closed.set()
            self.is_connected = False
            return {
                "text": result,
                "stable_text": result,
                "unstable_text": "",
                "correction_state": "finalized",
                "speech_detected": bool(result),
                "authoritative": bool(result),
            }

        async def close(self):
            assert any(event.get("type") == "capture_stopped" for event in events)
            self.closed.set()
            self.is_connected = False

    shared_client = object()

    class _Transcriber:
        def __init__(self, _settings, *, client):
            assert client is shared_client

        async def transcribe(self, _recording, *, language, progress):
            assert language == (
                "en" if local_result == "bosch_hedged" else ""
            )
            if local_result == "bosch_hedged":
                fallback_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    fallback_cancelled.set()
                    raise
            assert local_result in {None, "capacity"}
            await progress(1, 1)
            return "远程权威文本"

    settings = SimpleNamespace(
        asr_capture_max_connections=32,
        asr_provider_mode=(
            "bosch" if local_result == "bosch_hedged" else "local"
        ),
        asr_bosch_realtime_url="",
        asr_bosch_verified_stream_final_enabled=True,
        asr_bosch_final_hedge_delay_ms=1,
        asr_local_final_enabled=True,
        asr_realtime_model="qwen3-asr-local",
        asr_language="zh",
        asr_http_model="qwen3-asr-flash",
        asr_remote_fallback_on_stream_failure=True,
    )
    readiness_calls: list[bool] = []

    async def _get_asr_readiness(received_settings, *, force_refresh=False):
        assert received_settings is settings
        readiness_calls.append(force_refresh)
        return {"status": "ready"}

    metric_calls: list[tuple] = []
    monkeypatch.setattr(asr_route, "get_settings", lambda: settings)
    monkeypatch.setattr(asr_route, "get_asr_readiness", _get_asr_readiness)
    monkeypatch.setattr(asr_route, "PcmRecording", _Recording)

    def create_preview(
        received_settings,
        *,
        local_client,
        speech_client,
        routing_key,
        language,
    ):
        return _Preview(
            received_settings,
            client=local_client,
            language=language,
        )

    monkeypatch.setattr(
        asr_route,
        "create_realtime_asr_provider",
        create_preview,
    )
    monkeypatch.setattr(asr_route, "RemoteAsrTranscriber", _Transcriber)
    monkeypatch.setattr(
        asr_route,
        "get_shared_async_client",
        lambda pool_name="model_farm": (
            preview_client
            if pool_name == "local_inference"
            else shared_client
            if pool_name == "speech"
            else pytest.fail(f"unexpected HTTP client pool: {pool_name}")
        ),
    )
    monkeypatch.setattr(
        asr_route,
        "change_asr_active",
        lambda *args, **kwargs: metric_calls.append(("active", args, kwargs)),
    )
    monkeypatch.setattr(
        asr_route,
        "record_asr_audio_bytes",
        lambda *args, **kwargs: metric_calls.append(("bytes", args, kwargs)),
    )
    monkeypatch.setattr(
        asr_route,
        "record_asr_duration",
        lambda *args, **kwargs: metric_calls.append(("duration", args, kwargs)),
    )
    monkeypatch.setattr(
        asr_route,
        "record_asr_event",
        lambda *args, **kwargs: metric_calls.append(("event", args, kwargs)),
    )
    class _SessionService:
        @staticmethod
        def get_session(session_id):
            assert session_id == "workflow-session-1"
            return SimpleNamespace(session_id=session_id, locale="en")

    asr_route._capture_slots = None
    websocket = _WebSocket()

    await asr_route.asr_realtime(
        websocket,
        current_user=SimpleNamespace(email="user@bosch.com"),
        session_service=_SessionService(),
    )

    event_types = [event["type"] for event in events]
    assert websocket.accepted is True
    assert readiness_calls == [True]
    assert websocket.close_code == 1000
    assert appended == [b"\x00\x00" * 16000]
    assert recording_identity == {
        "owner_key": "user@bosch.com",
        "session_id": "workflow-session-1",
    }
    assert all(event["session_id"] == "workflow-session-1" for event in events)
    assert all(event["recording_id"] == "recording-1" for event in events)
    assert "recording_ready" in event_types
    assert "capture_stopped" in event_types
    if local_result == "capacity":
        assert "partial" not in event_types
        capacity_event = next(
            event
            for event in events
            if event.get("code") == "preview_capacity_exceeded"
        )
        assert capacity_event["type"] == "preview_unavailable"
        assert event_types.index("recording_ready") < event_types.index(
            "preview_unavailable"
        )
    else:
        assert "partial" in event_types
        assert event_types.index("recording_ready") < event_types.index("partial")
        assert event_types.index("partial") < event_types.index("capture_stopped")
    if local_result in {None, "capacity", "bosch_hedged"}:
        assert event_types.count("final") == 1
        assert "finalizing" in event_types
        assert event_types.index("capture_stopped") < event_types.index("finalizing")
        assert event_types.index("finalizing") < event_types.index("final")
    elif local_result == "":
        assert "final" not in event_types
        assert "finalizing" not in event_types
        no_speech = next(event for event in events if event.get("code") == "no_speech_detected")
        assert no_speech["type"] == "error"
    else:
        assert event_types.count("final") == 1
        assert "finalizing" not in event_types
        assert event_types.index("capture_stopped") < event_types.index("final")
    assert "partial" not in event_types[event_types.index("capture_stopped") + 1 :]
    if expected_final is None:
        assert not any(call[0] == "active" and call[1] == ("final_transcription", 1) for call in metric_calls)
        return
    final = next(event for event in events if event["type"] == "final")
    assert final["text"] == expected_final
    assert final["text"] != "临时预览"
    if local_result == "bosch_hedged":
        assert fallback_started.is_set()
        assert fallback_cancelled.is_set()
        assert final["provider"] == "qwen3-asr-flash"
        assert final["final_source"] == "verified_stream"
    elif local_result in {None, "capacity"}:
        assert final["provider"] == "qwen3-asr-flash"
        assert final["final_source"] == "full_fallback"
    else:
        assert final["provider"] == "qwen3-asr-local"
        assert final["final_source"] == "verified_stream"
    assert ("bytes", (32000,), {}) in metric_calls

@pytest.mark.asyncio
async def test_local_qwen_two_instances_keep_each_recording_on_one_endpoint():
    requests: list[str] = []

    class _RuntimeRecycler:
        @contextmanager
        def lease(self, _service_name):
            yield

    class _Response:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class _Client:
        async def post(self, url, **_kwargs):
            requests.append(url)
            if url.endswith("/api/start"):
                return _Response({"session_id": "sticky-session"})
            if url.endswith("/api/finish"):
                return _Response({"text": "最终文本", "speech_detected": True})
            return _Response({"cancelled": True})

    settings = SimpleNamespace(
        asr_enabled=True,
        asr_realtime_enabled=True,
        asr_local_streaming_url="http://qwen3_asr:7118",
        asr_local_streaming_urls=(
            "http://qwen3_asr:7118,http://qwen3_asr_secondary:7118"
        ),
        asr_local_chunk_ms=500,
        asr_local_request_timeout_seconds=30,
        asr_connect_timeout_seconds=15,
        asr_sample_rate=16000,
        asr_local_stream_window_seconds=12,
        asr_local_final_timeout_seconds=20,
        asr_language="zh",
    )
    proxy = RealtimeAsrProxy(
        settings,
        runtime_recycler=_RuntimeRecycler(),
        client=_Client(),
        routing_key="recording-42",
    )

    await proxy.connect()
    selected_base = requests[0].removesuffix("/api/start")
    assert selected_base in {
        "http://qwen3_asr:7118",
        "http://qwen3_asr_secondary:7118",
    }
    await proxy.finish()

    assert requests == [
        f"{selected_base}/api/start",
        f"{selected_base}/api/finish",
    ]
