import { useMemo } from 'react'
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { formatDistanceToNow } from 'date-fns'
import {
  Activity,
  ArrowRight,
  ClipboardList,
  FileSearch,
  Radar,
  Shield,
  TrendingUp,
  Wallet,
} from 'lucide-react'

import { api } from '@/lib/api'
import type {
  BotStatus,
  CryptoLedger,
  MarketStatus,
  RuntimeVisibility,
  StockAccount,
  WatchlistExitReadinessSnapshot,
  WatchlistMonitoringSnapshot,
  WatchlistOrchestrationStatus,
  WatchlistScope,
  WatchlistUploadRecord,
} from '@/types'

const scopeLabels: Record<WatchlistScope, string> = {
  stocks_only: 'Stocks',
  crypto_only: 'Crypto',
}

function formatMoney(value: number) {
  return `$${value.toFixed(2)}`
}

function formatRelative(value?: string | null) {
  if (!value) return '—'
  return formatDistanceToNow(new Date(value), { addSuffix: true })
}

function isHealthyValidationStatus(status?: string | null) {
  const normalized = (status ?? '').trim().toLowerCase()
  return normalized === 'accepted' || normalized === 'valid'
}

function stateTone(state?: string | null) {
  switch (state) {
    case 'ARMED':
    case 'READY':
      return 'good' as const
    case 'PAUSED':
    case 'DEGRADED':
      return 'warn' as const
    case 'LOCKED':
    case 'READ_ONLY':
    case 'REJECTED':
    case 'MISSING':
      return 'danger' as const
    default:
      return 'muted' as const
  }
}

