# vigil-cameras

Live IP camera tiles inside a Claude Code pane. A Python publisher (`libexec/campub.py`) runs ffmpeg against the camera's RTSP substream, writes each decoded frame to a fresh POSIX shared-memory object, and the pane mod swaps a keyed `Image` to it with `$.ui.blit`. The terminal reads the pixels itself; nothing crosses the model.

## Install

At the prompt of a Claude Code terminal session:

```
/plugin install vigil-cameras --marketplace oneshot2001/vigil-cameras
```

Answer `y` to add the marketplace, then pick a scope. From a shell: `claude plugin install vigil-cameras --marketplace oneshot2001/vigil-cameras`. The repository is private: installing needs read access on GitHub and a git credential for github.com on the machine (`gh auth login` then `gh auth setup-git`).

While developing: `claude --plugin-dir /path/to/vigil-cameras`.

## Use

- `/cam` opens the Cameras pane with every camera in the registry, in a grid (2 columns up to 4 tiles, 3 beyond) at 640×360 per tile.
- `/cam <id> [<id>…]` shows just those cameras; a single camera draws one tile at 1280×720.
- `/cam add <id>…` and `/cam rm <id>…` change the set without restarting the tiles that stay, except when the count crosses one camera: the tile resolution changes, so the stayer restarts (about 5 s black).
- `/cam off` stops every stream and closes the pane. Closing the pane by hand does the same.
- The status line shows each tile's blit rate or state; each tile's header row shows camera and state.

## Needs

- A terminal that draws kitty graphics from shared memory: kitty, Ghostty, cmux. Not tmux, ssh, iTerm2 or the desktop app.
- `python3` ≥ 3.13 on PATH and `~/.local/bin/ffmpeg` (override with `VIGIL_FFMPEG`).
- A camera registry at `~/Projects/onvif-mcp/cameras.json` (override with `VIGIL_CAMERAS_JSON`): `{ "<id>": { "base": "http://<ip>", "user": "...", "credKey": "..." } }`.
- `~/.claude/bin/cred get <credKey>` returning the camera password. The password goes to ffmpeg through a 0600 file that is deleted once the stream is live; it is never in argv.

## Status

v0.2.1: grid of tiles, one publisher per camera, Axis RTSP URLs. Reconnect verified (stall → live again in ~13 s, ffmpeg exit → ~8 s). Command-to-glass latency measured at ~0.4 s (PTZ nudge on a Q6358-LE, 1280×720 at 15 fps, cmux 0.65.2, 2026-11-01; the figure includes the PTZ motor's own start delay), down from ~1.45 s before the decoder ran with `low_delay` (no frame threading). Planned: Vigil-native decode and ONVIF analytics boxes.
