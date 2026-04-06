import { useMemo, useState, type ReactNode } from 'react'
import { keepPreviousData, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Activity,
  ArrowRightLeft,
  Bitcoin,
  CalendarClock,
  ChevronDown,
  ChevronUp,
  CircleAlert,
  Clock3,
  Layers3,
  ShieldCheck,
  Target,
  TrendingUp,
  Wallet,
  X,
} from 'lucide-react'

import { api } from '@/lib/api'
import {
  DetailRow,
  EmptyState,
  PageHero,
  SectionCard,
  StatusPill,
  ToneBadge,
  getStatusMeta,
  type Tone,
} from '@/components/operator-ui'
import type {
  BotStatus,
  CryptoLedger,
  OrderIntentRecord,
  PositionInspectRecord,
  PositionInspectTimeframeItem,
  PositionInspectTimelineEvent,
  StockAccount,
  TradeHistoryEntry,
  WatchlistExitReadinessSnapshot,
  WatchlistSymbolRecord,
} from '@/types'

const ET_DATE_TIME = new Intl.DateTimeFormat('en-US', {
  timeZone: 'America/New_York',
  month: 'short',
  day: '2-digit',
  year: 'numeric',
  hour: 'numeric',
  minute: '2-digit',
  hour12: false,
})

const ET_TIME = new Intl.DateTimeFormat('en-US', {
  timeZone: 'America/New_York',
  hour: 'numeric',
  minute: '2-digit',
  hour12: false,
})

function formatMoney(value: number) {
  return `$${value.toFixed(2)}`
}

function formatPrice(value: number) {
  const absolute = Math.abs(value)
  if (absolute === 0) return '$0.00'
  if (absolute < 1) return `$${value.toFixed(5)}`
  if (absolute < 100) return `$${value.toFixed(4)}`
  return `$${value.toFixed(2)}`
}

function formatPercent(value: number) {
  const prefix = value >= 0 ? '+' : ''
  return `${prefix}${value.toFixed(2)}%`
}

function formatTimestamp(value?: string | null) {
  if (!value) return '—'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return value
  return `${ET_DATE_TIME.format(parsed)} ET`
}

function formatCompactDateTime(value?: string | null) {
  return formatTimestamp(value)
}

