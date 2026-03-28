export type TradingMode = 'LIVE' | 'PAPER'

export interface StockPosition {
  symbol: string
  shares: number
  avgPrice: number
  currentPrice: number
  marketValue: number
  pnl: number
  pnlPercent: number
}

export interface CryptoPosition {
  pair: string
  ohlcvPair: string
  amount: number
  avgPrice: number
  currentPrice: number
  marketValue: number
  costBasis: number
  pnl: number
  pnlPercent: number
}

export interface AIDecision {
  id: string
  timestamp: string
  type: 'SCREENING' | 'BUY' | 'SELL' | 'HOLD'
  market: 'STOCK' | 'CRYPTO'
  symbol: string
  confidence: number
  reasoning: string
  executed: boolean
  rejected?: boolean
  rejectionReason?: string
  vix?: number
}

export interface TradeHistoryEntry {
  id: string
  timestamp: string
  market?: 'STOCK' | 'CRYPTO'
  pair?: string
  ohlcvPair?: string
  symbol?: string
  side: 'BUY' | 'SELL'
  shares?: number
  amount?: number
  price?: number
  total?: number
  status: 'PENDING' | 'FILLED' | 'REJECTED' | string
  balance?: number
}

export interface StockAccount {
  mode: TradingMode
  connected: boolean
  accountId: string
  buyingPower: number
  portfolioValue: number
  cash: number
  unrealizedPnL: number
  dailyPnL: number
}

export interface CryptoLedger {
  balance: number
  startingBalance: number
  equity: number
  marketValue: number
  totalPnL: number
  trades: TradeHistoryEntry[]
  positions: CryptoPosition[]
}

export interface MarketStatus {
  stock: {
    isOpen: boolean
    nextOpen?: string
    nextClose?: string
  }
  crypto: {
    isOpen: boolean
  }
}

export interface BotStatus {
  running: boolean
  mode: TradingMode
  stockMode: TradingMode
  cryptoMode: 'PAPER'
  lastHeartbeat: string
  safetyRequireMarketHours: boolean
  stockCapabilities: {
    paperReady: boolean
    liveReady: boolean
  }
  cryptoCapabilities: {
    paperReady: boolean
    liveReady: boolean
  }
}

export interface CryptoCandle {
  timestamp: string
  open: number
  high: number
  low: number
  close: number
  volume: number
}

export const TOP_15_CRYPTO_PAIRS = [
  { display: 'BTC/USD', ohlcv: 'XBTUSD' },
  { display: 'ETH/USD', ohlcv: 'ETHUSD' },
  { display: 'SOL/USD', ohlcv: 'SOLUSD' },
  { display: 'XRP/USD', ohlcv: 'XRPUSD' },
  { display: 'ADA/USD', ohlcv: 'ADAUSD' },
  { display: 'AVAX/USD', ohlcv: 'AVAXUSD' },
  { display: 'DOT/USD', ohlcv: 'DOTUSD' },
  { display: 'MATIC/USD', ohlcv: 'MATICUSD' },
  { display: 'LINK/USD', ohlcv: 'LINKUSD' },
  { display: 'UNI/USD', ohlcv: 'UNIUSD' },
  { display: 'ATOM/USD', ohlcv: 'ATOMUSD' },
  { display: 'LTC/USD', ohlcv: 'LTCUSD' },
  { display: 'BCH/USD', ohlcv: 'BCHUSD' },
  { display: 'ALGO/USD', ohlcv: 'ALGOUSD' },
  { display: 'XLM/USD', ohlcv: 'XLMUSD' },
]
