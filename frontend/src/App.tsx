import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'

import Navigation from './components/Navigation'
import AuditTrail from './pages/AuditTrail'
import Dashboard from './pages/Dashboard'
import Monitoring from './pages/Monitoring'
import Positions from './pages/Positions'
import TradeHistory from './pages/TradeHistory'
import Settings from './pages/Settings'
import Watchlists from './pages/Watchlists'

function App() {
  return (
    <BrowserRouter>
      <div className="min-h-screen bg-slate-950 text-slate-100">
        <div className="mx-auto flex min-h-screen max-w-[1700px] flex-col xl:flex-row">
          <Navigation />
          <main className="min-w-0 flex-1 px-4 py-5 sm:px-6 lg:px-8 xl:py-8">
            <Routes>
              <Route path="/" element={<Dashboard />} />
              <Route path="/watchlists" element={<Watchlists />} />
              <Route path="/monitoring" element={<Monitoring />} />
              <Route path="/positions" element={<Positions />} />
              <Route path="/trade-history" element={<TradeHistory />} />
              <Route path="/audit" element={<AuditTrail />} />
              <Route path="/runtime" element={<Settings />} />

              <Route path="/positions/stocks" element={<Navigate to="/positions" replace />} />
              <Route path="/positions/crypto" element={<Navigate to="/positions" replace />} />
              <Route path="/positions/paper-ledger" element={<Navigate to="/positions" replace />} />
              <Route path="/audit/ai-decisions" element={<Navigate to="/audit" replace />} />
              <Route path="/stocks" element={<Navigate to="/positions" replace />} />
              <Route path="/crypto" element={<Navigate to="/positions" replace />} />
              <Route path="/paper-ledger" element={<Navigate to="/positions" replace />} />
              <Route path="/ai-decisions" element={<Navigate to="/audit" replace />} />
              <Route path="/settings" element={<Navigate to="/runtime" replace />} />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Routes>
          </main>
        </div>
      </div>
    </BrowserRouter>
  )
}

export default App
