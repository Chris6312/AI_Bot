import { NavLink } from 'react-router-dom'
import { TrendingUp, Bitcoin, Brain, FileText, Settings as SettingsIcon } from 'lucide-react'

const links = [
  { to: '/', label: 'Dashboard', icon: TrendingUp },
  { to: '/stocks', label: 'Stocks', icon: TrendingUp },
  { to: '/crypto', label: 'Crypto', icon: Bitcoin },
  { to: '/ai-decisions', label: 'AI Decisions', icon: Brain },
  { to: '/paper-ledger', label: 'Paper Ledger', icon: FileText },
  { to: '/settings', label: 'Settings', icon: SettingsIcon },
]

export default function Navigation() {
  return (
    <nav className="bg-gray-900 border-b border-gray-800">
      <div className="container mx-auto px-4 py-3">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
          <div className="flex items-center gap-2">
            <Brain className="w-8 h-8 text-blue-500" />
            <div>
              <h1 className="text-xl font-bold text-white">AI Trading Bot</h1>
              <p className="text-xs text-gray-400">Tradier + Kraken + Discord</p>
            </div>
            <span className="ml-2 rounded bg-blue-900 px-2 py-1 text-xs text-blue-200">
              Hybrid System
            </span>
          </div>

          <div className="flex flex-wrap gap-2">
            {links.map(({ to, label, icon: Icon }) => (
              <NavLink
                key={to}
                to={to}
                end={to === '/'}
                className={({ isActive }) =>
                  `flex items-center gap-2 rounded-md px-3 py-2 text-sm transition-colors ${
                    isActive
                      ? 'bg-blue-600 text-white'
                      : 'text-gray-300 hover:bg-gray-800 hover:text-white'
                  }`
                }
              >
                <Icon className="h-4 w-4" />
                <span>{label}</span>
              </NavLink>
            ))}
          </div>
        </div>
      </div>
    </nav>
  )
}
