/**
 * StatusBar — the session chrome above the composer, RESPONSIVE across a
 * width ladder (`chromeMode`, design pass piece 3):
 *
 * WIDE (≥140 cols) — the chrome breathes: TWO lines of justified zones.
 *   line 1 (session vitals):   ● model ·effort   ███░░ 42% 84k/200k   $0.41 · 23m · cmp 2
 *   line 2 (environment):      profile │ 2 mcp │ 1 agent running          …/cwd (branch)
 *   A pending update renders in line 2's notice SLOT (before the cwd) instead
 *   of borrowing a line.
 *
 * MEDIUM — today's Variant A dense single row (v6 Epic 1.3; signed-off):
 *
 *   ● model ·effort │ ███░░ 42% 84k │ $0.41 · 23m · cmp 2 │ profile │ 2 mcp │ …/cwd (branch)
 *
 * NARROW (<72) — pinned essentials only, via the same progressive-disclosure
 * ladder (Ink's `statusRuleWidths` idiom): the dot+model and the context %
 * are PINNED; tail segments drop whole as columns shrink, in reverse priority
 * — mcp → bg → profile → cost → duration/cmp → token/bar detail — and the cwd
 * left-truncates into whatever remains. `statusSegments` is the pure
 * width→visibility table (table-tested); nothing truncates mid-segment, so a
 * row NEVER wraps or clips.
 *
 * In medium/narrow a pending update (`info.update_behind > 0`) BORROWS the
 * whole line as a transient notice (Variant A decision — no permanent
 * transcript row); it dismisses on Esc or after NOTICE_TTL_MS (the Esc/TTL
 * dismiss also clears the wide notice slot).
 *
 * Colors respect the Appendix C roles: the navy `statusBg` fill (the one
 * correct blue surface), `statusFg` primary text, muted metrics, ok/warn dot.
 *
 * Parity notes (data that does not reach this TUI yet — reported, not faked):
 *   - `N bg` (background tasks): the OpenTUI store has no background-task
 *     tracking (Ink counts `prompt.background` task_ids + `background.complete`
 *     locally); the segment slot exists in `statusSegments` but renders nothing.
 *   - `display.show_cost`: Ink reads it from its `config.get` polling loop,
 *     which this TUI doesn't have — cost shows whenever `usage.cost_usd` is
 *     present instead.
 *
 * Read-only chrome — the only input handled is Esc-to-dismiss for the notice.
 */
import { useKeyboard } from '@opentui/solid'
import { createEffect, createMemo, createSignal, onCleanup, Show } from 'solid-js'

import type { SessionStore } from '../logic/store.ts'
import { isTrayAgent } from './agentsTray.tsx'
import { useDimensions } from './dimensions.tsx'
import { elapsedSeconds, useElapsedTick } from './elapsed.ts'
import { useTheme } from './theme.tsx'

const HOME = process.env.HOME ?? ''
const CTX_BAR_CELLS = 5
const SEP = ' │ '
const DOT_SEP = ' · '
/** How long the transient update notice may borrow the bar line. */
const NOTICE_TTL_MS = 30_000

// ── pure, table-tested width/threshold logic ────────────────────────────

/** The responsive chrome ladder (design pass piece 3). WIDE spreads the
 *  chrome over TWO justified lines; MEDIUM is the dense single row; NARROW is
 *  the pinned-essentials end of the `statusSegments` drop ladder (where the
 *  ctx read-out has collapsed to a bare `42%`). */
export type ChromeMode = 'wide' | 'medium' | 'narrow'

export function chromeMode(cols: number): ChromeMode {
  const w = Math.max(1, Math.floor(cols || 1))
  if (w >= 140) return 'wide'
  if (w >= 72) return 'medium'
  return 'narrow'
}

/** Which tail segments are visible at a given column count. Drop order as the
 *  terminal narrows (reverse priority, spec Epic 1.3): mcp → bg → profile →
 *  cost → duration/cmp → ctxDetail (bar+token count collapse to a bare `42%`).
 *  Dot+model and the context % are pinned and never gated here. */
export interface StatusSegments {
  /** Full `███░░ 42% 84k` read-out; false → compact bare `42%`. */
  ctxDetail: boolean
  duration: boolean
  compressions: boolean
  cost: boolean
  profile: boolean
  /** Background-tasks count — reserved; no store data feeds it yet (see header). */
  bg: boolean
  mcp: boolean
}

