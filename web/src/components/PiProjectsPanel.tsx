import { useEffect, useState } from 'react'
import { api } from '../api'

interface PiProject {
  name: string
  entries: string[]
}

export function PiProjectsPanel() {
  const [projects, setProjects] = useState<PiProject[]>([])
  const [error, setError] = useState('')

  useEffect(() => {
    api.get('/api/pi/projects')
      .then((data) => setProjects(data as PiProject[]))
      .catch((e) => setError(String(e)))
  }, [])

  if (error) return <div className="text-sm text-red-500">PI 项目加载失败：{error}</div>
  if (projects.length === 0) return null

  return (
    <div className="border rounded p-3 mb-4 bg-gray-50">
      <h3 className="font-medium mb-2">可用 PI 项目</h3>
      {projects.map((p) => (
        <div key={p.name} className="mb-2">
          <div className="text-sm font-mono">{p.name}</div>
          <div className="text-xs text-gray-600">
            {p.entries.slice(0, 6).join(' · ')}
            {p.entries.length > 6 ? ` … +${p.entries.length - 6}` : ''}
          </div>
        </div>
      ))}
      <p className="text-xs text-gray-500 mt-2">
        入队示例：{`{"op":"enqueue","kind":"pi.${projects[0]?.name || 'demo'}.${projects[0]?.entries[0] || 'Nav.ToHome'}","account":"a01"}`}
      </p>
    </div>
  )
}
