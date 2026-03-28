import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { TrendingUp, Bitcoin, Activity, DollarSign } from 'lucide-react'

export default function Dashboard() {
  const { data: stockPositions } = useQuery({
    queryKey: ['stockPositions'],
    queryFn: api.getStockPositions,
    refetchInterval: 5000,
  })
  
  const { data: cryptoPositions } = useQuery({
    queryKey: ['cryptoPositions'],
    queryFn: api.getCryptoPositions,
    refetchInterval: 5000,
  })
  
  const { data: botStatus } = useQuery({
    queryKey: ['botStatus'],
    queryFn: api.getBotStatus,
    refetchInterval: 3000,
  })
  
  const { data: marketStatus } = useQuery({
    queryKey: ['marketStatus'],
    queryFn: api.getMarketStatus,
    refetchInterval: 60000,
  })
  
  const stockPnL = stockPositions?.reduce((sum: number, p: any) => sum + p.pnl, 0) || 0
  const cryptoPnL = cryptoPositions?.reduce((sum: number, p: any) => sum + p.pnl, 0) || 0
  const totalPnL = stockPnL + cryptoPnL
  
  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-3xl font-bold text-white">Dashboard</h1>
        <div className="flex items-center gap-3">
          <div className={`px-4 py-2 rounded-full ${marketStatus?.stock.isOpen ? 'bg-green-900 text-green-300' : 'bg-gray-800 text-gray-400'}`}>
            <div className="flex items-center gap-2">
              <TrendingUp className="w-4 h-4" />
              <span>Stock Market: {marketStatus?.stock.isOpen ? 'Open' : 'Closed'}</span>
            </div>
          </div>
          <div className={`px-4 py-2 rounded-full ${botStatus?.running ? 'bg-green-900 text-green-300' : 'bg-gray-800 text-gray-400'}`}>
            <div className="flex items-center gap-2">
              <Activity className="w-4 h-4" />
              <span>Bot: {botStatus?.running ? 'Active' : 'Inactive'}</span>
            </div>
          </div>
        </div>
      </div>
      
      {/* Summary Cards */}
      <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
        <StatCard
          title="Total P&L"
          value={`$${totalPnL.toFixed(2)}`}
          icon={<DollarSign className="w-6 h-6" />}
          trend={totalPnL >= 0 ? 'up' : 'down'}
        />
        <StatCard
          title="Stock P&L"
          value={`$${stockPnL.toFixed(2)}`}
          icon={<TrendingUp className="w-6 h-6" />}
          trend={stockPnL >= 0 ? 'up' : 'down'}
        />
        <StatCard
          title="Crypto P&L (Paper)"
          value={`$${cryptoPnL.toFixed(2)}`}
          icon={<Bitcoin className="w-6 h-6" />}
          trend={cryptoPnL >= 0 ? 'up' : 'down'}
        />
        <StatCard
          title="Active Positions"
          value={((stockPositions?.length || 0) + (cryptoPositions?.length || 0)).toString()}
          icon={<Activity className="w-6 h-6" />}
        />
      </div>
      
      {/* Markets Overview */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        <MarketPanel title="Stock Positions (Tradier)" positions={stockPositions || []} type="stock" />
        <MarketPanel title="Crypto Positions (Kraken Paper)" positions={cryptoPositions || []} type="crypto" />
      </div>
    </div>
  )
}

function StatCard({ title, value, icon, trend }: any) {
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg p-6">
      <div className="flex items-center justify-between mb-2">
        <span className="text-gray-400 text-sm">{title}</span>
        <div className={trend === 'up' ? 'text-green-500' : 'text-red-500'}>
          {icon}
        </div>
      </div>
      <div className={`text-2xl font-bold ${trend === 'up' ? 'text-green-500' : 'text-red-500'}`}>
        {value}
      </div>
    </div>
  )
}

function MarketPanel({ title, positions, type }: any) {
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg p-6">
      <h2 className="text-xl font-bold text-white mb-4">{title}</h2>
      {positions.length === 0 ? (
        <p className="text-gray-500 text-center py-8">No active positions</p>
      ) : (
        <div className="space-y-3">
          {positions.map((pos: any) => (
            <div key={pos.symbol || pos.pair} className="flex items-center justify-between p-3 bg-gray-800 rounded-lg">
              <div>
                <div className="font-semibold text-white">{pos.symbol || pos.pair}</div>
                <div className="text-sm text-gray-400">
                  {type === 'stock' ? `${pos.shares} shares @ $${pos.avgPrice.toFixed(2)}` : `${pos.amount.toFixed(6)} @ $${pos.avgPrice.toFixed(2)}`}
                </div>
              </div>
              <div className="text-right">
                <div className={`font-semibold ${pos.pnl >= 0 ? 'text-green-500' : 'text-red-500'}`}>
                  ${pos.pnl.toFixed(2)}
                </div>
                <div className={`text-sm ${pos.pnlPercent >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                  {pos.pnlPercent >= 0 ? '+' : ''}{pos.pnlPercent.toFixed(2)}%
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
