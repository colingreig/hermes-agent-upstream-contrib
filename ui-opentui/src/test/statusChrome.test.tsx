/**
 * Epic 1.3 — header/status chrome (Variant A dense single status bar).
 * Layers covered:
 *   1. schema: the new SessionInfo wire fields decode (and null/absence is safe)
 *   2. store: applyInfo merges the new usage/chrome fields
 *   3. pure logic: statusSegments width table (priority drop order) + the
 *      ctx/cmp threshold levels + compact formatters
 *   4. frames: the bar renders the dense segment layout with separators, drops
 *      tail segments when narrow, and the update notice borrows the line
 */
import { Option } from 'effect'
import { describe, expect, test } from 'vitest'

import { decodeSessionInfoPatch } from '../boundary/schema/SessionInfo.ts'
import { createSessionStore, type SessionStore } from '../logic/store.ts'
import {
  chromeMode,
  cmpLevel,
  ctxLevel,
  fmtShortDuration,
  fmtTokens,
  StatusBar,
  statusSegments
} from '../view/statusBar.tsx'
import { ThemeProvider } from '../view/theme.tsx'
import { captureFrame, renderProbe } from './lib/render.ts'

// ── 1. schema ────────────────────────────────────────────────────────────

describe('SessionInfoPatchSchema — Epic 1.3 wire fields', () => {
  test('decodes the new chrome fields (update/profile/mcp/cost)', () => {
    const decoded = decodeSessionInfoPatch({
      model: 'anthropic/claude-opus-4-8',
      update_behind: 3,
      update_command: 'hermes update',
      profile_name: 'researcher',
      mcp_servers: [{ name: 'railway' }, { name: 'beeper' }],
      usage: { context_percent: 42, context_used: 84_000, cost_usd: 0.41, compressions: 2 }
    })
    expect(Option.isSome(decoded)).toBe(true)
    if (Option.isSome(decoded)) {
      expect(decoded.value.update_behind).toBe(3)
      expect(decoded.value.update_command).toBe('hermes update')
      expect(decoded.value.profile_name).toBe('researcher')
      expect(decoded.value.mcp_servers).toHaveLength(2)
      expect(decoded.value.usage?.cost_usd).toBe(0.41)
    }
  })

  test('update_behind: null (check not resolved yet) decodes — None-safe', () => {
    const decoded = decodeSessionInfoPatch({ model: 'm', update_behind: null, update_command: '' })
    expect(Option.isSome(decoded)).toBe(true)
    if (Option.isSome(decoded)) expect(decoded.value.update_behind).toBeNull()
  })

  test('all new fields absent still decodes (every key optional)', () => {
    expect(Option.isSome(decodeSessionInfoPatch({ model: 'm' }))).toBe(true)
  })
})

// ── 2. store applyInfo ───────────────────────────────────────────────────

describe('store.applyInfo — Epic 1.3 chrome merge', () => {
  test('merges cost/update/profile/mcp into SessionInfo', () => {
    const store = createSessionStore()
    store.applyInfo({
      model: 'opus',
      update_behind: 4,
      update_command: 'uv tool upgrade hermes',
      profile_name: 'researcher',
      mcp_servers: [{}, {}, {}],
      usage: { cost_usd: 0.4129, context_percent: 42 }
    })
    expect(store.state.info.costUsd).toBeCloseTo(0.4129)
    expect(store.state.info.updateBehind).toBe(4)
    expect(store.state.info.updateCommand).toBe('uv tool upgrade hermes')
    expect(store.state.info.profileName).toBe('researcher')
    expect(store.state.info.mcpServers).toBe(3)
  })

  test('update_behind: null leaves the prior value alone (partial-patch rule)', () => {
    const store = createSessionStore()
    store.applyInfo({ update_behind: 2 })
    store.applyInfo({ update_behind: null })
    expect(store.state.info.updateBehind).toBe(2)
  })

  test('a usage patch with cost does not clobber unrelated chrome', () => {
    const store = createSessionStore()
    store.applyInfo({ model: 'opus', profile_name: 'researcher' })
    store.applyInfo({ usage: { cost_usd: 0.1 } })
    expect(store.state.info).toMatchObject({ model: 'opus', profileName: 'researcher', costUsd: 0.1 })
  })

  test('startedAt is seeded at store creation and never patched off the wire', () => {
    const before = Date.now()
    const store = createSessionStore()
    expect(store.state.info.startedAt).toBeGreaterThanOrEqual(before)
    const seeded = store.state.info.startedAt
    store.applyInfo({ model: 'opus' })
    expect(store.state.info.startedAt).toBe(seeded)
  })
})