function formatTimeOnly(value?: string | null) {
  if (!value) return '—'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return value
  return `${ET_TIME.format(parsed)} ET`
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

type UnifiedPositionRow = {
  assetClass: 'stock' | 'crypto'
  symbol: string
  displaySymbol: string
  quantity: number
  quantityUnit: 'shares' | 'units'
  avgPrice: number
  currentPrice: number
  marketValue: number
  pnl: number
  pnlPercent: number
  inspectSymbol: string
  inspectAssetClass: 'stock' | 'crypto'
  sourceStatus: 'aligned' | 'broker_only' | 'db_only' | 'ledger'
  sourceDetail: string
  entryTime?: string | null
}

type UnifiedPositionsResponse = {
  rows: UnifiedPositionRow[]
  summary: {
    totalCount: number
    stockCount: number
    cryptoCount: number
    stockDriftCount: number
    alignedStockCount: number
  }
}

type InspectTarget = {
  assetClass: 'stock' | 'crypto'
  symbol: string
  fallbackTitle: string
}

type StatCardRow = {
  label: string
  value: string
  tone?: Tone
}

type LabeledStat = {
  label: string
  value: string
  tone?: Tone
}

type DerivedInspectSections = {
  overview: StatCardRow[]
  strategyRows: LabeledStat[]
  sizingRows: LabeledStat[]
  exitRows: LabeledStat[]
  exitVerdictRows: LabeledStat[]
  exitVerdictReason: string | null
  exitNextTriggerRows: LabeledStat[]
  exitHealthRows: LabeledStat[]
  executionRows: LabeledStat[]
  exitStateHistory: Array<{ time: string | null; label: string; detail: string | null }>
  rawSections: Array<{ label: string; value: unknown }>
}

async function getUnifiedPositions(): Promise<UnifiedPositionsResponse> {
  const response = await fetch('/api/positions/unified')
  if (!response.ok) {
    throw new Error(`Failed to fetch unified positions: ${response.status}`)
  }
  return response.json()
}

export default function Positions() {
  const queryClient = useQueryClient()
  const [selectedInspectTarget, setSelectedInspectTarget] = useState<InspectTarget | null>(null)
  const [positionFilter, setPositionFilter] = useState<'all' | 'stock' | 'crypto'>('all')

  const { data: unifiedPositions } = useQuery<UnifiedPositionsResponse>({
    queryKey: ['unifiedPositions'],
    queryFn: getUnifiedPositions,
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

  const inspectQuery = useQuery<PositionInspectRecord>({
    queryKey: ['positionInspect', selectedInspectTarget?.assetClass ?? null, selectedInspectTarget?.symbol ?? null],
    queryFn: () => {
      if (!selectedInspectTarget) {
        throw new Error('No inspect target selected.')
      }
      return getPositionInspect(selectedInspectTarget.assetClass, selectedInspectTarget.symbol)
    },
    enabled: Boolean(selectedInspectTarget),
    staleTime: 30_000,
    gcTime: 5 * 60_000,
    placeholderData: keepPreviousData,
  })

  const summary = useMemo(() => {
    const rows = unifiedPositions?.rows ?? []
    const stockRows = rows.filter((row) => row.assetClass === 'stock')
    const cryptoRows = rows.filter((row) => row.assetClass === 'crypto')
    const stockOpenPnl = stockAccount?.unrealizedPnL ?? stockRows.reduce((sum, row) => sum + row.pnl, 0)
    const cryptoOpenPnl = cryptoLedger?.netPnL ?? cryptoLedger?.totalPnL ?? cryptoRows.reduce((sum, row) => sum + row.pnl, 0)

    return {
      totalPositions: unifiedPositions?.summary.totalCount ?? rows.length,
      openPnl: stockOpenPnl + cryptoOpenPnl,
      stockExposure: stockRows.reduce((sum, row) => sum + row.marketValue, 0),
      cryptoExposure: cryptoRows.reduce((sum, row) => sum + row.marketValue, 0),
      expiringSoon: (stockExitReadiness?.summary.expiringWithinWindowCount ?? 0) + (cryptoExitReadiness?.summary.expiringWithinWindowCount ?? 0),
      protectivePending: (stockExitReadiness?.summary.protectiveExitPendingCount ?? 0) + (cryptoExitReadiness?.summary.protectiveExitPendingCount ?? 0),
      stockCount: unifiedPositions?.summary.stockCount ?? stockRows.length,
      cryptoCount: unifiedPositions?.summary.cryptoCount ?? cryptoRows.length,
      stockDriftCount: unifiedPositions?.summary.stockDriftCount ?? stockRows.filter((row) => row.sourceStatus !== 'aligned').length,
      alignedStockCount: unifiedPositions?.summary.alignedStockCount ?? stockRows.filter((row) => row.sourceStatus === 'aligned').length,
    }
  }, [cryptoExitReadiness?.summary.expiringWithinWindowCount, cryptoExitReadiness?.summary.protectiveExitPendingCount, cryptoLedger, stockAccount, stockExitReadiness?.summary.expiringWithinWindowCount, stockExitReadiness?.summary.protectiveExitPendingCount, unifiedPositions])

  const filteredPositions = useMemo(() => {
    const rows = unifiedPositions?.rows ?? []
    if (positionFilter === 'all') return rows
    return rows.filter((row) => row.assetClass === positionFilter)
  }, [positionFilter, unifiedPositions])

  const actionRows = useMemo(() => {
    const rows: ActionRow[] = []
    collectActionRows(rows, 'Stocks', stockExitReadiness)
    collectActionRows(rows, 'Crypto', cryptoExitReadiness)
    return rows.slice(0, 10)
  }, [cryptoExitReadiness, stockExitReadiness])

  function prefetchInspect(target: InspectTarget) {
    void queryClient.prefetchQuery({
      queryKey: ['positionInspect', target.assetClass, target.symbol],
      queryFn: () => getPositionInspect(target.assetClass, target.symbol),
      staleTime: 30_000,
    })
  }

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
              <DetailRow label="Open split" value={`${summary.stockCount} stock · ${summary.cryptoCount} crypto`} tone="muted" />
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
              title="Open positions"
              eyebrow="Unified inventory view"
              icon={<Layers3 className="h-4 w-4 text-cyan-300" />}
              actions={
                <div className="flex flex-wrap gap-2">
                  <StatusPill tone={summary.stockDriftCount > 0 ? 'warn' : 'good'} label={summary.stockDriftCount > 0 ? `${summary.stockDriftCount} stock drift` : 'Stock mirror aligned'} />
                  <StatusPill tone="muted" label={`${summary.totalPositions} total`} />
                </div>
              }
            >
              <div className="mb-4 rounded-2xl border border-slate-800 bg-slate-950/60 px-4 py-3 text-sm text-slate-400">
                <div className="font-medium text-slate-200">One table, one cockpit</div>
                <div className="mt-2">Stocks and crypto now share one operational inventory view. Stock rows still show reconciliation health from broker versus DB mirror, but drift is surfaced as a badge instead of a second competing table.</div>
              </div>
              <UnifiedPositionsTable
                positions={filteredPositions}
                positionFilter={positionFilter}
                onFilterChange={setPositionFilter}
                onHover={prefetchInspect}
                onSelect={(position) => {
                  const target = {
                    assetClass: position.inspectAssetClass,
                    symbol: position.inspectSymbol,
                    fallbackTitle: position.displaySymbol,
                  } satisfies InspectTarget
                  prefetchInspect(target)
                  setSelectedInspectTarget(target)
                }}
              />
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
        inspect={selectedInspectTarget ? inspectQuery.data ?? null : null}
        loading={Boolean(selectedInspectTarget) && (inspectQuery.isLoading || inspectQuery.isFetching)}
        error={
          selectedInspectTarget && inspectQuery.isError
            ? inspectQuery.error instanceof Error
              ? inspectQuery.error.message
              : 'Failed to load inspect payload.'
            : null
        }
        fallbackTitle={selectedInspectTarget?.fallbackTitle ?? null}
        onClose={() => setSelectedInspectTarget(null)}
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

function UnifiedPositionsTable({
  positions,
  positionFilter,
  onFilterChange,
  onHover,
  onSelect,
}: {
  positions: UnifiedPositionRow[]
  positionFilter: 'all' | 'stock' | 'crypto'
  onFilterChange: (value: 'all' | 'stock' | 'crypto') => void
  onHover: (target: InspectTarget) => void
  onSelect: (position: UnifiedPositionRow) => void
}) {
  const sorted = [...positions].sort((a, b) => b.marketValue - a.marketValue)

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        {([['all', 'All'], ['stock', 'Stocks'], ['crypto', 'Crypto']] as const).map(([value, label]) => (
          <button
            key={value}
            type="button"
            onClick={() => onFilterChange(value)}
            className={`rounded-full border px-3 py-1 text-xs font-semibold uppercase tracking-wide transition ${positionFilter === value ? 'border-cyan-500 bg-cyan-500/15 text-cyan-200' : 'border-slate-700 bg-slate-950/60 text-slate-300 hover:border-cyan-700 hover:text-cyan-200'}`}
          >
            {label}
          </button>
        ))}
      </div>
      {sorted.length === 0 ? <EmptyState message="No active positions match the selected filter." /> : null}
      {sorted.length > 0 ? (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[1120px] text-sm">
            <thead>
              <tr className="border-b border-slate-800 text-left text-xs uppercase tracking-wide text-slate-500">
                <th className="pb-3 pr-4">Asset</th>
                <th className="pb-3 pr-4">Symbol</th>
                <th className="pb-3 pr-4">Quantity</th>
                <th className="pb-3 pr-4">Avg</th>
                <th className="pb-3 pr-4">Current</th>
                <th className="pb-3 pr-4">Market value</th>
                <th className="pb-3 pr-4">P&amp;L</th>
                <th className="pb-3 pr-4">P&amp;L %</th>
                <th className="pb-3 pr-4">Source</th>
                <th className="pb-3">Inspect</th>
              </tr>
            </thead>
            <tbody>
              {sorted.map((position) => {
                const target = {
                  assetClass: position.inspectAssetClass,
                  symbol: position.inspectSymbol,
                  fallbackTitle: position.displaySymbol,
                } satisfies InspectTarget
                return (
                  <tr key={`${position.assetClass}-${position.symbol}`} className="border-b border-slate-900/80 text-slate-300">
                    <td className="py-3 pr-4">
                      <ToneBadge tone={position.assetClass === 'stock' ? 'info' : 'good'}>{position.assetClass === 'stock' ? 'Stock' : 'Crypto'}</ToneBadge>
                    </td>
                    <td className="py-3 pr-4 font-semibold text-white">
                      <div>{position.displaySymbol}</div>
                      {position.entryTime ? <div className="mt-1 text-xs text-slate-500">Entry {formatTimestamp(position.entryTime)}</div> : null}
                    </td>
                    <td className="py-3 pr-4">{position.quantity.toFixed(position.assetClass === 'crypto' ? 6 : 0)} {position.quantityUnit}</td>
                    <td className="py-3 pr-4">{formatMoney(position.avgPrice)}</td>
                    <td className="py-3 pr-4">{formatMoney(position.currentPrice)}</td>
                    <td className="py-3 pr-4">{formatMoney(position.marketValue)}</td>
                    <td className={`py-3 pr-4 ${position.pnl >= 0 ? 'text-emerald-300' : 'text-rose-300'}`}>{formatMoney(position.pnl)}</td>
                    <td className={`${position.pnlPercent >= 0 ? 'py-3 pr-4 text-emerald-300' : 'py-3 pr-4 text-rose-300'}`}>{formatPercent(position.pnlPercent)}</td>
                    <td className="py-3 pr-4">
                      <div className="flex flex-col gap-2">
                        <ToneBadge tone={position.sourceStatus === 'aligned' || position.sourceStatus === 'ledger' ? 'good' : 'warn'}>{position.sourceStatus.replace('_', ' ')}</ToneBadge>
                        <span className="text-xs text-slate-500">{position.sourceDetail}</span>
                      </div>
                    </td>
                    <td className="py-3">
                      <button
                        type="button"
                        onMouseEnter={() => onHover(target)}
                        onFocus={() => onHover(target)}
                        onClick={() => onSelect(position)}
                        className="rounded-full border border-slate-700 bg-slate-950/70 px-3 py-1 text-xs font-semibold uppercase tracking-wide text-slate-200 transition hover:border-cyan-600 hover:text-cyan-200"
                      >
                        Inspect
                      </button>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      ) : null}
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
  const [showRaw, setShowRaw] = useState(false)
  const [timelineExpanded, setTimelineExpanded] = useState<Record<string, boolean>>({})

  if (!inspect && !loading && !error && !fallbackTitle) return null

  const sections = inspect ? deriveInspectSections(inspect) : null

  function toggleTimeline(key: string) {
    setTimelineExpanded((current) => ({ ...current, [key]: !current[key] }))
  }

  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-slate-950/80">
      <button type="button" aria-label="Close position drawer" className="absolute inset-0 cursor-default" onClick={onClose} />
      <aside className="relative h-full w-full max-w-3xl overflow-y-auto border-l border-slate-800 bg-slate-950 p-6 shadow-2xl shadow-black/60">
        <div className="flex items-start justify-between gap-4">
          <div>
            <div className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-500">Position inspect</div>
            <h2 className="mt-1 text-2xl font-semibold text-white">{inspect?.displaySymbol ?? fallbackTitle ?? 'Inspect'}</h2>
            <div className="mt-3 flex flex-wrap gap-2">
              <ToneBadge tone="info">{inspect?.assetClass ?? 'loading'}</ToneBadge>
              {inspect?.inspectSource ? <ToneBadge tone="muted">{inspect.inspectSource}</ToneBadge> : null}
              {inspect?.latestEvaluation?.state ? <ToneBadge tone={getStatusMeta(String(inspect.latestEvaluation.state)).tone}>{String(inspect.latestEvaluation.state)}</ToneBadge> : null}
            </div>
          </div>
          <button type="button" onClick={onClose} className="rounded-full border border-slate-700 p-2 text-slate-300 transition hover:border-cyan-600 hover:text-cyan-200">
            <X className="h-4 w-4" />
          </button>
        </div>

        {loading ? <DrawerLoadingSkeleton /> : null}
        {error ? <div className="mt-6 rounded-2xl border border-rose-900/70 bg-rose-950/30 p-4 text-sm text-rose-200">{error}</div> : null}

        {inspect && sections ? (
          <>
            <div className="mt-6 grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3">
              {sections.overview.map((row) => (
                <MetricTile key={row.label} label={row.label} value={row.value} tone={row.tone ?? 'muted'} />
              ))}
            </div>

            <StructuredInspectCard title="Signal snapshot" eyebrow="Strategy" icon={<Target className="h-4 w-4 text-cyan-300" />}>
              <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
                <KeyValueList rows={sections.strategyRows} />
                <div className="rounded-2xl border border-slate-800 bg-slate-950/70 p-4 text-sm text-slate-300">
                  <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.18em] text-slate-500">
                    <ArrowRightLeft className="h-4 w-4 text-cyan-300" />
                    Entry source
                  </div>
                  <p className="mt-3 leading-6 text-slate-400">
                    This panel keeps the execution breadcrumbs visible without spraying raw JSON across the drawer. The raw payload still exists below for deep-debug spelunking.
                  </p>
                </div>
              </div>
            </StructuredInspectCard>

            <StructuredInspectCard title="Sizing math" eyebrow="Fill and sizing" icon={<TrendingUp className="h-4 w-4 text-cyan-300" />}>
              <KeyValueGrid rows={sections.sizingRows} />
            </StructuredInspectCard>

            <StructuredInspectCard title="Timeframe alignment" eyebrow="Confirmation map" icon={<ShieldCheck className="h-4 w-4 text-cyan-300" />}>
              {inspect.timeframeAlignment?.note ? (
                <div className="mb-4 rounded-2xl border border-slate-800 bg-slate-950/70 px-4 py-3 text-sm leading-6 text-slate-400">
                  {inspect.timeframeAlignment.note}
                </div>
              ) : null}
              <TimeframeAlignmentTable items={inspect.timeframeAlignment?.items ?? []} />
            </StructuredInspectCard>

            <StructuredInspectCard title="Exit plan" eyebrow="Risk rails" icon={<CircleAlert className="h-4 w-4 text-cyan-300" />}>
              <KeyValueGrid rows={sections.exitRows} />
            </StructuredInspectCard>

            <StructuredInspectCard title="Current exit worker verdict" eyebrow="Primary decision engine" icon={<CalendarClock className="h-4 w-4 text-cyan-300" />}>
              <div className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)]">
                <KeyValueList rows={sections.exitVerdictRows} />
                <div className="rounded-2xl border border-slate-800 bg-slate-950/70 p-4">
                  <div className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-500">Why not exiting yet</div>
                  <p className="mt-3 text-sm leading-6 text-slate-300">{sections.exitVerdictReason ?? 'No exit-worker rationale stored yet.'}</p>
                </div>
              </div>
            </StructuredInspectCard>

            <StructuredInspectCard title="Next trigger and phase" eyebrow="Tier 1 operator telemetry" icon={<ArrowRightLeft className="h-4 w-4 text-cyan-300" />}>
              <KeyValueGrid rows={sections.exitNextTriggerRows} />
            </StructuredInspectCard>

            <StructuredInspectCard title="Structure, risk, and readiness" eyebrow="Tier 2 → Tier 3 telemetry" icon={<ShieldCheck className="h-4 w-4 text-cyan-300" />}>
              <KeyValueGrid rows={sections.exitHealthRows} />
            </StructuredInspectCard>

            <StructuredInspectCard title="Execution status" eyebrow="Logic versus order state" icon={<Clock3 className="h-4 w-4 text-cyan-300" />}>
              <div className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)]">
                <KeyValueList rows={sections.executionRows} />
                <ExitStateHistory items={sections.exitStateHistory} />
              </div>
            </StructuredInspectCard>

            <StructuredInspectCard title="Lifecycle timeline" eyebrow="Intent → broker → fill" icon={<Clock3 className="h-4 w-4 text-cyan-300" />}>
              <LifecycleTimeline events={inspect.lifecycle ?? []} expandedState={timelineExpanded} onToggle={toggleTimeline} />
            </StructuredInspectCard>

            <StructuredInspectCard title="Raw debug" eyebrow="Folded away on purpose" icon={<Layers3 className="h-4 w-4 text-cyan-300" />}>
              <div className="rounded-2xl border border-slate-800 bg-slate-950/70 p-4">
                <button
                  type="button"
                  onClick={() => setShowRaw((current) => !current)}
                  className="flex w-full items-center justify-between gap-3 rounded-2xl border border-slate-800 bg-slate-900/70 px-4 py-3 text-left transition hover:border-cyan-700"
                >
                  <div>
                    <div className="text-sm font-medium text-slate-200">Raw payload sections</div>
                    <div className="mt-1 text-xs text-slate-500">Open this only when you need the wiring diagram instead of the dashboard.</div>
                  </div>
                  {showRaw ? <ChevronUp className="h-4 w-4 text-slate-400" /> : <ChevronDown className="h-4 w-4 text-slate-400" />}
                </button>
                {showRaw ? (
                  <div className="mt-4 space-y-4">
                    {sections.rawSections.map((section) => (
                      <InspectJsonCard key={section.label} title={section.label} value={section.value} compact />
                    ))}
                  </div>
                ) : null}
              </div>
            </StructuredInspectCard>
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
    .replace(/[_-]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .split(' ')
    .map((part) => (part ? part[0].toUpperCase() + part.slice(1).toLowerCase() : part))
    .join(' ')
}

function displayValue(label: string, value: unknown): string {
  if (value == null || value === '') return '—'
  if (typeof value === 'number') {
    const normalized = label.toLowerCase()
    if (normalized.includes('pct') || normalized.includes('percent')) return formatPercent(value)
    if (
      normalized.includes('price') ||
      normalized.includes('target') ||
      normalized.includes('stop') ||
      normalized.includes('trigger') ||
      normalized.includes('breakout') ||
      normalized.includes('bounce')
    ) {
      return formatPrice(value)
    }
    if (
      normalized.includes('value') ||
      normalized.includes('pnl') ||
      normalized.includes('basis') ||
      normalized.includes('notional')
    ) {
      return formatMoney(value)
    }
    if (normalized.includes('hours')) {
      return `${value}`
    }
    return `${value}`
  }
  if (typeof value === 'boolean') return value ? 'Yes' : 'No'
  if (typeof value === 'string') {
    if (isTimestampLike(label, value)) return formatCompactDateTime(value)

    const normalized = label.toLowerCase()
    const shouldHumanize =
      normalized.includes('strategy') ||
      normalized.includes('source') ||
      normalized.includes('template') ||
      normalized.includes('status') ||
      normalized.includes('event') ||
      normalized.includes('state') ||
      normalized.includes('bias') ||
      normalized.includes('trigger')

    if (shouldHumanize) {
      return humanizeKey(value)
    }

    return value
  }
  if (Array.isArray(value)) return value.length ? value.map((item) => displayValue(label, item)).join(' · ') : '—'
  if (typeof value === 'object') return formatJson(value)
  return String(value)
}

function isTimestampLike(label: string, value: string) {
  const normalized = label.toLowerCase()
  return normalized.includes('time') || normalized.includes('utc') || normalized.includes('date') || normalized.includes('expires') || normalized.includes('syncedat') || normalized.includes('observedat') || (!Number.isNaN(new Date(value).getTime()) && /\d{4}-\d{2}-\d{2}/.test(value))
}

function deriveInspectSections(inspect: PositionInspectRecord): DerivedInspectSections {
  const snapshot = inspect.positionSnapshot ?? {}
  const signal = inspect.signalSnapshot ?? {}
  const entryReasoning = asRecord(signal.entryReasoning)
  const watchlist = Object.keys(asRecord(entryReasoning.watchlist)).length > 0 ? asRecord(entryReasoning.watchlist) : asRecord(signal)
  const reconciliation = asRecord(entryReasoning.reconciliation)
  const brokerSnapshot = asRecord(entryReasoning.brokerSnapshot)
  const signalDetails = asRecord(signal.details)
  const sizing = inspect.sizing ?? {}
  const exitPlan = inspect.exitPlan ?? {}
  const latestEvaluation = inspect.latestEvaluation ?? {}
  const exitWorker = asRecord(inspect.exitWorker)
  const signalLifecycleState = asText(signal.lifecycleState) ?? asText(watchlist.lifecycleState) ?? asText(entryReasoning.lifecycleState) ?? asText(signal.monitoringStatus)
  const signalLifecycleNote = asText(signal.lifecycleNote) ?? asText(watchlist.lifecycleNote) ?? asText(entryReasoning.lifecycleNote)

  const overview: StatCardRow[] = [
    { label: 'Quantity', value: displayValue('quantity', snapshot.quantity), tone: 'info' },
    { label: 'Avg entry', value: displayValue('avgEntryPrice', snapshot.avgEntryPrice), tone: 'muted' },
    { label: 'Current price', value: displayValue('currentPrice', snapshot.currentPrice), tone: 'muted' },
    { label: 'Market value', value: displayValue('marketValue', snapshot.marketValue), tone: 'muted' },
    { label: 'Unrealized P&L', value: displayValue('unrealizedPnl', snapshot.unrealizedPnl), tone: toneFromPnl(asNumber(snapshot.unrealizedPnl) ?? 0) },
    { label: 'Unrealized P&L %', value: displayValue('unrealizedPnlPct', snapshot.unrealizedPnlPct), tone: toneFromPnl(asNumber(snapshot.unrealizedPnlPct) ?? 0) },
    { label: 'Entry time', value: displayValue('entryTimeUtc', snapshot.entryTimeUtc), tone: 'muted' },
    { label: 'Account ID', value: displayValue('accountId', snapshot.accountId), tone: 'muted' },
    { label: 'Position open', value: displayValue('isOpen', snapshot.isOpen), tone: 'good' },
  ]

  const strategyRows: LabeledStat[] = [
    { label: 'Strategy', value: displayValue('strategy', signal.strategy), tone: 'info' },
    { label: 'Execution source', value: displayValue('executionSource', signal.executionSource), tone: 'muted' },
    { label: 'Setup template', value: displayValue('setupTemplate', watchlist.setupTemplate), tone: 'muted' },
    { label: 'Exit template', value: displayValue('exitTemplate', watchlist.exitTemplate ?? inspect.exitPlan?.template), tone: 'muted' },
    { label: 'Max hold hours', value: displayValue('maxHoldHours', watchlist.maxHoldHours ?? inspect.exitPlan?.maxHoldHours), tone: 'muted' },
    { label: 'Market regime', value: displayValue('marketRegime', signal.marketRegime), tone: 'muted' },
    { label: 'Trade direction', value: displayValue('tradeDirection', signal.tradeDirection), tone: 'muted' },
    { label: 'Bias', value: displayValue('bias', signal.bias), tone: 'muted' },
    { label: 'Tier', value: displayValue('tier', signal.tier), tone: 'muted' },
    { label: 'Priority rank', value: displayValue('priorityRank', signal.priorityRank), tone: 'muted' },
    { label: 'Risk flags', value: displayValue('riskFlags', signal.riskFlags), tone: 'warn' },
    { label: 'Monitoring status', value: displayValue('monitoringStatus', signal.monitoringStatus), tone: 'muted' },
    { label: 'Lifecycle state', value: displayValue('lifecycleState', signalLifecycleState), tone: 'muted' },
    { label: 'Lifecycle note', value: displayValue('lifecycleNote', signalLifecycleNote), tone: 'warn' },
    { label: 'Cooldown active', value: displayValue('cooldownActive', signal.cooldownActive), tone: 'warn' },
    { label: 'Re-entry blocked until', value: displayValue('reentryBlockedUntilUtc', signal.reentryBlockedUntilUtc), tone: 'warn' },
    { label: 'Last exit at', value: displayValue('lastExitAtUtc', signal.lastExitAtUtc), tone: 'muted' },
    { label: 'Seed intent status', value: displayValue('seedIntentStatus', entryReasoning.seedIntentStatus), tone: 'muted' },
    { label: 'Sync source', value: displayValue('syncSource', entryReasoning.syncSource), tone: 'muted' },
    { label: 'Broker shares', value: displayValue('shares', brokerSnapshot.shares), tone: 'muted' },
    { label: 'Broker avg price', value: displayValue('avgPrice', brokerSnapshot.avgPrice), tone: 'muted' },
    { label: 'Reconciliation event', value: displayValue('event', reconciliation.event), tone: 'warn' },
    { label: 'Observed at', value: displayValue('observedAtUtc', reconciliation.observedAtUtc), tone: 'muted' },
    { label: 'Trigger level', value: displayValue('triggerLevel', signalDetails.triggerLevel), tone: 'info' },
    { label: 'Breakout level', value: displayValue('breakoutLevel', signalDetails.breakoutLevel), tone: 'info' },
    { label: 'Bounce floor', value: displayValue('bounceFloor', signalDetails.bounceFloor), tone: 'info' },
    { label: 'Recent high', value: displayValue('recentHigh', signalDetails.recentHigh), tone: 'muted' },
    { label: 'Recent low', value: displayValue('recentLow', signalDetails.recentLow), tone: 'muted' },
    { label: 'Continuity OK', value: displayValue('continuityOk', signalDetails.continuityOk), tone: 'muted' },
    { label: 'Continuity gap seconds', value: displayValue('continuityGapSeconds', signalDetails.continuityGapSeconds), tone: 'muted' },
  ]

  const requestedQuantity = asNumber(sizing.requestedQuantity)
  const filledQuantity = asNumber(sizing.filledQuantity)
  const requestedPrice = asNumber(sizing.requestedPrice)
  const averageFillPrice = asNumber(sizing.avgFillPrice)
  const requestedNotional = requestedQuantity != null && requestedPrice != null ? requestedQuantity * requestedPrice : null
  const actualNotional = filledQuantity != null && averageFillPrice != null ? filledQuantity * averageFillPrice : null
  const slippage = requestedPrice != null && averageFillPrice != null ? averageFillPrice - requestedPrice : null

  const sizingRows: LabeledStat[] = [
    { label: 'Requested quantity', value: displayValue('requestedQuantity', requestedQuantity), tone: 'info' },
    { label: 'Filled quantity', value: displayValue('filledQuantity', filledQuantity), tone: 'good' },
    { label: 'Requested price', value: displayValue('requestedPrice', requestedPrice), tone: 'muted' },
    { label: 'Average fill price', value: displayValue('avgFillPrice', averageFillPrice), tone: 'muted' },
    { label: 'Requested notional', value: displayValue('requestedNotional', requestedNotional), tone: 'muted' },
    { label: 'Actual fill notional', value: displayValue('actualNotional', actualNotional), tone: 'muted' },
    { label: 'Slippage', value: displayValue('slippage', slippage), tone: toneFromPnl(-(slippage ?? 0)) },
    { label: 'Estimated value', value: displayValue('estimatedValue', sizing.estimatedValue), tone: 'muted' },
    { label: 'Position %', value: displayValue('positionPct', sizing.positionPct), tone: 'muted' },
    { label: 'Display pair', value: displayValue('displayPair', sizing.displayPair), tone: 'muted' },
    { label: 'OHLCV pair', value: displayValue('ohlcvPair', sizing.ohlcvPair), tone: 'muted' },
    { label: 'Account ID', value: displayValue('accountId', sizing.accountId), tone: 'muted' },
  ]

  const expectedExitThresholds = asRecord(exitPlan.expectedExitThresholds)
  const exitRows: LabeledStat[] = [
    { label: 'Exit template', value: displayValue('template', exitPlan.template), tone: 'info' },
    { label: 'Stop loss', value: displayValue('stopLoss', exitPlan.stopLoss), tone: 'danger' },
    { label: 'Profit target', value: displayValue('profitTarget', exitPlan.profitTarget), tone: 'good' },
    { label: 'Trailing stop', value: displayValue('trailingStop', exitPlan.trailingStop), tone: 'warn' },
    { label: 'Stop distance', value: displayValue('stopDistance', exitPlan.stopDistance), tone: 'warn' },
    { label: 'Target distance', value: displayValue('targetDistance', exitPlan.targetDistance), tone: 'good' },
    { label: 'Trailing distance', value: displayValue('trailingDistance', exitPlan.trailingDistance), tone: 'warn' },
    { label: 'Trigger level', value: displayValue('triggerLevel', expectedExitThresholds.triggerLevel ?? exitPlan.triggerLevel), tone: 'info' },
    { label: 'Breakout level', value: displayValue('breakoutLevel', expectedExitThresholds.breakoutLevel ?? exitPlan.breakoutLevel), tone: 'info' },
    { label: 'Bounce floor', value: displayValue('bounceFloor', expectedExitThresholds.bounceFloor ?? exitPlan.bounceFloor), tone: 'info' },
    { label: 'Peak price', value: displayValue('peakPrice', exitPlan.peakPrice), tone: 'muted' },
    { label: 'Exit trigger', value: displayValue('tradeExitTrigger', exitPlan.tradeExitTrigger), tone: 'muted' },
  ]

  const exitState = asText(exitWorker.logicSummary) ?? asText(latestEvaluation.state)
  const exitVerdictRows: LabeledStat[] = [
    { label: 'Worker', value: displayValue('worker', exitWorker.worker ?? 'Exit Worker'), tone: 'info' },
    { label: 'State', value: displayValue('logicSummary', exitState), tone: getStatusMeta(String(exitWorker.logicState ?? latestEvaluation.state ?? '')).tone },
    { label: 'Worker time', value: displayValue('evaluatedAtUtc', exitWorker.evaluatedAtUtc ?? latestEvaluation.evaluatedAtUtc), tone: 'muted' },
    { label: 'Monitoring status', value: displayValue('monitoringStatus', exitWorker.monitoringStatus ?? signal.monitoringStatus), tone: 'muted' },
    { label: 'Lifecycle state', value: displayValue('lifecycleState', exitWorker.lifecycleState ?? signalLifecycleState), tone: 'muted' },
    { label: 'Cooldown active', value: displayValue('cooldownActive', exitWorker.cooldownActive ?? signal.cooldownActive), tone: 'warn' },
  ]
  const exitNextTriggerRows: LabeledStat[] = [
    { label: 'Current phase', value: displayValue('currentPhase', exitWorker.currentPhase), tone: 'info' },
    { label: 'Next exit trigger', value: displayValue('nextExitTrigger', exitWorker.nextExitTrigger), tone: 'warn' },
    { label: 'Next trigger level', value: displayValue('nextTriggerLevel', exitWorker.nextTriggerLevel), tone: 'warn' },
    { label: 'Next trigger distance', value: displayValue('nextTriggerDistance', exitWorker.nextTriggerDistance), tone: toneFromPnl(-(asNumber(exitWorker.nextTriggerDistance) ?? 0)) },
    { label: 'Next review', value: displayValue('nextReviewAtUtc', exitWorker.nextReviewAtUtc), tone: 'muted' },
    { label: 'Transition condition', value: displayValue('phaseTransitionCondition', exitWorker.phaseTransitionCondition), tone: 'muted' },
    { label: 'Active trigger', value: displayValue('activeTriggerLabel', exitWorker.activeTriggerLabel), tone: 'info' },
    { label: 'Position maturity', value: displayValue('positionMaturity', exitWorker.positionMaturity), tone: 'muted' },
  ]
  const exitHealthRows: LabeledStat[] = [
    { label: 'Structure health', value: displayValue('structureHealth', exitWorker.structureHealth), tone: 'info' },
    { label: 'Signal conflict', value: displayValue('signalConflict', exitWorker.signalConflict), tone: 'warn' },
    { label: 'Trail status', value: displayValue('trailStatus', exitWorker.trailStatus), tone: 'warn' },
    { label: 'Volatility regime', value: displayValue('volatilityRegime', exitWorker.volatilityRegime), tone: 'muted' },
    { label: 'Risk state', value: displayValue('riskState', exitWorker.riskState), tone: 'warn' },
    { label: 'Risk compression', value: displayValue('riskCompression', exitWorker.riskCompression), tone: 'muted' },
    { label: 'Distance from stop %', value: displayValue('distanceFromStopPct', exitWorker.distanceFromStopPct), tone: 'warn' },
    { label: 'Distance from trail %', value: displayValue('distanceFromTrailPct', exitWorker.distanceFromTrailPct), tone: 'warn' },
    { label: 'Distance from target %', value: displayValue('distanceFromTargetPct', exitWorker.distanceFromTargetPct), tone: 'good' },
    { label: 'Unrealized profit exposed', value: displayValue('unrealizedProfitExposed', exitWorker.unrealizedProfitExposed), tone: toneFromPnl(asNumber(exitWorker.unrealizedProfitExposed) ?? 0) },
    { label: 'Exit readiness score', value: displayValue('exitReadinessScore', exitWorker.exitReadinessScore), tone: 'info' },
    { label: 'Exit likelihood', value: displayValue('exitLikelihood', exitWorker.exitLikelihood), tone: 'muted' },
    { label: 'Expected exit range R', value: formatRange(asRecord(exitWorker.expectedExitRangeR)), tone: 'muted' },
    { label: 'Current progress R', value: displayValue('currentProgressR', exitWorker.currentProgressR), tone: 'muted' },
    { label: 'Strategy biases', value: displayValue('strategyBiases', exitWorker.strategyBiases), tone: 'muted' },
    { label: 'Exit sensitivity', value: displayValue('exitSensitivity', exitWorker.exitSensitivity), tone: 'muted' },
  ]
  const executionRows: LabeledStat[] = [
    { label: 'Execution status', value: displayValue('executionStatus', exitWorker.executionStatus), tone: 'info' },
    { label: 'Broker status', value: displayValue('brokerStatus', exitWorker.brokerStatus), tone: 'muted' },
    { label: 'Managed only', value: displayValue('managedOnly', exitWorker.managedOnly), tone: 'warn' },
    { label: 'Managed only explanation', value: displayValue('managedOnlyExplanation', exitWorker.managedOnlyExplanation), tone: 'muted' },
  ]
  const exitStateHistory = Array.isArray(exitWorker.stateHistory) ? exitWorker.stateHistory.map((item) => {
    const row = asRecord(item)
    return {
      time: asText(row.time),
      label: asText(row.label) ?? 'State update',
      detail: asText(row.detail),
    }
  }) : []

  const rawSections = [
    { label: 'Signal snapshot', value: inspect.signalSnapshot ?? {} },
    { label: 'Sizing math', value: inspect.sizing ?? {} },
    { label: 'Exit plan', value: inspect.exitPlan ?? {} },
    { label: 'Latest evaluation', value: inspect.latestEvaluation ?? {} },
    { label: 'Exit worker', value: inspect.exitWorker ?? {} },
    { label: 'Raw context', value: inspect.rawContext ?? {} },
  ]

  return {
    overview,
    strategyRows,
    sizingRows,
    exitRows,
    exitVerdictRows,
    exitVerdictReason: asText(exitWorker.whyNotExitingYet) ?? asText(latestEvaluation.reason),
    exitNextTriggerRows,
    exitHealthRows,
    executionRows,
    exitStateHistory,
    rawSections,
  }
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {}
}

function asNumber(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if (typeof value === 'string') {
    const parsed = Number(value)
    return Number.isFinite(parsed) ? parsed : null
  }
  return null
}

function asText(value: unknown): string | null {
  if (value == null) return null
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  return null
}

function StructuredInspectCard({
  title,
  eyebrow,
  icon,
  children,
}: {
  title: string
  eyebrow: string
  icon: ReactNode
  children: ReactNode
}) {
  return (
    <div className="mt-6 rounded-3xl border border-slate-800 bg-slate-900/70 p-4">
      <div className="mb-4 flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.18em] text-slate-500">
        {icon}
        {eyebrow}
      </div>
      <div className="mb-4 text-lg font-semibold text-white">{title}</div>
      {children}
    </div>
  )
}

function MetricTile({ label, value, tone }: { label: string; value: string; tone: Tone }) {
  return (
    <div className="rounded-2xl border border-slate-800 bg-slate-900/70 p-4">
      <div className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-500">{label}</div>
      <div className={`mt-3 text-lg font-semibold ${toneTextClassForTile(tone)}`}>{value}</div>
    </div>
  )
}

function toneTextClassForTile(tone: Tone) {
  switch (tone) {
    case 'good':
      return 'text-emerald-300'
    case 'warn':
      return 'text-amber-300'
    case 'danger':
      return 'text-rose-300'
    case 'info':
      return 'text-cyan-200'
    default:
      return 'text-white'
  }
}

function KeyValueList({ rows }: { rows: LabeledStat[] }) {
  const filteredRows = rows.filter((row) => row.value && row.value !== '—')

  return (
    <div className="space-y-3 text-sm">
      {filteredRows.length === 0 ? <EmptyState message="No detail rows were stored for this section yet." /> : null}
      {filteredRows.map((row) => (
        <DetailRow key={row.label} label={row.label} value={row.value} tone={row.tone ?? 'muted'} />
      ))}
    </div>
  )
}

function KeyValueGrid({ rows }: { rows: LabeledStat[] }) {
  const filteredRows = rows.filter((row) => row.value && row.value !== '—')
  return (
    <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
      {filteredRows.length === 0 ? <EmptyState message="No structured values were stored for this section yet." /> : null}
      {filteredRows.map((row) => (
        <div key={row.label} className="rounded-2xl border border-slate-800 bg-slate-950/70 px-4 py-3">
          <div className="text-xs uppercase tracking-wide text-slate-500">{row.label}</div>
          <div className={`mt-2 text-sm ${toneTextClassForTile(row.tone ?? 'muted')}`}>{row.value}</div>
        </div>
      ))}
    </div>
  )
}

function ExitStateHistory({ items }: { items: Array<{ time: string | null; label: string; detail: string | null }> }) {
  if (items.length === 0) {
    return <EmptyState message="No exit-state transitions were stored yet." />
  }

  return (
    <div className="space-y-3 rounded-2xl border border-slate-800 bg-slate-950/70 p-4">
      <div className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-500">State history</div>
      {items.map((item, index) => (
        <div key={`${item.label}-${index}`} className="rounded-2xl border border-slate-800/80 bg-slate-900/70 px-4 py-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="text-sm font-semibold text-white">{item.label}</div>
            <div className="text-xs text-slate-500">{item.time ? formatCompactDateTime(item.time) : 'Latest'}</div>
          </div>
          <div className="mt-2 text-sm leading-6 text-slate-400">{item.detail ?? 'No extra detail stored for this state transition.'}</div>
        </div>
      ))}
    </div>
  )
}

function formatRange(value: Record<string, unknown>): string {
  const from = asNumber(value.from)
  const to = asNumber(value.to)
  if (from == null || to == null) return '—'
  return `${from.toFixed(1)}R – ${to.toFixed(1)}R`
}

function TimeframeAlignmentTable({ items }: { items: PositionInspectTimeframeItem[] }) {
  if (items.length === 0) {
    return <EmptyState message="No normalized timeframe confirmations were stored for this position." />
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[420px] text-sm">
        <thead>
          <tr className="border-b border-slate-800 text-left text-xs uppercase tracking-wide text-slate-500">
            <th className="pb-3 pr-4">Timeframe</th>
            <th className="pb-3 pr-4">Status</th>
            <th className="pb-3">Why</th>
          </tr>
        </thead>
        <tbody>
          {items.map((item) => {
            const meta = getStatusMeta(item.status)
            return (
              <tr key={item.timeframe} className="border-b border-slate-900/80 text-slate-300">
                <td className="py-3 pr-4 font-semibold text-white">{item.timeframe}</td>
                <td className="py-3 pr-4"><ToneBadge tone={meta.tone}>{item.status}</ToneBadge></td>
                <td className="py-3 text-slate-400">{item.reason || 'No reason stored.'}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function LifecycleTimeline({
  events,
  expandedState,
  onToggle,
}: {
  events: PositionInspectTimelineEvent[]
  expandedState: Record<string, boolean>
  onToggle: (key: string) => void
}) {
  if (events.length === 0) {
    return <EmptyState message="No lifecycle events were stored for this position yet." />
  }

  return (
    <div className="space-y-3">
      {events.map((event, index) => {
        const key = `${event.eventType}-${event.eventTime ?? index}`
        const expanded = Boolean(expandedState[key])
        const statusMeta = getStatusMeta(event.status)

        return (
          <div key={key} className="rounded-2xl border border-slate-800 bg-slate-950/70 px-4 py-4">
            <div className="flex flex-wrap items-center gap-2">
              <ToneBadge tone="info">{event.eventType}</ToneBadge>
              <ToneBadge tone={statusMeta.tone}>{event.status}</ToneBadge>
              <span className="text-xs uppercase tracking-wide text-slate-500">{formatTimeOnly(event.eventTime)}</span>
            </div>
            {event.message ? <p className="mt-3 text-sm text-slate-300">{event.message}</p> : null}
            <button
              type="button"
              onClick={() => onToggle(key)}
              className="mt-3 inline-flex items-center gap-2 rounded-full border border-slate-700 px-3 py-1 text-xs font-semibold uppercase tracking-wide text-slate-300 transition hover:border-cyan-700 hover:text-cyan-200"
            >
              {expanded ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
              {expanded ? 'Hide details' : 'Show details'}
            </button>
            {expanded ? <pre className="mt-3 overflow-x-auto whitespace-pre-wrap break-words rounded-2xl border border-slate-800 bg-slate-900/70 p-3 text-xs text-slate-400">{formatJson(event.payload ?? {})}</pre> : null}
          </div>
        )
      })}
    </div>
  )
}

function DrawerLoadingSkeleton() {
  return (
    <div className="mt-6 space-y-4">
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3">
        {Array.from({ length: 6 }).map((_, index) => (
          <div key={index} className="h-24 animate-pulse rounded-2xl border border-slate-800 bg-slate-900/70" />
        ))}
      </div>
      {Array.from({ length: 3 }).map((_, index) => (
        <div key={index} className="h-40 animate-pulse rounded-3xl border border-slate-800 bg-slate-900/70" />
      ))}
    </div>
  )
}

function InspectJsonCard({ title, value, compact = false }: { title: string; value: unknown; compact?: boolean }) {
  return (
    <div className={`${compact ? '' : 'mt-6 '}rounded-2xl border border-slate-800 bg-slate-900/70 p-4`}>
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
