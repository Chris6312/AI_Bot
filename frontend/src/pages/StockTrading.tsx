import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { TrendingUp, DollarSign, Activity } from 'lucide-react'
import { format } from 'date-fns'

export default function StockTrading() {
  const { data: positions } = useQuery({
    queryKey: ['stockPositions'],
    queryFn: api.getStockPositions,
    refetchInterval: 5000,
  })
  
  const { data: history } = useQuery({
    queryKey: ['stockHistory'],
    queryFn: () => api.getStockHistory(100),
    refetchInterval: 10000,
  })
  
  const { data: account } = useQuery({
    queryKey: ['stockAccount'],
    queryFn: api.getStockAccount,
    refetchInterval: 10000,
  })
  
  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold text-white">Stock Trading</h1>
          <p className="text-gray-400 mt-1">Tradier Integration - Live Trading</p>
        </div>
        <div className="flex gap-4">
          <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
            <div className="text-sm text-gray-400">Buying Power</div>
            <div className="text-xl font-bold text-white">${account?.buyingPower?.toFixed(2) || '0.00'}</div>
          </div>
          <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
            <div className="text-sm text-gray-400">Portfolio Value</div>
            <div className="text-xl font-bold text-white">${account?.portfolioValue?.toFixed(2) || '0.00'}</div>
          </div>
        </div>
      </div>
      
      {/* Active Positions */}
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-6">
        <h2 className="text-xl font-bold text-white mb-4 flex items-center gap-2">
          <TrendingUp className="w-5 h-5" />
          Active Positions
        </h2>
        {!positions || positions.length === 0 ? (
          <p className="text-gray-500 text-center py-8">No active stock positions</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr className="text-left text-gray-400 text-sm border-b border-gray-800">
                  <th className="pb-3">Symbol</th>
                  <th className="pb-3">Shares</th>
                  <th className="pb-3">Avg Price</th>
                  <th className="pb-3">Current</th>
                  <th className="pb-3">P&L</th>
                  <th className="pb-3">P&L %</th>
                </tr>
              </thead>
              <tbody>
                {positions.map((pos: any) => (
                  <tr key={pos.symbol} className="border-b border-gray-800 hover:bg-gray-800/50">
                    <td className="py-3 font-semibold text-white">{pos.symbol}</td>
                    <td className="py-3 text-gray-300">{pos.shares}</td>
                    <td className="py-3 text-gray-300">${pos.avgPrice.toFixed(2)}</td>
                    <td className="py-3 text-gray-300">${pos.currentPrice.toFixed(2)}</td>
                    <td className={`py-3 font-semibold ${pos.pnl >= 0 ? 'text-green-500' : 'text-red-500'}`}>
                      ${pos.pnl.toFixed(2)}
                    </td>
                    <td className={`py-3 ${pos.pnlPercent >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                      {pos.pnlPercent >= 0 ? '+' : ''}{pos.pnlPercent.toFixed(2)}%
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
      
      {/* Trade History */}
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-6">
        <h2 className="text-xl font-bold text-white mb-4 flex items-center gap-2">
          <Activity className="w-5 h-5" />
          Recent Trades
        </h2>
        {!history || history.length === 0 ? (
          <p className="text-gray-500 text-center py-8">No trade history</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr className="text-left text-gray-400 text-sm border-b border-gray-800">
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
                {history.map((trade: any) => (
                  <tr key={trade.id} className="border-b border-gray-800 hover:bg-gray-800/50">
                    <td className="py-3 text-gray-300 text-sm">{format(new Date(trade.timestamp), 'MMM dd, HH:mm')}</td>
                    <td className="py-3 font-semibold text-white">{trade.symbol}</td>
                    <td className="py-3">
                      <span className={`px-2 py-1 rounded text-xs ${trade.side === 'BUY' ? 'bg-green-900 text-green-300' : 'bg-red-900 text-red-300'}`}>
                        {trade.side}
                      </span>
                    </td>
                    <td className="py-3 text-gray-300">{trade.shares}</td>
                    <td className="py-3 text-gray-300">${trade.price.toFixed(2)}</td>
                    <td className="py-3 text-gray-300">${trade.total.toFixed(2)}</td>
                    <td className="py-3">
                      <span className={`px-2 py-1 rounded text-xs ${
                        trade.status === 'FILLED' ? 'bg-green-900 text-green-300' : 
                        trade.status === 'REJECTED' ? 'bg-red-900 text-red-300' : 
                        'bg-yellow-900 text-yellow-300'
                      }`}>
                        {trade.status}
                      </span>
                    </td>
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
