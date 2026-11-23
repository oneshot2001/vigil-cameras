import type { Register } from 'claude-code'

// vigil-cameras v0.5.0: a grid of live camera tiles in a pane, with ONVIF analytics boxes.
// /cam                 every camera in the registry
// /cam <id> [<id>…]    just those (one id = single tile at 1280×720)
// /cam add <id>…       /cam rm <id>…
// /cam boxes [<id>…] live|aligned|off   boxes mode for those tiles (default: every tile)
// /cam watch <id> person|vehicle [l,t,r,b] | off   flag the tile when the camera's analytics see one arrive
// /cam off             stop every stream and close the pane
// Frames: one libexec/campub.py per tile (RTSP → PyAV decode → boxes burned in → fresh POSIX shm per
// frame; ffmpeg when PyAV is missing) → $.ui.blit. A boxes or size change goes to the publisher's
// control file (its path rides on every state/stats line); the publisher applies it on the running
// stream, or opens the new size beside the old one and hands over once it has a frame. No restarts.

const PANE = 'cameras'
const CELL_ASPECT = 2.14 // cell height ÷ cell width; no API reports it (mobile-mcp hardcodes the same)
const BLACK = { rgba: 'AAAAAA==', width: 1, height: 1 } as const // 1×1 placeholder until the first blit
const SINGLE = [1280, 720] as const
const GRID = [640, 360] as const
const MODES = ['live', 'aligned', 'off'] as const
type Mode = (typeof MODES)[number]
let defaultMode: Mode = 'live' // `/cam boxes <mode>` with no ids also sets the mode new tiles start in
const CLASSES: Record<string, string> = { person: 'Human', vehicle: 'Vehicle' } // the Axis analytics class names
const WORDS: Record<string, string> = { Human: 'person', Vehicle: 'vehicle' }
let watchRev = 0 // every watch change gets a new rev; lines carrying an older one are ignored

type Wake = { cls: string; rev: number; n: number; k: number; utc_ms: number } // k: wakes under this rev, from 1
type WatchStats = { state: string; cls: string; rev: number; wakes: number; last: Wake | null }

type Tile = {
  camera: string
  width: number
  height: number
  state: string
  boxes: Mode
  stats?: { boxes: string; objects?: number; box_age_ms?: number | null; note?: string; watch?: WatchStats | null } // the publisher's once-a-second report
  watch?: { cls: string; rect?: number[]; rev: number }
  wake?: Wake // newest wake of this publisher under the current rev (from a wake line or, if the pipe dropped it, stats)
  child?: AsyncGenerator<any, any> // the live spawn stream; return() kills the publisher
  running?: Promise<void>          // settles when the stream loop has wound down
  ctl?: string                     // the publisher's control file, once it has announced it
  ctlQueue: Promise<void>          // control writes run one after another, each with the fields as they stand when it runs
  stop: boolean
  blits: number
  denied: number
  fps: string // last sampled rate, shown between samples
}
let tiles: Tile[] = [] // display order
let since = Date.now() // the status line is rewritten once a second by whichever tile crosses it
// Lifecycle transitions (/cam …, pane closed) run one after another.
let lifecycle: Promise<unknown> = Promise.resolve()
function serialize<T>(fn: () => Promise<T>): Promise<T> {
  const next = lifecycle.then(fn, fn)
  lifecycle = next.catch(() => undefined)
  return next
}

function sizeFor(n: number) {
  return n === 1 ? SINGLE : GRID
}

function grid(n: number) {
  const cols = n <= 1 ? 1 : n <= 4 ? 2 : 3
  return { cols, rows: Math.ceil(n / cols) }
}

function fit(bodyColumns: number, bodyRows: number | undefined, n: number, w: number, h: number) {
  const g = grid(n)
  let columns = Math.max(1, Math.min(255, Math.floor((bodyColumns - (g.cols - 1)) / g.cols))) // one spacer column between tiles
  let rows = Math.max(1, Math.round((columns * h) / (w * CELL_ASPECT)))
  const room = bodyRows ? Math.floor(bodyRows / g.rows) - 1 : undefined // one header row per tile
  if (room !== undefined && rows > room) {
    rows = Math.max(1, room)
    columns = Math.max(1, Math.min(255, Math.round((rows * w * CELL_ASPECT) / h)))
  }
  return { columns, rows, cols: g.cols }
}

