#!/bin/bash
# Unmute the default PipeWire source after the hardware slider is flipped on.
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/1000}"
export PULSE_SERVER="${PULSE_SERVER:-unix:/run/user/1000/pulse/native}"
wpctl set-mute @DEFAULT_AUDIO_SOURCE@ 0 2>/dev/null || true
