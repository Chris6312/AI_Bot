export type TradingMode = 'LIVE' | 'PAPER'

export interface ControlPlaneStatus {
  state: 'LOCKED' | 'READ_ONLY' | 'PAUSED' | 'ARMED' | string
  reason: string
  runtimeRunning: boolean
  adminApiReady: boolean
  discordAuthReady: boolean
  authorizationReady: boolean
  lastHeartbeat: string
}

export interface ExecutionGateStatus {
  allowed: boolean
  state: string
  reason: string
  statusCode: number
}

export interface DependencyCheck {
  name: string
  state: 'READY' | 'DEGRADED' | 'MISSING' | string
  ready: boolean
  reason: string
  checkedAtUtc: string
  details: Record<string, unknown>
}

export interface DependencyVisibility {
  observedAtUtc: string
  expiresAtUtc: string
  summary: {
    readyCount: number
    degradedCount: number
    missingCount: number
    criticalReady: boolean
  }
  checks: {
    tradierPaper: DependencyCheck
    tradierLive: DependencyCheck
    krakenMarketData: DependencyCheck
  }
}

export interface GateCheck {
  name: string
  passed: boolean
  reason: string
  details: Record<string, unknown>
}

export interface GateDecisionRecord {
  recordedAtUtc: string
  allowed: boolean
  assetClass: 'stock' | 'crypto' | string
  symbol: string
  state: string
  rejectionReason: string
  executionSource: string
  checks: GateCheck[]
  marketData: Record<string, unknown>
  riskData: Record<string, unknown>
  context: Record<string, unknown>
}

export interface GateSnapshot {
  capturedAtUtc: string
  summary: {
    total: number
    allowedCount: number
    rejectedCount: number
    lastDecision: GateDecisionRecord | null
    lastAllowed: GateDecisionRecord | null
    lastRejected: GateDecisionRecord | null
  }
  recent: GateDecisionRecord[]
  recentRejections: GateDecisionRecord[]
}

export interface RuntimeVisibility {
  capturedAtUtc: string
  controlPlane: ControlPlaneStatus
  executionGate: ExecutionGateStatus
  dependencies: DependencyVisibility
  gate: GateSnapshot
}

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
  controlPlane: ControlPlaneStatus
  executionGate: ExecutionGateStatus
  runtimeVisibility: {
    gateSummary: GateSnapshot['summary']
    dependencySummary: DependencyVisibility['summary']
    lastDecision: GateDecisionRecord | null
    lastRejected: GateDecisionRecord | null
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
