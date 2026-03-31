import { useMemo } from 'react'
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { formatDistanceToNow } from 'date-fns'
import {
  Activity,
  AlertTriangle,
  ArrowRight,
  ClipboardList,
  FileSearch,
  Radar,
  Shield,
  TimerReset,
  TrendingUp,
  Wallet,
} from 'lucide-react'

import { api } from '@/lib/api'
import {
  DetailRow,
  EmptyState,
  MetricCard,
  MiniMetric,
  PageHero,
  SectionCard,
  StatusPill,
  ToneBadge,
  getScopeSessionMeta,
  getStatusMeta,
  type Tone,
} from '@/components/operator-ui'
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


function getAvailableToTrade(account?: StockAccount) {
  if (!account) return 0
  return account.availableToTrade ?? account.cash ?? account.buyingPower ?? 0
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

  const { data: stockMonitoring } = useQuery<WatchlistMonitoringSnapshot | null>({
    queryKey: ['watchlistMonitoring', 'stocks_only'],
    queryFn: () => api.getWatchlistMonitoringOptional('stocks_only'),
    refetchInterval: 10000,
  })

  const { data: cryptoMonitoring } = useQuery<WatchlistMonitoringSnapshot | null>({
    queryKey: ['watchlistMonitoring', 'crypto_only'],
    queryFn: () => api.getWatchlistMonitoringOptional('crypto_only'),
    refetchInterval: 10000,
  })

  const { data: stockOrchestration } = useQuery<WatchlistOrchestrationStatus | null>({
    queryKey: ['watchlistOrchestration', 'stocks_only'],
    queryFn: () => api.getWatchlistOrchestrationOptional('stocks_only'),
    refetchInterval: 10000,
  })

  const { data: cryptoOrchestration } = useQuery<WatchlistOrchestrationStatus | null>({
    queryKey: ['watchlistOrchestration', 'crypto_only'],
    queryFn: () => api.getWatchlistOrchestrationOptional('crypto_only'),
    refetchInterval: 10000,
  })

  const { data: stockExitReadiness } = useQuery<WatchlistExitReadinessSnapshot | null>({
    queryKey: ['watchlistExitReadiness', 'stocks_only'],
    queryFn: () => api.getWatchlistExitReadinessOptional('stocks_only', 24),
    refetchInterval: 10000,
  })

  const { data: cryptoExitReadiness } = useQuery<WatchlistExitReadinessSnapshot | null>({
    queryKey: ['watchlistExitReadiness', 'crypto_only'],
    queryFn: () => api.getWatchlistExitReadinessOptional('crypto_only', 24),
    refetchInterval: 10000,
  })

  const summary = useMemo(() => {
    const stockEquity = stockAccount?.portfolioValue ?? 0
    const cryptoEquity = cryptoLedger?.equity ?? 0
    const stockPnl = stockAccount?.unrealizedPnL ?? 0
    const cryptoPnl = cryptoLedger?.netPnL ?? cryptoLedger?.totalPnL ?? 0

    return {
      totalEquity: stockEquity + cryptoEquity,
      openPnl: stockPnl + cryptoPnl,
      activePositions: stockPositions.length + cryptoPositions.length,
      watchSymbols: (stockWatchlist?.selectedCount ?? 0) + (cryptoWatchlist?.selectedCount ?? 0),
      availableCapital: getAvailableToTrade(stockAccount) + (cryptoLedger?.balance ?? 0),
    }
  }, [cryptoLedger, cryptoPositions.length, stockAccount, stockPositions.length, stockWatchlist?.selectedCount, cryptoWatchlist?.selectedCount])

  const dependencySummary = runtimeVisibility?.dependencies.summary
  const recentGateDecisions = runtimeVisibility?.gate.recent ?? []

  const attentionItems = useMemo(() => {
    const items: Array<{ label: string; detail: string; tone: Tone; to: string }> = []

    const stockProtective = stockExitReadiness?.summary.protectiveExitPendingCount ?? 0
    const cryptoProtective = cryptoExitReadiness?.summary.protectiveExitPendingCount ?? 0
    if (stockProtective + cryptoProtective > 0) {
      items.push({
        label: 'Protective exits pending',
        detail: `${stockProtective} stock · ${cryptoProtective} crypto`,
        tone: 'warn',
        to: '/positions',
      })
    }

    const unavailable = (stockMonitoring?.summary.dataUnavailableCount ?? 0) + (cryptoMonitoring?.summary.dataUnavailableCount ?? 0)
    if (unavailable > 0) {
      items.push({
        label: 'Symbols missing market data',
        detail: `${unavailable} rows still marked DATA_UNAVAILABLE`,
        tone: 'danger',
        to: '/monitoring',
      })
    }

    const blockedDue = [stockOrchestration, cryptoOrchestration].reduce((sum, item) => {
      const snapshot = item?.dueSnapshot
      if (snapshot && 'summary' in snapshot) {
        return sum + (snapshot.summary.blockedDueCount ?? 0)
      }
      if (snapshot && 'blockedDueCount' in snapshot) {
        return sum + (snapshot.blockedDueCount ?? 0)
      }
      return sum
    }, 0)
    if (blockedDue > 0) {
      items.push({
        label: 'Due evaluations are blocked',
        detail: `${blockedDue} evaluations are waiting on session, data, or control state`,
        tone: 'warn',
        to: '/monitoring',
      })
    }

    const gateRejections = runtimeVisibility?.gate.summary.rejectedCount ?? 0
    if (gateRejections > 0) {
      items.push({
        label: 'Recent gate rejections',
        detail: `${gateRejections} recent decisions were denied`,
        tone: 'warn',
        to: '/audit',
      })
    }

    if (items.length === 0) {
      items.push({
        label: 'Immediate pressure looks calm',
        detail: 'No protective exits, blocked due runs, or fresh gate bruises are visible right now.',
        tone: 'good',
        to: '/monitoring',
      })
    }

    return items.slice(0, 4)
  }, [cryptoExitReadiness?.summary.protectiveExitPendingCount, cryptoMonitoring?.summary.dataUnavailableCount, cryptoOrchestration, runtimeVisibility?.gate.summary.rejectedCount, stockExitReadiness?.summary.protectiveExitPendingCount, stockMonitoring?.summary.dataUnavailableCount, stockOrchestration])

  return (
    <div className="space-y-6">
      <PageHero
        eyebrow={
          <>
            <Radar className="h-4 w-4" />
            Operator console
          </>
        }
        title="Daily watchlist mission board"
        description="This dashboard is the flight deck. It surfaces watchlist pressure, runtime truth, inventory, and the shortest routes into monitoring, positions, audit, and control."
        aside={
          <>
            <StatusPill tone={marketStatus?.stock.isOpen ? 'good' : 'warn'} label={`Stocks ${marketStatus?.stock.isOpen ? 'open' : 'closed'}`} />
            <StatusPill tone="info" label="Crypto 24/7" />
            <StatusPill tone={botStatus?.running ? 'good' : 'warn'} label={botStatus?.running ? 'Runtime active' : 'Runtime paused'} />
            <StatusPill tone={getStatusMeta(botStatus?.executionGate.state).tone} label={`Gate ${getStatusMeta(botStatus?.executionGate.state).canonicalLabel}`} />
          </>
        }
      />

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-5">
        <MetricCard label="Combined equity" value={formatMoney(summary.totalEquity)} detail={`${formatMoney(summary.openPnl)} open P&L across stock + crypto`} icon={<Wallet className="h-5 w-5" />} />
        <MetricCard label="Deployable capital" value={formatMoney(summary.availableCapital)} detail="Stock available-to-trade plus crypto ledger cash" icon={<Shield className="h-5 w-5" />} />
        <MetricCard label="Active positions" value={String(summary.activePositions)} detail={`${stockPositions.length} stock · ${cryptoPositions.length} crypto`} icon={<TrendingUp className="h-5 w-5" />} />
        <MetricCard label="Watch symbols" value={String(summary.watchSymbols)} detail={`${stockWatchlist?.selectedCount ?? 0} stock · ${cryptoWatchlist?.selectedCount ?? 0} crypto`} icon={<ClipboardList className="h-5 w-5" />} />
        <MetricCard label="Dependency readiness" value={`${dependencySummary?.readyCount ?? 0}/${(dependencySummary?.readyCount ?? 0) + (dependencySummary?.degradedCount ?? 0) + (dependencySummary?.missingCount ?? 0)}`} detail={dependencySummary?.criticalReady ? 'Critical rails ready' : 'Critical rails degraded'} icon={<Activity className="h-5 w-5" />} />
      </div>

      <div className="grid grid-cols-1 gap-6 xl:grid-cols-[minmax(0,1.45fr)_minmax(320px,0.95fr)]">
        <SectionCard
          title="Immediate attention"
          eyebrow="Mission pressure"
          icon={<AlertTriangle className="h-4 w-4 text-amber-300" />}
        >
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            {attentionItems.map((item) => (
              <Link key={item.label} to={item.to} className="rounded-2xl border border-slate-800 bg-slate-950/60 p-4 transition hover:border-cyan-700 hover:bg-slate-950">
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <div className="flex items-center gap-2">
                      <span className="text-base font-semibold text-white">{item.label}</span>
                      <ToneBadge tone={item.tone}>{item.tone}</ToneBadge>
                    </div>
                    <div className="mt-2 text-sm leading-6 text-slate-400">{item.detail}</div>
                  </div>
                  <ArrowRight className="mt-1 h-4 w-4 text-cyan-300" />
                </div>
              </Link>
            ))}
          </div>
        </SectionCard>

        <SectionCard title="Command deck" eyebrow="Fast lanes" icon={<Radar className="h-4 w-4 text-cyan-300" />}>
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            <CommandCard to="/watchlists" title="Watchlists" description="Review accepted uploads, context notes, and provider limitations." />
            <CommandCard to="/monitoring" title="Monitoring" description="See who is due, who is blocked, and which rows are starved for data." />
            <CommandCard to="/positions" title="Positions" description="Track inventory, exit deadlines, and the most urgent symbols." />
            <CommandCard to="/audit" title="Audit trail" description="Inspect receipts, gate decisions, and execution breadcrumbs." />
          </div>
        </SectionCard>
      </div>

      <div className="grid grid-cols-1 gap-6 xl:grid-cols-2">
        <ScopePulseCard
          scope="stocks_only"
          watchlist={stockWatchlist}
          monitoring={stockMonitoring}
          orchestration={stockOrchestration}
          exitReadiness={stockExitReadiness}
        />
        <ScopePulseCard
          scope="crypto_only"
          watchlist={cryptoWatchlist}
          monitoring={cryptoMonitoring}
          orchestration={cryptoOrchestration}
          exitReadiness={cryptoExitReadiness}
        />
      </div>

      <div className="grid grid-cols-1 gap-6 xl:grid-cols-[minmax(0,1.2fr)_minmax(340px,0.9fr)]">
        <SectionCard title="Recent gate decisions" eyebrow="Audit pulse" icon={<FileSearch className="h-4 w-4 text-cyan-300" />}>
          <div className="space-y-3">
            {recentGateDecisions.length === 0 ? (
              <EmptyState message="No gate decisions have been recorded yet." />
            ) : (
              recentGateDecisions.slice(0, 6).map((decision) => (
                <div key={`${decision.recordedAtUtc}-${decision.symbol}-${decision.state}`} className="rounded-2xl border border-slate-800 bg-slate-950/60 p-4">
                  <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                    <div>
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="text-base font-semibold text-white">{decision.symbol}</span>
                        <ToneBadge tone={decision.allowed ? 'good' : 'danger'}>{decision.allowed ? 'Healthy' : 'Blocked'}</ToneBadge>
                        <ToneBadge tone={getStatusMeta(decision.state).tone}>{getStatusMeta(decision.state).rawLabel}</ToneBadge>
                      </div>
                      <div className="mt-2 text-sm text-slate-400">{decision.executionSource} · {decision.assetClass}</div>
                    </div>
                    <div className="text-sm text-slate-500">{formatRelative(decision.recordedAtUtc)}</div>
                  </div>
                  <div className="mt-3 text-sm text-slate-300">{decision.rejectionReason || 'Gate opened cleanly. No rejection reason recorded.'}</div>
                </div>
              ))
            )}
          </div>
        </SectionCard>

        <SectionCard title="Dependency board" eyebrow="Runtime truth" icon={<Shield className="h-4 w-4 text-emerald-300" />}>
          <div className="space-y-3">
            {runtimeVisibility?.dependencies.checks ? (
              Object.values(runtimeVisibility.dependencies.checks).map((dependency) => (
                <div key={dependency.name} className="rounded-2xl border border-slate-800 bg-slate-950/60 p-4">
                  <div className="flex items-center justify-between gap-3">
                    <div>
                      <div className="font-medium text-white">{dependency.name}</div>
                      <div className="mt-1 text-sm text-slate-400">{dependency.reason || 'No detail provided.'}</div>
                    </div>
                    <div className="flex flex-col items-end gap-2">
                      <ToneBadge tone={getStatusMeta(dependency.state).tone}>{getStatusMeta(dependency.state).canonicalLabel}</ToneBadge>
                      <span className="text-xs text-slate-500">Raw: {getStatusMeta(dependency.state).rawLabel}</span>
                    </div>
                  </div>
                </div>
              ))
            ) : (
              <EmptyState message="Dependency visibility has not arrived yet." />
            )}
          </div>
        </SectionCard>
      </div>
    </div>
  )
}