// ── 3. pure logic ────────────────────────────────────────────────────────

describe('chromeMode — the responsive width ladder (design pass piece 3)', () => {
  test('wide ≥140, medium in between, narrow <72 (where ctx detail collapses)', () => {
    expect(chromeMode(220)).toBe('wide')
    expect(chromeMode(140)).toBe('wide')
    expect(chromeMode(139)).toBe('medium')
    expect(chromeMode(120)).toBe('medium')
    expect(chromeMode(72)).toBe('medium')
    expect(chromeMode(71)).toBe('narrow')
    expect(chromeMode(40)).toBe('narrow')
    expect(chromeMode(0)).toBe('narrow') // degenerate input stays well-formed
  })
})

describe('statusSegments — progressive disclosure table', () => {
  test('full width shows everything', () => {
    expect(statusSegments(220)).toEqual({
      ctxDetail: true,
      duration: true,
      compressions: true,
      cost: true,
      profile: true,
      bg: true,
      mcp: true
    })
  })

  test('segments drop in reverse priority as width shrinks: mcp → bg → profile → cost → duration/cmp → ctx detail', () => {
    // each row: [width, expected visible flags]
    const table: Array<[number, Partial<ReturnType<typeof statusSegments>>]> = [
      [115, { mcp: false, bg: true }], // mcp drops first
      [107, { mcp: false, bg: false, profile: true }], // then bg
      [99, { profile: false, cost: true }], // then profile
      [91, { cost: false, duration: true, compressions: true }], // then cost
      [79, { duration: false, compressions: false, ctxDetail: true }], // then duration/cmp
      [71, { ctxDetail: false }] // finally the bar/token detail collapses to bare `42%`
    ]
    for (const [width, expected] of table) {
      expect(statusSegments(width)).toMatchObject(expected)
    }
  })

  test('pinned essentials are never gated: statusSegments only governs the tail', () => {
    // even at absurdly narrow widths the table stays well-formed (booleans, no throw)
    const segs = statusSegments(10)
    expect(Object.values(segs).every(v => v === false)).toBe(true)
  })
})

describe('threshold levels (spec 50/80/95 and cmp 5/10)', () => {
  test('ctxLevel boundaries', () => {
    expect(ctxLevel(0)).toBe('ok')
    expect(ctxLevel(49)).toBe('ok')
    expect(ctxLevel(50)).toBe('warn')
    expect(ctxLevel(79)).toBe('warn')
    expect(ctxLevel(80)).toBe('bad')
    expect(ctxLevel(94)).toBe('bad')
    expect(ctxLevel(95)).toBe('critical')
    expect(ctxLevel(100)).toBe('critical')
  })

  test('cmpLevel boundaries', () => {
    expect(cmpLevel(0)).toBe('ok')
    expect(cmpLevel(4)).toBe('ok')
    expect(cmpLevel(5)).toBe('warn')
    expect(cmpLevel(9)).toBe('warn')
    expect(cmpLevel(10)).toBe('bad')
  })
})

describe('compact formatters', () => {
  test('fmtTokens', () => {
    expect(fmtTokens(950)).toBe('950')
    expect(fmtTokens(84_321)).toBe('84k')
    expect(fmtTokens(1_000_000)).toBe('1M')
    expect(fmtTokens(1_250_000)).toBe('1.3M')
  })

  test('fmtShortDuration', () => {
    expect(fmtShortDuration(42)).toBe('42s')
    expect(fmtShortDuration(23 * 60)).toBe('23m')
    expect(fmtShortDuration(65 * 60)).toBe('1h05m')
  })
})

// ── 4. frames ────────────────────────────────────────────────────────────

function seededStore(): SessionStore {
  const store = createSessionStore()
  store.apply({ type: 'gateway.ready' })
  store.applyInfo({
    model: 'anthropic/claude-opus-4-8',
    reasoning_effort: 'high',
    cwd: '/tmp/proj',
    branch: 'main',
    profile_name: 'researcher',
    mcp_servers: [{}, {}],
    usage: { context_percent: 42, context_used: 84_000, context_max: 200_000, cost_usd: 0.41, compressions: 2 }
  })
  return store
}

