import type { ReactNode } from 'react'

import type { WatchlistScope } from '@/types'

export type Tone = 'good' | 'warn' | 'danger' | 'info' | 'muted'
export type CanonicalStatus = 'healthy' | 'warning' | 'blocked' | 'stale' | 'managed-only' | 'unmanaged' | 'idle'


export interface ScopeSessionLike {
  scope?: WatchlistScope
  sessionOpen?: boolean | null
  reason?: string | null
  sessionLabel?: string | null
  observedAtUtc?: string | null
  observedAtEt?: string | null
  nextSessionStartUtc?: string | null
  nextSessionStartEt?: string | null
  sessionCloseUtc?: string | null
  sessionCloseEt?: string | null
  nextOpenUtc?: string | null
  nextCloseUtc?: string | null
}

export function getScopeSessionMeta(scope: WatchlistScope, session?: ScopeSessionLike | null): {
  label: string
  tone: Tone
  detail: string | null
} {
  const label = resolveScopeSessionLabel(scope, session)
  const detail = resolveScopeSessionDetail(scope, session)
  const tone: Tone = session?.sessionOpen ? 'good' : scope === 'crypto_only' ? 'info' : 'warn'
  return { label, tone, detail }
}

function resolveScopeSessionLabel(scope: WatchlistScope, session?: ScopeSessionLike | null): string {
  const explicit = (session?.sessionLabel ?? '').trim()
  if (explicit) return explicit

  const reason = (session?.reason ?? '').toLowerCase()
  if (scope === 'crypto_only') {
    return '24/7 session'
  }
  if (session?.sessionOpen) {
    return 'Session open'
  }
  if (reason.includes('weekend')) {
    return 'Weekend pause'
  }
  if (reason.includes('waiting for the regular et market open')) {
    return 'Pre-market'
  }
  if (reason.includes('after the regular et market close')) {
    return 'After-hours'
  }
  if (reason.includes('outside the regular et market session')) {
    return 'Session closed'
  }
  return scope === 'stocks_only' ? 'Session closed' : '24/7 session'
}

function resolveScopeSessionDetail(scope: WatchlistScope, session?: ScopeSessionLike | null): string | null {
  if (!session) return null
  if (scope === 'crypto_only') {
    return session.reason ?? 'Crypto monitoring is always on.'
  }
  const reason = (session.reason ?? '').trim()
  const nextStart = session.nextSessionStartEt ?? session.nextSessionStartUtc ?? session.nextOpenUtc ?? null
  const sessionClose = session.sessionCloseEt ?? session.sessionCloseUtc ?? session.nextCloseUtc ?? null
  if (session.sessionOpen && sessionClose) {
    return `Regular session until ${formatSessionDateTime(sessionClose)}`
  }
  if (nextStart) {
    return `${reason || 'Monitoring is paused.'} Next open ${formatSessionDateTime(nextStart)}`
  }
  return reason || null
}

function formatSessionDateTime(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) {
    return value
  }
  return date.toLocaleString([], {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  })
}

export function toneBadgeClass(tone: Tone): string {
  switch (tone) {
    case 'good':
      return 'border border-emerald-700/60 bg-emerald-500/10 text-emerald-200'
    case 'warn':
      return 'border border-amber-700/60 bg-amber-500/10 text-amber-200'
    case 'danger':
      return 'border border-rose-700/60 bg-rose-500/10 text-rose-200'
    case 'info':
      return 'border border-cyan-700/60 bg-cyan-500/10 text-cyan-200'
    default:
      return 'border border-slate-700 bg-slate-800/80 text-slate-300'
  }
}

export function toneTextClass(tone: Tone): string {
  switch (tone) {
    case 'good':
      return 'text-right text-emerald-300'
    case 'warn':
      return 'text-right text-amber-300'
    case 'danger':
      return 'text-right text-rose-300'
    case 'info':
      return 'text-right text-cyan-300'
    default:
      return 'text-right text-slate-300'
  }
}

function toUpperKey(value?: string | null): string {
  return (value ?? '').trim().replace(/[\s-]+/g, '_').toUpperCase()
}

