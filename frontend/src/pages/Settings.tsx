import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { Settings as SettingsIcon, Shield, Activity } from 'lucide-react'
import { useState } from 'react'

export default function Settings() {
  const queryClient = useQueryClient()
  const [message, setMessage] = useState('')
  
  const { data: status } = useQuery({
    queryKey: ['botStatus'],
    queryFn: api.getBotStatus,
    refetchInterval: 3000,
  })
  
  const toggleBotMutation = useMutation({
    mutationFn: (enabled: boolean) => api.toggleBot(enabled),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['botStatus'] })
      setMessage('Bot status updated')
      setTimeout(() => setMessage(''), 3000)
    },
  })
  
  const toggleSafetyMutation = useMutation({
    mutationFn: (enabled: boolean) => api.toggleSafetyOverride(enabled),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['botStatus'] })
      setMessage('Safety settings updated')
      setTimeout(() => setMessage(''), 3000)
    },
  })
  
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-bold text-white">Settings</h1>
        <p className="text-gray-400 mt-1">Configure AI Bot behavior and safety controls</p>
      </div>
      
      {message && (
        <div className="bg-green-900/20 border border-green-800 rounded-lg p-4">
          <p className="text-green-300">{message}</p>
        </div>
      )}
      
      {/* Bot Control */}
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-6">
        <h2 className="text-xl font-bold text-white mb-4 flex items-center gap-2">
          <Activity className="w-5 h-5" />
          Bot Control
        </h2>
        <div className="space-y-4">
          <div className="flex items-center justify-between">
            <div>
              <div className="font-semibold text-white">Bot Status</div>
              <div className="text-sm text-gray-400">Enable or disable AI trading bot</div>
            </div>
            <button
              onClick={() => toggleBotMutation.mutate(!status?.running)}
              className={`px-6 py-2 rounded-lg font-semibold transition-colors ${
                status?.running
                  ? 'bg-red-600 hover:bg-red-700 text-white'
                  : 'bg-green-600 hover:bg-green-700 text-white'
              }`}
            >
              {status?.running ? 'Stop Bot' : 'Start Bot'}
            </button>
          </div>
          
          <div className="border-t border-gray-800 pt-4">
            <div className="grid grid-cols-3 gap-4">
              <div>
                <div className="text-sm text-gray-400">Stock Mode</div>
                <div className="text-lg font-semibold text-white">{status?.stockMode || 'PAPER'}</div>
              </div>
              <div>
                <div className="text-sm text-gray-400">Crypto Mode</div>
                <div className="text-lg font-semibold text-yellow-500">PAPER ONLY</div>
              </div>
              <div>
                <div className="text-sm text-gray-400">Last Heartbeat</div>
                <div className="text-sm text-gray-300">
                  {status?.lastHeartbeat ? new Date(status.lastHeartbeat).toLocaleTimeString() : 'N/A'}
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
      
      {/* Safety Controls */}
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-6">
        <h2 className="text-xl font-bold text-white mb-4 flex items-center gap-2">
          <Shield className="w-5 h-5" />
          Safety Controls
        </h2>
        <div className="space-y-4">
          <div className="flex items-center justify-between">
            <div>
              <div className="font-semibold text-white">Require Market Hours</div>
              <div className="text-sm text-gray-400">
                Only allow stock trades during market hours (9:30 AM - 4:00 PM ET)
              </div>
            </div>
            <button
              onClick={() => toggleSafetyMutation.mutate(!status?.safetyRequireMarketHours)}
              className={`px-4 py-2 rounded-lg text-sm font-semibold transition-colors ${
                status?.safetyRequireMarketHours
                  ? 'bg-green-600 hover:bg-green-700 text-white'
                  : 'bg-gray-700 hover:bg-gray-600 text-gray-300'
              }`}
            >
              {status?.safetyRequireMarketHours ? 'Enabled' : 'Disabled'}
            </button>
          </div>
        </div>
      </div>
      
      {/* Integration Status */}
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-6">
        <h2 className="text-xl font-bold text-white mb-4 flex items-center gap-2">
          <SettingsIcon className="w-5 h-5" />
          Integration Status
        </h2>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div className="bg-gray-800 rounded-lg p-4">
            <div className="flex items-center justify-between mb-2">
              <span className="font-semibold text-white">Tradier (Stocks)</span>
              <span className="px-2 py-1 bg-green-900 text-green-300 rounded text-xs">Connected</span>
            </div>
            <div className="text-sm text-gray-400">Live trading enabled</div>
          </div>
          <div className="bg-gray-800 rounded-lg p-4">
            <div className="flex items-center justify-between mb-2">
              <span className="font-semibold text-white">Kraken (Crypto)</span>
              <span className="px-2 py-1 bg-yellow-900 text-yellow-300 rounded text-xs">Paper Only</span>
            </div>
            <div className="text-sm text-gray-400">Paper trading with live prices</div>
          </div>
          <div className="bg-gray-800 rounded-lg p-4">
            <div className="flex items-center justify-between mb-2">
              <span className="font-semibold text-white">Discord Webhook</span>
              <span className="px-2 py-1 bg-green-900 text-green-300 rounded text-xs">Active</span>
            </div>
            <div className="text-sm text-gray-400">Receiving AI decisions</div>
          </div>
          <div className="bg-gray-800 rounded-lg p-4">
            <div className="flex items-center justify-between mb-2">
              <span className="font-semibold text-white">PostgreSQL</span>
              <span className="px-2 py-1 bg-green-900 text-green-300 rounded text-xs">Connected</span>
            </div>
            <div className="text-sm text-gray-400">Database online</div>
          </div>
        </div>
      </div>
    </div>
  )
}
