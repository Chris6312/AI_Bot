import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { format } from 'date-fns'
import { Activity, Bitcoin, Clock3, ShieldCheck, TrendingUp, Wallet } from 'lucide-react'

import { api } from '@/lib/api'
import type {
  BotStatus,
  CryptoLedger,
  CryptoPosition,
  OrderIntentRecord,
  StockAccount,
  StockPosition,
  TradeHistoryEntry,
  WatchlistExitReadinessSnapshot,
} from '@/types'

function formatMoney(value: number) {
  return `$${value.toFixed(2)}`
}

function formatPercent(value: number) {
  const prefix = value >= 0 ? '+' : ''
  return `${prefix}${value.toFixed(2)}%`
}

function getAvailableToTrade(account?: StockAccount) {
  if (!account) return 0
  return account.availableToTrade ?? account.cash ?? account.buyingPower ?? 0
}

function getBrokerBuyingPower(account?: StockAccount) {
  if (!account) return 0
  return account.brokerBuyingPower ?? account.buyingPower ?? 0
}

function formatStockCapacityDetail(account?: StockAccount) {
  if (!account) {
    return '$0.00 available'
  }

  const available = getAvailableToTrade(account)
  const broker = getBrokerBuyingPower(account)

  if (Math.abs(broker - available) >= 0.01) {
    return `${formatMoney(available)} available · ${formatMoney(broker)} broker BP`
  }

  return `${formatMoney(available)} available`
}

function formatTimestamp(value?: string | null) {
  if (!value) return '—'
  return format(new Date(value), 'MMM dd, yyyy HH:mm')
}

