import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { formatDistanceToNow } from 'date-fns'
import { Activity, Settings as SettingsIcon, Shield, Wifi } from 'lucide-react'

import { api } from '@/lib/api'
import type { BotStatus, DependencyCheck, RuntimeVisibility, TradingMode } from '@/types'

function relativeTime(value?: string | null) {
  if (!value) return '—'
  return formatDistanceToNow(new Date(value), { addSuffix: true })
}

export default function Settings() {
  const queryClient = useQueryClient()
  const [message, setMessage] = useState('')
  const [errorMessage, setErrorMessage] = useState('')

  const { data: status } = useQuery<BotStatus>({
    queryKey: ['botStatus'],
    queryFn: api.getBotStatus,
    refetchInterval: 3000,
  })

  const { data: runtimeVisibility } = useQuery<RuntimeVisibility>({
    queryKey: ['runtimeVisibility'],
    queryFn: () => api.getRuntimeVisibility(10),
    refetchInterval: 10000,
  })

  const showSuccess = (text: string) => {
    setErrorMessage('')
    setMessage(text)
    window.setTimeout(() => setMessage(''), 3000)
  }

  const showError = (text: string) => {
    setMessage('')
    setErrorMessage(text)
    window.setTimeout(() => setErrorMessage(''), 5000)
  }

  const invalidateStatus = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['botStatus'] }),
      queryClient.invalidateQueries({ queryKey: ['runtimeVisibility'] }),
      queryClient.invalidateQueries({ queryKey: ['stockAccount'] }),
      queryClient.invalidateQueries({ queryKey: ['stockPositions'] }),
    ])
  }

  const toggleBotMutation = useMutation({
    mutationFn: (enabled: boolean) => api.toggleBot(enabled),
    onSuccess: async () => {
      await invalidateStatus()
      showSuccess('Bot status updated')
    },
    onError: (error: Error) => showError(error.message),
  })

  const setStockModeMutation = useMutation({
    mutationFn: (mode: TradingMode) => api.setStockMode(mode),
    onSuccess: async () => {
      await invalidateStatus()
      showSuccess('Stock account mode updated')
    },
    onError: (error: Error) => showError(error.message),
  })

  const toggleSafetyMutation = useMutation({
    mutationFn: (enabled: boolean) => api.toggleSafetyOverride(enabled),
    onSuccess: async () => {
      await invalidateStatus()
      showSuccess('Safety setting updated')
    },
    onError: (error: Error) => showError(error.message),
  })

  const stockReady = useMemo(
    () => ({
      paper: status?.stockCapabilities.paperReady ?? false,
      live: status?.stockCapabilities.liveReady ?? false,
    }),
    [status],
  )

  const dependencyChecks = runtimeVisibility?.dependencies.checks
  const gateSummary = runtimeVisibility?.gate.summary

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between gap-4">
        <div>
          <h1 className="text-3xl font-bold text-white">Runtime & Settings</h1>
          <p className="mt-1 text-gray-400">Control-plane visibility, dependency health, and execution safety controls.</p>
        </div>
        <div className="rounded-lg border border-gray-800 bg-gray-900 px-4 py-3 text-right">
          <div className="text-xs uppercase tracking-wide text-gray-500">Control State</div>
          <div className="text-lg font-semibold text-white">{status?.controlPlane.state ?? 'UNKNOWN'}</div>
        </div>
      </div>

      {message ? <Notice tone="success" message={message} /> : null}
      {errorMessage ? <Notice tone="error" message={errorMessage} /> : null}

      <div className="grid grid-cols-1 gap-6 xl:grid-cols-3">
        <div className="xl:col-span-2 space-y-6">
          <div className="rounded-lg border border-gray-800 bg-gray-900 p-6">
            <h2 className="mb-4 flex items-center gap-2 text-xl font-bold text-white">
              <Activity className="h-5 w-5" />
              Runtime Controls
            </h2>
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
              <Metric label="Running" value={status?.running ? 'Enabled' : 'Paused'} valueClass={status?.running ? 'text-green-400' : 'text-yellow-400'} />
              <Metric label="Execution Gate" value={status?.executionGate.state ?? 'UNKNOWN'} valueClass={status?.executionGate.allowed ? 'text-green-400' : 'text-red-400'} />
              <Metric label="Last Heartbeat" value={relativeTime(status?.lastHeartbeat)} />
              <Metric label="Dependency Readiness" value={runtimeVisibility?.dependencies.summary.criticalReady ? 'Critical dependencies ready' : 'Degraded'} valueClass={runtimeVisibility?.dependencies.summary.criticalReady ? 'text-green-400' : 'text-yellow-400'} />
            </div>

            <div className="mt-6 grid grid-cols-1 gap-4 md:grid-cols-2">
              <div className="rounded-lg bg-gray-800 p-4">
                <div className="font-semibold text-white">Bot Runtime</div>
                <div className="mt-1 text-sm text-gray-400">The running flag is now part of the execution gate, not decorative confetti.</div>
                <button
                  onClick={() => toggleBotMutation.mutate(!(status?.running ?? false))}
                  disabled={toggleBotMutation.isPending}
                  className={`mt-4 rounded-lg px-4 py-2 text-sm font-semibold transition-colors ${
                    status?.running ? 'bg-yellow-600 text-white hover:bg-yellow-700' : 'bg-green-600 text-white hover:bg-green-700'
                  }`}
                >
                  {status?.running ? 'Pause Bot' : 'Resume Bot'}
                </button>
              </div>

              <div className="rounded-lg bg-gray-800 p-4">
                <div className="font-semibold text-white">Stock Account</div>
                <div className="mt-1 text-sm text-gray-400">Switch the active Tradier account without editing the frontend.</div>
                <div className="mt-4 flex flex-wrap gap-3">
                  <ModeButton
                    active={(status?.stockMode ?? 'PAPER') === 'PAPER'}
                    disabled={!stockReady.paper || setStockModeMutation.isPending}
                    label={stockReady.paper ? 'Use Paper Account' : 'Paper Account Missing'}
                    onClick={() => setStockModeMutation.mutate('PAPER')}
                  />
                  <ModeButton
                    active={(status?.stockMode ?? 'PAPER') === 'LIVE'}
                    disabled={!stockReady.live || setStockModeMutation.isPending}
                    label={stockReady.live ? 'Use Live Account' : 'Live Account Missing'}
                    onClick={() => setStockModeMutation.mutate('LIVE')}
                  />
                </div>
              </div>
            </div>
          </div>

          <div className="rounded-lg border border-gray-800 bg-gray-900 p-6">
            <h2 className="mb-4 flex items-center gap-2 text-xl font-bold text-white">
              <Shield className="h-5 w-5" />
              Safety Controls
            </h2>
            <div className="flex items-center justify-between gap-4 rounded-lg bg-gray-800 p-4">
              <div>
                <div className="font-semibold text-white">Require Market Hours</div>
                <div className="text-sm text-gray-400">Only allow stock trades during regular US market hours.</div>
              </div>
              <button
                onClick={() => toggleSafetyMutation.mutate(!(status?.safetyRequireMarketHours ?? true))}
                className={`rounded-lg px-4 py-2 text-sm font-semibold transition-colors ${
                  status?.safetyRequireMarketHours
                    ? 'bg-green-600 text-white hover:bg-green-700'
                    : 'bg-gray-700 text-gray-300 hover:bg-gray-600'
                }`}
              >
                {status?.safetyRequireMarketHours ? 'Enabled' : 'Disabled'}
              </button>
            </div>

            <div className="mt-4 rounded-lg bg-gray-800 p-4 text-sm text-gray-300">
              <div className="font-semibold text-white">Last Gate Rejection</div>
              <div className="mt-2">{gateSummary?.lastRejected?.rejectionReason ?? 'No recent rejection recorded.'}</div>
            </div>
          </div>
        </div>

        <div className="space-y-6">
          <div className="rounded-lg border border-gray-800 bg-gray-900 p-6">
            <h2 className="mb-4 flex items-center gap-2 text-xl font-bold text-white">
              <Wifi className="h-5 w-5" />
              Dependency Probes
            </h2>
            <div className="space-y-3">
              {dependencyChecks ? (
                Object.values(dependencyChecks).map((check) => <IntegrationCard key={check.name} check={check} />)
              ) : (
                <p className="text-sm text-gray-500">Waiting for probes…</p>
              )}
            </div>
          </div>

          <div className="rounded-lg border border-gray-800 bg-gray-900 p-6">
            <h2 className="mb-4 flex items-center gap-2 text-xl font-bold text-white">
              <SettingsIcon className="h-5 w-5" />
              Gate Observability
            </h2>
            <div className="space-y-3 text-sm text-gray-300">
              <Metric label="Total Decisions" value={String(gateSummary?.total ?? 0)} />
              <Metric label="Allowed" value={String(gateSummary?.allowedCount ?? 0)} valueClass="text-green-400" />
              <Metric label="Rejected" value={String(gateSummary?.rejectedCount ?? 0)} valueClass="text-red-400" />
              <Metric label="Last Observation" value={relativeTime(runtimeVisibility?.capturedAtUtc)} />
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}

