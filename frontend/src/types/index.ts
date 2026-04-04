export type TradingMode = 'LIVE' | 'PAPER'
export type WatchlistScope = 'stocks_only' | 'crypto_only'

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
  state: 'READY' | 'DEGRADED' | 'MISSING' | 'STALE' | 'DISABLED' | string
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
    staleCount: number
    disabledCount: number
    criticalReady: boolean
    workerReady: boolean
    operationalReady: boolean
  }
  checks: {
    tradierPaper: DependencyCheck
    tradierLive: DependencyCheck
    krakenMarketData: DependencyCheck
    watchlistMonitor: DependencyCheck
    watchlistExitWorker: DependencyCheck
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


export interface AuditReplayRejection {
  recordedAtUtc: string
  reason: string
  messageId: string
  authorId: string
  channelId: string
  schemaVersion: string
  scope: string
  provider: string
  payloadHash: string
}

export interface AuditSystemError {
  id: string
  timestamp: string
  source: string
  component: string
  severity: 'error' | 'warn' | string
  state: string
  message: string
  symbol?: string | null
  details: Record<string, unknown>
}

export interface AuditExitEvent {
  id: string
  timestamp: string
  symbol: string
  assetClass: string
  status: string
  eventType: string
  executionSource: string
  trigger: string
  message: string
  details: Record<string, unknown>
}

export interface RuntimeVisibility {
  capturedAtUtc: string
  controlPlane: ControlPlaneStatus
  executionGate: ExecutionGateStatus
  dependencies: DependencyVisibility
  gate: GateSnapshot
  audit: {
    replayRejections: AuditReplayRejection[]
    systemErrors: AuditSystemError[]
    exitTimeline: AuditExitEvent[]
  }
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
  entryTimeUtc?: string | null
  realizedPnl?: number
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



export interface OrderEventRecord {
  eventType: string
  status: string
  message: string
  eventTime: string | null
  payload: Record<string, unknown>
}

export interface OrderIntentRecord {
  intentId: string
  accountId: string
  assetClass: string
  symbol: string
  side: string
  requestedQuantity: number
  requestedPrice: number | null
  filledQuantity: number
  avgFillPrice: number | null
  status: string
  executionSource: string
  submittedOrderId?: string | null
  positionId?: number | null
  tradeId?: number | null
  rejectionReason?: string | null
  submittedAt?: string | null
  firstFillAt?: string | null
  lastFillAt?: string | null
  context: Record<string, unknown>
  events: OrderEventRecord[]
}

export interface TradeHistoryRow {
  id: string
  tradeId?: string | null
  assetClass: 'stock' | 'crypto' | string
  mode: 'PAPER' | 'LIVE' | string
  symbol: string
  buyIntentId?: string | null
  sellIntentId?: string | null
  source: string
  boughtAtUtc?: string | null
  boughtAtEt?: string | null
  buyPrice: number
  buyQuantity: number
  buyTotal: number
  soldAtUtc?: string | null
  soldAtEt?: string | null
  sellPrice: number
  sellQuantity: number
  sellTotal: number
  priceDifference: number
  differenceAmount: number
  fees: number
  realizedPnl: number
  holdDurationMinutes?: number | null
  exitTrigger?: string | null
}

export interface TradeHistoryResponse {
  rows: TradeHistoryRow[]
  summary: {
    totalCount: number
    realizedPnl: number
    winCount: number
    lossCount: number
    assetCounts: {
      stock: number
      crypto: number
    }
    modeCounts: {
      PAPER: number
      LIVE: number
    }
    dateRange: {
      fromUtc?: string | null
      toUtc?: string | null
      fromEt?: string | null
      toEt?: string | null
    }
  }
  filters: {
    mode: string
    assetClass: string
    symbol: string
    dateFromUtc?: string | null
    dateToUtc?: string | null
    dateFromEt?: string | null
    dateToEt?: string | null
  }
  generatedAtUtc?: string | null
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
  brokerBuyingPower?: number
  availableToTrade?: number
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
  realizedPnL?: number
  netPnL?: number
  returnPct?: number
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

export interface WatchlistSymbolContext {
  thesis?: string
  why_now?: string
  notes?: string
  [key: string]: unknown
}

export interface WatchlistSymbolMonitoring {
  latestDecisionState: string
  latestDecisionReason: string
  decisionContext: Record<string, unknown>
  requiredTimeframes: string[]
  evaluationIntervalSeconds: number | null
  lastDecisionAtUtc: string | null
  lastEvaluatedAtUtc: string | null
  nextEvaluationAtUtc: string | null
  lastMarketDataAtUtc: string | null
}

export interface WatchlistPositionState {
  hasOpenPosition: boolean
  positionState?: string | null
  entryTimeUtc?: string | null
  basePositionExpiresAtUtc?: string | null
  positionExpiresAtUtc?: string | null
  positionExpired?: boolean
  hoursUntilExpiry?: number | null
  hoursSinceEntry?: number | null
  followThroughWindowHours?: number | null
  followThroughFailed?: boolean
  timeStopStructureCheckPassed?: boolean
  timeStopExtended?: boolean
  timeStopExtensionHours?: number | null
  timeStopExtendedUntilUtc?: string | null
  exitDeadlineSource?: string | null
  stopLoss?: number | null
  profitTarget?: number | null
  trailingStop?: number | null
  protectiveExitPending?: boolean
  protectiveExitReasons?: string[]
  stopLossBreached?: boolean
  trailingStopBreached?: boolean
  profitTargetReached?: boolean
  scaleOutReady?: boolean
  scaleOutAlreadyTaken?: boolean
  impulseTrailArmed?: boolean
  impulseTrailingStop?: number | null
  peakPrice?: number | null
  currentPrice?: number | null
  unrealizedPnl?: number | null
  unrealizedPnlPct?: number | null
  maxHoldHours?: number | null
}

export interface WatchlistSymbolRecord {
  symbol: string
  quoteCurrency: string
  assetClass: string
  enabled: boolean
  tradeDirection: string
  priorityRank: number
  tier: string
  bias: string
  setupTemplate: string
  botTimeframes: string[]
  exitTemplate: string
  maxHoldHours: number
  riskFlags: string[]
  monitoringStatus: string
  uploadId: string
  managedOnly?: boolean
  monitoring?: WatchlistSymbolMonitoring | null
  positionState?: WatchlistPositionState | null
}

export interface WatchlistUploadRecord {
  uploadId: string
  scanId: string
  schemaVersion: string
  provider: string
  scope: WatchlistScope
  source: string
  sourceUserId?: string | null
  sourceChannelId?: string | null
  sourceMessageId?: string | null
  payloadHash?: string | null
  generatedAtUtc?: string | null
  generatedAtUtcSource?: string | null
  receivedAtUtc?: string | null
  watchlistExpiresAtUtc?: string | null
  validationStatus: string
  rejectionReason?: string | null
  marketRegime?: string | null
  selectedCount: number
  isActive: boolean
  validation: Record<string, unknown>
  symbols: WatchlistSymbolRecord[]
  managedOnlySymbols: WatchlistSymbolRecord[]
  statusSummary: {
    activeCount: number
    managedOnlyCount: number
    inactiveCount: number
  }
  monitoringSummary: WatchlistMonitoringSummary
  targetSessionEt?: string | null
  targetSessionSource?: string | null
  uiPayload: {
    summary: Record<string, unknown>
    providerLimitations: string[]
    symbolContext: Record<string, WatchlistSymbolContext>
  }
}

export interface WatchlistMonitoringSummary {
  total: number
  activeCount: number
  managedOnlyCount: number
  inactiveCount: number
  pendingEvaluationCount: number
  entryCandidateCount: number
  entrySubmittedCount: number
  entryFilledCount: number
  entryRejectedCount: number
  entrySkippedCount: number
  gateRejectedCount: number
  submissionRejectedCount: number
  waitingForSetupCount: number
  dataStaleCount: number
  dataUnavailableCount: number
  biasConflictCount: number
  evaluationBlockedCount: number
  monitorOnlyCount: number
  inactiveDecisionCount: number
  openPositionCount: number
  expiredPositionCount: number
  protectiveExitPendingCount: number
  stopLossBreachedCount: number
  trailingStopBreachedCount: number
  profitTargetReachedCount: number
  scaleOutReadyCount: number
  followThroughFailedCount: number
  impulseTrailArmedCount: number
  timeStopExtendedCount: number
  expiringWithin24hCount: number
  nextEvaluationAtUtc: string | null
  lastEvaluatedAtUtc: string | null
}

export interface WatchlistScopeTruth {
  scope: WatchlistScope
  state: 'READY' | 'DEGRADED' | 'MISSING' | 'STALE' | string
  ready: boolean
  reason: string
  activeUploadId: string | null
  activeUploadReceivedAtUtc: string | null
  watchlistExpiresAtUtc: string | null
  watchlistExpired: boolean
  activeSymbolCount: number
  managedOnlyCount: number
  openPositionCount: number
  dataWarningCount: number
}

export interface WatchlistMonitoringSnapshot {
  scope: WatchlistScope
  capturedAtUtc: string
  activeUploadId: string | null
  scopeTruth: WatchlistScopeTruth
  summary: WatchlistMonitoringSummary
  rows: WatchlistSymbolRecord[]
}

export interface ScopeSessionStatus {
  scope: WatchlistScope
  observedAtUtc: string
  sessionOpen: boolean
  reason: string
  nextSessionStartUtc?: string | null
  nextSessionStartEt?: string | null
  sessionCloseUtc?: string | null
  sessionCloseEt?: string | null
  sessionLabel?: string | null
}

export interface WatchlistDueScopeSnapshot {
  scope: WatchlistScope
  dueCount: number
  eligibleDueCount: number
  blockedDueCount: number
  activeDueCount: number
  managedOnlyDueCount: number
  nextEvaluationAtUtc: string | null
  activeUploadId: string | null
  session: ScopeSessionStatus
}

export interface WatchlistOrchestrationStatus {
  enabled: boolean
  pollSeconds: number
  lastStartedAtUtc: string | null
  lastFinishedAtUtc: string | null
  lastError: string | null
  consecutiveFailures: number
  lastRunSummary: Record<string, unknown>
  dueSnapshot:
    | {
        capturedAtUtc: string
        scopes: Partial<Record<WatchlistScope, WatchlistDueScopeSnapshot>>
        summary: {
          totalDueCount: number
          eligibleDueCount: number
          blockedDueCount: number
          activeDueCount: number
          managedOnlyDueCount: number
        }
      }
    | WatchlistDueScopeSnapshot
    | null
}

export interface WatchlistExitReadinessSummary {
  openPositionCount: number
  expiredPositionCount: number
  expiringWithinWindowCount: number
  protectiveExitPendingCount: number
  stopLossBreachedCount: number
  trailingStopBreachedCount: number
  profitTargetReachedCount: number
  scaleOutReadyCount: number
  followThroughFailedCount: number
  impulseTrailArmedCount: number
  timeStopExtendedCount: number
  managedOnlyOpenCount: number
}

export interface WatchlistExitReadinessSnapshot {
  scope: WatchlistScope
  capturedAtUtc: string
  activeUploadId: string | null
  expiringWithinHours: number
  summary: WatchlistExitReadinessSummary
  rows: WatchlistSymbolRecord[]
}

export interface WatchlistExitWorkerRow {
  symbol: string
  managedOnly: boolean
  monitoringStatus: string
  positionState: WatchlistPositionState
  exitTrigger: string | null
  exitReasons: string[]
  exitAlreadyInProgress: boolean
}

export interface WatchlistExitWorkerStatus {
  scope: WatchlistScope
  capturedAtUtc: string
  mode: TradingMode
  runtimeRunning: boolean
  brokerReady: boolean
  session: ScopeSessionStatus
  enabled: boolean
  pollSeconds: number
  lastStartedAtUtc: string | null
  lastFinishedAtUtc: string | null
  lastError: string | null
  consecutiveFailures: number
  lastRunSummary: Record<string, unknown>
  summary: {
    candidateExitCount: number
    expiredPositionCount: number
    protectiveExitCount: number
    profitTargetCount: number
    followThroughExitCount: number
    eligibleExpiredCount: number
    blockedExpiredCount: number
    eligibleProtectiveCount: number
    blockedProtectiveCount: number
    eligibleProfitTargetCount: number
    blockedProfitTargetCount: number
    eligibleExitCount: number
    blockedExitCount: number
    managedOnlyExpiredCount: number
    alreadyInProgressCount: number
  }
  rows: WatchlistExitWorkerRow[]
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
] as const


export interface PositionInspectTimelineEvent {
  eventType: string
  status: string
  message: string | null
  eventTime: string | null
  payload: Record<string, unknown>
}

export interface PositionInspectTimeframeItem {
  timeframe: string
  status: string
  reason: string
}

export interface PositionInspectRecord {
  assetClass: 'stock' | 'crypto' | string
  symbol: string
  displaySymbol: string
  inspectSource: string
  positionSnapshot: Record<string, unknown>
  signalSnapshot: Record<string, unknown>
  sizing: Record<string, unknown>
  timeframeAlignment: {
    mode: string
    configured: string[]
    confirmed: string[]
    items: PositionInspectTimeframeItem[]
    note?: string | null
  }
  exitPlan: Record<string, unknown>
  latestEvaluation?: Record<string, unknown> | null
  lifecycle: PositionInspectTimelineEvent[]
  rawContext: Record<string, unknown>
}


