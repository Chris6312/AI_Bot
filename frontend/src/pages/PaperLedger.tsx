import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { FileText, TrendingUp, TrendingDown } from 'lucide-react'
import { format } from 'date-fns'

export default function PaperLedger() {
  const { data: ledger } = useQuery({
    queryKey: ['cryptoPaperLedger'],
    queryFn: api.getCryptoPaperLedger,
    refetchInterval: 5000,
  })
  
  const balance = ledger?.balance || 100000
  const totalTrades = ledger?.trades?.length || 0
  const filledTrades = ledger?.trades?.filter((t: any) => t.status === 'FILLED').length || 0
  const totalPnL = ledger?.totalPnL || 0
  
  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold text-white">Crypto Paper Ledger</h1>
          <p className="text-gray-400 mt-1">Virtual trading account for Kraken crypto</p>
        </div>
      </div>
      
      {/* Stats */}
      <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-6">
          <div className="text-sm text-gray-400 mb-2">Paper Balance</div>
          <div className="text-2xl font-bold text-white">${balance.toFixed(2)}</div>
        </div>
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-6">
          <div className="text-sm text-gray-400 mb-2">Total P&L</div>
          <div className={`text-2xl font-bold ${totalPnL >= 0 ? 'text-green-500' : 'text-red-500'}`}>
            ${totalPnL.toFixed(2)}
          </div>
        </div>
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-6">
          <div className="text-sm text-gray-400 mb-2">Total Trades</div>
          <div className="text-2xl font-bold text-white">{totalTrades}</div>
        </div>
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-6">
          <div className="text-sm text-gray-400 mb-2">Filled Trades</div>
          <div className="text-2xl font-bold text-white">{filledTrades}</div>
        </div>
      </div>
      
      {/* Trade History */}
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-6">
        <h2 className="text-xl font-bold text-white mb-4 flex items-center gap-2">
          <FileText className="w-5 h-5" />
          Trade History
        </h2>
        {!ledger?.trades || ledger.trades.length === 0 ? (
          <p className="text-gray-500 text-center py-8">No paper trades yet</p>
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
                  <th className="pb-3">Balance After</th>
                </tr>
              </thead>
              <tbody>
                {ledger.trades.map((trade: any) => (
                  <tr key={trade.id} className="border-b border-gray-800 hover:bg-gray-800/50">
                    <td className="py-3 text-gray-300 text-sm">
                      {format(new Date(trade.timestamp), 'MMM dd, HH:mm:ss')}
                    </td>
                    <td className="py-3 font-semibold text-white">
                      <div>{trade.pair}</div>
                      <div className="text-xs text-gray-500">{trade.ohlcvPair}</div>
                    </td>
                    <td className="py-3">
                      <span className={`px-2 py-1 rounded text-xs flex items-center gap-1 w-fit ${
                        trade.side === 'BUY' 
                          ? 'bg-green-900 text-green-300' 
                          : 'bg-red-900 text-red-300'
                      }`}>
                        {trade.side === 'BUY' ? <TrendingUp className="w-3 h-3" /> : <TrendingDown className="w-3 h-3" />}
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
                    <td className="py-3 text-gray-300">${trade.balance.toFixed(2)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
      
      {/* Note */}
      <div className="bg-blue-900/20 border border-blue-800 rounded-lg p-4">
        <p className="text-blue-300 text-sm">
          <strong>Note:</strong> This is a paper trading ledger. All crypto trades are simulated and no real money is involved. 
          Starting balance: $100,000 USD. Prices fetched from Kraken in real-time.
        </p>
      </div>
    </div>
  )
}
