import { useEffect, useRef, useState } from 'react'
import { api, describeError } from '../api'
import type { Account, NavPack, RunSnapshot } from '../types'

interface AutoEventPanelProps {
  accounts: Account[]
  activeRunId: string | null
  onSelectRun: (runId: string) => void
}

const PHASE_OPTIONS = [
  { id: 'main', label: '第 1 阶段（普通图）', defaultTask: '进入普通图' },
  { id: 'ex', label: '第 2 阶段（EX 图）', defaultTask: '进入EX图' },
  { id: 's', label: '第 3 阶段（S 图）', defaultTask: '进入S图' },
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
  const [phaseNavTasks, setPhaseNavTasks] = useState<Record<string, string>>({})
  const [navPacks, setNavPacks] = useState<NavPack[]>([])
  const [importName, setImportName] = useState('')
  const [importPhase, setImportPhase] = useState('main')
  const [importing, setImporting] = useState(false)
  const fileRef = useRef<HTMLInputElement | null>(null)
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
      .then((res) => setGroups(res.groups ?? []))
      .catch(() => {})
    api
      .navPacks()
      .then((res) => setNavPacks(res.packs ?? []))
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

  async function handleImportNav() {
    const file = fileRef.current?.files?.[0]
    if (!file) {
      setNotice('请先选择 MAA 可视化编辑器 / MPE 导出的 .json 或 .zip 文件')
      return
    }
    setImporting(true)
    setNotice('')
    try {
      const res = await api.importNavPack(file, importName, importPhase)
      setNavPacks((prev) => [...prev, res.pack])
      if (res.pack.phase) {
        setPhaseNavTasks((prev) => ({ ...prev, [res.pack.phase]: res.pack.taskName }))
      }
      setImportName('')
      if (fileRef.current) fileRef.current.value = ''
      setNotice(`已导入导航「${res.pack.name}」（入口 ${res.pack.entry}，${res.pack.nodes.length} 节点）并自动绑定到对应阶段`)
    } catch (err) {
      setNotice(`导入导航失败：${describeError(err)}`)
    } finally {
      setImporting(false)
    }
  }

  async function handleDeleteNav(id: string) {
    try {
      await api.deleteNavPack(id)
      setNavPacks((prev) => prev.filter((p) => p.id !== id))
    } catch (err) {
      setNotice(describeError(err))
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
    const customNav: Record<string, string> = {}
    for (const p of phases) {
      if (phaseNavTasks[p]) customNav[p] = phaseNavTasks[p]
    }
    const results: string[] = []
    let firstRunId = ''
    for (const accId of selectedAccounts) {
      try {
        const res = await api.startAutoEvent({
          accountId: accId,
          stageCodes: codes,
          phases,
          phaseNavTasks: Object.keys(customNav).length > 0 ? customNav : undefined,
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
        <div className="mt-3 space-y-2.5 text-xs">
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
            <label className="inline-flex items-center gap-1 ml-2 cursor-pointer">
              <input
                type="checkbox"
                checked={autoScanBox}
                onChange={(e) => setAutoScanBox(e.target.checked)}
              />
              <span>启动前自动扫描干员</span>
            </label>
          </div>

          <div className="space-y-1.5 border-t pt-2">
            <div className="font-medium">各阶段开关与对应导航（进入该页 → 自动执行该阶段作业集）：</div>
            <div className="flex flex-wrap gap-3">
              {PHASE_OPTIONS.map((p) => (
                <div key={p.id} className="inline-flex items-center gap-1.5">
                  <label className="inline-flex items-center gap-1 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={phases.includes(p.id)}
                      onChange={() => togglePhase(p.id)}
                    />
                    <span>{p.label}</span>
                  </label>
                  <select
                    className="border rounded px-1.5 py-0.5 bg-white"
                    value={phaseNavTasks[p.id] ?? ''}
                    onChange={(e) =>
                      setPhaseNavTasks((prev) => ({ ...prev, [p.id]: e.target.value }))
                    }
                  >
                    <option value="">默认（{p.defaultTask}）</option>
                    {navPacks.map((np) => (
                      <option key={np.id} value={np.taskName}>
                        {np.name}（{np.entry}）
                      </option>
                    ))}
                  </select>
                </div>
              ))}
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-2 border-t pt-2">
            <span className="font-medium">导入可视化导航（MAA/MPE 导出）：</span>
            <input ref={fileRef} type="file" accept=".json,.zip" className="text-xs" />
            <input
              className="border rounded px-1.5 py-0.5 bg-white"
              placeholder="名称（如 新活动1阶段）"
              value={importName}
              onChange={(e) => setImportName(e.target.value)}
              size={14}
            />
            <select
              className="border rounded px-1.5 py-0.5 bg-white"
              value={importPhase}
              onChange={(e) => setImportPhase(e.target.value)}
            >
              <option value="main">绑定到第 1 阶段</option>
              <option value="ex">绑定到第 2 阶段</option>
              <option value="s">绑定到第 3 阶段</option>
            </select>
            <button
              type="button"
              className="border rounded px-2 py-0.5 bg-white hover:bg-blue-50 disabled:opacity-50"
              disabled={importing}
              onClick={() => void handleImportNav()}
            >
              {importing ? '导入中…' : '导入导航文件'}
            </button>
            {navPacks.length > 0 && (
              <div className="flex flex-wrap gap-1.5 w-full mt-1">
                {navPacks.map((np) => (
                  <span
                    key={np.id}
                    className="inline-flex items-center gap-1 px-2 py-0.5 rounded border bg-white"
                  >
                    <span>
                      {np.name} ({np.nodes.length}节点{np.images > 0 ? `/${np.images}图` : ''})
                    </span>
                    <button
                      type="button"
                      className="text-red-500 hover:underline"
                      onClick={() => void handleDeleteNav(np.id)}
                    >
                      删
                    </button>
                  </span>
                ))}
              </div>
            )}
          </div>

          <div className="flex flex-wrap items-center gap-2 border-t pt-2">
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