export default function Dashboard() {
  const { data: botStatus } = useQuery<BotStatus>({
    queryKey: ['botStatus'],
    queryFn: api.getBotStatus,
    refetchInterval: 3000,
  })

  const { data: runtimeVisibility } = useQuery<RuntimeVisibility>({
    queryKey: ['runtimeVisibility'],
    queryFn: () => api.getRuntimeVisibility(8),
    refetchInterval: 10000,
  })

  const { data: marketStatus } = useQuery<MarketStatus>({
    queryKey: ['marketStatus'],
    queryFn: api.getMarketStatus,
    refetchInterval: 60000,
  })

  const { data: stockAccount } = useQuery<StockAccount>({
    queryKey: ['stockAccount'],
    queryFn: api.getStockAccount,
    refetchInterval: 10000,
  })

  const { data: cryptoLedger } = useQuery<CryptoLedger>({
    queryKey: ['cryptoPaperLedger'],
    queryFn: api.getCryptoPaperLedger,
    refetchInterval: 5000,
  })

  const { data: stockPositions = [] } = useQuery({
    queryKey: ['stockPositions'],
    queryFn: api.getStockPositions,
    refetchInterval: 5000,
  })

  const { data: cryptoPositions = [] } = useQuery({
    queryKey: ['cryptoPositions'],
    queryFn: api.getCryptoPositions,
    refetchInterval: 5000,
  })

  const { data: stockWatchlist } = useQuery<WatchlistUploadRecord | null>({
    queryKey: ['activeWatchlist', 'stocks_only'],
    queryFn: () => api.getActiveWatchlist('stocks_only'),
    refetchInterval: 10000,
  })

  const { data: cryptoWatchlist } = useQuery<WatchlistUploadRecord | null>({
    queryKey: ['activeWatchlist', 'crypto_only'],
    queryFn: () => api.getActiveWatchlist('crypto_only'),
    refetchInterval: 10000,
  })

  const { data: stockMonitoring } = useQuery<WatchlistMonitoringSnapshot>({
    queryKey: ['watchlistMonitoring', 'stocks_only'],
    queryFn: () => api.getWatchlistMonitoring('stocks_only'),
    refetchInterval: 10000,
  })

  const { data: cryptoMonitoring } = useQuery<WatchlistMonitoringSnapshot>({
    queryKey: ['watchlistMonitoring', 'crypto_only'],
    queryFn: () => api.getWatchlistMonitoring('crypto_only'),
    refetchInterval: 10000,
  })

  const { data: stockOrchestration } = useQuery<WatchlistOrchestrationStatus>({
    queryKey: ['watchlistOrchestration', 'stocks_only'],
    queryFn: () => api.getWatchlistOrchestration('stocks_only'),
    refetchInterval: 10000,
  })

  const { data: cryptoOrchestration } = useQuery<WatchlistOrchestrationStatus>({
    queryKey: ['watchlistOrchestration', 'crypto_only'],
    queryFn: () => api.getWatchlistOrchestration('crypto_only'),
    refetchInterval: 10000,
  })

  const { data: stockExitReadiness } = useQuery<WatchlistExitReadinessSnapshot>({
    queryKey: ['watchlistExitReadiness', 'stocks_only'],
    queryFn: () => api.getWatchlistExitReadiness('stocks_only', 24),
    refetchInterval: 10000,
  })

  const { data: cryptoExitReadiness } = useQuery<WatchlistExitReadinessSnapshot>({
    queryKey: ['watchlistExitReadiness', 'crypto_only'],
    queryFn: () => api.getWatchlistExitReadiness('crypto_only', 24),
    refetchInterval: 10000,
  })

  const summary = useMemo(() => {
    const stockEquity = stockAccount?.portfolioValue ?? 0
    const cryptoEquity = cryptoLedger?.equity ?? 0
    const stockPnl = stockAccount?.unrealizedPnL ?? 0
    const cryptoPnl = cryptoLedger?.totalPnL ?? 0
    const activeWatchSymbols = (stockWatchlist?.selectedCount ?? 0) + (cryptoWatchlist?.selectedCount ?? 0)

    return {
      totalEquity: stockEquity + cryptoEquity,
      openPnl: stockPnl + cryptoPnl,
      activePositions: stockPositions.length + cryptoPositions.length,
      activeWatchSymbols,
    }
  }, [cryptoLedger, cryptoPositions.length, cryptoWatchlist?.selectedCount, stockAccount, stockPositions.length, stockWatchlist?.selectedCount])

  const recentGateDecisions = runtimeVisibility?.gate.recent ?? []
  const dependencySummary = runtimeVisibility?.dependencies.summary

  return (
    <div className="space-y-6">
      <header className="rounded-3xl border border-slate-800 bg-slate-900/70 p-6 shadow-2xl shadow-slate-950/30">
        <div className="flex flex-col gap-4 xl:flex-row xl:items-end xl:justify-between">
          <div>
            <div className="mb-2 flex items-center gap-2 text-sm font-medium uppercase tracking-[0.22em] text-cyan-300">
              <Radar className="h-4 w-4" />
              Operator console
            </div>
            <h1 className="text-3xl font-semibold text-white">Daily watchlist mission board</h1>
            <p className="mt-2 max-w-3xl text-sm leading-6 text-slate-400">
              The dashboard now behaves like a flight deck: runtime truth, active watchlists, execution pressure, and the fastest lanes into positions,
              monitoring, and audit.
            </p>
          </div>

          <div className="flex flex-wrap gap-3">
            <StatusPill tone={marketStatus?.stock.isOpen ? 'good' : 'warn'} label={`Stocks ${marketStatus?.stock.isOpen ? 'open' : 'closed'}`} />
            <StatusPill tone={botStatus?.running ? 'good' : 'warn'} label={botStatus?.running ? 'Runtime active' : 'Runtime paused'} />
            <StatusPill tone={stateTone(botStatus?.controlPlane.state)} label={`Control ${botStatus?.controlPlane.state ?? 'UNKNOWN'}`} />
            <StatusPill tone={stateTone(botStatus?.executionGate.state)} label={`Gate ${botStatus?.executionGate.state ?? 'UNKNOWN'}`} />
          </div>
        </div>
      </header>

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
        <MetricCard label="Total equity" value={formatMoney(summary.totalEquity)} detail={`${formatMoney(summary.openPnl)} open P&L`} icon={<Wallet className="h-5 w-5" />} />
        <MetricCard label="Active positions" value={String(summary.activePositions)} detail={`${stockPositions.length} stock · ${cryptoPositions.length} crypto`} icon={<TrendingUp className="h-5 w-5" />} />
        <MetricCard label="Active watch symbols" value={String(summary.activeWatchSymbols)} detail={`${stockWatchlist?.selectedCount ?? 0} stock · ${cryptoWatchlist?.selectedCount ?? 0} crypto`} icon={<ClipboardList className="h-5 w-5" />} />
        <MetricCard label="Dependency readiness" value={`${dependencySummary?.readyCount ?? 0}/${(dependencySummary?.readyCount ?? 0) + (dependencySummary?.degradedCount ?? 0) + (dependencySummary?.missingCount ?? 0)}`} detail={dependencySummary?.criticalReady ? 'Critical rails ready' : 'Critical rails degraded'} icon={<Shield className="h-5 w-5" />} />
      </div>

      <div className="grid grid-cols-1 gap-6 xl:grid-cols-[minmax(0,1.55fr)_minmax(320px,0.95fr)]">
        <section className="rounded-3xl border border-slate-800 bg-slate-900/70 p-5 shadow-xl shadow-slate-950/20">
          <div className="flex items-center gap-2 text-sm font-semibold uppercase tracking-[0.18em] text-slate-400">
            <Activity className="h-4 w-4 text-cyan-300" />
            Command deck
          </div>
          <div className="mt-4 grid grid-cols-1 gap-4 md:grid-cols-2">
            <CommandCard to="/watchlists" title="Watchlists" description="Review the accepted uploads, context notes, and provider limitations before the bot leans forward." />
            <CommandCard to="/monitoring" title="Monitoring" description="See who is due, who is blocked, and which symbols are standing on a trigger." />
            <CommandCard to="/positions" title="Positions" description="Track live inventory, exit pressure, and the trade tape without bouncing between separate pages." />
            <CommandCard to="/audit" title="Audit trail" description="Inspect watchlist receipts, gate decisions, order lifecycle events, and the paper-ledger breadcrumb trail." />
          </div>
        </section>

        <section className="rounded-3xl border border-slate-800 bg-slate-900/70 p-5 shadow-xl shadow-slate-950/20">
          <div className="flex items-center gap-2 text-sm font-semibold uppercase tracking-[0.18em] text-slate-400">
            <Shield className="h-4 w-4 text-emerald-300" />
            Control plane truth
          </div>
          <div className="mt-4 space-y-3 text-sm text-slate-300">
            <SummaryRow label="Control state" value={botStatus?.controlPlane.state ?? 'UNKNOWN'} tone={stateTone(botStatus?.controlPlane.state)} />
            <SummaryRow label="Control reason" value={botStatus?.controlPlane.reason ?? '—'} />
            <SummaryRow label="Gate state" value={botStatus?.executionGate.state ?? 'UNKNOWN'} tone={stateTone(botStatus?.executionGate.state)} />
            <SummaryRow label="Last heartbeat" value={formatRelative(botStatus?.lastHeartbeat)} />
            <SummaryRow label="Stock session" value={marketStatus?.stock.isOpen ? 'Open' : 'Closed'} tone={marketStatus?.stock.isOpen ? 'good' : 'warn'} />
            <SummaryRow label="Stock mode" value={botStatus?.stockMode ?? 'PAPER'} />
          </div>
        </section>
      </div>

      <div className="grid grid-cols-1 gap-6 2xl:grid-cols-2">
        <ScopeCard
          scope="stocks_only"
          watchlist={stockWatchlist}
          monitoring={stockMonitoring}
          orchestration={stockOrchestration}
          exitReadiness={stockExitReadiness}
        />
        <ScopeCard
          scope="crypto_only"
          watchlist={cryptoWatchlist}
          monitoring={cryptoMonitoring}
          orchestration={cryptoOrchestration}
          exitReadiness={cryptoExitReadiness}
        />
      </div>

      <div className="grid grid-cols-1 gap-6 xl:grid-cols-[minmax(0,1.45fr)_minmax(320px,0.95fr)]">
        <section className="rounded-3xl border border-slate-800 bg-slate-900/70 p-5 shadow-xl shadow-slate-950/20">
          <div className="flex items-center gap-2 text-sm font-semibold uppercase tracking-[0.18em] text-slate-400">
            <FileSearch className="h-4 w-4 text-cyan-300" />
            Recent gate decisions
          </div>
          <div className="mt-4 space-y-3">
            {recentGateDecisions.length === 0 ? (
              <EmptyState message="No gate decisions have been recorded yet." />
            ) : (
              recentGateDecisions.slice(0, 6).map((decision) => (
                <div key={`${decision.recordedAtUtc}-${decision.symbol}-${decision.state}`} className="rounded-2xl border border-slate-800 bg-slate-950/60 p-4">
                  <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                    <div>
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="text-base font-semibold text-white">{decision.symbol}</span>
                        <ToneBadge tone={decision.allowed ? 'good' : 'danger'}>{decision.allowed ? 'Allowed' : 'Rejected'}</ToneBadge>
                        <ToneBadge tone={stateTone(decision.state)}>{decision.state}</ToneBadge>
                      </div>
                      <div className="mt-2 text-sm text-slate-400">
                        {decision.executionSource} · {decision.assetClass}
                      </div>
                    </div>
                    <div className="text-sm text-slate-500">{formatRelative(decision.recordedAtUtc)}</div>
                  </div>
                  <div className="mt-3 text-sm text-slate-300">{decision.rejectionReason || 'Gate opened cleanly. No rejection reason recorded.'}</div>
                </div>
              ))
            )}
          </div>
        </section>

        <section className="rounded-3xl border border-slate-800 bg-slate-900/70 p-5 shadow-xl shadow-slate-950/20">
          <div className="flex items-center gap-2 text-sm font-semibold uppercase tracking-[0.18em] text-slate-400">
            <Shield className="h-4 w-4 text-emerald-300" />
            Dependency board
          </div>
          <div className="mt-4 space-y-3">
            {runtimeVisibility?.dependencies.checks ? (
              Object.values(runtimeVisibility.dependencies.checks).map((dependency) => (
                <div key={dependency.name} className="rounded-2xl border border-slate-800 bg-slate-950/60 p-4">
                  <div className="flex items-center justify-between gap-3">
                    <div>
                      <div className="font-medium text-white">{dependency.name}</div>
                      <div className="mt-1 text-sm text-slate-400">{dependency.reason || 'No detail provided.'}</div>
                    </div>
                    <ToneBadge tone={stateTone(dependency.state)}>{dependency.state}</ToneBadge>
                  </div>
                </div>
              ))
            ) : (
              <EmptyState message="Dependency visibility has not arrived yet." />
            )}
          </div>
        </section>
      </div>
    </div>
  )
}

