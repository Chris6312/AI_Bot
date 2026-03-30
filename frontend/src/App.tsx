import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'

import Navigation from './components/Navigation'
import Dashboard from './pages/Dashboard'
import Watchlists from './pages/Watchlists'
import Monitoring from './pages/Monitoring'
import StockTrading from './pages/StockTrading'
import CryptoTrading from './pages/CryptoTrading'
import PaperLedger from './pages/PaperLedger'
import AIDecisions from './pages/AIDecisions'
import Settings from './pages/Settings'

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
              <Route path="/positions/stocks" element={<StockTrading />} />
              <Route path="/positions/crypto" element={<CryptoTrading />} />
              <Route path="/positions/paper-ledger" element={<PaperLedger />} />
              <Route path="/audit/ai-decisions" element={<AIDecisions />} />
              <Route path="/runtime" element={<Settings />} />

              <Route path="/stocks" element={<Navigate to="/positions/stocks" replace />} />
              <Route path="/crypto" element={<Navigate to="/positions/crypto" replace />} />
              <Route path="/paper-ledger" element={<Navigate to="/positions/paper-ledger" replace />} />
              <Route path="/ai-decisions" element={<Navigate to="/audit/ai-decisions" replace />} />
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
