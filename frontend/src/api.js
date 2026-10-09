// Thin fetch wrapper around the FastAPI backend. Errors carry the backend's {"detail": ...} message.
const BASE = import.meta.env.VITE_API_URL || '/api'

function describe(detail) {
  if (!detail) return 'Request failed'
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) return detail.map((d) => `${(d.loc || []).slice(1).join('.')}: ${d.msg}`).join('; ')
  return JSON.stringify(detail)
}

export async function api(path, { method = 'GET', params, body } = {}) {
  const qs = new URLSearchParams()
  Object.entries(params || {}).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== '') qs.set(k, v)
  })
  const url = `${BASE}${path}${qs.toString() ? `?${qs}` : ''}`
  let res
  try {
    res = await fetch(url, {
      method,
      headers: body ? { 'Content-Type': 'application/json' } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    })
  } catch {
    throw new Error('Cannot reach the API. Is the backend running on port 8000?')
  }
  const data = await res.json().catch(() => ({}))
  if (!res.ok) {
    const err = new Error(describe(data.detail))
    err.status = res.status
    throw err
  }
  return data
}
