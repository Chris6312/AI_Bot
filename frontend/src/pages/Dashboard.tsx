import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { formatDistanceToNow } from 'date-fns'
import { Activity, AlertTriangle, Bitcoin, DollarSign, Shield, TrendingUp, Wallet } from 'lucide-react'

import { api } from '@/lib/api'
import type {
  BotStatus,
  CryptoLedger,
  CryptoPosition,
  DependencyCheck,
  GateDecisionRecord,
  MarketStatus,
  RuntimeVisibility,
  StockAccount,
  StockPosition,
} from '@/types'

function formatMoney(value: number) {
  return `$${value.toFixed(2)}`
}

function formatObserved(value?: string | null) {
  if (!value) return '—'
  return `${formatDistanceToNow(new Date(value), { addSuffix: true })}`
}

function stateTone(state: string) {
  switch (state) {
    case 'ARMED':
    case 'READY':
      return 'bg-green-900/60 text-green-300 border-green-700'
    case 'PAUSED':
    case 'DEGRADED':
      return 'bg-yellow-900/50 text-yellow-300 border-yellow-700'
    case 'LOCKED':
    case 'READ_ONLY':
    case 'REJECTED':
    case 'MISSING':
      return 'bg-red-900/50 text-red-300 border-red-700'
    default:
      return 'bg-gray-800 text-gray-300 border-gray-700'
  }
}

