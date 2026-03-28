import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { Brain, TrendingUp, Bitcoin, CheckCircle, XCircle } from 'lucide-react'
import { format } from 'date-fns'

export default function AIDecisions() {
  const { data: allDecisions } = useQuery({
    queryKey: ['aiDecisions'],
    queryFn: () => api.getAIDecisions(100),
    refetchInterval: 5000,
  })
  
  const stockDecisions = allDecisions?.filter((d: any) => d.market === 'STOCK') || []
  const cryptoDecisions = allDecisions?.filter((d: any) => d.market === 'CRYPTO') || []
  
  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-3xl font-bold text-white">AI Decisions</h1>
        <div className="flex gap-3">
          <div className="bg-gray-900 border border-gray-800 rounded-lg px-4 py-2">
            <div className="text-sm text-gray-400">Stock Decisions</div>
            <div className="text-xl font-bold text-white">{stockDecisions.length}</div>
          </div>
          <div className="bg-gray-900 border border-gray-800 rounded-lg px-4 py-2">
            <div className="text-sm text-gray-400">Crypto Decisions</div>
            <div className="text-xl font-bold text-white">{cryptoDecisions.length}</div>
          </div>
        </div>
      </div>
      
      {/* Stock Decisions */}
      <DecisionPanel title="Stock Decisions (Tradier)" decisions={stockDecisions} icon={<TrendingUp className="w-5 h-5" />} />
      
      {/* Crypto Decisions */}
      <DecisionPanel title="Crypto Decisions (Kraken)" decisions={cryptoDecisions} icon={<Bitcoin className="w-5 h-5" />} />
    </div>
  )
}

function DecisionPanel({ title, decisions, icon }: any) {
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg p-6">
      <h2 className="text-xl font-bold text-white mb-4 flex items-center gap-2">
        {icon}
        {title}
      </h2>
      {decisions.length === 0 ? (
        <p className="text-gray-500 text-center py-8">No AI decisions yet</p>
      ) : (
        <div className="space-y-3">
          {decisions.map((decision: any) => (
            <div key={decision.id} className="bg-gray-800 rounded-lg p-4 border border-gray-700">
              <div className="flex items-start justify-between mb-2">
                <div className="flex items-center gap-3">
                  <Brain className="w-5 h-5 text-blue-500" />
                  <div>
                    <div className="flex items-center gap-2">
                      <span className="font-semibold text-white">{decision.symbol}</span>
                      <span className={`px-2 py-1 rounded text-xs ${
                        decision.type === 'BUY' ? 'bg-green-900 text-green-300' :
                        decision.type === 'SELL' ? 'bg-red-900 text-red-300' :
                        decision.type === 'SCREENING' ? 'bg-blue-900 text-blue-300' :
                        'bg-gray-700 text-gray-300'
                      }`}>
                        {decision.type}
                      </span>
                      {decision.vix && (
                        <span className="text-xs text-gray-400">VIX: {decision.vix}</span>
                      )}
                    </div>
                    <div className="text-sm text-gray-400 mt-1">
                      {format(new Date(decision.timestamp), 'MMM dd, yyyy HH:mm:ss')}
                    </div>
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <span className="text-sm text-gray-400">Confidence: {(decision.confidence * 100).toFixed(0)}%</span>
                  {decision.executed ? (
                    <CheckCircle className="w-5 h-5 text-green-500" />
                  ) : decision.rejected ? (
                    <XCircle className="w-5 h-5 text-red-500" />
                  ) : (
                    <div className="w-5 h-5 rounded-full border-2 border-yellow-500" />
                  )}
                </div>
              </div>
              <div className="text-sm text-gray-300 mt-2">{decision.reasoning}</div>
              {decision.rejected && decision.rejectionReason && (
                <div className="mt-2 text-sm text-red-400">
                  Rejected: {decision.rejectionReason}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
