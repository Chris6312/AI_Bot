import type { ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Activity, Clock3, Radar, ShieldCheck, Siren } from 'lucide-react'
import { formatDistanceToNowStrict } from 'date-fns'

import { api } from '@/lib/api'
import type {
  WatchlistExitReadinessSnapshot,
  WatchlistExitWorkerStatus,
  WatchlistMonitoringSnapshot,
  WatchlistOrchestrationStatus,
  WatchlistScope,
  WatchlistSymbolRecord,
} from '@/types'

type MonitoringCollection = Partial<Record<WatchlistScope, WatchlistMonitoringSnapshot>>
type ExitReadinessCollection = Partial<Record<WatchlistScope, WatchlistExitReadinessSnapshot>>

const scopeLabels: Record<WatchlistScope, string> = {
  stocks_only: 'Stocks',
  crypto_only: 'Crypto',
}

export default function Monitoring() {
  const { data: monitoring = {} } = useQuery<MonitoringCollection>({
    queryKey: ['watchlists', 'monitoring'],
    queryFn: () => api.getMonitoringSnapshot() as Promise<MonitoringCollection>,
    refetchInterval: 10000,
  })

  const { data: orchestration } = useQuery<WatchlistOrchestrationStatus>({
    queryKey: ['watchlists', 'orchestration'],
    queryFn: () => api.getOrchestrationStatus(),
    refetchInterval: 10000,
  })

  const { data: exitReadiness = {} } = useQuery<ExitReadinessCollection>({
    queryKey: ['watchlists', 'exitReadiness'],
    queryFn: () => api.getExitReadiness() as Promise<ExitReadinessCollection>,
    refetchInterval: 10000,
  })

  const { data: exitWorker } = useQuery<WatchlistExitWorkerStatus>({
    queryKey: ['watchlists', 'exitWorker'],
    queryFn: api.getExitWorkerStatus,
    refetchInterval: 10000,
  })

  const totalActive = Object.values(monitoring).reduce((sum, scope) => sum + (scope?.summary.activeCount ?? 0), 0)
  const totalEntry = Object.values(monitoring).reduce((sum, scope) => sum + (scope?.summary.entryCandidateCount ?? 0), 0)
  const totalOpenPositions = Object.values(exitReadiness).reduce((sum, scope) => sum + (scope?.summary.openPositionCount ?? 0), 0)
  const totalProtective = Object.values(exitReadiness).reduce((sum, scope) => sum + (scope?.summary.protectiveExitPendingCount ?? 0), 0)

  return (
    <div className="space-y-6">
      <header className="rounded-3xl border border-slate-800 bg-slate-900/70 p-6 shadow-2xl shadow-slate-950/30">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
          <div>
            <div className="mb-2 flex items-center gap-2 text-sm font-medium uppercase tracking-[0.22em] text-cyan-300">
              <Radar className="h-4 w-4" />
              Monitoring engine
            </div>
            <h1 className="text-3xl font-semibold text-white">Due runs, decision states, and exit pressure</h1>
            <p className="mt-2 max-w-3xl text-sm leading-6 text-slate-400">
              Session-aware sweeps for stocks, 24/7 sweeps for crypto, plus the exit worker standing by with a clipboard and a sharp pencil.
            </p>
          </div>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <HeadlineMetric label="Active rows" value={String(totalActive)} />
            <HeadlineMetric label="Entry candidates" value={String(totalEntry)} />
            <HeadlineMetric label="Open positions" value={String(totalOpenPositions)} />
            <HeadlineMetric label="Protective exits" value={String(totalProtective)} />
          </div>
        </div>
      </header>

      <div className="grid grid-cols-1 gap-6 xl:grid-cols-[minmax(0,1.65fr)_minmax(320px,0.95fr)]">
        <div className="space-y-6">
          {(['stocks_only', 'crypto_only'] as WatchlistScope[]).map((scope) => (
            <ScopeMonitoringPanel
              key={scope}
              scope={scope}
              monitoring={monitoring[scope]}
              exitReadiness={exitReadiness[scope]}
              orchestration={extractScopeSnapshot(orchestration, scope)}
            />
          ))}
        </div>

        <aside className="space-y-6">
          <RuntimeCard
            title="Monitoring orchestrator"
            icon={<Activity className="h-4 w-4 text-cyan-300" />}
            enabled={orchestration?.enabled}
            pollSeconds={orchestration?.pollSeconds}
            lastStartedAtUtc={orchestration?.lastStartedAtUtc ?? null}
            lastFinishedAtUtc={orchestration?.lastFinishedAtUtc ?? null}
            lastError={orchestration?.lastError ?? null}
            extraRows={[
              ['Eligible due', String(orchestration?.dueSnapshot && 'summary' in orchestration.dueSnapshot ? orchestration.dueSnapshot.summary.eligibleDueCount : 0)],
              ['Blocked due', String(orchestration?.dueSnapshot && 'summary' in orchestration.dueSnapshot ? orchestration.dueSnapshot.summary.blockedDueCount : 0)],
            ]}
          />

          <RuntimeCard
            title="Exit worker"
            icon={<Siren className="h-4 w-4 text-amber-300" />}
            enabled={exitWorker?.enabled}
            pollSeconds={exitWorker?.pollSeconds}
            lastStartedAtUtc={exitWorker?.lastStartedAtUtc ?? null}
            lastFinishedAtUtc={exitWorker?.lastFinishedAtUtc ?? null}
            lastError={exitWorker?.lastError ?? null}
            extraRows={[
              ['Eligible exits', String(exitWorker?.summary.eligibleExitCount ?? 0)],
              ['Blocked exits', String(exitWorker?.summary.blockedExitCount ?? 0)],
              ['Already in progress', String(exitWorker?.summary.alreadyInProgressCount ?? 0)],
            ]}
          />

          <div className="rounded-3xl border border-slate-800 bg-slate-900/70 p-5">
            <div className="flex items-center gap-2 text-sm font-semibold text-slate-200">
              <ShieldCheck className="h-4 w-4 text-emerald-300" />
              Exit triggers in play
            </div>
            <div className="mt-4 space-y-3 text-sm text-slate-400">
              <SummaryRow label="Expired" value={String(exitWorker?.summary.expiredPositionCount ?? 0)} />
              <SummaryRow label="Protective" value={String(exitWorker?.summary.protectiveExitCount ?? 0)} />
              <SummaryRow label="Profit target" value={String(exitWorker?.summary.profitTargetCount ?? 0)} />
              <SummaryRow label="Failed follow-through" value={String(exitWorker?.summary.followThroughExitCount ?? 0)} />
            </div>
          </div>
        </aside>
      </div>
    </div>
  )
}

