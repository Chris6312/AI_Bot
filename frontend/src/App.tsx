import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import Navigation from './components/Navigation'
import Dashboard from './pages/Dashboard'
import StockTrading from './pages/StockTrading'
import CryptoTrading from './pages/CryptoTrading'
import AIDecisions from './pages/AIDecisions'
import PaperLedger from './pages/PaperLedger'
import Settings from './pages/Settings'

function App() {
  return (
    <BrowserRouter>
      <div className="min-h-screen bg-gray-950">
        <Navigation />
        <main className="container mx-auto px-4 py-6">
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/stocks" element={<StockTrading />} />
            <Route path="/crypto" element={<CryptoTrading />} />
            <Route path="/ai-decisions" element={<AIDecisions />} />
            <Route path="/paper-ledger" element={<PaperLedger />} />
            <Route path="/settings" element={<Settings />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </main>
      </div>
    </BrowserRouter>
  )
}

export default App