// One toast per new wake, whether it came as its own line or, when the publisher's stdout dropped that
// line, as stats.watch.last on the next stats line. Wakes the pipe lost entirely are counted, not toasted.
function noteWake($: any, t: Tile, w: Wake | null | undefined) {
  if (!t.watch || !w || w.rev !== t.watch.rev || (t.wake && w.k <= t.wake.k)) return
  const missed = w.k - (t.wake?.k ?? 0) - 1
  t.wake = w
  $.ui.toast(`${t.camera}: ${WORDS[w.cls] ?? w.cls} ${new Date(w.utc_ms).toTimeString().slice(0, 8)}${missed > 0 ? ` (+${missed} more)` : ''}`)
  $.ui.invalidate('ui.render')
}

// The watch part as the publisher reports it: "watching vehicle", "● vehicle 14:02:11", "watch waiting".
function watchPart(t: Tile): { text: string; alert: boolean } | undefined {
  if (!t.watch) return undefined
  const word = WORDS[t.watch.cls] ?? t.watch.cls
  const w = t.stats?.watch
  if (!w || w.rev !== t.watch.rev) return { text: `watch ${word} pending`, alert: false } // not acknowledged yet
  if (w.state !== 'on') return { text: `watch ${w.state}`, alert: false }
  const last = [t.wake, w.last].filter((x): x is Wake => !!x && x.rev === t.watch!.rev).sort((a, b) => b.k - a.k)[0]
  if (!last) return { text: `watching ${word}`, alert: false }
  return { text: `● ${word} ${new Date(last.utc_ms).toTimeString().slice(0, 8)}`, alert: true }
}

// "q6325 · live · ● vehicle 14:02:11 · boxes aligned · 3 obj · 0.5 s" — the publisher reports what it
// actually runs (boxes off when PyAV is missing or the camera has no metadata track). The watch part
// comes right after the state so a narrow tile drops box details first. Returns the line in three runs
// so the watch part can carry its own colour.
function header(t: Tile, columns: number): [string, string, string, boolean] {
  const watch = watchPart(t)
  const parts = [t.camera, t.state]
  if (watch) parts.push(watch.text)
  const fixed = parts.length // never dropped
  const s = t.stats
  if (t.state === 'live' && t.boxes !== 'off') {
    parts.push(`boxes ${s?.boxes ?? t.boxes}`)
    if (s?.boxes && s.boxes !== 'off') {
      parts.push(`${s.objects ?? 0} obj`)
      if (s.boxes === 'live' && s.box_age_ms != null) parts.push(`${(s.box_age_ms / 1000).toFixed(1)} s`)
    }
  }
  // Narrow tile: drop whole parts from the end (age, count, boxes) rather than cut a number in half or
  // leave "live · live" (the word "boxes" is what gives the second "live" its meaning).
  let line = parts.join(' · ')
  while (line.length > columns && parts.length > fixed) {
    parts.pop()
    line = parts.join(' · ')
  }
  if (!watch) return [line.slice(0, columns), '', '', false]
  const pre = `${t.camera} · ${t.state} · `
  let mid = watch.text
  if (watch.alert && pre.length + mid.length > columns) mid = mid.slice(0, -3) // narrow tile: hh:mm rather than a cut-off hh:mm:
  line = [pre + mid, ...parts.slice(fixed)].join(' · ')
  return [pre.slice(0, columns), mid.slice(0, Math.max(0, columns - pre.length)), line.slice(pre.length + mid.length, Math.max(0, columns)), watch.alert]
}

async function registry($: any): Promise<string[]> {
  const path = (await $.env.get('VIGIL_CAMERAS_JSON')) || `${await $.env.get('HOME')}/Projects/onvif-mcp/cameras.json`
  return Object.keys(JSON.parse(await $.fs.read(path)))
}

// Once a second: sample every tile's rate and rewrite the status line. `force` (a state
// change) rewrites the line at once from the last samples without touching the counters.
function status($: any, force = false) {
  const now = Date.now()
  const elapsed = now - since
  if (elapsed >= 1000) {
    since = now
    for (const t of tiles) {
      const rate = Math.round((t.blits * 1000) / elapsed)
      t.fps = `${rate} fps${t.denied ? ` (${t.denied} denied)` : ''}`
      t.blits = 0
      t.denied = 0
    }
  } else if (!force) return
  if (!tiles.length) {
    $.ui.status(undefined)
    return
  }
  $.ui.status(tiles.map(t => `${t.camera} ${t.state === 'live' ? t.fps : t.state}`).join(' · '))
}

