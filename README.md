# ThinkSmart Voice

Grok Speech-to-Speech on a Lenovo ThinkSmart View (CD-18781Y). The panel opens `wss://api.x.ai/v1/realtime` itself. There is no `voice-assistant` in the path.

Flash and 6.12 hybrid boot live in [thinksmart-flash](https://github.com/mbrewski/thinksmart-flash). This repo is the kiosk that runs on the glass.

## What it does

- Cage + GTK4/WebKit face, hold-to-talk or **hey grok**
- Eve via Grok Voice (`grok-voice-latest`), clip-then-reply (`turn_detection: null`)
- `web_search` on the Voice session
- Local wake (RMS + optional Sherpa; fallback xAI `/stt`)
- Black overlay after `SCREEN_IDLE_SEC` (default 60); tap or wake turns the glass back on. User `idle-blanker` is masked so the Himax backlight is not killed.
- JPEG stills in `kiosk/faces/` (no Bloub required)

## Configure

```bash
cp kiosk.env.example kiosk.env
# set XAI_API_KEY, CLOCK_TZ
chmod 600 kiosk.env
```

`XAI_API_KEY` is on the wall unit (`0600`). Change the default `ubuntu` / `thinksmart` password first.

System prompt is [`instructions.md`](instructions.md) (`session.update` → `instructions`). Edit that file; do not bury persona in Python.

## Deploy

Panel already flashed from thinksmart-flash:

```bash
./tools/deploy-kiosk.sh ubuntu@PANEL_IPV4
```

Needs `python3-gi`, `python3-websockets`, cage, PipeWire (install-panel.sh).

## Credits

Hardware path and TAS/WirePlumber overlay follow thinksmart-flash. Voice API: [Speech to Speech](https://docs.x.ai/developers/model-capabilities/audio/speech-to-speech).