function ScopeMonitoringPanel({
  scope,
  monitoring,
  exitReadiness,
  orchestration,
}: {
  scope: WatchlistScope
  monitoring?: WatchlistMonitoringSnapshot
  exitReadiness?: WatchlistExitReadinessSnapshot
  orchestration?: {
    dueCount: number
    eligibleDueCount: number
    blockedDueCount: number
    session: {
      sessionOpen: boolean
      sessionLabel: string
      nextOpenUtc?: string | null
      nextCloseUtc?: string | null
    }
  }
}) {
  const rows = monitoring?.rows ?? []

  return (
    <section className="rounded-3xl border border-slate-800 bg-slate-900/70 p-5 shadow-xl shadow-slate-950/20">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div>
          <div className="text-sm font-semibold uppercase tracking-[0.18em] text-slate-500">{scopeLabels[scope]}</div>
          <h2 className="mt-1 text-2xl font-semibold text-white">Monitoring snapshot</h2>
          <div className="mt-3 flex flex-wrap gap-2">
            <StatusBadge tone={orchestration?.session.sessionOpen ? 'good' : 'warn'}>
              {orchestration?.session.sessionLabel ?? 'Unknown session'}
            </StatusBadge>
            <StatusBadge tone="muted">Due {orchestration?.dueCount ?? 0}</StatusBadge>
            <StatusBadge tone="info">Eligible {orchestration?.eligibleDueCount ?? 0}</StatusBadge>
            {orchestration?.blockedDueCount ? <StatusBadge tone="warn">Blocked {orchestration.blockedDueCount}</StatusBadge> : null}
          </div>
        </div>

        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <MiniCard label="Entry" value={String(monitoring?.summary.entryCandidateCount ?? 0)} />
          <MiniCard label="Waiting" value={String(monitoring?.summary.waitingForSetupCount ?? 0)} />
          <MiniCard label="Open" value={String(exitReadiness?.summary.openPositionCount ?? 0)} />
          <MiniCard label="Expired" value={String(exitReadiness?.summary.expiredPositionCount ?? 0)} />
        </div>
      </div>

      <div className="mt-5 grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,1.3fr)_minmax(260px,0.7fr)]">
        <div className="rounded-2xl border border-slate-800 bg-slate-950/60 p-4">
          {rows.length === 0 ? (
            <EmptyState message="No monitoring rows are available for this scope yet." />
          ) : (
            <MonitoringTable rows={rows} />
          )}
        </div>

        <div className="space-y-4">
          <SummaryCard
            title="Monitoring summary"
            rows={[
              ['Pending evaluation', String(monitoring?.summary.pendingEvaluationCount ?? 0)],
              ['Monitor only', String(monitoring?.summary.monitorOnlyCount ?? 0)],
              ['Data stale', String(monitoring?.summary.dataStaleCount ?? 0)],
              ['Data unavailable', String(monitoring?.summary.dataUnavailableCount ?? 0)],
              ['Bias conflict', String(monitoring?.summary.biasConflictCount ?? 0)],
              ['Evaluation blocked', String(monitoring?.summary.evaluationBlockedCount ?? 0)],
            ]}
          />
          <SummaryCard
            title="Exit pressure"
            rows={[
              ['Protective pending', String(exitReadiness?.summary.protectiveExitPendingCount ?? 0)],
              ['Scale-out ready', String(exitReadiness?.summary.scaleOutReadyCount ?? 0)],
              ['Follow-through failed', String(exitReadiness?.summary.followThroughFailedCount ?? 0)],
              ['Impulse trail armed', String(exitReadiness?.summary.impulseTrailArmedCount ?? 0)],
              ['Time-stop extended', String(exitReadiness?.summary.timeStopExtendedCount ?? 0)],
            ]}
          />
        </div>
      </div>
    </section>
  )
}

