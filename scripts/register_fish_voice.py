from __future__ import annotations

import argparse
from array import array
import json
import math
from pathlib import Path
import re
import sys
import wave

import httpx


_VOICE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_MAX_UPLOAD_BYTES = 10 * 1024 * 1024

# Reference recordings are used as a speaker prompt, so accepting an empty or
# badly clipped file is worse than rejecting a registration early.  These are
# deliberately conservative, codec-independent gates (the Fish service does
# not expose a reference-audio quality score).
_MIN_RMS = 0.008  # approximately -42 dBFS
_MIN_ACTIVE_VOICE_RATIO = 0.10
_MAX_CLIPPING_RATIO = 0.01
_VAD_FRAME_SECONDS = 0.020


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Register a consented natural reference voice in vLLM-Omni Fish Audio.",
    )
    parser.add_argument("--name", required=True, help="Stable ASCII voice name used by TTS_DEFAULT_VOICE.")
    parser.add_argument("--audio", required=True, type=Path, help="Clean mono PCM WAV reference recording.")
    parser.add_argument("--ref-text", required=True, help="Exact transcript of the reference recording.")
    parser.add_argument("--consent", required=True, help="Internal consent or recording approval identifier.")
    parser.add_argument(
        "--description",
        default="natural employee conversation voice",
        help="Non-sensitive voice description stored with the reference.",
    )
    parser.add_argument(
        "--endpoint",
        default="http://127.0.0.1:7119",
        help="Fish Audio server base URL or /v1/audio/speech URL.",
    )
    return parser


def inspect_reference_wav(path: Path) -> dict[str, int | float]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise ValueError(f"Reference audio does not exist: {resolved}")
    if resolved.suffix.casefold() != ".wav":
        raise ValueError("Reference audio must be an uncompressed PCM WAV file.")
    size = resolved.stat().st_size
    if size <= 44 or size > _MAX_UPLOAD_BYTES:
        raise ValueError("Reference WAV must be non-empty and no larger than 10 MiB.")

    try:
        with wave.open(str(resolved), "rb") as wav_file:
            channels = wav_file.getnchannels()
            sample_width = wav_file.getsampwidth()
            sample_rate = wav_file.getframerate()
            frame_count = wav_file.getnframes()
            compression = wav_file.getcomptype()
            raw_audio = wav_file.readframes(frame_count)
    except (EOFError, wave.Error) as exc:
        raise ValueError("Reference audio is not a valid PCM WAV file.") from exc

    if compression != "NONE" or channels != 1 or sample_width != 2:
        raise ValueError("Reference WAV must be mono, uncompressed, 16-bit PCM.")
    if sample_rate < 24_000:
        raise ValueError("Reference WAV sample rate must be at least 24 kHz.")
    duration_seconds = frame_count / sample_rate if sample_rate else 0.0
    if duration_seconds < 5.0 or duration_seconds > 30.0:
        raise ValueError("Reference speech duration must be between 5 and 30 seconds.")

    # The format gate above guarantees 16-bit mono PCM.  Compute a small set
    # of transparent diagnostics without pulling in numpy/librosa.  The
    # active ratio is an energy-based VAD proxy, not a phoneme recognizer; it
    # catches empty, truncated and mostly-silent recordings while remaining
    # deterministic in the registration script and CI.
    if len(raw_audio) % 2:
        raise ValueError("Reference WAV contains an incomplete 16-bit PCM sample.")
    samples = array("h")
    samples.frombytes(raw_audio)
    if sys.byteorder != "little":
        samples.byteswap()
    if not samples:
        raise ValueError("Reference WAV contains no audio samples.")

    sum_squares = sum(float(sample) * float(sample) for sample in samples)
    rms_normalized = math.sqrt(sum_squares / len(samples)) / 32768.0
    peak_normalized = max(abs(sample) for sample in samples) / 32768.0
    clipping_samples = sum(abs(sample) >= 32760 for sample in samples)
    clipping_ratio = clipping_samples / len(samples)

    frame_size = max(1, int(round(sample_rate * _VAD_FRAME_SECONDS)))
    vad_threshold = max(_MIN_RMS, rms_normalized * 0.25)
    active_frames = 0
    frame_count_for_vad = 0
    for offset in range(0, len(samples), frame_size):
        frame = samples[offset : offset + frame_size]
        if not frame:
            continue
        frame_rms = math.sqrt(
            sum(float(sample) * float(sample) for sample in frame) / len(frame)
        ) / 32768.0
        frame_count_for_vad += 1
        if frame_rms >= vad_threshold:
            active_frames += 1
    active_voice_ratio = active_frames / frame_count_for_vad if frame_count_for_vad else 0.0

    if rms_normalized < _MIN_RMS:
        raise ValueError(
            "Reference WAV appears silent or too quiet "
            f"(RMS {20 * math.log10(max(rms_normalized, 1e-12)):.1f} dBFS)."
        )
    if clipping_ratio > _MAX_CLIPPING_RATIO:
        raise ValueError(
            "Reference WAV is excessively clipped "
            f"({clipping_ratio:.1%} clipped samples; maximum {_MAX_CLIPPING_RATIO:.1%})."
        )
    if active_voice_ratio < _MIN_ACTIVE_VOICE_RATIO:
        raise ValueError(
            "Reference WAV contains too little active speech "
            f"({active_voice_ratio:.1%} energy-VAD ratio; minimum {_MIN_ACTIVE_VOICE_RATIO:.1%})."
        )

    return {
        "bytes": size,
        "sample_rate": sample_rate,
        "duration_seconds": round(duration_seconds, 3),
        "rms_dbfs": round(20 * math.log10(max(rms_normalized, 1e-12)), 2),
        "peak_dbfs": round(20 * math.log10(max(peak_normalized, 1e-12)), 2),
        "clipping_ratio": round(clipping_ratio, 6),
        "active_voice_ratio": round(active_voice_ratio, 6),
    }


