import { useEffect, useState } from 'react'
import type { Account } from '../types'

interface PiProject {
  name: string
  resource?: string
  entries: string[]
}

interface PiProjectsPanelProps {
  accounts?: Account[]
  selectedAccountId?: string
}

const DEFAULT_VISIBLE = 8

export function PiProjectsPanel({ accounts = [], selectedAccountId = '' }: PiProjectsPanelProps) {
  const [projects, setProjects] = useState<PiProject[]>([])
  const [error, setError] = useState('')
  const [account, setAccount] = useState(selectedAccountId || accounts[0]?.id || 'a01')
  const [busy, setBusy] = useState('')
  const [msg, setMsg] = useState('')
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})

  useEffect(() => {
    if (selectedAccountId) {
      setAccount(selectedAccountId)
    } else if (!account && accounts[0]?.id) {
      setAccount(accounts[0].id)
    }
  }, [selectedAccountId, accounts])

  useEffect(() => {
    fetch('/api/pi/projects')
      .then((r) => r.json())
      .then((data) => setProjects(Array.isArray(data) ? (data as PiProject[]) : []))
      .catch((e) => setError(String(e)))
  }, [])

  async function enqueue(project: string, entry: string) {
    const targetAccount = account.trim()
    if (!targetAccount) {
      setMsg('请先选择或输入账号')
      return
    }
    const key = `${project}:${entry}`
    setBusy(key)
    setMsg('')
    try {
      const res = await fetch('/api/pi/enqueue', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project, entry, account: targetAccount }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.error || 'enqueue failed')
      setMsg(`已入队 ${data.kind}（账号 ${data.account}）`)
    } catch (e) {
      setMsg(String(e))
    } finally {
      setBusy('')
    }
  }

  if (error) return <div className="text-sm text-red-500">PI 项目加载失败：{error}</div>
  if (projects.length === 0) return null

  return (
    <div className="border rounded p-3 mb-4 bg-gray-50">
      <div className="flex items-center justify-between gap-2 mb-2">
        <h3 className="font-medium">可用 PI 项目（{projects.length}）</h3>
        <div className="text-sm flex items-center gap-1">
          <span>账号：</span>
          {accounts.length > 0 ? (
            <select
              className="border px-1.5 py-0.5 text-sm rounded bg-white"
              value={account}
              onChange={(e) => setAccount(e.target.value)}
            >
              {accounts.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name ? `${a.id} · ${a.name}` : a.id}
                </option>
              ))}
            </select>
          ) : (
            <input
              className="border px-1 text-sm rounded"
              value={account}
              onChange={(e) => setAccount(e.target.value)}
              size={10}
            />
          )}
        </div>
      </div>
      {projects.map((p) => {
        const isExpanded = Boolean(expanded[p.name])
        const visible = isExpanded ? p.entries : p.entries.slice(0, DEFAULT_VISIBLE)
        const hiddenCount = p.entries.length - visible.length
        return (
          <div key={p.name} className="mb-2">
            <div className="text-sm font-mono">
              {p.name}
              {p.resource ? <span className="text-xs text-gray-500 ml-1">({p.resource})</span> : null}
            </div>
            <div className="flex flex-wrap gap-1 mt-1">
              {visible.map((e) => {
                const key = `${p.name}:${e}`
                return (
                  <button
                    key={e}
                    type="button"
                    className="text-xs border rounded px-2 py-0.5 hover:bg-blue-50 disabled:opacity-50"
                    disabled={busy === key}
                    onClick={() => enqueue(p.name, e)}
                  >
                    {busy === key ? '...' : e}
                  </button>
                )
              })}
              {hiddenCount > 0 && (
                <button
                  type="button"
                  className="text-xs text-blue-600 px-1.5 py-0.5 hover:underline"
                  onClick={() => setExpanded((prev) => ({ ...prev, [p.name]: true }))}
                >
                  展开其余 {hiddenCount} 项
                </button>
              )}
              {isExpanded && p.entries.length > DEFAULT_VISIBLE && (
                <button
                  type="button"
                  className="text-xs text-gray-500 px-1.5 py-0.5 hover:underline"
                  onClick={() => setExpanded((prev) => ({ ...prev, [p.name]: false }))}
                >
                  收起
                </button>
              )}
            </div>
          </div>
        )
      })}
      {msg && <p className="text-xs mt-2 text-gray-600">{msg}</p>}
    </div>
  )
}