function startCase(value?: string | null): string {
  if (!value || !value.trim()) return 'Idle'
  return value
    .trim()
    .replace(/[_-]+/g, ' ')
    .toLowerCase()
    .replace(/\b\w/g, (char) => char.toUpperCase())
}

export function getStatusMeta(raw?: string | null): {
  canonical: CanonicalStatus
  canonicalLabel: string
  rawLabel: string
  tone: Tone
} {
  const normalized = toUpperKey(raw)

  if (!normalized) {
    return { canonical: 'idle', canonicalLabel: 'Idle', rawLabel: 'Idle', tone: 'muted' }
  }

  if (normalized.includes('MANAGED_ONLY')) {
    return { canonical: 'managed-only', canonicalLabel: 'Managed-only', rawLabel: startCase(raw), tone: 'warn' }
  }

  if (normalized.includes('STALE')) {
    return { canonical: 'stale', canonicalLabel: 'Stale', rawLabel: startCase(raw), tone: 'warn' }
  }

  if (
    normalized.includes('LOCKED') ||
    normalized.includes('READ_ONLY') ||
    normalized.includes('REJECT') ||
    normalized.includes('MISSING') ||
    normalized.includes('UNAVAILABLE') ||
    normalized.includes('BLOCKED') ||
    normalized.includes('FAILED') ||
    normalized.includes('ERROR') ||
    normalized.includes('CONFLICT')
  ) {
    return { canonical: 'blocked', canonicalLabel: 'Blocked', rawLabel: startCase(raw), tone: 'danger' }
  }

  if (
    normalized.includes('PAUSED') ||
    normalized.includes('DEGRADED') ||
    normalized.includes('WAITING') ||
    normalized.includes('PENDING') ||
    normalized.includes('CLOSED') ||
    normalized.includes('MONITOR_ONLY')
  ) {
    return { canonical: 'warning', canonicalLabel: 'Warning', rawLabel: startCase(raw), tone: 'warn' }
  }

  if (normalized.includes('INACTIVE') || normalized.includes('UNMANAGED') || normalized === 'FLAT') {
    return { canonical: 'unmanaged', canonicalLabel: 'Unmanaged', rawLabel: startCase(raw), tone: 'muted' }
  }

  return { canonical: 'healthy', canonicalLabel: 'Healthy', rawLabel: startCase(raw), tone: 'good' }
}

export function canonicalStatusLabel(raw?: string | null): string {
  return getStatusMeta(raw).canonicalLabel
}


const BADGE_TOOLTIP_MAP: Record<string, string> = {
  'stocks closed': 'Stock monitoring is outside the regular market session right now.',
  'crypto 24/7': 'Crypto monitoring runs continuously because crypto markets do not close.',
  'runtime active': 'The bot runtime is running and its worker loops should be active.',
  'runtime paused': 'The bot runtime is paused, so automated actions are not advancing.',
  'gate healthy': 'The centralized pre-trade gate is currently allowing eligible orders to proceed.',
  'gate blocked': 'The centralized pre-trade gate is currently preventing new orders from advancing.',
  'operationally ready': 'Dependencies and worker probes look healthy enough for normal operation.',
  'operational review needed': 'One or more dependencies or worker probes need attention before trusting the system fully.',
  'operational review': 'One or more dependencies or worker probes need attention before trusting the system fully.',
  'due': 'Rows already scheduled for evaluation in this scope.',
  'eligible': 'Rows due now and currently unblocked for evaluation.',
  'blocked': 'Rows due now but blocked by session, data freshness, or control state.',
  '0 protective': 'Open positions currently waiting on protective exit handling.',
  '0 expiring soon': 'Positions nearing their time-stop deadline.',
}

export function getBadgeTooltip(label?: string | null): string | null {
  const normalized = (label ?? '').trim().toLowerCase()
  if (!normalized) return null
  if (BADGE_TOOLTIP_MAP[normalized]) return BADGE_TOOLTIP_MAP[normalized]
  if (normalized.startsWith('due ')) return BADGE_TOOLTIP_MAP['due']
  if (normalized.startsWith('eligible ')) return BADGE_TOOLTIP_MAP['eligible']
  if (normalized.startsWith('blocked ')) return BADGE_TOOLTIP_MAP['blocked']
  return null
}

