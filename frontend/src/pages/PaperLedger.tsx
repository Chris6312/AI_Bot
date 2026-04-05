import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, ArrowRight, ArrowRightLeft, Bitcoin, RefreshCw, TrendingUp, Wallet } from 'lucide-react'

import { api } from '@/lib/api'
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
import type { CryptoLedger, CryptoPaperAdminActionResponse, TradeHistoryEntry } from '@/types'

const ET_DATE_TIME = new Intl.DateTimeFormat('en-US', {
  timeZone: 'America/New_York',
  month: 'short',
  day: '2-digit',
  year: 'numeric',
  hour: 'numeric',
  minute: '2-digit',
  hour12: false,
})

const USD = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  maximumFractionDigits: 2,
})

const NUMBER = new Intl.NumberFormat('en-US', {
  maximumFractionDigits: 8,
})

const RESET_CONFIRMATION_TEXT = 'RESET CRYPTO PAPER'

function formatMoney(value?: number | null) {
  return USD.format(value ?? 0)
}

function formatQuantity(value?: number | null) {
  return NUMBER.format(value ?? 0)
}

function formatEt(value?: string | null) {
  if (!value) return '—'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return '—'
  return `${ET_DATE_TIME.format(parsed)} ET`
}

function formatPercent(value?: number | null) {
  if (value == null || Number.isNaN(value)) return '—'
  const prefix = value > 0 ? '+' : ''
  return `${prefix}${value.toFixed(2)}%`
}

function toneFromValue(value?: number | null): Tone {
  if ((value ?? 0) > 0) return 'good'
  if ((value ?? 0) < 0) return 'danger'
  return 'muted'
}

function tradeTone(side?: string | null): Tone {
  return String(side ?? '').toUpperCase() === 'BUY' ? 'good' : 'warn'
}

function latestTradeTime(trades: TradeHistoryEntry[]) {
  return trades[0]?.timestamp ?? null
}

function formatAdminSummary(summary?: CryptoPaperAdminActionResponse | null) {
  if (!summary) return 'No admin action has run in this session.'
  const parts = [summary.message]
  if ((summary.canceledPendingOrders ?? 0) > 0 || (summary.canceledPendingIntents ?? 0) > 0) {
    parts.push(`Canceled ${summary.canceledPendingOrders ?? 0} orders / ${summary.canceledPendingIntents ?? 0} intents`)
  }
  if ((summary.flattenedPositions ?? 0) > 0) {
    parts.push(`Flattened ${summary.flattenedPositions ?? 0} positions`)
  }
  if ((summary.deletedTrades ?? 0) > 0 || (summary.deletedOrders ?? 0) > 0 || (summary.deletedIntents ?? 0) > 0) {
    parts.push(`Deleted ${summary.deletedTrades ?? 0} trades, ${summary.deletedOrders ?? 0} orders, ${summary.deletedIntents ?? 0} intents`)
  }
  if (summary.newCashBalance != null) {
    parts.push(`Cash ${formatMoney(summary.newCashBalance)}`)
  }
  return parts.join(' · ')
}