// Write the tile's whole desired state to its publisher's control file. Writes queue per tile and
// read the fields when they run, so the announce-triggered write can never overtake a later command.
function control($: any, t: Tile) {
  const run = async () => {
    if (!t.ctl || !t.running) return
    const body = JSON.stringify({ boxes: t.boxes, size: `${t.width}x${t.height}`, watch: t.watch ?? null })
    try {
      await $.fs.write(t.ctl, body)
    } catch (err) {
      $.ui.log(`cameras ${t.camera}: control write failed, retrying once: ${String(err)}`, { to: 'debug' })
      try { await $.fs.write(t.ctl, body) } catch (err2) { $.ui.log(`cameras ${t.camera}: control write failed: ${String(err2)}`, { to: 'debug' }) }
    }
  }
  t.ctlQueue = t.ctlQueue.then(run, run)
  return t.ctlQueue
}

async function stream($: any, t: Tile) {
  t.ctl = undefined // a new publisher announces its own file
  t.stats = undefined // and counts its own wakes from 1: nothing of the last one's may outrank them
  t.wake = undefined
  const proc = $.process.spawn({
    argv: [
      'python3',
      `${$.plugin.root}/libexec/campub.py`,
      '--camera', t.camera,
      '--size', `${t.width}x${t.height}`,
      '--fps', '15',
      '--boxes', t.boxes,
    ],
  })
  t.child = proc
  let buf = ''
  try {
    for await (const chunk of proc) {
      if (t.stop) break
      if (chunk.stream !== 'stdout') {
        $.ui.log(`campub ${t.camera}: ${chunk.text}`, { to: 'debug' })
        continue
      }
      buf += chunk.text
      let nl: number
      while ((nl = buf.indexOf('\n')) >= 0) {
        if (t.stop) break // stopped during an awaited blit: the rest of this chunk is the old publisher's
        const line = buf.slice(0, nl)
        buf = buf.slice(nl + 1)
        let msg: any
        try { msg = JSON.parse(line) } catch { continue }
        if (typeof msg.ctl === 'string' && msg.ctl !== t.ctl) {
          t.ctl = msg.ctl
          void control($, t) // the state as it stands now, in case a command landed before the announcement
        }
        if (msg.shm) {
          const r = await $.ui.blit({
            requestId: PANE,
            key: `cam-${t.camera}`,
            source: { shm: msg.shm, format: 'rgb', width: msg.w, height: msg.h },
          })
          if (r && 'deny' in r) t.denied++
          else t.blits++
          status($)
        } else if (msg.state) {
          t.state = msg.state
          if (msg.error) $.ui.log(`campub ${t.camera}: ${msg.error}`, { to: 'debug' })
          $.ui.invalidate('ui.render')
          status($, true)
        } else if (msg.stats) {
          if (msg.stats.note && msg.stats.note !== t.stats?.note) $.ui.log(`campub ${t.camera}: ${msg.stats.note}`, { to: 'debug' })
          t.stats = msg.stats
          noteWake($, t, msg.stats.watch?.last)
          $.ui.invalidate('ui.render')
        } else if (msg.wake) {
          noteWake($, t, msg.wake)
        } else if (msg.swap) {
          $.ui.log(`campub ${t.camera}: swap to ${msg.swap.size}: ${msg.swap.state}${msg.swap.error ? ` (${msg.swap.error})` : ''}`, { to: 'debug' })
        }
      }
    }
  } catch (err) {
    $.ui.log(`cameras ${t.camera}: stream ended with an error: ${String(err)}`, { to: 'debug' })
  } finally {
    if (t.child === proc) t.child = undefined
    t.running = undefined
    t.ctl = undefined
    t.state = 'off'
    $.ui.invalidate('ui.render')
    status($, true)
  }
}

async function stopTile(t: Tile) {
  t.stop = true
  const c = t.child
  const r = t.running
  t.child = undefined
  if (c) {
    try { await c.return(undefined) } catch { /* already ended */ }
  }
  if (r) {
    try { await r } catch { /* reported in stream() */ }
  }
}

