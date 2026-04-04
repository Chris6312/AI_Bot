import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Download, Filter, History, RefreshCw, Trophy, TriangleAlert, Wallet } from 'lucide-react'

import {
  DetailRow,
  EmptyState,
  MetricCard,
  PageHero,
  SectionCard,
  StatusPill,
  toneTextClass,
  type Tone,
} from '@/components/operator-ui'
import type { TradeHistoryResponse, TradeHistoryRow } from '@/types'

const ET_DATE_FORMAT = new Intl.DateTimeFormat('en-US', {
  timeZone: 'America/New_York',
  year: 'numeric',
  month: 'short',
  day: '2-digit',
  hour: 'numeric',
  minute: '2-digit',
  hour12: false,
})

const ET_CURRENCY = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  maximumFractionDigits: 2,
})

const ET_NUMBER = new Intl.NumberFormat('en-US', {
  maximumFractionDigits: 8,
})

async function fetchTradeHistory(filters: {
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
    throw new Error(`Trade history request failed: ${response.status}`)
  }
  return response.json()
}

function formatMoney(value?: number | null) {
  return ET_CURRENCY.format(value ?? 0)
}

function formatQuantity(value?: number | null) {
  return ET_NUMBER.format(value ?? 0)
}

function formatEt(value?: string | null) {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  return `${ET_DATE_FORMAT.format(date)} ET`
}

function prettifyLabel(value?: string | null) {
  if (!value) return '—'
  return value
    .split('_')
    .filter(Boolean)
    .map((part) => part.charAt(0) + part.slice(1).toLowerCase())
    .join(' ')
}

function titleCase(value?: string | null) {
  if (!value) return '—'
  return value.charAt(0).toUpperCase() + value.slice(1).toLowerCase()
}

function pnlTone(value?: number | null): Tone {
  if ((value ?? 0) > 0) return 'good'
  if ((value ?? 0) < 0) return 'danger'
  return 'muted'
}

function formatPercent(value?: number | null) {
  if (value == null || Number.isNaN(value)) return '—'
  return `${value.toFixed(2)}%`
}

function strategySummary(row: TradeHistoryRow) {
  const setup = prettifyLabel(row.strategySnapshot?.setupTemplate)
  const exit = prettifyLabel(row.strategySnapshot?.exitTemplate)
  const timeframes = (row.strategySnapshot?.botTimeframes ?? []).filter(Boolean).join(', ') || '—'
  return { setup, exit, timeframes }
}

function technicalSummary(row: TradeHistoryRow) {
  const pieces: string[] = []
  if (row.technicalSnapshot?.changePct != null) pieces.push(`Δ ${formatPercent(row.technicalSnapshot.changePct)}`)
  if (row.technicalSnapshot?.sma5 != null) pieces.push(`SMA5 ${ET_NUMBER.format(row.technicalSnapshot.sma5)}`)
  if (row.technicalSnapshot?.sma10 != null) pieces.push(`SMA10 ${ET_NUMBER.format(row.technicalSnapshot.sma10)}`)
  if (row.technicalSnapshot?.recentHigh != null) pieces.push(`Hi ${ET_NUMBER.format(row.technicalSnapshot.recentHigh)}`)
  if (row.technicalSnapshot?.recentLow != null) pieces.push(`Lo ${ET_NUMBER.format(row.technicalSnapshot.recentLow)}`)
  if (row.technicalSnapshot?.continuityOk != null) pieces.push(`Continuity ${row.technicalSnapshot.continuityOk ? 'ok' : 'check'}`)
  return pieces.join(' • ') || 'Snapshot unavailable'
}

