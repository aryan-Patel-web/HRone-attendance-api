import { useEffect, useState } from 'react'
import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { api } from '../api.js'
import { Notice } from '../components/Ui.jsx'
import { addDays, pct, todayIst } from '../util.js'

export default function Dashboard() {
  const [month, setMonth] = useState(todayIst().slice(0, 7))
  const [summary, setSummary] = useState([])
  const [board, setBoard] = useState([])
  const [limit, setLimit] = useState(10)
  const [boardDept, setBoardDept] = useState('')
  const [dept, setDept] = useState('')
  const [range, setRange] = useState({ from: addDays(todayIst(), -29), to: todayIst() })
  const [trend, setTrend] = useState([])
  const [error, setError] = useState('')



  
  useEffect(() => {
    setError('')
    api('/analytics/departments/summary', { params: { month } })
      .then((d) => {
        setSummary(d.items)
        if (!dept && d.items.length) setDept(d.items[0].department)
      })
      .catch((e) => setError(e.message))
  }, [month]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    api('/analytics/leaderboard/late', { params: { month, limit, department: boardDept } })
      .then((d) => setBoard(d.items))
      .catch((e) => setError(e.message))
  }, [month, limit, boardDept])

  useEffect(() => {
    if (!dept) return
    api(`/analytics/departments/${encodeURIComponent(dept)}/trend`, { params: range })
      .then((d) => setTrend(d.items))
      .catch((e) => setError(e.message))
  }, [dept, range.from, range.to]) // eslint-disable-line react-hooks/exhaustive-deps

  const totals = summary.reduce(
    (a, s) => ({ head: a.head + s.headcount, late: a.late + s.late_count, leave: a.leave + s.leave_count, duty: a.duty + s.on_duty_count }),
    { head: 0, late: 0, leave: 0, duty: 0 },
  )

  return (
    <div className="stack">
      <div className="toolbar">
        <h2>Overview</h2>
        <label className="inline">Month <input type="month" value={month} onChange={(e) => e.target.value && setMonth(e.target.value)} /></label>
      </div>
      <Notice>{error}</Notice>

      <div className="cards">
        <Stat label="Headcount" value={totals.head} />
        <Stat label="Late arrivals" value={totals.late} />
        <Stat label="Leave days" value={totals.leave} />
        <Stat label="On-duty records" value={totals.duty} />
      </div>

      <section className="panel">
        <h3>Department summary</h3>
        <div className="table-wrap">
          <table>
            <thead>
              <tr><th>Department</th><th className="num">Headcount</th><th className="num">Present days</th><th className="num">Avg hours</th>
                <th className="num">Late</th><th className="num">Late min</th><th className="num">Leave</th><th className="num">On duty</th></tr>
            </thead>
            <tbody>
              {summary.map((s) => (
                <tr key={s.department}>
                  <td>{s.department}</td><td className="num">{s.headcount}</td><td className="num">{s.present_days}</td>
                  <td className="num">{s.avg_work_hours ?? '—'}</td><td className="num">{s.late_count}</td>
                  <td className="num">{s.total_late_minutes}</td><td className="num">{s.leave_count}</td><td className="num">{s.on_duty_count}</td>
                </tr>
              ))}
              {!summary.length && <tr><td colSpan="8" className="empty">No departments with headcount for {month}.</td></tr>}
            </tbody>
          </table>
        </div>
      </section>

      <section className="panel">
        <div className="toolbar">
          <h3>Daily trend</h3>
          <div className="row">
            <select value={dept} onChange={(e) => setDept(e.target.value)} aria-label="Department">
              {summary.map((s) => <option key={s.department}>{s.department}</option>)}
            </select>
            <input type="date" value={range.from} onChange={(e) => setRange({ ...range, from: e.target.value })} />
            <input type="date" value={range.to} onChange={(e) => setRange({ ...range, to: e.target.value })} />
          </div>
        </div>
        <div className="chart">
          <ResponsiveContainer width="100%" height={280}>
            <LineChart data={trend} margin={{ top: 8, right: 16, bottom: 0, left: -10 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--line)" />
              <XAxis dataKey="date" tickFormatter={(d) => d.slice(5)} stroke="var(--muted)" fontSize={12} />
              <YAxis domain={[0, 1]} tickFormatter={(v) => `${Math.round(v * 100)}%`} stroke="var(--muted)" fontSize={12} />
              <Tooltip formatter={(v) => (v == null ? '—' : pct(v, 2))} contentStyle={{ background: 'var(--panel)', border: '1px solid var(--line)' }} />
              <Legend />
              <Line type="linear" dataKey="attendance_rate" name="Attendance rate" stroke="var(--accent)" dot={false} strokeWidth={2} connectNulls={false} />
              <Line type="linear" dataKey="moving_avg_7d" name="7-day average" stroke="var(--warm)" dot={false} strokeWidth={2} strokeDasharray="5 4" />
            </LineChart>
          </ResponsiveContainer>
        </div>
        <p className="muted small">Weekends have no rate (gaps in the line); the 7-day average only uses the days inside the selected range.</p>
      </section>

      <section className="panel">
        <div className="toolbar">
          <h3>Late-comers leaderboard</h3>
          <div className="row">
            <select value={boardDept} onChange={(e) => setBoardDept(e.target.value)} aria-label="Department filter">
              <option value="">All departments</option>
              {summary.map((s) => <option key={s.department}>{s.department}</option>)}
            </select>
            <select value={limit} onChange={(e) => setLimit(Number(e.target.value))} aria-label="Limit">
              {[3, 5, 10, 20, 50].map((n) => <option key={n} value={n}>Top {n}</option>)}
            </select>
          </div>
        </div>
        <div className="table-wrap">
          <table>
            <thead><tr><th>Rank</th><th>Employee</th><th>Department</th><th className="num">Late minutes</th><th className="num">Late days</th></tr></thead>
            <tbody>
              {board.map((r) => (
                <tr key={r.emp_code}>
                  <td><span className="rank">{r.rank}</span></td>
                  <td>{r.name} <span className="muted">{r.emp_code}</span></td><td>{r.department}</td>
                  <td className="num">{r.total_late_minutes}</td><td className="num">{r.late_count}</td>
                </tr>
              ))}
              {!board.length && <tr><td colSpan="5" className="empty">Nobody was late this month.</td></tr>}
            </tbody>
          </table>
        </div>
        <p className="muted small">Ties share a rank and the next rank is skipped (1, 2, 2, 4), so a tie at the cutoff can show more rows than the limit.</p>
      </section>
    </div>
  )
}

function Stat({ label, value }) {
  return (
    <div className="card">
      <span className="muted">{label}</span>
      <strong>{value}</strong>
    </div>
  )
}
