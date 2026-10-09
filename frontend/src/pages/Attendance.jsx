import { Fragment, useCallback, useEffect, useState } from 'react'
import { api } from '../api.js'
import { Field, Modal, Notice, Pager, StatusBadge } from '../components/Ui.jsx'
import { STATUSES, fmtDateTime, fmtTime, istInputToMs, msToIstInput } from '../util.js'

const PAGE_SIZE = 15

export default function Attendance() {
  const [filters, setFilters] = useState({ emp_code: '', date_from: '', date_to: '', status: '' })
  const [page, setPage] = useState(1)
  const [data, setData] = useState({ items: [], total: 0 })
  const [error, setError] = useState('')
  const [open, setOpen] = useState(null) // row whose history is expanded
  const [editing, setEditing] = useState(null)

  const load = useCallback(() => {
    setError('')
    api('/attendance', { params: { ...filters, page, page_size: PAGE_SIZE } })
      .then(setData)
      .catch((e) => setError(e.message))
  }, [filters, page])
  useEffect(load, [load])

  const set = (k) => (e) => { setPage(1); setFilters({ ...filters, [k]: e.target.value }) }

  return (
    <div className="stack">
      <PunchPanel onDone={load} />
      <section className="panel">
        <div className="toolbar">
          <h3>Attendance records</h3>
          <div className="row wrap">
            <input placeholder="emp_code" value={filters.emp_code} onChange={set('emp_code')} />
            <input type="date" value={filters.date_from} onChange={set('date_from')} aria-label="From date" />
            <input type="date" value={filters.date_to} onChange={set('date_to')} aria-label="To date" />
            <select value={filters.status} onChange={set('status')}>
              <option value="">Any status</option>
              {STATUSES.map((s) => <option key={s}>{s}</option>)}
            </select>
          </div>
        </div>
        <Notice>{error}</Notice>
        <div className="table-wrap">
          <table>
            <thead><tr><th>Date</th><th>Employee</th><th>Status</th><th>In</th><th>Out</th><th className="num">Hours</th>
              <th className="num">Late</th><th className="num">OT</th><th>Flags</th><th /></tr></thead>
            <tbody>
              {data.items.map((r) => {
                const key = `${r.emp_code}|${r.date}`
                return (
                  <Fragment key={key}>
                    <tr>
                      <td>{r.date}</td><td>{r.emp_code}</td><td><StatusBadge status={r.status} /></td>
                      <td>{fmtTime(r.punch_in)}</td><td>{fmtTime(r.punch_out)}</td>
                      <td className="num">{r.work_hours ?? '—'}</td><td className="num">{r.late_minutes || '—'}</td>
                      <td className="num">{r.overtime_minutes || '—'}</td>
                      <td>{r.half_day && <span className="chip">half day</span>}{r.punch_in && !r.punch_out && <span className="chip warm">open</span>}</td>
                      <td className="actions">
                        <button className="btn small" onClick={() => setEditing(r)}>Correct</button>
                        {r.history.length > 0 && (
                          <button className="btn small ghost" onClick={() => setOpen(open === key ? null : key)}>
                            History ({r.history.length})
                          </button>
                        )}
                      </td>
                    </tr>
                    {open === key && (
                      <tr className="subrow"><td colSpan="10"><History items={r.history} /></td></tr>
                    )}
                  </Fragment>
                )
              })}
              {!data.items.length && <tr><td colSpan="10" className="empty">No records match.</td></tr>}
            </tbody>
          </table>
        </div>
        <Pager page={page} pageSize={PAGE_SIZE} total={data.total} onPage={setPage} />
      </section>
      {editing && <CorrectModal record={editing} onClose={() => setEditing(null)} onSaved={() => { setEditing(null); load() }} />}
    </div>
  )
}

function History({ items }) {
  return (
    <ul className="history">
      {items.map((h, i) => (
        <li key={i}>
          <strong>{h.by}</strong> <span className="muted">· {fmtDateTime(h.at)} · “{h.reason}”</span>
          <div className="changes">
            {Object.entries(h.changes).map(([field, c]) => (
              <span key={field} className="chip">
                {field}: {fmtVal(field, c.from)} → {fmtVal(field, c.to)}
              </span>
            ))}
          </div>
        </li>
      ))}
    </ul>
  )
}

const fmtVal = (field, v) => (field === 'punch_in' || field === 'punch_out' ? fmtTime(v) : v === null ? '—' : String(v))