function exportRowsToCsv(rows: TradeHistoryRow[]) {
  const headers = [
    'Asset Class',
    'Mode',
    'Symbol',
    'Bought At ET',
    'Buy Price',
    'Buy Quantity',
    'Buy Total',
    'Sold At ET',
    'Sell Price',
    'Sell Quantity',
    'Sell Total',
    'Price Difference',
    'Difference Amount',
    'Realized PnL',
    'Setup Template',
    'Exit Template',
    'Bias',
    'Timeframes',
    'Change Pct',
    'SMA5',
    'SMA10',
    'Recent High',
    'Recent Low',
    'Continuity Ok',
    'Exit Trigger',
    'Source',
  ]

  const lines = rows.map((row) => [
    titleCase(row.assetClass),
    titleCase(row.mode),
    row.symbol,
    formatEt(row.boughtAtEt ?? row.boughtAtUtc),
    row.buyPrice ?? '',
    row.buyQuantity ?? '',
    row.buyTotal ?? '',
    formatEt(row.soldAtEt ?? row.soldAtUtc),
    row.sellPrice ?? '',
    row.sellQuantity ?? '',
    row.sellTotal ?? '',
    row.priceDifference ?? '',
    row.differenceAmount ?? '',
    row.realizedPnl ?? '',
    row.strategySnapshot?.setupTemplate ?? '',
    row.strategySnapshot?.exitTemplate ?? '',
    row.strategySnapshot?.bias ?? '',
    (row.strategySnapshot?.botTimeframes ?? []).join(' | '),
    row.technicalSnapshot?.changePct ?? '',
    row.technicalSnapshot?.sma5 ?? '',
    row.technicalSnapshot?.sma10 ?? '',
    row.technicalSnapshot?.recentHigh ?? '',
    row.technicalSnapshot?.recentLow ?? '',
    row.technicalSnapshot?.continuityOk ?? '',
    prettifyLabel(row.exitTrigger),
    prettifyLabel(row.source),
  ])

  const csv = [headers, ...lines]
    .map((row) =>
      row
        .map((value) => `"${String(value ?? '').split('"').join('""')}"`)
        .join(','),
    )
    .join('\n')

  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  const stamp = new Date().toISOString().slice(0, 10)
  link.href = url
  link.download = `trade-history-${stamp}.csv`
  link.click()
  URL.revokeObjectURL(url)
}

