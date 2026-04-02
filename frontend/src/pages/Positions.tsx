import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { format } from 'date-fns'
import { Activity, Bitcoin, Clock3, Database, TrendingUp, Wallet, X } from 'lucide-react'

import { api } from '@/lib/api'
import {
  DetailRow,
  EmptyState,
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
  PositionInspectRecord,
  WatchlistExitReadinessSnapshot,
  WatchlistSymbolRecord,
} from '@/types'

function formatMoney(value: number) {
  return `$${value.toFixed(2)}`
}

function formatMaybeMoney(value?: number | null) {
  return value == null ? '—' : formatMoney(value)
}

function formatPercent(value: number) {
  const prefix = value >= 0 ? '+' : ''
  return `${prefix}${value.toFixed(2)}%`
}

function formatMaybePercent(value?: number | null) {
  return value == null ? '—' : formatPercent(value)
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

type DbStockPosition = {
  ticker: string
  accountId?: string | null
  shares: number
  avgEntryPrice?: number | null
  currentPrice?: number | null
  unrealizedPnl?: number | null
  unrealizedPnlPct?: number | null
  strategy?: string | null
  entryTime?: string | null
  entryReasoning?: Record<string, unknown> | null
  stopLoss?: number | null
  profitTarget?: number | null
  peakPrice?: number | null
  trailingStop?: number | null
  isOpen: boolean
  executionId?: string | null
  createdAt?: string | null
  updatedAt?: string | null
}

async function getDbStockPositions(): Promise<DbStockPosition[]> {
  const response = await fetch('/api/stocks/db-positions')
  if (!response.ok) {
    throw new Error(`Failed to fetch DB stock positions: ${response.status}`)
  }
  return response.json()
}

export default function Positions() {
  const [selectedInspectTarget, setSelectedInspectTarget] = useState<{ assetClass: 'stock' | 'crypto'; symbol: string; fallbackTitle: string } | null>(null)
  const [selectedInspect, setSelectedInspect] = useState<PositionInspectRecord | null>(null)
  const [inspectLoading, setInspectLoading] = useState(false)
  const [inspectError, setInspectError] = useState<string | null>(null)

  const { data: stockPositions = [] } = useQuery<StockPosition[]>({
    queryKey: ['stockPositions'],
    queryFn: api.getStockPositions,
    refetchInterval: 5000,
  })

  const { data: dbStockPositions = [] } = useQuery<DbStockPosition[]>({
    queryKey: ['stockDbPositions'],
    queryFn: getDbStockPositions,
    refetchInterval: 10000,
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
    const openDbPositions = dbStockPositions.filter((row) => row.isOpen)
    const brokerSymbols = new Set(stockPositions.map((row) => row.symbol.toUpperCase()))
    const dbSymbols = new Set(openDbPositions.map((row) => row.ticker.toUpperCase()))
    const brokerOnlySymbols = [...brokerSymbols].filter((symbol) => !dbSymbols.has(symbol))
    const dbOnlySymbols = [...dbSymbols].filter((symbol) => !brokerSymbols.has(symbol))

    return {
      totalPositions: stockPositions.length + cryptoPositions.length,
      openPnl: stockOpenPnl + cryptoOpenPnl,
      stockExposure: stockPositions.reduce((sum, row) => sum + row.marketValue, 0),
      cryptoExposure: cryptoPositions.reduce((sum, row) => sum + row.marketValue, 0),
      expiringSoon: (stockExitReadiness?.summary.expiringWithinWindowCount ?? 0) + (cryptoExitReadiness?.summary.expiringWithinWindowCount ?? 0),
      protectivePending: (stockExitReadiness?.summary.protectiveExitPendingCount ?? 0) + (cryptoExitReadiness?.summary.protectiveExitPendingCount ?? 0),
      dbOpenCount: openDbPositions.length,
      dbTotalCount: dbStockPositions.length,
      brokerOnlySymbols,
      dbOnlySymbols,
      positionSourceMismatch: brokerOnlySymbols.length > 0 || dbOnlySymbols.length > 0,
    }
  }, [cryptoExitReadiness?.summary.expiringWithinWindowCount, cryptoExitReadiness?.summary.protectiveExitPendingCount, cryptoLedger, cryptoPositions, dbStockPositions, stockAccount, stockExitReadiness?.summary.expiringWithinWindowCount, stockExitReadiness?.summary.protectiveExitPendingCount, stockPositions])

  const actionRows = useMemo(() => {
    const rows: ActionRow[] = []
    collectActionRows(rows, 'Stocks', stockExitReadiness)
    collectActionRows(rows, 'Crypto', cryptoExitReadiness)
    return rows.slice(0, 10)
  }, [cryptoExitReadiness, stockExitReadiness])


  useEffect(() => {
    if (!selectedInspectTarget) {
      setSelectedInspect(null)
      setInspectLoading(false)
      setInspectError(null)
      return
    }

    let cancelled = false
    setInspectLoading(true)
    setInspectError(null)
    getPositionInspect(selectedInspectTarget.assetClass, selectedInspectTarget.symbol)
      .then((payload) => {
        if (!cancelled) setSelectedInspect(payload)
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          const message = error instanceof Error ? error.message : 'Failed to load inspect payload.'
          setInspectError(message)
          setSelectedInspect(null)
        }
      })
      .finally(() => {
        if (!cancelled) setInspectLoading(false)
      })

    return () => {
      cancelled = true
    }
  }, [selectedInspectTarget])

  const mismatchDetail = useMemo(() => {
    if (!summary.positionSourceMismatch) {
      return 'Broker inventory and DB mirror are aligned right now.'
    }
    const parts: string[] = []
    if (summary.brokerOnlySymbols.length > 0) {
      parts.push(`Broker only: ${summary.brokerOnlySymbols.join(', ')}`)
    }
    if (summary.dbOnlySymbols.length > 0) {
      parts.push(`DB only: ${summary.dbOnlySymbols.join(', ')}`)
    }
    return parts.join(' · ')
  }, [summary.brokerOnlySymbols, summary.dbOnlySymbols, summary.positionSourceMismatch])

  return (
    <>
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

        <div className="grid grid-cols-1 gap-6 xl:grid-cols-3">
          <AccountCard
            title="Stock account"
            rows={[
              ['Mode', botStatus?.stockMode ?? 'PAPER', 'info'],
              ['Portfolio value', formatMoney(stockAccount?.portfolioValue ?? 0), 'muted'],
              ['Available to trade', formatMoney(getAvailableToTrade(stockAccount)), 'good'],
              ...(Math.abs((stockAccount?.cash ?? 0) - getAvailableToTrade(stockAccount)) >= 0.01
                ? [['Cash', formatMoney(stockAccount?.cash ?? 0), 'muted'] as [string, string, Tone]]
                : []),
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

          <SectionCard title="Inventory summary" eyebrow="Pressure board" icon={<Clock3 className="h-4 w-4 text-amber-300" />}>
            <div className="space-y-3 text-sm text-slate-400">
              <DetailRow label="Open positions" value={String(summary.totalPositions)} tone="info" />
              <DetailRow label="Open split" value={`${stockPositions.length} stock · ${cryptoPositions.length} crypto`} tone="muted" />
              <DetailRow label="Protective pending" value={String(summary.protectivePending)} tone={summary.protectivePending > 0 ? 'warn' : 'good'} />
              <DetailRow label="Expiring within 24h" value={String(summary.expiringSoon)} tone={summary.expiringSoon > 0 ? 'warn' : 'good'} />
              <DetailRow label="Stock exposure" value={formatMoney(summary.stockExposure)} tone="muted" />
              <DetailRow label="Crypto exposure" value={formatMoney(summary.cryptoExposure)} tone="muted" />
              <DetailRow label="Open P&L" value={formatMoney(summary.openPnl)} tone={toneFromPnl(summary.openPnl)} />
            </div>
          </SectionCard>
        </div>

        <div className="grid grid-cols-1 gap-6 2xl:grid-cols-[minmax(0,1.45fr)_minmax(360px,0.95fr)]">
          <div className="space-y-6">
            <SectionCard
              title="Stock positions"
              eyebrow="Inventory · broker snapshot"
              icon={<TrendingUp className="h-4 w-4 text-cyan-300" />}
              actions={<StatusPill tone={summary.positionSourceMismatch ? 'warn' : 'good'} label={`Broker ${stockPositions.length} · DB open ${summary.dbOpenCount}`} />}
            >
              <div className="mb-4 text-sm text-slate-500">This table comes straight from Tradier paper/live inventory, which is why it can show positions even when the local DB mirror is behind.</div>
              <StockPositionsTable positions={stockPositions} />
            </SectionCard>

            <SectionCard
              title="Stock position mirror"
              eyebrow="Inventory · local database"
              icon={<Database className="h-4 w-4 text-cyan-300" />}
              actions={
                <div className="flex flex-wrap gap-2">
                  <StatusPill tone={summary.positionSourceMismatch ? 'warn' : 'good'} label={summary.positionSourceMismatch ? 'Mirror drift detected' : 'Mirror aligned'} />
                  <StatusPill tone="muted" label={`${summary.dbTotalCount} DB rows`} />
                </div>
              }
            >
              <div className="mb-4 rounded-2xl border border-slate-800 bg-slate-950/60 px-4 py-3 text-sm text-slate-400">
                <div className="font-medium text-slate-200">Why the pages can disagree</div>
                <div className="mt-2">The broker table above is fetched from Tradier in real time. This mirror below is fetched from the local <code className="rounded bg-slate-900 px-1 py-0.5 text-slate-200">positions</code> table. When those diverge, monitoring can lose track of managed-only rows until the DB mirror catches up.</div>
                <div className={`mt-3 ${summary.positionSourceMismatch ? 'text-amber-300' : 'text-emerald-300'}`}>{mismatchDetail}</div>
              </div>
              <DbStockPositionsTable positions={dbStockPositions} onSelect={(position) => setSelectedInspectTarget({ assetClass: 'stock', symbol: position.ticker, fallbackTitle: position.ticker })} />
            </SectionCard>

            <SectionCard title="Crypto positions" eyebrow="Inventory" icon={<Bitcoin className="h-4 w-4 text-cyan-300" />}>
              <CryptoPositionsTable positions={cryptoPositions} onSelect={(position) => setSelectedInspectTarget({ assetClass: 'crypto', symbol: position.pair, fallbackTitle: position.pair })} />
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

      <PositionInspectDrawer
        inspect={selectedInspect}
        loading={inspectLoading}
        error={inspectError}
        fallbackTitle={selectedInspectTarget?.fallbackTitle ?? null}
        onClose={() => {
          setSelectedInspectTarget(null)
          setSelectedInspect(null)
          setInspectError(null)
        }}
      />
    </>
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
            <th className="pb-3 pr-4">P&amp;L %</th>
            <th className="pb-3">Inspect</th>
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
              <td className={`${position.pnlPercent >= 0 ? 'py-3 pr-4 text-emerald-300' : 'py-3 pr-4 text-rose-300'}`}>{formatPercent(position.pnlPercent)}</td>
              <td className="py-3">
                <button
                  type="button"
                  onClick={() => onSelect(position)}
                  className="rounded-full border border-slate-700 bg-slate-950/70 px-3 py-1 text-xs font-semibold uppercase tracking-wide text-slate-200 transition hover:border-cyan-600 hover:text-cyan-200"
                >
                  Inspect
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function DbStockPositionsTable({ positions, onSelect }: { positions: DbStockPosition[]; onSelect: (position: DbStockPosition) => void }) {
  if (positions.length === 0) {
    return <EmptyState message="No rows are currently stored in the positions table." />
  }

  const sorted = [...positions].sort((a, b) => {
    if (a.isOpen !== b.isOpen) {
      return a.isOpen ? -1 : 1
    }
    return String(b.entryTime ?? b.createdAt ?? '').localeCompare(String(a.entryTime ?? a.createdAt ?? ''))
  })

  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[980px] text-sm">
        <thead>
          <tr className="border-b border-slate-800 text-left text-xs uppercase tracking-wide text-slate-500">
            <th className="pb-3 pr-4">Ticker</th>
            <th className="pb-3 pr-4">Shares</th>
            <th className="pb-3 pr-4">Avg entry</th>
            <th className="pb-3 pr-4">Current</th>
            <th className="pb-3 pr-4">Unrealized P&amp;L</th>
            <th className="pb-3 pr-4">State</th>
            <th className="pb-3">Inspect</th>
          </tr>
        </thead>
        <tbody>
          {sorted.map((position, index) => (
            <tr key={`${position.ticker}-${position.entryTime ?? position.createdAt ?? index}`} className="border-b border-slate-900/80 text-slate-300">
              <td className="py-3 pr-4 font-semibold text-white">{position.ticker}</td>
              <td className="py-3 pr-4">{position.shares}</td>
              <td className="py-3 pr-4">{formatMaybeMoney(position.avgEntryPrice)}</td>
              <td className="py-3 pr-4">{formatMaybeMoney(position.currentPrice)}</td>
              <td className={`py-3 pr-4 ${(position.unrealizedPnl ?? 0) >= 0 ? 'text-emerald-300' : 'text-rose-300'}`}>{formatMaybeMoney(position.unrealizedPnl)}</td>
              <td className="py-3 pr-4">
                <ToneBadge tone={position.isOpen ? 'good' : 'muted'}>{position.isOpen ? 'Open' : 'Closed'}</ToneBadge>
              </td>
              <td className="py-3">
                <button
                  type="button"
                  onClick={() => onSelect(position)}
                  className="rounded-full border border-slate-700 bg-slate-950/70 px-3 py-1 text-xs font-semibold uppercase tracking-wide text-slate-200 transition hover:border-cyan-600 hover:text-cyan-200"
                >
                  Inspect
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function PositionInspectDrawer({
  inspect,
  loading,
  error,
  fallbackTitle,
  onClose,
}: {
  inspect: PositionInspectRecord | null
  loading: boolean
  error: string | null
  fallbackTitle: string | null
  onClose: () => void
}) {
  if (!inspect && !loading && !error && !fallbackTitle) return null

  const detailRows = inspect
    ? Object.entries(inspect.positionSnapshot ?? {}).map(([label, value]) => [humanizeKey(label), renderInspectValue(label, value)] as [string, string])
    : []

  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-slate-950/80">
      <button type="button" aria-label="Close position drawer" className="absolute inset-0 cursor-default" onClick={onClose} />
      <aside className="relative h-full w-full max-w-xl overflow-y-auto border-l border-slate-800 bg-slate-950 p-6 shadow-2xl shadow-black/60">
        <div className="flex items-start justify-between gap-4">
          <div>
            <div className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-500">Position inspect</div>
            <h2 className="mt-1 text-2xl font-semibold text-white">{inspect?.displaySymbol ?? fallbackTitle ?? 'Inspect'}</h2>
            <div className="mt-3 flex flex-wrap gap-2">
              <ToneBadge tone="info">{inspect?.assetClass ?? 'loading'}</ToneBadge>
              {inspect?.inspectSource ? <ToneBadge tone="muted">{inspect.inspectSource}</ToneBadge> : null}
            </div>
          </div>
          <button type="button" onClick={onClose} className="rounded-full border border-slate-700 p-2 text-slate-300 transition hover:border-cyan-600 hover:text-cyan-200">
            <X className="h-4 w-4" />
          </button>
        </div>

        {loading ? <div className="mt-6 rounded-2xl border border-slate-800 bg-slate-900/70 p-4 text-sm text-slate-300">Loading inspect payload…</div> : null}
        {error ? <div className="mt-6 rounded-2xl border border-rose-900/70 bg-rose-950/30 p-4 text-sm text-rose-200">{error}</div> : null}

        {inspect ? (
          <>
            <div className="mt-6 rounded-2xl border border-slate-800 bg-slate-900/70 p-4">
              <div className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-500">Position snapshot</div>
              <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2">
                {detailRows.map(([label, value]) => (
                  <div key={label} className="rounded-2xl border border-slate-800 bg-slate-950/70 px-4 py-3">
                    <div className="text-xs uppercase tracking-wide text-slate-500">{label}</div>
                    <div className="mt-2 text-sm text-slate-200">{value}</div>
                  </div>
                ))}
              </div>
            </div>

            <InspectJsonCard title="Signal snapshot" value={inspect.signalSnapshot ?? {}} />
            <InspectJsonCard title="Sizing math" value={inspect.sizing ?? {}} />

            <div className="mt-6 rounded-2xl border border-slate-800 bg-slate-900/70 p-4">
              <div className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-500">Timeframe alignment</div>
              {inspect.timeframeAlignment?.note ? <p className="mt-3 text-sm leading-6 text-slate-400">{inspect.timeframeAlignment.note}</p> : null}
              <div className="mt-4 overflow-x-auto">
                <table className="w-full min-w-[420px] text-sm">
                  <thead>
                    <tr className="border-b border-slate-800 text-left text-xs uppercase tracking-wide text-slate-500">
                      <th className="pb-3 pr-4">Timeframe</th>
                      <th className="pb-3 pr-4">Status</th>
                      <th className="pb-3">Why</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(inspect.timeframeAlignment?.items ?? []).map((item) => (
                      <tr key={item.timeframe} className="border-b border-slate-900/80 text-slate-300">
                        <td className="py-3 pr-4 font-semibold text-white">{item.timeframe}</td>
                        <td className="py-3 pr-4">{item.status}</td>
                        <td className="py-3">{item.reason}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>

            <InspectJsonCard title="Exit plan" value={inspect.exitPlan ?? {}} />
            <InspectJsonCard title="Latest evaluation" value={inspect.latestEvaluation ?? {}} />

            <div className="mt-6 rounded-2xl border border-slate-800 bg-slate-900/70 p-4">
              <div className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-500">Lifecycle timeline</div>
              <div className="mt-4 space-y-3">
                {(inspect.lifecycle ?? []).length === 0 ? (
                  <div className="rounded-2xl border border-slate-800 bg-slate-950/70 px-4 py-3 text-sm text-slate-400">No lifecycle events were stored for this position yet.</div>
                ) : (inspect.lifecycle ?? []).map((event, index) => (
                  <div key={`${event.eventType}-${event.eventTime ?? index}`} className="rounded-2xl border border-slate-800 bg-slate-950/70 px-4 py-3">
                    <div className="flex flex-wrap items-center gap-2">
                      <ToneBadge tone="info">{event.eventType}</ToneBadge>
                      <ToneBadge tone="muted">{event.status}</ToneBadge>
                      <span className="text-xs uppercase tracking-wide text-slate-500">{formatTimestamp(event.eventTime)}</span>
                    </div>
                    {event.message ? <p className="mt-3 text-sm text-slate-300">{event.message}</p> : null}
                    <pre className="mt-3 overflow-x-auto whitespace-pre-wrap break-words rounded-2xl border border-slate-800 bg-slate-950/70 p-3 text-xs text-slate-400">{formatJson(event.payload ?? {})}</pre>
                  </div>
                ))}
              </div>
            </div>

            <InspectJsonCard title="Raw context" value={inspect.rawContext ?? {}} />
          </>
        ) : null}
      </aside>
    </div>
  )
}



async function getPositionInspect(assetClass: 'stock' | 'crypto', symbol: string): Promise<PositionInspectRecord> {
  const params = new URLSearchParams({ asset_class: assetClass, symbol })
  const response = await fetch(`/api/positions/inspect?${params.toString()}`)
  if (!response.ok) {
    const detail = await response.text()
    throw new Error(detail || `Failed to fetch inspect payload: ${response.status}`)
  }
  return response.json()
}

function humanizeKey(value: string) {
  return value
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .replace(/_/g, ' ')
    .split(' ')
    .map((part) => (part ? part[0].toUpperCase() + part.slice(1) : part))
    .join(' ')
}

function renderInspectValue(label: string, value: unknown) {
  if (value == null) return '—'
  if (typeof value === 'number') {
    if (label.toLowerCase().includes('pct')) return formatPercent(value)
    if (label.toLowerCase().includes('price') || label.toLowerCase().includes('value') || label.toLowerCase().includes('pnl') || label.toLowerCase().includes('basis')) {
      return formatMoney(value)
    }
    return String(value)
  }
  if (typeof value === 'boolean') return value ? 'True' : 'False'
  if (typeof value === 'string' && (label.toLowerCase().includes('time') || label.toLowerCase().includes('atutc'))) return formatTimestamp(value)
  if (typeof value === 'object') return formatJson(value)
  return String(value)
}

function InspectJsonCard({ title, value }: { title: string; value: unknown }) {
  return (
    <div className="mt-6 rounded-2xl border border-slate-800 bg-slate-900/70 p-4">
      <div className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-500">{title}</div>
      <pre className="mt-3 overflow-x-auto whitespace-pre-wrap break-words rounded-2xl border border-slate-800 bg-slate-950/70 p-4 text-xs text-slate-300">{formatJson(value ?? {})}</pre>
    </div>
  )
}

function formatJson(value: unknown): string {
  try {
    return JSON.stringify(value ?? {}, null, 2)
  } catch {
    return String(value ?? '—')
  }
}

function CryptoPositionsTable({ positions, onSelect }: { positions: CryptoPosition[]; onSelect: (position: CryptoPosition) => void }) {
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
            <th className="pb-3 pr-4">P&amp;L %</th>
            <th className="pb-3">Inspect</th>
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
              <td className={`${position.pnlPercent >= 0 ? 'py-3 pr-4 text-emerald-300' : 'py-3 pr-4 text-rose-300'}`}>{formatPercent(position.pnlPercent)}</td>
              <td className="py-3">
                <button
                  type="button"
                  onClick={() => onSelect(position)}
                  className="rounded-full border border-slate-700 bg-slate-950/70 px-3 py-1 text-xs font-semibold uppercase tracking-wide text-slate-200 transition hover:border-cyan-600 hover:text-cyan-200"
                >
                  Inspect
                </button>
              </td>
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