function MonitoringTable({ rows }: { rows: WatchlistSymbolRecord[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="min-w-full text-sm">
        <thead>
          <tr className="border-b border-slate-800 text-left text-xs uppercase tracking-wide text-slate-500">
            <th className="pb-3 pr-4">Symbol</th>
            <th className="pb-3 pr-4">Decision</th>
            <th className="pb-3 pr-4">Reason</th>
            <th className="pb-3 pr-4">Next eval</th>
            <th className="pb-3 pr-4">Position</th>
            <th className="pb-3 pr-4">Exit flags</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={`${row.uploadId}-${row.symbol}`} className="border-b border-slate-900/80 align-top text-slate-300">
              <td className="py-3 pr-4">
                <div className="font-semibold text-white">{row.symbol}</div>
                <div className="text-xs text-slate-500">{row.monitoringStatus}</div>
              </td>
              <td className="py-3 pr-4">
                <StatusBadge tone={decisionTone(row.monitoring?.latestDecisionState)}>
                  {row.monitoring?.latestDecisionState ?? 'UNKNOWN'}
                </StatusBadge>
              </td>
              <td className="py-3 pr-4 text-slate-400">{row.monitoring?.latestDecisionReason ?? '—'}</td>
              <td className="py-3 pr-4 text-slate-400">{formatTimestamp(row.monitoring?.nextEvaluationAtUtc)}</td>
              <td className="py-3 pr-4 text-slate-400">
                {row.positionState?.hasOpenPosition ? (
                  <div>
                    <div className="font-medium text-slate-200">Open</div>
                    <div className="text-xs text-slate-500">
                      {row.positionState.positionExpired
                        ? 'Expired'
                        : row.positionState.hoursUntilExpiry != null
                          ? `${row.positionState.hoursUntilExpiry.toFixed(1)}h left`
                          : 'Watching'}
                    </div>
                  </div>
                ) : (
                  'Flat'
                )}
              </td>
              <td className="py-3 pr-4 text-slate-400">{buildExitFlags(row)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function RuntimeCard({
  title,
  icon,
  enabled,
  pollSeconds,
  lastStartedAtUtc,
  lastFinishedAtUtc,
  lastError,
  extraRows,
}: {
  title: string
  icon: ReactNode
  enabled?: boolean
  pollSeconds?: number
  lastStartedAtUtc: string | null
  lastFinishedAtUtc: string | null
  lastError: string | null
  extraRows: [string, string][]
}) {
  return (
    <div className="rounded-3xl border border-slate-800 bg-slate-900/70 p-5">
      <div className="flex items-center gap-2 text-sm font-semibold text-slate-200">
        {icon}
        {title}
      </div>
      <div className="mt-4 space-y-3 text-sm text-slate-400">
        <SummaryRow label="Enabled" value={enabled ? 'Yes' : 'No'} />
        <SummaryRow label="Poll seconds" value={pollSeconds != null ? String(pollSeconds) : '—'} />
        <SummaryRow label="Last started" value={formatTimestamp(lastStartedAtUtc)} />
        <SummaryRow label="Last finished" value={formatTimestamp(lastFinishedAtUtc)} />
        {extraRows.map(([label, value]) => (
          <SummaryRow key={label} label={label} value={value} />
        ))}
        <SummaryRow label="Last error" value={lastError ?? 'None'} />
      </div>
    </div>
  )
}

function SummaryCard({ title, rows }: { title: string; rows: [string, string][] }) {
  return (
    <div className="rounded-2xl border border-slate-800 bg-slate-950/60 p-4">
      <div className="mb-4 flex items-center gap-2 text-sm font-semibold text-slate-200">
        <Clock3 className="h-4 w-4 text-slate-400" />
        {title}
      </div>
      <div className="space-y-3 text-sm text-slate-400">
        {rows.map(([label, value]) => (
          <SummaryRow key={label} label={label} value={value} />
        ))}
      </div>
    </div>
  )
}

function HeadlineMetric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-2xl border border-slate-800 bg-slate-950/60 px-4 py-3">
      <div className="text-xs uppercase tracking-wide text-slate-500">{label}</div>
      <div className="mt-1 text-2xl font-semibold text-white">{value}</div>
    </div>
  )
}

function MiniCard({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-2xl border border-slate-800 bg-slate-950/60 px-3 py-3">
      <div className="text-[11px] uppercase tracking-wide text-slate-500">{label}</div>
      <div className="mt-1 text-lg font-semibold text-white">{value}</div>
    </div>
  )
}

function SummaryRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-start justify-between gap-4 border-b border-slate-900/90 pb-3 last:border-b-0 last:pb-0">
      <span className="text-slate-500">{label}</span>
      <span className="max-w-[60%] text-right text-slate-200">{value}</span>
    </div>
  )
}

