export interface StockPosition {
  symbol: string
  shares: number
  avgPrice: number
  currentPrice: number
  pnl: number
  pnlPercent: number
}

export interface CryptoPosition {
  pair: string
  ohlcvPair: string // XBTUSD format for Kraken
  amount: number
  avgPrice: number
  currentPrice: number
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

export interface PaperTrade {
  id: string
  timestamp: string
  market: 'CRYPTO'
  pair: string
  ohlcvPair: string
  side: 'BUY' | 'SELL'
  amount: number
  price: number
  total: number
  status: 'PENDING' | 'FILLED' | 'REJECTED'
  balance: number
}

export interface MarketStatus {
  stock: {
    isOpen: boolean
    nextOpen?: string
    nextClose?: string
  }
  crypto: {
    isOpen: boolean // Always true
  }
}

export interface BotStatus {
  running: boolean
  mode: 'LIVE' | 'PAPER'
  stockMode: 'LIVE' | 'PAPER'
  cryptoMode: 'PAPER' // Always paper for crypto
  lastHeartbeat: string
  safetyRequireMarketHours: boolean
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
