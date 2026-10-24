# vigil-cameras

Live IP camera tiles inside a Claude Code pane. A Python publisher (`libexec/campub.py`) runs ffmpeg against the camera's RTSP substream, writes each decoded frame to a fresh POSIX shared-memory object, and the pane mod swaps a keyed `Image` to it with `$.ui.blit`. The terminal reads the pixels itself; nothing crosses the model.

## Use

- `/cam` or `/cam <camera-id>` opens the Cameras pane and streams that camera (default `q6358`).
- `/cam off` stops the stream and closes the pane.
- The status line shows the blit rate; the pane's first row shows camera, state and tile size.

## Needs

- A terminal that draws kitty graphics from shared memory: kitty, Ghostty, cmux. Not tmux, ssh, iTerm2 or the desktop app.
- `python3` ≥ 3.13 on PATH and `~/.local/bin/ffmpeg` (override with `VIGIL_FFMPEG`).
- A camera registry at `~/Projects/onvif-mcp/cameras.json` (override with `VIGIL_CAMERAS_JSON`): `{ "<id>": { "base": "http://<ip>", "user": "...", "credKey": "..." } }`.
- `~/.claude/bin/cred get <credKey>` returning the camera password. The password goes to ffmpeg through a 0600 file that is deleted once the stream is live; it is never in argv.

## Status

v0.1: one tile, Axis RTSP URLs. Planned: grid of tiles, reconnect tuning, Vigil-native decode and ONVIF analytics boxes.
