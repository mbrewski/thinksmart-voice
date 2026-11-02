"""On-device Hey Grok keyword spotter (sherpa-onnx zipformer)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENV_SITE = HERE / "venv/lib/python3.12/site-packages"
if VENV_SITE.is_dir() and str(VENV_SITE) not in sys.path:
    sys.path.insert(0, str(VENV_SITE))

KWS_DIR = HERE / "kws/sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01"
KEYWORDS = KWS_DIR / "hey_grok.txt"


def load_spotter():
    import sherpa_onnx

    encoder = KWS_DIR / "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx"
    decoder = KWS_DIR / "decoder-epoch-12-avg-2-chunk-16-left-64.onnx"
    joiner = KWS_DIR / "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx"
    tokens = KWS_DIR / "tokens.txt"
    for p in (encoder, decoder, joiner, tokens, KEYWORDS):
        if not p.is_file():
            raise FileNotFoundError(str(p))
    spotter = sherpa_onnx.KeywordSpotter(
        tokens=str(tokens),
        encoder=str(encoder),
        decoder=str(decoder),
        joiner=str(joiner),
        keywords_file=str(KEYWORDS),
        num_threads=1,
        max_active_paths=4,
        keywords_score=2.5,
        keywords_threshold=0.1,
        num_trailing_blanks=2,
        provider="cpu",
    )
    return spotter


def feed(spotter, stream, pcm16: bytes) -> str:
    if not pcm16 or spotter is None or stream is None:
        return ""
    import numpy as np

    samples = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
    if samples.size == 0:
        return ""
    stream.accept_waveform(16000, samples)
    hit = ""
    while spotter.is_ready(stream):
        spotter.decode_stream(stream)
        result = spotter.get_result(stream)
        if result:
            hit = str(result).strip()
            spotter.reset_stream(stream)
            break
    return hit