function ScopeCard({
  scope,
  watchlist,
  monitoring,
  orchestration,
  exitReadiness,
}: {
  scope: WatchlistScope
  watchlist: WatchlistUploadRecord | null | undefined
  monitoring: WatchlistMonitoringSnapshot | undefined
  orchestration: WatchlistOrchestrationStatus | undefined
  exitReadiness: WatchlistExitReadinessSnapshot | undefined
}) {
  const dueSnapshot = orchestration?.dueSnapshot
  const dueCount = dueSnapshot && 'dueCount' in dueSnapshot ? dueSnapshot.dueCount : 0
  const eligibleDueCount = dueSnapshot && 'eligibleDueCount' in dueSnapshot ? dueSnapshot.eligibleDueCount : 0
  const blockedDueCount = dueSnapshot && 'blockedDueCount' in dueSnapshot ? dueSnapshot.blockedDueCount : 0
  const sessionLabel = dueSnapshot && 'session' in dueSnapshot ? dueSnapshot.session.sessionLabel : 'Unknown session'
  const sessionOpen = dueSnapshot && 'session' in dueSnapshot ? dueSnapshot.session.sessionOpen : false

  return (
    <section className="rounded-3xl border border-slate-800 bg-slate-900/70 p-5 shadow-xl shadow-slate-950/20">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div>
          <div className="text-sm font-semibold uppercase tracking-[0.18em] text-slate-500">{scopeLabels[scope]}</div>
          <h2 className="mt-1 text-2xl font-semibold text-white">{watchlist?.provider ?? 'No active watchlist'}</h2>
          <div className="mt-3 flex flex-wrap gap-2">
            <ToneBadge tone={isHealthyValidationStatus(watchlist?.validationStatus) ? 'good' : 'warn'}>{watchlist?.validationStatus ?? 'MISSING'}</ToneBadge>
            <ToneBadge tone={sessionOpen ? 'good' : 'warn'}>{sessionLabel}</ToneBadge>
            <ToneBadge tone="info">{watchlist?.marketRegime ?? 'No regime'}</ToneBadge>
          </div>
        </div>

        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <SmallMetric label="Symbols" value={String(watchlist?.selectedCount ?? 0)} />
          <SmallMetric label="Entry ready" value={String(monitoring?.summary.entryCandidateCount ?? 0)} />
          <SmallMetric label="Open" value={String(exitReadiness?.summary.openPositionCount ?? 0)} />
          <SmallMetric label="Protective" value={String(exitReadiness?.summary.protectiveExitPendingCount ?? 0)} />
        </div>
      </div>

      <div className="mt-5 grid grid-cols-1 gap-4 xl:grid-cols-2">
        <div className="rounded-2xl border border-slate-800 bg-slate-950/60 p-4">
          <div className="mb-4 text-sm font-semibold text-slate-200">Watchlist state</div>
          <div className="space-y-3 text-sm text-slate-400">
            <SummaryRow label="Generated" value={formatRelative(watchlist?.generatedAtUtc)} />
            <SummaryRow label="Received" value={formatRelative(watchlist?.receivedAtUtc)} />
            <SummaryRow label="Expires" value={formatRelative(watchlist?.watchlistExpiresAtUtc)} />
            <SummaryRow label="Waiting for setup" value={String(monitoring?.summary.waitingForSetupCount ?? 0)} />
            <SummaryRow label="Managed-only" value={String(monitoring?.summary.managedOnlyCount ?? 0)} />
          </div>
        </div>

        <div className="rounded-2xl border border-slate-800 bg-slate-950/60 p-4">
          <div className="mb-4 text-sm font-semibold text-slate-200">Evaluation pressure</div>
          <div className="space-y-3 text-sm text-slate-400">
            <SummaryRow label="Due" value={String(dueCount)} />
            <SummaryRow label="Eligible" value={String(eligibleDueCount)} />
            <SummaryRow label="Blocked" value={String(blockedDueCount)} tone={blockedDueCount > 0 ? 'warn' : 'muted'} />
            <SummaryRow label="Expired positions" value={String(exitReadiness?.summary.expiredPositionCount ?? 0)} tone={(exitReadiness?.summary.expiredPositionCount ?? 0) > 0 ? 'warn' : 'muted'} />
            <SummaryRow label="Scale-out ready" value={String(exitReadiness?.summary.scaleOutReadyCount ?? 0)} />
          </div>
        </div>
      </div>
    </section>
  )
}

