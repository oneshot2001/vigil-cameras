# vigil-cameras

Live IP camera tiles inside a Claude Code pane, with the camera's own analytics boxes drawn on them. A Python publisher (`libexec/campub.py`) pulls the camera's RTSP substream (its own RTSP client and PyAV decode when boxes are on, ffmpeg when they are off), burns the ONVIF analytics boxes into each frame, writes the frame to a fresh POSIX shared-memory object, and the pane mod swaps a keyed `Image` to it with `$.ui.blit`. The terminal reads the pixels itself; nothing crosses the model.

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
- `/cam boxes [<id>…] live|aligned|off` sets the analytics-boxes mode for those tiles (every tile when no id is given, and that becomes the mode new tiles start in). The tiles restart (about 3 s black).
  - `live` (default): video at the lowest latency; the newest ONVIF analytics document's boxes are drawn on it, so a box trails a moving object by the camera's analytics delay (about 0.5 s on an Axis camera). The header shows how old the boxes are.
  - `aligned`: each frame is held until its own analytics document has arrived (about 0.6 s on top of the live latency) and only that document's boxes are drawn, so a box sits on the object it was computed for. Axis analytics run at 10 documents a second against a 15 fps stream, so two frames in three carry the previous document's boxes (at most 67 ms old); the publisher counts those as held.
  - `off`: no boxes; ffmpeg decodes, as in v0.2.
  - Box colour by class: person green, vehicle orange, anything else white. No labels yet.
- `/cam off` stops every stream and closes the pane. Closing the pane by hand does the same.
- The status line shows each tile's blit rate or state; each tile's header row shows camera, state, boxes mode, object count.

## Needs

- A terminal that draws kitty graphics from shared memory: kitty, Ghostty, cmux. Not tmux, ssh, iTerm2 or the desktop app.
- `python3` ≥ 3.13 on PATH and `~/.local/bin/ffmpeg` (override with `VIGIL_FFMPEG`).
- A camera registry at `~/Projects/onvif-mcp/cameras.json` (override with `VIGIL_CAMERAS_JSON`): `{ "<id>": { "base": "http://<ip>", "user": "...", "credKey": "..." } }`.
- `~/.claude/bin/cred get <credKey>` returning the camera password. With boxes off the password goes to ffmpeg through a 0600 file that is deleted once the stream is live; with boxes on it never leaves the publisher process (RTSP digest computed in-process, core dumps disabled). It is never in argv.
- For boxes: PyAV (`pip3 install av`; on a Homebrew Python add `--user --break-system-packages`). Without it a tile says so in the debug log and runs without boxes. The camera must offer an ONVIF analytics metadata track on the same RTSP session (Axis: `analytics=polygon`); the publisher reads the camera capture time from the RTP `0xABAC` header extension (`onvifreplayext=1`) to match documents to frames.

## Status

v0.3.1: grid of tiles (2 columns up to 4 cameras, 3 beyond; six tiles verified 2026-11-01 at ~11 % of one core), one publisher per camera, Axis RTSP URLs, ONVIF analytics boxes (`live` / `aligned` / `off`). Reconnect verified (stall → live again in ~13 s, ffmpeg exit → ~8 s). Command-to-glass latency measured at ~0.27 s with boxes live and ~0.4 s with boxes off (PTZ nudge on a Q6358-LE, 1280×720 at 15 fps, cmux 0.65.2, 2026-11-01; the figure includes the PTZ motor's own start delay). A hot-reload of the plugin closes the pane and clears the status line (the publishers die with the old module). Not done: class text labels, track IDs, non-Axis RTSP dialects, HEVC/AV1.
