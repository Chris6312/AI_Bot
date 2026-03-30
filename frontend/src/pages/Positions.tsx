import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { format } from 'date-fns'
import { Activity, Bitcoin, Clock3, ShieldCheck, TrendingUp, Wallet } from 'lucide-react'

import { api } from '@/lib/api'
import {
  DetailRow,
  EmptyState,
  MetricCard,
  PageHero,
  SectionCard,
  StatusPill,
  ToneBadge,
  type Tone,
} from '@/components/operator-ui'
import type {
  BotStatus,
  CryptoLedger,
  CryptoPosition,
  OrderIntentRecord,
  StockAccount,
  StockPosition,
  TradeHistoryEntry,
  WatchlistExitReadinessSnapshot,
  WatchlistSymbolRecord,
} from '@/types'

function formatMoney(value: number) {
  return `$${value.toFixed(2)}`
}

function formatPercent(value: number) {
  const prefix = value >= 0 ? '+' : ''
  return `${prefix}${value.toFixed(2)}%`
}

function formatTimestamp(value?: string | null) {
  if (!value) return '—'
  return format(new Date(value), 'MMM dd, yyyy HH:mm')
}

function getAvailableToTrade(account?: StockAccount) {
  if (!account) return 0
  return account.availableToTrade ?? account.cash ?? account.buyingPower ?? 0
}

function getBrokerBuyingPower(account?: StockAccount) {
  if (!account) return 0
  return account.brokerBuyingPower ?? account.buyingPower ?? 0
}

function toneFromPnl(value: number): Tone {
  if (value > 0) return 'good'
  if (value < 0) return 'danger'
  return 'muted'
}

