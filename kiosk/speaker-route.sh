#!/usr/bin/env bash
# TAS5782M speaker path for CD-18781Y. amixer only — never poke /dev/mem.
# Stock pmos-audio-route turns PRI_MI2S_RX on and QUAT_MI2S_RX off; TAS is
# on Quaternary MI2S. Mask that unit and run this instead.
set -u

CARD="${CARD:-0}"
AMP="${AMP:-50}"
ok=0
fail=0

cset() {
  local name="$1"
  local value="$2"
  if amixer -c "$CARD" cset "name=$name" "$value" >/dev/null 2>&1; then
    echo "ok  $name=$value"
    ok=$((ok + 1))
  else
    echo "fail $name=$value" >&2
    fail=$((fail + 1))
  fi
}

cset "QUAT_MI2S_RX Audio Mixer MultiMedia1" 1
cset "PRI_MI2S_RX Audio Mixer MultiMedia1" 0
cset "Headphone Jack Switch" on
cset "Speaker Amp Master Playback Volume" "${AMP},${AMP}"

echo "speaker-route card=$CARD amp=$AMP ok=$ok fail=$fail"
# QUAT is the TAS path; treat that miss as fatal.
amixer -c "$CARD" cget "name=QUAT_MI2S_RX Audio Mixer MultiMedia1" >/dev/null 2>&1
