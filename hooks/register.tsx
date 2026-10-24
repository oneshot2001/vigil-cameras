import type { Register } from 'claude-code'

// vigil-cameras v0.1: one live camera tile in a pane.
// /cam [camera]  opens the pane and streams that camera (default q6358)
// /cam off       stops the stream and closes the pane
// Frames: libexec/campub.py (ffmpeg → fresh POSIX shm per frame) → $.ui.blit.

const PANE = 'cameras'
const DEFAULT_CAMERA = 'q6358'
const CELL_ASPECT = 2.14 // cell height ÷ cell width; no API reports it (mobile-mcp hardcodes the same)
const BLACK = { rgba: 'AAAAAA==', width: 1, height: 1 } as const // 1×1 placeholder until the first blit

type Tile = { camera: string; width: number; height: number; state: string }
let tile: Tile = { camera: DEFAULT_CAMERA, width: 640, height: 360, state: 'off' }

// One stream at a time. `child` is the live spawn stream (its return() kills the
// publisher); `running` settles when the stream loop has fully wound down.
let child: AsyncGenerator<any, any> | undefined
let running: Promise<void> | undefined
let stop = false
// Lifecycle transitions (/cam, /cam off, pane closed) run one after another.
let lifecycle: Promise<unknown> = Promise.resolve()
function serialize<T>(fn: () => Promise<T>): Promise<T> {
  const next = lifecycle.then(fn, fn)
  lifecycle = next.catch(() => undefined)
  return next
}

function fit(bodyColumns: number, bodyRows: number | undefined, w: number, h: number) {
  let columns = Math.max(1, Math.min(255, bodyColumns))
  let rows = Math.max(1, Math.round((columns * h) / (w * CELL_ASPECT)))
  const room = bodyRows ? bodyRows - 1 : undefined // one row above for the status line
  if (room && rows > room) {
    rows = Math.max(1, room)
    columns = Math.max(1, Math.min(255, Math.round((rows * w * CELL_ASPECT) / h)))
  }
  return { columns, rows }
}

async function stream($: any, camera: string) {
  stop = false
  tile = { ...tile, camera, state: 'starting' }
  $.ui.invalidate('ui.render')
  const proc = $.process.spawn({
    argv: [
      'python3',
      `${$.plugin.root}/libexec/campub.py`,
      '--camera', camera,
      '--tile', '0',
      '--size', `${tile.width}x${tile.height}`,
      '--fps', '15',
    ],
  })
  child = proc
  let buf = ''
  let blits = 0
  let denied = 0
  let since = Date.now()
  try {
    for await (const chunk of proc) {
      if (stop) break
      if (chunk.stream !== 'stdout') {
        $.ui.log(`campub: ${chunk.text}`, { to: 'debug' })
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
            key: 'cam0',
            source: { shm: msg.shm, format: 'rgb', width: msg.w, height: msg.h },
          })
          if (r && 'deny' in r) denied++
          else blits++
          const now = Date.now()
          if (now - since >= 1000) {
            $.ui.status(`cam ${tile.camera}: ${blits} fps${denied ? ` (${denied} denied)` : ''}`)
            blits = 0
            denied = 0
            since = now
          }
        } else if (msg.state) {
          tile = { ...tile, state: msg.state }
          if (msg.error) $.ui.log(`campub: ${msg.error}`, { to: 'debug' })
          $.ui.invalidate('ui.render')
          if (msg.state !== 'live') $.ui.status(`cam ${tile.camera}: ${msg.state}`)
        }
      }
    }
  } catch (err) {
    $.ui.log(`cameras: stream ended with an error: ${String(err)}`, { to: 'debug' })
  } finally {
    if (child === proc) child = undefined
    tile = { ...tile, state: 'off' }
    $.ui.status(undefined)
    $.ui.invalidate('ui.render')
  }
}

async function stopStream() {
  stop = true
  const c = child
  child = undefined
  if (c) {
    try { await c.return(undefined) } catch { /* already ended */ }
  }
  if (running) {
    try { await running } catch { /* reported in stream() */ }
  }
  running = undefined
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'cam',
      description: 'Live camera pane: /cam [camera] to watch, /cam off to stop',
      argumentHint: '[camera|off]',
    })
    return next(e)
  })

  on('command.run', { command: 'cam' }, ($, e) =>
    serialize(async () => {
      const arg = e.args.trim()
      if (arg === 'off') {
        await stopStream()
        await $.ui.close({ id: PANE })
        return { text: 'Camera pane closed.' }
      }
      const camera = arg || tile.camera || DEFAULT_CAMERA
      await stopStream()
      const opened = await $.ui.open({ id: PANE, title: 'Cameras', columns: 120 })
      running = stream($, camera).catch(() => undefined)
      return { text: `Streaming ${camera}${opened.isPlaced ? '' : ' (pane waits for room)'}.` }
    }),
  )

  on('ui.close', { id: PANE }, async ($, e, next) => {
    if (e.origin.kind !== 'plugin') void serialize(stopStream)
    return next(e)
  }).catch(($, e, next) => next(e)) // never a guard: the close goes on whatever this hook did

  on('ui.render', { component: 'Pane', requestId: PANE }, ($, e) => {
    const { Box, Text, Image } = $.ui.resolve(e) as any
    if (e.surface !== 'terminal' || !Image) {
      return <Text dimColor>Live camera tiles draw in a terminal (kitty, Ghostty, cmux).</Text>
    }
    const { columns, rows } = fit(e.props.bodyColumns, e.props.scroll?.bodyRows, tile.width, tile.height)
    return (
      <Box flexDirection="column">
        <Text dimColor>{tile.camera} · {tile.state} · {tile.width}×{tile.height}</Text>
        <Image key="cam0" source={BLACK} columns={columns} rows={rows} alt={`live view of ${tile.camera}`} />
      </Box>
    )
  })
}
