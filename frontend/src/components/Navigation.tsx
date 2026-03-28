import { Link, useLocation } from 'react-router-dom'
import { TrendingUp, Bitcoin, Brain, FileText, Settings as SettingsIcon } from 'lucide-react'

export default function Navigation() {
  const location = useLocation()
  
  const links = [
    { to: '/', label: 'Dashboard', icon: TrendingUp },
    { to: '/stocks', label: 'Stocks', icon: TrendingUp },
    { to: '/crypto', label: 'Crypto', icon: Bitcoin },
    { to: '/ai-decisions', label: 'AI Decisions', icon: Brain },
    { to: '/paper-ledger', label: 'Paper Ledger', icon: FileText },
    { to: '/settings', label: 'Settings', icon: SettingsIcon },
  ]
  
  return (
    <nav className="bg-gray-900 border-b border-gray-800">
      <div className="container mx-auto px-4">
        <div className="flex items-center justify-between h-16">
          <div className="flex items-center gap-2">
            <Brain className="w-8 h-8 text-blue-500" />
            <h1 className="text-xl font-bold text-white">AI Trading Bot</h1>
            <span className="ml-4 px-2 py-1 text-xs bg-blue-900 text-blue-200 rounded">
              Stock + Crypto
            </span>
          </div>
          
          <div className="flex gap-6">
            {links.map(({ to, label, icon: Icon }) => (
              <Link
                key={to}
                to={to}
                className={`flex items-center gap-2 px-3 py-2 rounded-md transition-colors ${
                  location.pathname === to
                    ? 'bg-blue-600 text-white'
                    : 'text-gray-300 hover:bg-gray-800 hover:text-white'
                }`}
              >
                <Icon className="w-4 h-4" />
                <span>{label}</span>
              </Link>
            ))}
          </div>
        </div>
      </div>
    </nav>
  )
}
