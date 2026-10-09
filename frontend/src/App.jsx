import { useState } from 'react'
import Dashboard from './pages/Dashboard.jsx'
import Attendance from './pages/Attendance.jsx'
import Employees from './pages/Employees.jsx'
import Explain from './pages/Explain.jsx'

const TABS = [
  { id: 'dashboard', label: 'Dashboard', el: <Dashboard /> },
  { id: 'attendance', label: 'Attendance', el: <Attendance /> },
  { id: 'employees', label: 'Employees', el: <Employees /> },
  { id: 'explain', label: 'Query plans', el: <Explain /> },
]

export default function App() {
  const [tab, setTab] = useState('dashboard')
  return (
    <div className="shell">
      <header className="topbar">
        <div className="brand">
          <span className="logo">◷</span>
          <div>
            <strong>Attendance Console</strong>
            <small>FastAPI · MongoDB</small>
          </div>
        </div>
        <nav>
          {TABS.map((t) => (
            <button key={t.id} className={tab === t.id ? 'tab active' : 'tab'} onClick={() => setTab(t.id)}>
              {t.label}
            </button>
          ))}
        </nav>
      </header>
      <main>{TABS.find((t) => t.id === tab).el}</main>
    </div>
  )
}
