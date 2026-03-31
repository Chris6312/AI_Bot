import { useMemo } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { formatDistanceToNow, format } from 'date-fns'
import { BrainCircuit, ClipboardList, FileSearch, Siren, Shield, TrendingUp } from 'lucide-react'

import { api } from '@/lib/api'
import {
  DetailRow,
  EmptyState,
  MetricCard,
  PageHero,
  SectionCard,
  StatusPill,
  ToneBadge,
  getStatusMeta,
  type Tone,
} from '@/components/operator-ui'
import type {
  AIDecision,
  OrderIntentRecord,
  RuntimeVisibility,
  TradeHistoryEntry,
  WatchlistUploadRecord,
} from '@/types'

type AuditLane = 'all' | 'receipt' | 'gate' | 'stock' | 'crypto' | 'ai'

type AuditEvent = {
  id: string
  lane: Exclude<AuditLane, 'all'>
  title: string
  subtitle: string
  detail: string
  timestamp: string | null
  tone: Tone
  statusLabel: string
  to: string
  symbol?: string | null
  scope?: 'stocks_only' | 'crypto_only' | null
}

const scopeLabels = {
  stocks_only: 'Stocks',
  crypto_only: 'Crypto',
} as const

function formatRelative(value?: string | null) {
  if (!value) return '—'
  return formatDistanceToNow(new Date(value), { addSuffix: true })
}

function formatTimestamp(value?: string | null) {
  if (!value) return '—'
  return format(new Date(value), 'MMM dd, yyyy HH:mm')
}

function isHealthyValidationStatus(status?: string | null) {
  const normalized = (status ?? '').trim().toLowerCase()
  return normalized === 'accepted' || normalized === 'valid'
}

function gateTone(allowed: boolean): Tone {
  return allowed ? 'good' : 'danger'
}

function buildLaneHref(base: string, options: { lane?: Exclude<AuditLane, 'all'> | AuditLane; symbol?: string | null; scope?: 'stocks_only' | 'crypto_only' | null } = {}) {
  const params = new URLSearchParams()
  if (options.lane && options.lane !== 'all') params.set('lane', options.lane)
  if (options.symbol) params.set('symbol', options.symbol)
  if (options.scope) params.set('scope', options.scope)
  const query = params.toString()
  return query ? `${base}?${query}` : base
}

function decisionScope(decision: { assetClass?: string | null } | { market?: string | null }): 'stocks_only' | 'crypto_only' | null {
  const assetClass = 'assetClass' in decision ? String(decision.assetClass ?? '').toLowerCase() : ''
  const market = 'market' in decision ? String(decision.market ?? '').toUpperCase() : ''
  if (assetClass === 'stock' || market === 'STOCK') return 'stocks_only'
  if (assetClass === 'crypto' || market === 'CRYPTO') return 'crypto_only'
  return null
}

