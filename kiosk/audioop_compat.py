"""Minimal audioop for Python 3.13+ when audioop-lts is not installed."""
from __future__ import annotations

import array
import logging
from typing import Any


logger = logging.getLogger("thinksmart-audio")


def lin2lin(fragment: bytes, width: int, newwidth: int) -> bytes:
    """Convert sample format between widths (e.g., s32 stereo ↔ s16 mono)."""
    if not fragment or width == 0 or newwidth <= 0:
        raise ValueError(f"Invalid audio data for lin2lin: {len(fragment)} samples, {width}->{newwidth}")
    if width == newwidth:
        return fragment
    n = len(fragment) // width
    if width == 4 and newwidth == 2:
        out = bytearray(n * 2)
        for i in range(n):
            s = int.from_bytes(fragment[i * 4 : i * 4 + 4], "little", signed=True)
            s = max(-32768, min(32767, s >> 16))
            out[i * 2 : i * 2 + 2] = s.to_bytes(2, "little", signed=True)
        return bytes(out)
    if width == 2 and newwidth == 4:
        out = bytearray(n * 4)
        for i in range(n):
            s = int.from_bytes(fragment[i * 2 : i * 2 + 2], "little", signed=True)
            out[i * 4 : i * 4 + 4] = (s << 16).to_bytes(4, "little", signed=True)
        return bytes(out)
    raise NotImplementedError(f"lin2lin {width}->{newwidth}")


def tomono(fragment: bytes, width: int, lfactor: float, rfactor: float) -> bytes:
    """Convert stereo PCM to mono using left/right factors."""
    if not fragment or width != 2:
        raise ValueError(f"Invalid audio data for tomono: {len(fragment)} samples")
    samples = array.array("h")
    samples.frombytes(fragment)
    out = array.array("h")
    # Process all but last sample (which may be incomplete in stereo pair)
    for i in range(0, len(samples) - 1, 2):
        v = int(samples[i] * lfactor + samples[i + 1] * rfactor)
        out.append(max(-32768, min(32767, v)))
    return out.tobytes()


def tostereo(fragment: bytes, width: int, lfactor: float, rfactor: float) -> bytes:
    """Convert mono PCM to stereo by duplicating with left/right factors."""
    if not fragment or width != 2:
        raise ValueError(f"Invalid audio data for tostereo: {len(fragment)} samples")
    samples = array.array("h")
    samples.frombytes(fragment)
    out = array.array("h")
    for s in samples:
        lv = int(s * lfactor)
        rv = int(s * rfactor)
        out.append(max(-32768, min(32767, lv)))
        out.append(max(-32768, min(32767, rv)))
    return out.tobytes()


def ratecv(
    fragment: bytes,
    width: int,
    nchannels: int,
    inrate: int,
    outrate: int,
    state: Any,
    weightA: int = 1,
    weightB: int = 0,
) -> tuple[bytes, Any]:
    """Resample audio from one sample rate to another using linear interpolation."""
    del weightA, weightB  # unused parameters for compatibility with original API
    if not fragment or width != 2 or nchannels <= 0 or inrate <= 0 or outrate <= 0:
        raise ValueError(f"Invalid arguments for ratecv")
    if inrate == outrate:
        return fragment, state
    samples = array.array("h")
    try:
        samples.frombytes(fragment)
    except (ValueError, OverflowError):
        logger.warning(f"Failed to decode audio data of {len(fragment)} bytes for resampling")
        return fragment, state
    frames = len(samples) // nchannels
    if frames <= 1:
        # Not enough samples for meaningful resampling; pass through unchanged
        return fragment, state
    out_frames = max(1, int(frames * outrate / inrate))
    out = array.array("h")
    for i in range(out_frames):
        src = i * (frames - 1) / max(1, out_frames - 1)
        i0 = int(src)
        frac = src - i0
        i1 = min(i0 + 1, frames - 1)
        for ch in range(nchannels):
            a = samples[i0 * nchannels + ch]
            b = samples[i1 * nchannels + ch]
            out.append(int(a + (b - a) * frac))
    return out.tobytes(), state


def rms(fragment: bytes, width: int) -> int:
    """Calculate RMS (root mean square) amplitude of audio data."""
    if not fragment or len(fragment) % 2 != 0 or width != 2:
        return 0
    samples = array.array("h")
    try:
        # Truncate to even number of bytes for valid sample pairs
        trunc_bytes = len(fragment) - (len(fragment) % 2)
        truncated_data = fragment[:trunc_bytes] if trunc_bytes > 0 else b""
        samples.frombytes(truncated_data)
    except (ValueError, OverflowError):
        logger.warning(f"Failed to decode audio data of {len(fragment)} bytes for RMS calculation")
        return 0
    acc = 0
    for x in samples:
        acc += x * x
    return int((acc / len(samples)) ** 0.5)