// Make the running tiles match `cameras`: stop what left, start what is missing, and tell a stayer
// whose size changed; its publisher swaps streams itself, so it never stops drawing.
async function apply($: any, cameras: string[]) {
  const [w, h] = sizeFor(cameras.length)
  const keep = tiles.filter(t => cameras.includes(t.camera))
  const leaving = tiles.filter(t => !keep.includes(t))
  tiles = cameras.map(
    c =>
      keep.find(t => t.camera === c) ?? {
        camera: c, width: w, height: h, state: 'starting', boxes: defaultMode, ctlQueue: Promise.resolve(),
        stop: false, blits: 0, denied: 0, fps: '0 fps',
      },
  )
  since = Date.now() // a fresh sampling window: an idle span before this would read as a low rate
  $.ui.invalidate('ui.render')
  await Promise.all(leaving.map(stopTile))
  for (const t of tiles) {
    if (t.width !== w || t.height !== h) {
      t.width = w
      t.height = h
      void control($, t)
    }
    if (!t.running) {
      t.stop = false
      t.running = stream($, t).catch(() => undefined)
    }
  }
}

// Give the given tiles a new boxes mode on their running streams; a dead tile is started again.
function setMode($: any, which: Tile[], mode: Mode) {
  for (const t of which) {
    t.boxes = mode
    if (t.running) {
      void control($, t)
    } else {
      t.stop = false
      t.state = 'starting'
      t.stats = undefined
      t.running = stream($, t).catch(() => undefined)
    }
  }
  $.ui.invalidate('ui.render')
}