export default function Dashboard() {
  const { data: stockPositions = [] } = useQuery<StockPosition[]>({
    queryKey: ['stockPositions'],
    queryFn: api.getStockPositions,
    refetchInterval: 5000,
  })

  const { data: cryptoPositions = [] } = useQuery<CryptoPosition[]>({
    queryKey: ['cryptoPositions'],
    queryFn: api.getCryptoPositions,
    refetchInterval: 5000,
  })

  const { data: botStatus } = useQuery<BotStatus>({
    queryKey: ['botStatus'],
    queryFn: api.getBotStatus,
    refetchInterval: 3000,
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

  const { data: runtimeVisibility } = useQuery<RuntimeVisibility>({
    queryKey: ['runtimeVisibility'],
    queryFn: () => api.getRuntimeVisibility(8),
    refetchInterval: 10000,
  })

  const summary = useMemo(() => {
    const stockEquity = stockAccount?.portfolioValue ?? 0
    const cryptoEquity = cryptoLedger?.equity ?? 0
    const stockPnL = stockAccount?.unrealizedPnL ?? stockPositions.reduce((sum, p) => sum + p.pnl, 0)
    const cryptoPnL = cryptoLedger?.totalPnL ?? cryptoPositions.reduce((sum, p) => sum + p.pnl, 0)
    return {
      stockEquity,
      cryptoEquity,
      totalEquity: stockEquity + cryptoEquity,
      openPnL: stockPnL + cryptoPnL,
      activePositions: stockPositions.length + cryptoPositions.length,
    }
  }, [cryptoLedger, cryptoPositions, stockAccount, stockPositions])

  const gateSummary = runtimeVisibility?.gate.summary
  const lastDecision = gateSummary?.lastDecision
  const lastRejected = gateSummary?.lastRejected
  const dependencies = runtimeVisibility?.dependencies.checks

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
        <div>
          <h1 className="text-3xl font-bold text-white">Operator Dashboard</h1>
          <p className="mt-1 text-gray-400">Runtime truth board for control state, market readiness, and recent gate decisions.</p>
        </div>
        <div className="flex flex-wrap gap-3">
          <StatusPill
            active={marketStatus?.stock.isOpen ?? false}
            icon={<TrendingUp className="h-4 w-4" />}
            label={`Stock Market: ${marketStatus?.stock.isOpen ? 'Open' : 'Closed'}`}
          />
          <StatusPill
            active={botStatus?.running ?? false}
            icon={<Activity className="h-4 w-4" />}
            label={`Bot: ${botStatus?.running ? 'Active' : 'Inactive'}`}
          />
          <StatusPill
            active={(botStatus?.stockMode ?? 'PAPER') === 'LIVE'}
            icon={<Wallet className="h-4 w-4" />}
            label={`Stock Mode: ${botStatus?.stockMode ?? 'PAPER'}`}
          />
        </div>
      </div>

      <ControlPlaneBanner
        state={botStatus?.controlPlane.state ?? 'UNKNOWN'}
        reason={botStatus?.controlPlane.reason ?? 'Waiting for control-plane status.'}
        executionGateState={botStatus?.executionGate.state ?? 'UNKNOWN'}
        lastDecision={lastDecision ?? null}
      />

      <div className="grid grid-cols-1 gap-4 md:grid-cols-5">
        <StatCard title="Total Equity" value={formatMoney(summary.totalEquity)} icon={<DollarSign className="h-6 w-6" />} trend={summary.totalEquity >= 0 ? 'up' : 'down'} />
        <StatCard title="Stock Equity" value={formatMoney(summary.stockEquity)} icon={<TrendingUp className="h-6 w-6" />} trend={summary.stockEquity >= 0 ? 'up' : 'down'} />
        <StatCard title="Crypto Equity" value={formatMoney(summary.cryptoEquity)} icon={<Bitcoin className="h-6 w-6" />} trend={summary.cryptoEquity >= 0 ? 'up' : 'down'} />
        <StatCard title="Open P&L" value={formatMoney(summary.openPnL)} icon={<Activity className="h-6 w-6" />} trend={summary.openPnL >= 0 ? 'up' : 'down'} />
        <StatCard title="Gate Rejections" value={String(gateSummary?.rejectedCount ?? 0)} icon={<Shield className="h-6 w-6" />} trend={(gateSummary?.rejectedCount ?? 0) > 0 ? 'down' : 'up'} />
      </div>

      <div className="grid grid-cols-1 gap-6 xl:grid-cols-3">
        <div className="xl:col-span-2 space-y-6">
          <div className="grid grid-cols-1 gap-6 md:grid-cols-2">
            <MarketPanel title={`Stock Positions (${botStatus?.stockMode ?? 'PAPER'})`} positions={stockPositions} type="stock" emptyMessage="No active stock positions" />
            <MarketPanel title="Crypto Positions (PAPER)" positions={cryptoPositions} type="crypto" emptyMessage="No active crypto positions" />
          </div>
          <GateDecisionPanel recent={runtimeVisibility?.gate.recent ?? []} />
        </div>

        <div className="space-y-6">
          <DependencyPanel dependencies={dependencies} />
          <LastRejectionCard decision={lastRejected ?? null} />
        </div>
      </div>
    </div>
  )
}

function ControlPlaneBanner({
  state,
  reason,
  executionGateState,
  lastDecision,
}: {
  state: string
  reason: string
  executionGateState: string
  lastDecision: GateDecisionRecord | null
}) {
  return (
    <div className={`rounded-2xl border p-5 ${stateTone(state)}`}>
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div>
          <div className="flex items-center gap-2 text-sm font-semibold uppercase tracking-wide">
            <Shield className="h-4 w-4" />
            Control Plane {state}
          </div>
          <p className="mt-2 text-sm leading-6">{reason}</p>
        </div>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <MiniMetric label="Execution Gate" value={executionGateState} />
          <MiniMetric label="Last Gate Decision" value={lastDecision ? `${lastDecision.symbol} · ${lastDecision.allowed ? 'Allowed' : 'Rejected'}` : 'No decisions yet'} />
        </div>
      </div>
    </div>
  )
}

function StatusPill({ active, icon, label }: { active: boolean; icon: React.ReactNode; label: string }) {
  return (
    <div className={`rounded-full px-4 py-2 ${active ? 'bg-green-900 text-green-300' : 'bg-gray-800 text-gray-400'}`}>
      <div className="flex items-center gap-2">
        {icon}
        <span>{label}</span>
      </div>
    </div>
  )
}

function StatCard({ title, value, icon, trend }: { title: string; value: string; icon: React.ReactNode; trend?: 'up' | 'down' }) {
  const iconClass = trend === 'down' ? 'text-red-500' : 'text-green-500'
  const valueClass = trend === 'down' ? 'text-red-500' : trend === 'up' ? 'text-green-500' : 'text-white'

  return (
    <div className="rounded-lg border border-gray-800 bg-gray-900 p-6">
      <div className="mb-2 flex items-center justify-between">
        <span className="text-sm text-gray-400">{title}</span>
        <div className={iconClass}>{icon}</div>
      </div>
      <div className={`text-2xl font-bold ${valueClass}`}>{value}</div>
    </div>
  )
}

function MarketPanel({
  title,
  positions,
  type,
  emptyMessage,
}: {
  title: string
  positions: Array<StockPosition | CryptoPosition>
  type: 'stock' | 'crypto'
  emptyMessage: string
}) {
  return (
    <div className="rounded-lg border border-gray-800 bg-gray-900 p-6">
      <h2 className="mb-4 text-xl font-bold text-white">{title}</h2>
      {positions.length === 0 ? (
        <p className="py-8 text-center text-gray-500">{emptyMessage}</p>
      ) : (
        <div className="space-y-3">
          {positions.map((position) => {
            const key = type === 'stock' ? (position as StockPosition).symbol : (position as CryptoPosition).pair
            const qtyLabel =
              type === 'stock'
                ? `${(position as StockPosition).shares.toFixed(0)} shares`
                : `${(position as CryptoPosition).amount.toFixed(6)}`
            return (
              <div key={key} className="flex items-center justify-between rounded-lg bg-gray-800 p-3">
                <div>
                  <div className="font-semibold text-white">{key}</div>
                  <div className="text-sm text-gray-400">
                    {qtyLabel} @ ${position.avgPrice.toFixed(2)}
                  </div>
                </div>
                <div className="text-right">
                  <div className={`font-semibold ${position.pnl >= 0 ? 'text-green-500' : 'text-red-500'}`}>
                    {formatMoney(position.pnl)}
                  </div>
                  <div className={`text-sm ${position.pnlPercent >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                    {position.pnlPercent >= 0 ? '+' : ''}
                    {position.pnlPercent.toFixed(2)}%
                  </div>
                </div>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

function DependencyPanel({ dependencies }: { dependencies?: RuntimeVisibility['dependencies']['checks'] }) {
  const entries = dependencies ? Object.values(dependencies) : []
  return (
    <div className="rounded-lg border border-gray-800 bg-gray-900 p-6">
      <h2 className="mb-4 text-xl font-bold text-white">Dependency Readiness</h2>
      <div className="space-y-3">
        {entries.length === 0 ? (
          <p className="text-sm text-gray-500">Waiting for dependency probes…</p>
        ) : (
          entries.map((entry) => <DependencyRow key={entry.name} entry={entry} />)
        )}
      </div>
    </div>
  )
}

function DependencyRow({ entry }: { entry: DependencyCheck }) {
  return (
    <div className="rounded-lg bg-gray-800 p-4">
      <div className="flex items-center justify-between gap-4">
        <div>
          <div className="font-semibold text-white">{entry.name}</div>
          <div className="mt-1 text-xs text-gray-400">Checked {formatObserved(entry.checkedAtUtc)}</div>
        </div>
        <span className={`rounded border px-2 py-1 text-xs font-semibold ${stateTone(entry.state)}`}>{entry.state}</span>
      </div>
      <div className="mt-2 text-sm text-gray-300">{entry.reason || 'Probe succeeded.'}</div>
    </div>
  )
}

function GateDecisionPanel({ recent }: { recent: GateDecisionRecord[] }) {
  return (
    <div className="rounded-lg border border-gray-800 bg-gray-900 p-6">
      <h2 className="mb-4 text-xl font-bold text-white">Recent Pre-Trade Gate Decisions</h2>
      {recent.length === 0 ? (
        <p className="text-sm text-gray-500">No gate decisions recorded yet.</p>
      ) : (
        <div className="space-y-3">
          {recent.map((decision) => (
            <div key={`${decision.recordedAtUtc}-${decision.symbol}-${decision.executionSource}`} className="rounded-lg bg-gray-800 p-4">
              <div className="flex flex-col gap-2 md:flex-row md:items-center md:justify-between">
                <div>
                  <div className="flex items-center gap-2">
                    <span className="font-semibold text-white">{decision.symbol}</span>
                    <span className={`rounded border px-2 py-1 text-xs font-semibold ${decision.allowed ? 'border-green-700 bg-green-900/50 text-green-300' : 'border-red-700 bg-red-900/50 text-red-300'}`}>
                      {decision.allowed ? 'ALLOWED' : 'REJECTED'}
                    </span>
                    <span className="text-xs uppercase tracking-wide text-gray-400">{decision.assetClass}</span>
                  </div>
                  <div className="mt-1 text-sm text-gray-400">
                    {decision.executionSource} · {formatObserved(decision.recordedAtUtc)}
                  </div>
                </div>
                <div className="text-sm text-gray-300">State: {decision.state}</div>
              </div>
              <div className="mt-3 text-sm text-gray-300">
                {decision.rejectionReason || 'All gate checks passed.'}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function LastRejectionCard({ decision }: { decision: GateDecisionRecord | null }) {
  return (
    <div className="rounded-lg border border-gray-800 bg-gray-900 p-6">
      <h2 className="mb-4 flex items-center gap-2 text-xl font-bold text-white">
        <AlertTriangle className="h-5 w-5 text-yellow-400" />
        Last Rejection
      </h2>
      {!decision ? (
        <p className="text-sm text-gray-500">No rejected gate decisions recorded yet.</p>
      ) : (
        <div className="space-y-3 text-sm">
          <div>
            <div className="text-xs uppercase tracking-wide text-gray-500">Symbol</div>
            <div className="font-semibold text-white">{decision.symbol}</div>
          </div>
          <div>
            <div className="text-xs uppercase tracking-wide text-gray-500">Reason</div>
            <div className="text-gray-300">{decision.rejectionReason}</div>
          </div>
          <div className="rounded-lg bg-gray-800 p-3 text-gray-400">
            {decision.executionSource} · {formatObserved(decision.recordedAtUtc)}
          </div>
        </div>
      )}
    </div>
  )
}

function MiniMetric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-xl bg-black/20 px-4 py-3">
      <div className="text-xs uppercase tracking-wide opacity-70">{label}</div>
      <div className="mt-1 text-sm font-semibold">{value}</div>
    </div>
  )
}