function Notice({ tone, message }: { tone: 'success' | 'error'; message: string }) {
  return (
    <div className={`rounded-lg px-4 py-3 text-sm ${tone === 'success' ? 'bg-green-900/40 text-green-300' : 'bg-red-900/40 text-red-300'}`}>
      {message}
    </div>
  )
}

function Metric({ label, value, valueClass }: { label: string; value: string; valueClass?: string }) {
  return (
    <div>
      <div className="text-sm text-gray-400">{label}</div>
      <div className={`text-lg font-semibold ${valueClass ?? 'text-white'}`}>{value}</div>
    </div>
  )
}

function ModeButton({ active, disabled, label, onClick }: { active: boolean; disabled: boolean; label: string; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className={`rounded-lg px-4 py-2 text-sm font-semibold transition-colors ${
        active
          ? 'bg-blue-600 text-white'
          : disabled
            ? 'cursor-not-allowed bg-gray-700 text-gray-500'
            : 'bg-gray-700 text-gray-200 hover:bg-gray-600'
      }`}
    >
      {label}
    </button>
  )
}

function IntegrationCard({ check }: { check: DependencyCheck }) {
  const tone =
    check.state === 'READY'
      ? 'bg-green-900/40 text-green-300'
      : check.state === 'MISSING'
        ? 'bg-red-900/40 text-red-300'
        : 'bg-yellow-900/40 text-yellow-300'

  return (
    <div className="rounded-lg bg-gray-800 p-4">
      <div className="mb-2 flex items-center justify-between gap-4">
        <span className="font-semibold text-white">{check.name}</span>
        <span className={`rounded px-2 py-1 text-xs ${tone}`}>{check.state}</span>
      </div>
      <div className="text-sm text-gray-300">{check.reason || 'Probe succeeded.'}</div>
      <div className="mt-2 text-xs text-gray-500">Checked {relativeTime(check.checkedAtUtc)}</div>
    </div>
  )
}
