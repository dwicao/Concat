#!/usr/bin/env node
// Drives the local-concat plugin the way dsh-go does: a fake ctx, a fake dshGo,
// and the real mock editor behind it. Verifies the socket layer, the strip text
// and the panel data without needing the app.
//
//   node test_plugin.mjs

import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import net from 'node:net'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
// The plugin ships in this repo; the installed copy is a link to it.
const PLUGIN = process.env.CONCAT_PLUGIN
  ? path.resolve(process.env.CONCAT_PLUGIN)
  : path.resolve(HERE, '..', '..', 'plugins', 'dsh-go-local-concat', 'lib', 'index.js')

const checks = []
function check(label, condition, detail = '') {
  checks.push({ label, ok: Boolean(condition), detail })
}

function freePort() {
  return new Promise((resolve, reject) => {
    const probe = net.createServer()
    probe.on('error', reject)
    probe.listen(0, '127.0.0.1', () => {
      const { port } = probe.address()
      probe.close(() => resolve(port))
    })
  })
}

const LONG_PROJECT = 'A very long project name that will not fit the strip'

function startMock() {
  return new Promise((resolve, reject) => {
    const child = spawn('python3',
      [path.join(HERE, 'mock_concat.py'), '--port', '0', '--project', LONG_PROJECT], {
      stdio: ['ignore', 'pipe', 'inherit'],
    })
    let buffer = ''
    const timer = setTimeout(() => reject(new Error('mock did not start')), 8000)
    child.stdout.on('data', (chunk) => {
      buffer += chunk
      const line = buffer.split('\n')[0]
      if (!line || !line.includes(':')) return
      clearTimeout(timer)
      const token = ['--token', process.env.MOCK_TOKEN || 'test-token-0123456789abcdef']
      resolve({ child, address: line.trim(), token: token[1] })
    })
  })
}

function loadPlugin() {
  const handlers = { actions: new Map(), strips: new Map(), panels: new Map() }
  const dshGo = {
    on: (id, handler) => handlers.actions.set(id, handler),
    strip: (id, factory) => handlers.strips.set(id, factory),
    panel: (id, spec) => handlers.panels.set(id, spec),
  }
  const ctx = { inject: (names, callback) => { assert.deepEqual(names, ['dshGo']); callback(ctx) }, dshGo }
  return import(PLUGIN).then((module) => {
    module.apply(ctx)
    return { module, handlers }
  })
}

async function readPanel(handlers, locale) {
  const panel = handlers.panels.get('concat')
  assert.ok(panel, 'panel registered')
  let latest = null
  const stop = panel.data({}, (state) => { latest = state }, { locale })
  await new Promise((resolve) => setTimeout(resolve, 400))
  if (typeof stop === 'function') stop()
  return latest
}

async function readStrip(handlers, locale) {
  const factory = handlers.strips.get('concat-status')
  assert.ok(factory, 'strip registered')
  let latest = null
  const stop = factory({}, (state) => { latest = state }, { locale })
  await new Promise((resolve) => setTimeout(resolve, 400))
  if (typeof stop === 'function') stop()
  return latest
}

const mock = await startMock()
process.env.CONCAT_ENDPOINT = mock.address
process.env.CONCAT_TOKEN = mock.token

try {
  const { module, handlers } = await loadPlugin()

  check('plugin name matches its package', module.name === 'local-concat')
  check('one strip, two actions, one panel',
    handlers.strips.size === 1 && handlers.actions.size === 2 && handlers.panels.size === 1)

  const state = await readPanel(handlers, 'zh-CN')
  check('panel sees the editor', state && state.connected === true, JSON.stringify(state))
  check('panel reports the address it used', state && state.address === mock.address)
  check('panel reports the version the editor sent', state && /0\./.test(String(state && state.version)))

  const strip = await readStrip(handlers, 'zh-CN')
  check('strip reads as connected', strip && strip.tone === 'good' && /Concat/.test(strip.text), JSON.stringify(strip))
  check('strip names the open project', strip && strip.text.includes('A very long'), strip && strip.text)
  check('strip stays inside 24 characters', strip && strip.text.length <= 24, `${strip && strip.text.length}`)
  check('strip truncates with an ellipsis', strip && strip.text.endsWith('…'), strip && strip.text)

  const brief = handlers.actions.get('concat-clip')({ clipboard: '/sdcard/DCIM/VID_0001.mp4' }, { locale: 'en-US' })
  check('clip capsule inserts a brief naming the file', /VID_0001\.mp4/.test(brief.insert), brief.insert.split('\n')[0])
  const empty = handlers.actions.get('concat-clip')({ clipboard: '   ' }, { locale: 'zh-CN' })
  check('empty clipboard asks for a path instead of inserting', !empty.insert && Boolean(empty.toast), JSON.stringify(empty))
  const prompt = handlers.actions.get('concat-prompt')({}, { locale: 'zh-CN' })
  check('prompt capsule inserts the general brief', /Concat tools/.test(prompt.insert))

  // Now point at a port where nothing listens.
  process.env.CONCAT_ENDPOINT = `127.0.0.1:${await freePort()}`
  const dead = await readPanel(handlers, 'en-US')
  check('offline panel says so', dead && dead.connected === false, JSON.stringify(dead))
  check('offline panel tells the user what to enable', /Settings > Automation/.test(dead.note), dead.note)
  const deadStrip = await readStrip(handlers, 'en-US')
  check('offline strip is a warning', deadStrip && deadStrip.tone === 'warn' && /offline/.test(deadStrip.text), JSON.stringify(deadStrip))
} finally {
  mock.child.kill()
}

for (const { label, ok, detail } of checks) {
  console.log(`${ok ? 'ok  ' : 'FAIL'} ${label}${ok ? '' : ` — ${detail}`}`)
}
const failed = checks.filter((item) => !item.ok).length
console.log(`\n${checks.length - failed}/${checks.length} checks passed`)
process.exit(failed ? 1 : 0)