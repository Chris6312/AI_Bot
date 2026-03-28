import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import type { CryptoLedger, CryptoPosition, TradeHistoryEntry } from '@/types'
import { Bitcoin, Activity, TrendingUp } from 'lucide-react'
import { format } from 'date-fns'
import { TOP_15_CRYPTO_PAIRS } from '@/types'

export default function CryptoTrading() {
  const { data: ledger } = useQuery<CryptoLedger>({
    queryKey: ['cryptoPaperLedger'],
    queryFn: api.getCryptoPaperLedger,
    refetchInterval: 5000,
  })

  const { data: history = [] } = useQuery<TradeHistoryEntry[]>({
    queryKey: ['cryptoHistory'],
    queryFn: () => api.getCryptoHistory(100),
    refetchInterval: 10000,
  })

  const { data: prices = {} } = useQuery<Record<string, number>>({
    queryKey: ['cryptoPrices'],
    queryFn: () => api.getCryptoPrices(TOP_15_CRYPTO_PAIRS.map((pair) => pair.ohlcv)),
    refetchInterval: 5000,
  })

  const positions = ledger?.positions ?? []

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
        <div>
          <h1 className="text-3xl font-bold text-white">Crypto Trading</h1>
          <p className="mt-1 text-gray-400">Kraken CLI pricing with paper-ledger execution.</p>
        </div>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
          <AccountMetric label="Mode" value="PAPER" />
          <AccountMetric label="Cash Balance" value={`$${(ledger?.balance ?? 0).toFixed(2)}`} />
          <AccountMetric label="Equity" value={`$${(ledger?.equity ?? 0).toFixed(2)}`} />
        </div>
      </div>

      <div className="rounded-lg border border-gray-800 bg-gray-900 p-6">
        <h2 className="mb-4 flex items-center gap-2 text-xl font-bold text-white">
          <Bitcoin className="h-5 w-5" />
          Active Positions
        </h2>
        {positions.length === 0 ? (
          <p className="py-8 text-center text-gray-500">No active crypto positions</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr className="border-b border-gray-800 text-left text-sm text-gray-400">
                  <th className="pb-3">Pair</th>
                  <th className="pb-3">Amount</th>
                  <th className="pb-3">Avg Price</th>
                  <th className="pb-3">Current</th>
                  <th className="pb-3">Market Value</th>
                  <th className="pb-3">P&L</th>
                  <th className="pb-3">P&L %</th>
                </tr>
              </thead>
              <tbody>
                {positions.map((pos: CryptoPosition) => (
                  <tr key={pos.pair} className="border-b border-gray-800 hover:bg-gray-800/50">
                    <td className="py-3 font-semibold text-white">{pos.pair}</td>
                    <td className="py-3 text-gray-300">{pos.amount.toFixed(6)}</td>
                    <td className="py-3 text-gray-300">${pos.avgPrice.toFixed(2)}</td>
                    <td className="py-3 text-gray-300">${pos.currentPrice.toFixed(2)}</td>
                    <td className="py-3 text-gray-300">${pos.marketValue.toFixed(2)}</td>
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

      <div className="rounded-lg border border-gray-800 bg-gray-900 p-6">
        <h2 className="mb-4 flex items-center gap-2 text-xl font-bold text-white">
          <TrendingUp className="h-5 w-5" />
          Top 15 Liquid Pairs
        </h2>
        <div className="grid grid-cols-1 gap-3 md:grid-cols-3 lg:grid-cols-5">
          {TOP_15_CRYPTO_PAIRS.map(({ display, ohlcv }) => {
            const price = prices[ohlcv]
            return (
              <div key={display} className="rounded-lg bg-gray-800 p-4">
                <div className="text-sm text-gray-400">{display}</div>
                <div className="text-lg font-bold text-white">${price ? price.toFixed(2) : '—'}</div>
              </div>
            )
          })}
        </div>
      </div>

      <div className="rounded-lg border border-gray-800 bg-gray-900 p-6">
        <h2 className="mb-4 flex items-center gap-2 text-xl font-bold text-white">
          <Activity className="h-5 w-5" />
          Recent Trades (Paper)
        </h2>
        {history.length === 0 ? (
          <p className="py-8 text-center text-gray-500">No crypto trade history</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr className="border-b border-gray-800 text-left text-sm text-gray-400">
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
                {history.map((trade) => (
                  <tr key={trade.id} className="border-b border-gray-800 hover:bg-gray-800/50">
                    <td className="py-3 text-sm text-gray-300">{format(new Date(trade.timestamp), 'MMM dd, HH:mm')}</td>
                    <td className="py-3 font-semibold text-white">{trade.pair ?? '—'}</td>
                    <td className="py-3 text-gray-300">{trade.side}</td>
                    <td className="py-3 text-gray-300">{trade.amount?.toFixed(6) ?? '—'}</td>
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
