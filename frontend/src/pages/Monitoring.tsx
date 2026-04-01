import type { ReactNode } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Activity, ArrowRight, Clock3, Radar, ShieldCheck, Siren } from 'lucide-react'
import { formatDistanceToNowStrict } from 'date-fns'

import { api } from '@/lib/api'
import {
  EmptyState,
  MetricCard,
  PageHero,
  StatusPill,
  ToneBadge,
  getScopeSessionMeta,
  getStatusMeta,
} from '@/components/operator-ui'
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
  const [searchParams] = useSearchParams()
  const symbolFilter = (searchParams.get('symbol') ?? '').trim().toUpperCase()
  const scopeFilter = (searchParams.get('scope') ?? '').trim() as WatchlistScope | ''
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

  const totalHealthy = Object.values(monitoring).reduce((sum, scope) => sum + (scope?.summary.activeCount ?? 0), 0)
  const totalEntry = Object.values(monitoring).reduce((sum, scope) => sum + (scope?.summary.entryCandidateCount ?? 0), 0)
  const totalOpenPositions = Object.values(exitReadiness).reduce((sum, scope) => sum + (scope?.summary.openPositionCount ?? 0), 0)
  const totalProtective = Object.values(exitReadiness).reduce((sum, scope) => sum + (scope?.summary.protectiveExitPendingCount ?? 0), 0)

  return (
    <div className="space-y-6">
      <PageHero
        eyebrow={
          <>
            <Radar className="h-4 w-4" />
            Monitoring engine
          </>
        }
        title="Due runs, decision states, and exit pressure"
        description="Session-aware sweeps for stocks, 24/7 sweeps for crypto, and an exit worker that keeps expired or protective positions from drifting into the weeds."
        aside={
          <>
            <StatusPill tone={Boolean(orchestration?.enabled) ? 'good' : 'warn'} label={orchestration?.enabled ? 'Monitor healthy' : 'Monitor warning'} />
            <StatusPill tone={Boolean(exitWorker?.enabled) ? 'good' : 'warn'} label={exitWorker?.enabled ? 'Exit worker healthy' : 'Exit worker warning'} />
            <StatusPill tone={totalProtective > 0 ? 'warn' : 'muted'} label={totalProtective > 0 ? `${totalProtective} protective exits` : 'No protective exits'} />
            {symbolFilter ? <StatusPill tone="info" label={`Filtered: ${symbolFilter}`} /> : null}
          </>
        }
      />

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
        <MetricCard label="Healthy rows" value={String(totalHealthy)} detail="Symbols still on the monitoring rail" icon={<Activity className="h-5 w-5" />} />
        <MetricCard label="Entry candidates" value={String(totalEntry)} detail="Rows currently closest to a deterministic entry" icon={<Radar className="h-5 w-5" />} />
        <MetricCard label="Open positions" value={String(totalOpenPositions)} detail="Rows with live position state attached" icon={<ShieldCheck className="h-5 w-5" />} />
        <MetricCard label="Protective exits" value={String(totalProtective)} detail="Stops, trails, or follow-through exits needing attention" icon={<Siren className="h-5 w-5" />} />
      </div>

      <div className="grid grid-cols-1 gap-6 xl:grid-cols-3">
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
      </div>

      <div className="space-y-6">
        {(['stocks_only', 'crypto_only'] as WatchlistScope[]).map((scope) => (
          <ScopeMonitoringPanel
            key={scope}
            scope={scope}
            monitoring={monitoring[scope]}
            exitReadiness={exitReadiness[scope]}
            orchestration={extractScopeSnapshot(orchestration, scope)}
            selectedSymbol={scopeFilter === '' || scopeFilter === scope ? symbolFilter || null : null}
          />
        ))}
      </div>
    </div>
  )
}

