import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { Bitcoin, Activity, TrendingUp } from 'lucide-react'
import { format } from 'date-fns'
import { TOP_15_CRYPTO_PAIRS } from '@/types'

export default function CryptoTrading() {
  const { data: positions } = useQuery({
    queryKey: ['cryptoPositions'],
    queryFn: api.getCryptoPositions,
    refetchInterval: 5000,
  })
  
  const { data: history } = useQuery({
    queryKey: ['cryptoHistory'],
    queryFn: () => api.getCryptoHistory(100),
    refetchInterval: 10000,
  })
  
  const { data: prices } = useQuery({
    queryKey: ['cryptoPrices'],
    queryFn: () => api.getCryptoPrices(TOP_15_CRYPTO_PAIRS.map(p => p.ohlcv)),
    refetchInterval: 5000,
  })
  
  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold text-white">Crypto Trading</h1>
          <p className="text-gray-400 mt-1">Kraken Integration - Paper Trading Only</p>
        </div>
        <div className="px-4 py-2 bg-yellow-900 text-yellow-300 rounded-lg">
          <div className="flex items-center gap-2">
            <Bitcoin className="w-4 h-4" />
            <span>Paper Mode</span>
          </div>
        </div>
      </div>
      
      {/* Active Positions */}
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-6">
        <h2 className="text-xl font-bold text-white mb-4 flex items-center gap-2">
          <Bitcoin className="w-5 h-5" />
          Active Positions
        </h2>
        {!positions || positions.length === 0 ? (
          <p className="text-gray-500 text-center py-8">No active crypto positions</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr className="text-left text-gray-400 text-sm border-b border-gray-800">
                  <th className="pb-3">Pair</th>
                  <th className="pb-3">Amount</th>
                  <th className="pb-3">Avg Price</th>
                  <th className="pb-3">Current</th>
                  <th className="pb-3">P&L</th>
                  <th className="pb-3">P&L %</th>
                </tr>
              </thead>
              <tbody>
                {positions.map((pos: any) => (
                  <tr key={pos.pair} className="border-b border-gray-800 hover:bg-gray-800/50">
                    <td className="py-3 font-semibold text-white">{pos.pair}</td>
                    <td className="py-3 text-gray-300">{pos.amount.toFixed(6)}</td>
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
      
      {/* Top Crypto Pairs */}
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-6">
        <h2 className="text-xl font-bold text-white mb-4 flex items-center gap-2">
          <TrendingUp className="w-5 h-5" />
          Top 15 Liquid Pairs
        </h2>
        <div className="grid grid-cols-1 md:grid-cols-3 lg:grid-cols-5 gap-3">
          {TOP_15_CRYPTO_PAIRS.map(({ display, ohlcv }) => {
            const price = prices?.[ohlcv]
            return (
              <div key={display} className="bg-gray-800 rounded-lg p-4">
                <div className="text-sm text-gray-400">{display}</div>
                <div className="text-lg font-bold text-white">
                  ${price ? price.toFixed(2) : '—'}
                </div>
              </div>
            )
          })}
        </div>
      </div>
      
      {/* Trade History */}
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-6">
        <h2 className="text-xl font-bold text-white mb-4 flex items-center gap-2">
          <Activity className="w-5 h-5" />
          Recent Trades (Paper)
        </h2>
        {!history || history.length === 0 ? (
          <p className="text-gray-500 text-center py-8">No trade history</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr className="text-left text-gray-400 text-sm border-b border-gray-800">
                  <th className="pb-3">Time</th>
                  <th className="pb-3">Pair</th>
                  <th className="pb-3">Side</th>
                  <th className="pb-3">Amount</th>
                  <th className="pb-3">Price</th>
                  <th className="pb-3">Total</th>
                  <th className="pb-3">Status</th>
                </tr>
              </thead>
              <tbody>
                {history.map((trade: any) => (
                  <tr key={trade.id} className="border-b border-gray-800 hover:bg-gray-800/50">
                    <td className="py-3 text-gray-300 text-sm">{format(new Date(trade.timestamp), 'MMM dd, HH:mm')}</td>
                    <td className="py-3 font-semibold text-white">{trade.pair}</td>
                    <td className="py-3">
                      <span className={`px-2 py-1 rounded text-xs ${trade.side === 'BUY' ? 'bg-green-900 text-green-300' : 'bg-red-900 text-red-300'}`}>
                        {trade.side}
                      </span>
                    </td>
                    <td className="py-3 text-gray-300">{trade.amount.toFixed(6)}</td>
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
