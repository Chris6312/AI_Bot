import type { ReactNode } from 'react'

export type Tone = 'good' | 'warn' | 'danger' | 'info' | 'muted'

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
}: {
  label: string
  value: string
  detail: string
  icon?: ReactNode
}) {
  return (
    <div className="rounded-3xl border border-slate-800 bg-slate-900/70 p-5 shadow-xl shadow-slate-950/20">
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

export function StatusPill({ label, tone, compact = false }: { label: string; tone: Tone; compact?: boolean }) {
  return <span className={`${compact ? 'px-2 py-1 text-[11px]' : 'px-3 py-2 text-sm'} rounded-full ${toneBadgeClass(tone)}`}>{label}</span>
}

export function ToneBadge({ children, tone }: { children: ReactNode; tone: Tone }) {
  return <span className={`rounded-full px-2.5 py-1 text-xs font-semibold uppercase tracking-wide ${toneBadgeClass(tone)}`}>{children}</span>
}

export function EmptyState({ message }: { message: string }) {
  return <div className="rounded-2xl border border-dashed border-slate-700 px-4 py-8 text-center text-sm text-slate-500">{message}</div>
}
