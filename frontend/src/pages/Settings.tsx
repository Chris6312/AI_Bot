import { useMemo, useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { format, formatDistanceToNowStrict } from 'date-fns'
import {
  ArrowRightLeft,
  Clock3,
  PlayCircle,
  Shield,
  Siren,
  Wifi,
} from 'lucide-react'

import { api } from '@/lib/api'
import { MetricCard, PageHero, StatusPill, getBadgeTooltip, getStatusMeta } from '@/components/operator-ui'
import type {
  BotStatus,
  DependencyCheck,
  GateDecisionRecord,
  RuntimeVisibility,
  TradingMode,
} from '@/types'

function formatRelative(value?: string | null) {
  if (!value) return '—'
  return formatDistanceToNowStrict(new Date(value), { addSuffix: true })
}

function formatAbsolute(value?: string | null) {
  if (!value) return '—'
  return format(new Date(value), 'MMM d, yyyy h:mm:ss a')
}

function decisionTone(decision?: GateDecisionRecord | null) {
  if (!decision) return 'muted' as const
  return decision.allowed ? ('good' as const) : ('danger' as const)
}

function stateTone(state?: string | null) {
  switch (state) {
    case 'ARMED':
    case 'READY':
      return 'good' as const
    case 'PAUSED':
    case 'DEGRADED':
    case 'STALE':
    case 'STARTING':
      return 'warn' as const
    case 'DISABLED':
      return 'muted' as const
    case 'LOCKED':
    case 'READ_ONLY':
    case 'REJECTED':
    case 'MISSING':
      return 'danger' as const
    default:
      return 'muted' as const
  }
}

export default function Settings() {
  const queryClient = useQueryClient()
  const [message, setMessage] = useState('')
  const [errorMessage, setErrorMessage] = useState('')

  const { data: status } = useQuery<BotStatus>({
    queryKey: ['botStatus'],
    queryFn: api.getBotStatus,
    refetchInterval: 3000,
  })

  const { data: runtimeVisibility } = useQuery<RuntimeVisibility>({
    queryKey: ['runtimeVisibility'],
    queryFn: () => api.getRuntimeVisibility(10),
    refetchInterval: 10000,
  })

  const showSuccess = (text: string) => {
    setErrorMessage('')
    setMessage(text)
    window.setTimeout(() => setMessage(''), 3000)
  }

  const showError = (text: string) => {
    setMessage('')
    setErrorMessage(text)
    window.setTimeout(() => setErrorMessage(''), 5000)
  }

  const invalidateStatus = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['botStatus'] }),
      queryClient.invalidateQueries({ queryKey: ['runtimeVisibility'] }),
      queryClient.invalidateQueries({ queryKey: ['stockAccount'] }),
      queryClient.invalidateQueries({ queryKey: ['stockPositions'] }),
    ])
  }

  const toggleBotMutation = useMutation({
    mutationFn: (enabled: boolean) => api.toggleBot(enabled),
    onSuccess: async () => {
      await invalidateStatus()
      showSuccess('Bot status updated')
    },
    onError: (error: Error) => showError(error.message),
  })

  const setStockModeMutation = useMutation({
    mutationFn: (mode: TradingMode) => api.setStockMode(mode),
    onSuccess: async () => {
      await invalidateStatus()
      showSuccess('Stock account mode updated')
    },
    onError: (error: Error) => showError(error.message),
  })

  const toggleSafetyMutation = useMutation({
    mutationFn: (enabled: boolean) => api.toggleSafetyOverride(enabled),
    onSuccess: async () => {
      await invalidateStatus()
      showSuccess('Safety setting updated')
    },
    onError: (error: Error) => showError(error.message),
  })

  const stockReady = useMemo(
    () => ({
      paper: status?.stockCapabilities.paperReady ?? false,
      live: status?.stockCapabilities.liveReady ?? false,
    }),
    [status],
  )

  const dependencyChecks = runtimeVisibility?.dependencies.checks
  const gateSummary = runtimeVisibility?.gate.summary
  const recentGateDecisions = runtimeVisibility?.gate.recent ?? []
  const recentRejections = runtimeVisibility?.gate.recentRejections ?? []

  return (
    <div className="space-y-6">
      <PageHero
        eyebrow={
          <>
            <Shield className="h-4 w-4" />
            Runtime & risk
          </>
        }
        title="Control plane truth board"
        description="Real dependency probes, worker loop health, gate observations, and runtime controls. This page is the bot's actual pulse, not a decorative heartbeat lamp."
        aside={
          <>
            <StatusPill tone={getStatusMeta(status?.controlPlane.state).tone} label={`Control ${getStatusMeta(status?.controlPlane.state).canonicalLabel}`} />
            <StatusPill tone={getStatusMeta(status?.executionGate.state).tone} label={`Gate ${getStatusMeta(status?.executionGate.state).canonicalLabel}`} />
            <StatusPill tone={runtimeVisibility?.dependencies.summary.operationalReady ? 'good' : 'warn'} label={runtimeVisibility?.dependencies.summary.operationalReady ? 'Operationally ready' : 'Operational review needed'} />
          </>
        }
      />

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
        <MetricCard label="Control state" value={getStatusMeta(status?.controlPlane.state).canonicalLabel} detail={status?.controlPlane.reason ?? 'No control-plane status returned'} icon={<Shield className="h-5 w-5" />} />
        <MetricCard label="Gate" value={getStatusMeta(status?.executionGate.state).canonicalLabel} detail={status?.executionGate.reason || (status?.executionGate.allowed ? 'Execution path armed' : 'Execution currently blocked')} icon={<ArrowRightLeft className="h-5 w-5" />} />
        <MetricCard label="Operational readiness" value={runtimeVisibility?.dependencies.summary.operationalReady ? 'Ready' : 'Review'} detail={runtimeVisibility?.dependencies.summary.workerReady ? 'Dependency and worker probes look healthy' : 'At least one worker or dependency probe needs attention'} icon={<Wifi className="h-5 w-5" />} />
        <MetricCard label="Last heartbeat" value={formatRelative(status?.lastHeartbeat)} detail={formatAbsolute(status?.lastHeartbeat)} icon={<Clock3 className="h-5 w-5" />} />
      </div>

      {message ? <Notice tone="success" message={message} /> : null}
      {errorMessage ? <Notice tone="error" message={errorMessage} /> : null}

      <div className="grid grid-cols-1 gap-6 xl:grid-cols-[minmax(0,1.45fr)_minmax(320px,0.9fr)]">
        <div className="space-y-6">
          <section className="rounded-3xl border border-slate-800 bg-slate-900/70 p-5 shadow-xl shadow-slate-950/20">
            <div className="flex items-center gap-2 text-sm font-semibold text-slate-200">
              <PlayCircle className="h-4 w-4 text-emerald-300" />
              Runtime controls
            </div>

            <div className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-2">
              <ActionCard
                title="Bot runtime"
                description="The running flag is wired into the execution gate. Pause it here when you want the launch rails cold."
                footer={`Last heartbeat ${formatRelative(status?.lastHeartbeat)}`}
                action={
                  <button
                    onClick={() => toggleBotMutation.mutate(!(status?.running ?? false))}
                    disabled={toggleBotMutation.isPending}
                    className={`rounded-2xl px-4 py-2 text-sm font-semibold transition ${
                      status?.running
                        ? 'bg-amber-500/90 text-slate-950 hover:bg-amber-400'
                        : 'bg-emerald-500/90 text-slate-950 hover:bg-emerald-400'
                    } disabled:cursor-not-allowed disabled:opacity-60`}
                  >
                    {status?.running ? 'Pause Bot' : 'Resume Bot'}
                  </button>
                }
              >
                <div className="flex flex-wrap gap-2">
                  <StatusBadge tone={status?.running ? 'good' : 'warn'}>{status?.running ? 'Running' : 'Paused'}</StatusBadge>
                  <StatusBadge tone={status?.executionGate.allowed ? 'good' : 'danger'}>{status?.executionGate.state ?? 'UNKNOWN'}</StatusBadge>
                </div>
              </ActionCard>

              <ActionCard
                title="Stock account mode"
                description="Switch the active Tradier route without editing config by hand. Paper and live readiness are shown before you flip the lever."
                footer={`Current mode ${status?.stockMode ?? 'PAPER'}`}
                action={
                  <div className="flex flex-wrap gap-2">
                    <ModeButton
                      active={(status?.stockMode ?? 'PAPER') === 'PAPER'}
                      disabled={!stockReady.paper || setStockModeMutation.isPending}
                      label={stockReady.paper ? 'Use Paper' : 'Paper Missing'}
                      onClick={() => setStockModeMutation.mutate('PAPER')}
                    />
                    <ModeButton
                      active={(status?.stockMode ?? 'PAPER') === 'LIVE'}
                      disabled={!stockReady.live || setStockModeMutation.isPending}
                      label={stockReady.live ? 'Use Live' : 'Live Missing'}
                      onClick={() => setStockModeMutation.mutate('LIVE')}
                    />
                  </div>
                }
              >
                <div className="flex flex-wrap gap-2">
                  <StatusBadge tone={stockReady.paper ? 'good' : 'danger'}>{`Paper ${stockReady.paper ? 'Ready' : 'Missing'}`}</StatusBadge>
                  <StatusBadge tone={stockReady.live ? 'good' : 'warn'}>{`Live ${stockReady.live ? 'Ready' : 'Missing'}`}</StatusBadge>
                </div>
              </ActionCard>

              <ActionCard
                title="Market-hours guard"
                description="This is the stock-hours lock. Leave it on unless you intentionally want to let stocks wander off into the night."
                footer={`Safety override ${status?.safetyRequireMarketHours ? 'enabled' : 'disabled'}`}
                action={
                  <button
                    onClick={() => toggleSafetyMutation.mutate(!(status?.safetyRequireMarketHours ?? true))}
                    disabled={toggleSafetyMutation.isPending}
                    className={`rounded-2xl px-4 py-2 text-sm font-semibold transition ${
                      status?.safetyRequireMarketHours
                        ? 'bg-emerald-500/90 text-slate-950 hover:bg-emerald-400'
                        : 'bg-slate-700 text-slate-100 hover:bg-slate-600'
                    } disabled:cursor-not-allowed disabled:opacity-60`}
                  >
                    {status?.safetyRequireMarketHours ? 'Enabled' : 'Disabled'}
                  </button>
                }
              >
                <div className="flex flex-wrap gap-2">
                  <StatusBadge tone={status?.safetyRequireMarketHours ? 'good' : 'warn'}>
                    {status?.safetyRequireMarketHours ? 'Market-hours required' : 'Override active'}
                  </StatusBadge>
                </div>
              </ActionCard>

              <ActionCard
                title="Control plane truth"
                description="These state surfaces are the real supervisory rails behind the bot. If they sag, the gate should sag with them."
                footer={`Captured ${formatRelative(runtimeVisibility?.capturedAtUtc)}`}
                action={
                  <div className="flex flex-wrap gap-2">
                    <StatusBadge tone={stateTone(status?.controlPlane.state)}>{status?.controlPlane.state ?? 'UNKNOWN'}</StatusBadge>
                    <StatusBadge tone={runtimeVisibility?.dependencies.summary.operationalReady ? 'good' : 'warn'}>
                      {runtimeVisibility?.dependencies.summary.operationalReady ? 'Operationally ready' : 'Operational review'}
                    </StatusBadge>
                  </div>
                }
              >
                <div className="grid grid-cols-1 gap-3 text-sm text-slate-400 sm:grid-cols-2">
                  <MiniMetric label="Runtime running" value={status?.controlPlane.runtimeRunning ? 'true' : 'false'} />
                  <MiniMetric label="Admin API" value={status?.controlPlane.adminApiReady ? 'ready' : 'missing'} />
                  <MiniMetric label="Discord auth" value={status?.controlPlane.discordAuthReady ? 'ready' : 'missing'} />
                  <MiniMetric label="Authorization" value={status?.controlPlane.authorizationReady ? 'ready' : 'missing'} />
                </div>
              </ActionCard>
            </div>
          </section>

          <section className="rounded-3xl border border-slate-800 bg-slate-900/70 p-5 shadow-xl shadow-slate-950/20">
            <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
              <div className="flex items-center gap-2 text-sm font-semibold text-slate-200">
                <ArrowRightLeft className="h-4 w-4 text-cyan-300" />
                Recent gate decisions
              </div>
              <div className="flex flex-wrap gap-2">
                <StatusBadge tone="good">{`Allowed ${gateSummary?.allowedCount ?? 0}`}</StatusBadge>
                <StatusBadge tone="danger">{`Rejected ${gateSummary?.rejectedCount ?? 0}`}</StatusBadge>
              </div>
            </div>

            {recentGateDecisions.length === 0 ? (
              <EmptyState message="No gate decisions have been captured yet." />
            ) : (
              <div className="mt-4 overflow-x-auto">
                <table className="w-full min-w-[900px] text-sm">
                  <thead>
                    <tr className="border-b border-slate-800 text-left text-xs uppercase tracking-wide text-slate-500">
                      <th className="pb-3 pr-4">Time</th>
                      <th className="pb-3 pr-4">Symbol</th>
                      <th className="pb-3 pr-4">Asset</th>
                      <th className="pb-3 pr-4">Result</th>
                      <th className="pb-3 pr-4">State</th>
                      <th className="pb-3 pr-4">Reason</th>
                      <th className="pb-3 pr-4">Source</th>
                    </tr>
                  </thead>
                  <tbody>
                    {recentGateDecisions.map((decision) => (
                      <tr key={`${decision.recordedAtUtc}-${decision.symbol}-${decision.state}`} className="border-b border-slate-900/80 align-top text-slate-300">
                        <td className="py-3 pr-4 text-slate-400">
                          <div>{formatAbsolute(decision.recordedAtUtc)}</div>
                          <div className="text-xs text-slate-500">{formatRelative(decision.recordedAtUtc)}</div>
                        </td>
                        <td className="py-3 pr-4 font-semibold text-white">{decision.symbol}</td>
                        <td className="py-3 pr-4 uppercase text-slate-400">{decision.assetClass}</td>
                        <td className="py-3 pr-4">
                          <StatusBadge tone={decisionTone(decision)}>{decision.allowed ? 'ALLOWED' : 'REJECTED'}</StatusBadge>
                        </td>
                        <td className="py-3 pr-4">
                          <StatusBadge tone={stateTone(decision.state)}>{decision.state}</StatusBadge>
                        </td>
                        <td className="py-3 pr-4 text-slate-400">{decision.rejectionReason || 'Passed all checks'}</td>
                        <td className="py-3 pr-4 text-slate-400">{decision.executionSource}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </div>

        <aside className="space-y-6">
          <section className="rounded-3xl border border-slate-800 bg-slate-900/70 p-5">
            <div className="flex items-center gap-2 text-sm font-semibold text-slate-200">
              <Wifi className="h-4 w-4 text-emerald-300" />
              Dependency & worker probes
            </div>
            <div className="mt-4 space-y-3">
              {dependencyChecks ? (
                Object.values(dependencyChecks).map((check) => <DependencyTruthCard key={check.name} check={check} />)
              ) : (
                <EmptyState message="Waiting for dependency probes…" compact />
              )}
            </div>
          </section>

          <section className="rounded-3xl border border-slate-800 bg-slate-900/70 p-5">
            <div className="flex items-center gap-2 text-sm font-semibold text-slate-200">
              <Siren className="h-4 w-4 text-amber-300" />
              Rejection radar
            </div>

            <div className="mt-4 space-y-3">
              <SummaryRow label="Recent rejections" value={String(recentRejections.length)} />
              <SummaryRow label="Stale probes" value={String(runtimeVisibility?.dependencies.summary.staleCount ?? 0)} />
              <SummaryRow label="Disabled probes" value={String(runtimeVisibility?.dependencies.summary.disabledCount ?? 0)} />
              <SummaryRow label="Last rejection" value={formatRelative(gateSummary?.lastRejected?.recordedAtUtc ?? null)} />
              <SummaryRow label="Last allowed" value={formatRelative(gateSummary?.lastAllowed?.recordedAtUtc ?? null)} />
              <SummaryRow label="Observed" value={formatRelative(runtimeVisibility?.capturedAtUtc ?? null)} />
            </div>

            <div className="mt-4 space-y-3">
              {recentRejections.slice(0, 4).map((decision) => (
                <div key={`${decision.recordedAtUtc}-${decision.symbol}-reject`} className="rounded-2xl border border-rose-900/70 bg-rose-500/5 p-3">
                  <div className="flex items-start justify-between gap-3">
                    <div>
                      <div className="font-semibold text-white">{decision.symbol}</div>
                      <div className="text-xs uppercase tracking-wide text-slate-500">{decision.assetClass}</div>
                    </div>
                    <StatusBadge tone="danger">{decision.state}</StatusBadge>
                  </div>
                  <div className="mt-2 text-sm leading-6 text-slate-300">{decision.rejectionReason || 'Rejected without a recorded reason.'}</div>
                  <div className="mt-2 text-xs text-slate-500">{formatAbsolute(decision.recordedAtUtc)}</div>
                </div>
              ))}

              {recentRejections.length === 0 ? <EmptyState message="No recent rejections. The radar is quiet." compact /> : null}
            </div>
          </section>

          <section className="rounded-3xl border border-slate-800 bg-slate-900/70 p-5">
            <div className="flex items-center gap-2 text-sm font-semibold text-slate-200">
              <Clock3 className="h-4 w-4 text-cyan-300" />
              Operator notes
            </div>
            <ul className="mt-4 space-y-3 text-sm leading-6 text-slate-400">
              <li>The execution gate state matters more than the button color.</li>
              <li>Dependency readiness is broker truth, not optimism.</li>
              <li>Worker loops can go stale even when the API still answers. That is why this board now probes the rails, not just the paint.</li>
            </ul>
          </section>
        </aside>
      </div>
    </div>
  )
}

function ActionCard({
  title,
  description,
  footer,
  action,
  children,
}: {
  title: string
  description: string
  footer: string
  action: ReactNode
  children: ReactNode
}) {
  return (
    <div className="rounded-2xl border border-slate-800 bg-slate-950/60 p-4">
      <div className="flex flex-col gap-4">
        <div>
          <div className="font-semibold text-white">{title}</div>
          <div className="mt-1 text-sm leading-6 text-slate-400">{description}</div>
        </div>
        {children}
        <div className="flex flex-col gap-3 border-t border-slate-900 pt-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="text-xs uppercase tracking-wide text-slate-500">{footer}</div>
          {action}
        </div>
      </div>
    </div>
  )
}

function DependencyTruthCard({ check }: { check: DependencyCheck }) {
  const details = Object.entries(check.details ?? {})
    .filter(([, value]) => value !== null && value !== undefined && value !== '')
    .slice(0, 2)

  return (
    <div className="rounded-2xl border border-slate-800 bg-slate-950/60 p-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="font-semibold text-white">{humanize(check.name)}</div>
          <div className="mt-1 text-sm text-slate-400">{check.reason || 'Probe healthy.'}</div>
        </div>
        <StatusBadge tone={stateTone(check.state)}>{check.state}</StatusBadge>
      </div>
      <div className="mt-3 grid grid-cols-1 gap-2 text-sm text-slate-400">
        <MiniMetric label="Ready" value={check.ready ? 'true' : 'false'} />
        <MiniMetric label="Checked" value={formatRelative(check.checkedAtUtc)} />
        {details.map(([key, value]) => (
          <MiniMetric key={key} label={humanize(key)} value={formatDetailValue(value)} />
        ))}
      </div>
    </div>
  )
}

function SummaryRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between gap-4 border-b border-slate-900/80 pb-3 text-sm last:border-b-0 last:pb-0">
      <span className="text-slate-500">{label}</span>
      <span className="text-right text-slate-200">{value}</span>
    </div>
  )
}

function MiniMetric({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between gap-3 rounded-2xl border border-slate-900 bg-slate-900/70 px-3 py-2">
      <span className="text-slate-500">{label}</span>
      <span className="text-slate-200">{value}</span>
    </div>
  )
}

function Notice({ tone, message }: { tone: 'success' | 'error'; message: string }) {
  return (
    <div
      className={`rounded-2xl border px-4 py-3 text-sm ${
        tone === 'success'
          ? 'border-emerald-800/70 bg-emerald-500/10 text-emerald-300'
          : 'border-rose-800/70 bg-rose-500/10 text-rose-300'
      }`}
    >
      {message}
    </div>
  )
}

function ModeButton({
  active,
  disabled,
  label,
  onClick,
}: {
  active: boolean
  disabled: boolean
  label: string
  onClick: () => void
}) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className={`rounded-2xl px-4 py-2 text-sm font-semibold transition ${
        active
          ? 'bg-cyan-500/90 text-slate-950 hover:bg-cyan-400'
          : 'bg-slate-800 text-slate-200 hover:bg-slate-700'
      } disabled:cursor-not-allowed disabled:opacity-50`}
    >
      {label}
    </button>
  )
}