export default function AuditTrail() {
  const [searchParams, setSearchParams] = useSearchParams()
  const laneParam = searchParams.get('lane')
  const lane = (['all', 'receipt', 'gate', 'stock', 'crypto', 'ai'] as AuditLane[]).includes((laneParam as AuditLane) ?? 'all') ? ((laneParam as AuditLane) || 'all') : 'all'
  const symbolFilter = (searchParams.get('symbol') ?? '').trim().toUpperCase()
  const scopeFilter = (searchParams.get('scope') ?? '').trim()

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

  const { data: runtimeVisibility } = useQuery<RuntimeVisibility>({
    queryKey: ['runtimeVisibility'],
    queryFn: () => api.getRuntimeVisibility(12),
    refetchInterval: 10000,
  })

  const { data: stockHistory = [] } = useQuery<OrderIntentRecord[]>({
    queryKey: ['stockHistory'],
    queryFn: () => api.getStockHistory(30),
    refetchInterval: 10000,
  })

  const { data: cryptoHistory = [] } = useQuery<TradeHistoryEntry[]>({
    queryKey: ['cryptoHistory'],
    queryFn: () => api.getCryptoHistory(30),
    refetchInterval: 10000,
  })

  const { data: aiDecisions = [] } = useQuery<AIDecision[]>({
    queryKey: ['aiDecisions'],
    queryFn: () => api.getAIDecisions(20),
    refetchInterval: 15000,
  })

  const events = useMemo<AuditEvent[]>(() => {
    const receiptEvents: AuditEvent[] = [stockWatchlist, cryptoWatchlist]
      .filter((record): record is WatchlistUploadRecord => Boolean(record))
      .map((record) => ({
        id: `receipt-${record.uploadId}`,
        lane: 'receipt',
        title: `${scopeLabels[record.scope]} watchlist received`,
        subtitle: `${record.provider} · ${record.validationStatus}`,
        detail: `${record.selectedCount} selected, ${record.statusSummary.managedOnlyCount} managed-only, hash ${record.payloadHash?.slice(0, 12) ?? '—'}`,
        timestamp: record.receivedAtUtc ?? null,
        tone: isHealthyValidationStatus(record.validationStatus) ? 'good' : 'warn',
        statusLabel: getStatusMeta(record.validationStatus).canonicalLabel,
        to: buildLaneHref('/watchlists', { scope: record.scope }),
        scope: record.scope,
      }))

    const gateEvents: AuditEvent[] = (runtimeVisibility?.gate.recent ?? []).map((decision) => ({
      id: `gate-${decision.recordedAtUtc}-${decision.symbol}-${decision.state}`,
      lane: 'gate',
      title: `${decision.symbol} gate ${decision.allowed ? 'opened' : 'blocked'}`,
      subtitle: `${decision.assetClass} · ${decision.executionSource}`,
      detail: decision.rejectionReason || 'Gate allowed the request to continue.',
      timestamp: decision.recordedAtUtc,
      tone: gateTone(decision.allowed),
      statusLabel: getStatusMeta(decision.state).canonicalLabel,
      to: buildLaneHref('/monitoring', { scope: decisionScope(decision), symbol: decision.symbol }),
      symbol: decision.symbol,
      scope: decisionScope(decision),
    }))

    const stockEvents: AuditEvent[] = stockHistory.map((row) => ({
      id: `stock-${row.intentId}`,
      lane: 'stock',
      title: `${row.symbol} ${row.side}`,
      subtitle: `${row.status} · qty ${row.requestedQuantity}`,
      detail: row.rejectionReason ?? row.events[row.events.length - 1]?.message ?? 'Lifecycle record stored.',
      timestamp: row.lastFillAt ?? row.submittedAt ?? null,
      tone: row.status.toUpperCase().includes('REJECT') ? 'danger' : row.status.toUpperCase().includes('FILL') ? 'good' : 'info',
      statusLabel: getStatusMeta(row.status).canonicalLabel,
      to: buildLaneHref('/monitoring', { scope: 'stocks_only', symbol: row.symbol }),
      symbol: row.symbol,
      scope: 'stocks_only',
    }))

    const cryptoEvents: AuditEvent[] = cryptoHistory.map((row) => ({
      id: `crypto-${row.id}`,
      lane: 'crypto',
      title: `${row.pair ?? row.symbol ?? 'Unknown pair'} ${row.side}`,
      subtitle: `${row.status} · total ${row.total ?? 0}`,
      detail: `Amount ${row.amount ?? row.shares ?? 0} at ${row.price ?? 0}`,
      timestamp: row.timestamp,
      tone: row.status.toUpperCase().includes('REJECT') ? 'danger' : row.side === 'SELL' ? 'warn' : 'good',
      statusLabel: getStatusMeta(row.status).canonicalLabel,
      to: buildLaneHref('/monitoring', { scope: 'crypto_only', symbol: row.symbol ?? row.pair ?? undefined }),
      symbol: row.symbol ?? row.pair ?? null,
      scope: 'crypto_only',
    }))

    const aiEvents: AuditEvent[] = aiDecisions.map((row) => ({
      id: `ai-${row.id}`,
      lane: 'ai',
      title: `${row.symbol} ${row.type}`,
      subtitle: `${row.market} · confidence ${Math.round(row.confidence * 100)}%`,
      detail: row.rejectionReason ?? row.reasoning,
      timestamp: row.timestamp,
      tone: row.rejected ? 'danger' : row.executed ? 'good' : 'info',
      statusLabel: row.rejected ? 'Blocked' : row.executed ? 'Healthy' : 'Warning',
      to: buildLaneHref('/watchlists', { scope: decisionScope(row), symbol: row.symbol }),
      symbol: row.symbol,
      scope: decisionScope(row),
    }))

    return [...receiptEvents, ...gateEvents, ...stockEvents, ...cryptoEvents, ...aiEvents].sort((a, b) => {
      const aTime = a.timestamp ? new Date(a.timestamp).getTime() : 0
      const bTime = b.timestamp ? new Date(b.timestamp).getTime() : 0
      return bTime - aTime
    })
  }, [aiDecisions, cryptoHistory, cryptoWatchlist, runtimeVisibility?.gate.recent, stockHistory, stockWatchlist])

  const filteredEvents = events.filter((event) => {
    if (lane !== 'all' && event.lane !== lane) return false
    if (symbolFilter && String(event.symbol ?? '').toUpperCase() != symbolFilter) return false
    if (scopeFilter && String(event.scope ?? '') != scopeFilter && event.scope) return false
    return true
  })
  const latestAllowed = runtimeVisibility?.gate.summary.lastAllowed
  const latestRejected = runtimeVisibility?.gate.summary.lastRejected

  return (
    <div className="space-y-6">
      <PageHero
        eyebrow={
          <>
            <FileSearch className="h-4 w-4" />
            Audit trail
          </>
        }
        title="Receipts, gate decisions, and execution breadcrumbs"
        description="This page gathers the evidence we actually persist right now: watchlist receipts, gate outcomes, stock lifecycle records, crypto paper tape, and the derived AI watchlist feed built from stored uploads."
        aside={
          <>
            <StatusPill tone={stockWatchlist ? 'good' : 'warn'} label={stockWatchlist ? 'Stock receipt present' : 'No stock receipt'} />
            <StatusPill tone={cryptoWatchlist ? 'good' : 'warn'} label={cryptoWatchlist ? 'Crypto receipt present' : 'No crypto receipt'} />
            <StatusPill tone={(runtimeVisibility?.gate.summary.rejectedCount ?? 0) > 0 ? 'warn' : 'good'} label={`${runtimeVisibility?.gate.summary.rejectedCount ?? 0} gate rejections`} />
            {symbolFilter ? <StatusPill tone="info" label={`Filtered: ${symbolFilter}`} /> : null}
          </>
        }
      />

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
        <MetricCard label="Watchlist receipts" value={String([stockWatchlist, cryptoWatchlist].filter(Boolean).length)} detail="Latest stock + crypto uploads" icon={<ClipboardList className="h-5 w-5" />} />
        <MetricCard label="Recent gate decisions" value={String(runtimeVisibility?.gate.summary.total ?? 0)} detail={`${runtimeVisibility?.gate.summary.rejectedCount ?? 0} rejected`} icon={<Shield className="h-5 w-5" />} />
        <MetricCard label="Stock lifecycle records" value={String(stockHistory.length)} detail="Order intents with event trails" icon={<TrendingUp className="h-5 w-5" />} />
        <MetricCard label="AI decision feed" value={String(aiDecisions.length)} detail={aiDecisions.length > 0 ? 'Derived from stored watchlist uploads' : 'No derived decisions stored yet'} icon={<BrainCircuit className="h-5 w-5" />} />
      </div>

      <div className="grid grid-cols-1 gap-6 xl:grid-cols-[minmax(0,1.35fr)_minmax(340px,0.95fr)]">
        <SectionCard
          title="Unified event river"
          eyebrow="Evidence stream"
          icon={<FileSearch className="h-4 w-4 text-cyan-300" />}
          actions={
            (['all', 'receipt', 'gate', 'stock', 'crypto', 'ai'] as AuditLane[]).map((option) => (
              <button
                key={option}
                onClick={() => {
                  const next = new URLSearchParams(searchParams)
                  if (option === 'all') next.delete('lane')
                  else next.set('lane', option)
                  setSearchParams(next)
                }}
                className={`rounded-full px-3 py-2 text-sm transition ${lane === option ? 'border border-cyan-700 bg-cyan-500/10 text-cyan-200' : 'border border-slate-700 bg-slate-950/60 text-slate-300 hover:border-slate-600'}`}
              >
                {option === 'all' ? 'All lanes' : option}
              </button>
            ))
          }
        >
          <div className="space-y-3">
            {filteredEvents.length === 0 ? (
              <EmptyState message="No audit events match this filter yet." />
            ) : (
              filteredEvents.slice(0, 24).map((event) => (
                <div key={event.id} className="rounded-2xl border border-slate-800 bg-slate-950/60 p-4">
                  <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                    <div>
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="text-base font-semibold text-white">{event.title}</span>
                        <ToneBadge tone={event.tone}>{event.statusLabel}</ToneBadge>
                        <ToneBadge tone="muted">{event.lane}</ToneBadge>
                      </div>
                      <div className="mt-2 text-sm text-slate-400">{event.subtitle}</div>
                    </div>
                    <div className="text-sm text-slate-500">{formatRelative(event.timestamp)}</div>
                  </div>
                  <div className="mt-3 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                    <div className="text-sm text-slate-300">{event.detail}</div>
                    <Link to={event.to} className="inline-flex items-center gap-2 rounded-full border border-slate-700 bg-slate-900/70 px-3 py-1.5 text-xs text-slate-200 transition hover:border-cyan-700 hover:text-white">
                      Open lane
                    </Link>
                  </div>
                </div>
              ))
            )}
          </div>
        </SectionCard>

        <div className="space-y-6">
          <ReceiptCard title="Stock watchlist receipt" record={stockWatchlist} />
          <ReceiptCard title="Crypto watchlist receipt" record={cryptoWatchlist} />

          <SectionCard title="Gate snapshot" eyebrow="Control evidence" icon={<Shield className="h-4 w-4 text-emerald-300" />}>
            <div className="space-y-3 text-sm text-slate-400">
              <DetailRow label="Allowed count" value={String(runtimeVisibility?.gate.summary.allowedCount ?? 0)} tone="good" />
              <DetailRow label="Rejected count" value={String(runtimeVisibility?.gate.summary.rejectedCount ?? 0)} tone={(runtimeVisibility?.gate.summary.rejectedCount ?? 0) > 0 ? 'warn' : 'muted'} />
              <DetailRow label="Last allowed" value={latestAllowed?.symbol ?? '—'} tone="good" />
              <DetailRow label="Last rejected" value={latestRejected?.symbol ?? '—'} tone={latestRejected ? 'danger' : 'muted'} />
              <DetailRow label="Captured" value={formatTimestamp(runtimeVisibility?.capturedAtUtc)} />
            </div>
          </SectionCard>

          <SectionCard title="Gaps still visible" eyebrow="Known holes" icon={<Siren className="h-4 w-4 text-amber-300" />}>
            <div className="space-y-3 text-sm text-slate-400">
              <DetailRow label="AI decisions feed" value={aiDecisions.length > 0 ? 'Present' : 'No derived entries yet'} tone={aiDecisions.length > 0 ? 'good' : 'warn'} />
              <DetailRow label="Replay rejection stream" value="Not yet exposed in frontend API" tone="warn" />
              <DetailRow label="System error timeline" value="Not yet exposed in frontend API" tone="warn" />
              <DetailRow label="Exit decision timeline" value="Partially visible through stock lifecycle and tape" tone="info" />
            </div>
          </SectionCard>
        </div>
      </div>
    </div>
  )
}

function ReceiptCard({ title, record }: { title: string; record: WatchlistUploadRecord | null | undefined }) {
  return (
    <SectionCard title={title} eyebrow="Receipt" icon={<ClipboardList className="h-4 w-4 text-cyan-300" />}>
      {!record ? (
        <EmptyState message="No receipt found for this scope yet." />
      ) : (
        <div className="space-y-3 text-sm text-slate-400">
          <DetailRow label="Provider" value={record.provider} />
          <DetailRow label="Scope" value={scopeLabels[record.scope]} />
          <DetailRow label="Validation" value={record.validationStatus} tone={isHealthyValidationStatus(record.validationStatus) ? 'good' : 'warn'} />
          <DetailRow label="Generated" value={formatTimestamp(record.generatedAtUtc)} />
          <DetailRow label="Received" value={formatTimestamp(record.receivedAtUtc)} />
          <DetailRow label="Payload hash" value={record.payloadHash ? `${record.payloadHash.slice(0, 12)}…` : '—'} />
          <DetailRow label="Upload id" value={record.uploadId.slice(0, 12)} />
        </div>
      )}
    </SectionCard>
  )
}