export default function PaperLedger() {
  const queryClient = useQueryClient()
  const [cashBalanceInput, setCashBalanceInput] = useState('100000')
  const [typedConfirmation, setTypedConfirmation] = useState('')
  const [adminMessage, setAdminMessage] = useState('')
  const [adminError, setAdminError] = useState('')
  const [lastAdminResult, setLastAdminResult] = useState<CryptoPaperAdminActionResponse | null>(null)

  const {
    data: ledger,
    isLoading: ledgerLoading,
    isFetching: ledgerFetching,
    error: ledgerError,
    refetch: refetchLedger,
  } = useQuery<CryptoLedger>({
    queryKey: ['cryptoPaperLedger'],
    queryFn: api.getCryptoPaperLedger,
    refetchInterval: 5000,
  })

  const {
    data: trades = [],
    isLoading: tradesLoading,
    error: tradesError,
    refetch: refetchTrades,
  } = useQuery<TradeHistoryEntry[]>({
    queryKey: ['cryptoHistory', 50],
    queryFn: () => api.getCryptoHistory(50),
    refetchInterval: 10000,
  })

  const positions = ledger?.positions ?? []
  const openCount = positions.length
  const lastTradeAt = latestTradeTime(trades)
  const realizedPnL = ledger?.realizedPnL ?? ledger?.totalPnL ?? 0
  const unrealizedPnL = (ledger?.netPnL ?? ledger?.totalPnL ?? 0) - realizedPnL
  const typedConfirmationOk = typedConfirmation.trim().toUpperCase() === RESET_CONFIRMATION_TEXT

  const invalidateLedgerViews = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['cryptoPaperLedger'] }),
      queryClient.invalidateQueries({ queryKey: ['cryptoHistory'] }),
      queryClient.invalidateQueries({ queryKey: ['cryptoPositions'] }),
      queryClient.invalidateQueries({ queryKey: ['unifiedPositions'] }),
      queryClient.invalidateQueries({ queryKey: ['runtimeVisibility'] }),
    ])
  }

  const handleAdminSuccess = async (summary: CryptoPaperAdminActionResponse) => {
    setAdminError('')
    setAdminMessage(summary.message)
    setLastAdminResult(summary)
    if (summary.newCashBalance != null) {
      setCashBalanceInput(String(summary.newCashBalance))
    }
    setTypedConfirmation('')
    await invalidateLedgerViews()
  }

  const handleAdminError = (error: Error) => {
    setAdminMessage('')
    setAdminError(error.message)
  }

  const setCashMutation = useMutation({
    mutationFn: (cashBalance: number) => api.setCryptoPaperCashBalance(cashBalance),
    onSuccess: handleAdminSuccess,
    onError: handleAdminError,
  })

  const cancelPendingMutation = useMutation({
    mutationFn: api.cancelPendingCryptoPaperOrders,
    onSuccess: handleAdminSuccess,
    onError: handleAdminError,
  })

  const flattenPositionsMutation = useMutation({
    mutationFn: api.flattenCryptoPaperPositions,
    onSuccess: handleAdminSuccess,
    onError: handleAdminError,
  })

  const deleteHistoryMutation = useMutation({
    mutationFn: api.deleteCryptoPaperHistory,
    onSuccess: handleAdminSuccess,
    onError: handleAdminError,
  })

  const freshStartMutation = useMutation({
    mutationFn: (cashBalance: number) => api.freshStartCryptoPaperAccount(cashBalance),
    onSuccess: handleAdminSuccess,
    onError: handleAdminError,
  })

  const adminBusy = useMemo(
    () => [setCashMutation, cancelPendingMutation, flattenPositionsMutation, deleteHistoryMutation, freshStartMutation].some((mutation) => mutation.isPending),
    [setCashMutation, cancelPendingMutation, flattenPositionsMutation, deleteHistoryMutation, freshStartMutation],
  )

  const parseCashInput = () => {
    const value = Number(cashBalanceInput)
    if (!Number.isFinite(value) || value < 0) {
      throw new Error('Cash balance must be a valid non-negative number.')
    }
    return value
  }

  const refreshAll = () => {
    void refetchLedger()
    void refetchTrades()
    void invalidateLedgerViews()
  }

  return (
    <div className="space-y-6">
      <PageHero
        eyebrow={
          <>
            <Bitcoin className="h-4 w-4" />
            Crypto paper ledger
          </>
        }
        title="Broker-style crypto ledger with ET timestamps"
        description="This lane shows the persisted paper ledger instead of toy mock data. Cash, equity, open positions, and recent fills all pull from the same backend tape that the monitoring and reconciliation flows now rely on."
        aside={
          <>
            <StatusPill tone="good" label={`${openCount} open`} />
            <StatusPill tone={toneFromValue(realizedPnL)} label={`Realized ${formatMoney(realizedPnL)}`} />
            <StatusPill tone={ledgerFetching ? 'warn' : 'good'} label={ledgerFetching ? 'Refreshing' : 'Ledger synced'} />
          </>
        }
      />

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
        <MetricCard label="Cash" value={formatMoney(ledger?.balance ?? 0)} detail="Paper buying power still on hand" icon={<Wallet className="h-5 w-5" />} />
        <MetricCard label="Equity" value={formatMoney(ledger?.equity ?? 0)} detail="Cash plus marked-to-market crypto" icon={<TrendingUp className="h-5 w-5" />} />
        <MetricCard label="Market value" value={formatMoney(ledger?.marketValue ?? 0)} detail="Current value of open crypto positions" icon={<Bitcoin className="h-5 w-5" />} />
        <MetricCard label="Open positions" value={String(openCount)} detail={lastTradeAt ? `Last fill ${formatEt(lastTradeAt)}` : 'No fills yet'} icon={<ArrowRightLeft className="h-5 w-5" />} />
      </div>

      <SectionCard
        title="Ledger snapshot"
        eyebrow="Persisted balances"
        icon={<Wallet className="h-5 w-5" />}
        actions={
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              onClick={refreshAll}
              className="inline-flex items-center gap-2 rounded-2xl border border-slate-700 bg-slate-950/70 px-4 py-2 text-sm text-slate-200 transition hover:border-slate-600 hover:text-white"
            >
              <RefreshCw className={`h-4 w-4 ${ledgerFetching ? 'animate-spin' : ''}`} />
              Refresh
            </button>

            <Link
              to="/trade-history"
              className="inline-flex items-center gap-2 rounded-2xl border border-cyan-700 bg-cyan-500/10 px-4 py-2 text-sm text-cyan-100 transition hover:bg-cyan-500/20"
            >
              Full trade history
              <ArrowRight className="h-4 w-4" />
            </Link>
          </div>
        }
      >
        {ledgerError ? (
          <EmptyState message={ledgerError instanceof Error ? ledgerError.message : 'Paper ledger request failed.'} />
        ) : ledgerLoading ? (
          <EmptyState message="Loading persisted crypto paper ledger." />
        ) : (
          <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
            <div className="space-y-3 rounded-2xl border border-slate-800 bg-slate-950/60 p-4">
              <DetailRow label="Starting balance" value={formatMoney(ledger?.startingBalance ?? 0)} />
              <DetailRow label="Current cash" value={formatMoney(ledger?.balance ?? 0)} />
              <DetailRow label="Current equity" value={formatMoney(ledger?.equity ?? 0)} />
              <DetailRow label="Current market value" value={formatMoney(ledger?.marketValue ?? 0)} />
            </div>

            <div className="space-y-3 rounded-2xl border border-slate-800 bg-slate-950/60 p-4">
              <DetailRow label="Realized PnL" value={formatMoney(realizedPnL)} />
              <DetailRow label="Open PnL estimate" value={formatMoney(unrealizedPnL)} />
              <DetailRow label="Net PnL" value={formatMoney(ledger?.netPnL ?? ledger?.totalPnL ?? 0)} />
              <DetailRow label="Return" value={formatPercent(ledger?.returnPct ?? null)} />
            </div>
          </div>
        )}
      </SectionCard>

      <SectionCard title="Admin / reset controls" eyebrow="Authenticated operator actions" icon={<AlertTriangle className="h-5 w-5" />}>
        <div className="space-y-4">
          <div className="rounded-2xl border border-slate-800 bg-slate-950/60 p-4 text-sm text-slate-300">
            These controls affect only the crypto PAPER ledger. Stock PAPER and any live records stay untouched.
          </div>

          {adminMessage ? <div className="rounded-2xl border border-emerald-800/70 bg-emerald-500/10 px-4 py-3 text-sm text-emerald-100">{adminMessage}</div> : null}
          {adminError ? <div className="rounded-2xl border border-rose-800/70 bg-rose-500/10 px-4 py-3 text-sm text-rose-100">{adminError}</div> : null}

          <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,1.2fr)_minmax(320px,0.8fr)]">
            <div className="space-y-4">
              <div className="rounded-2xl border border-slate-800 bg-slate-950/60 p-4">
                <div className="text-sm font-semibold text-slate-100">Set crypto paper cash balance</div>
                <div className="mt-1 text-sm text-slate-400">Reset available crypto paper cash without touching stock lanes.</div>
                <div className="mt-4 flex flex-col gap-3 sm:flex-row">
                  <input
                    type="number"
                    min="0"
                    step="0.01"
                    value={cashBalanceInput}
                    onChange={(event) => setCashBalanceInput(event.target.value)}
                    className="w-full rounded-2xl border border-slate-700 bg-slate-950 px-4 py-2 text-sm text-slate-100 outline-none transition focus:border-cyan-500"
                    placeholder="100000"
                  />
                  <button
                    type="button"
                    disabled={adminBusy}
                    onClick={() => {
                      try {
                        setCashMutation.mutate(parseCashInput())
                      } catch (error) {
                        handleAdminError(error as Error)
                      }
                    }}
                    className="rounded-2xl bg-cyan-500/90 px-4 py-2 text-sm font-semibold text-slate-950 transition hover:bg-cyan-400 disabled:cursor-not-allowed disabled:opacity-60"
                  >
                    Set Cash
                  </button>
                </div>
              </div>

              <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
                <AdminActionCard
                  title="Cancel pending crypto paper orders"
                  description="Cancel open internal crypto paper orders and intents if they exist."
                  buttonLabel="Cancel Pending"
                  buttonClassName="bg-cyan-500/90 text-slate-950 hover:bg-cyan-400"
                  disabled={adminBusy}
                  onClick={() => {
                    if (!window.confirm('Cancel pending crypto paper orders and intents?')) return
                    cancelPendingMutation.mutate()
                  }}
                />

                <AdminActionCard
                  title="Flatten all crypto paper positions"
                  description="Close every open crypto paper position and clear related crypto monitor state."
                  buttonLabel="Flatten Positions"
                  buttonClassName="bg-amber-500/90 text-slate-950 hover:bg-amber-400"
                  disabled={adminBusy}
                  onClick={() => {
                    if (!window.confirm('Flatten all open crypto paper positions?')) return
                    flattenPositionsMutation.mutate()
                  }}
                />

                <AdminActionCard
                  title="Delete crypto paper trade history"
                  description="Delete crypto paper trade, order, and intent history once positions are flat."
                  buttonLabel="Delete History"
                  buttonClassName="bg-rose-500/90 text-white hover:bg-rose-400"
                  disabled={adminBusy || !typedConfirmationOk}
                  onClick={() => {
                    if (!typedConfirmationOk) {
                      handleAdminError(new Error(`Type ${RESET_CONFIRMATION_TEXT} to enable history deletion.`))
                      return
                    }
                    if (!window.confirm('Delete crypto paper trade and order history? This cannot be undone.')) return
                    deleteHistoryMutation.mutate()
                  }}
                />

                <AdminActionCard
                  title="Start fresh crypto paper account"
                  description="Run the full reset sequence, then set cash to the operator-supplied balance."
                  buttonLabel="Fresh Start"
                  buttonClassName="bg-rose-500/90 text-white hover:bg-rose-400"
                  disabled={adminBusy || !typedConfirmationOk}
                  onClick={() => {
                    if (!typedConfirmationOk) {
                      handleAdminError(new Error(`Type ${RESET_CONFIRMATION_TEXT} to enable fresh start.`))
                      return
                    }
                    if (!window.confirm('Run the full crypto paper fresh-start reset sequence?')) return
                    try {
                      freshStartMutation.mutate(parseCashInput())
                    } catch (error) {
                      handleAdminError(error as Error)
                    }
                  }}
                />
              </div>
            </div>

            <div className="rounded-2xl border border-amber-900/50 bg-amber-500/5 p-4">
              <div className="text-sm font-semibold text-amber-100">Destructive confirmation</div>
              <div className="mt-1 text-sm text-amber-200/80">Type the phrase below before deleting history or starting fresh.</div>
              <div className="mt-4 rounded-2xl border border-slate-800 bg-slate-950/70 px-4 py-3 text-sm font-semibold tracking-[0.22em] text-white">
                {RESET_CONFIRMATION_TEXT}
              </div>
              <input
                type="text"
                value={typedConfirmation}
                onChange={(event) => setTypedConfirmation(event.target.value)}
                className="mt-4 w-full rounded-2xl border border-slate-700 bg-slate-950 px-4 py-2 text-sm text-slate-100 outline-none transition focus:border-amber-500"
                placeholder={RESET_CONFIRMATION_TEXT}
              />
              <div className={`mt-3 text-sm ${typedConfirmationOk ? 'text-emerald-300' : 'text-slate-400'}`}>
                {typedConfirmationOk ? 'Typed confirmation accepted.' : 'History delete and fresh start stay locked until the phrase matches exactly.'}
              </div>
              <div className="mt-4 rounded-2xl border border-slate-800 bg-slate-950/70 px-4 py-3 text-sm text-slate-300">
                {formatAdminSummary(lastAdminResult)}
              </div>
            </div>
          </div>
        </div>
      </SectionCard>

      <SectionCard title="Open crypto positions" eyebrow="Live paper inventory" icon={<Bitcoin className="h-5 w-5" />}>
        {ledgerError ? (
          <EmptyState message={ledgerError instanceof Error ? ledgerError.message : 'Paper ledger request failed.'} />
        ) : ledgerLoading ? (
          <EmptyState message="Loading open ledger positions." />
        ) : positions.length === 0 ? (
          <EmptyState message="No open paper-crypto positions are sitting in the ledger right now." />
        ) : (
          <div className="overflow-x-auto">
            <table className="min-w-full divide-y divide-slate-800 text-sm">
              <thead>
                <tr className="text-left text-xs uppercase tracking-[0.2em] text-slate-400">
                  <th className="px-3 py-3">Pair</th>
                  <th className="px-3 py-3">Entry ET</th>
                  <th className="px-3 py-3">Quantity</th>
                  <th className="px-3 py-3">Avg entry</th>
                  <th className="px-3 py-3">Current</th>
                  <th className="px-3 py-3">Market value</th>
                  <th className="px-3 py-3">PnL</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-900/80">
                {positions.map((row) => (
                  <tr key={`${row.pair}-${row.entryTimeUtc ?? 'open'}`} className="align-top text-slate-200">
                    <td className="px-3 py-4 font-medium text-white">{row.pair}</td>
                    <td className="px-3 py-4 text-slate-300">{formatEt(row.entryTimeUtc)}</td>
                    <td className="px-3 py-4 text-slate-300">{formatQuantity(row.amount)}</td>
                    <td className="px-3 py-4 text-slate-300">{formatMoney(row.avgPrice)}</td>
                    <td className="px-3 py-4 text-slate-300">{formatMoney(row.currentPrice)}</td>
                    <td className="px-3 py-4 text-slate-300">{formatMoney(row.marketValue)}</td>
                    <td className="px-3 py-4">
                      <div className={toneTextClass(toneFromValue(row.pnl))}>{formatMoney(row.pnl)}</div>
                      <div className={`text-xs ${toneTextClass(toneFromValue(row.pnlPercent))}`}>{formatPercent(row.pnlPercent)}</div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </SectionCard>

      <SectionCard title="Recent fills" eyebrow="Execution tape" icon={<ArrowRightLeft className="h-5 w-5" />}>
        {tradesError ? (
          <EmptyState message={tradesError instanceof Error ? tradesError.message : 'Crypto history request failed.'} />
        ) : tradesLoading ? (
          <EmptyState message="Loading recent crypto fills." />
        ) : trades.length === 0 ? (
          <EmptyState message="No crypto fill history has been recorded yet." />
        ) : (
          <div className="overflow-x-auto">
            <table className="min-w-full divide-y divide-slate-800 text-sm">
              <thead>
                <tr className="text-left text-xs uppercase tracking-[0.2em] text-slate-400">
                  <th className="px-3 py-3">Time ET</th>
                  <th className="px-3 py-3">Pair</th>
                  <th className="px-3 py-3">Side</th>
                  <th className="px-3 py-3">Quantity</th>
                  <th className="px-3 py-3">Price</th>
                  <th className="px-3 py-3">Total</th>
                  <th className="px-3 py-3">Status</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-900/80">
                {trades.map((trade) => (
                  <tr key={trade.id} className="align-top text-slate-200">
                    <td className="px-3 py-4 text-slate-300">{formatEt(trade.timestamp)}</td>
                    <td className="px-3 py-4 font-medium text-white">{trade.pair ?? trade.symbol ?? '—'}</td>
                    <td className="px-3 py-4">
                      <StatusPill tone={tradeTone(trade.side)} label={trade.side} />
                    </td>
                    <td className="px-3 py-4 text-slate-300">{formatQuantity(trade.amount)}</td>
                    <td className="px-3 py-4 text-slate-300">{formatMoney(trade.price)}</td>
                    <td className="px-3 py-4 text-slate-300">{formatMoney(trade.total)}</td>
                    <td className="px-3 py-4 text-slate-300">{trade.status}</td>
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

function AdminActionCard({
  title,
  description,
  buttonLabel,
  buttonClassName,
  disabled,
  onClick,
}: {
  title: string
  description: string
  buttonLabel: string
  buttonClassName: string
  disabled: boolean
  onClick: () => void
}) {
  return (
    <div className="rounded-2xl border border-slate-800 bg-slate-950/60 p-4">
      <div className="text-sm font-semibold text-slate-100">{title}</div>
      <div className="mt-1 text-sm leading-6 text-slate-400">{description}</div>
      <button
        type="button"
        disabled={disabled}
        onClick={onClick}
        className={`mt-4 rounded-2xl px-4 py-2 text-sm font-semibold transition disabled:cursor-not-allowed disabled:opacity-60 ${buttonClassName}`}
      >
        {buttonLabel}
      </button>
    </div>
  )
}
