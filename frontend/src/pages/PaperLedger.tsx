import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import type { CryptoLedger } from '@/types'
import { FileText, TrendingUp, TrendingDown, Wallet } from 'lucide-react'
import { format } from 'date-fns'

export default function PaperLedger() {
  const { data: ledger } = useQuery<CryptoLedger>({
    queryKey: ['cryptoPaperLedger'],
    queryFn: api.getCryptoPaperLedger,
    refetchInterval: 5000,
  })

  const balance = ledger?.balance ?? 100000
  const equity = ledger?.equity ?? balance
  const totalTrades = ledger?.trades.length ?? 0
  const filledTrades = ledger?.trades.filter((trade) => trade.status === 'FILLED').length ?? 0
  const totalPnL = ledger?.totalPnL ?? 0

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-bold text-white">Crypto Paper Ledger</h1>
        <p className="mt-1 text-gray-400">Virtual trading account for Kraken-driven crypto decisions.</p>
      </div>

      <div className="grid grid-cols-1 gap-4 md:grid-cols-5">
        <LedgerStat title="Cash Balance" value={`$${balance.toFixed(2)}`} />
        <LedgerStat title="Equity" value={`$${equity.toFixed(2)}`} />
        <LedgerStat title="Open P&L" value={`$${totalPnL.toFixed(2)}`} valueClass={totalPnL >= 0 ? 'text-green-500' : 'text-red-500'} />
        <LedgerStat title="Total Trades" value={String(totalTrades)} />
        <LedgerStat title="Filled Trades" value={String(filledTrades)} />
      </div>

      <div className="rounded-lg border border-gray-800 bg-gray-900 p-6">
        <h2 className="mb-4 flex items-center gap-2 text-xl font-bold text-white">
          <Wallet className="h-5 w-5" />
          Open Positions Snapshot
        </h2>
        {!ledger?.positions.length ? (
          <p className="py-8 text-center text-gray-500">No open paper positions</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr className="border-b border-gray-800 text-left text-sm text-gray-400">
                  <th className="pb-3">Pair</th>
                  <th className="pb-3">Amount</th>
                  <th className="pb-3">Cost Basis</th>
                  <th className="pb-3">Market Value</th>
                  <th className="pb-3">P&L</th>
                </tr>
              </thead>
              <tbody>
                {ledger.positions.map((position) => (
                  <tr key={position.pair} className="border-b border-gray-800 hover:bg-gray-800/50">
                    <td className="py-3 font-semibold text-white">{position.pair}</td>
                    <td className="py-3 text-gray-300">{position.amount.toFixed(6)}</td>
                    <td className="py-3 text-gray-300">${position.costBasis.toFixed(2)}</td>
                    <td className="py-3 text-gray-300">${position.marketValue.toFixed(2)}</td>
                    <td className={`py-3 font-semibold ${position.pnl >= 0 ? 'text-green-500' : 'text-red-500'}`}>
                      ${position.pnl.toFixed(2)}
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
          <FileText className="h-5 w-5" />
          Trade History
        </h2>
        {!ledger?.trades.length ? (
          <p className="py-8 text-center text-gray-500">No paper trades yet</p>
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
                  <th className="pb-3">Balance After</th>
                </tr>
              </thead>
              <tbody>
                {ledger.trades.map((trade) => (
                  <tr key={trade.id} className="border-b border-gray-800 hover:bg-gray-800/50">
                    <td className="py-3 text-sm text-gray-300">{format(new Date(trade.timestamp), 'MMM dd, HH:mm:ss')}</td>
                    <td className="py-3 font-semibold text-white">
                      <div>{trade.pair}</div>
                      <div className="text-xs text-gray-500">{trade.ohlcvPair}</div>
                    </td>
                    <td className="py-3">
                      <span className={`flex w-fit items-center gap-1 rounded px-2 py-1 text-xs ${trade.side === 'BUY' ? 'bg-green-900 text-green-300' : 'bg-red-900 text-red-300'}`}>
                        {trade.side === 'BUY' ? <TrendingUp className="h-3 w-3" /> : <TrendingDown className="h-3 w-3" />}
                        {trade.side}
                      </span>
                    </td>
                    <td className="py-3 text-gray-300">{trade.amount?.toFixed(6) ?? '—'}</td>
                    <td className="py-3 text-gray-300">${(trade.price ?? 0).toFixed(2)}</td>
                    <td className="py-3 text-gray-300">${(trade.total ?? 0).toFixed(2)}</td>
                    <td className="py-3 text-gray-300">{trade.status}</td>
                    <td className="py-3 text-gray-300">${(trade.balance ?? 0).toFixed(2)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="rounded-lg border border-blue-800 bg-blue-900/20 p-4 text-sm text-blue-300">
        <strong>Note:</strong> The crypto ledger now surfaces cash, open market value, and live paper equity so the dashboard reflects the current paper account instead of only showing position P&L.
      </div>
    </div>
  )
}

function LedgerStat({ title, value, valueClass }: { title: string; value: string; valueClass?: string }) {
  return (
    <div className="rounded-lg border border-gray-800 bg-gray-900 p-6">
      <div className="mb-2 text-sm text-gray-400">{title}</div>
      <div className={`text-2xl font-bold ${valueClass ?? 'text-white'}`}>{value}</div>
    </div>
  )
}
