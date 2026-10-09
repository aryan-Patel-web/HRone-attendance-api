import { useState } from 'react'
import { api } from '../api.js'
import { Notice } from '../components/Ui.jsx'
import { todayIst, addDays } from '../util.js'

const TARGETS = {
  attendance_list: ['emp_code', 'date_from', 'date_to', 'status', 'page', 'page_size'],
  employee_monthly: ['emp_code', 'month'],
  department_summary: ['month', 'department'],
  late_leaderboard: ['month', 'limit', 'department'],
  department_trend: ['department', 'from', 'to'],
}

// Walk the raw explain document and collect plan stage names + execution counters.
function summarize(explain) {
  const stages = new Set()
  const walk = (o) => {
    if (Array.isArray(o)) o.forEach(walk)
    else if (o && typeof o === 'object') {
      if (typeof o.stage === 'string' && o.stage === o.stage.toUpperCase()) stages.add(o.stage) // plan stages only (IXSCAN, FETCH...)
      if (typeof o.indexName === 'string') stages.add(`index:${o.indexName}`)
      Object.values(o).forEach(walk)
    }
  }
  walk(explain)
  const stats = explain.executionStats || explain.stages?.[0]?.$cursor?.executionStats || {}
  return { stages: [...stages], stats }
}

export default function Explain() {
  const [target, setTarget] = useState('late_leaderboard')
  const [vals, setVals] = useState({ month: todayIst().slice(0, 7), limit: 10, from: addDays(todayIst(), -29), to: todayIst() })
  const [out, setOut] = useState(null)
  const [error, setError] = useState('')
  const [raw, setRaw] = useState(false)

  async function run() {
    setError(''); setOut(null)
    try {
      const params = Object.fromEntries(TARGETS[target].map((k) => [k, vals[k]]))
      setOut(await api(`/admin/explain/${target}`, { params }))
    } catch (e) { setError(e.message) }
  }

  const s = out && summarize(out.explain)
  const hasIx = s?.stages.includes('IXSCAN')
  const hasColl = s?.stages.includes('COLLSCAN')

  return (
    <section className="panel">
      <h3>Query plans</h3>
      <p className="muted small">Runs MongoDB <code>explain("executionStats")</code> on the exact query or pipeline each endpoint builds.</p>
      <div className="row wrap end">
        <label className="field"><span>Endpoint</span>
          <select value={target} onChange={(e) => { setTarget(e.target.value); setOut(null) }}>
            {Object.keys(TARGETS).map((t) => <option key={t}>{t}</option>)}
          </select>
        </label>
        {TARGETS[target].map((k) => (
          <label className="field" key={k}><span>{k}</span>
            <input value={vals[k] ?? ''} onChange={(e) => setVals({ ...vals, [k]: e.target.value })} />
          </label>
        ))}
        <button className="btn primary" onClick={run}>Explain</button>
      </div>
      <Notice>{error}</Notice>
      {out && (
        <div className="stack">
          <div className="cards">
            <div className="card"><span className="muted">Collection</span><strong className="small-v">{out.collection}</strong></div>
            <div className={`card ${hasIx ? 'good' : 'bad'}`}><span className="muted">IXSCAN</span><strong>{hasIx ? 'yes' : 'no'}</strong></div>
            <div className={`card ${hasColl ? 'bad' : 'good'}`}><span className="muted">COLLSCAN</span><strong>{hasColl ? 'yes' : 'none'}</strong></div>
            <div className="card"><span className="muted">Docs examined</span><strong>{s.stats.totalDocsExamined ?? '—'}</strong></div>
            <div className="card"><span className="muted">Keys examined</span><strong>{s.stats.totalKeysExamined ?? '—'}</strong></div>
            <div className="card"><span className="muted">Time (ms)</span><strong>{s.stats.executionTimeMillis ?? '—'}</strong></div>
          </div>
          <div className="row wrap">{s.stages.map((x) => <span className="chip" key={x}>{x}</span>)}</div>
          <button className="btn small self-start" onClick={() => setRaw(!raw)}>{raw ? 'Hide' : 'Show'} raw explain JSON</button>
          {raw && <pre className="raw">{JSON.stringify(out.explain, null, 2)}</pre>}
        </div>
      )}
    </section>
  )
}