function EmptyState({ message }: { message: string }) {
  return <div className="rounded-2xl border border-dashed border-slate-800 px-4 py-8 text-center text-sm text-slate-500">{message}</div>
}

function StatusBadge({ tone, children }: { tone: 'good' | 'warn' | 'info' | 'muted'; children: string }) {
  const className =
    tone === 'good'
      ? 'border-emerald-800/70 bg-emerald-500/10 text-emerald-300'
      : tone === 'warn'
        ? 'border-amber-800/70 bg-amber-500/10 text-amber-300'
        : tone === 'info'
          ? 'border-cyan-800/70 bg-cyan-500/10 text-cyan-300'
          : 'border-slate-800 bg-slate-950/80 text-slate-300'

  return <span className={`rounded-full border px-2.5 py-1 text-xs font-medium ${className}`}>{children}</span>
}

function formatTimestamp(value?: string | null) {
  if (!value) return '—'
  const date = new Date(value)
  return `${date.toLocaleString()} · ${formatDistanceToNowStrict(date, { addSuffix: true })}`
}

function buildExitFlags(row: WatchlistSymbolRecord) {
  const flags: string[] = []
  if (row.positionState?.protectiveExitPending) flags.push('protective')
  if (row.positionState?.scaleOutReady) flags.push('scale-out')
  if (row.positionState?.followThroughFailed) flags.push('follow-through')
  if (row.positionState?.timeStopExtended) flags.push('extended')
  if (flags.length === 0) return '—'
  return flags.join(', ')
}

function decisionTone(value?: string) {
  switch (value) {
    case 'ENTRY_CANDIDATE':
      return 'good'
    case 'WAITING_FOR_SETUP':
    case 'MONITOR_ONLY':
      return 'info'
    case 'DATA_STALE':
    case 'DATA_UNAVAILABLE':
    case 'EVALUATION_BLOCKED':
      return 'warn'
    default:
      return 'muted'
  }
}

function extractScopeSnapshot(orchestration: WatchlistOrchestrationStatus | undefined, scope: WatchlistScope) {
  const dueSnapshot = orchestration?.dueSnapshot
  if (!dueSnapshot || !('scopes' in dueSnapshot)) return undefined
  return dueSnapshot.scopes[scope]
}