export function statusSegments(cols: number): StatusSegments {
  const w = Math.max(1, Math.floor(cols || 1))
  return {
    ctxDetail: w >= 72,
    duration: w >= 80,
    compressions: w >= 80,
    cost: w >= 92,
    profile: w >= 100,
    bg: w >= 108,
    mcp: w >= 116
  }
}

/** Context-pressure level for the bar/% colour (spec thresholds 50/80/95). */
export type CtxLevel = 'ok' | 'warn' | 'bad' | 'critical'
export function ctxLevel(pct: number): CtxLevel {
  if (pct >= 95) return 'critical'
  if (pct >= 80) return 'bad'
  if (pct >= 50) return 'warn'
  return 'ok'
}

/** Compression-count level (spec: warn ≥5, error ≥10). */
export type CmpLevel = 'ok' | 'warn' | 'bad'
export function cmpLevel(n: number): CmpLevel {
  if (n >= 10) return 'bad'
  if (n >= 5) return 'warn'
  return 'ok'
}

/** Compact token count: 84321 → `84k`, 1_250_000 → `1.3M`, 950 → `950`. */
export function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1).replace(/\.0$/, '')}M`
  if (n >= 1_000) return `${Math.round(n / 1_000)}k`
  return `${Math.max(0, Math.round(n))}`
}

/** Compact session duration: 42 → `42s`, 23*60 → `23m`, 65*60 → `1h05m`. */
export function fmtShortDuration(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds))
  if (s < 60) return `${s}s`
  if (s < 3600) return `${Math.floor(s / 60)}m`
  return `${Math.floor(s / 3600)}h${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}m`
}

// ── local formatting helpers ────────────────────────────────────────────

/** `anthropic/claude-opus-4-8` → `claude-opus-4-8`; trims the provider prefix (Ink shortModelLabel). */
function shortModel(model: string): string {
  return model.includes('/') ? (model.split('/').at(-1) ?? model) : model
}

/** Reasoning effort → a compact suffix; hidden for the default/medium effort. */
function effortSuffix(effort: string | undefined, fast: boolean | undefined): string {
  const parts: string[] = []
  if (effort && effort !== 'medium' && effort !== 'default') parts.push(effort)
  if (fast) parts.push('fast')
  return parts.length ? ` ·${parts.join('·')}` : ''
}

/** Abbreviate cwd with `~` for $HOME, then collapse to the last two path segments
 *  (`…/lively-thrush/hermes-agent`) so deep worktree paths stay readable (Ink fmtCwdBranch). */
function shortCwd(cwd: string): string {
  const home = HOME && (cwd === HOME || cwd.startsWith(HOME + '/')) ? '~' + cwd.slice(HOME.length) : cwd
  const segs = home.split('/').filter(Boolean)
  return segs.length <= 3 ? home : '…/' + segs.slice(-2).join('/')
}

/** Keep the TAIL of a string, prefixing with `…` when it must be clipped. */
function truncLeft(s: string, max: number): string {
  if (max <= 1) return s.length > max ? '…' : s
  return s.length <= max ? s : '…' + s.slice(s.length - max + 1)
}

/** Keep the HEAD of a string, suffixing with `…` when it must be clipped. */
function truncRight(s: string, max: number): string {
  if (max <= 1) return s.length > max ? '…' : s
  return s.length <= max ? s : s.slice(0, max - 1) + '…'
}

/** A unicode meter: `███░░` filled to `pct`% over `width` cells (Ink ctxBar). */
function ctxBar(pct: number, width: number): string {
  const filled = Math.max(0, Math.min(width, Math.round((pct / 100) * width)))
  return '█'.repeat(filled) + '░'.repeat(width - filled)
}

export function StatusBar(props: { store: SessionStore }) {
  const theme = useTheme()
  const dims = useDimensions()
  const info = () => props.store.state.info
  const tick = useElapsedTick()

  const ctxColorOf = (pct: number) => {
    const level = ctxLevel(pct)
    return level === 'critical'
      ? theme().color.statusCritical
      : level === 'bad'
        ? theme().color.statusBad
        : level === 'warn'
          ? theme().color.statusWarn
          : theme().color.statusGood
  }
  const cmpColorOf = (n: number) => {
    const level = cmpLevel(n)
    return level === 'bad' ? theme().color.error : level === 'warn' ? theme().color.warn : theme().color.muted
  }

  const dot = () => (info().running ? '◐' : props.store.state.ready ? '●' : '○')
  const dotColor = () =>
    info().running ? theme().color.statusWarn : props.store.state.ready ? theme().color.statusGood : theme().color.muted

  const segs = createMemo(() => statusSegments(dims().width))
  const mode = createMemo(() => chromeMode(dims().width))

  // ── transient update notice (borrows the whole line; Esc / TTL dismisses) ──
  const [dismissed, setDismissed] = createSignal(false)
  const noticeText = createMemo(() => {
    const behind = info().updateBehind
    if (dismissed() || behind === undefined || behind <= 0) return ''
    const cmd = info().updateCommand
    const base = `↑ hermes is ${behind} commit${behind === 1 ? '' : 's'} behind`
    return `${base}${cmd ? ` — update: ${cmd}` : ''}${SEP}Esc to dismiss`
  })
  createEffect(() => {
    if (!noticeText()) return
    const timer = setTimeout(() => setDismissed(true), NOTICE_TTL_MS)
    onCleanup(() => clearTimeout(timer))
  })
  // Dismiss-only handler: never swallows Esc from overlays/composer (they keep
  // their own handlers); dismissing the notice alongside is benign.
  useKeyboard(key => {
    if (key.name === 'escape' && noticeText()) setDismissed(true)
  })

  // ── segment texts (each '' when hidden/absent — also feeds the width budget) ──
  const model = () => {
    const m = info().model
    return m ? shortModel(m) : ''
  }
  const effort = () => effortSuffix(info().effort, info().fast)
  const pct = () => info().contextPercent

  const ctxText = createMemo(() => {
    const p = pct()
    if (p === undefined) return ''
    if (!segs().ctxDetail) return `${p}%`
    const used = info().contextUsed
    return `${ctxBar(p, CTX_BAR_CELLS)} ${p}%${used !== undefined ? ` ${fmtTokens(used)}` : ''}`
  })

  const costText = createMemo(() => {
    const c = info().costUsd
    return segs().cost && c !== undefined ? `$${c.toFixed(2)}` : ''
  })
  const durationText = createMemo(() => {
    const started = info().startedAt
    if (!segs().duration || !started || !model()) return ''
    tick() // re-derive once per second while shown
    return fmtShortDuration(elapsedSeconds(started))
  })
  const cmpCount = () => info().compressions ?? 0
  const cmpText = createMemo(() => (segs().compressions && cmpCount() > 0 ? `cmp ${cmpCount()}` : ''))
  /** cost · duration · cmp as ONE bar segment (the spec's `$0.41 · 23m · cmp 2`). */
  const meterText = createMemo(() => [costText(), durationText(), cmpText()].filter(Boolean).join(DOT_SEP))

  const profileText = createMemo(() => {
    const p = info().profileName
    return segs().profile && p && p !== 'default' && p !== 'custom' ? p : ''
  })
  const mcpText = createMemo(() => {
    const n = info().mcpServers ?? 0
    return segs().mcp && n > 0 ? `${n} mcp` : ''
  })

  // Width budget for the right-aligned cwd: total minus box padding minus the
  // plain-text width of every visible left segment (all monospace-1-col chars).
  const leftLen = createMemo(() => {
    let len = 1 // dot
    if (model()) len += 1 + model().length + effort().length
    for (const seg of [ctxText(), meterText(), profileText(), mcpText()]) {
      if (seg) len += SEP.length + seg.length
    }
    return len
  })
  const cwdFull = createMemo(() => {
    const cwd = info().cwd
    const c = cwd ? shortCwd(cwd) : ''
    if (!c) return ''
    return info().branch ? `${c} (${info().branch})` : c
  })
  const rightText = createMemo(() => {
    // dims() is the TERMINAL width; the bar's row is narrower by the app shell's
    // horizontal padding (2) + this box's own padding (2), and we keep a 2-col
    // gap so the cwd never butts against the left segments.
    const budget = dims().width - 4 - leftLen() - 2
    return budget > 4 ? truncLeft(cwdFull(), budget) : ''
  })

  // ── wide-mode extras (the two-line spread) ──────────────────────────────
  /** Full context read-out with used/max tokens — the wide line-1 center zone. */
  const wideCtx = createMemo(() => {
    const p = pct()
    if (p === undefined) return ''
    const used = info().contextUsed
    const max = info().contextMax
    const tokens = used !== undefined ? ` ${fmtTokens(used)}${max ? `/${fmtTokens(max)}` : ''}` : ''
    return `${ctxBar(p, CTX_BAR_CELLS)} ${p}%${tokens}`
  })
  /** Live delegated-agents count (same predicate as the agents tray). */
  const agentsText = createMemo(() => {
    const n = props.store.state.subagents.filter(isTrayAgent).length
    return n > 0 ? `${n} agent${n === 1 ? '' : 's'} running` : ''
  })
  /** Wide line 2's left zone: profile │ N mcp │ agents-running. */
  const envLeft = createMemo(() => [profileText(), mcpText(), agentsText()].filter(Boolean))
  /** Wide line 2's right zone trims the cwd around the update-notice slot. */
  const wideCwd = createMemo(() => {
    let len = 0
    for (const seg of envLeft()) len += (len ? SEP.length : 0) + seg.length
    const notice = noticeText()
    if (notice) len += (len ? SEP.length : 0) + notice.length
    const budget = dims().width - 4 - len - 2
    return budget > 4 ? truncLeft(cwdFull(), budget) : ''
  })

  /** cost · duration · cmp spans — shared by the medium row and wide line 1. */
  const Meter = () => (
    <>
      <Show when={costText()}>
        <span style={{ fg: theme().color.muted }}>{costText()}</span>
      </Show>
      <Show when={costText() && durationText()}>
        <span style={{ fg: theme().color.muted }}>{DOT_SEP}</span>
      </Show>
      <Show when={durationText()}>
        <span style={{ fg: theme().color.muted }}>{durationText()}</span>
      </Show>
      <Show when={(costText() || durationText()) && cmpText()}>
        <span style={{ fg: theme().color.muted }}>{DOT_SEP}</span>
      </Show>
      <Show when={cmpText()}>
        <span style={{ fg: cmpColorOf(cmpCount()) }}>{cmpText()}</span>
      </Show>
    </>
  )

  return (
    <box
      style={{
        flexShrink: 0,
        flexDirection: 'column',
        backgroundColor: theme().color.statusBg,
        paddingLeft: 1,
        paddingRight: 1
      }}
    >
      <Show
        when={mode() === 'wide'}
        fallback={
          /* MEDIUM/NARROW — the dense single row (statusSegments drop ladder). */
          <box style={{ flexShrink: 0, flexDirection: 'row' }}>
            <Show
              when={!noticeText()}
              fallback={
                // the update notice borrows the WHOLE line (Variant A) — warn-tinted,
                // head-truncated so the Esc hint clips last only on absurd widths.
                <text selectable={false}>
                  <span style={{ fg: theme().color.warn }}>
                    {truncRight(noticeText(), Math.max(1, dims().width - 4))}
                  </span>
                </text>
              }
            >
              {/* left: pinned dot+model, then the priority-ordered tail segments */}
              <box style={{ flexShrink: 0, flexDirection: 'row' }}>
                <text selectable={false}>
                  <span style={{ fg: dotColor() }}>{dot()}</span>
                  <Show when={model()}>
                    <span style={{ fg: theme().color.statusFg }}>{` ${model()}`}</span>
                    <span style={{ fg: theme().color.muted }}>{effort()}</span>
                  </Show>
                  <Show when={ctxText()}>
                    <span style={{ fg: theme().color.border }}>{SEP}</span>
                    {/* ctxText() truthy guarantees pct() is defined; `?? 0` only satisfies the type. */}
                    <Show
                      when={segs().ctxDetail}
                      fallback={<span style={{ fg: ctxColorOf(pct() ?? 0) }}>{ctxText()}</span>}
                    >
                      <span style={{ fg: ctxColorOf(pct() ?? 0) }}>{ctxBar(pct() ?? 0, CTX_BAR_CELLS)}</span>
                      <span style={{ fg: theme().color.statusFg }}>{` ${pct()}%`}</span>
                      <Show when={info().contextUsed !== undefined}>
                        <span style={{ fg: theme().color.muted }}>{` ${fmtTokens(info().contextUsed ?? 0)}`}</span>
                      </Show>
                    </Show>
                  </Show>
                  <Show when={meterText()}>
                    <span style={{ fg: theme().color.border }}>{SEP}</span>
                    <Meter />
                  </Show>
                  <Show when={profileText()}>
                    <span style={{ fg: theme().color.border }}>{SEP}</span>
                    {/* statusFg, not accent — persistent chrome spends no warm ink
                        (design pass); the navy fill is the bar's one blue surface. */}
                    <span style={{ fg: theme().color.statusFg }}>{profileText()}</span>
                  </Show>
                  {/* `N bg` would slot here (segs().bg) — no store data feeds it yet (see header). */}
                  <Show when={mcpText()}>
                    <span style={{ fg: theme().color.border }}>{SEP}</span>
                    <span style={{ fg: theme().color.muted }}>{mcpText()}</span>
                  </Show>
                </text>
              </box>

              {/* spacer pushes the cwd to the right edge */}
              <box style={{ flexGrow: 1, minWidth: 0 }} />

              {/* right: cwd (branch), pre-truncated so the row never wraps */}
              <Show when={rightText()}>
                <box style={{ flexShrink: 0, flexDirection: 'row' }}>
                  <text selectable={false}>
                    <span style={{ fg: theme().color.muted }}>{rightText()}</span>
                  </text>
                </box>
              </Show>
            </Show>
          </box>
        }
      >
        {/* WIDE line 1 — session vitals in justified zones:
            model·effort │ ctx bar + tokens │ cost·duration·cmp */}
        <box style={{ flexShrink: 0, flexDirection: 'row' }}>
          <box style={{ flexShrink: 0 }}>
            <text selectable={false}>
              <span style={{ fg: dotColor() }}>{dot()}</span>
              <Show when={model()}>
                <span style={{ fg: theme().color.statusFg }}>{` ${model()}`}</span>
                <span style={{ fg: theme().color.muted }}>{effort()}</span>
              </Show>
            </text>
          </box>
          <box style={{ flexGrow: 1, minWidth: 0 }} />
          <Show when={wideCtx()}>
            <box style={{ flexShrink: 0 }}>
              <text selectable={false}>
                <span style={{ fg: ctxColorOf(pct() ?? 0) }}>{ctxBar(pct() ?? 0, CTX_BAR_CELLS)}</span>
                <span style={{ fg: theme().color.statusFg }}>{` ${pct()}%`}</span>
                <Show when={info().contextUsed !== undefined}>
                  <span style={{ fg: theme().color.muted }}>
                    {` ${fmtTokens(info().contextUsed ?? 0)}${
                      info().contextMax ? `/${fmtTokens(info().contextMax ?? 0)}` : ''
                    }`}
                  </span>
                </Show>
              </text>
            </box>
          </Show>
          <box style={{ flexGrow: 1, minWidth: 0 }} />
          <Show when={meterText()}>
            <box style={{ flexShrink: 0 }}>
              <text selectable={false}>
                <Meter />
              </text>
            </box>
          </Show>
        </box>

        {/* WIDE line 2 — environment:
            profile │ N mcp │ agents-running … update-notice slot │ cwd (branch) */}
        <box style={{ flexShrink: 0, flexDirection: 'row' }}>
          <box style={{ flexShrink: 0 }}>
            <text selectable={false}>
              <Show when={profileText()}>
                <span style={{ fg: theme().color.statusFg }}>{profileText()}</span>
              </Show>
              <Show when={profileText() && (mcpText() || agentsText())}>
                <span style={{ fg: theme().color.border }}>{SEP}</span>
              </Show>
              <Show when={mcpText()}>
                <span style={{ fg: theme().color.muted }}>{mcpText()}</span>
              </Show>
              <Show when={mcpText() && agentsText()}>
                <span style={{ fg: theme().color.border }}>{SEP}</span>
              </Show>
              <Show when={agentsText()}>
                <span style={{ fg: theme().color.muted }}>{agentsText()}</span>
              </Show>
            </text>
          </box>
          <box style={{ flexGrow: 1, minWidth: 0 }} />
          <Show when={noticeText() || wideCwd()}>
            <box style={{ flexShrink: 0 }}>
              <text selectable={false}>
                {/* the update notice rides line 2's slot in wide mode — no line
                    borrowing; Esc/TTL dismisses it exactly as in medium. */}
                <Show when={noticeText()}>
                  <span style={{ fg: theme().color.warn }}>{noticeText()}</span>
                </Show>
                <Show when={noticeText() && wideCwd()}>
                  <span style={{ fg: theme().color.border }}>{SEP}</span>
                </Show>
                <Show when={wideCwd()}>
                  <span style={{ fg: theme().color.muted }}>{wideCwd()}</span>
                </Show>
              </text>
            </box>
          </Show>
        </box>
      </Show>
    </box>
  )
}
