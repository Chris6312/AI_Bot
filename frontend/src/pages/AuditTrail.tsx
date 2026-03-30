import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { format, formatDistanceToNow } from 'date-fns'
import { BrainCircuit, ClipboardList, FileSearch, Shield, Siren, TrendingUp } from 'lucide-react'

import { api } from '@/lib/api'
import type {
  AIDecision,
  OrderIntentRecord,
  RuntimeVisibility,
  TradeHistoryEntry,
  WatchlistScope,
  WatchlistUploadRecord,
} from '@/types'

type AuditEvent = {
  id: string
  timestamp: string | null
  lane: 'watchlist' | 'gate' | 'order' | 'crypto' | 'ai'
  title: string
  subtitle: string
  detail: string
  tone: 'good' | 'warn' | 'danger' | 'info' | 'muted'
}

const scopeLabels: Record<WatchlistScope, string> = {
  stocks_only: 'Stocks',
  crypto_only: 'Crypto',
}

function formatTimestamp(value?: string | null) {
  if (!value) return '—'
  return format(new Date(value), 'MMM dd, yyyy HH:mm:ss')
}

function isHealthyValidationStatus(status?: string | null) {
  const normalized = (status ?? '').trim().toLowerCase()
  return normalized === 'accepted' || normalized === 'valid'
}

function formatRelative(value?: string | null) {
  if (!value) return '—'
  return formatDistanceToNow(new Date(value), { addSuffix: true })
}