function ScopePulseCard({
  scope,
  watchlist,
  monitoring,
  orchestration,
  exitReadiness,
}: {
  scope: WatchlistScope
  watchlist: WatchlistUploadRecord | null | undefined
  monitoring: WatchlistMonitoringSnapshot | null | undefined
  orchestration: WatchlistOrchestrationStatus | null | undefined
  exitReadiness: WatchlistExitReadinessSnapshot | null | undefined
}) {
  const dueSnapshot = orchestration?.dueSnapshot
  const dueCount = dueSnapshot && 'summary' in dueSnapshot ? dueSnapshot.summary.totalDueCount : dueSnapshot && 'dueCount' in dueSnapshot ? dueSnapshot.dueCount : 0
  const eligibleDueCount = dueSnapshot && 'summary' in dueSnapshot ? dueSnapshot.summary.eligibleDueCount : dueSnapshot && 'eligibleDueCount' in dueSnapshot ? dueSnapshot.eligibleDueCount : 0
  const blockedDueCount = dueSnapshot && 'summary' in dueSnapshot ? dueSnapshot.summary.blockedDueCount : dueSnapshot && 'blockedDueCount' in dueSnapshot ? dueSnapshot.blockedDueCount : 0
  const nextRun = dueSnapshot && 'scopes' in dueSnapshot ? dueSnapshot.scopes?.[scope]?.nextEvaluationAtUtc : dueSnapshot && 'nextEvaluationAtUtc' in dueSnapshot ? dueSnapshot.nextEvaluationAtUtc : null
  const session = dueSnapshot && 'scopes' in dueSnapshot ? dueSnapshot.scopes?.[scope]?.session : dueSnapshot && 'session' in dueSnapshot ? dueSnapshot.session : undefined
  const sessionMeta = getScopeSessionMeta(scope, session)

  return (
    <SectionCard title={`${scopeLabels[scope]} pulse`} eyebrow="Scope snapshot" icon={<TimerReset className="h-4 w-4 text-cyan-300" />}>
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div>
          <div className="text-2xl font-semibold text-white">{watchlist?.provider ?? 'No active watchlist'}</div>
          <div className="mt-3 flex flex-wrap gap-2">
            <ToneBadge tone={getStatusMeta(watchlist?.validationStatus).tone}>{getStatusMeta(watchlist?.validationStatus).canonicalLabel}</ToneBadge>
            <ToneBadge tone={sessionMeta.tone}>{sessionMeta.label}</ToneBadge>
            <ToneBadge tone="info">{watchlist?.marketRegime ?? 'regime unavailable'}</ToneBadge>
          </div>
        </div>

        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <MiniMetric label="Symbols" value={String(watchlist?.selectedCount ?? 0)} />
          <MiniMetric label="Ready" value={String(monitoring?.summary.entryCandidateCount ?? 0)} />
          <MiniMetric label="Open" value={String(exitReadiness?.summary.openPositionCount ?? 0)} />
          <MiniMetric label="Protective" value={String(exitReadiness?.summary.protectiveExitPendingCount ?? 0)} />
        </div>
      </div>

      <div className="mt-5 grid grid-cols-1 gap-4 xl:grid-cols-2">
        <div className="rounded-2xl border border-slate-800 bg-slate-950/60 p-4">
          <div className="mb-4 text-sm font-semibold text-slate-200">Watchlist state</div>
          <div className="space-y-3 text-sm text-slate-400">
            <DetailRow label="Generated" value={formatRelative(watchlist?.generatedAtUtc)} />
            <DetailRow label="Received" value={formatRelative(watchlist?.receivedAtUtc)} />
            <DetailRow label="Expires" value={formatRelative(watchlist?.watchlistExpiresAtUtc)} />
            <DetailRow label="Waiting for setup" value={String(monitoring?.summary.waitingForSetupCount ?? 0)} />
            <DetailRow label="Data unavailable" value={String(monitoring?.summary.dataUnavailableCount ?? 0)} tone={(monitoring?.summary.dataUnavailableCount ?? 0) > 0 ? 'danger' : 'muted'} />
          </div>
        </div>

        <div className="rounded-2xl border border-slate-800 bg-slate-950/60 p-4">
          <div className="mb-4 text-sm font-semibold text-slate-200">Evaluation pressure</div>
          <div className="space-y-3 text-sm text-slate-400">
            <DetailRow label="Due" value={String(dueCount)} />
            <DetailRow label="Eligible due" value={String(eligibleDueCount)} />
            <DetailRow label="Blocked due" value={String(blockedDueCount)} tone={blockedDueCount > 0 ? 'warn' : 'muted'} />
            <DetailRow label="Expiring within 24h" value={String(exitReadiness?.summary.expiringWithinWindowCount ?? 0)} tone={(exitReadiness?.summary.expiringWithinWindowCount ?? 0) > 0 ? 'warn' : 'muted'} />
            <DetailRow label="Next run" value={formatRelative(nextRun)} />
          </div>
        </div>
      </div>
    </SectionCard>
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