function EmptyState({ message, compact = false }: { message: string; compact?: boolean }) {
  return (
    <div
      className={`rounded-2xl border border-dashed border-slate-800 text-center text-sm text-slate-500 ${
        compact ? 'px-4 py-5' : 'px-4 py-8'
      }`}
    >
      {message}
    </div>
  )
}

function StatusBadge({
  tone,
  children,
  tooltip,
}: {
  tone: 'good' | 'warn' | 'danger' | 'info' | 'muted'
  children: string
  tooltip?: string | null
}) {
  const className =
    tone === 'good'
      ? 'border-emerald-800/70 bg-emerald-500/10 text-emerald-300'
      : tone === 'warn'
        ? 'border-amber-800/70 bg-amber-500/10 text-amber-300'
        : tone === 'danger'
          ? 'border-rose-800/70 bg-rose-500/10 text-rose-300'
          : tone === 'info'
            ? 'border-cyan-800/70 bg-cyan-500/10 text-cyan-300'
            : 'border-slate-800 bg-slate-950/80 text-slate-300'

  const hint = (tooltip ?? getBadgeTooltip(children) ?? '').trim() || null

  return (
    <span className="group relative inline-flex">
      <span title={hint ?? undefined} className={`rounded-full border px-2.5 py-1 text-xs font-medium ${className}`}>{children}</span>
      {hint ? (
        <span className="pointer-events-none absolute left-1/2 top-full z-30 mt-2 hidden w-56 -translate-x-1/2 rounded-2xl border border-slate-700 bg-slate-950/95 px-3 py-2 text-left text-xs font-medium text-slate-200 shadow-2xl group-hover:block">
          {hint}
        </span>
      ) : null}
    </span>
  )
}

function humanize(value: string) {
  return value
    .replace(/([a-z])([A-Z])/g, '$1 $2')
    .replace(/_/g, ' ')
    .replace(/\b\w/g, (char) => char.toUpperCase())
}

function formatDetailValue(value: unknown) {
  if (typeof value === 'boolean') return value ? 'true' : 'false'
  if (typeof value === 'number') return Number.isFinite(value) ? String(value) : '—'
  if (typeof value === 'string') return value
  if (Array.isArray(value)) return `${value.length} items`
  if (value && typeof value === 'object') return 'Structured detail'
  return '—'
}
