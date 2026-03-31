import { useEffect, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {
  AlertCircle,
  ArrowRight,
  CalendarClock,
  CheckCircle2,
  ChevronRight,
  Clock3,
  FileJson,
  ShieldAlert,
  Sparkles,
  Target,
  X,
} from 'lucide-react'
import { formatDistanceToNowStrict } from 'date-fns'

import { api } from '@/lib/api'
import {
  EmptyState,
  MetricCard,
  PageHero,
  StatusPill,
  ToneBadge,
  getStatusMeta,
} from '@/components/operator-ui'
import type { WatchlistScope, WatchlistSymbolRecord, WatchlistUploadRecord } from '@/types'

type WatchlistCollection = Partial<Record<WatchlistScope, WatchlistUploadRecord>>

const scopeLabels: Record<WatchlistScope, string> = {
  stocks_only: 'Stocks',
  crypto_only: 'Crypto',
}

export default function Watchlists() {
  const [searchParams, setSearchParams] = useSearchParams()
  const [selectedSymbol, setSelectedSymbol] = useState<{ scope: WatchlistScope; symbol: string } | null>(null)

  const { data: activeWatchlists = {} } = useQuery<WatchlistCollection>({
    queryKey: ['watchlists', 'active'],
    queryFn: () => api.getActiveWatchlists() as Promise<WatchlistCollection>,
    refetchInterval: 15000,
  })

  const { data: latestWatchlists = {} } = useQuery<WatchlistCollection>({
    queryKey: ['watchlists', 'latest'],
    queryFn: () => api.getLatestWatchlists() as Promise<WatchlistCollection>,
    refetchInterval: 15000,
  })


  useEffect(() => {
    const requestedSymbol = (searchParams.get('symbol') ?? '').trim().toUpperCase()
    const requestedScope = (searchParams.get('scope') ?? '').trim() as WatchlistScope | ''
    if (!requestedSymbol) {
      if (selectedSymbol) setSelectedSymbol(null)
      return
    }

    const candidateScopes: WatchlistScope[] = requestedScope && ['stocks_only', 'crypto_only'].includes(requestedScope)
      ? [requestedScope as WatchlistScope]
      : ['stocks_only', 'crypto_only']

    let resolved: { scope: WatchlistScope; symbol: string } | null = null
    for (const scope of candidateScopes) {
      const watchlist = activeWatchlists[scope]
      const activeMatch = watchlist?.symbols.find((row) => row.symbol.toUpperCase() === requestedSymbol)
      const managedMatch = watchlist?.managedOnlySymbols.find((row) => row.symbol.toUpperCase() === requestedSymbol)
      const match = activeMatch ?? managedMatch
      if (match) {
        resolved = { scope, symbol: match.symbol }
        break
      }
    }

    if (!resolved) return
    if (selectedSymbol?.scope === resolved.scope && selectedSymbol.symbol === resolved.symbol) return
    setSelectedSymbol(resolved)
  }, [activeWatchlists, searchParams, selectedSymbol])

  const openSymbol = (scope: WatchlistScope, symbol: string) => {
    setSelectedSymbol({ scope, symbol })
    const next = new URLSearchParams(searchParams)
    next.set('scope', scope)
    next.set('symbol', symbol)
    setSearchParams(next)
  }

  const closeSymbol = () => {
    setSelectedSymbol(null)
    const next = new URLSearchParams(searchParams)
    next.delete('symbol')
    next.delete('scope')
    setSearchParams(next)
  }

  const selectedContext = useMemo(() => {
    if (!selectedSymbol) return null
    const watchlist = activeWatchlists[selectedSymbol.scope]
    if (!watchlist) return null
    return watchlist.uiPayload.symbolContext?.[selectedSymbol.symbol] ?? null
  }, [activeWatchlists, selectedSymbol])

  const activeScopeCount = Object.values(activeWatchlists).filter(Boolean).length
  const latestScopeCount = Object.values(latestWatchlists).filter(Boolean).length
  const managedOnlyCount = Object.values(activeWatchlists).reduce((sum, item) => sum + (item?.managedOnlySymbols.length ?? 0), 0)

  return (
    <>
      <div className="space-y-6">
        <PageHero
          eyebrow={
            <>
              <FileJson className="h-4 w-4" />
              Daily watchlists
            </>
          }
          title="Accepted payloads and operator context"
          description="Machine-safe fields stay in the table, human context stays in the drawer, and the flow into monitoring, positions, and audit stays one click away."
          aside={
            <>
              <StatusPill tone={activeWatchlists.stocks_only ? 'good' : 'warn'} label={activeWatchlists.stocks_only ? 'Stocks healthy' : 'Stocks missing'} />
              <StatusPill tone={activeWatchlists.crypto_only ? 'good' : 'warn'} label={activeWatchlists.crypto_only ? 'Crypto healthy' : 'Crypto missing'} />
              <StatusPill tone={selectedSymbol ? 'info' : 'muted'} label={selectedSymbol ? `Context open: ${selectedSymbol.symbol}` : 'Context drawer idle'} />
            </>
          }
        />

        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
          <MetricCard label="Active scopes" value={String(activeScopeCount)} detail="Scopes with an active payload" icon={<FileJson className="h-5 w-5" />} />
          <MetricCard label="Latest payloads" value={String(latestScopeCount)} detail="Newest uploads across both scopes" icon={<CalendarClock className="h-5 w-5" />} />
          <MetricCard label="Managed-only rows" value={String(managedOnlyCount)} detail="Positions still supervised after a symbol leaves the newest payload" icon={<ShieldAlert className="h-5 w-5" />} />
          <MetricCard label="Operator flow" value={selectedSymbol ? 'Live' : 'Ready'} detail={selectedSymbol ? `Jump links armed for ${selectedSymbol.symbol}` : 'Pick a row to open context and jump lanes'} icon={<Sparkles className="h-5 w-5" />} />
        </div>

        <OperatorCuesCard hasSelectedSymbol={Boolean(selectedSymbol)} />

        <div className="space-y-6">
          {(['stocks_only', 'crypto_only'] as WatchlistScope[]).map((scope) => (
            <ScopePanel
              key={scope}
              scope={scope}
              activeWatchlist={activeWatchlists[scope]}
              latestWatchlist={latestWatchlists[scope]}
              selectedSymbol={selectedSymbol?.scope === scope ? selectedSymbol.symbol : null}
              onSelectSymbol={(symbol) => openSymbol(scope, symbol)}
            />
          ))}
        </div>
      </div>

      <SymbolContextDrawer selectedSymbol={selectedSymbol} context={selectedContext} onClose={closeSymbol} />
    </>
  )
}

function ScopePanel({
  scope,
  activeWatchlist,
  latestWatchlist,
  selectedSymbol,
  onSelectSymbol,
}: {
  scope: WatchlistScope
  activeWatchlist?: WatchlistUploadRecord
  latestWatchlist?: WatchlistUploadRecord
  selectedSymbol: string | null
  onSelectSymbol: (symbol: string) => void
}) {
  const activeRows = activeWatchlist?.symbols ?? []
  const managedRows = activeWatchlist?.managedOnlySymbols ?? []
  const limitations = activeWatchlist?.uiPayload.providerLimitations ?? []
  const summaryEntries = Object.entries(activeWatchlist?.uiPayload.summary ?? {})
  const validationMeta = getStatusMeta(activeWatchlist?.validationStatus)

  return (
    <section className="rounded-3xl border border-slate-800 bg-slate-900/70 p-5 shadow-xl shadow-slate-950/20">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div>
          <div className="text-sm font-semibold uppercase tracking-[0.18em] text-slate-500">{scopeLabels[scope]}</div>
          <h2 className="mt-1 text-2xl font-semibold text-white">{activeWatchlist ? 'Active watchlist loaded' : 'No active watchlist yet'}</h2>
          <div className="mt-3 flex flex-wrap gap-2">
            <ToneBadge tone={validationMeta.tone}>{validationMeta.canonicalLabel}</ToneBadge>
            <ToneBadge tone="muted">{activeWatchlist?.provider ?? latestWatchlist?.provider ?? 'Unknown provider'}</ToneBadge>
            {activeWatchlist?.marketRegime ? <ToneBadge tone="info">{activeWatchlist.marketRegime}</ToneBadge> : null}
            {activeWatchlist?.validationStatus ? <ToneBadge tone="muted">Raw: {validationMeta.rawLabel}</ToneBadge> : null}
          </div>
        </div>

        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <MiniCard label="Selected" value={String(activeWatchlist?.selectedCount ?? 0)} />
          <MiniCard label="Healthy" value={String(activeWatchlist?.statusSummary.activeCount ?? 0)} />
          <MiniCard label="Managed-only" value={String(activeWatchlist?.statusSummary.managedOnlyCount ?? 0)} />
          <MiniCard label="Unmanaged" value={String(activeWatchlist?.statusSummary.inactiveCount ?? 0)} />
        </div>
      </div>

      <div className="mt-5 grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,1.75fr)_minmax(340px,0.85fr)] 2xl:grid-cols-[minmax(0,1.95fr)_minmax(360px,0.8fr)]">
        <div className="rounded-2xl border border-slate-800 bg-slate-950/60 p-4">
          <div className="mb-3 flex flex-wrap items-center gap-2 text-sm text-slate-400">
            <Clock3 className="h-4 w-4 text-slate-500" />
            <span>Generated {formatTimestamp(activeWatchlist?.generatedAtUtc)}</span>
            <span className="text-slate-600">•</span>
            <span>Received {formatTimestamp(activeWatchlist?.receivedAtUtc)}</span>
            <span className="text-slate-600">•</span>
            <span>Expires {formatTimestamp(activeWatchlist?.watchlistExpiresAtUtc)}</span>
          </div>

          {!activeWatchlist ? (
            <EmptyState
              message={
                latestWatchlist
                  ? `No active ${scopeLabels[scope].toLowerCase()} payload is currently armed. The latest payload is ${latestWatchlist.validationStatus}${latestWatchlist.rejectionReason ? ` because ${latestWatchlist.rejectionReason}` : '.'}`
                  : `No ${scopeLabels[scope].toLowerCase()} payload has been received yet.`
              }
            />
          ) : activeRows.length === 0 ? (
            <EmptyState message="The active payload exists, but it does not currently contain any active rows." />
          ) : (
            <SymbolTable scope={scope} rows={activeRows} selectedSymbol={selectedSymbol} onSelectSymbol={onSelectSymbol} />
          )}

          {managedRows.length > 0 ? (
            <div className="mt-5 rounded-2xl border border-amber-900/70 bg-amber-500/5 p-4">
              <div className="mb-3 flex items-center gap-2 text-sm font-semibold text-amber-300">
                <ShieldAlert className="h-4 w-4" />
                Managed-only symbols
              </div>
              <div className="grid gap-2 md:grid-cols-2">
                {managedRows.map((row) => (
                  <button
                    key={`${row.uploadId}-${row.symbol}`}
                    onClick={() => onSelectSymbol(row.symbol)}
                    className="flex items-center justify-between rounded-2xl border border-amber-900/60 bg-slate-950/50 px-3 py-2 text-left text-sm text-slate-200 transition hover:border-amber-700 hover:bg-slate-900"
                  >
                    <span className="font-semibold">{row.symbol}</span>
                    <ChevronRight className="h-4 w-4 text-slate-500" />
                  </button>
                ))}
              </div>
            </div>
          ) : null}
        </div>

        <div className="space-y-4">
          <div className="rounded-2xl border border-slate-800 bg-slate-950/60 p-4">
            <div className="flex items-center gap-2 text-sm font-semibold text-slate-200">
              <CalendarClock className="h-4 w-4 text-cyan-300" />
              Upload health
            </div>
            <div className="mt-4 space-y-3 text-sm text-slate-400">
              <MetaRow label="Upload ID" value={activeWatchlist?.uploadId ?? '—'} />
              <MetaRow label="Scan ID" value={activeWatchlist?.scanId ?? '—'} />
              <MetaRow label="Schema" value={activeWatchlist?.schemaVersion ?? '—'} />
              <MetaRow label="Target session" value={activeWatchlist?.targetSessionEt ?? '—'} />
              <MetaRow
                label="Latest payload"
                value={latestWatchlist ? `${latestWatchlist.validationStatus}${latestWatchlist.isActive ? ' · active' : ''}` : '—'}
              />
              <MetaRow label="Latest rejection" value={latestWatchlist?.rejectionReason ?? 'None'} />
            </div>
          </div>

          <div className="rounded-2xl border border-slate-800 bg-slate-950/60 p-4">
            <div className="flex items-center gap-2 text-sm font-semibold text-slate-200">
              <CheckCircle2 className="h-4 w-4 text-emerald-300" />
              UI summary
            </div>
            <div className="mt-4 space-y-3 text-sm text-slate-400">
              {summaryEntries.length === 0 ? (
                <p>No UI summary metadata stored.</p>
              ) : (
                summaryEntries.map(([key, value]) => <MetaRow key={key} label={readableKey(key)} value={stringifyValue(value)} />)
              )}
            </div>
          </div>

          <div className="rounded-2xl border border-slate-800 bg-slate-950/60 p-4">
            <div className="flex items-center gap-2 text-sm font-semibold text-slate-200">
              <AlertCircle className="h-4 w-4 text-amber-300" />
              Provider limitations
            </div>
            {limitations.length === 0 ? (
              <p className="mt-4 text-sm text-slate-400">No provider limitations were stored for this watchlist.</p>
            ) : (
              <ul className="mt-4 space-y-2 text-sm leading-6 text-slate-400">
                {limitations.map((item) => (
                  <li key={item} className="rounded-2xl border border-slate-800 bg-slate-900/80 px-3 py-2">
                    {item}
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      </div>
    </section>
  )
}

function OperatorCuesCard({ hasSelectedSymbol }: { hasSelectedSymbol: boolean }) {
  return (
    <div className="rounded-3xl border border-slate-800 bg-slate-900/70 p-5">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div>
          <div className="flex items-center gap-2 text-sm font-semibold text-slate-200">
            <Target className="h-4 w-4 text-emerald-300" />
            Operator cues
          </div>
          <ul className="mt-3 space-y-2 text-sm leading-6 text-slate-400">
            <li>Healthy rows are still eligible for new entries.</li>
            <li>Managed-only rows keep positions supervised after a symbol drops from the newest payload.</li>
            <li>Validation status and freshness belong to the upload, not to the symbol narrative.</li>
          </ul>
        </div>

        <div className="rounded-2xl border border-slate-800 bg-slate-950/60 px-4 py-3 text-sm text-slate-400 lg:max-w-md">
          {hasSelectedSymbol
            ? 'The symbol drawer is open. Use its jump links to move from context into monitoring, positions, or audit without hunting through the nav.'
            : 'Click any row to open the symbol drawer and get quick routes into the rest of the operator console.'}
        </div>
      </div>
    </div>
  )
}

function SymbolContextDrawer({
  selectedSymbol,
  context,
  onClose,
}: {
  selectedSymbol: { scope: WatchlistScope; symbol: string } | null
  context: Record<string, unknown> | null
  onClose: () => void
}) {
  const isOpen = Boolean(selectedSymbol)

  return (
    <>
      <div
        className={`fixed inset-0 z-40 bg-slate-950/60 transition ${isOpen ? 'pointer-events-auto opacity-100' : 'pointer-events-none opacity-0'}`}
        onClick={onClose}
      />

      <aside
        className={`fixed right-0 top-0 z-50 h-full w-full max-w-xl transform border-l border-slate-800 bg-slate-950/95 shadow-2xl shadow-black/50 backdrop-blur transition duration-300 ${
          isOpen ? 'translate-x-0' : 'translate-x-full'
        }`}
      >
        <div className="flex h-full flex-col">
          <div className="flex items-start justify-between border-b border-slate-800 px-6 py-5">
            <div>
              <div className="flex items-center gap-2 text-sm font-semibold text-slate-200">
                <Sparkles className="h-4 w-4 text-cyan-300" />
                Symbol context
              </div>
              <div className="mt-2 text-sm text-slate-400">
                {selectedSymbol ? (
                  <>
                    <span className="font-semibold text-white">{selectedSymbol.symbol}</span>
                    <span className="mx-2 text-slate-600">•</span>
                    <span>{scopeLabels[selectedSymbol.scope]}</span>
                  </>
                ) : (
                  'No symbol selected'
                )}
              </div>
            </div>

            <button
              onClick={onClose}
              className="rounded-2xl border border-slate-800 bg-slate-900/70 p-2 text-slate-300 transition hover:border-slate-700 hover:bg-slate-900 hover:text-white"
              aria-label="Close symbol context drawer"
            >
              <X className="h-5 w-5" />
            </button>
          </div>

          <div className="border-b border-slate-800 px-6 py-4">
            <div className="flex flex-wrap gap-2">
              <QuickJump to="/monitoring" label="Open Monitoring" scope={selectedSymbol?.scope} symbol={selectedSymbol?.symbol} />
              <QuickJump to="/positions" label="Open Positions" scope={selectedSymbol?.scope} symbol={selectedSymbol?.symbol} />
              <QuickJump to="/audit" label="Open Audit Trail" scope={selectedSymbol?.scope} symbol={selectedSymbol?.symbol} />
            </div>
          </div>

          <div className="flex-1 overflow-y-auto px-6 py-5">
            {!selectedSymbol ? (
              <p className="text-sm leading-6 text-slate-400">Pick a symbol from an active watchlist to inspect the stored UI-only context.</p>
            ) : !context ? (
              <p className="text-sm leading-6 text-slate-400">
                No UI-only context was stored for <span className="font-semibold text-slate-200">{selectedSymbol.symbol}</span>.
              </p>
            ) : (
              <div className="space-y-5 text-sm text-slate-300">
                <ContextBlock label="Thesis" value={context.thesis} />
                <ContextBlock label="Why now" value={context.why_now} />
                <ContextBlock label="Notes" value={context.notes} />
                {Object.entries(context)
                  .filter(([key, value]) => !['thesis', 'why_now', 'notes'].includes(key) && value !== null && value !== undefined && value !== '')
                  .map(([key, value]) => (
                    <ContextBlock key={key} label={readableKey(key)} value={stringifyValue(value)} />
                  ))}
              </div>
            )}
          </div>
        </div>
      </aside>
    </>
  )
}

function SymbolTable({
  scope,
  rows,
  selectedSymbol,
  onSelectSymbol,
}: {
  scope: WatchlistScope
  rows: WatchlistSymbolRecord[]
  selectedSymbol: string | null
  onSelectSymbol: (symbol: string) => void
}) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[1260px] text-sm">
        <thead>
          <tr className="border-b border-slate-800 text-left text-xs uppercase tracking-wide text-slate-500">
            <th className="w-[150px] pb-3 pr-4">Symbol</th>
            <th className="w-[220px] pb-3 pr-4">Setup</th>
            <th className="w-[220px] pb-3 pr-4">Exit</th>
            <th className="w-[110px] pb-3 pr-4">Tier</th>
            <th className="w-[110px] pb-3 pr-4">Bias</th>
            <th className="w-[170px] pb-3 pr-4">Timeframes</th>
            <th className="w-[180px] pb-3 pr-4">Lifecycle</th>
            <th className="w-[220px] pb-3 pr-4">Jump lanes</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const isSelected = selectedSymbol === row.symbol
            const status = getStatusMeta(row.monitoringStatus)

            return (
              <tr key={`${row.uploadId}-${row.symbol}`} className="border-b border-slate-900/80 text-slate-300 align-top">
                <td className="py-3 pr-4 align-top">
                  <button
                    onClick={() => onSelectSymbol(row.symbol)}
                    className={`w-full rounded-xl border px-3 py-2 text-left transition ${
                      isSelected
                        ? 'border-cyan-700 bg-cyan-500/10 text-cyan-100'
                        : 'border-slate-800 bg-slate-900/60 text-white hover:border-slate-700 hover:bg-slate-900'
                    }`}
                  >
                    <div className="font-semibold">{row.symbol}</div>
                    <div className="text-xs text-slate-500">Rank {row.priorityRank}</div>
                  </button>
                </td>
                <td className="py-3 pr-4 align-top whitespace-normal break-words [overflow-wrap:anywhere]">{row.setupTemplate}</td>
                <td className="py-3 pr-4 align-top whitespace-normal break-words [overflow-wrap:anywhere]">{row.exitTemplate}</td>
                <td className="py-3 pr-4 align-top whitespace-nowrap">{row.tier}</td>
                <td className="py-3 pr-4 align-top whitespace-nowrap">{row.bias}</td>
                <td className="py-3 pr-4 align-top whitespace-normal break-words [overflow-wrap:anywhere]">{row.botTimeframes.join(', ')}</td>
                <td className="py-3 pr-4 align-top whitespace-nowrap">
                  <div className="flex flex-col gap-2">
                    <ToneBadge tone={status.tone}>{status.canonicalLabel}</ToneBadge>
                    <span className="text-xs text-slate-500">Raw: {status.rawLabel}</span>
                  </div>
                </td>
                <td className="py-3 pr-4 align-top">
                  <div className="flex flex-wrap gap-2">
                    <QuickJump to="/monitoring" label="Monitoring" compact scope={scope} symbol={row.symbol} />
                    <QuickJump to="/positions" label="Positions" compact scope={scope} symbol={row.symbol} />
                    <QuickJump to="/audit" label="Audit" compact scope={scope} symbol={row.symbol} />
                  </div>
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function QuickJump({
  to,
  label,
  compact = false,
  scope,
  symbol,
}: {
  to: string
  label: string
  compact?: boolean
  scope?: WatchlistScope
  symbol?: string
}) {
  const query = new URLSearchParams()
  if (scope) query.set('scope', scope)
  if (symbol) query.set('symbol', symbol)
  const href = query.toString() ? `${to}?${query.toString()}` : to
  return (
    <Link
      to={href}
      className={`inline-flex items-center gap-2 rounded-full border border-slate-700 bg-slate-900/70 text-slate-200 transition hover:border-cyan-700 hover:text-white ${
        compact ? 'px-2.5 py-1 text-[11px]' : 'px-3 py-2 text-sm'
      }`}
    >
      <span>{label}</span>
      <ArrowRight className="h-3.5 w-3.5 text-cyan-300" />
    </Link>
  )
}

function MiniCard({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-2xl border border-slate-800 bg-slate-950/60 px-3 py-3">
      <div className="text-[11px] uppercase tracking-wide text-slate-500">{label}</div>
      <div className="mt-1 text-lg font-semibold text-white">{value}</div>
    </div>
  )
}

function MetaRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col gap-1 border-b border-slate-900/90 pb-3 last:border-b-0 last:pb-0 sm:flex-row sm:items-start sm:justify-between sm:gap-4">
      <span className="text-slate-500 sm:max-w-[34%]">{label}</span>
      <span className="min-w-0 break-all text-left text-slate-200 sm:max-w-[66%] sm:text-right">{value}</span>
    </div>
  )
}

function ContextBlock({ label, value }: { label: string; value: unknown }) {
  if (!value) return null

  return (
    <div>
      <div className="mb-1 text-xs uppercase tracking-wide text-slate-500">{label}</div>
      <div className="rounded-2xl border border-slate-800 bg-slate-900/70 px-4 py-4 leading-7 text-slate-100">{stringifyValue(value)}</div>
    </div>
  )
}

function formatTimestamp(value?: string | null) {
  if (!value) return '—'
  const date = new Date(value)
  return `${date.toLocaleString()} · ${formatDistanceToNowStrict(date, { addSuffix: true })}`
}

function readableKey(value: string) {
  return value.replace(/_/g, ' ').replace(/([a-z])([A-Z])/g, '$1 $2').replace(/\b\w/g, (char) => char.toUpperCase())
}

function stringifyValue(value: unknown) {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  if (Array.isArray(value)) return value.join(', ')
  return JSON.stringify(value)
}
