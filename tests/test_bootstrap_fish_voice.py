from __future__ import annotations

import argparse
import io
import json
import math
from pathlib import Path
import struct
from typing import Callable
import wave

import httpx
import pytest

from scripts import bootstrap_fish_voice


def _args(**overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "name": "employee-natural",
        "endpoint": "http://fish.test:8091",
        "model": "/app/checkpoints/s2-pro",
        "sample_rate": 44_100,
        "timeout_seconds": 5.0,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _wav_bytes(*, duration_seconds: int = 6, sample_rate: int = 44_100) -> bytes:
    output = io.BytesIO()
    samples = [
        int(
            8_000 * math.sin(2 * math.pi * 220 * index / sample_rate)
            + 1_500 * math.sin(2 * math.pi * 440 * index / sample_rate)
        )
        for index in range(sample_rate * duration_seconds)
    ]
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return output.getvalue()


def _install_mock_client(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    real_client = httpx.Client
    transport = httpx.MockTransport(handler)

    def client_factory(*args: object, **kwargs: object) -> httpx.Client:
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", client_factory)


def test_bootstrap_skips_existing_voice_without_synthesis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        return httpx.Response(
            200,
            json={
                "voices": ["employee-natural"],
                "uploaded_voices": [{"name": "employee-natural"}],
            },
        )

    _install_mock_client(monkeypatch, handler)
    monkeypatch.setattr(
        bootstrap_fish_voice,
        "register_voice",
        lambda _args: pytest.fail("existing voices must not be synthesized or registered"),
    )

    result = bootstrap_fish_voice.bootstrap_voice(_args())

    assert result == {
        "event": "fish_voice_bootstrap_skipped",
        "voice": "employee-natural",
        "reason": "already_registered",
        "source": "synthetic-generated-no-human-source",
    }
    assert requests == [("GET", "/v1/audio/voices")]


def test_bootstrap_generates_fixed_wav_and_registers_synthetic_voice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registered = False
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal registered
        requests.append(request)
        if request.method == "GET" and request.url.path == "/v1/audio/voices":
            uploaded = [{"name": "employee-natural"}] if registered else []
            return httpx.Response(200, json={"voices": [], "uploaded_voices": uploaded})
        if request.method == "POST" and request.url.path == "/v1/audio/speech":
            return httpx.Response(
                200,
                headers={"content-type": "audio/wav"},
                content=_wav_bytes(),
            )
        if request.method == "POST" and request.url.path == "/v1/audio/voices":
            body = request.content
            assert b"synthetic-generated-no-human-source" in body
            assert b"employee-natural.wav" in body
            assert b"RIFF" in body
            registered = True
            return httpx.Response(200, json={"success": True})
        return httpx.Response(404)

    _install_mock_client(monkeypatch, handler)
    original_register = bootstrap_fish_voice.register_voice
    temporary_paths: list[Path] = []

    def capture_registration(args: argparse.Namespace) -> dict[str, object]:
        audio_path = Path(args.audio)
        temporary_paths.append(audio_path)
        assert audio_path.is_file()
        return original_register(args)

    monkeypatch.setattr(bootstrap_fish_voice, "register_voice", capture_registration)

    result = bootstrap_fish_voice.bootstrap_voice(_args())

    speech_request = next(
        request for request in requests if request.url.path == "/v1/audio/speech"
    )
    assert json.loads(speech_request.content) == {
        "model": "/app/checkpoints/s2-pro",
        "input": bootstrap_fish_voice.DEFAULT_REFERENCE_TEXT,
        "voice": "default",
        "stream": False,
        "response_format": "wav",
        "max_new_tokens": 1_024,
        "seed": 42,
    }
    assert result["event"] == "fish_voice_bootstrapped"
    assert result["voice"] == "employee-natural"
    assert result["source"] == "synthetic-generated-no-human-source"
    assert result["generated_audio"]["duration_seconds"] == 6.0
    assert temporary_paths and all(not path.exists() for path in temporary_paths)


def test_bootstrap_wraps_headerless_pcm_before_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_pcm = b"".join(
        struct.pack(
            "<h",
            int(8_000 * math.sin(2 * math.pi * 220 * index / 44_100)),
        )
        for index in range(44_100 * 5)
    )
    captured_audio: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"voices": [], "uploaded_voices": []})
        if request.url.path == "/v1/audio/speech":
            return httpx.Response(
                200,
                headers={"content-type": "audio/pcm"},
                content=raw_pcm,
            )
        return httpx.Response(404)

    def capture_registration(args: argparse.Namespace) -> dict[str, object]:
        audio_path = Path(args.audio)
        captured_audio.append(audio_path.read_bytes())
        return {"event": "fish_voice_registered", "voice": args.name}

    _install_mock_client(monkeypatch, handler)
    monkeypatch.setattr(bootstrap_fish_voice, "register_voice", capture_registration)

    result = bootstrap_fish_voice.bootstrap_voice(_args())

    assert result["generated_audio"]["duration_seconds"] == 5.0
    assert captured_audio[0][:4] == b"RIFF"
    assert captured_audio[0][8:12] == b"WAVE"


def test_bootstrap_rejects_short_audio_without_registering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"voices": [], "uploaded_voices": []})
        return httpx.Response(
            200,
            headers={"content-type": "audio/wav"},
            content=_wav_bytes(duration_seconds=1),
        )

    _install_mock_client(monkeypatch, handler)
    monkeypatch.setattr(
        bootstrap_fish_voice,
        "register_voice",
        lambda _args: pytest.fail("invalid generated audio must not be registered"),
    )

    with pytest.raises(ValueError, match="between 5 and 30 seconds"):
        bootstrap_fish_voice.bootstrap_voice(_args())


def test_bootstrap_removes_temporary_audio_when_registration_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    temporary_paths: list[Path] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"voices": [], "uploaded_voices": []})
        return httpx.Response(
            200,
            headers={"content-type": "audio/wav"},
            content=_wav_bytes(),
        )

    def fail_registration(args: argparse.Namespace) -> dict[str, object]:
        audio_path = Path(args.audio)
        temporary_paths.append(audio_path)
        assert audio_path.is_file()
        raise RuntimeError("registration failed")

    _install_mock_client(monkeypatch, handler)
    monkeypatch.setattr(bootstrap_fish_voice, "register_voice", fail_registration)

    with pytest.raises(RuntimeError, match="registration failed"):
        bootstrap_fish_voice.bootstrap_voice(_args())

    assert temporary_paths and all(not path.exists() for path in temporary_paths)
