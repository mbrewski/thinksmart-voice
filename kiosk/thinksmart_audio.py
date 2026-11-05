"""Capture helpers. Default is 16 kHz s16 mono from PipeWire; 48 kHz s32 stereo is THINKSMART_CAPTURE=48k."""
from __future__ import annotations

import importlib
import io
import os
import wave
from dataclasses import dataclass

from thinksmart_config import MIC, RATE_IN, RATE_OUT, pipewire_env

_AUDIOOP_CANDIDATES = ("audioop", "audioop_lts", "audioop_compat")


def _import_audioop():
    last: ImportError | None = None
    for name in _AUDIOOP_CANDIDATES:
        try:
            return importlib.import_module(name)
        except ImportError as exc:
            last = exc
    raise ImportError(
        "need audioop, audioop_lts, or audioop_compat"
    ) from last


audioop = _import_audioop()

_CAPTURE_NATIVE = "16k"
_CAPTURE_LEGACY_48K = "48k"


@dataclass(frozen=True)
class CaptureProfile:
    """Wire format for pw-record plus whether ingest must downsample to RATE_OUT."""

    rate: int
    channels: int
    sample_format: str
    sample_width: int
    downsample: bool

    def chunk_bytes(self, window_s: float = 0.1) -> int:
        return int(self.rate * self.channels * self.sample_width * window_s)


def capture_profile() -> CaptureProfile:
    mode = (os.environ.get("THINKSMART_CAPTURE") or _CAPTURE_NATIVE).strip().lower()
    if mode == _CAPTURE_LEGACY_48K:
        return CaptureProfile(
            rate=RATE_IN,
            channels=2,
            sample_format="s32",
            sample_width=4,
            downsample=True,
        )
    return CaptureProfile(
        rate=RATE_OUT,
        channels=1,
        sample_format="s16",
        sample_width=2,
        downsample=False,
    )


def capture_16k() -> bool:
    return not capture_profile().downsample


def rms(pcm: bytes) -> int:
    n = len(pcm) // 2
    if n <= 0:
        return 0
    return audioop.rms(pcm, 2)


def pcm_wav(pcm: bytes, rate: int = RATE_OUT) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm)
    return buf.getvalue()


def downsample_s32_stereo_48k(raw: bytes, rv_state):
    s16 = audioop.lin2lin(raw, 4, 2)
    mono = audioop.tomono(s16, 2, 0.5, 0.5)
    return audioop.ratecv(mono, 2, 1, RATE_IN, RATE_OUT, rv_state)


def chunk_bytes() -> int:
    return capture_profile().chunk_bytes()


def ingest(raw: bytes, rv_state):
    if capture_profile().downsample:
        return downsample_s32_stereo_48k(raw, rv_state)
    return raw, rv_state


def pw_record_argv(latency: str = "50ms") -> list[str]:
    raw_target = os.environ.get("THINKSMART_MIC")
    target = MIC if raw_target is None else raw_target.strip()
    profile = capture_profile()
    argv = [
        "/usr/bin/pw-record",
        f"--rate={profile.rate}",
        f"--channels={profile.channels}",
        f"--format={profile.sample_format}",
        f"--latency={latency}",
    ]
    if target:
        argv += ["--target", target]
    return argv + ["-"]


def write_private(path: str, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def pipewire_env_copy() -> dict[str, str]:
    return pipewire_env()