export default function TradeHistory() {
  const [mode, setMode] = useState<'ALL' | 'PAPER' | 'LIVE'>('ALL')
  const [assetClass, setAssetClass] = useState<'all' | 'stock' | 'crypto'>('all')
  const [symbol, setSymbol] = useState('')
  const [dateFrom, setDateFrom] = useState('')
  const [dateTo, setDateTo] = useState('')

  const filters = useMemo(
    () => ({ mode, assetClass, symbol, dateFrom, dateTo }),
    [assetClass, dateFrom, dateTo, mode, symbol],
  )

  const { data, isLoading, isFetching, error, refetch } = useQuery<TradeHistoryResponse>({
    queryKey: ['tradeHistory', filters],
    queryFn: () => fetchTradeHistory(filters),
    refetchInterval: 15000,
  })

  const rows = data?.rows ?? []
  const summary = data?.summary

  return (
    <div className="space-y-6">
      <PageHero
        eyebrow={
          <>
            <History className="h-4 w-4" />
            Trade history
          </>
        }
        title="Closed trades with ET timestamps and export-ready filters"
        description="Stocks and crypto share one tax-prep lane here. The table stays anchored to reconciled fills, filters by sold date, and the export button only ships the rows you are currently looking at."
        aside={
          <>
            <StatusPill tone="info" label={`${summary?.totalCount ?? 0} rows`} />
            <StatusPill tone="good" label={`${summary?.assetCounts.stock ?? 0} stock`} />
            <StatusPill tone="good" label={`${summary?.assetCounts.crypto ?? 0} crypto`} />
            <StatusPill tone={isFetching ? 'warn' : 'good'} label={isFetching ? 'Refreshing' : 'Data ready'} />
          </>
        }
      />

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-5">
        <MetricCard label="Realized PnL" value={formatMoney(summary?.realizedPnl ?? 0)} detail="Displayed rows only" icon={<Wallet className="h-5 w-5" />} />
        <MetricCard label="Wins" value={String(summary?.winCount ?? 0)} detail="Closed trades with positive PnL" icon={<Trophy className="h-5 w-5" />} />
        <MetricCard label="Losses" value={String(summary?.lossCount ?? 0)} detail="Closed trades with negative PnL" icon={<TriangleAlert className="h-5 w-5" />} />
        <MetricCard label="Paper trades" value={String(summary?.modeCounts.PAPER ?? 0)} detail="Rows tagged PAPER" />
        <MetricCard label="Live trades" value={String(summary?.modeCounts.LIVE ?? 0)} detail="Rows tagged LIVE" />
      </div>

      <SectionCard
        title="Filters"
        eyebrow="Current view"
        icon={<Filter className="h-5 w-5" />}
        actions={
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              onClick={() => refetch()}
              className="inline-flex items-center gap-2 rounded-2xl border border-slate-700 bg-slate-950/70 px-4 py-2 text-sm text-slate-200 transition hover:border-slate-600 hover:text-white"
            >
              <RefreshCw className={`h-4 w-4 ${isFetching ? 'animate-spin' : ''}`} />
              Refresh
            </button>
            <button
              type="button"
              onClick={() => exportRowsToCsv(rows)}
              disabled={rows.length === 0}
              className="inline-flex items-center gap-2 rounded-2xl border border-cyan-700 bg-cyan-500/10 px-4 py-2 text-sm text-cyan-100 transition enabled:hover:bg-cyan-500/20 disabled:cursor-not-allowed disabled:opacity-50"
            >
              <Download className="h-4 w-4" />
              Export current view
            </button>
          </div>
        }
      >
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-5">
          <label className="space-y-2 text-sm text-slate-300">
            <span>Mode</span>
            <select value={mode} onChange={(event) => setMode(event.target.value as 'ALL' | 'PAPER' | 'LIVE')} className="w-full rounded-2xl border border-slate-700 bg-slate-950/70 px-3 py-2 text-white outline-none focus:border-cyan-600">
              <option value="ALL">All</option>
              <option value="PAPER">Paper</option>
              <option value="LIVE">Live</option>
            </select>
          </label>

          <label className="space-y-2 text-sm text-slate-300">
            <span>Asset class</span>
            <select value={assetClass} onChange={(event) => setAssetClass(event.target.value as 'all' | 'stock' | 'crypto')} className="w-full rounded-2xl border border-slate-700 bg-slate-950/70 px-3 py-2 text-white outline-none focus:border-cyan-600">
              <option value="all">All</option>
              <option value="stock">Stocks</option>
              <option value="crypto">Crypto</option>
            </select>
          </label>

          <label className="space-y-2 text-sm text-slate-300">
            <span>Symbol contains</span>
            <input value={symbol} onChange={(event) => setSymbol(event.target.value.toUpperCase())} placeholder="AAPL or BTC" className="w-full rounded-2xl border border-slate-700 bg-slate-950/70 px-3 py-2 text-white outline-none placeholder:text-slate-500 focus:border-cyan-600" />
          </label>

          <label className="space-y-2 text-sm text-slate-300">
            <span>From date</span>
            <input type="date" value={dateFrom} onChange={(event) => setDateFrom(event.target.value)} className="w-full rounded-2xl border border-slate-700 bg-slate-950/70 px-3 py-2 text-white outline-none focus:border-cyan-600" />
          </label>

          <label className="space-y-2 text-sm text-slate-300">
            <span>To date</span>
            <input type="date" value={dateTo} onChange={(event) => setDateTo(event.target.value)} className="w-full rounded-2xl border border-slate-700 bg-slate-950/70 px-3 py-2 text-white outline-none focus:border-cyan-600" />
          </label>
        </div>

        <div className="mt-4 grid grid-cols-1 gap-4 xl:grid-cols-3">
          <DetailRow label="Current ET range" value={summary?.dateRange.fromEt || summary?.dateRange.toEt ? `${formatEt(summary?.dateRange.fromEt)} → ${formatEt(summary?.dateRange.toEt)}` : 'All sold dates'} />
          <DetailRow label="Export scope" value={`${rows.length} displayed row${rows.length === 1 ? '' : 's'}`} />
          <DetailRow label="Filter symbol" value={filters.symbol || 'Any'} />
        </div>
      </SectionCard>

      <SectionCard title="Closed trade ledger" eyebrow="Normalized rows" icon={<History className="h-5 w-5" />}>
        {error ? (
          <EmptyState message={error instanceof Error ? error.message : 'Trade history request failed.'} />
        ) : isLoading ? (
          <EmptyState message="Pulling the closed-trade tape from the backend." />
        ) : rows.length === 0 ? (
          <EmptyState message="No closed trades for this filter set. Try widening the sold-date range, switching mode, or clearing the symbol filter." />
        ) : (
          <div className="overflow-x-auto">
            <table className="min-w-full divide-y divide-slate-800 text-sm">
              <thead>
                <tr className="text-left text-xs uppercase tracking-[0.2em] text-slate-400">
                  <th className="px-3 py-3">Asset</th>
                  <th className="px-3 py-3">Strategy</th>
                  <th className="px-3 py-3">Bought ET</th>
                  <th className="px-3 py-3">Buy</th>
                  <th className="px-3 py-3">Sold ET</th>
                  <th className="px-3 py-3">Sell</th>
                  <th className="px-3 py-3">Difference</th>
                  <th className="px-3 py-3">PnL</th>
                  <th className="px-3 py-3">Exit</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-900/80">
                {rows.map((row) => (
                  <tr key={row.id} className="align-top text-slate-200">
                    <td className="px-3 py-4">
                      <div className="font-medium text-white">{row.symbol}</div>
                      <div className="mt-1 flex flex-wrap gap-2 text-xs text-slate-400">
                        <StatusPill tone={row.assetClass === 'stock' ? 'info' : 'good'} label={row.assetClass} />
                        <StatusPill tone={row.mode === 'LIVE' ? 'warn' : 'muted'} label={row.mode} />
                      </div>
                    </td>
                    <td className="px-3 py-4">
                      <div className="font-medium text-white">{strategySummary(row).setup}</div>
                      <div className="text-xs text-slate-400">Exit {strategySummary(row).exit}</div>
                      <div className="text-xs text-slate-500">TF {strategySummary(row).timeframes}</div>
                    </td>
                    <td className="px-3 py-4 text-slate-300">{formatEt(row.boughtAtEt ?? row.boughtAtUtc)}</td>
                    <td className="px-3 py-4">
                      <div>{formatMoney(row.buyPrice)}</div>
                      <div className="text-xs text-slate-400">Qty {formatQuantity(row.buyQuantity)}</div>
                      <div className="text-xs text-slate-500">Total {formatMoney(row.buyTotal)}</div>
                    </td>
                    <td className="px-3 py-4 text-slate-300">{formatEt(row.soldAtEt ?? row.soldAtUtc)}</td>
                    <td className="px-3 py-4">
                      <div>{formatMoney(row.sellPrice)}</div>
                      <div className="text-xs text-slate-400">Qty {formatQuantity(row.sellQuantity)}</div>
                      <div className="text-xs text-slate-500">Total {formatMoney(row.sellTotal)}</div>
                    </td>
                    <td className="px-3 py-4">
                      <div className={toneTextClass(pnlTone(row.priceDifference))}>{formatMoney(row.priceDifference)}</div>
                      <div className={`text-xs ${toneTextClass(pnlTone(row.differenceAmount))}`}>Total {formatMoney(row.differenceAmount)}</div>
                    </td>
                    <td className="px-3 py-4">
                      <div className={toneTextClass(pnlTone(row.realizedPnl))}>{formatMoney(row.realizedPnl)}</div>
                      <div className="text-xs text-slate-500">{row.holdDurationMinutes != null ? `${row.holdDurationMinutes} min hold` : 'Hold n/a'}</div>
                    </td>
                    <td className="px-3 py-4 text-slate-300">
                      <div>{row.exitTrigger || '—'}</div>
                      <div className="text-xs text-slate-400">{row.source}</div>
                      <div className="mt-1 text-xs text-slate-500">{technicalSummary(row)}</div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </SectionCard>
    </div>
  )
}