const TZ = 'Asia/Kolkata'

// epoch milliseconds -> "06 Jul, 09:28" (IST)
export function fmtTime(ms) {
  if (ms == null) return '—'
  return new Intl.DateTimeFormat('en-IN', {
    timeZone: TZ, day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(new Date(ms))
}

export function fmtDateTime(ms) {
  if (ms == null) return '—'
  return new Intl.DateTimeFormat('en-IN', {
    timeZone: TZ, day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(new Date(ms))
}

// <input type="datetime-local"> value ("2026-07-06T09:30"), interpreted as IST -> epoch milliseconds
export function istInputToMs(value) {
  if (!value) return undefined
  return Date.parse(`${value}:00+05:30`)
}

// epoch milliseconds -> value for <input type="datetime-local"> in IST
export function msToIstInput(ms) {
  if (ms == null) return ''
  const p = new Intl.DateTimeFormat('sv-SE', {
    timeZone: TZ, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(new Date(ms))
  return p.replace(' ', 'T')
}

export const pct = (x, digits = 1) => (x == null ? '—' : `${(x * 100).toFixed(digits)}%`)

export function todayIst() {
  return new Intl.DateTimeFormat('sv-SE', { timeZone: TZ }).format(new Date())
}

export function addDays(iso, n) {
  const d = new Date(`${iso}T00:00:00Z`)
  d.setUTCDate(d.getUTCDate() + n)
  return d.toISOString().slice(0, 10)
}

export const STATUSES = ['PRESENT', 'WFH', 'ON_DUTY', 'ABSENT', 'LEAVE']
