import { useEffect, useState } from 'react'

interface PiProject {
  name: string
  entries: string[]
}

export function PiProjectsPanel() {
  const [projects, setProjects] = useState<PiProject[]>([])
  const [error, setError] = useState('')
  const [account, setAccount] = useState('a01')
  const [busy, setBusy] = useState('')
  const [msg, setMsg] = useState('')

  useEffect(() => {
    fetch('/api/pi/projects')
      .then((r) => r.json())
      .then((data) => setProjects(data as PiProject[]))
      .catch((e) => setError(String(e)))
  }, [])

  async function enqueue(project: string, entry: string) {
    setBusy(entry)
    setMsg('')
    try {
      const res = await fetch('/api/pi/enqueue', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project, entry, account }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.error || 'enqueue failed')
      setMsg(`已入队 ${data.kind}`)
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
      <h3 className="font-medium mb-2">可用 PI 项目</h3>
      <div className="mb-2 text-sm">
        账号：
        <input
          className="border px-1 ml-1 text-sm"
          value={account}
          onChange={(e) => setAccount(e.target.value)}
          size={8}
        />
      </div>
      {projects.map((p) => (
        <div key={p.name} className="mb-2">
          <div className="text-sm font-mono">{p.name}</div>
          <div className="flex flex-wrap gap-1 mt-1">
            {p.entries.slice(0, 8).map((e) => (
              <button
                key={e}
                className="text-xs border rounded px-2 py-0.5 hover:bg-blue-50 disabled:opacity-50"
                disabled={busy === e}
                onClick={() => enqueue(p.name, e)}
              >
                {busy === e ? '...' : e}
              </button>
            ))}
          </div>
        </div>
      ))}
      {msg && <p className="text-xs mt-2 text-gray-600">{msg}</p>}
    </div>
  )
}