function CommandCard({ to, title, description }: { to: string; title: string; description: string }) {
  return (
    <Link to={to} className="rounded-2xl border border-slate-800 bg-slate-950/60 p-4 transition hover:border-cyan-700 hover:bg-slate-950">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="text-base font-semibold text-white">{title}</div>
          <div className="mt-2 text-sm leading-6 text-slate-400">{description}</div>
        </div>
        <ArrowRight className="mt-1 h-4 w-4 text-cyan-300" />
      </div>
    </Link>
  )
}

function MetricCard({ label, value, detail, icon }: { label: string; value: string; detail: string; icon: JSX.Element }) {
  return (
    <div className="rounded-3xl border border-slate-800 bg-slate-900/70 p-5 shadow-xl shadow-slate-950/20">
      <div className="flex items-center justify-between gap-3">
        <div className="text-sm text-slate-400">{label}</div>
        <div className="text-cyan-300">{icon}</div>
      </div>
      <div className="mt-3 text-3xl font-semibold text-white">{value}</div>
      <div className="mt-2 text-sm text-slate-500">{detail}</div>
    </div>
  )
}

function SmallMetric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-2xl border border-slate-800 bg-slate-950/60 px-4 py-3">
      <div className="text-xs uppercase tracking-wide text-slate-500">{label}</div>
      <div className="mt-1 text-2xl font-semibold text-white">{value}</div>
    </div>
  )
}

