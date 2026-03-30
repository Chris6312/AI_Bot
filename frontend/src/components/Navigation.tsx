import { NavLink } from 'react-router-dom'
import {
  Activity,
  BrainCircuit,
  ClipboardList,
  LayoutDashboard,
  Settings,
} from 'lucide-react'

type NavItem = {
  to: string
  label: string
  icon: typeof LayoutDashboard
  section: 'operator' | 'audit'
}

const navItems: NavItem[] = [
  { to: '/', label: 'Dashboard', icon: LayoutDashboard, section: 'operator' },
  { to: '/watchlists', label: 'Watchlists', icon: ClipboardList, section: 'operator' },
  { to: '/monitoring', label: 'Monitoring', icon: Activity, section: 'operator' },
  { to: '/ai-decisions', label: 'AI Decisions', icon: BrainCircuit, section: 'audit' },
  { to: '/settings', label: 'Runtime & Risk', icon: Settings, section: 'audit' },
]

const sectionLabels: Record<'operator' | 'audit', string> = {
  operator: 'Operator',
  audit: 'Audit & Runtime',
}

export default function Navigation() {
  const grouped = {
    operator: navItems.filter((item) => item.section === 'operator'),
    audit: navItems.filter((item) => item.section === 'audit'),
  }

  return (
    <aside className="flex h-full min-h-0 flex-col border-r border-slate-800 bg-[#020b2a]">
      <div className="border-b border-slate-800 px-5 py-5">
        <div className="text-lg font-semibold text-white">AI Bot vNext</div>
        <div className="mt-1 text-xs uppercase tracking-[0.22em] text-cyan-300">Menu</div>
      </div>

      <nav className="min-h-0 flex-1 overflow-y-auto px-3 py-4">
        <div className="space-y-6">
          {(Object.keys(grouped) as Array<keyof typeof grouped>).map((sectionKey) => (
            <div key={sectionKey}>
              <div className="mb-3 px-2 text-[11px] font-semibold uppercase tracking-[0.22em] text-slate-500">
                {sectionLabels[sectionKey]}
              </div>

              <div className="space-y-2">
                {grouped[sectionKey].map((item) => {
                  const Icon = item.icon

                  return (
                    <NavLink
                      key={item.to}
                      to={item.to}
                      end={item.to === '/'}
                      className={({ isActive }) =>
                        [
                          'group flex items-center gap-3 rounded-2xl border px-4 py-3 transition',
                          isActive
                            ? 'border-cyan-700 bg-cyan-500/10 text-cyan-100 shadow-[0_0_0_1px_rgba(8,145,178,0.15)]'
                            : 'border-slate-800 bg-slate-900/50 text-slate-200 hover:border-slate-700 hover:bg-slate-900',
                        ].join(' ')
                      }
                    >
                      {({ isActive }) => (
                        <>
                          <div
                            className={[
                              'flex h-10 w-10 items-center justify-center rounded-xl border transition',
                              isActive
                                ? 'border-cyan-700 bg-cyan-500/10 text-cyan-200'
                                : 'border-slate-700 bg-slate-950/70 text-slate-300 group-hover:border-slate-600 group-hover:text-white',
                            ].join(' ')}
                          >
                            <Icon className="h-5 w-5" />
                          </div>

                          <div className="min-w-0 flex-1">
                            <div className="truncate text-base font-medium">{item.label}</div>
                          </div>
                        </>
                      )}
                    </NavLink>
                  )
                })}
              </div>
            </div>
          ))}
        </div>
      </nav>
    </aside>
  )
}