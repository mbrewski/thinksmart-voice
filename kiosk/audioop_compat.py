"""Minimal audioop for Python 3.13+ when audioop-lts is not installed."""
from __future__ import annotations

import array


def lin2lin(fragment: bytes, width: int, newwidth: int) -> bytes:
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
    if width != 2:
        raise NotImplementedError("tomono width")
    samples = array.array("h")
    samples.frombytes(fragment)
    out = array.array("h")
    for i in range(0, len(samples) - 1, 2):
        v = int(samples[i] * lfactor + samples[i + 1] * rfactor)
        out.append(max(-32768, min(32767, v)))
    return out.tobytes()


def tostereo(fragment: bytes, width: int, lfactor: float, rfactor: float) -> bytes:
    if width != 2:
        raise NotImplementedError("tostereo width")
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
    state,
    weightA: int = 1,
    weightB: int = 0,
):
    del weightA, weightB
    if width != 2:
        raise NotImplementedError("ratecv width")
    if inrate == outrate:
        return fragment, state
    samples = array.array("h")
    samples.frombytes(fragment)
    frames = len(samples) // nchannels
    if frames <= 1:
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
    if width != 2 or not fragment:
        return 0
    samples = array.array("h")
    samples.frombytes(fragment[: len(fragment) - (len(fragment) % 2)])
    if not samples:
        return 0
    acc = 0
    for x in samples:
        acc += x * x
    return int((acc / len(samples)) ** 0.5)
