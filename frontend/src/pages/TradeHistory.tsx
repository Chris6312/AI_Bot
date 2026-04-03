import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Download, History, Search } from 'lucide-react'

import { PageHero, SectionCard, StatusPill, ToneBadge, EmptyState, type Tone } from '@/components/operator-ui'
import type { TradeHistoryResponse, TradeHistoryRow } from '@/types'

function formatMoney(value?: number | null) {
  const amount = Number(value ?? 0)
  return amount.toLocaleString('en-US', { style: 'currency', currency: 'USD' })
}

function formatQuantity(value?: number | null, assetClass?: string) {
  const amount = Number(value ?? 0)
  return amount.toLocaleString('en-US', {
    minimumFractionDigits: assetClass === 'crypto' ? 2 : 0,
    maximumFractionDigits: assetClass === 'crypto' ? 8 : 4,
  })
}

function formatDuration(minutes?: number | null) {
  if (!minutes || minutes <= 0) return '—'
  if (minutes < 60) return `${minutes}m`
  const hours = Math.floor(minutes / 60)
  const remainder = minutes % 60
  if (!remainder) return `${hours}h`
  return `${hours}h ${remainder}m`
}

function formatEt(value?: string | null) {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  return new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/New_York',
    year: 'numeric',
    month: 'short',
    day: '2-digit',
    hour: 'numeric',
    minute: '2-digit',
    hour12: true,
  }).format(date)
}

function pnlTone(value?: number | null): Tone {
  const amount = Number(value ?? 0)
  if (amount > 0) return 'good'
  if (amount < 0) return 'danger'
  return 'muted'
}

async function getTradeHistory(filters: {
  mode: 'ALL' | 'PAPER' | 'LIVE'
  assetClass: 'all' | 'stock' | 'crypto'
  symbol: string
  dateFrom: string
  dateTo: string
}): Promise<TradeHistoryResponse> {
  const params = new URLSearchParams()
  params.set('mode', filters.mode)
  params.set('asset_class', filters.assetClass)
  if (filters.symbol.trim()) params.set('symbol', filters.symbol.trim())
  if (filters.dateFrom) params.set('date_from', filters.dateFrom)
  if (filters.dateTo) params.set('date_to', filters.dateTo)
  const response = await fetch(`/api/trade-history?${params.toString()}`)
  if (!response.ok) {
    throw new Error(`Failed to load trade history: ${response.status}`)
  }
  return response.json()
}

function exportRowsToCsv(rows: TradeHistoryRow[]) {
  const headers = [
    'Symbol',
    'Asset Class',
    'Mode',
    'Bought At ET',
    'Buy Price',
    'Buy Quantity',
    'Buy Total',
    'Sold At ET',
    'Sell Price',
    'Sell Quantity',
    'Sell Total',
    'Unit Diff',
    'Fees',
    'Realized PnL',
    'Hold Duration Minutes',
    'Source',
    'Trade ID',
    'Buy Intent ID',
    'Sell Intent ID',
    'Exit Trigger',
  ]

  const lines = rows.map((row) => [
    row.symbol,
    row.assetClass,
    row.mode,
    formatEt(row.boughtAtUtc),
    row.buyPrice ?? '',
    row.buyQuantity,
    row.buyTotal ?? '',
    formatEt(row.soldAtUtc),
    row.sellPrice ?? '',
    row.sellQuantity,
    row.sellTotal ?? '',
    row.unitDiff ?? '',
    row.fees ?? '',
    row.realizedPnl ?? '',
    row.holdDurationMinutes ?? '',
    row.source ?? '',
    row.tradeId ?? '',
    row.buyIntentId ?? '',
    row.sellIntentId ?? '',
    row.exitTrigger ?? '',
  ])

  const escape = (value: unknown) => `"${String(value ?? '').replace(/"/g, '""')}"`
  const csv = [headers, ...lines].map((row) => row.map(escape).join(',')).join('\n')
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  const stamp = new Date().toISOString().slice(0, 10)
  anchor.href = url
  anchor.download = `trade_history_${stamp}.csv`
  anchor.click()
  URL.revokeObjectURL(url)
}

