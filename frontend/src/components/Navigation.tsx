import type { ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { NavLink } from 'react-router-dom'
import {
  Activity,
  Bitcoin,
  Bot,
  Brain,
  FileText,
  LayoutDashboard,
  Radar,
  Settings as SettingsIcon,
  Shield,
  TrendingUp,
  Wallet,
} from 'lucide-react'

import { api } from '@/lib/api'
import type { BotStatus, MarketStatus } from '@/types'

type LinkItem = {
  to: string
  label: string
  icon: typeof LayoutDashboard
  end?: boolean
}

type LinkSection = {
  title: string
  links: LinkItem[]
}

const sections: LinkSection[] = [
  {
    title: 'Operator',
    links: [
      { to: '/', label: 'Dashboard', icon: LayoutDashboard, end: true },
      { to: '/watchlists', label: 'Watchlists', icon: FileText },
      { to: '/monitoring', label: 'Monitoring', icon: Radar },
    ],
  },
  {
    title: 'Positions',
    links: [
      { to: '/positions/stocks', label: 'Stocks', icon: TrendingUp },
      { to: '/positions/crypto', label: 'Crypto', icon: Bitcoin },
      { to: '/positions/paper-ledger', label: 'Paper Ledger', icon: Wallet },
    ],
  },
  {
    title: 'Audit & Runtime',
    links: [
      { to: '/audit/ai-decisions', label: 'AI Decisions', icon: Brain },
      { to: '/runtime', label: 'Runtime & Risk', icon: SettingsIcon },
    ],
  },
]

function pillTone(active: boolean) {
  return active
    ? 'border-emerald-800/80 bg-emerald-500/10 text-emerald-300'
    : 'border-slate-800 bg-slate-900/80 text-slate-400'
}

export default function Navigation() {
  const { data: botStatus } = useQuery<BotStatus>({
    queryKey: ['botStatus'],
    queryFn: api.getBotStatus,
    refetchInterval: 5000,
  })

  const { data: marketStatus } = useQuery<MarketStatus>({
    queryKey: ['marketStatus'],
    queryFn: api.getMarketStatus,
    refetchInterval: 60000,
  })

  return (
    <aside className="border-b border-slate-800/80 bg-slate-950/95 backdrop-blur xl:sticky xl:top-0 xl:h-screen xl:w-80 xl:border-b-0 xl:border-r">
      <div className="flex h-full flex-col gap-6 px-4 py-5 sm:px-6 xl:px-5 xl:py-6">
        <div className="rounded-3xl border border-slate-800 bg-slate-900/80 p-5 shadow-2xl shadow-slate-950/40">
          <div className="flex items-start gap-3">
            <div className="rounded-2xl border border-cyan-700/70 bg-cyan-500/10 p-3 text-cyan-300">
              <Bot className="h-6 w-6" />
            </div>
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-2">
                <h1 className="text-lg font-semibold text-white">AI Bot vNext</h1>
                <span className="rounded-full border border-cyan-700/70 bg-cyan-500/10 px-2.5 py-1 text-[11px] font-medium uppercase tracking-wide text-cyan-300">
                  Operator Console
                </span>
              </div>
              <p className="mt-2 text-sm leading-6 text-slate-400">
                Daily watchlists in, deterministic execution out. No narrative fields get to touch the launch buttons.
              </p>
            </div>
          </div>

          <div className="mt-4 grid grid-cols-1 gap-2 sm:grid-cols-3 xl:grid-cols-1">
            <StatusPill
              icon={<Activity className="h-3.5 w-3.5" />}
              label={`Bot ${botStatus?.running ? 'Running' : 'Paused'}`}
              active={Boolean(botStatus?.running)}
            />
            <StatusPill
              icon={<Shield className="h-3.5 w-3.5" />}
              label={`Gate ${botStatus?.executionGate.state ?? 'Unknown'}`}
              active={Boolean(botStatus?.executionGate.allowed)}
            />
            <StatusPill
              icon={<TrendingUp className="h-3.5 w-3.5" />}
              label={`Stocks ${marketStatus?.stock.isOpen ? 'Open' : 'Closed'}`}
              active={Boolean(marketStatus?.stock.isOpen)}
            />
          </div>
        </div>

        <nav className="grid gap-5 xl:overflow-y-auto xl:pr-1">
          {sections.map((section) => (
            <div key={section.title}>
              <div className="mb-2 px-3 text-[11px] font-semibold uppercase tracking-[0.22em] text-slate-500">
                {section.title}
              </div>
              <div className="space-y-1.5">
                {section.links.map(({ to, label, icon: Icon, end }) => (
                  <NavLink
                    key={to}
                    to={to}
                    end={end}
                    className={({ isActive }) =>
                      [
                        'group flex items-center gap-3 rounded-2xl border px-3.5 py-3 text-sm transition-all',
                        isActive
                          ? 'border-cyan-700/80 bg-cyan-500/10 text-cyan-100 shadow-lg shadow-cyan-950/30'
                          : 'border-slate-800 bg-slate-900/70 text-slate-300 hover:border-slate-700 hover:bg-slate-900 hover:text-white',
                      ].join(' ')
                    }
                  >
                    <span className="rounded-xl border border-current/20 bg-black/10 p-2">
                      <Icon className="h-4 w-4" />
                    </span>
                    <span className="font-medium">{label}</span>
                  </NavLink>
                ))}
              </div>
            </div>
          ))}
        </nav>

        <div className="hidden xl:block">
          <div className="rounded-3xl border border-slate-800 bg-slate-900/70 p-4 text-sm text-slate-400">
            <div className="font-semibold text-slate-200">Runtime heartbeat</div>
            <div className="mt-2 leading-6">
              Stock mode: <span className="text-slate-200">{botStatus?.stockMode ?? 'PAPER'}</span>
              <br />
              Crypto mode: <span className="text-slate-200">{botStatus?.cryptoMode ?? 'PAPER'}</span>
            </div>
          </div>
        </div>
      </div>
    </aside>
  )
}

function StatusPill({ active, icon, label }: { active: boolean; icon: ReactNode; label: string }) {
  return (
    <div className={`flex items-center gap-2 rounded-2xl border px-3 py-2 text-xs font-medium ${pillTone(active)}`}>
      {icon}
      <span>{label}</span>
    </div>
  )
}