export function PageHero({
  eyebrow,
  title,
  description,
  aside,
}: {
  eyebrow: ReactNode
  title: string
  description: string
  aside?: ReactNode
}) {
  return (
    <header className="rounded-3xl border border-slate-800 bg-slate-900/70 p-6 shadow-2xl shadow-slate-950/30">
      <div className="flex flex-col gap-4 xl:flex-row xl:items-end xl:justify-between">
        <div>
          <div className="mb-2 flex items-center gap-2 text-sm font-medium uppercase tracking-[0.22em] text-cyan-300">{eyebrow}</div>
          <h1 className="text-3xl font-semibold text-white">{title}</h1>
          <p className="mt-2 max-w-3xl text-sm leading-6 text-slate-400">{description}</p>
        </div>
        {aside ? <div className="flex flex-wrap gap-3">{aside}</div> : null}
      </div>
    </header>
  )
}

export function SectionCard({
  title,
  eyebrow,
  icon,
  actions,
  children,
}: {
  title: string
  eyebrow?: string
  icon?: ReactNode
  actions?: ReactNode
  children: ReactNode
}) {
  return (
    <section className="rounded-3xl border border-slate-800 bg-slate-900/70 p-5 shadow-xl shadow-slate-950/20">
      <div className="mb-4 flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          {eyebrow ? <div className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-500">{eyebrow}</div> : null}
          <div className="mt-1 flex items-center gap-2 text-sm font-semibold uppercase tracking-[0.18em] text-slate-400">
            {icon}
            <span>{title}</span>
          </div>
        </div>
        {actions ? <div className="flex flex-wrap gap-2">{actions}</div> : null}
      </div>
      {children}
    </section>
  )
}

export function MetricCard({
  label,
  value,
  detail,
  icon,
  tooltip,
}: {
  label: string
  value: string
  detail: string
  icon?: ReactNode
  tooltip?: string
}) {
  return (
    <div title={tooltip} className="rounded-3xl border border-slate-800 bg-slate-900/70 p-5 shadow-xl shadow-slate-950/20">
      <div className="flex items-center justify-between gap-3">
        <div className="text-sm text-slate-400">{label}</div>
        {icon ? <div className="text-cyan-300">{icon}</div> : null}
      </div>
      <div className="mt-3 text-3xl font-semibold text-white">{value}</div>
      <div className="mt-2 text-sm text-slate-500">{detail}</div>
    </div>
  )
}

export function MiniMetric({ label, value, detail }: { label: string; value: string; detail?: string }) {
  return (
    <div className="rounded-2xl border border-slate-800 bg-slate-950/60 px-4 py-3">
      <div className="text-xs uppercase tracking-wide text-slate-500">{label}</div>
      <div className="mt-1 text-2xl font-semibold text-white">{value}</div>
      {detail ? <div className="mt-1 text-xs text-slate-500">{detail}</div> : null}
    </div>
  )
}

export function DetailRow({ label, value, tone = 'muted' }: { label: string; value: string; tone?: Tone }) {
  return (
    <div className="flex items-start justify-between gap-4">
      <div className="text-slate-500">{label}</div>
      <div className={toneTextClass(tone)}>{value}</div>
    </div>
  )
}

export function StatusPill({ label, tone, compact = false, tooltip }: { label: string; tone: Tone; compact?: boolean; tooltip?: string }) {
  return <span title={tooltip} className={`${compact ? 'px-2 py-1 text-[11px]' : 'px-3 py-2 text-sm'} rounded-full ${toneBadgeClass(tone)}`}>{label}</span>
}

export function ToneBadge({ children, tone, tooltip }: { children: ReactNode; tone: Tone; tooltip?: string }) {
  return <span title={tooltip} className={`rounded-full px-2.5 py-1 text-xs font-semibold uppercase tracking-wide ${toneBadgeClass(tone)}`}>{children}</span>
}

export function EmptyState({ message }: { message: string }) {
  return <div className="rounded-2xl border border-dashed border-slate-700 px-4 py-8 text-center text-sm text-slate-500">{message}</div>
}
