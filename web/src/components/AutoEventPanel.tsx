import { useEffect, useState } from 'react'
import { api, describeError } from '../api'
import type { Account, RunSnapshot } from '../types'

interface AutoEventPanelProps {
  accounts: Account[]
  activeRunId: string | null
  onSelectRun: (runId: string) => void
}

const PHASE_OPTIONS = [
  { id: 'main', label: '第 1 阶段（普通图）' },
  { id: 'ex', label: '第 2 阶段（EX 图）' },
  { id: 's', label: '第 3 阶段（S 图）' },
]

function phaseBadge(phase?: string): string {
  switch (phase) {
    case 'main':
      return '第1阶段'
    case 'ex':
      return '第2阶段 EX'
    case 's':
      return '第3阶段 S'
    default:
      return ''
  }
}

export function AutoEventPanel({ accounts, activeRunId, onSelectRun }: AutoEventPanelProps) {
  const [open, setOpen] = useState(false)
  const [selectedAccounts, setSelectedAccounts] = useState<string[]>([])
  const [groups, setGroups] = useState<{ name: string; stages: string[] }[]>([])
  const [selectedGroup, setSelectedGroup] = useState('')
  const [stagesText, setStagesText] = useState('')
  const [phases, setPhases] = useState<string[]>(['main', 'ex', 's'])
  const [autoScanBox, setAutoScanBox] = useState(true)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')
  const [running, setRunning] = useState<RunSnapshot[]>([])

  useEffect(() => {
    if (selectedAccounts.length === 0 && accounts.length > 0) {
      const enabled = accounts.filter((a) => a.enabled).slice(0, 2).map((a) => a.id)
      setSelectedAccounts(enabled.length > 0 ? enabled : [accounts[0].id])
    }
  }, [accounts])

  useEffect(() => {
    api
      .stageCatalog()
      .then((res) => {
        const list = res.groups ?? []
        setGroups(list)
      })
      .catch(() => {})
  }, [])

  useEffect(() => {
    let cancelled = false
    const poll = () => {
      api
        .activeRuns()
        .then((res) => {
          if (!cancelled) setRunning(res.runs ?? [])
        })
        .catch(() => {})
    }
    poll()
    const timer = window.setInterval(poll, 3000)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [])

  function toggleAccount(id: string) {
    setSelectedAccounts((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id],
    )
  }

  function togglePhase(p: string) {
    setPhases((prev) => (prev.includes(p) ? prev.filter((x) => x !== p) : [...prev, p]))
  }

  function applyGroup(name: string) {
    setSelectedGroup(name)
    const found = groups.find((g) => g.name === name)
    if (found) {
      setStagesText(found.stages.join(', '))
    }
  }

  async function handleStartAll() {
    const codes = stagesText
      .split(/[\s,，;；]+/)
      .map((s) => s.trim())
      .filter(Boolean)
    if (selectedAccounts.length === 0) {
      setNotice('请至少勾选一个账号/模拟器')
      return
    }
    if (codes.length === 0) {
      setNotice('请选择活动分组或输入关卡代号（如 OS-1, OS-2, OS-EX-1）')
      return
    }
    if (phases.length === 0) {
      setNotice('请至少勾选一个阶段')
      return
    }
    setBusy(true)
    setNotice('')
    const results: string[] = []
    let firstRunId = ''
    for (const accId of selectedAccounts) {
      try {
        const res = await api.startAutoEvent({
          accountId: accId,
          stageCodes: codes,
          phases,
          autoScanBox,
          useSupport: true,
        })
        if (!firstRunId) firstRunId = res.runId
        results.push(`${accId}: 已启动 ${res.stageCount} 关`)
      } catch (err) {
        results.push(`${accId}: ${describeError(err)}`)
      }
    }
    if (firstRunId) onSelectRun(firstRunId)
    setNotice(results.join(' ｜ '))
    setBusy(false)
  }

  async function handleStop(id: string) {
    try {
      await api.stopRun(id)
    } catch {
      // ignore
    }
  }

  return (
    <div className="border rounded p-3 mb-3 bg-gray-50">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <strong className="text-sm">一键全自动推活动（按阶段分流 · 支持多模拟器并发）</strong>
          {running.length > 0 && (
            <span className="text-xs px-2 py-0.5 rounded bg-blue-100 text-blue-800">
              运行中 {running.length} 台
            </span>
          )}
        </div>
        <button
          type="button"
          className="text-xs border rounded px-2 py-0.5 hover:bg-white"
          onClick={() => setOpen((v) => !v)}
        >
          {open ? '收起' : '展开配置'}
        </button>
      </div>

      {running.length > 0 && (
        <div className="flex flex-wrap gap-2 mt-2">
          {running.map((r) => (
            <div
              key={r.id}
              className={`flex items-center gap-2 text-xs border rounded px-2 py-1 bg-white ${
                activeRunId === r.id ? 'border-blue-500' : ''
              }`}
            >
              <button
                type="button"
                className="font-mono hover:underline"
                onClick={() => onSelectRun(r.id)}
              >
                {r.accountName || r.accountId}
                {r.phase ? ` · ${phaseBadge(r.phase)}` : ''}
                {` · ${r.stageIndex ?? 1}/${r.stageTotal ?? 1} ${r.stageCode}`}
              </button>
              <button
                type="button"
                className="text-red-600 hover:underline"
                onClick={() => void handleStop(r.id)}
              >
                停止
              </button>
            </div>
          ))}
        </div>
      )}

      {open && (
        <div className="mt-3 space-y-2 text-xs">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-medium">模拟器/账号：</span>
            {accounts.map((a) => (
              <label key={a.id} className="inline-flex items-center gap-1 cursor-pointer">
                <input
                  type="checkbox"
                  checked={selectedAccounts.includes(a.id)}
                  onChange={() => toggleAccount(a.id)}
                />
                <span>
                  {a.id}
                  {a.name ? `(${a.name})` : ''}
                </span>
              </label>
            ))}
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <span className="font-medium">阶段分流：</span>
            {PHASE_OPTIONS.map((p) => (
              <label key={p.id} className="inline-flex items-center gap-1 cursor-pointer">
                <input
                  type="checkbox"
                  checked={phases.includes(p.id)}
                  onChange={() => togglePhase(p.id)}
                />
                <span>{p.label}</span>
              </label>
            ))}
            <label className="inline-flex items-center gap-1 ml-2 cursor-pointer">
              <input
                type="checkbox"
                checked={autoScanBox}
                onChange={(e) => setAutoScanBox(e.target.checked)}
              />
              <span>启动前自动扫描干员</span>
            </label>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <span className="font-medium">活动分组：</span>
            <select
              className="border rounded px-1.5 py-0.5 bg-white"
              value={selectedGroup}
              onChange={(e) => applyGroup(e.target.value)}
            >
              <option value="">-- 选择活动快捷填充 --</option>
              {groups.map((g) => (
                <option key={g.name} value={g.name}>
                  {g.name}（{g.stages.length} 关）
                </option>
              ))}
            </select>
            <input
              className="border rounded px-2 py-0.5 flex-1 min-w-[220px] bg-white font-mono"
              placeholder="关卡代号，逗号分隔，如 OS-1, OS-2, OS-EX-1, OS-S-1"
              value={stagesText}
              onChange={(e) => setStagesText(e.target.value)}
            />
            <button
              type="button"
              className="border rounded px-3 py-1 bg-blue-600 text-white disabled:opacity-50"
              disabled={busy}
              onClick={() => void handleStartAll()}
            >
              {busy ? '正在扫箱与匹配作业…' : '开始全自动推关'}
            </button>
          </div>

          {notice && <p className="text-gray-700">{notice}</p>}
        </div>
      )}
    </div>
  )
}