export default function Positions() {
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

  const { data: stockHistory = [] } = useQuery<OrderIntentRecord[]>({
    queryKey: ['stockHistory'],
    queryFn: () => api.getStockHistory(50),
    refetchInterval: 10000,
  })

  const { data: cryptoHistory = [] } = useQuery<TradeHistoryEntry[]>({
    queryKey: ['cryptoHistory'],
    queryFn: () => api.getCryptoHistory(50),
    refetchInterval: 10000,
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

  const { data: botStatus } = useQuery<BotStatus>({
    queryKey: ['botStatus'],
    queryFn: api.getBotStatus,
    refetchInterval: 5000,
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
    const stockOpenPnl = stockAccount?.unrealizedPnL ?? stockPositions.reduce((sum, row) => sum + row.pnl, 0)
    const cryptoOpenPnl = cryptoLedger?.totalPnL ?? cryptoPositions.reduce((sum, row) => sum + row.pnl, 0)

    return {
      totalPositions: stockPositions.length + cryptoPositions.length,
      openPnl: stockOpenPnl + cryptoOpenPnl,
      stockExposure: stockPositions.reduce((sum, row) => sum + row.marketValue, 0),
      cryptoExposure: cryptoPositions.reduce((sum, row) => sum + row.marketValue, 0),
      expiringSoon: (stockExitReadiness?.summary.expiringWithinWindowCount ?? 0) + (cryptoExitReadiness?.summary.expiringWithinWindowCount ?? 0),
    }
  }, [cryptoExitReadiness?.summary.expiringWithinWindowCount, cryptoLedger, cryptoPositions, stockAccount, stockExitReadiness?.summary.expiringWithinWindowCount, stockPositions])

  return (
    <div className="space-y-6">
      <header className="rounded-3xl border border-slate-800 bg-slate-900/70 p-6 shadow-2xl shadow-slate-950/30">
        <div className="flex flex-col gap-4 xl:flex-row xl:items-end xl:justify-between">
          <div>
            <div className="mb-2 flex items-center gap-2 text-sm font-medium uppercase tracking-[0.22em] text-cyan-300">
              <Wallet className="h-4 w-4" />
              Positions
            </div>
            <h1 className="text-3xl font-semibold text-white">Inventory and exit pressure board</h1>
            <p className="mt-2 max-w-3xl text-sm leading-6 text-slate-400">
              One page for live inventory, paper inventory, exit deadlines, and the latest tape. No tab labyrinth, no scavenger hunt.
            </p>
          </div>

          <div className="flex flex-wrap gap-3">
            <Pill tone="info" label={`Stock mode ${botStatus?.stockMode ?? 'PAPER'}`} />
            <Pill tone={botStatus?.running ? 'good' : 'warn'} label={botStatus?.running ? 'Runtime active' : 'Runtime paused'} />
            <Pill tone={summary.expiringSoon > 0 ? 'warn' : 'good'} label={`${summary.expiringSoon} expiring soon`} />
          </div>
        </div>
      </header>

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-5">
        <MetricCard label="Open positions" value={String(summary.totalPositions)} detail={`${stockPositions.length} stock · ${cryptoPositions.length} crypto`} icon={<TrendingUp className="h-5 w-5" />} />
        <MetricCard label="Open P&L" value={formatMoney(summary.openPnl)} detail="Across stock and crypto inventory" icon={<Activity className="h-5 w-5" />} />
        <MetricCard label="Stock exposure" value={formatMoney(summary.stockExposure)} detail={formatStockCapacityDetail(stockAccount)} icon={<ShieldCheck className="h-5 w-5" />} />
        <MetricCard label="Crypto exposure" value={formatMoney(summary.cryptoExposure)} detail={formatMoney(cryptoLedger?.balance ?? 0) + ' cash balance'} icon={<Bitcoin className="h-5 w-5" />} />
        <MetricCard label="Expiring within 24h" value={String(summary.expiringSoon)} detail="Time-stop pressure" icon={<Clock3 className="h-5 w-5" />} />
      </div>

      <div className="grid grid-cols-1 gap-6 2xl:grid-cols-[minmax(0,1.5fr)_minmax(360px,0.9fr)]">
        <div className="space-y-6">
          <section className="rounded-3xl border border-slate-800 bg-slate-900/70 p-5 shadow-xl shadow-slate-950/20">
            <div className="mb-4 flex items-center gap-2 text-sm font-semibold uppercase tracking-[0.18em] text-slate-400">
              <TrendingUp className="h-4 w-4 text-cyan-300" />
              Stock positions
            </div>
            <StockPositionsTable positions={stockPositions} />
          </section>

          <section className="rounded-3xl border border-slate-800 bg-slate-900/70 p-5 shadow-xl shadow-slate-950/20">
            <div className="mb-4 flex items-center gap-2 text-sm font-semibold uppercase tracking-[0.18em] text-slate-400">
              <Bitcoin className="h-4 w-4 text-cyan-300" />
              Crypto positions
            </div>
            <CryptoPositionsTable positions={cryptoPositions} />
          </section>
        </div>

        <div className="space-y-6">
          <ExitPressureCard title="Stock exit pressure" snapshot={stockExitReadiness} />
          <ExitPressureCard title="Crypto exit pressure" snapshot={cryptoExitReadiness} />
          <AccountCard title="Stock account" rows={[
            ['Mode', botStatus?.stockMode ?? 'PAPER'],
            ['Portfolio value', formatMoney(stockAccount?.portfolioValue ?? 0)],
            ['Available to trade', formatMoney(getAvailableToTrade(stockAccount))],
            ['Cash', formatMoney(stockAccount?.cash ?? 0)],
            ...(Math.abs(getBrokerBuyingPower(stockAccount) - getAvailableToTrade(stockAccount)) >= 0.01
              ? [['Broker buying power', formatMoney(getBrokerBuyingPower(stockAccount))] as [string, string]]
              : []),
          ]} />
          <AccountCard title="Crypto paper ledger" rows={[
            ['Equity', formatMoney(cryptoLedger?.equity ?? 0)],
            ['Market value', formatMoney(cryptoLedger?.marketValue ?? 0)],
            ['Realized P&L', formatMoney(cryptoLedger?.realizedPnl ?? 0)],
            ['Net P&L', formatMoney(cryptoLedger?.netPnL ?? cryptoLedger?.totalPnL ?? 0)],
          ]} />
        </div>
      </div>

      <div className="grid grid-cols-1 gap-6 2xl:grid-cols-2">
        <section className="rounded-3xl border border-slate-800 bg-slate-900/70 p-5 shadow-xl shadow-slate-950/20">
          <div className="mb-4 flex items-center gap-2 text-sm font-semibold uppercase tracking-[0.18em] text-slate-400">
            <Activity className="h-4 w-4 text-cyan-300" />
            Stock order lifecycle
          </div>
          <StockTapeTable rows={stockHistory} />
        </section>

        <section className="rounded-3xl border border-slate-800 bg-slate-900/70 p-5 shadow-xl shadow-slate-950/20">
          <div className="mb-4 flex items-center gap-2 text-sm font-semibold uppercase tracking-[0.18em] text-slate-400">
            <Bitcoin className="h-4 w-4 text-cyan-300" />
            Crypto trade tape
          </div>
          <CryptoTapeTable rows={cryptoHistory} />
        </section>
      </div>
    </div>
  )
}

function StockPositionsTable({ positions }: { positions: StockPosition[] }) {
  if (positions.length === 0) {
    return <EmptyState message="No active stock positions." />
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[900px] text-sm">
        <thead>
          <tr className="border-b border-slate-800 text-left text-xs uppercase tracking-wide text-slate-500">
            <th className="pb-3 pr-4">Symbol</th>
            <th className="pb-3 pr-4">Shares</th>
            <th className="pb-3 pr-4">Avg</th>
            <th className="pb-3 pr-4">Current</th>
            <th className="pb-3 pr-4">Market value</th>
            <th className="pb-3 pr-4">P&L</th>
            <th className="pb-3">P&L %</th>
          </tr>
        </thead>
        <tbody>
          {positions.map((position) => (
            <tr key={position.symbol} className="border-b border-slate-900/80 text-slate-300">
              <td className="py-3 pr-4 font-semibold text-white">{position.symbol}</td>
              <td className="py-3 pr-4">{position.shares.toFixed(0)}</td>
              <td className="py-3 pr-4">{formatMoney(position.avgPrice)}</td>
              <td className="py-3 pr-4">{formatMoney(position.currentPrice)}</td>
              <td className="py-3 pr-4">{formatMoney(position.marketValue)}</td>
              <td className={`py-3 pr-4 ${position.pnl >= 0 ? 'text-emerald-300' : 'text-rose-300'}`}>{formatMoney(position.pnl)}</td>
              <td className={position.pnlPercent >= 0 ? 'py-3 text-emerald-300' : 'py-3 text-rose-300'}>{formatPercent(position.pnlPercent)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function CryptoPositionsTable({ positions }: { positions: CryptoPosition[] }) {
  if (positions.length === 0) {
    return <EmptyState message="No active crypto positions." />
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[1050px] text-sm">
        <thead>
          <tr className="border-b border-slate-800 text-left text-xs uppercase tracking-wide text-slate-500">
            <th className="pb-3 pr-4">Pair</th>
            <th className="pb-3 pr-4">Amount</th>
            <th className="pb-3 pr-4">Avg</th>
            <th className="pb-3 pr-4">Current</th>
            <th className="pb-3 pr-4">Market value</th>
            <th className="pb-3 pr-4">P&L</th>
            <th className="pb-3 pr-4">P&L %</th>
            <th className="pb-3">Entry</th>
          </tr>
        </thead>
        <tbody>
          {positions.map((position) => (
            <tr key={position.pair} className="border-b border-slate-900/80 text-slate-300">
              <td className="py-3 pr-4 font-semibold text-white">{position.pair}</td>
              <td className="py-3 pr-4">{position.amount.toFixed(6)}</td>
              <td className="py-3 pr-4">{formatMoney(position.avgPrice)}</td>
              <td className="py-3 pr-4">{formatMoney(position.currentPrice)}</td>
              <td className="py-3 pr-4">{formatMoney(position.marketValue)}</td>
              <td className={`py-3 pr-4 ${position.pnl >= 0 ? 'text-emerald-300' : 'text-rose-300'}`}>{formatMoney(position.pnl)}</td>
              <td className={position.pnlPercent >= 0 ? 'py-3 pr-4 text-emerald-300' : 'py-3 pr-4 text-rose-300'}>{formatPercent(position.pnlPercent)}</td>
              <td className="py-3 text-slate-400">{formatTimestamp(position.entryTimeUtc)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function StockTapeTable({ rows }: { rows: OrderIntentRecord[] }) {
  if (rows.length === 0) {
    return <EmptyState message="No stock lifecycle records yet." />
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[1100px] text-sm">
        <thead>
          <tr className="border-b border-slate-800 text-left text-xs uppercase tracking-wide text-slate-500">
            <th className="pb-3 pr-4">Submitted</th>
            <th className="pb-3 pr-4">Symbol</th>
            <th className="pb-3 pr-4">Side</th>
            <th className="pb-3 pr-4">Status</th>
            <th className="pb-3 pr-4">Requested</th>
            <th className="pb-3 pr-4">Filled</th>
            <th className="pb-3 pr-4">Avg fill</th>
            <th className="pb-3">Source</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.intentId} className="border-b border-slate-900/80 text-slate-300">
              <td className="py-3 pr-4 text-slate-400">{formatTimestamp(row.submittedAt ?? row.firstFillAt ?? row.lastFillAt)}</td>
              <td className="py-3 pr-4 font-semibold text-white">{row.symbol}</td>
              <td className="py-3 pr-4">{row.side}</td>
              <td className="py-3 pr-4"><Pill tone={row.status === 'FILLED' ? 'good' : row.status === 'REJECTED' ? 'danger' : 'info'} label={row.status} compact /></td>
              <td className="py-3 pr-4">{row.requestedQuantity.toFixed(2)}</td>
              <td className="py-3 pr-4">{row.filledQuantity.toFixed(2)}</td>
              <td className="py-3 pr-4">{row.avgFillPrice != null ? formatMoney(row.avgFillPrice) : '—'}</td>
              <td className="py-3">{row.executionSource}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function CryptoTapeTable({ rows }: { rows: TradeHistoryEntry[] }) {
  if (rows.length === 0) {
    return <EmptyState message="No crypto trades yet." />
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[1000px] text-sm">
        <thead>
          <tr className="border-b border-slate-800 text-left text-xs uppercase tracking-wide text-slate-500">
            <th className="pb-3 pr-4">Time</th>
            <th className="pb-3 pr-4">Pair</th>
            <th className="pb-3 pr-4">Side</th>
            <th className="pb-3 pr-4">Amount</th>
            <th className="pb-3 pr-4">Price</th>
            <th className="pb-3 pr-4">Total</th>
            <th className="pb-3 pr-4">Balance</th>
            <th className="pb-3">Status</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.id} className="border-b border-slate-900/80 text-slate-300">
              <td className="py-3 pr-4 text-slate-400">{formatTimestamp(row.timestamp)}</td>
              <td className="py-3 pr-4 font-semibold text-white">{row.pair ?? row.symbol ?? '—'}</td>
              <td className="py-3 pr-4">{row.side}</td>
              <td className="py-3 pr-4">{row.amount?.toFixed(6) ?? '—'}</td>
              <td className="py-3 pr-4">{row.price != null ? formatMoney(row.price) : '—'}</td>
              <td className="py-3 pr-4">{row.total != null ? formatMoney(row.total) : '—'}</td>
              <td className="py-3 pr-4">{row.balance != null ? formatMoney(row.balance) : '—'}</td>
              <td className="py-3"><Pill tone={row.status === 'FILLED' ? 'good' : row.status === 'REJECTED' ? 'danger' : 'info'} label={row.status} compact /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function ExitPressureCard({ title, snapshot }: { title: string; snapshot: WatchlistExitReadinessSnapshot | undefined }) {
  return (
    <div className="rounded-3xl border border-slate-800 bg-slate-900/70 p-5 shadow-xl shadow-slate-950/20">
      <div className="mb-4 text-sm font-semibold uppercase tracking-[0.18em] text-slate-400">{title}</div>
      <div className="space-y-3 text-sm text-slate-400">
        <SummaryRow label="Open positions" value={String(snapshot?.summary.openPositionCount ?? 0)} />
        <SummaryRow label="Expired" value={String(snapshot?.summary.expiredPositionCount ?? 0)} tone={(snapshot?.summary.expiredPositionCount ?? 0) > 0 ? 'warn' : 'muted'} />
        <SummaryRow label="Protective pending" value={String(snapshot?.summary.protectiveExitPendingCount ?? 0)} tone={(snapshot?.summary.protectiveExitPendingCount ?? 0) > 0 ? 'warn' : 'muted'} />
        <SummaryRow label="Scale-out ready" value={String(snapshot?.summary.scaleOutReadyCount ?? 0)} />
        <SummaryRow label="Follow-through failed" value={String(snapshot?.summary.followThroughFailedCount ?? 0)} tone={(snapshot?.summary.followThroughFailedCount ?? 0) > 0 ? 'warn' : 'muted'} />
      </div>
    </div>
  )
}

function AccountCard({ title, rows }: { title: string; rows: [string, string][] }) {
  return (
    <div className="rounded-3xl border border-slate-800 bg-slate-900/70 p-5 shadow-xl shadow-slate-950/20">
      <div className="mb-4 text-sm font-semibold uppercase tracking-[0.18em] text-slate-400">{title}</div>
      <div className="space-y-3 text-sm text-slate-400">
        {rows.map(([label, value]) => (
          <SummaryRow key={label} label={label} value={value} />
        ))}
      </div>
    </div>
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

function SummaryRow({ label, value, tone = 'muted' }: { label: string; value: string; tone?: 'good' | 'warn' | 'danger' | 'info' | 'muted' }) {
  return (
    <div className="flex items-start justify-between gap-4">
      <div className="text-slate-500">{label}</div>
      <div className={toneTextClass(tone)}>{value}</div>
    </div>
  )
}

function EmptyState({ message }: { message: string }) {
  return <div className="rounded-2xl border border-dashed border-slate-700 px-4 py-8 text-center text-sm text-slate-500">{message}</div>
}

function Pill({ label, tone, compact = false }: { label: string; tone: 'good' | 'warn' | 'danger' | 'info'; compact?: boolean }) {
  return <span className={`${compact ? 'px-2 py-1 text-[11px]' : 'px-3 py-2 text-sm'} rounded-full ${toneBadgeClass(tone)}`}>{label}</span>
}

function toneBadgeClass(tone: 'good' | 'warn' | 'danger' | 'info') {
  switch (tone) {
    case 'good':
      return 'border border-emerald-700/60 bg-emerald-500/10 text-emerald-200'
    case 'warn':
      return 'border border-amber-700/60 bg-amber-500/10 text-amber-200'
    case 'danger':
      return 'border border-rose-700/60 bg-rose-500/10 text-rose-200'
    default:
      return 'border border-cyan-700/60 bg-cyan-500/10 text-cyan-200'
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