type ActionRow = {
  scope: 'Stocks' | 'Crypto'
  symbol: string
  reason: string
  detail: string
  tone: Tone
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
    const stockOpenPnl = stockAccount?.unrealizedPnL ?? stockPositions.reduce((sum, row) => sum + row.pnl, 0)
    const cryptoOpenPnl = cryptoLedger?.netPnL ?? cryptoLedger?.totalPnL ?? cryptoPositions.reduce((sum, row) => sum + row.pnl, 0)

    return {
      totalPositions: stockPositions.length + cryptoPositions.length,
      openPnl: stockOpenPnl + cryptoOpenPnl,
      stockExposure: stockPositions.reduce((sum, row) => sum + row.marketValue, 0),
      cryptoExposure: cryptoPositions.reduce((sum, row) => sum + row.marketValue, 0),
      expiringSoon: (stockExitReadiness?.summary.expiringWithinWindowCount ?? 0) + (cryptoExitReadiness?.summary.expiringWithinWindowCount ?? 0),
      protectivePending: (stockExitReadiness?.summary.protectiveExitPendingCount ?? 0) + (cryptoExitReadiness?.summary.protectiveExitPendingCount ?? 0),
    }
  }, [cryptoExitReadiness?.summary.expiringWithinWindowCount, cryptoExitReadiness?.summary.protectiveExitPendingCount, cryptoLedger, cryptoPositions, stockAccount, stockExitReadiness?.summary.expiringWithinWindowCount, stockExitReadiness?.summary.protectiveExitPendingCount, stockPositions])

  const actionRows = useMemo(() => {
    const rows: ActionRow[] = []
    collectActionRows(rows, 'Stocks', stockExitReadiness)
    collectActionRows(rows, 'Crypto', cryptoExitReadiness)
    return rows.slice(0, 10)
  }, [cryptoExitReadiness, stockExitReadiness])

  return (
    <div className="space-y-6">
      <PageHero
        eyebrow={
          <>
            <Wallet className="h-4 w-4" />
            Positions
          </>
        }
        title="Inventory and exit pressure board"
        description="One page for live inventory, paper inventory, exit pressure, and the latest tape. This is the bot’s cargo manifest with the emergency labels still attached."
        aside={
          <>
            <StatusPill tone="info" label={`Stock mode ${botStatus?.stockMode ?? 'PAPER'}`} />
            <StatusPill tone={botStatus?.running ? 'good' : 'warn'} label={botStatus?.running ? 'Runtime active' : 'Runtime paused'} />
            <StatusPill tone={summary.protectivePending > 0 ? 'warn' : 'good'} label={`${summary.protectivePending} protective`} />
            <StatusPill tone={summary.expiringSoon > 0 ? 'warn' : 'good'} label={`${summary.expiringSoon} expiring soon`} />
          </>
        }
      />

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-5">
        <MetricCard label="Open positions" value={String(summary.totalPositions)} detail={`${stockPositions.length} stock · ${cryptoPositions.length} crypto`} icon={<TrendingUp className="h-5 w-5" />} />
        <MetricCard label="Open P&L" value={formatMoney(summary.openPnl)} detail="Across stock and crypto inventory" icon={<Activity className="h-5 w-5" />} />
        <MetricCard label="Stock exposure" value={formatMoney(summary.stockExposure)} detail={`${formatMoney(getAvailableToTrade(stockAccount))} available`} icon={<ShieldCheck className="h-5 w-5" />} />
        <MetricCard label="Crypto exposure" value={formatMoney(summary.cryptoExposure)} detail={`${formatMoney(cryptoLedger?.balance ?? 0)} cash balance`} icon={<Bitcoin className="h-5 w-5" />} />
        <MetricCard label="Expiring within 24h" value={String(summary.expiringSoon)} detail="Time-stop pressure" icon={<Clock3 className="h-5 w-5" />} />
      </div>

      <div className="grid grid-cols-1 gap-6 2xl:grid-cols-[minmax(0,1.45fr)_minmax(360px,0.95fr)]">
        <div className="space-y-6">
          <SectionCard title="Stock positions" eyebrow="Inventory" icon={<TrendingUp className="h-4 w-4 text-cyan-300" />}>
            <StockPositionsTable positions={stockPositions} />
          </SectionCard>

          <SectionCard title="Crypto positions" eyebrow="Inventory" icon={<Bitcoin className="h-4 w-4 text-cyan-300" />}>
            <CryptoPositionsTable positions={cryptoPositions} />
          </SectionCard>
        </div>

        <div className="space-y-6">
          <SectionCard title="Action queue" eyebrow="Operator focus" icon={<Clock3 className="h-4 w-4 text-amber-300" />}>
            {actionRows.length === 0 ? (
              <EmptyState message="No urgent exit pressure is visible right now." />
            ) : (
              <div className="space-y-3">
                {actionRows.map((row) => (
                  <div key={`${row.scope}-${row.symbol}-${row.reason}`} className="rounded-2xl border border-slate-800 bg-slate-950/60 p-4">
                    <div className="flex items-center justify-between gap-3">
                      <div>
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="text-base font-semibold text-white">{row.symbol}</span>
                          <ToneBadge tone={row.tone}>{row.scope}</ToneBadge>
                        </div>
                        <div className="mt-2 text-sm text-slate-300">{row.reason}</div>
                      </div>
                      <ToneBadge tone={row.tone}>{row.detail}</ToneBadge>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </SectionCard>

          <AccountCard
            title="Stock account"
            rows={[
              ['Mode', botStatus?.stockMode ?? 'PAPER', 'info'],
              ['Portfolio value', formatMoney(stockAccount?.portfolioValue ?? 0), 'muted'],
              ['Available to trade', formatMoney(getAvailableToTrade(stockAccount)), 'good'],
              ['Cash', formatMoney(stockAccount?.cash ?? 0), 'muted'],
              ...(Math.abs(getBrokerBuyingPower(stockAccount) - getAvailableToTrade(stockAccount)) >= 0.01
                ? [['Broker buying power', formatMoney(getBrokerBuyingPower(stockAccount)), 'warn'] as [string, string, Tone]]
                : []),
            ]}
          />

          <AccountCard
            title="Crypto paper ledger"
            rows={[
              ['Equity', formatMoney(cryptoLedger?.equity ?? 0), 'info'],
              ['Market value', formatMoney(cryptoLedger?.marketValue ?? 0), 'muted'],
              ['Realized P&L', formatMoney(cryptoLedger?.realizedPnL ?? 0), toneFromPnl(cryptoLedger?.realizedPnL ?? 0)],
              ['Net P&L', formatMoney(cryptoLedger?.netPnL ?? cryptoLedger?.totalPnL ?? 0), toneFromPnl(cryptoLedger?.netPnL ?? cryptoLedger?.totalPnL ?? 0)],
            ]}
          />
        </div>
      </div>

      <div className="grid grid-cols-1 gap-6 2xl:grid-cols-2">
        <SectionCard title="Stock order lifecycle" eyebrow="Recent tape" icon={<Activity className="h-4 w-4 text-cyan-300" />}>
          <StockTapeTable rows={stockHistory} />
        </SectionCard>

        <SectionCard title="Crypto trade tape" eyebrow="Recent tape" icon={<Bitcoin className="h-4 w-4 text-cyan-300" />}>
          <CryptoTapeTable rows={cryptoHistory} />
        </SectionCard>
      </div>
    </div>
  )
}

function collectActionRows(target: ActionRow[], scope: 'Stocks' | 'Crypto', snapshot: WatchlistExitReadinessSnapshot | null | undefined) {
  const rows = snapshot?.rows ?? []
  for (const row of rows) {
    if (!row.positionState?.hasOpenPosition) {
      continue
    }

    const item = buildActionRow(scope, row)
    if (item) {
      target.push(item)
    }
  }
}

function buildActionRow(scope: 'Stocks' | 'Crypto', row: WatchlistSymbolRecord): ActionRow | null {
  const position = row.positionState
  if (!position) return null

  if (position.protectiveExitPending) {
    return {
      scope,
      symbol: row.symbol,
      reason: (position.protectiveExitReasons ?? ['Protective exit pending']).join(' · '),
      detail: position.currentPrice != null ? formatMoney(position.currentPrice) : 'protective',
      tone: 'warn',
    }
  }

  if (position.positionExpired) {
    return {
      scope,
      symbol: row.symbol,
      reason: 'Position has crossed its time-stop deadline.',
      detail: position.exitDeadlineSource ?? 'expired',
      tone: 'danger',
    }
  }

  if ((position.hoursUntilExpiry ?? 999) <= 24) {
    return {
      scope,
      symbol: row.symbol,
      reason: 'Position is inside the next 24-hour expiry window.',
      detail: `${Math.max(position.hoursUntilExpiry ?? 0, 0).toFixed(1)}h left`,
      tone: 'warn',
    }
  }

  if (position.profitTargetReached) {
    return {
      scope,
      symbol: row.symbol,
      reason: 'Profit target has been reached.',
      detail: position.profitTarget != null ? formatMoney(position.profitTarget) : 'target',
      tone: 'good',
    }
  }

  if (position.scaleOutReady) {
    return {
      scope,
      symbol: row.symbol,
      reason: 'Scale-out is armed and waiting for execution logic.',
      detail: position.currentPrice != null ? formatMoney(position.currentPrice) : 'ready',
      tone: 'info',
    }
  }

  return null
}

function StockPositionsTable({ positions }: { positions: StockPosition[] }) {
  if (positions.length === 0) {
    return <EmptyState message="No active stock positions." />
  }

  const sorted = [...positions].sort((a, b) => b.marketValue - a.marketValue)

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
            <th className="pb-3 pr-4">P&amp;L</th>
            <th className="pb-3">P&amp;L %</th>
          </tr>
        </thead>
        <tbody>
          {sorted.map((position) => (
            <tr key={position.symbol} className="border-b border-slate-900/80 text-slate-300">
              <td className="py-3 pr-4 font-semibold text-white">{position.symbol}</td>
              <td className="py-3 pr-4">{position.shares}</td>
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

  const sorted = [...positions].sort((a, b) => b.marketValue - a.marketValue)

  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[980px] text-sm">
        <thead>
          <tr className="border-b border-slate-800 text-left text-xs uppercase tracking-wide text-slate-500">
            <th className="pb-3 pr-4">Pair</th>
            <th className="pb-3 pr-4">Amount</th>
            <th className="pb-3 pr-4">Avg</th>
            <th className="pb-3 pr-4">Current</th>
            <th className="pb-3 pr-4">Market value</th>
            <th className="pb-3 pr-4">Realized</th>
            <th className="pb-3 pr-4">P&amp;L</th>
            <th className="pb-3">P&amp;L %</th>
          </tr>
        </thead>
        <tbody>
          {sorted.map((position) => (
            <tr key={position.pair} className="border-b border-slate-900/80 text-slate-300">
              <td className="py-3 pr-4 font-semibold text-white">{position.pair}</td>
              <td className="py-3 pr-4">{position.amount}</td>
              <td className="py-3 pr-4">{formatMoney(position.avgPrice)}</td>
              <td className="py-3 pr-4">{formatMoney(position.currentPrice)}</td>
              <td className="py-3 pr-4">{formatMoney(position.marketValue)}</td>
              <td className={`py-3 pr-4 ${(position.realizedPnl ?? 0) >= 0 ? 'text-emerald-300' : 'text-rose-300'}`}>{formatMoney(position.realizedPnl ?? 0)}</td>
              <td className={`py-3 pr-4 ${position.pnl >= 0 ? 'text-emerald-300' : 'text-rose-300'}`}>{formatMoney(position.pnl)}</td>
              <td className={position.pnlPercent >= 0 ? 'py-3 text-emerald-300' : 'py-3 text-rose-300'}>{formatPercent(position.pnlPercent)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function AccountCard({ title, rows }: { title: string; rows: [string, string, Tone][] }) {
  return (
    <SectionCard title={title} eyebrow="Account state" icon={<Wallet className="h-4 w-4 text-cyan-300" />}>
      <div className="space-y-3 text-sm text-slate-400">
        {rows.map(([label, value, tone]) => (
          <DetailRow key={label} label={label} value={value} tone={tone} />
        ))}
      </div>
    </SectionCard>
  )
}

function StockTapeTable({ rows }: { rows: OrderIntentRecord[] }) {
  if (rows.length === 0) {
    return <EmptyState message="No stock lifecycle records yet." />
  }

  return (
    <div className="space-y-3">
      {rows.slice(0, 10).map((row) => (
        <div key={row.intentId} className="rounded-2xl border border-slate-800 bg-slate-950/60 p-4">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
            <div>
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-base font-semibold text-white">{row.symbol}</span>
                <ToneBadge tone={row.status.toUpperCase().includes('REJECT') ? 'danger' : row.status.toUpperCase().includes('FILL') ? 'good' : 'info'}>{row.status}</ToneBadge>
              </div>
              <div className="mt-2 text-sm text-slate-400">{row.side} · qty {row.requestedQuantity} · {row.executionSource}</div>
            </div>
            <div className="text-sm text-slate-500">{formatTimestamp(row.lastFillAt ?? row.submittedAt)}</div>
          </div>
          <div className="mt-3 text-sm text-slate-300">{row.rejectionReason ?? row.events[row.events.length - 1]?.message ?? 'Lifecycle events recorded.'}</div>
        </div>
      ))}
    </div>
  )
}

function CryptoTapeTable({ rows }: { rows: TradeHistoryEntry[] }) {
  if (rows.length === 0) {
    return <EmptyState message="No crypto trades recorded yet." />
  }

  return (
    <div className="space-y-3">
      {rows.slice(0, 10).map((row) => (
        <div key={row.id} className="rounded-2xl border border-slate-800 bg-slate-950/60 p-4">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
            <div>
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-base font-semibold text-white">{row.pair ?? row.symbol ?? 'Unknown pair'}</span>
                <ToneBadge tone={row.status.toUpperCase().includes('REJECT') ? 'danger' : row.side === 'SELL' ? 'warn' : 'good'}>{row.side}</ToneBadge>
              </div>
              <div className="mt-2 text-sm text-slate-400">{row.amount ?? row.shares ?? 0} @ {formatMoney(row.price ?? 0)}</div>
            </div>
            <div className="text-sm text-slate-500">{formatTimestamp(row.timestamp)}</div>
          </div>
          <div className="mt-3 text-sm text-slate-300">Status {row.status} · Total {formatMoney(row.total ?? 0)}</div>
        </div>
      ))}
    </div>
  )
}
