import { useEffect, useMemo, useState } from 'react'
import { api, describeError } from '../api'
import type { SpaAccountStatus, SpaStatusResponse } from '../types'

function statusLabel(st?: string): { text: string; color: string } {
  if (!st) return { text: '未运行', color: '#888' }
  if (st === 'completed') return { text: '已完成', color: '#22c55e' }
  if (st === 'running') return { text: '刷取中', color: '#3b82f6' }
  if (st.startsWith('retrying_')) return { text: `自动重试 (${st.replace('retrying_', '#')})`, color: '#f59e0b' }
  if (st === 'waiting_mas_idle') return { text: '避让 MAS 中', color: '#a855f7' }
  if (st === 'pending') return { text: '排队中', color: '#94a3b8' }
  if (st === 'tutorial_locked' || st === 'skipped_tutorial_locked') {
    return { text: '未过入门协议', color: '#ef4444' }
  }
  if (st === 'skipped') return { text: '已跳过', color: '#64748b' }
  return { text: st, color: '#ef4444' }
}

export function SpaBadgePanel() {
  const [open, setOpen] = useState(false)
  const [data, setData] = useState<SpaStatusResponse | null>(null)
  const [selectedEmus, setSelectedEmus] = useState<string[]>([])
  const [initedSelection, setInitedSelection] = useState(false)

  const [mode, setMode] = useState<'badge' | 'score'>('badge')
  const [target, setTarget] = useState<number>(3)
  const [runs, setRuns] = useState<number>(60)
  const [concurrency, setConcurrency] = useState<number>(3)
  const [maxRetries, setMaxRetries] = useState<number>(3)

  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')
  const [logEmu, setLogEmu] = useState<string | null>(null)
  const [logText, setLogText] = useState('')
  const [logLoading, setLogLoading] = useState(false)

  const refreshStatus = () => {
    api
      .spaStatus()
      .then((res) => {
        setData(res)
        if (!initedSelection && res.configAccounts && res.configAccounts.length > 0) {
          const stMap = res.status?.accounts ?? {}
          const defaults = res.configAccounts
            .filter((a) => {
              const s = stMap[a.emu]?.status
              return s !== 'tutorial_locked' && s !== 'skipped_tutorial_locked'
            })
            .map((a) => a.emu)
          setSelectedEmus(defaults)
          setInitedSelection(true)
        }
      })
      .catch(() => {})
  }

  useEffect(() => {
    refreshStatus()
    const timer = window.setInterval(refreshStatus, 5000)
    return () => window.clearInterval(timer)
  }, [initedSelection])

  useEffect(() => {
    if (!logEmu) return
    let cancelled = false
    const load = () => {
      setLogLoading(true)
      api
        .spaLog(logEmu)
        .then((txt) => {
          if (!cancelled) setLogText(txt)
        })
        .catch((err) => {
          if (!cancelled) setLogText(describeError(err))
        })
        .finally(() => {
          if (!cancelled) setLogLoading(false)
        })
    }
    load()
    const t = window.setInterval(load, 4000)
    return () => {
      cancelled = true
      window.clearInterval(t)
    }
  }, [logEmu])

  const accounts = data?.configAccounts ?? []
  const statusMap: Record<string, SpaAccountStatus> = data?.status?.accounts ?? {}

  const summary = useMemo(() => {
    let running = 0
    let done = 0
    let locked = 0
    for (const acc of accounts) {
      const st = statusMap[acc.emu]?.status
      if (st === 'running' || st?.startsWith('retrying_')) running++
      else if (st === 'completed') done++
      else if (st === 'tutorial_locked' || st === 'skipped_tutorial_locked') locked++
    }
    return { running, done, locked, total: accounts.length }
  }, [accounts, statusMap])

  function toggleEmu(emu: string) {
    setSelectedEmus((prev) =>
      prev.includes(emu) ? prev.filter((x) => x !== emu) : [...prev, emu],
    )
  }

  async function handleStart() {
    if (selectedEmus.length === 0) {
      setNotice('请至少勾选一个账号')
      return
    }
    setBusy(true)
    setNotice('')
    try {
      const res = await api.spaStart({
        mode,
        target,
        runs,
        attempts: 0,
        concurrency,
        maxRetries,
        stallMinutes: 12,
        onlyEmus: selectedEmus,
      })
      setNotice(`已启动卫戍协议批量任务（PID ${res.pid}），共 ${selectedEmus.length} 个账号，${concurrency} 开并发`)
      refreshStatus()
    } catch (err) {
      setNotice(describeError(err))
    } finally {
      setBusy(false)
    }
  }

  async function handleStop() {
    setBusy(true)
    setNotice('')
    try {
      const res = await api.spaStop()
      setNotice(`已停止批量任务（PID: ${(res.stoppedPids ?? []).join(', ') || '无'}）`)
      refreshStatus()
    } catch (err) {
      setNotice(describeError(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section
      style={{
        margin: '0 20px 12px',
        padding: '10px 14px',
        borderRadius: 10,
        border: '1px solid rgba(148,163,184,0.22)',
        background: 'rgba(15,23,42,0.45)',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
          <strong style={{ fontSize: 14 }}>卫戍协议：规程 · 刷角标（MaaCore v6.23+）</strong>
          <span
            style={{
              fontSize: 12,
              padding: '2px 8px',
              borderRadius: 999,
              background: data?.running ? 'rgba(59,130,246,0.2)' : 'rgba(100,116,139,0.2)',
              color: data?.running ? '#60a5fa' : '#94a3b8',
            }}
          >
            {data?.running ? `运行中 · ${summary.running} 开` : '空闲'}
          </span>
          <span style={{ fontSize: 12, color: '#94a3b8' }}>
            完成 {summary.done}/{summary.total}
            {summary.locked > 0 ? ` · 未过入门 ${summary.locked}` : ''}
          </span>
        </div>
        <button
          type="button"
          className="btn btn-sm"
          onClick={() => setOpen((v) => !v)}
        >
          {open ? '收起' : '展开管理'}
        </button>
      </div>

      {open ? (
        <div style={{ marginTop: 12, display: 'flex', flexDirection: 'column', gap: 12 }}>
          {!data?.maaV623Ready || !data?.runnerReady ? (
            <div style={{ fontSize: 12, color: '#f87171' }}>
              环境提示：
              {!data?.maaV623Ready ? ' 缺少 maa_v623/MaaCore.dll' : ''}
              {!data?.runnerReady ? ' 缺少 tools/spa_batch_runner.py' : ''}
              {data?.configError ? ` (${data.configError})` : ''}
            </div>
          ) : null}

          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 10, alignItems: 'center', fontSize: 13 }}>
            <label>
              模式：
              <select
                value={mode}
                onChange={(e) => setMode(e.target.value as 'badge' | 'score')}
                style={{ marginLeft: 4 }}
              >
                <option value="badge">刷角标（完整通关）</option>
                <option value="score">刷分（放生速通）</option>
              </select>
            </label>

            {mode === 'badge' ? (
              <label>
                目标：
                <select
                  value={target}
                  onChange={(e) => setTarget(Number(e.target.value))}
                  style={{ marginLeft: 4 }}
                >
                  <option value={3}>金色角标（每策略 3 胜）</option>
                  <option value={6}>彩色角标（每策略 6 胜）</option>
                </select>
              </label>
            ) : null}

            <label>
              并发：
              <input
                type="number"
                min={1}
                max={6}
                value={concurrency}
                onChange={(e) => setConcurrency(Math.max(1, Math.min(6, Number(e.target.value) || 3)))}
                style={{ width: 52, marginLeft: 4 }}
              />
            </label>

            <label>
              单号目标胜场：
              <input
                type="number"
                min={1}
                max={999}
                value={runs}
                onChange={(e) => setRuns(Math.max(1, Math.min(999, Number(e.target.value) || 60)))}
                style={{ width: 64, marginLeft: 4 }}
              />
            </label>

            <label>
              失败自动重试：
              <input
                type="number"
                min={0}
                max={10}
                value={maxRetries}
                onChange={(e) => setMaxRetries(Math.max(0, Math.min(10, Number(e.target.value) || 0)))}
                style={{ width: 52, marginLeft: 4 }}
              />
            </label>

            <button
              type="button"
              className="btn btn-primary btn-sm"
              disabled={busy || Boolean(data?.running) || !data?.maaV623Ready || !data?.runnerReady}
              onClick={() => void handleStart()}
            >
              一键启动（{selectedEmus.length} 号）
            </button>

            <button
              type="button"
              className="btn btn-danger btn-sm"
              disabled={busy || !data?.running}
              onClick={() => void handleStop()}
            >
              停止批量
            </button>

            <button
              type="button"
              className="btn btn-sm"
              onClick={() => setLogEmu((cur) => (cur === 'main' ? null : 'main'))}
            >
              {logEmu === 'main' ? '关闭总日志' : '总调度日志'}
            </button>
          </div>

          {notice ? <div style={{ fontSize: 12, color: '#93c5fd' }}>{notice}</div> : null}

          <div style={{ overflowX: 'auto' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
              <thead>
                <tr style={{ textAlign: 'left', borderBottom: '1px solid rgba(148,163,184,0.2)', color: '#94a3b8' }}>
                  <th style={{ padding: '6px 8px' }}>选</th>
                  <th style={{ padding: '6px 8px' }}>账号</th>
                  <th style={{ padding: '6px 8px' }}>区服</th>
                  <th style={{ padding: '6px 8px' }}>模拟器</th>
                  <th style={{ padding: '6px 8px' }}>状态</th>
                  <th style={{ padding: '6px 8px' }}>重试</th>
                  <th style={{ padding: '6px 8px' }}>开始 / 结束</th>
                  <th style={{ padding: '6px 8px' }}>日志</th>
                </tr>
              </thead>
              <tbody>
                {accounts.map((acc) => {
                  const st = statusMap[acc.emu]
                  const badge = statusLabel(st?.status)
                  const checked = selectedEmus.includes(acc.emu)
                  return (
                    <tr key={acc.emu} style={{ borderBottom: '1px solid rgba(148,163,184,0.1)' }}>
                      <td style={{ padding: '5px 8px' }}>
                        <input
                          type="checkbox"
                          checked={checked}
                          onChange={() => toggleEmu(acc.emu)}
                        />
                      </td>
                      <td style={{ padding: '5px 8px', fontWeight: 500 }}>{acc.name}</td>
                      <td style={{ padding: '5px 8px' }}>{acc.client === 'Bilibili' ? 'B服' : '官服'}</td>
                      <td style={{ padding: '5px 8px' }} className="mono">emu={acc.emu}</td>
                      <td style={{ padding: '5px 8px', color: badge.color, fontWeight: 600 }}>{badge.text}</td>
                      <td style={{ padding: '5px 8px' }}>{st?.retries ?? 0}</td>
                      <td style={{ padding: '5px 8px', color: '#94a3b8' }} className="mono">
                        {st?.started_at ? st.started_at.slice(5) : '—'}
                        {st?.finished_at ? ` → ${st.finished_at.slice(11)}` : ''}
                      </td>
                      <td style={{ padding: '5px 8px' }}>
                        <button
                          type="button"
                          className="btn btn-sm"
                          onClick={() => setLogEmu((cur) => (cur === acc.emu ? null : acc.emu))}
                        >
                          {logEmu === acc.emu ? '收起' : '看日志'}
                        </button>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>

          {logEmu ? (
            <div style={{ background: 'rgba(2,6,23,0.75)', borderRadius: 8, padding: 10 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 6, fontSize: 12, color: '#94a3b8' }}>
                <span>
                  {logEmu === 'main' ? '批量总调度日志 (spa_batch_main.log)' : `emu=${logEmu} 运行日志`}
                  {logLoading ? ' · 刷新中…' : ''}
                </span>
                <button type="button" className="btn btn-sm" onClick={() => setLogEmu(null)}>
                  关闭
                </button>
              </div>
              <pre
                style={{
                  margin: 0,
                  maxHeight: 260,
                  overflow: 'auto',
                  fontSize: 11,
                  lineHeight: 1.45,
                  whiteSpace: 'pre-wrap',
                  color: '#cbd5e1',
                }}
              >
                {logText || '（暂无日志输出）'}
              </pre>
            </div>
          ) : null}
        </div>
      ) : null}
    </section>
  )
}