def voices_url(endpoint: str) -> str:
    base = endpoint.strip().rstrip("/")
    suffix = "/v1/audio/speech"
    if base.endswith(suffix):
        base = base[: -len(suffix)]
    if base.endswith("/v1/audio/voices"):
        return base
    return f"{base}/v1/audio/voices"


def register_voice(args: argparse.Namespace) -> dict[str, object]:
    name = str(args.name).strip()
    if not _VOICE_NAME.fullmatch(name):
        raise ValueError("Voice name must use 1-64 ASCII letters, digits, dots, underscores, or hyphens.")
    ref_text = str(args.ref_text).strip()
    if not ref_text:
        raise ValueError("The exact reference transcript cannot be empty.")
    consent = str(args.consent).strip()
    if not consent:
        raise ValueError("A consent identifier is required.")

    audio_path = Path(args.audio).expanduser().resolve()
    audio_info = inspect_reference_wav(audio_path)
    endpoint = voices_url(str(args.endpoint))
    with audio_path.open("rb") as audio_file, httpx.Client(timeout=120.0) as client:
        response = client.post(
            endpoint,
            data={
                "name": name,
                "consent": consent,
                "ref_text": ref_text,
                "speaker_description": str(args.description).strip(),
            },
            files={"audio_sample": (audio_path.name, audio_file, "audio/wav")},
        )
        response.raise_for_status()
        listing = client.get(endpoint)
        listing.raise_for_status()
        payload = listing.json()

    registered = {
        str(item.get("name") or "").casefold()
        for item in payload.get("uploaded_voices", [])
        if isinstance(item, dict)
    }
    if name.casefold() not in registered:
        raise RuntimeError("Fish Audio accepted the upload but did not expose the registered voice.")
    return {
        "event": "fish_voice_registered",
        "voice": name,
        **audio_info,
        "next_environment": {
            "TTS_DEFAULT_VOICE": name,
            "TTS_AVAILABLE_VOICES": name,
        },
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = register_voice(args)
    except (ValueError, RuntimeError, httpx.HTTPError) as exc:
        print(f"fish_voice_registration_failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
