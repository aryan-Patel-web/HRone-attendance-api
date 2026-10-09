import { useEffect } from 'react'

export function Pager({ page, pageSize, total, onPage }) {
  const pages = Math.max(1, Math.ceil(total / pageSize))
  return (
    <div className="pager">
      <span className="muted">
        {total} record{total === 1 ? '' : 's'} · page {page} of {pages}
      </span>
      <div className="row">
        <button className="btn" disabled={page <= 1} onClick={() => onPage(page - 1)}>← Prev</button>
        <button className="btn" disabled={page >= pages} onClick={() => onPage(page + 1)}>Next →</button>
      </div>
    </div>
  )
}

export function Modal({ title, onClose, children }) {
  useEffect(() => {
    const onKey = (e) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])
  return (
    <div className="overlay" onMouseDown={onClose}>
      <div className="modal" onMouseDown={(e) => e.stopPropagation()} role="dialog" aria-label={title}>
        <div className="modal-head">
          <h3>{title}</h3>
          <button className="btn ghost" onClick={onClose} aria-label="Close">✕</button>
        </div>
        {children}
      </div>
    </div>
  )
}

export function Notice({ kind = 'error', children }) {
  if (!children) return null
  return <div className={`notice ${kind}`}>{children}</div>
}

export function Field({ label, children, hint }) {
  return (
    <label className="field">
      <span>{label}</span>
      {children}
      {hint && <small className="muted">{hint}</small>}
    </label>
  )
}

export function StatusBadge({ status }) {
  return <span className={`badge ${status}`}>{status.replace('_', ' ')}</span>
}
