import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import type { BotStatus, StockAccount, StockPosition, TradeHistoryEntry } from '@/types'
import { TrendingUp, Activity } from 'lucide-react'
import { format } from 'date-fns'

export default function StockTrading() {
  const { data: positions = [] } = useQuery<StockPosition[]>({
    queryKey: ['stockPositions'],
    queryFn: api.getStockPositions,
    refetchInterval: 5000,
  })

  const { data: history = [] } = useQuery<TradeHistoryEntry[]>({
    queryKey: ['stockHistory'],
    queryFn: () => api.getStockHistory(100),
    refetchInterval: 10000,
  })

  const { data: account } = useQuery<StockAccount>({
    queryKey: ['stockAccount'],
    queryFn: api.getStockAccount,
    refetchInterval: 10000,
  })

  const { data: status } = useQuery<BotStatus>({
    queryKey: ['botStatus'],
    queryFn: api.getBotStatus,
    refetchInterval: 5000,
  })

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
        <div>
          <h1 className="text-3xl font-bold text-white">Stock Trading</h1>
          <p className="mt-1 text-gray-400">Tradier integration with switchable paper/live account mode.</p>
        </div>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
          <AccountMetric label="Mode" value={status?.stockMode ?? 'PAPER'} />
          <AccountMetric label="Buying Power" value={`$${(account?.buyingPower ?? 0).toFixed(2)}`} />
          <AccountMetric label="Portfolio Value" value={`$${(account?.portfolioValue ?? 0).toFixed(2)}`} />
        </div>
      </div>

      <div className="rounded-lg border border-gray-800 bg-gray-900 p-6">
        <h2 className="mb-4 flex items-center gap-2 text-xl font-bold text-white">
          <TrendingUp className="h-5 w-5" />
          Active Positions
        </h2>
        {positions.length === 0 ? (
          <p className="py-8 text-center text-gray-500">No active stock positions</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr className="border-b border-gray-800 text-left text-sm text-gray-400">
                  <th className="pb-3">Symbol</th>
                  <th className="pb-3">Shares</th>
                  <th className="pb-3">Avg Price</th>
                  <th className="pb-3">Current</th>
                  <th className="pb-3">Market Value</th>
                  <th className="pb-3">P&L</th>
                  <th className="pb-3">P&L %</th>
                </tr>
              </thead>
              <tbody>
                {positions.map((pos) => (
                  <tr key={pos.symbol} className="border-b border-gray-800 hover:bg-gray-800/50">
                    <td className="py-3 font-semibold text-white">{pos.symbol}</td>
                    <td className="py-3 text-gray-300">{pos.shares.toFixed(0)}</td>
                    <td className="py-3 text-gray-300">${pos.avgPrice.toFixed(2)}</td>
                    <td className="py-3 text-gray-300">${pos.currentPrice.toFixed(2)}</td>
                    <td className="py-3 text-gray-300">${pos.marketValue.toFixed(2)}</td>
                    <td className={`py-3 font-semibold ${pos.pnl >= 0 ? 'text-green-500' : 'text-red-500'}`}>
                      ${pos.pnl.toFixed(2)}
                    </td>
                    <td className={`py-3 ${pos.pnlPercent >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                      {pos.pnlPercent >= 0 ? '+' : ''}
                      {pos.pnlPercent.toFixed(2)}%
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="rounded-lg border border-gray-800 bg-gray-900 p-6">
        <h2 className="mb-4 flex items-center gap-2 text-xl font-bold text-white">
          <Activity className="h-5 w-5" />
          Recent Trades
        </h2>
        {history.length === 0 ? (
          <p className="py-8 text-center text-gray-500">Order history is not wired yet.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr className="border-b border-gray-800 text-left text-sm text-gray-400">
                  <th className="pb-3">Time</th>
                  <th className="pb-3">Symbol</th>
                  <th className="pb-3">Side</th>
                  <th className="pb-3">Shares</th>
                  <th className="pb-3">Price</th>
                  <th className="pb-3">Total</th>
                  <th className="pb-3">Status</th>
                </tr>
              </thead>
              <tbody>
                {history.map((trade) => (
                  <tr key={trade.id} className="border-b border-gray-800 hover:bg-gray-800/50">
                    <td className="py-3 text-sm text-gray-300">{format(new Date(trade.timestamp), 'MMM dd, HH:mm')}</td>
                    <td className="py-3 font-semibold text-white">{trade.symbol ?? '—'}</td>
                    <td className="py-3 text-gray-300">{trade.side}</td>
                    <td className="py-3 text-gray-300">{trade.shares?.toFixed(0) ?? '—'}</td>
                    <td className="py-3 text-gray-300">${(trade.price ?? 0).toFixed(2)}</td>
                    <td className="py-3 text-gray-300">${(trade.total ?? 0).toFixed(2)}</td>
                    <td className="py-3 text-gray-300">{trade.status}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}

function AccountMetric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-gray-800 bg-gray-900 p-4">
      <div className="text-sm text-gray-400">{label}</div>
      <div className="text-xl font-bold text-white">{value}</div>
    </div>
  )
}