export default function TradeHistory() {
  const [mode, setMode] = useState<'ALL' | 'PAPER' | 'LIVE'>('ALL')
  const [assetClass, setAssetClass] = useState<'all' | 'stock' | 'crypto'>('all')
  const [symbol, setSymbol] = useState('')
  const [dateFrom, setDateFrom] = useState('')
  const [dateTo, setDateTo] = useState('')

  const { data, isLoading, isError, error } = useQuery<TradeHistoryResponse>({
    queryKey: ['tradeHistory', mode, assetClass, symbol, dateFrom, dateTo],
    queryFn: () => getTradeHistory({ mode, assetClass, symbol, dateFrom, dateTo }),
    refetchInterval: 15000,
  })

  const rows = data?.rows ?? []
  const summary = data?.summary
  const avgPnl = useMemo(() => (rows.length ? (summary?.realizedPnl ?? 0) / rows.length : 0), [rows.length, summary?.realizedPnl])

  return (
    <div className="space-y-6">
      <PageHero
        eyebrow={
          <>
            <History className="h-4 w-4" />
            Trade History
          </>
        }
        title="Closed trades, one ledger lantern at a time"
        description="Filter realized stock and crypto trades by mode, asset class, date range, and symbol. Export sends the exact rows currently on screen to CSV."
        aside={
          <>
            <StatusPill tone="info" label={`${summary?.totalCount ?? 0} closed trades`} />
            <StatusPill tone={pnlTone(summary?.realizedPnl)} label={`Realized ${formatMoney(summary?.realizedPnl)}`} />
          </>
        }
      />

      <SectionCard
        title="Filters"
        eyebrow="Slice controls"
        icon={<Search className="h-5 w-5" />}
        actions={
          <button
            type="button"
            onClick={() => exportRowsToCsv(rows)}
            className="inline-flex items-center gap-2 rounded-2xl border border-cyan-700/60 bg-cyan-500/10 px-4 py-2 text-sm font-medium text-cyan-200 transition hover:bg-cyan-500/20"
          >
            <Download className="h-4 w-4" />
            Export current view
          </button>
        }
      >
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-5">
          <label className="space-y-2 text-sm text-slate-300">
            <span className="block text-xs uppercase tracking-[0.18em] text-slate-500">Mode</span>
            <select value={mode} onChange={(event) => setMode(event.target.value as 'ALL' | 'PAPER' | 'LIVE')} className="w-full rounded-2xl border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-white">
              <option value="ALL">All</option>
              <option value="PAPER">Paper</option>
              <option value="LIVE">Live</option>
            </select>
          </label>

          <label className="space-y-2 text-sm text-slate-300">
            <span className="block text-xs uppercase tracking-[0.18em] text-slate-500">Asset</span>
            <select value={assetClass} onChange={(event) => setAssetClass(event.target.value as 'all' | 'stock' | 'crypto')} className="w-full rounded-2xl border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-white">
              <option value="all">All</option>
              <option value="stock">Stock</option>
              <option value="crypto">Crypto</option>
            </select>
          </label>

          <label className="space-y-2 text-sm text-slate-300">
            <span className="block text-xs uppercase tracking-[0.18em] text-slate-500">Symbol</span>
            <input value={symbol} onChange={(event) => setSymbol(event.target.value.toUpperCase())} placeholder="AAPL or SOL" className="w-full rounded-2xl border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-white placeholder:text-slate-500" />
          </label>

          <label className="space-y-2 text-sm text-slate-300">
            <span className="block text-xs uppercase tracking-[0.18em] text-slate-500">From</span>
            <input type="date" value={dateFrom} onChange={(event) => setDateFrom(event.target.value)} className="w-full rounded-2xl border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-white" />
          </label>

          <label className="space-y-2 text-sm text-slate-300">
            <span className="block text-xs uppercase tracking-[0.18em] text-slate-500">To</span>
            <input type="date" value={dateTo} onChange={(event) => setDateTo(event.target.value)} className="w-full rounded-2xl border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-white" />
          </label>
        </div>
      </SectionCard>

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
        <SummaryCard label="Closed trades" value={String(summary?.totalCount ?? 0)} detail="Rows currently visible" tone="info" />
        <SummaryCard label="Net realized" value={formatMoney(summary?.realizedPnl)} detail={`${summary?.winCount ?? 0} wins · ${summary?.lossCount ?? 0} losses`} tone={pnlTone(summary?.realizedPnl)} />
        <SummaryCard label="By asset" value={`${summary?.assetCounts.stock ?? 0} stock / ${summary?.assetCounts.crypto ?? 0} crypto`} detail="Unified closed-trade table" tone="muted" />
        <SummaryCard label="Average PnL" value={formatMoney(avgPnl)} detail={`${summary?.modeCounts.PAPER ?? 0} paper / ${summary?.modeCounts.LIVE ?? 0} live`} tone={pnlTone(avgPnl)} />
      </div>

      <SectionCard title="Closed trades table" eyebrow="Realized ledger" icon={<History className="h-5 w-5" />}>
        {isLoading ? <EmptyState message="Loading closed trades…" /> : null}
        {isError ? <EmptyState message={error instanceof Error ? error.message : 'Failed to load trade history.'} /> : null}
        {!isLoading && !isError && rows.length === 0 ? <EmptyState message="No closed trades match the current filters yet." /> : null}
        {!isLoading && !isError && rows.length > 0 ? (
          <div className="overflow-x-auto">
            <table className="min-w-full text-sm text-slate-200">
              <thead>
                <tr className="border-b border-slate-800 text-left text-xs uppercase tracking-[0.18em] text-slate-500">
                  <th className="px-3 py-3">Symbol</th>
                  <th className="px-3 py-3">Bought ET</th>
                  <th className="px-3 py-3">Buy</th>
                  <th className="px-3 py-3">Sold ET</th>
                  <th className="px-3 py-3">Sell</th>
                  <th className="px-3 py-3">Unit Diff</th>
                  <th className="px-3 py-3">Realized PnL</th>
                  <th className="px-3 py-3">Duration</th>
                  <th className="px-3 py-3">Source</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.id} className="border-b border-slate-900/80 align-top hover:bg-slate-900/40">
                    <td className="px-3 py-4">
                      <div className="font-medium text-white">{row.symbol}</div>
                      <div className="mt-2 flex flex-wrap gap-2">
                        <ToneBadge tone="muted">{row.assetClass}</ToneBadge>
                        <ToneBadge tone="info">{row.mode}</ToneBadge>
                      </div>
                    </td>
                    <td className="px-3 py-4 text-slate-300">{formatEt(row.boughtAtUtc)}</td>
                    <td className="px-3 py-4">
                      <div>{formatMoney(row.buyPrice)}</div>
                      <div className="text-slate-400">{formatQuantity(row.buyQuantity, row.assetClass)} · {formatMoney(row.buyTotal)}</div>
                    </td>
                    <td className="px-3 py-4 text-slate-300">{formatEt(row.soldAtUtc)}</td>
                    <td className="px-3 py-4">
                      <div>{formatMoney(row.sellPrice)}</div>
                      <div className="text-slate-400">{formatQuantity(row.sellQuantity, row.assetClass)} · {formatMoney(row.sellTotal)}</div>
                    </td>
                    <td className="px-3 py-4">
                      <span className={pnlTone(row.unitDiff) === 'good' ? 'text-emerald-300' : pnlTone(row.unitDiff) === 'danger' ? 'text-rose-300' : 'text-slate-300'}>
                        {formatMoney(row.unitDiff)}
                      </span>
                    </td>
                    <td className="px-3 py-4">
                      <span className={pnlTone(row.realizedPnl) === 'good' ? 'text-emerald-300' : pnlTone(row.realizedPnl) === 'danger' ? 'text-rose-300' : 'text-slate-300'}>
                        {formatMoney(row.realizedPnl)}
                      </span>
                      {row.fees ? <div className="text-slate-500">Fees {formatMoney(row.fees)}</div> : null}
                    </td>
                    <td className="px-3 py-4 text-slate-300">{formatDuration(row.holdDurationMinutes)}</td>
                    <td className="px-3 py-4 text-slate-400">
                      <div>{row.source ?? '—'}</div>
                      {row.exitTrigger ? <div className="mt-1 text-slate-500">Exit: {row.exitTrigger}</div> : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </SectionCard>
    </div>
  )
}

function SummaryCard({ label, value, detail, tone }: { label: string; value: string; detail: string; tone: Tone }) {
  return (
    <div className="rounded-3xl border border-slate-800 bg-slate-900/70 p-5 shadow-xl shadow-slate-950/20">
      <div className="text-xs uppercase tracking-[0.18em] text-slate-500">{label}</div>
      <div className="mt-3 text-2xl font-semibold text-white">{value}</div>
      <div className={`mt-2 text-sm ${tone === 'good' ? 'text-emerald-300' : tone === 'danger' ? 'text-rose-300' : tone === 'info' ? 'text-cyan-300' : 'text-slate-400'}`}>{detail}</div>
    </div>
  )
}