function ScopeMonitoringPanel({
  scope,
  monitoring,
  exitReadiness,
  orchestration,
  selectedSymbol,
}: {
  scope: WatchlistScope
  monitoring?: WatchlistMonitoringSnapshot
  exitReadiness?: WatchlistExitReadinessSnapshot
  orchestration?: {
    dueCount: number
    eligibleDueCount: number
    blockedDueCount: number
    session?: {
      sessionOpen?: boolean
      reason?: string | null
      sessionLabel?: string | null
      nextSessionStartUtc?: string | null
      nextSessionStartEt?: string | null
      sessionCloseUtc?: string | null
      sessionCloseEt?: string | null
      nextOpenUtc?: string | null
      nextCloseUtc?: string | null
    }
  }
  selectedSymbol?: string | null
}) {
  const rows = monitoring?.rows ?? []
  const filteredRows = selectedSymbol ? rows.filter((row) => row.symbol.toUpperCase() === selectedSymbol.toUpperCase()) : rows
  const sessionMeta = getScopeSessionMeta(scope, orchestration?.session)

  return (
    <section className="rounded-3xl border border-slate-800 bg-slate-900/70 p-5 shadow-xl shadow-slate-950/20">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div>
          <div className="text-sm font-semibold uppercase tracking-[0.18em] text-slate-500">{scopeLabels[scope]}</div>
          <h2 className="mt-1 text-2xl font-semibold text-white">Monitoring snapshot</h2>
          <div className="mt-3 flex flex-wrap gap-2">
            <ToneBadge tone={sessionMeta.tone} tooltip={sessionMeta.detail ?? undefined}>{sessionMeta.label}</ToneBadge>
            {sessionMeta.detail ? <span className="self-center text-xs text-slate-500">{sessionMeta.detail}</span> : null}
            <ToneBadge tone="muted" tooltip="Rows scheduled for evaluation in this scope.">Due {orchestration?.dueCount ?? 0}</ToneBadge>
            <ToneBadge tone="info" tooltip="Rows due now and currently unblocked for evaluation.">Eligible {orchestration?.eligibleDueCount ?? 0}</ToneBadge>
            {orchestration?.blockedDueCount ? <ToneBadge tone="warn" tooltip="Rows due for evaluation but blocked by session, data freshness, or control state.">Blocked {orchestration.blockedDueCount}</ToneBadge> : null}
          </div>
        </div>

        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <MiniCard label="Entry" value={String(monitoring?.summary.entryCandidateCount ?? 0)} />
          <MiniCard label="Warning" value={String(monitoring?.summary.waitingForSetupCount ?? 0)} />
          <MiniCard label="Open" value={String(exitReadiness?.summary.openPositionCount ?? 0)} />
          <MiniCard label="Blocked" value={String((monitoring?.summary.dataUnavailableCount ?? 0) + (monitoring?.summary.evaluationBlockedCount ?? 0))} />
        </div>
      </div>

      <div className="mt-5 grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,1.75fr)_minmax(340px,0.95fr)] 2xl:grid-cols-[minmax(0,1.95fr)_minmax(380px,0.9fr)]">
        <div className="rounded-2xl border border-slate-800 bg-slate-950/60 p-4">
          {!monitoring ? (
            <EmptyState message={`No ${scopeLabels[scope].toLowerCase()} monitoring snapshot is available yet.`} />
          ) : filteredRows.length === 0 ? (
            <EmptyState message="A monitoring snapshot exists, but there are no rows to display yet." />
          ) : (
            <MonitoringTable scope={scope} rows={filteredRows} selectedSymbol={selectedSymbol ?? null} />
          )}
        </div>

        <div className="space-y-4">
          <SummaryCard
            title="Monitoring summary"
            rows={[
              ['Pending evaluation', String(monitoring?.summary.pendingEvaluationCount ?? 0)],
              ['Warning state', String(monitoring?.summary.waitingForSetupCount ?? 0)],
              ['Stale', String(monitoring?.summary.dataStaleCount ?? 0)],
              ['Blocked', String((monitoring?.summary.dataUnavailableCount ?? 0) + (monitoring?.summary.evaluationBlockedCount ?? 0) + (monitoring?.summary.biasConflictCount ?? 0))],
              ['Managed-only', String(monitoring?.summary.managedOnlyCount ?? 0)],
              ['Unmanaged', String(monitoring?.summary.inactiveCount ?? 0)],
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

function MonitoringTable({ scope, rows, selectedSymbol }: { scope: WatchlistScope; rows: WatchlistSymbolRecord[]; selectedSymbol: string | null }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[1380px] text-sm">
        <thead>
          <tr className="border-b border-slate-800 text-left text-xs uppercase tracking-wide text-slate-500">
            <th className="w-[150px] pb-3 pr-4">Symbol</th>
            <th className="w-[180px] pb-3 pr-4">Lifecycle</th>
            <th className="w-[180px] pb-3 pr-4">Decision</th>
            <th className="w-[360px] pb-3 pr-4">Reason</th>
            <th className="w-[220px] pb-3 pr-4">Next eval</th>
            <th className="w-[180px] pb-3 pr-4">Position</th>
            <th className="w-[180px] pb-3 pr-4">Exit flags</th>
            <th className="w-[230px] pb-3 pr-4">Jump lanes</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const lifecycleMeta = getStatusMeta(row.monitoringStatus)
            const decisionMeta = getStatusMeta(row.monitoring?.latestDecisionState)

            const entryExecution = (row.monitoring?.decisionContext as Record<string, unknown> | undefined)?.entryExecution as Record<string, unknown> | undefined
            const executionAction = String(entryExecution?.action ?? '').trim()
            const executionReason = String(entryExecution?.reason ?? '').trim()
            const isFocused = selectedSymbol != null && row.symbol.toUpperCase() === selectedSymbol.toUpperCase()

            return (
              <tr key={`${row.uploadId}-${row.symbol}`} className={`border-b border-slate-900/80 align-top text-slate-300 ${isFocused ? 'bg-cyan-500/5' : ''}`}>
                <td className="py-3 pr-4 align-top">
                  <div className="font-semibold text-white">{row.symbol}</div>
                  <div className="text-xs text-slate-500">{row.assetClass}</div>
                </td>
                <td className="py-3 pr-4 align-top">
                  <div className="flex flex-col gap-2">
                    <ToneBadge tone={lifecycleMeta.tone}>{lifecycleMeta.canonicalLabel}</ToneBadge>
                    <span className="text-xs text-slate-500">Raw: {lifecycleMeta.rawLabel}</span>
                  </div>
                </td>
                <td className="py-3 pr-4 align-top">
                  <div className="flex flex-col gap-2">
                    <ToneBadge tone={decisionMeta.tone}>{decisionMeta.canonicalLabel}</ToneBadge>
                    <span className="text-xs text-slate-500">Raw: {decisionMeta.rawLabel}</span>
                  </div>
                </td>
                <td className="py-3 pr-4 align-top text-slate-400 whitespace-normal break-words [overflow-wrap:anywhere]">
                  <div>{row.monitoring?.latestDecisionReason ?? '—'}</div>
                  {executionAction ? <div className="mt-2 text-xs text-slate-500">Entry rail: {executionAction}{executionReason ? ` · ${executionReason}` : ''}</div> : null}
                </td>
                <td className="py-3 pr-4 align-top text-slate-400 whitespace-normal break-words">
                  {formatTimestamp(row.monitoring?.nextEvaluationAtUtc)}
                </td>
                <td className="py-3 pr-4 align-top text-slate-400">
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
                <td className="py-3 pr-4 align-top text-slate-400 whitespace-normal break-words">{buildExitFlags(row)}</td>
                <td className="py-3 pr-4 align-top">
                  <div className="flex flex-wrap gap-2">
                    <JumpLane to="/watchlists" label="Watchlists" scope={scope} symbol={row.symbol} />
                    <JumpLane to="/positions" label="Positions" scope={scope} symbol={row.symbol} />
                    <JumpLane to="/audit" label="Audit" scope={scope} symbol={row.symbol} />
                  </div>
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function JumpLane({ to, label, scope, symbol }: { to: string; label: string; scope?: WatchlistScope; symbol?: string }) {
  const params = new URLSearchParams()
  if (scope) params.set('scope', scope)
  if (symbol) params.set('symbol', symbol)
  const href = params.toString() ? `${to}?${params.toString()}` : to
  return (
    <Link
      to={href}
      className="inline-flex items-center gap-2 rounded-full border border-slate-700 bg-slate-900/70 px-2.5 py-1 text-[11px] text-slate-200 transition hover:border-cyan-700 hover:text-white"
    >
      <span>{label}</span>
      <ArrowRight className="h-3.5 w-3.5 text-cyan-300" />
    </Link>
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
  const stateMeta = getStatusMeta(enabled ? 'READY' : 'PAUSED')

  return (
    <div className="rounded-3xl border border-slate-800 bg-slate-900/70 p-5">
      <div className="flex items-center gap-2 text-sm font-semibold text-slate-200">
        {icon}
        {title}
      </div>
      <div className="mt-3 flex flex-wrap gap-2">
        <ToneBadge tone={stateMeta.tone}>{stateMeta.canonicalLabel}</ToneBadge>
        {lastError ? <ToneBadge tone="warn">Last error present</ToneBadge> : <ToneBadge tone="good">No recent error</ToneBadge>}
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
      <span className="max-w-[60%] break-words text-right text-slate-200">{value}</span>
    </div>
  )
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

function extractScopeSnapshot(orchestration: WatchlistOrchestrationStatus | undefined, scope: WatchlistScope) {
  const dueSnapshot = orchestration?.dueSnapshot
  if (!dueSnapshot || !('scopes' in dueSnapshot)) return undefined
  return dueSnapshot.scopes[scope]
}
