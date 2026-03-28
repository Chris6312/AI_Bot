import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import type { AIDecision } from '@/types'
import { Brain, TrendingUp, Bitcoin, CheckCircle, XCircle } from 'lucide-react'
import { format } from 'date-fns'

export default function AIDecisions() {
  const { data: allDecisions = [] } = useQuery<AIDecision[]>({
    queryKey: ['aiDecisions'],
    queryFn: () => api.getAIDecisions(100),
    refetchInterval: 5000,
  })

  const stockDecisions = allDecisions.filter((decision) => decision.market === 'STOCK')
  const cryptoDecisions = allDecisions.filter((decision) => decision.market === 'CRYPTO')

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-3xl font-bold text-white">AI Decisions</h1>
        <div className="flex gap-3">
          <CounterCard label="Stock Decisions" value={stockDecisions.length} />
          <CounterCard label="Crypto Decisions" value={cryptoDecisions.length} />
        </div>
      </div>

      <DecisionPanel title="Stock Decisions (Tradier)" decisions={stockDecisions} icon={<TrendingUp className="h-5 w-5" />} />
      <DecisionPanel title="Crypto Decisions (Kraken)" decisions={cryptoDecisions} icon={<Bitcoin className="h-5 w-5" />} />
    </div>
  )
}

function CounterCard({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-lg border border-gray-800 bg-gray-900 px-4 py-2">
      <div className="text-sm text-gray-400">{label}</div>
      <div className="text-xl font-bold text-white">{value}</div>
    </div>
  )
}

function DecisionPanel({ title, decisions, icon }: { title: string; decisions: AIDecision[]; icon: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-gray-800 bg-gray-900 p-6">
      <h2 className="mb-4 flex items-center gap-2 text-xl font-bold text-white">
        {icon}
        {title}
      </h2>
      {decisions.length === 0 ? (
        <p className="py-8 text-center text-gray-500">No AI decisions yet</p>
      ) : (
        <div className="space-y-3">
          {decisions.map((decision) => (
            <div key={decision.id} className="rounded-lg border border-gray-700 bg-gray-800 p-4">
              <div className="mb-2 flex items-start justify-between">
                <div className="flex items-center gap-3">
                  <Brain className="h-5 w-5 text-blue-500" />
                  <div>
                    <div className="flex items-center gap-2">
                      <span className="font-semibold text-white">{decision.symbol}</span>
                      <span className={`rounded px-2 py-1 text-xs ${decision.type === 'BUY' ? 'bg-green-900 text-green-300' : decision.type === 'SELL' ? 'bg-red-900 text-red-300' : decision.type === 'SCREENING' ? 'bg-blue-900 text-blue-300' : 'bg-gray-700 text-gray-300'}`}>
                        {decision.type}
                      </span>
                      {decision.vix !== undefined ? <span className="text-xs text-gray-400">VIX: {decision.vix}</span> : null}
                    </div>
                    <div className="mt-1 text-sm text-gray-400">{format(new Date(decision.timestamp), 'MMM dd, yyyy HH:mm:ss')}</div>
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <span className="text-sm text-gray-400">Confidence: {(decision.confidence * 100).toFixed(0)}%</span>
                  {decision.executed ? <CheckCircle className="h-5 w-5 text-green-500" /> : decision.rejected ? <XCircle className="h-5 w-5 text-red-500" /> : <div className="h-5 w-5 rounded-full border-2 border-yellow-500" />}
                </div>
              </div>
              <div className="mt-2 text-sm text-gray-300">{decision.reasoning}</div>
              {decision.rejected && decision.rejectionReason ? <div className="mt-2 text-sm text-red-400">Rejected: {decision.rejectionReason}</div> : null}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
