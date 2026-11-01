import type { Register } from 'claude-code'

// vigil-cameras v0.3.1: a grid of live camera tiles in a pane, with ONVIF analytics boxes.
// /cam                 every camera in the registry
// /cam <id> [<id>…]    just those (one id = single tile at 1280×720)
// /cam add <id>…       /cam rm <id>…
// /cam boxes [<id>…] live|aligned|off   boxes mode for those tiles (default: every tile); they restart
// /cam off             stop every stream and close the pane
// Frames: one libexec/campub.py per tile (RTSP → PyAV decode → boxes burned in → fresh POSIX shm per
// frame; ffmpeg instead when boxes are off) → $.ui.blit.

const PANE = 'cameras'
const CELL_ASPECT = 2.14 // cell height ÷ cell width; no API reports it (mobile-mcp hardcodes the same)
const BLACK = { rgba: 'AAAAAA==', width: 1, height: 1 } as const // 1×1 placeholder until the first blit
const SINGLE = [1280, 720] as const
const GRID = [640, 360] as const
const MODES = ['live', 'aligned', 'off'] as const
type Mode = (typeof MODES)[number]
let defaultMode: Mode = 'live' // `/cam boxes <mode>` with no ids also sets the mode new tiles start in

type Tile = {
  camera: string
  width: number
  height: number
  state: string
  boxes: Mode
  stats?: { boxes: string; objects?: number; box_age_ms?: number | null; note?: string } // the publisher's once-a-second report
  child?: AsyncGenerator<any, any> // the live spawn stream; return() kills the publisher
  running?: Promise<void>          // settles when the stream loop has wound down
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

// "q6325 · live · boxes aligned · 3 obj · 0.5 s" — the publisher reports the mode it actually runs
// (off when PyAV is missing or the camera has no metadata track).
function header(t: Tile, columns: number) {
  const parts = [t.camera, t.state]
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
  while (line.length > columns && parts.length > 2) {
    parts.pop()
    line = parts.join(' · ')
  }
  return line.slice(0, columns)
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

async function stream($: any, t: Tile) {
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
        const line = buf.slice(0, nl)
        buf = buf.slice(nl + 1)
        let msg: any
        try { msg = JSON.parse(line) } catch { continue }
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
          $.ui.invalidate('ui.render')
        }
      }
    }
  } catch (err) {
    $.ui.log(`cameras ${t.camera}: stream ended with an error: ${String(err)}`, { to: 'debug' })
  } finally {
    if (t.child === proc) t.child = undefined
    t.running = undefined
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

// Make the running tiles match `cameras`: stop what left or changed size, start what is missing.
async function apply($: any, cameras: string[]) {
  const [w, h] = sizeFor(cameras.length)
  const keep = tiles.filter(t => cameras.includes(t.camera) && t.width === w && t.height === h)
  const leaving = tiles.filter(t => !keep.includes(t))
  tiles = cameras.map(
    c =>
      keep.find(t => t.camera === c) ?? {
        camera: c, width: w, height: h, state: 'starting', stop: false, blits: 0, denied: 0, fps: '0 fps',
        boxes: leaving.find(t => t.camera === c)?.boxes ?? defaultMode, // a tile recreated at the other size keeps its mode
      },
  )
  since = Date.now() // a fresh sampling window: an idle span before this would read as a low rate
  $.ui.invalidate('ui.render')
  await Promise.all(leaving.map(stopTile))
  for (const t of tiles) {
    if (!t.running) {
      t.stop = false
      t.running = stream($, t).catch(() => undefined)
    }
  }
}

// Restart the given tiles with a new boxes mode; the others keep streaming.
async function setMode($: any, which: Tile[], mode: Mode) {
  for (const t of which) t.boxes = mode
  await Promise.all(which.map(stopTile))
  since = Date.now()
  for (const t of which) {
    t.stop = false
    t.state = 'starting'
    t.stats = undefined
    t.running = stream($, t).catch(() => undefined)
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
      description: 'Live camera pane: /cam, /cam <id>…, /cam add|rm <id>…, /cam boxes [<id>…] live|aligned|off, /cam off',
      argumentHint: '[<id>… | add <id>… | rm <id>… | boxes [<id>…] live|aligned|off | off]',
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
                  <Text dimColor>{header(t, columns)}</Text>
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