function PunchPanel({ onDone }) {
  const [form, setForm] = useState({ emp_code: '', status: 'PRESENT', at: '' })
  const [msg, setMsg] = useState(null)

  async function submit(kind) {
    setMsg(null)
    try {
      const body = { emp_code: form.emp_code.trim() }
      if (form.at) body.punched_at = istInputToMs(form.at)
      if (kind === 'in') body.status = form.status
      const r = await api(`/attendance/punch-${kind}`, { method: 'POST', body })
      const text = kind === 'in'
        ? `Punched in ${r.emp_code} for ${r.date}${r.late_minutes ? ` — ${r.late_minutes} min late` : ''}`
        : `Punched out ${r.emp_code}: ${r.work_hours} h${r.overtime_minutes ? `, ${r.overtime_minutes} min overtime` : ''}${r.half_day ? ' (half day)' : ''}`
      setMsg({ kind: 'ok', text })
      onDone()
    } catch (e) {
      setMsg({ kind: 'error', text: e.message })
    }
  }

  return (
    <section className="panel">
      <h3>Punch clock</h3>
      <div className="row wrap end">
        <Field label="Employee code"><input value={form.emp_code} placeholder="EMP0001" onChange={(e) => setForm({ ...form, emp_code: e.target.value })} /></Field>
        <Field label="Status (punch-in)">
          <select value={form.status} onChange={(e) => setForm({ ...form, status: e.target.value })}>
            {['PRESENT', 'WFH', 'ON_DUTY'].map((s) => <option key={s}>{s}</option>)}
          </select>
        </Field>
        <Field label="Time (IST)" hint="empty = now"><input type="datetime-local" value={form.at} onChange={(e) => setForm({ ...form, at: e.target.value })} /></Field>
        <button className="btn primary" disabled={!form.emp_code.trim()} onClick={() => submit('in')}>Punch in</button>
        <button className="btn" disabled={!form.emp_code.trim()} onClick={() => submit('out')}>Punch out</button>
      </div>
      {msg && <Notice kind={msg.kind}>{msg.text}</Notice>}
    </section>
  )
}

function CorrectModal({ record, onClose, onSaved }) {
  const [form, setForm] = useState({
    status: record.status,
    punch_in: msToIstInput(record.punch_in),
    punch_out: msToIstInput(record.punch_out),
    reason: '',
    regularized_by: '',
  })
  const [error, setError] = useState('')
  const clears = form.status === 'ABSENT' || form.status === 'LEAVE'

  async function save() {
    setError('')
    // send only what the user actually changed; the backend recomputes all derived fields itself
    const body = { reason: form.reason, regularized_by: form.regularized_by }
    if (form.status !== record.status) body.status = form.status
    if (!clears) {
      if (form.punch_in && istInputToMs(form.punch_in) !== record.punch_in) body.punch_in = istInputToMs(form.punch_in)
      if (form.punch_out && istInputToMs(form.punch_out) !== record.punch_out) body.punch_out = istInputToMs(form.punch_out)
    }
    try {
      await api(`/attendance/${encodeURIComponent(record.emp_code)}/${record.date}`, { method: 'PATCH', body })
      onSaved()
    } catch (e) {
      setError(e.message)
    }
  }

  return (
    <Modal title={`Correct ${record.emp_code} · ${record.date}`} onClose={onClose}>
      <div className="form-grid">
        <Field label="Status">
          <select value={form.status} onChange={(e) => setForm({ ...form, status: e.target.value })}>
            {STATUSES.map((s) => <option key={s}>{s}</option>)}
          </select>
        </Field>
        <Field label="Punch in (IST)"><input type="datetime-local" disabled={clears} value={clears ? '' : form.punch_in} onChange={(e) => setForm({ ...form, punch_in: e.target.value })} /></Field>
        <Field label="Punch out (IST)"><input type="datetime-local" disabled={clears} value={clears ? '' : form.punch_out} onChange={(e) => setForm({ ...form, punch_out: e.target.value })} /></Field>
        <Field label="Corrected by"><input value={form.regularized_by} maxLength={50} onChange={(e) => setForm({ ...form, regularized_by: e.target.value })} /></Field>
        <Field label="Reason (5–200 chars)"><textarea rows={2} value={form.reason} maxLength={200} onChange={(e) => setForm({ ...form, reason: e.target.value })} /></Field>
      </div>
      {clears && <p className="muted small">ABSENT and LEAVE clear both punch times.</p>}
      <Notice>{error}</Notice>
      <div className="row end">
        <button className="btn" onClick={onClose}>Cancel</button>
        <button className="btn primary" disabled={form.reason.trim().length < 5 || !form.regularized_by.trim()} onClick={save}>Save correction</button>
      </div>
    </Modal>
  )
}