export default function AuditTrail() {
  const { data: runtimeVisibility } = useQuery<RuntimeVisibility>({
    queryKey: ['runtimeVisibility'],
    queryFn: () => api.getRuntimeVisibility(12),
    refetchInterval: 10000,
  })

  const { data: stockWatchlist } = useQuery<WatchlistUploadRecord | null>({
    queryKey: ['latestWatchlist', 'stocks_only'],
    queryFn: () => api.getLatestWatchlist('stocks_only'),
    refetchInterval: 10000,
  })

  const { data: cryptoWatchlist } = useQuery<WatchlistUploadRecord | null>({
    queryKey: ['latestWatchlist', 'crypto_only'],
    queryFn: () => api.getLatestWatchlist('crypto_only'),
    refetchInterval: 10000,
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

  const { data: aiDecisions = [] } = useQuery<AIDecision[]>({
    queryKey: ['aiDecisions'],
    queryFn: () => api.getAIDecisions(50),
    refetchInterval: 10000,
  })

  const events = useMemo<AuditEvent[]>(() => {
    const watchlistEvents = [stockWatchlist, cryptoWatchlist]
      .filter((record): record is WatchlistUploadRecord => Boolean(record))
      .map((record) => ({
        id: `watchlist-${record.uploadId}`,
        timestamp: record.receivedAtUtc ?? record.generatedAtUtc ?? null,
        lane: 'watchlist' as const,
        title: `${scopeLabels[record.scope]} watchlist ${record.validationStatus.toLowerCase()}`,
        subtitle: `${record.provider} · ${record.selectedCount} symbols`,
        detail: record.payloadHash ? `Payload ${record.payloadHash.slice(0, 12)}…` : 'Payload hash unavailable',
        tone: isHealthyValidationStatus(record.validationStatus) ? ('good' as const) : ('warn' as const),
      }))

    const gateEvents = (runtimeVisibility?.gate.recent ?? []).map((decision) => ({
      id: `gate-${decision.recordedAtUtc}-${decision.symbol}-${decision.state}`,
      timestamp: decision.recordedAtUtc,
      lane: 'gate' as const,
      title: `${decision.symbol} ${decision.allowed ? 'allowed' : 'rejected'}`,
      subtitle: `${decision.executionSource} · ${decision.assetClass}`,
      detail: decision.rejectionReason || decision.state,
      tone: decision.allowed ? ('good' as const) : ('danger' as const),
    }))

    const orderEvents = stockHistory.map((record) => ({
      id: `order-${record.intentId}`,
      timestamp: record.lastFillAt ?? record.firstFillAt ?? record.submittedAt ?? null,
      lane: 'order' as const,
      title: `${record.symbol} ${record.side} ${record.status.toLowerCase()}`,
      subtitle: `${record.executionSource} · qty ${record.requestedQuantity.toFixed(2)}`,
      detail: record.rejectionReason || (record.events[record.events.length - 1]?.message ?? 'No order-event detail recorded.'),
      tone:
        record.status === 'FILLED'
          ? ('good' as const)
          : record.status === 'REJECTED'
            ? ('danger' as const)
            : ('info' as const),
    }))

    const cryptoEvents = cryptoHistory.map((record) => ({
      id: `crypto-${record.id}`,
      timestamp: record.timestamp,
      lane: 'crypto' as const,
      title: `${record.pair ?? record.symbol ?? 'Pair'} ${record.side.toLowerCase()}`,
      subtitle: `${record.status} · ${record.amount?.toFixed(6) ?? '—'} units`,
      detail: record.total != null ? `Notional ${record.total.toFixed(2)}` : 'Notional unavailable',
      tone: record.status === 'FILLED' ? ('good' as const) : record.status === 'REJECTED' ? ('danger' as const) : ('info' as const),
    }))

    const aiEvents = aiDecisions.map((decision) => ({
      id: `ai-${decision.id}`,
      timestamp: decision.timestamp,
      lane: 'ai' as const,
      title: `${decision.symbol} ${decision.type.toLowerCase()}`,
      subtitle: `${decision.market} · ${(decision.confidence * 100).toFixed(0)}% confidence`,
      detail: decision.rejectionReason || decision.reasoning,
      tone: decision.executed ? ('good' as const) : decision.rejected ? ('danger' as const) : ('warn' as const),
    }))

    return [...watchlistEvents, ...gateEvents, ...orderEvents, ...cryptoEvents, ...aiEvents].sort((left, right) => {
      const leftTime = left.timestamp ? new Date(left.timestamp).getTime() : 0
      const rightTime = right.timestamp ? new Date(right.timestamp).getTime() : 0
      return rightTime - leftTime
    })
  }, [aiDecisions, cryptoHistory, cryptoWatchlist, runtimeVisibility?.gate.recent, stockHistory, stockWatchlist])

  return (
    <div className="space-y-6">
      <header className="rounded-3xl border border-slate-800 bg-slate-900/70 p-6 shadow-2xl shadow-slate-950/30">
        <div className="flex flex-col gap-4 xl:flex-row xl:items-end xl:justify-between">
          <div>
            <div className="mb-2 flex items-center gap-2 text-sm font-medium uppercase tracking-[0.22em] text-cyan-300">
              <FileSearch className="h-4 w-4" />
              Audit trail
            </div>
            <h1 className="text-3xl font-semibold text-white">Receipts, gate decisions, and execution breadcrumbs</h1>
            <p className="mt-2 max-w-3xl text-sm leading-6 text-slate-400">
              This page stitches together the evidence we currently persist: watchlist receipts, recent gate outcomes, stock order lifecycle events, and the
              crypto paper tape. It also calls out the holes still waiting on backend plumbing.
            </p>
          </div>

          <div className="flex flex-wrap gap-3">
            <Pill tone={stockWatchlist ? 'good' : 'warn'} label={stockWatchlist ? 'Stock receipt present' : 'No stock receipt'} />
            <Pill tone={cryptoWatchlist ? 'good' : 'warn'} label={cryptoWatchlist ? 'Crypto receipt present' : 'No crypto receipt'} />
            <Pill tone={(runtimeVisibility?.gate.summary.rejectedCount ?? 0) > 0 ? 'warn' : 'good'} label={`${runtimeVisibility?.gate.summary.rejectedCount ?? 0} gate rejections`} />
          </div>
        </div>
      </header>

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
        <MetricCard label="Watchlist receipts" value={String([stockWatchlist, cryptoWatchlist].filter(Boolean).length)} detail="Latest stock + crypto uploads" icon={<ClipboardList className="h-5 w-5" />} />
        <MetricCard label="Recent gate decisions" value={String(runtimeVisibility?.gate.summary.total ?? 0)} detail={`${runtimeVisibility?.gate.summary.rejectedCount ?? 0} rejected`} icon={<Shield className="h-5 w-5" />} />
        <MetricCard label="Stock lifecycle records" value={String(stockHistory.length)} detail="Order intents with event trails" icon={<TrendingUp className="h-5 w-5" />} />
        <MetricCard label="AI decision feed" value={String(aiDecisions.length)} detail={aiDecisions.length > 0 ? 'Decision feed alive' : 'Backend placeholder still empty'} icon={<BrainCircuit className="h-5 w-5" />} />
      </div>

      <div className="grid grid-cols-1 gap-6 xl:grid-cols-[minmax(0,1.55fr)_minmax(340px,0.95fr)]">
        <section className="rounded-3xl border border-slate-800 bg-slate-900/70 p-5 shadow-xl shadow-slate-950/20">
          <div className="mb-4 flex items-center gap-2 text-sm font-semibold uppercase tracking-[0.18em] text-slate-400">
            <FileSearch className="h-4 w-4 text-cyan-300" />
            Unified event river
          </div>
          <div className="space-y-3">
            {events.length === 0 ? (
              <EmptyState message="No audit events have been recorded yet." />
            ) : (
              events.slice(0, 20).map((event) => (
                <div key={event.id} className="rounded-2xl border border-slate-800 bg-slate-950/60 p-4">
                  <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                    <div>
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="text-base font-semibold text-white">{event.title}</span>
                        <Pill tone={event.tone} label={event.lane.toUpperCase()} compact />
                      </div>
                      <div className="mt-2 text-sm text-slate-400">{event.subtitle}</div>
                    </div>
                    <div className="text-sm text-slate-500">{formatRelative(event.timestamp)}</div>
                  </div>
                  <div className="mt-3 text-sm text-slate-300">{event.detail}</div>
                </div>
              ))
            )}
          </div>
        </section>

        <div className="space-y-6">
          <ReceiptCard title="Stock watchlist receipt" record={stockWatchlist} />
          <ReceiptCard title="Crypto watchlist receipt" record={cryptoWatchlist} />

          <section className="rounded-3xl border border-slate-800 bg-slate-900/70 p-5 shadow-xl shadow-slate-950/20">
            <div className="mb-4 flex items-center gap-2 text-sm font-semibold uppercase tracking-[0.18em] text-slate-400">
              <Siren className="h-4 w-4 text-amber-300" />
              Gaps still visible
            </div>
            <div className="space-y-3 text-sm text-slate-400">
              <SummaryRow
                label="AI decisions feed"
                value={aiDecisions.length > 0 ? 'Present' : 'Placeholder endpoint still empty'}
                tone={aiDecisions.length > 0 ? 'good' : 'warn'}
              />
              <SummaryRow label="Replay rejection stream" value="Not yet exposed in frontend API" tone="warn" />
              <SummaryRow label="System error timeline" value="Not yet exposed in frontend API" tone="warn" />
              <SummaryRow label="Exit decision timeline" value="Partially visible through stock lifecycle + exit worker summaries" tone="info" />
            </div>
          </section>
        </div>
      </div>

      <section className="rounded-3xl border border-slate-800 bg-slate-900/70 p-5 shadow-xl shadow-slate-950/20">
        <div className="mb-4 flex items-center gap-2 text-sm font-semibold uppercase tracking-[0.18em] text-slate-400">
          <Shield className="h-4 w-4 text-emerald-300" />
          Gate snapshot
        </div>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
          <SummaryTile label="Allowed" value={String(runtimeVisibility?.gate.summary.allowedCount ?? 0)} />
          <SummaryTile label="Rejected" value={String(runtimeVisibility?.gate.summary.rejectedCount ?? 0)} />
          <SummaryTile label="Last decision" value={runtimeVisibility?.gate.summary.lastDecision?.symbol ?? '—'} />
          <SummaryTile label="Captured" value={formatTimestamp(runtimeVisibility?.capturedAtUtc)} />
        </div>
      </section>
    </div>
  )
}

function ReceiptCard({ title, record }: { title: string; record: WatchlistUploadRecord | null | undefined }) {
  return (
    <section className="rounded-3xl border border-slate-800 bg-slate-900/70 p-5 shadow-xl shadow-slate-950/20">
      <div className="mb-4 text-sm font-semibold uppercase tracking-[0.18em] text-slate-400">{title}</div>
      {!record ? (
        <EmptyState message="No receipt found for this scope yet." />
      ) : (
        <div className="space-y-3 text-sm text-slate-400">
          <SummaryRow label="Provider" value={record.provider} />
          <SummaryRow label="Scope" value={scopeLabels[record.scope]} />
          <SummaryRow label="Validation" value={record.validationStatus} tone={isHealthyValidationStatus(record.validationStatus) ? 'good' : 'warn'} />
          <SummaryRow label="Generated" value={formatTimestamp(record.generatedAtUtc)} />
          <SummaryRow label="Received" value={formatTimestamp(record.receivedAtUtc)} />
          <SummaryRow label="Payload hash" value={record.payloadHash ? `${record.payloadHash.slice(0, 12)}…` : '—'} />
          <SummaryRow label="Upload id" value={record.uploadId.slice(0, 12)} />
        </div>
      )}
    </section>
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

function SummaryTile({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-2xl border border-slate-800 bg-slate-950/60 px-4 py-3">
      <div className="text-xs uppercase tracking-wide text-slate-500">{label}</div>
      <div className="mt-1 text-lg font-semibold text-white">{value}</div>
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

function Pill({ label, tone, compact = false }: { label: string; tone: 'good' | 'warn' | 'danger' | 'info' | 'muted'; compact?: boolean }) {
  return <span className={`${compact ? 'px-2 py-1 text-[11px]' : 'px-3 py-2 text-sm'} rounded-full ${toneBadgeClass(tone)}`}>{label}</span>
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
