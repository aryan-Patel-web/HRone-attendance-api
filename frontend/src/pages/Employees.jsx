import { useCallback, useEffect, useState } from 'react'
import { api } from '../api.js'
import { Field, Modal, Notice, Pager } from '../components/Ui.jsx'
import { fmtDateTime, todayIst } from '../util.js'

const PAGE_SIZE = 15
const EMPTY = { emp_code: '', name: '', email: '', department: '', shift_start: '09:30', shift_end: '18:30', joined_on: todayIst() }

export default function Employees() {
  const [department, setDepartment] = useState('')
  const [page, setPage] = useState(1)
  const [data, setData] = useState({ items: [], total: 0 })
  const [error, setError] = useState('')
  const [creating, setCreating] = useState(false)

  const load = useCallback(() => {
    api('/employees', { params: { department, page, page_size: PAGE_SIZE } })
      .then((d) => { setData(d); setError('') })
      .catch((e) => setError(e.message))
  }, [department, page])
  useEffect(load, [load])

  return (
    <section className="panel">
      <div className="toolbar">
        <h3>Employees</h3>
        <div className="row">
          <input placeholder="Department (exact)" value={department} onChange={(e) => { setPage(1); setDepartment(e.target.value) }} />
          <button className="btn primary" onClick={() => setCreating(true)}>+ New employee</button>
        </div>
      </div>
      <Notice>{error}</Notice>
      <div className="table-wrap">
        <table>
          <thead><tr><th>Code</th><th>Name</th><th>Email</th><th>Department</th><th>Shift (IST)</th><th>Joined</th><th>Created</th></tr></thead>
          <tbody>
            {data.items.map((e) => (
              <tr key={e.emp_code}>
                <td><code>{e.emp_code}</code></td><td>{e.name}</td><td>{e.email}</td><td>{e.department}</td>
                <td>{e.shift_start}–{e.shift_end}{e.shift_end <= e.shift_start && <span className="chip warm">overnight</span>}</td>
                <td>{e.joined_on}</td><td>{fmtDateTime(e.created_at)}</td>
              </tr>
            ))}
            {!data.items.length && <tr><td colSpan="7" className="empty">No employees found.</td></tr>}
          </tbody>
        </table>
      </div>
      <Pager page={page} pageSize={PAGE_SIZE} total={data.total} onPage={setPage} />
      {creating && <CreateModal onClose={() => setCreating(false)} onSaved={() => { setCreating(false); load() }} />}
    </section>
  )
}

function CreateModal({ onClose, onSaved }) {
  const [form, setForm] = useState(EMPTY)
  const [error, setError] = useState('')
  const bind = (k) => ({ value: form[k], onChange: (e) => setForm({ ...form, [k]: e.target.value }) })

  async function save() {
    setError('')
    try {
      await api('/employees', { method: 'POST', body: form })
      onSaved()
    } catch (e) {
      setError(e.status === 409 ? 'That employee code already exists.' : e.message)
    }
  }

  return (
    <Modal title="New employee" onClose={onClose}>
      <div className="form-grid">
        <Field label="Employee code" hint="EMP + 4–6 digits"><input placeholder="EMP0123" {...bind('emp_code')} /></Field>
        <Field label="Name"><input {...bind('name')} /></Field>
        <Field label="Email"><input type="email" {...bind('email')} /></Field>
        <Field label="Department"><input {...bind('department')} /></Field>
        <Field label="Shift start"><input type="time" {...bind('shift_start')} /></Field>
        <Field label="Shift end" hint="≤ start means overnight"><input type="time" {...bind('shift_end')} /></Field>
        <Field label="Joined on"><input type="date" {...bind('joined_on')} /></Field>
      </div>
      <Notice>{error}</Notice>
      <div className="row end">
        <button className="btn" onClick={onClose}>Cancel</button>
        <button className="btn primary" onClick={save}>Create</button>
      </div>
    </Modal>
  )
}