async function stopAll($: any) {
  const all = tiles
  tiles = []
  await Promise.all(all.map(stopTile))
  status($, true)
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    // A hot-reload starts this module over with no tiles (the publishers die with the old module) while the
    // engine keeps the old status line and pane: clear both so nothing stale stays on screen.
    $.ui.status(undefined)
    if ((await $.ui.panes()).some((p: { id: string }) => p.id === PANE)) await $.ui.close({ id: PANE })
    await $.command.register({
      name: 'cam',
      description: 'Live camera pane: /cam, /cam <id>…, /cam add|rm <id>…, /cam boxes [<id>…] live|aligned|off, /cam watch <id> person|vehicle [l,t,r,b] | off, /cam off',
      argumentHint: '[<id>… | add <id>… | rm <id>… | boxes [<id>…] live|aligned|off | watch <id> person|vehicle [l,t,r,b] | watch <id> off | off]',
    })
    return next(e)
  })

  on('command.run', { command: 'cam' }, ($, e) =>
    serialize(async () => {
      const words = e.args.trim().split(/\s+/).filter(Boolean)
      const current = tiles.map(t => t.camera)
      let cameras: string[]
      if (words[0] === 'off') {
        await stopAll($)
        await $.ui.close({ id: PANE })
        return { text: 'Camera pane closed.' }
      }
      if (words[0] === 'boxes') {
        const mode = words[words.length - 1] as Mode
        if (words.length < 2 || !MODES.includes(mode)) return { text: 'Usage: /cam boxes [<id>…] live|aligned|off' }
        const ids = words.slice(1, -1)
        const named = ids.length ? tiles.filter(t => ids.includes(t.camera)) : tiles
        const which = named.filter(t => t.boxes !== mode || !t.running) // already in that mode and streaming: leave it alone
        const missing = ids.filter(id => !tiles.some(t => t.camera === id))
        if (missing.length) return { text: `Not on screen: ${missing.join(', ')}. Showing: ${current.join(', ') || 'nothing'}.` }
        if (!ids.length) defaultMode = mode
        await setMode($, which, mode)
        return { text: which.length ? `Boxes ${mode} on ${which.map(t => t.camera).join(', ')}.` : named.length ? `Boxes already ${mode} on ${named.map(t => t.camera).join(', ')}.` : `Boxes ${mode} for the next /cam.` }
      }
      if (words[0] === 'watch') {
        const usage = { text: 'Usage: /cam watch <id> person|vehicle [l,t,r,b] (ONVIF −1..1, y up, l<r, b<t) | /cam watch <id> off' }
        const [, id, what, rectArg] = words
        if (!id || !what || words.length > 4 || (what === 'off' && rectArg)) return usage
        const t = tiles.find(x => x.camera === id)
        if (!t) return { text: `Not on screen: ${id}. Showing: ${current.join(', ') || 'nothing'}.` }
        if (what === 'off') {
          t.watch = undefined
          t.wake = undefined
          if (t.running) void control($, t) // a stopped tile stays stopped
          $.ui.invalidate('ui.render')
          return { text: `Stopped watching ${t.camera}.` }
        } else {
          if (!Object.prototype.hasOwnProperty.call(CLASSES, what)) return usage
          const cls = CLASSES[what]!
          let rect: number[] | undefined
          if (rectArg) {
            const toks = rectArg.split(',')
            if (toks.length !== 4 || toks.some(x => x.trim() === '')) return usage
            rect = toks.map(Number)
            if (rect.length !== 4 || !rect.every(v => Number.isFinite(v) && v >= -1 && v <= 1)) return usage
            const [l, top, r, b] = rect as [number, number, number, number]
            if (!(l < r && b < top)) return usage
          }
          t.watch = { cls, rev: ++watchRev, ...(rect ? { rect } : {}) }
        }
        t.wake = undefined
        setMode($, [t], t.boxes) // writes the control file (watch rides beside boxes and size), or restarts a dead tile
        return { text: `Watching ${t.camera} for ${what}${rectArg ? ` in ${rectArg}` : ''}.` }
      }
      const known = await registry($)
      if (words[0] === 'add') cameras = [...current, ...words.slice(1)]
      else if (words[0] === 'rm') cameras = current.filter(c => !words.slice(1).includes(c))
      else if (words.length) cameras = words
      else cameras = known
      cameras = [...new Set(cameras)] // a repeated id would make two tiles share one Image key and orphan a publisher
      const unknown = cameras.filter(c => !known.includes(c))
      if (unknown.length) return { text: `Unknown camera ${unknown.join(', ')}. Registry: ${known.join(', ')}.` }
      if (!cameras.length) {
        await stopAll($)
        await $.ui.close({ id: PANE })
        return { text: 'No cameras left; pane closed.' }
      }
      const opened = await $.ui.open({ id: PANE, title: 'Cameras', columns: 120 })
      await apply($, cameras)
      const [w, h] = sizeFor(cameras.length)
      return { text: `Streaming ${cameras.join(', ')} at ${w}×${h}${opened.isPlaced ? '' : ' (pane waits for room)'}.` }
    }),
  )

  on('ui.close', { id: PANE }, async ($, e, next) => {
    if (e.origin.kind !== 'plugin') void serialize(() => stopAll($))
    return next(e)
  }).catch(($, e, next) => next(e)) // never a guard: the close goes on whatever this hook did

  on('ui.render', { component: 'Pane', requestId: PANE }, ($, e) => {
    const { Box, Text, Image } = $.ui.resolve(e) as any
    if (e.surface !== 'terminal' || !Image) {
      return <Text dimColor>Live camera tiles draw in a terminal (kitty, Ghostty, cmux).</Text>
    }
    if (!tiles.length) {
      return <Text dimColor>No cameras. /cam for all, /cam {'<id>'} for one.</Text>
    }
    const [tw, th] = sizeFor(tiles.length) // not w/h: JSX compiles to the global `h`
    const { columns, rows, cols } = fit(e.props.bodyColumns, e.props.scroll?.bodyRows, tiles.length, tw, th)
    const lines: Tile[][] = []
    for (let i = 0; i < tiles.length; i += cols) lines.push(tiles.slice(i, i + cols))
    return (
      <Box flexDirection="column">
        {lines.map((line, li) => (
          <Box key={`row${li}`} flexDirection="row">
            {line.flatMap((t, ci) => {
              const tile = (
                <Box key={`tile:${t.camera}`} flexDirection="column">
                  {(([pre, mid, post, alert]) => (
                    <Text dimColor>{pre}{mid ? <Text dimColor={!alert} color={alert ? 'yellow' : undefined}>{mid}</Text> : null}{post}</Text>
                  ))(header(t, columns))}
                  <Image key={`cam-${t.camera}`} source={BLACK} columns={columns} rows={rows} alt={`live view of ${t.camera}`} />
                </Box>
              )
              return ci ? [<Text key={`gap:${ci}`}> </Text>, tile] : [tile]
            })}
          </Box>
        ))}
      </Box>
    )
  })
}