function SummaryRow({ label, value, tone = 'muted' }: { label: string; value: string; tone?: 'good' | 'warn' | 'danger' | 'info' | 'muted' }) {
  return (
    <div className="flex items-start justify-between gap-4">
      <div className="text-slate-500">{label}</div>
      <div className={toneTextClass(tone)}>{value}</div>
    </div>
  )
}

function StatusPill({ label, tone }: { label: string; tone: 'good' | 'warn' | 'danger' | 'info' | 'muted' }) {
  return <span className={`rounded-full px-3 py-2 text-sm ${toneBadgeClass(tone)}`}>{label}</span>
}

function ToneBadge({ children, tone }: { children: string; tone: 'good' | 'warn' | 'danger' | 'info' | 'muted' }) {
  return <span className={`rounded-full px-2.5 py-1 text-xs font-semibold uppercase tracking-wide ${toneBadgeClass(tone)}`}>{children}</span>
}

function EmptyState({ message }: { message: string }) {
  return <div className="rounded-2xl border border-dashed border-slate-700 px-4 py-8 text-center text-sm text-slate-500">{message}</div>
}

function toneBadgeClass(tone: 'good' | 'warn' | 'danger' | 'info' | 'muted') {
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

function toneTextClass(tone: 'good' | 'warn' | 'danger' | 'info' | 'muted') {
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
