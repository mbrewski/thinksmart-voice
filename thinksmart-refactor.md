# ThinkSmart Voice — refactor notes

A prioritized review of the kiosk app as it stands. No code changes implied; these are suggestions.

## Top 3

### 1. De-duplicate the two audio sidecars

`kiosk/thinksmart-face.py` and `kiosk/thinksmart-wake.py` duplicate:

- `_load_env_file` — `face.py:37`, `wake.py:27`
- `_env` — `face.py:159`, `wake.py:73`
- `_log` (including rotation) — `face.py:166`, `wake.py:80`
- `_rms` — `face.py:336`, `wake.py:109`
- `_pcm_wav` — `face.py:326`, `wake.py:123`
- `MIC`, `RATE_IN`, `RATE_OUT`, `BYTES_100MS`
- the `pw-record → lin2lin → tomono → ratecv` capture chain — `face.py:551`, `wake.py:315`

This already caused a bug: the face log was being written to `/tmp/kiosk-wake.log` (now `kiosk-face.log`, `face.py:169`).

Extract `thinksmart_audio.py` and `thinksmart_config.py` (or a small package) and have both entry points import them.

### 2. Break up `thinksmart-face.py` (1148 lines)

It currently mixes:

- GTK application/UI
- HTTP server and proxies
- microphone capture
- TTS playback and resampling
- Home Assistant login/token injection
- mute and blank watchers
- an implicit state machine

Module globals `_mode`, `_result`, `_followup_depth`, `_followup_skip`, `_take_hold`, `_mic_muted`, `_play_proc`, `_play_vol_restore` (`face.py:126-137`) are mutated from several threads, and some writes bypass `_lock` (e.g. `_followup_depth` at 685/694/708/731, `_play_vol_restore` at 516).

Introduce a `KioskState` object that owns the lock and exposes explicit transitions. Then split modules along the list above.

### 3. Security

- The panel still accepts the default password `thinksmart` (verified by logging in). Change it, or disable `PasswordAuthentication` now that key auth works.
- `Handler` serves `directory=HERE` (`face.py:793`). In the installed layout that directory contains `kiosk.env` (the xAI key), sources, and `instructions.md`. The bind is localhost-only, but any local process/WebView page can fetch `/kiosk.env`. Serve a whitelisted `static/` directory instead.
- Use exact route matching: `startswith("/state")` (`face.py:802-835`) also matches `/statefoo`.
- `wake.py:157-174` passes the API key on the `curl` argv, visible in `ps`. Use `urllib`/`http.client` with an `Authorization` header.
- Predictable `/tmp/kiosk-*.wav` speech files (`face.py:91,497-498`, `wake.py:147`). Use `tempfile` in a 0700 dir with mode 0600.
- `install-panel.sh` copies `kiosk.env` into `$DEST` (which is HTTP-served) as well as `/etc`. Keep only `/etc/thinksmart-kiosk.env`.

## Performance

- `_rms` is a pure-Python loop over 16k samples every 100 ms in both processes (`face.py:336`, `wake.py:109`). `audioop.rms(pcm, 2)` is C and is available (the panel runs Python 3.12); numpy is installed too. This is likely a large share of the wake process's CPU.
- `pw-record` requests 48 kHz stereo s32, then converts to 16 kHz mono s16 (`face.py:551`, `wake.py:315`). Ask PipeWire for `--rate=16000 --channels=1 --format=s16` and delete `lin2lin`/`tomono`/`ratecv`. Re-tune the RMS thresholds afterward.
- `?v=44` cache-busting is duplicated (`face.py:90`, the HTML `<script>` tag) and redundant because responses set `Cache-Control: no-store`. Centralize or remove it.

## Config / deploy drift

- README says `cp kiosk.env.example kiosk.env` at the repo root, but deploy tars only `kiosk/` and `install-panel.sh` reads `kiosk/kiosk.env`. Root `kiosk.env.example` is dead, and the two examples have already diverged.
- Two `instructions.md` files (root and `kiosk/`); the loader tries both (`voice.py:49-60`). Pick one.
- `install-panel.sh` masks `i2c1-pinctrl-fix.service`, but the unit actually failing on the panel is `qup-i2c-pinctrl-fix.service`.
- `sherpa_onnx` and the `kws/` model are not installed by `install-panel.sh`; wake silently degrades to STT-only (`kws skipped`).
- Personal keyterms/cities are hardcoded (`voice.py:68-70,121-130`); move them to env/config.

## Cleanup / testability

- `voice.py:171` uses `__import__("re")` twice — just `import re`.
- `asyncio.get_event_loop()` inside coroutines (`voice.py:240,267`) → `get_running_loop()`.
- Hyphenated filenames (`thinksmart-face.py`, `thinksmart-wake.py`) can't be imported. Rename to underscore modules with thin `__main__` wrappers so pytest can import them; add tests for `_is_wake`, `_usable_transcript`, resampling, `_collect` with a fake websocket, and state transitions.
- Import-time side effects — `SystemExit` (`face.py:85-86`), environment mutation, `os.chdir` (`face.py:1134`, `wake.py:316`) — block testing and make path handling order-dependent. Move them into `main()`/a config object.
- No lint/CI. Add `ruff` plus `python -m compileall` (optionally `mypy`) as a `make check`.
- The watchdog restarts wake whenever `pw-record` is missing (`thinksmart-watchdog.sh`). A `WatchdogSec`/`sd_notify` health check is sturdier than the busy-file heuristic.
- The JS `player` element is unused (playback is server-side `pw-play`), and the HTML/JS face is bypassed entirely when `HA_DASHBOARD_URL` is set (`face.py:1057`). Either delete the static UI or make it an explicit fallback mode.

## Suggested first slice

Items 1, 2, and the `_rms` change are low-risk and high-leverage, and they make the remaining work easier.
