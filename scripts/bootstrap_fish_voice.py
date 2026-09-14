from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
import tempfile
import wave

import httpx

try:
    from scripts.register_fish_voice import inspect_reference_wav, register_voice, voices_url
except ModuleNotFoundError:  # Direct execution: python scripts/bootstrap_fish_voice.py
    from register_fish_voice import inspect_reference_wav, register_voice, voices_url


SYNTHETIC_CONSENT = "synthetic-generated-no-human-source"
BOOTSTRAP_SEED = 42
DEFAULT_VOICE_NAME = "employee-natural"
DEFAULT_REFERENCE_TEXT = (
    "你好，我会认真听取你的反馈，也会结合具体事实说明我的理解和疑问。"
    "接下来，我们可以一起确认工作目标、改进重点、需要的支持，以及清晰可执行的后续行动安排。"
)
_VOICE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_MAX_AUDIO_BYTES = 10 * 1024 * 1024
_PCM_CONTENT_TYPES = frozenset(
    {
        "application/octet-stream",
        "audio/l16",
        "audio/pcm",
        "audio/raw",
    }
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Idempotently bootstrap a fixed Fish Audio reference voice from "
            "model-generated speech; no human voice source is used."
        ),
    )
    parser.add_argument(
        "--name",
        default=DEFAULT_VOICE_NAME,
        help="Stable ASCII voice name used by TTS_DEFAULT_VOICE.",
    )
    parser.add_argument(
        "--endpoint",
        default="http://127.0.0.1:7119",
        help="Fish Audio server base URL, /v1/audio/speech, or /v1/audio/voices URL.",
    )
    parser.add_argument(
        "--model",
        default="/app/checkpoints/s2-pro",
        help="Model value accepted by the Fish Audio speech endpoint.",
    )
    parser.add_argument(
        "--sample-rate",
        type=int,
        default=44_100,
        help="Sample rate used only when the server returns headerless PCM.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=120.0,
        help="Timeout for listing and synthesizing the bootstrap voice.",
    )
    return parser


def speech_url(endpoint: str) -> str:
    base = endpoint.strip().rstrip("/")
    if not base:
        raise ValueError("Fish Audio endpoint cannot be empty.")
    for suffix in ("/v1/audio/speech", "/v1/audio/voices"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    return f"{base}/v1/audio/speech"


def _voice_names(payload: object) -> set[str]:
    if not isinstance(payload, dict):
        raise ValueError("Fish Audio voice listing must be a JSON object.")

    names: set[str] = set()
    for key in ("voices", "uploaded_voices"):
        items = payload.get(key, [])
        if not isinstance(items, list):
            raise ValueError(f"Fish Audio voice listing field {key!r} must be a list.")
        for item in items:
            raw_name = item.get("name") if isinstance(item, dict) else item
            name = str(raw_name or "").strip()
            if name:
                names.add(name.casefold())
    return names


def _write_synthesized_wav(
    response: httpx.Response,
    destination: Path,
    *,
    sample_rate: int,
) -> dict[str, int | float]:
    audio = response.content
    if not audio:
        raise ValueError("Fish Audio synthesis returned an empty response.")
    if len(audio) > _MAX_AUDIO_BYTES:
        raise ValueError("Fish Audio bootstrap audio exceeds the 10 MiB reference limit.")

    is_wav = len(audio) >= 12 and audio[:4] == b"RIFF" and audio[8:12] == b"WAVE"
    if is_wav:
        destination.write_bytes(audio)
        return inspect_reference_wav(destination)

    media_type = response.headers.get("content-type", "").partition(";")[0].strip().casefold()
    if media_type not in _PCM_CONTENT_TYPES:
        raise ValueError(
            "Fish Audio synthesis did not return PCM/WAV audio "
            f"(content-type={media_type or 'missing'})."
        )
    if sample_rate < 24_000:
        raise ValueError("Bootstrap PCM sample rate must be at least 24 kHz.")
    if len(audio) % 2:
        raise ValueError("Fish Audio returned an incomplete 16-bit PCM sample.")

    with wave.open(str(destination), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(audio)
    return inspect_reference_wav(destination)


def bootstrap_voice(args: argparse.Namespace) -> dict[str, object]:
    name = str(args.name).strip()
    if not _VOICE_NAME.fullmatch(name):
        raise ValueError(
            "Voice name must use 1-64 ASCII letters, digits, dots, underscores, or hyphens."
        )
    endpoint = str(args.endpoint).strip()
    model = str(args.model).strip()
    if not model:
        raise ValueError("Fish Audio model cannot be empty.")
    sample_rate = int(args.sample_rate)
    timeout_seconds = float(args.timeout_seconds)
    if timeout_seconds <= 0:
        raise ValueError("Timeout must be greater than zero seconds.")

    listing_endpoint = voices_url(endpoint)
    with httpx.Client(timeout=timeout_seconds) as client:
        listing = client.get(listing_endpoint)
        listing.raise_for_status()
        if name.casefold() in _voice_names(listing.json()):
            return {
                "event": "fish_voice_bootstrap_skipped",
                "voice": name,
                "reason": "already_registered",
                "source": SYNTHETIC_CONSENT,
            }

        synthesis = client.post(
            speech_url(endpoint),
            json={
                "model": model,
                "input": DEFAULT_REFERENCE_TEXT,
                "voice": "default",
                "stream": False,
                "response_format": "wav",
                "max_new_tokens": 1_024,
                "seed": BOOTSTRAP_SEED,
            },
        )
        synthesis.raise_for_status()

        with tempfile.TemporaryDirectory(prefix="hr-agent-fish-voice-") as temp_dir:
            audio_path = Path(temp_dir) / f"{name}.wav"
            audio_info = _write_synthesized_wav(
                synthesis,
                audio_path,
                sample_rate=sample_rate,
            )
            registration = register_voice(
                argparse.Namespace(
                    name=name,
                    audio=audio_path,
                    ref_text=DEFAULT_REFERENCE_TEXT,
                    consent=SYNTHETIC_CONSENT,
                    description="fixed synthetic employee conversation voice",
                    endpoint=endpoint,
                )
            )

    return {
        **registration,
        "event": "fish_voice_bootstrapped",
        "source": SYNTHETIC_CONSENT,
        "seed": BOOTSTRAP_SEED,
        "generated_audio": audio_info,
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = bootstrap_voice(args)
    except (ValueError, RuntimeError, httpx.HTTPError) as exc:
        print(f"fish_voice_bootstrap_failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