function bar(store: SessionStore) {
  return () => (
    <ThemeProvider theme={() => store.state.theme}>
      <StatusBar store={store} />
    </ThemeProvider>
  )
}

describe('StatusBar frames (responsive chrome)', () => {
  test('WIDE (160) spreads into the two-line layout: vitals line + environment line', async () => {
    const frame = await captureFrame(bar(seededStore()), { width: 160, height: 4 })
    // every segment present…
    expect(frame).toContain('claude-opus-4-8')
    expect(frame).toContain('·high') // effort suffix
    expect(frame).toContain('42%')
    expect(frame).toContain('84k/200k') // wide ctx zone shows used/max tokens
    expect(frame).toContain('░') // the meter is partially filled at 42%
    expect(frame).toContain('$0.41')
    expect(frame).toContain('cmp 2')
    expect(frame).toContain('researcher') // profile badge
    expect(frame).toContain('2 mcp')
    expect(frame).toContain('/tmp/proj (main)')
    expect(frame).toContain('│')
    // …split across TWO lines: vitals (model + cost) up, environment (profile +
    // cwd) below.
    const rows = frame.split('\n')
    const vitals = rows.findIndex(r => r.includes('claude-opus-4-8'))
    const env = rows.findIndex(r => r.includes('researcher'))
    expect(vitals).toBeGreaterThanOrEqual(0)
    expect(env).toBe(vitals + 1)
    expect(rows[vitals]).toContain('$0.41')
    expect(rows[vitals]).toContain('42%')
    expect(rows[env]).toContain('2 mcp')
    expect(rows[env]).toContain('/tmp/proj (main)')
  })

  test('MEDIUM (120) keeps the dense single line (model and cwd share a row)', async () => {
    const frame = await captureFrame(bar(seededStore()), { width: 120, height: 3 })
    const rows = frame.split('\n')
    const row = rows.find(r => r.includes('claude-opus-4-8')) ?? ''
    expect(row).toContain('42%')
    expect(row).toContain('$0.41')
    expect(row).toContain('/tmp/proj (main)')
  })

  test('WIDE line 2 carries the agents-running count when subagents are live', async () => {
    const store = seededStore()
    store.apply({ payload: { goal: 'crunch data', subagent_id: 'sa1' }, type: 'subagent.start' })
    const frame = await captureFrame(bar(store), { width: 160, height: 4 })
    expect(frame).toContain('1 agent running')
    const rows = frame.split('\n')
    const env = rows.find(r => r.includes('researcher')) ?? ''
    expect(env).toContain('1 agent running')
  })

  test('WIDE update notice rides line 2 (no line borrowing — segments stay)', async () => {
    const store = seededStore()
    store.applyInfo({ update_behind: 3, update_command: 'hermes update' })
    const frame = await captureFrame(bar(store), { width: 200, height: 4 })
    expect(frame).toContain('3 commits behind')
    expect(frame).toContain('$0.41') // vitals NOT replaced in wide mode
    expect(frame).toContain('claude-opus-4-8')
  })

  test('narrow width drops the tail (no mcp/profile/cost) and compacts the context read-out', async () => {
    const frame = await captureFrame(bar(seededStore()), { width: 70, height: 3 })
    expect(frame).toContain('claude-opus-4-8') // pinned
    expect(frame).toContain('42%') // pinned (compact)
    expect(frame).not.toContain('█') // bar detail dropped
    expect(frame).not.toContain('84k')
    expect(frame).not.toContain('$0.41')
    expect(frame).not.toContain('cmp 2')
    expect(frame).not.toContain('researcher')
    expect(frame).not.toContain('mcp')
  })

  test('update notice borrows the line and Esc dismisses it back to the normal bar', async () => {
    const store = seededStore()
    store.applyInfo({ update_behind: 3, update_command: 'hermes update' })
    const probe = await renderProbe(bar(store), { width: 120, height: 3, kittyKeyboard: true })
    try {
      expect(probe.frame()).toContain('3 commits behind')
      expect(probe.frame()).toContain('hermes update')
      expect(probe.frame()).not.toContain('$0.41') // the notice replaced the segments
      probe.keys.pressEscape()
      await probe.settle()
      const after = await probe.waitForFrame(f => f.includes('$0.41'))
      expect(after).not.toContain('commits behind')
    } finally {
      probe.destroy()
    }
  })
})
