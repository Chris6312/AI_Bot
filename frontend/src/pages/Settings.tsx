import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import type { BotStatus, TradingMode } from '@/types'
import { Settings as SettingsIcon, Shield, Activity, Wifi } from 'lucide-react'

export default function Settings() {
  const queryClient = useQueryClient()
  const [message, setMessage] = useState('')
  const [errorMessage, setErrorMessage] = useState('')

  const { data: status } = useQuery<BotStatus>({
    queryKey: ['botStatus'],
    queryFn: api.getBotStatus,
    refetchInterval: 3000,
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
    onSuccess: async (_, mode) => {
      await invalidateStatus()
      showSuccess(`Stock trading mode switched to ${mode}`)
    },
    onError: (error: Error) => showError(error.message),
  })

  const toggleSafetyMutation = useMutation({
    mutationFn: (enabled: boolean) => api.toggleSafetyOverride(enabled),
    onSuccess: async () => {
      await invalidateStatus()
      showSuccess('Safety settings updated')
    },
    onError: (error: Error) => showError(error.message),
  })

  const stockReady = useMemo(() => ({
    paper: status?.stockCapabilities.paperReady ?? false,
    live: status?.stockCapabilities.liveReady ?? false,
  }), [status])

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-bold text-white">Settings</h1>
        <p className="mt-1 text-gray-400">Control bot status, stock paper/live mode, and integration readiness.</p>
      </div>

      {message ? (
        <div className="rounded-lg border border-green-800 bg-green-900/20 p-4 text-green-300">{message}</div>
      ) : null}
      {errorMessage ? (
        <div className="rounded-lg border border-red-800 bg-red-900/20 p-4 text-red-300">{errorMessage}</div>
      ) : null}

      <div className="rounded-lg border border-gray-800 bg-gray-900 p-6">
        <h2 className="mb-4 flex items-center gap-2 text-xl font-bold text-white">
          <Activity className="h-5 w-5" />
          Bot Control
        </h2>
        <div className="space-y-6">
          <div className="flex items-center justify-between gap-4">
            <div>
              <div className="font-semibold text-white">Bot Status</div>
              <div className="text-sm text-gray-400">Enable or disable automated bot processing.</div>
            </div>
            <button
              onClick={() => toggleBotMutation.mutate(!(status?.running ?? false))}
              className={`rounded-lg px-6 py-2 font-semibold transition-colors ${
                status?.running ? 'bg-red-600 text-white hover:bg-red-700' : 'bg-green-600 text-white hover:bg-green-700'
              }`}
            >
              {status?.running ? 'Stop Bot' : 'Start Bot'}
            </button>
          </div>

          <div className="border-t border-gray-800 pt-4">
            <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
              <Metric label="Stock Mode" value={status?.stockMode ?? 'PAPER'} valueClass={status?.stockMode === 'LIVE' ? 'text-amber-400' : 'text-white'} />
              <Metric label="Crypto Mode" value={status?.cryptoMode ?? 'PAPER'} valueClass="text-white" />
              <Metric
                label="Last Heartbeat"
                value={status?.lastHeartbeat ? new Date(status.lastHeartbeat).toLocaleTimeString() : 'N/A'}
                valueClass="text-gray-300"
              />
            </div>
          </div>
        </div>
      </div>

      <div className="rounded-lg border border-gray-800 bg-gray-900 p-6">
        <h2 className="mb-4 flex items-center gap-2 text-xl font-bold text-white">
          <Wifi className="h-5 w-5" />
          Trading Modes
        </h2>
        <div className="space-y-5">
          <div>
            <div className="font-semibold text-white">Tradier Stock Account</div>
            <div className="mt-1 text-sm text-gray-400">
              The active stock account can now be toggled between paper and live without editing the frontend.
            </div>
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

          <div className="rounded-lg bg-gray-800 p-4">
            <div className="font-semibold text-white">Kraken Crypto Mode</div>
            <div className="mt-1 text-sm text-gray-400">
              Execution remains on the paper ledger today. Live Kraken credentials are exposed in <code>.env.example</code> so the next integration slice can wire an authenticated live account without changing the UI contract.
            </div>
          </div>
        </div>
      </div>

      <div className="rounded-lg border border-gray-800 bg-gray-900 p-6">
        <h2 className="mb-4 flex items-center gap-2 text-xl font-bold text-white">
          <Shield className="h-5 w-5" />
          Safety Controls
        </h2>
        <div className="flex items-center justify-between gap-4">
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
      </div>

      <div className="rounded-lg border border-gray-800 bg-gray-900 p-6">
        <h2 className="mb-4 flex items-center gap-2 text-xl font-bold text-white">
          <SettingsIcon className="h-5 w-5" />
          Integration Status
        </h2>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          <IntegrationCard
            name="Tradier Paper"
            status={stockReady.paper ? 'Connected' : 'Missing credentials'}
            active={stockReady.paper}
            detail="Used for sandbox / paper stock trading and dashboard equity."
          />
          <IntegrationCard
            name="Tradier Live"
            status={stockReady.live ? 'Ready' : 'Missing credentials'}
            active={stockReady.live}
            detail="Live stock mode is now switchable when live credentials are present."
          />
          <IntegrationCard
            name="Kraken CLI"
            status="Connected for market data"
            active
            detail="Used for live pricing plus the paper ledger for crypto."
          />
          <IntegrationCard
            name="Discord Webhook"
            status="Configured in backend flow"
            active
            detail="Webhook intake remains the execution path for AI decisions."
          />
        </div>
      </div>
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
            ? 'cursor-not-allowed bg-gray-800 text-gray-500'
            : 'bg-gray-700 text-gray-200 hover:bg-gray-600'
      }`}
    >
      {label}
    </button>
  )
}

function IntegrationCard({ name, status, active, detail }: { name: string; status: string; active: boolean; detail: string }) {
  return (
    <div className="rounded-lg bg-gray-800 p-4">
      <div className="mb-2 flex items-center justify-between gap-4">
        <span className="font-semibold text-white">{name}</span>
        <span className={`rounded px-2 py-1 text-xs ${active ? 'bg-green-900 text-green-300' : 'bg-yellow-900 text-yellow-300'}`}>
          {status}
        </span>
      </div>
      <div className="text-sm text-gray-400">{detail}</div>
    </div>
  )
}
