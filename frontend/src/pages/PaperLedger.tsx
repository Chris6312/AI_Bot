import { useEffect, useState } from 'react'
import {
  Wallet,
  TrendingUp,
  Activity,
  ArrowRightLeft,
  RefreshCw,
  AlertCircle
} from 'lucide-react'

interface LedgerBalance {
  cash: number
  equity: number
  unrealizedPnl: number
  realizedPnl: number
}

interface LedgerPosition {
  pair: string
  amount: number
  avgPrice: number
  currentPrice: number
  pnl: number
  pnlPct: number
}

interface LedgerTrade {
  id: string
  timestamp: string
  pair: string
  side: 'BUY' | 'SELL'
  amount: number
  price: number
  status: string
}

export default function PaperLedger() {
  const [balance, setBalance] = useState<LedgerBalance | null>(null)
  const [positions, setPositions] = useState<LedgerPosition[]>([])
  const [trades, setTrades] = useState<LedgerTrade[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const fetchLedgerData = async () => {
    setLoading(true)
    setError(null)
    try {
      // Replace with your actual backend endpoint when ready
      const res = await fetch('/api/crypto/paper/ledger')
      if (!res.ok) {
        throw new Error('Failed to fetch ledger data')
      }
      const data = await res.json()
      setBalance(data.balance)
      setPositions(data.positions || [])
      setTrades(data.trades || [])
    } catch (err) {
      console.error(err)
      // Fallback mock data for UI testing and development
      setBalance({
        cash: 100000.0,
        equity: 105230.5,
        unrealizedPnl: 2500.0,
        realizedPnl: 2730.5
      })
      setPositions([
        { pair: 'BTC/USD', amount: 1.5, avgPrice: 60000, currentPrice: 62000, pnl: 3000, pnlPct: 3.33 },
        { pair: 'ETH/USD', amount: 10, avgPrice: 3200, currentPrice: 3150, pnl: -500, pnlPct: -1.56 }
      ])
      setTrades([
        { id: 'trd-1', timestamp: new Date().toISOString(), pair: 'BTC/USD', side: 'BUY', amount: 1.5, price: 60000, status: 'FILLED' },
        { id: 'trd-2', timestamp: new Date(Date.now() - 86400000).toISOString(), pair: 'ETH/USD', side: 'BUY', amount: 10, price: 3200, status: 'FILLED' }
      ])
      setError('Using mock data. Backend endpoint /api/crypto/paper/ledger unavailable.')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchLedgerData()
  }, [])

  const formatCurrency = (val: number) =>
    new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(val)

  const formatCrypto = (val: number) =>
    new Intl.NumberFormat('en-US', { maximumFractionDigits: 6 }).format(val)

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-white">Paper Trading Ledger</h1>
          <p className="text-sm text-slate-400">Simulated execution environment and balance tracking</p>
        </div>
        <button
          onClick={fetchLedgerData}
          disabled={loading}
          className="flex items-center gap-2 rounded-lg border border-slate-700 bg-slate-800 px-4 py-2 text-sm font-medium text-slate-200 transition hover:bg-slate-700 disabled:opacity-50"
        >
          <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
          Refresh
        </button>
      </div>

      {error && (
        <div className="flex items-center gap-3 rounded-lg border border-amber-900/50 bg-amber-900/20 p-4 text-amber-200">
          <AlertCircle className="h-5 w-5 flex-shrink-0 text-amber-500" />
          <p className="text-sm">{error}</p>
        </div>
      )}

      {/* Metrics Grid */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <div className="rounded-xl border border-slate-800 bg-[#020b2a] p-5 shadow-sm">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-indigo-500/10 text-indigo-400">
              <Wallet className="h-5 w-5" />
            </div>
            <div className="text-sm font-medium text-slate-400">Available Cash</div>
          </div>
          <div className="mt-4 text-2xl font-bold text-white">
            {balance ? formatCurrency(balance.cash) : '---'}
          </div>
        </div>

        <div className="rounded-xl border border-slate-800 bg-[#020b2a] p-5 shadow-sm">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-cyan-500/10 text-cyan-400">
              <Activity className="h-5 w-5" />
            </div>
            <div className="text-sm font-medium text-slate-400">Total Equity</div>
          </div>
          <div className="mt-4 text-2xl font-bold text-white">
            {balance ? formatCurrency(balance.equity) : '---'}
          </div>
        </div>

        <div className="rounded-xl border border-slate-800 bg-[#020b2a] p-5 shadow-sm">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-emerald-500/10 text-emerald-400">
              <TrendingUp className="h-5 w-5" />
            </div>
            <div className="text-sm font-medium text-slate-400">Unrealized P&L</div>
          </div>
          <div className={`mt-4 text-2xl font-bold ${balance && balance.unrealizedPnl >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
            {balance ? (balance.unrealizedPnl >= 0 ? '+' : '') + formatCurrency(balance.unrealizedPnl) : '---'}
          </div>
        </div>

        <div className="rounded-xl border border-slate-800 bg-[#020b2a] p-5 shadow-sm">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-fuchsia-500/10 text-fuchsia-400">
              <ArrowRightLeft className="h-5 w-5" />
            </div>
            <div className="text-sm font-medium text-slate-400">Realized P&L</div>
          </div>
          <div className={`mt-4 text-2xl font-bold ${balance && balance.realizedPnl >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
            {balance ? (balance.realizedPnl >= 0 ? '+' : '') + formatCurrency(balance.realizedPnl) : '---'}
          </div>
        </div>
      </div>

      {/* Open Positions */}
      <div className="rounded-xl border border-slate-800 bg-[#020b2a] shadow-sm">
        <div className="border-b border-slate-800 px-6 py-4">
          <h2 className="text-lg font-semibold text-white">Open Positions</h2>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm text-slate-300">
            <thead className="bg-slate-900/50 text-xs uppercase tracking-wider text-slate-500">
              <tr>
                <th className="px-6 py-3 font-medium">Asset Pair</th>
                <th className="px-6 py-3 font-medium text-right">Amount</th>
                <th className="px-6 py-3 font-medium text-right">Avg Entry</th>
                <th className="px-6 py-3 font-medium text-right">Current Price</th>
                <th className="px-6 py-3 font-medium text-right">Unrealized P&L</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800/60">
              {positions.length === 0 ? (
                <tr>
                  <td colSpan={5} className="px-6 py-8 text-center text-slate-500">
                    No open positions found.
                  </td>
                </tr>
              ) : (
                positions.map((pos, idx) => (
                  <tr key={idx} className="transition hover:bg-slate-800/30">
                    <td className="whitespace-nowrap px-6 py-4 font-medium text-white">{pos.pair}</td>
                    <td className="whitespace-nowrap px-6 py-4 text-right">{formatCrypto(pos.amount)}</td>
                    <td className="whitespace-nowrap px-6 py-4 text-right">{formatCurrency(pos.avgPrice)}</td>
                    <td className="whitespace-nowrap px-6 py-4 text-right">{formatCurrency(pos.currentPrice)}</td>
                    <td className={`whitespace-nowrap px-6 py-4 text-right font-medium ${pos.pnl >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                      {pos.pnl >= 0 ? '+' : ''}{formatCurrency(pos.pnl)} ({pos.pnlPct.toFixed(2)}%)
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* Trade History */}
      <div className="rounded-xl border border-slate-800 bg-[#020b2a] shadow-sm">
        <div className="border-b border-slate-800 px-6 py-4">
          <h2 className="text-lg font-semibold text-white">Recent Fills</h2>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm text-slate-300">
            <thead className="bg-slate-900/50 text-xs uppercase tracking-wider text-slate-500">
              <tr>
                <th className="px-6 py-3 font-medium">Time</th>
                <th className="px-6 py-3 font-medium">Pair</th>
                <th className="px-6 py-3 font-medium">Side</th>
                <th className="px-6 py-3 font-medium text-right">Amount</th>
                <th className="px-6 py-3 font-medium text-right">Fill Price</th>
                <th className="px-6 py-3 font-medium">Status</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800/60">
              {trades.length === 0 ? (
                <tr>
                  <td colSpan={6} className="px-6 py-8 text-center text-slate-500">
                    No recent trade activity.
                  </td>
                </tr>
              ) : (
                trades.map((trade, idx) => (
                  <tr key={idx} className="transition hover:bg-slate-800/30">
                    <td className="whitespace-nowrap px-6 py-4 text-slate-400">
                      {new Date(trade.timestamp).toLocaleString()}
                    </td>
                    <td className="whitespace-nowrap px-6 py-4 font-medium text-white">{trade.pair}</td>
                    <td className="whitespace-nowrap px-6 py-4">
                      <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-semibold ${
                        trade.side === 'BUY' ? 'bg-emerald-500/10 text-emerald-400' : 'bg-rose-500/10 text-rose-400'
                      }`}>
                        {trade.side}
                      </span>
                    </td>
                    <td className="whitespace-nowrap px-6 py-4 text-right">{formatCrypto(trade.amount)}</td>
                    <td className="whitespace-nowrap px-6 py-4 text-right">{formatCurrency(trade.price)}</td>
                    <td className="whitespace-nowrap px-6 py-4">
                      <span className="inline-flex items-center gap-1.5 text-slate-300">
                        <span className="h-1.5 w-1.5 rounded-full bg-cyan-400"></span>
                        {trade.status}
                      </span>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}