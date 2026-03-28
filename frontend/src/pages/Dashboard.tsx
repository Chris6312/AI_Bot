import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import type { BotStatus, CryptoLedger, MarketStatus, StockAccount, StockPosition, CryptoPosition } from '@/types'
import { TrendingUp, Bitcoin, Activity, DollarSign, Wallet } from 'lucide-react'

function formatMoney(value: number) {
  return `$${value.toFixed(2)}`
}

export default function Dashboard() {
  const { data: stockPositions = [] } = useQuery<StockPosition[]>({
    queryKey: ['stockPositions'],
    queryFn: api.getStockPositions,
    refetchInterval: 5000,
  })

  const { data: cryptoPositions = [] } = useQuery<CryptoPosition[]>({
    queryKey: ['cryptoPositions'],
    queryFn: api.getCryptoPositions,
    refetchInterval: 5000,
  })

  const { data: botStatus } = useQuery<BotStatus>({
    queryKey: ['botStatus'],
    queryFn: api.getBotStatus,
    refetchInterval: 3000,
  })

  const { data: marketStatus } = useQuery<MarketStatus>({
    queryKey: ['marketStatus'],
    queryFn: api.getMarketStatus,
    refetchInterval: 60000,
  })

  const { data: stockAccount } = useQuery<StockAccount>({
    queryKey: ['stockAccount'],
    queryFn: api.getStockAccount,
    refetchInterval: 10000,
  })

  const { data: cryptoLedger } = useQuery<CryptoLedger>({
    queryKey: ['cryptoPaperLedger'],
    queryFn: api.getCryptoPaperLedger,
    refetchInterval: 5000,
  })

  const summary = useMemo(() => {
    const stockEquity = stockAccount?.portfolioValue ?? 0
    const cryptoEquity = cryptoLedger?.equity ?? 0
    const stockPnL = stockAccount?.unrealizedPnL ?? stockPositions.reduce((sum, p) => sum + p.pnl, 0)
    const cryptoPnL = cryptoLedger?.totalPnL ?? cryptoPositions.reduce((sum, p) => sum + p.pnl, 0)
    return {
      stockEquity,
      cryptoEquity,
      totalEquity: stockEquity + cryptoEquity,
      openPnL: stockPnL + cryptoPnL,
      activePositions: stockPositions.length + cryptoPositions.length,
    }
  }, [cryptoLedger, cryptoPositions, stockAccount, stockPositions])

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
        <div>
          <h1 className="text-3xl font-bold text-white">Dashboard</h1>
          <p className="mt-1 text-gray-400">Live portfolio view for Tradier and the Kraken paper ledger.</p>
        </div>
        <div className="flex flex-wrap gap-3">
          <StatusPill
            active={marketStatus?.stock.isOpen ?? false}
            icon={<TrendingUp className="h-4 w-4" />}
            label={`Stock Market: ${marketStatus?.stock.isOpen ? 'Open' : 'Closed'}`}
          />
          <StatusPill
            active={botStatus?.running ?? false}
            icon={<Activity className="h-4 w-4" />}
            label={`Bot: ${botStatus?.running ? 'Active' : 'Inactive'}`}
          />
          <StatusPill
            active={(botStatus?.stockMode ?? 'PAPER') === 'LIVE'}
            icon={<Wallet className="h-4 w-4" />}
            label={`Stock Mode: ${botStatus?.stockMode ?? 'PAPER'}`}
          />
        </div>
      </div>

      <div className="grid grid-cols-1 gap-4 md:grid-cols-5">
        <StatCard title="Total Equity" value={formatMoney(summary.totalEquity)} icon={<DollarSign className="h-6 w-6" />} trend={summary.totalEquity >= 0 ? 'up' : 'down'} />
        <StatCard title="Stock Equity" value={formatMoney(summary.stockEquity)} icon={<TrendingUp className="h-6 w-6" />} trend={summary.stockEquity >= 0 ? 'up' : 'down'} />
        <StatCard title="Crypto Equity" value={formatMoney(summary.cryptoEquity)} icon={<Bitcoin className="h-6 w-6" />} trend={summary.cryptoEquity >= 0 ? 'up' : 'down'} />
        <StatCard title="Open P&L" value={formatMoney(summary.openPnL)} icon={<Activity className="h-6 w-6" />} trend={summary.openPnL >= 0 ? 'up' : 'down'} />
        <StatCard title="Active Positions" value={String(summary.activePositions)} icon={<Wallet className="h-6 w-6" />} />
      </div>

      <div className="grid grid-cols-1 gap-6 md:grid-cols-2">
        <MarketPanel title={`Stock Positions (${botStatus?.stockMode ?? 'PAPER'})`} positions={stockPositions} type="stock" emptyMessage="No active stock positions" />
        <MarketPanel title="Crypto Positions (PAPER)" positions={cryptoPositions} type="crypto" emptyMessage="No active crypto positions" />
      </div>
    </div>
  )
}

function StatusPill({ active, icon, label }: { active: boolean; icon: React.ReactNode; label: string }) {
  return (
    <div className={`rounded-full px-4 py-2 ${active ? 'bg-green-900 text-green-300' : 'bg-gray-800 text-gray-400'}`}>
      <div className="flex items-center gap-2">
        {icon}
        <span>{label}</span>
      </div>
    </div>
  )
}

function StatCard({ title, value, icon, trend }: { title: string; value: string; icon: React.ReactNode; trend?: 'up' | 'down' }) {
  const iconClass = trend === 'down' ? 'text-red-500' : 'text-green-500'
  const valueClass = trend === 'down' ? 'text-red-500' : trend === 'up' ? 'text-green-500' : 'text-white'

  return (
    <div className="rounded-lg border border-gray-800 bg-gray-900 p-6">
      <div className="mb-2 flex items-center justify-between">
        <span className="text-sm text-gray-400">{title}</span>
        <div className={iconClass}>{icon}</div>
      </div>
      <div className={`text-2xl font-bold ${valueClass}`}>{value}</div>
    </div>
  )
}

function MarketPanel({
  title,
  positions,
  type,
  emptyMessage,
}: {
  title: string
  positions: Array<StockPosition | CryptoPosition>
  type: 'stock' | 'crypto'
  emptyMessage: string
}) {
  return (
    <div className="rounded-lg border border-gray-800 bg-gray-900 p-6">
      <h2 className="mb-4 text-xl font-bold text-white">{title}</h2>
      {positions.length === 0 ? (
        <p className="py-8 text-center text-gray-500">{emptyMessage}</p>
      ) : (
        <div className="space-y-3">
          {positions.map((position) => {
            const key = type === 'stock' ? (position as StockPosition).symbol : (position as CryptoPosition).pair
            const qtyLabel =
              type === 'stock'
                ? `${(position as StockPosition).shares.toFixed(0)} shares`
                : `${(position as CryptoPosition).amount.toFixed(6)}`
            return (
              <div key={key} className="flex items-center justify-between rounded-lg bg-gray-800 p-3">
                <div>
                  <div className="font-semibold text-white">{key}</div>
                  <div className="text-sm text-gray-400">
                    {qtyLabel} @ ${position.avgPrice.toFixed(2)}
                  </div>
                </div>
                <div className="text-right">
                  <div className={`font-semibold ${position.pnl >= 0 ? 'text-green-500' : 'text-red-500'}`}>
                    {formatMoney(position.pnl)}
                  </div>
                  <div className={`text-sm ${position.pnlPercent >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                    {position.pnlPercent >= 0 ? '+' : ''}
                    {position.pnlPercent.toFixed(2)}%
                  </div>
                </div>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
