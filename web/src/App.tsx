// 单页应用：三段式流程（账号+名册 → 关卡 → 作业）+ 右侧执行面板 + 设置抽屉。
//
// 网络请求全部走 ./api（类型化 fetch / SSE），失败统一用 describeError 转中文；
// 步骤状态集中在这里，components/ 下的展示组件按既有 props 接线，不改它们的对外签名。
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, ApiError, describeError } from './api'
import { DEFAULT_SETTINGS } from './types'
import type {
  Account,
  EventLevel,
  Health,
  Job,
  JobMode,
  JobSearch,
  Roster,
  RunEvent,
  RunOptions,
  RunSnapshot,
  RunStatus,
  Settings,
  Stage,
  StagePlan,
  StageProgress,
} from './types'
import { AccountStep } from './components/AccountStep'
import { Notice, Spinner } from './components/Feedback'
import { JobsStep } from './components/JobsStep'
import { ProgressPanel } from './components/ProgressPanel'
import { QueuePanel } from './components/QueuePanel'
import type { QueueEntry } from './components/QueuePanel'
import { ServiceBanner } from './components/ServiceBanner'
import { StageStep } from './components/StageStep'
import { StatusBadge } from './components/StatusBadge'
import { StepCard } from './components/StepCard'
import { TopBar } from './components/TopBar'
import { Icon } from './components/icons'
import { PiProjectsPanel } from './components/PiProjectsPanel'

// 本地设置（只影响下一次开始执行，存在浏览器里）。
const SETTINGS_KEY = 'copilot-picker.settings.v1'

// 页面会话状态：账号/名册/关卡/搜索结果/队列，刷新或重开客户端后自动恢复。
const SESSION_KEY = 'copilot-picker.session.v1'

interface SessionData {
  accountId?: string
  rosterText?: string
  roster?: Roster | null
  stageQuery?: string
  stages?: Stage[]
  selectedStages?: Stage[]
  activeStageKey?: string
  search?: JobSearch | null
  selectedIds?: number[]
  queue?: QueueEntry[]
  /** 作业难度过滤（'' 全部 / normal / raid），刷新后保持 */
  jobMode?: JobMode
}

interface UiStatePayload extends SessionData {
  customStages?: string[]
  settings?: Partial<Settings>
}

/** 宽松校验：只接受数组，元素必须是对象（具体字段由组件兜底）。 */
function asObjectList<T>(value: unknown): T[] | null {
  if (!Array.isArray(value)) return null
  return value.filter((item): item is T => Boolean(item) && typeof item === 'object') 
}

function asNumberList(value: unknown): number[] | null {
  if (!Array.isArray(value)) return null
  return value.filter((item): item is number => typeof item === 'number')
}

function asStringList(value: unknown): string[] | null {
  if (!Array.isArray(value)) return null
  return value.filter((item): item is string => typeof item === 'string')
}

function loadSession(): SessionData | null {
  try {
    const raw = window.localStorage.getItem(SESSION_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw) as SessionData
    if (!parsed || typeof parsed !== 'object') return null
    return parsed
  } catch {
    return null
  }
}

// 自定义快捷关卡（页面里手动添加，存浏览器；点击按钮 = 解析该关 + 自动搜作业）。
const SHORTCUTS_KEY = 'copilot-picker.stage-shortcuts.v1'
const OLD_PRESETS_KEY = 'copilot-picker.stage-presets.v1'

function loadCustomStages(): string[] {
  try {
    const raw = window.localStorage.getItem(SHORTCUTS_KEY)
    if (raw) {
      const parsed = JSON.parse(raw) as unknown
      if (Array.isArray(parsed)) {
        return parsed.filter((item): item is string => typeof item === 'string' && item.trim() !== '')
      }
    }
    // 兼容旧版“分组”数据：摊平成一串关卡代号
    const oldRaw = window.localStorage.getItem(OLD_PRESETS_KEY)
    if (oldRaw) {
      const old = JSON.parse(oldRaw) as unknown
      if (Array.isArray(old)) {
        const out: string[] = []
        for (const item of old) {
          if (!item || typeof item !== 'object') continue
          const stages = (item as { stages?: unknown }).stages
          if (!Array.isArray(stages)) continue
          for (const code of stages) {
            if (typeof code === 'string' && code.trim() && !out.includes(code.trim())) {
              out.push(code.trim())
            }
          }
        }
        return out
      }
    }
    return []
  } catch {
    return []
  }
}

// 搜索作业时每关最多返回几份（服务端默认也是 8）。
const SEARCH_LIMIT = 8

// 搜完默认勾选前几份，用户再自己调顺序。
const DEFAULT_PICK = 3

// 日志最多保留多少条，防止长任务把 DOM 撑爆。
const MAX_LOG_LINES = 400

// 执行面板轮询间隔：同时用于刷新状态快照与截图（契约要求截图 2s 刷新）。
const POLL_INTERVAL_MS = 2000

const CLIENT_LABELS: Record<string, string> = {
  Official: '官服',
  Bilibili: 'B 服',
  YoStarJP: '日服',
  YoStarEN: '国际服',
  YoStarKR: '韩服',
  txwy: '繁中服',
}

function clientLabel(client: string): string {
  if (!client) return '未知客户端'
  return CLIENT_LABELS[client] ?? client
}

const LEVEL_LABELS: Record<EventLevel, string> = {
  info: '信息',
  warn: '警告',
  error: '错误',
  stage: '阶段',
}

function isTerminal(status: RunStatus | null | undefined): boolean {
  return status === 'success' || status === 'failed' || status === 'stopped'
}

/** 关卡的稳定键（code + 作业站代码）。 */
function stageKey(stage: Stage): string {
  return `${stage.code}|${stage.stageName}`
}

/** 作业难度 2=突袭 3=磨难（S 关高难）：执行时都要按突袭下发 is_raid=true。 */
function isRaidJob(job: Job | undefined): boolean {
  return Boolean(job && (job.difficulty === 2 || job.difficulty === 3))
}

function clampInt(value: unknown, min: number, max: number, fallback: number): number {
  const num = typeof value === 'number' ? Math.round(value) : Number.NaN
  if (!Number.isFinite(num)) return fallback
  return Math.min(max, Math.max(min, num))
}

/** 从 localStorage 读执行设置；坏数据/隐私模式都回落到默认值。 */
function loadSettings(): Settings {
  try {
    const raw = window.localStorage.getItem(SETTINGS_KEY)
    if (!raw) return DEFAULT_SETTINGS
    const parsed = JSON.parse(raw) as Partial<Settings>
    const merged: Settings = { ...DEFAULT_SETTINGS, ...parsed }
    merged.client = typeof merged.client === 'string' ? merged.client : ''
    merged.formation = Boolean(merged.formation)
    merged.addTrust = Boolean(merged.addTrust)
    merged.ignoreRequirements = Boolean(merged.ignoreRequirements)
    merged.manualMode = Boolean(merged.manualMode)
    merged.useSupport = merged.useSupport !== false
    merged.maxAttempts = clampInt(merged.maxAttempts, 1, 10, DEFAULT_SETTINGS.maxAttempts)
    merged.timeoutMin = clampInt(merged.timeoutMin, 1, 120, DEFAULT_SETTINGS.timeoutMin)
    return merged
  } catch {
    return DEFAULT_SETTINGS
  }
}

// ---------------------------------------------------------------------------
// 执行面板：状态徽标 / 进度 / 截图 / SSE 日志 / 停止
// ---------------------------------------------------------------------------

interface RunPanelProps {
  runId: string | null
  snapshot: RunSnapshot | null
  events: RunEvent[]
  runError: string
  streamError: string
  shotTs: number
  shotFailed: boolean
  stopping: boolean
  maaLogText: string
  maaLogError: string
  maaLogMissing: boolean
  killMaaLoading: boolean
  killMaaNotice: string
  onStop: () => void
  onShotError: () => void
  onKillMaa: () => void
}

function RunPanel({
  runId,
  snapshot,
  events,
  runError,
  streamError,
  shotTs,
  shotFailed,
  stopping,
  maaLogText,
  maaLogError,
  maaLogMissing,
  killMaaLoading,
  killMaaNotice,
  onStop,
  onShotError,
  onKillMaa,
}: RunPanelProps) {
  const logRef = useRef<HTMLDivElement | null>(null)
  // 日志是否吸附在底部：用户往上翻时暂停自动滚动，避免读日志被拽走。
  const [stick, setStick] = useState(true)

  const status: RunStatus = snapshot?.status ?? 'idle'
  const active = runId !== null && !isTerminal(snapshot?.status)
  const attempt = snapshot?.attempt ?? 0
  const total = snapshot?.total ?? 0
  const percent = total > 0 ? Math.min(100, Math.round((attempt / total) * 100)) : 0
  const showShot = runId !== null && snapshot?.hasShot === true && !shotFailed

  // 换一次执行就把日志滚回底部，重新吸附。
  useEffect(() => {
    setStick(true)
  }, [runId])

  useEffect(() => {
    const el = logRef.current
    if (!el || !stick) return
    el.scrollTop = el.scrollHeight
  }, [events, stick])

  const handleLogScroll = () => {
    const el = logRef.current
    if (!el) return
    setStick(el.scrollHeight - el.scrollTop - el.clientHeight < 36)
  }

  const jumpToLatest = () => {
    const el = logRef.current
    if (el) el.scrollTop = el.scrollHeight
    setStick(true)
  }

  return (
    <div className="run-inner">
      <header className="run-head">
        <div className="run-title-row">
          <span className="run-title">
            <Icon name="terminal" size={16} /> 执行面板
          </span>
          <StatusBadge status={status} />
        </div>
        <div className="run-actions">
          <span className="run-id mono" title={runId ?? '还没有执行记录'}>
            {runId ?? '未开始'}
          </span>
          <button
            type="button"
            className="btn btn-danger btn-sm"
            onClick={onStop}
            disabled={!active || stopping}
            title={active ? '停止当前执行（会杀掉当前 MAA 进程）' : '当前没有执行中的任务'}
          >
            {stopping ? <Spinner /> : <Icon name="stop" size={13} />} 停止
          </button>
        </div>
      </header>

      {runId === null ? (
        <div className="run-empty">
          <span className="run-empty-icon">
            <Icon name="radar" size={30} />
          </span>
          <p className="run-empty-title">还没有执行记录</p>
          <p className="run-empty-text">
            左侧完成三步后点「开始执行」。这里会实时显示状态、SSE 日志与每 2 秒刷新的模拟器截图。
          </p>
        </div>
      ) : (
        <>
          {snapshot === null ? (
            <p className="loading-line">
              <Spinner /> 正在读取执行状态…
            </p>
          ) : null}

          <div className="run-meta">
            <div className="run-meta-item">
              <span className="run-meta-label">账号</span>
              <span className="run-meta-value">{snapshot?.accountName || snapshot?.accountId || '—'}</span>
            </div>
            <div className="run-meta-item">
              <span className="run-meta-label">客户端</span>
              <span className="run-meta-value">{snapshot?.client ? clientLabel(snapshot.client) : '—'}</span>
            </div>
            <div className="run-meta-item">
              <span className="run-meta-label">关卡</span>
              <span className="run-meta-value mono" title={snapshot?.stageName}>
                {snapshot?.stageTotal && snapshot.stageTotal > 1
                  ? `${snapshot.stageIndex ?? 0}/${snapshot.stageTotal} · `
                  : ''}
                {snapshot?.stageCode || '—'}
              </span>
            </div>
            <div className="run-meta-item">
              <span className="run-meta-label">开始</span>
              <span className="run-meta-value mono">{snapshot?.startedAt || '—'}</span>
            </div>
          </div>

          <div className="run-progress">
            <div className="run-progress-head">
              <span className="run-meta-label">进度</span>
              <span className="mono">
                {attempt}/{total || '—'}
              </span>
            </div>
            <div className="run-progress-bar">
              <span className="run-progress-fill" style={{ width: `${percent}%` }} />
            </div>
          </div>

          {snapshot?.currentJobId ? (
            <div className="run-current">
              <span className="run-meta-label">当前作业</span>
              <span className="mono run-current-id">#{snapshot.currentJobId}</span>
              <span className="run-current-title">{snapshot.currentJobTitle || '（无标题作业）'}</span>
            </div>
          ) : null}

          {snapshot?.error ? <Notice tone="error">{snapshot.error}</Notice> : null}
          {runError ? <Notice tone="error">{runError}</Notice> : null}
          {streamError ? <Notice tone="info">{streamError}</Notice> : null}

          <section className="run-shot">
            <div className="run-shot-head">
              <span className="run-shot-title">
                <Icon name="camera" size={14} /> 实时截图
              </span>
              <span className="field-note">{active ? '每 2 秒刷新' : '执行已结束'}</span>
            </div>
            <div className="shot-frame">
              {showShot && runId !== null ? (
                <img
                  className="shot-img"
                  src={api.shotUrl(runId, shotTs)}
                  alt="模拟器实时截图"
                  onError={onShotError}
                />
              ) : (
                <div className="shot-empty">
                  <Icon name="camera" size={24} />
                  <span>
                    {shotFailed
                      ? '截图加载失败，稍后自动重试'
                      : '等待截图：每份作业开始前会截一张'}
                  </span>
                </div>
              )}
            </div>
          </section>

          <section className="run-log">
            <div className="run-log-head">
              <span className="run-shot-title">
                <Icon name="terminal" size={14} /> 执行日志
              </span>
              <div className="row row-tight">
                <span className="field-note">{events.length} 条</span>
                {!stick ? (
                  <button type="button" className="btn btn-ghost btn-xs log-jump" onClick={jumpToLatest}>
                    跳到最新
                  </button>
                ) : null}
              </div>
            </div>
            <div className="run-log-body" ref={logRef} onScroll={handleLogScroll}>
              {events.length === 0 ? (
                <p className="log-empty">
                  {snapshot ? '等待事件…' : '正在连接执行器…'}
                </p>
              ) : (
                events.map((event) => (
                  <div className={`log-line log-${event.level}`} key={event.seq}>
                    <span className="log-at mono">{event.at}</span>
                    <span className={`log-tag log-tag-${event.level}`}>
                      {LEVEL_LABELS[event.level] ?? event.level}
                    </span>
                    <span className="log-msg">{event.message}</span>
                  </div>
                ))
              )}
            </div>
          </section>
        </>
      )}

      <MaaLogSection text={maaLogText} error={maaLogError} missing={maaLogMissing} />

      <div className="run-kill-row">
        <button
          type="button"
          className="btn btn-ghost btn-sm"
          onClick={onKillMaa}
          disabled={killMaaLoading}
          title="停掉当前任务，并杀掉本服务启动的残留 maa-cli 进程（不碰 MAA 桌面端）"
        >
          {killMaaLoading ? '正在结束…' : '结束 MAA 进程'}
        </button>
        <span className="field-note">
          {killMaaNotice || '只结束本服务起的 maa-cli（按 copilot-picker profile 匹配）'}
        </span>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// MAA 日志区：直接嵌在执行面板里（不是弹层），每 2 秒刷 asst.log 尾巴
// ---------------------------------------------------------------------------

interface MaaLogSectionProps {
  text: string
  error: string
  /** asst.log 还没生成（404）：显示友好等待态，不显示成“读取失败” */
  missing: boolean
}

const MAA_LOG_MAX_LINES = 240

function MaaLogSection({ text, error, missing }: MaaLogSectionProps) {
  const ref = useRef<HTMLPreElement | null>(null)
  const [stick, setStick] = useState(true)

  const tail = useMemo(() => {
    if (!text) return ''
    const lines = text.split('\n')
    return (lines.length > MAA_LOG_MAX_LINES ? lines.slice(lines.length - MAA_LOG_MAX_LINES) : lines).join('\n')
  }, [text])

  useEffect(() => {
    const el = ref.current
    if (!el || !stick) return
    el.scrollTop = el.scrollHeight
  }, [tail, stick])

  return (
    <section className="run-log">
      <div className="run-log-head">
        <span className="run-shot-title">
          <Icon name="terminal" size={14} /> MAA 日志（asst.log）
        </span>
        <div className="row row-tight">
          <span className="field-note">
            {missing ? '等待日志生成（每 2 秒检查）' : error ? '读取失败，2 秒后重试' : '每 2 秒刷新'}
          </span>
          {!stick ? (
            <button
              type="button"
              className="btn btn-ghost btn-xs"
              onClick={() => {
                const el = ref.current
                if (el) el.scrollTop = el.scrollHeight
                setStick(true)
              }}
            >
              跳到最新
            </button>
          ) : null}
        </div>
      </div>
      <pre
        className="run-log-body maa-log-body"
        ref={ref}
        onScroll={() => {
          const el = ref.current
          if (!el) return
          setStick(el.scrollHeight - el.scrollTop - el.clientHeight < 36)
        }}
      >
        {missing
          ? 'MAA 还没写过 asst.log：跑一次「扫描干员」或执行任务后，这里会自动出现日志。'
          : error
            ? error
            : tail || '等待 MAA 日志…'}
      </pre>
    </section>
  )
}

// ---------------------------------------------------------------------------
// 设置抽屉：客户端 / 编队选项 / 尝试数 / 超时 / 手动模式
// ---------------------------------------------------------------------------

interface SettingsDrawerProps {
  open: boolean
  settings: Settings
  clients: string[]
  accountClient: string
  onClose: () => void
  onChange: (patch: Partial<Settings>) => void
}

function SettingsDrawer({ open, settings, clients, accountClient, onClose, onChange }: SettingsDrawerProps) {
  if (!open) return null

  return (
    <div className="drawer-backdrop" role="presentation" onClick={onClose}>
      <aside
        className="drawer"
        role="dialog"
        aria-modal="true"
        aria-label="执行设置"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="drawer-head">
          <div>
            <h2 className="drawer-title">执行设置</h2>
            <p className="drawer-sub">只影响下一次「开始执行」，保存在本机浏览器里。</p>
          </div>
          <button
            type="button"
            className="btn btn-icon btn-ghost"
            onClick={onClose}
            title="关闭设置"
            aria-label="关闭设置"
          >
            <Icon name="close" size={16} />
          </button>
        </header>

        <div className="drawer-body">
          <label className="drawer-field">
            <span className="field-label">
              <Icon name="user" size={13} /> 客户端
            </span>
            <select
              className="select"
              value={settings.client}
              onChange={(event) => onChange({ client: event.target.value })}
            >
              <option value="">跟随账号（{accountClient ? clientLabel(accountClient) : '未知'}）</option>
              {clients.map((client) => (
                <option key={client} value={client}>
                  {clientLabel(client)}（{client}）
                </option>
              ))}
            </select>
            <span className="field-note">账号表里没写 client 时用这里；两边都为空则服务端用默认客户端。</span>
          </label>

          <div className="drawer-field">
            <span className="field-label">
              <Icon name="settings" size={13} /> 执行选项
            </span>
            <label className="check-row">
              <input
                type="checkbox"
                checked={settings.formation}
                onChange={(event) => onChange({ formation: event.target.checked })}
              />
              <span>自动编队（--formation）</span>
            </label>
            <label className={`check-row ${settings.formation ? '' : 'is-disabled'}`}>
              <input
                type="checkbox"
                checked={settings.addTrust}
                disabled={!settings.formation}
                onChange={(event) => onChange({ addTrust: event.target.checked })}
              />
              <span>补低信赖（--add-trust）</span>
            </label>
            <label className={`check-row ${settings.formation ? '' : 'is-disabled'}`}>
              <input
                type="checkbox"
                checked={settings.ignoreRequirements}
                disabled={!settings.formation}
                onChange={(event) => onChange({ ignoreRequirements: event.target.checked })}
              />
              <span>忽略练度要求（--ignore-requirements）</span>
            </label>
            <label className="check-row">
              <input
                type="checkbox"
                checked={settings.manualMode}
                onChange={(event) => onChange({ manualMode: event.target.checked })}
              />
              <span>手动模式：跳过自动导航</span>
            </label>
            <label className="check-row">
              <input
                type="checkbox"
                checked={settings.useSupport}
                onChange={(event) => onChange({ useSupport: event.target.checked })}
              />
              <span>缺 1 个干员时借用助战</span>
            </label>
            <span className="field-note">
              手动模式需要先让游戏停在关卡编队/关卡详情页；关掉自动编队后，补信赖与忽略练度不会生效。
            </span>
          </div>

          <div className="drawer-grid">
            <label className="drawer-field">
              <span className="field-label">最大尝试份数</span>
              <input
                className="input"
                type="number"
                min={1}
                max={10}
                value={settings.maxAttempts}
                onChange={(event) =>
                  onChange({
                    maxAttempts: clampInt(event.target.valueAsNumber, 1, 10, settings.maxAttempts),
                  })
                }
              />
              <span className="field-note">实际不会超过勾选的作业数。</span>
            </label>
            <label className="drawer-field">
              <span className="field-label">单份作业超时（分钟）</span>
              <input
                className="input"
                type="number"
                min={1}
                max={120}
                value={settings.timeoutMin}
                onChange={(event) =>
                  onChange({
                    timeoutMin: clampInt(event.target.valueAsNumber, 1, 120, settings.timeoutMin),
                  })
                }
              />
              <span className="field-note">超时即判这份失败，自动换下一份。</span>
            </label>
          </div>

          <Notice tone="info">
            执行期间可以在右侧面板随时停止；停止会杀掉当前 MAA 进程，之前已经成功的作业不受影响。
          </Notice>
        </div>

        <footer className="drawer-foot">
          <button type="button" className="btn btn-primary btn-block" onClick={onClose}>
            完成
          </button>
        </footer>
      </aside>
    </div>
  )
}

// ---------------------------------------------------------------------------
// 主应用
// ---------------------------------------------------------------------------

export function App() {
  // 上一次页面会话（账号/名册/关卡/搜索/队列），用于刷新后恢复。
  const saved = useMemo(loadSession, [])

  // ---- 服务与账号 ----
  const [health, setHealth] = useState<Health | null>(null)
  const [healthLoading, setHealthLoading] = useState(true)
  const [healthError, setHealthError] = useState('')
  const [accounts, setAccounts] = useState<Account[]>([])
  const [accountsLoading, setAccountsLoading] = useState(true)
  const [accountsError, setAccountsError] = useState('')
  const [maaReady, setMaaReady] = useState(false)
  const [maaError, setMaaError] = useState('')
  const [selectedAccountId, setSelectedAccountId] = useState(saved?.accountId ?? '')

  // ---- 第 1 步：名册 ----
  const [rosterText, setRosterText] = useState(saved?.rosterText ?? '')
  const [roster, setRoster] = useState<Roster | null>(saved?.roster ?? null)
  const [rosterLoading, setRosterLoading] = useState(false)
  const [rosterError, setRosterError] = useState('')
  const [pasteMessage, setPasteMessage] = useState('')
  const [maaLoading, setMaaLoading] = useState(false)
  const [scanLoading, setScanLoading] = useState(false)
  // 模拟器开关（MuMuManager），走服务端 /api/emu/start
  const [emuStarting, setEmuStarting] = useState(false)
  const [emuNotice, setEmuNotice] = useState('')

  // ---- 第 2 步：关卡（多选）----
  const [stageQuery, setStageQuery] = useState(saved?.stageQuery ?? '')
  const [stages, setStages] = useState<Stage[]>(saved?.stages ?? [])
  const [stageLoading, setStageLoading] = useState(false)
  const [stageError, setStageError] = useState('')
  const [selectedStages, setSelectedStages] = useState<Stage[]>(saved?.selectedStages ?? [])
  const [activeStageKey, setActiveStageKey] = useState(saved?.activeStageKey ?? '')
  // 快捷关卡：服务端按 MAA stages.json 分组 + 页面自定义分组
  const [customStages, setCustomStages] = useState<string[]>(loadCustomStages)

  // ---- 第 3 步：作业 ----
  const [search, setSearch] = useState<JobSearch | null>(saved?.search ?? null)
  const [searchLoading, setSearchLoading] = useState(false)
  const [searchError, setSearchError] = useState('')
  const [searchStale, setSearchStale] = useState(false)
  const [jobMode, setJobMode] = useState<JobMode>(
    saved?.jobMode === 'normal' || saved?.jobMode === 'raid' ? saved.jobMode : '',
  )
  const [selectedIds, setSelectedIds] = useState<number[]>(saved?.selectedIds ?? [])
  const [queue, setQueue] = useState<QueueEntry[]>(saved?.queue ?? [])
  // 批量搜索：一次搜多关，按关卡分组显示
  const [multiSearch, setMultiSearch] = useState<{ stage: Stage; result: JobSearch }[] | null>(null)
  const [multiSelected, setMultiSelected] = useState<Record<string, number[]>>({})
  const [multiLoading, setMultiLoading] = useState(false)
  const [multiError, setMultiError] = useState('')
  const [starting, setStarting] = useState(false)
  const [startError, setStartError] = useState('')
  const [batchQueueing, setBatchQueueing] = useState(false)
  const [batchNotice, setBatchNotice] = useState('')
  // ---- 推关台账（GET /api/progress）----
  const [progress, setProgress] = useState<StageProgress[]>([])
  const [progressLoading, setProgressLoading] = useState(false)
  const [progressError, setProgressError] = useState('')

  // 自定义快捷关卡落盘
  useEffect(() => {
    try {
      window.localStorage.setItem(SHORTCUTS_KEY, JSON.stringify(customStages))
    } catch {
      // 忽略
    }
  }, [customStages])

  // ---- 执行面板 ----
  const [runId, setRunId] = useState<string | null>(null)
  const [snapshot, setSnapshot] = useState<RunSnapshot | null>(null)
  const [events, setEvents] = useState<RunEvent[]>([])
  const [runError, setRunError] = useState('')
  const [streamError, setStreamError] = useState('')
  const [stopping, setStopping] = useState(false)
  const [killMaaLoading, setKillMaaLoading] = useState(false)
  const [killMaaNotice, setKillMaaNotice] = useState('')
  const [shotTs, setShotTs] = useState(0)
  const [shotFailed, setShotFailed] = useState(false)
  const seenEventsRef = useRef<Set<number>>(new Set())

  // ---- 设置抽屉 ----
  const [settings, setSettings] = useState<Settings>(loadSettings)
  const [settingsOpen, setSettingsOpen] = useState(false)

  // ---- 界面状态：存服务端（刷新/重开客户端都能恢复；iframe 的 localStorage 不可靠） ----
  const [uiStateLoaded, setUiStateLoaded] = useState(false)

  const applyUiState = useCallback((state: UiStatePayload) => {
    if (typeof state.accountId === 'string') setSelectedAccountId(state.accountId)
    if (typeof state.rosterText === 'string') setRosterText(state.rosterText)
    if (state.roster && typeof state.roster === 'object') setRoster(state.roster)
    if (typeof state.stageQuery === 'string') setStageQuery(state.stageQuery)
    const stages = asObjectList<Stage>(state.stages)
    if (stages) setStages(stages)
    const selectedStages = asObjectList<Stage>(state.selectedStages)
    if (selectedStages) setSelectedStages(selectedStages)
    if (typeof state.activeStageKey === 'string') setActiveStageKey(state.activeStageKey)
    if (state.search && typeof state.search === 'object') setSearch(state.search)
    if (state.jobMode === '' || state.jobMode === 'normal' || state.jobMode === 'raid') {
      setJobMode(state.jobMode)
    }
    const selectedIds = asNumberList(state.selectedIds)
    if (selectedIds) setSelectedIds(selectedIds)
    const queue = asObjectList<QueueEntry>(state.queue)
    if (queue) setQueue(queue)
    const customStages = asStringList(state.customStages)
    if (customStages) setCustomStages(customStages)
    if (state.settings && typeof state.settings === 'object') {
      setSettings((prev) => ({ ...prev, ...state.settings }))
    }
  }, [])

  useEffect(() => {
    let cancelled = false
    void (async () => {
      try {
        const res = await api.uiState()
        if (!cancelled && res.state && typeof res.state === 'object') {
          applyUiState(res.state as UiStatePayload)
        }
      } catch {
        // 服务没起来时用 localStorage 兜底
      } finally {
        if (!cancelled) setUiStateLoaded(true)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [applyUiState])

  // 状态变化 → localStorage 兜底 + 服务端 ui_state.json（防抖 600ms）。
  useEffect(() => {
    if (!uiStateLoaded) return
    const data: UiStatePayload = {
      accountId: selectedAccountId,
      rosterText,
      roster,
      stageQuery,
      stages,
      selectedStages,
      activeStageKey,
      search,
      selectedIds,
      queue,
      jobMode,
      customStages,
      settings,
    }
    try {
      window.localStorage.setItem(SESSION_KEY, JSON.stringify(data))
    } catch {
      // 忽略配额/隐私模式
    }
    const timer = window.setTimeout(() => {
      void api.saveUiState(data).catch(() => {})
    }, 600)
    return () => window.clearTimeout(timer)
  }, [
    uiStateLoaded,
    selectedAccountId,
    rosterText,
    roster,
    stageQuery,
    stages,
    selectedStages,
    activeStageKey,
    search,
    selectedIds,
    queue,
    jobMode,
    customStages,
    settings,
  ])

  // ---- MAA 日志（直接嵌在执行面板里） ----
  const [maaLogText, setMaaLogText] = useState('')
  const [maaLogError, setMaaLogError] = useState('')
  // asst.log 还没生成（404）不算错误：第一次「扫描干员」/执行前本来就没有日志。
  const [maaLogMissing, setMaaLogMissing] = useState(false)

  const serviceDown = !healthLoading && health?.ok !== true
  const selectedAccount = useMemo(
    () => accounts.find((account) => account.id === selectedAccountId) ?? null,
    [accounts, selectedAccountId],
  )
  const runActive = runId !== null && !isTerminal(snapshot?.status)

  // 当前编辑作业的关卡：优先用 active key，找不到就退回第一关。
  const activeStage = useMemo(() => {
    if (selectedStages.length === 0) return null
    return selectedStages.find((item) => stageKey(item) === activeStageKey) ?? selectedStages[0]
  }, [selectedStages, activeStageKey])
  // 兼容原有用到 selectedStage 的地方（现在它是「当前关卡」）。
  const selectedStage = activeStage

  // 半自动队列的按钮可用性：队列优先，没有队列时退回单关执行。
  const canAddToQueue =
    !runActive && !serviceDown && !settings.manualMode && Boolean(selectedStage) && selectedIds.length > 0
  const canStart = !runActive && (queue.length > 0 ? !(settings.manualMode && queue.length > 1) : Boolean(selectedStage) && selectedIds.length > 0)
  const startLabel = queue.length > 0 ? `按队列执行（${queue.length} 关）` : '开始执行'

  // 设置里的客户端候选：账号表里出现过的 + 常见客户端 + 当前已选值。
  const clientOptions = useMemo(() => {
    const set = new Set<string>()
    for (const account of accounts) {
      if (account.client) set.add(account.client)
    }
    for (const client of ['Official', 'Bilibili', 'YoStarJP', 'YoStarEN', 'YoStarKR', 'txwy']) {
      set.add(client)
    }
    if (settings.client) set.add(settings.client)
    return [...set].sort()
  }, [accounts, settings.client])

  // ---- 服务健康 + 账号表 ----
  const reloadService = useCallback(async () => {
    setHealthLoading(true)
    setAccountsLoading(true)
    setHealthError('')
    setAccountsError('')

    try {
      const nextHealth = await api.health()
      setHealth(nextHealth)
      setMaaReady(nextHealth.maaReady)
      setHealthLoading(false)
    } catch (err) {
      const message = describeError(err)
      setHealth(null)
      setHealthError(message)
      setHealthLoading(false)
      setAccounts([])
      setAccountsError(message)
      setMaaError('服务未连接，读不到机队账号表。')
      setMaaReady(false)
      setAccountsLoading(false)
      return
    }

    try {
      const res = await api.accounts()
      setAccounts(res.accounts)
      setMaaReady(res.maaReady)
      setMaaError(res.maaError || '')
    } catch (err) {
      setAccounts([])
      setAccountsError(describeError(err))
    } finally {
      setAccountsLoading(false)
    }
  }, [])

  useEffect(() => {
    void reloadService()
  }, [reloadService])

  // 推关台账：进入页面时读一次，执行结束/手动刷新时再读。
  const reloadProgress = useCallback(async () => {
    setProgressLoading(true)
    setProgressError('')
    try {
      const res = await api.progress()
      setProgress(res.stages ?? [])
    } catch (err) {
      setProgress([])
      setProgressError(describeError(err))
    } finally {
      setProgressLoading(false)
    }
  }, [])

  useEffect(() => {
    void reloadProgress()
  }, [reloadProgress])

  // 账号列表刷新后保持原选择；没有选择或选择已失效时挑第一个启用的账号。
  useEffect(() => {
    if (accounts.length === 0) return
    setSelectedAccountId((prev) => {
      if (accounts.some((account) => account.id === prev)) return prev
      const firstEnabled = accounts.find((account) => account.enabled)
      return firstEnabled?.id ?? accounts[0].id
    })
  }, [accounts])

  // 设置写回 localStorage（隐私模式下写失败直接忽略）。
  useEffect(() => {
    try {
      window.localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings))
    } catch {
      // 忽略：只影响下次打开页面时的默认值
    }
  }, [settings])

  // 设置抽屉打开时支持 Esc 关闭。
  useEffect(() => {
    if (!settingsOpen) return
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setSettingsOpen(false)
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [settingsOpen])

  // MAA 日志：页面开着就每 2 秒拉一次 asst.log 尾巴（不依赖有没有正在跑的任务）。
  useEffect(() => {
    let cancelled = false
    const load = async () => {
      if (document.visibilityState === 'hidden') return
      try {
        const text = await api.maaLogTail()
        if (cancelled) return
        setMaaLogText(text)
        setMaaLogError('')
        setMaaLogMissing(false)
      } catch (err) {
        if (cancelled) return
        // 404 = asst.log 还没生成（第一次扫描/执行前），是正常状态，不当错误弹。
        if (err instanceof ApiError && err.status === 404) {
          setMaaLogMissing(true)
          setMaaLogError('')
        } else {
          setMaaLogMissing(false)
          setMaaLogError(describeError(err))
        }
      }
    }
    void load()
    const timer = window.setInterval(() => void load(), 2000)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [])

  // ---- 名册 ----

  const handleParseRoster = useCallback(async () => {
    const text = rosterText.trim()
    if (!text) {
      setRosterError('请先粘贴干员识别结果，或点「读取剪贴板」。')
      return
    }
    setRosterLoading(true)
    setRosterError('')
    try {
      const parsed = await api.parseBox(rosterText)
      setRoster(parsed)
      setPasteMessage('')
      if (search) setSearchStale(true)
    } catch (err) {
      setRosterError(describeError(err))
    } finally {
      setRosterLoading(false)
    }
  }, [rosterText, search])

  // 读该账号 MAA 目录里由「干员识别」落盘的 OperBoxData.json。
  const handleLoadMaa = useCallback(async () => {
    if (!selectedAccountId) {
      setRosterError('请先在第 1 步选择一个账号。')
      return
    }
    setMaaLoading(true)
    setRosterError('')
    setPasteMessage('')
    try {
      const parsed = await api.loadBoxFromMaa(selectedAccountId)
      setRoster(parsed)
      setRosterText('')
      setPasteMessage(
        `已读取 MAA 干员识别：${parsed.count} 人（source=${parsed.source || 'local'}）`,
      )
      if (search) setSearchStale(true)
    } catch (err) {
      setRosterError(describeError(err))
    } finally {
      setMaaLoading(false)
    }
  }, [selectedAccountId, search])

  // 用 maa-cli 跑 OperBox 任务；结果由服务端从 asst.log 回调里抓。
  const handleScanMaa = useCallback(async () => {
    if (!selectedAccountId) {
      setRosterError('请先在第 1 步选择一个账号。')
      return
    }
    setScanLoading(true)
    setRosterError('')
    setPasteMessage('正在让 MAA 开始唤醒并扫描干员，约 1–2 分钟…')
    try {
      const res = await api.scanBoxFromMaa(selectedAccountId)
      setRoster(res.roster)
      setRosterText('')
      setPasteMessage(`已扫描：${res.roster.count} 人（${res.path}）`)
      if (search) setSearchStale(true)
    } catch (err) {
      setRosterError(describeError(err))
      setPasteMessage('')
    } finally {
      setScanLoading(false)
    }
  }, [selectedAccountId, search])

  const handlePaste = useCallback(async () => {
    setPasteMessage('')
    const clipboard = navigator.clipboard
    if (!clipboard || typeof clipboard.readText !== 'function') {
      setPasteMessage('当前浏览器不支持读取剪贴板，请手动 Ctrl+V 粘贴到输入框。')
      return
    }
    try {
      const text = await clipboard.readText()
      if (!text.trim()) {
        setPasteMessage('剪贴板是空的。')
        return
      }
      setRosterText(text)
      setPasteMessage(`已读取 ${text.length} 个字符，确认无误后点「解析」。`)
    } catch (err) {
      setPasteMessage(`读取剪贴板失败：${describeError(err)}（浏览器可能拒绝权限，请手动粘贴）`)
    }
  }, [])

  // ---- 关卡（多选）----

  const handleResolveStages = useCallback(async () => {
    const raw = stageQuery.trim()
    if (!raw) {
      setStageError('请输入关卡代号，例如 TL-9、1-7、CE-6；多个用逗号/空格分隔。')
      return
    }
    setMultiSearch(null)
    setMultiSelected({})
    const queries = raw
      .split(/[\s,，;；]+/)
      .map((item) => item.trim())
      .filter(Boolean)
    setStageLoading(true)
    setStageError('')
    try {
      const merged: Stage[] = []
      const autoSelected: Stage[] = []
      const failed: string[] = []
      for (const query of queries) {
        try {
          const res = await api.resolveStages(query)
          if (res.stages.length === 0) {
            failed.push(query)
            continue
          }
          for (const stage of res.stages) {
            if (!merged.some((item) => stageKey(item) === stageKey(stage))) merged.push(stage)
          }
          const first = res.stages[0]
          if (!autoSelected.some((item) => stageKey(item) === stageKey(first))) autoSelected.push(first)
        } catch {
          failed.push(query)
        }
      }
      setStages(merged)
      if (merged.length === 0) {
        setSelectedStages([])
        setActiveStageKey('')
        setStageError('没有解析出候选关卡，换个代号试试（例如完整代号 TL-9 或活动代号 act55side）。')
        return
      }
      // 每个输入代号默认选中它自己的第一个候选，其余候选留着多选。
      setSelectedStages(autoSelected)
      setActiveStageKey(stageKey(autoSelected[0]))
      if (failed.length > 0) {
        setStageError(`这些代号没解析出来：${failed.join('、')}`)
      }
      setSearch(null)
      setSelectedIds([])
      setSearchError('')
      setSearchStale(false)
      setStartError('')
      setBatchNotice('')
    } catch (err) {
      setStageError(describeError(err))
    } finally {
      setStageLoading(false)
    }
  }, [stageQuery])

  // 多选候选关卡：点一张卡片切换选中/取消。
  const handleToggleStage = useCallback(
    (stage: Stage) => {
      const exists = selectedStages.some((item) => stageKey(item) === stageKey(stage))
      setMultiSearch(null)
      setMultiSelected({})
      const next = exists
        ? selectedStages.filter((item) => stageKey(item) !== stageKey(stage))
        : [...selectedStages, stage]
      setSelectedStages(next)
      setActiveStageKey((prev) => {
        if (!exists) return stageKey(stage)
        return prev === stageKey(stage) ? (next.length > 0 ? stageKey(next[0]) : '') : prev
      })
      setSearch(null)
      setSelectedIds([])
      setSearchError('')
      setSearchStale(false)
      setStartError('')
      setBatchNotice('')
    },
    [selectedStages],
  )

  const handleSelectAllStages = useCallback(() => {
    setSelectedStages(stages)
    if (stages.length > 0 && !stages.some((item) => stageKey(item) === activeStageKey)) {
      setActiveStageKey(stageKey(stages[0]))
    }
  }, [stages, activeStageKey])

  const handleClearStages = useCallback(() => {
    setSelectedStages([])
    setActiveStageKey('')
    setMultiSearch(null)
    setMultiSelected({})
    setSearch(null)
    setSelectedIds([])
    setSearchError('')
    setStartError('')
    setBatchNotice('')
  }, [])

  // 第 3 步里切换“当前编辑作业”的关卡。
  const handleSelectActiveStage = useCallback(
    (stage: Stage) => {
      if (activeStageKey === stageKey(stage)) return
      setActiveStageKey(stageKey(stage))
      setSearch(null)
      setSelectedIds([])
      setSearchError('')
      setSearchStale(false)
      setStartError('')
    },
    [activeStageKey],
  )

  // 点快捷关卡按钮：解析 → 选中 → 自动搜一次作业（一步到位）。
  const handleQuickStage = useCallback(async (code: string) => {
    const query = code.trim()
    if (!query) return
    setMultiSearch(null)
    setMultiSelected({})
    setStageQuery(query)
    setStageLoading(true)
    setStageError('')
    setSearch(null)
    setSelectedIds([])
    setSearchError('')
    setSearchStale(false)
    setStartError('')
    setBatchNotice('')
    try {
      const res = await api.resolveStages(query)
      if (res.stages.length === 0) {
        setStages([])
        setSelectedStages([])
        setActiveStageKey('')
        setStageError(`快捷关卡 ${query} 没有解析出候选，检查代号或改用手输。`)
        return
      }
      setStages(res.stages)
      const exact =
        res.stages.find((item) => item.code.toUpperCase() === query.toUpperCase()) ?? res.stages[0]
      setSelectedStages([exact])
      setActiveStageKey(stageKey(exact))
      setStageLoading(false)
      setSearchLoading(true)
      try {
        const jobs = await api.searchJobs({
          stageCode: exact.code,
          stageName: exact.stageName,
          limit: SEARCH_LIMIT,
          mode: jobMode,
        })
        setSearch(jobs)
        setSelectedIds(
          jobs.jobs.slice(0, Math.min(DEFAULT_PICK, jobs.jobs.length)).map((job) => job.id),
        )
      } catch (err) {
        setSearchError(describeError(err))
      } finally {
        setSearchLoading(false)
      }
    } catch (err) {
      setStageError(describeError(err))
    } finally {
      setStageLoading(false)
    }
  }, [jobMode])

  // 添加自定义关卡（去重；可一次贴多个代号）
  const handleAddCustomStages = useCallback((codes: string[]) => {
    const add = codes.map((code) => code.trim()).filter(Boolean)
    if (add.length === 0) return
    setCustomStages((prev) => {
      const next = prev.slice()
      for (const code of add) {
        if (!next.includes(code)) next.push(code)
      }
      return next
    })
  }, [])

  // 删除一个自定义关卡
  const handleDeleteCustomStage = useCallback((code: string) => {
    setCustomStages((prev) => prev.filter((item) => item !== code))
  }, [])

  // ---- 作业 ----

  /** 搜当前关卡的作业；mode 由难度过滤（'' 全部 / normal / raid）决定。 */
  const runJobSearch = useCallback(
    async (mode: JobMode) => {
      if (!selectedStage) {
        setSearchError('请先在第 2 步选中一个关卡。')
        return
      }
      setSearchLoading(true)
      setSearchError('')
      try {
        const res = await api.searchJobs({
          stageCode: selectedStage.code,
          stageName: selectedStage.stageName,
          limit: SEARCH_LIMIT,
          mode,
        })
        setSearch(res)
        // 默认勾选前几份，剩下的用户自己挑。
        setSelectedIds(
          res.jobs.slice(0, Math.min(DEFAULT_PICK, res.jobs.length)).map((job) => job.id),
        )
        setSearchStale(false)
        setStartError('')
      } catch (err) {
        setSearchError(describeError(err))
      } finally {
        setSearchLoading(false)
      }
    },
    [selectedStage],
  )

  const handleSearchJobs = useCallback(() => runJobSearch(jobMode), [runJobSearch, jobMode])

  /** 切难度过滤：立即按新难度重搜当前关卡（服务端 mode 参数），并清掉旧勾选。 */
  const handleJobModeChange = useCallback(
    (mode: JobMode) => {
      if (mode === jobMode) return
      setJobMode(mode)
      setSelectedIds([])
      setSearchStale(false)
      setStartError('')
      setMultiSearch(null)
      setMultiSelected({})
      if (!selectedStage) return
      void runJobSearch(mode)
    },
    [jobMode, runJobSearch, selectedStage],
  )

  const handleToggleJob = useCallback((id: number) => {
    setSelectedIds((prev) => (prev.includes(id) ? prev.filter((item) => item !== id) : [...prev, id]))
  }, [])

  const handleMoveJob = useCallback((id: number, direction: -1 | 1) => {
    setSelectedIds((prev) => {
      const index = prev.indexOf(id)
      if (index < 0) return prev
      const target = index + direction
      if (target < 0 || target >= prev.length) return prev
      const next = prev.slice()
      next[index] = prev[target]
      next[target] = id
      return next
    })
  }, [])

  // ---- 执行队列（半自动）----

  const handleAddToQueue = useCallback(() => {
    if (!selectedStage || selectedIds.length === 0) {
      setStartError('先选好关卡并至少勾选一份作业，再加入队列。')
      return
    }
    if (settings.manualMode) {
      setStartError('手动模式只支持单关，不能加入队列。')
      return
    }
    const raidJobIds = selectedIds.filter(
      (id) => isRaidJob(search?.jobs.find((job) => job.id === id)),
    )
    setQueue((prev) => [...prev, { stage: selectedStage, jobIds: selectedIds.slice(), raidJobIds }])
    // 加入队列后清空当前勾选，方便接着选下一关。
    setSelectedIds([])
    setStartError('')
  }, [selectedStage, selectedIds, settings.manualMode, search])

  // 批量入队：对第 2 步选中的每关搜一次作业，取前 3 份按顺序加入队列。
  const handleBatchQueue = useCallback(async () => {
    if (selectedStages.length === 0) {
      setStartError('先在第 2 步选中关卡。')
      return
    }
    if (settings.manualMode) {
      setStartError('手动模式只支持单关，不能批量入队。')
      return
    }
    setBatchQueueing(true)
    setBatchNotice('')
    setStartError('')
    const queuedKeys = new Set(queue.map((item) => stageKey(item.stage)))
    const added: string[] = []
    const skipped: string[] = []
    const failed: string[] = []
    try {
      for (const stage of selectedStages) {
        const key = stageKey(stage)
        if (queuedKeys.has(key)) {
          skipped.push(stage.code)
          continue
        }
        try {
          const res = await api.searchJobs({
            stageCode: stage.code,
            stageName: stage.stageName,
            limit: SEARCH_LIMIT,
            mode: jobMode,
          })
          const jobIds = res.jobs
            .slice(0, Math.min(DEFAULT_PICK, res.jobs.length))
            .map((job) => job.id)
          if (jobIds.length === 0) {
            failed.push(`${stage.code}（没有可用作业）`)
            continue
          }
          const raidJobIds = res.jobs
            .filter((job) => jobIds.includes(job.id) && isRaidJob(job))
            .map((job) => job.id)
          queuedKeys.add(key)
          setQueue((prev) => [...prev, { stage, jobIds, raidJobIds }])
          added.push(stage.code)
        } catch (err) {
          failed.push(`${stage.code}（${describeError(err)}）`)
        }
      }
      const parts = [`批量入队：新增 ${added.length} 关（每关前 ${DEFAULT_PICK} 份）`]
      if (skipped.length > 0) parts.push(`跳过已在队列的 ${skipped.length} 关（${skipped.join('、')}）`)
      if (failed.length > 0) parts.push(`失败：${failed.join('、')}`)
      setBatchNotice(parts.join('；'))
    } finally {
      setBatchQueueing(false)
    }
  }, [selectedStages, queue, settings.manualMode, jobMode])

  // 批量搜索：对第 2 步选中的每关都搜一次，默认每关勾前 3 份。
  const handleSearchAllStages = useCallback(async () => {
    if (selectedStages.length === 0) {
      setMultiError('先在第 2 步选中关卡。')
      return
    }
    setMultiLoading(true)
    setMultiError('')
    setMultiSearch(null)
    setMultiSelected({})
    const out: { stage: Stage; result: JobSearch }[] = []
    const picked: Record<string, number[]> = {}
    const failed: string[] = []
    for (const stage of selectedStages.slice(0, 12)) {
      try {
        const res = await api.searchJobs({
          stageCode: stage.code,
          stageName: stage.stageName,
          limit: SEARCH_LIMIT,
          mode: jobMode,
        })
        out.push({ stage, result: res })
        picked[stageKey(stage)] = res.jobs
          .slice(0, Math.min(DEFAULT_PICK, res.jobs.length))
          .map((job) => job.id)
      } catch (err) {
        failed.push(`${stage.code}：${describeError(err)}`)
      }
    }
    setMultiSearch(out)
    setMultiSelected(picked)
    if (failed.length > 0) setMultiError(failed.join('；'))
    setMultiLoading(false)
  }, [selectedStages, jobMode])

  const handleToggleMultiJob = useCallback((key: string, id: number) => {
    setMultiSelected((prev) => {
      const cur = prev[key] ?? []
      return {
        ...prev,
        [key]: cur.includes(id) ? cur.filter((item) => item !== id) : [...cur, id],
      }
    })
  }, [])

  const handleAddMultiToQueue = useCallback(() => {
    if (!multiSearch) return
    const queuedKeys = new Set(queue.map((item) => stageKey(item.stage)))
    let added = 0
    let skipped = 0
    setQueue((prev) => {
      const next = prev.slice()
      for (const group of multiSearch) {
        const key = stageKey(group.stage)
        const ids = multiSelected[key] ?? []
        if (ids.length === 0) continue
        if (queuedKeys.has(key)) {
          skipped += 1
          continue
        }
        const raidJobIds = ids.filter(
          (id) => isRaidJob(group.result.jobs.find((job) => job.id === id)),
        )
        next.push({ stage: group.stage, jobIds: ids, raidJobIds })
        queuedKeys.add(key)
        added += 1
      }
      return next
    })
    setBatchNotice(
      `批量入队：新增 ${added} 关${skipped > 0 ? `，跳过已在队列 ${skipped} 关` : ''}。`,
    )
  }, [multiSearch, multiSelected, queue])

  const handleClearMulti = useCallback(() => {
    setMultiSearch(null)
    setMultiSelected({})
    setMultiError('')
  }, [])

  const handleRemoveQueue = useCallback((index: number) => {
    setQueue((prev) => prev.filter((_, i) => i !== index))
  }, [])

  const handleMoveQueue = useCallback((index: number, direction: -1 | 1) => {
    setQueue((prev) => {
      const target = index + direction
      if (target < 0 || target >= prev.length) return prev
      const next = prev.slice()
      const item = next[index]
      next[index] = next[target]
      next[target] = item
      return next
    })
  }, [])

  const handleClearQueue = useCallback(() => setQueue([]), [])

  // ---- 执行 ----

  const handleStart = useCallback(async () => {
    if (!selectedAccount) {
      setStartError('请先在第 1 步选择一个账号。')
      return
    }
    if (queue.length === 0) {
      if (!selectedStage) {
        setStartError('请先在第 2 步选中一个关卡，或先把它加入执行队列。')
        return
      }
      if (selectedIds.length === 0) {
        setStartError('至少勾选一份作业，或先把本关加入执行队列。')
        return
      }
    }
    if (settings.manualMode && queue.length > 1) {
      setStartError('手动模式只支持单关，请清空队列或关闭手动模式。')
      return
    }
    if (runActive) {
      setStartError('已有执行中的任务，请先停止或等待结束。')
      return
    }

    // 队列优先；队列为空时退回“当前关卡 + 当前勾选作业”的单关执行。
    const singleRaidIds = selectedIds.filter(
      (id) => isRaidJob(search?.jobs.find((job) => job.id === id)),
    )
    const stages: StagePlan[] =
      queue.length > 0
        ? queue.map((item) => ({
            stageCode: item.stage.code,
            stageName: item.stage.stageName,
            jobIds: item.jobIds,
            raidJobIds: item.raidJobIds ?? [],
          }))
        : [
            {
              stageCode: selectedStage?.code ?? '',
              stageName: selectedStage?.stageName ?? '',
              jobIds: selectedIds,
              raidJobIds: singleRaidIds,
            },
          ]
    const first = stages[0]

    const options: RunOptions = {
      accountId: selectedAccount.id,
      client: settings.client || selectedAccount.client || '',
      stageCode: first.stageCode,
      stageName: first.stageName,
      jobIds: first.jobIds,
      stages,
      formation: settings.formation,
      addTrust: settings.addTrust,
      ignoreRequirements: settings.ignoreRequirements,
      manualMode: settings.manualMode,
      // 每关实际尝试份数由服务端按该关作业数截断。
      maxAttempts: settings.maxAttempts,
      supportUnitUsage: settings.useSupport ? 1 : 0,
      timeoutMin: settings.timeoutMin,
    }

    setStarting(true)
    setStartError('')
    try {
      const res = await api.startRun(options)
      setRunId(res.runId)
    } catch (err) {
      setStartError(describeError(err))
    } finally {
      setStarting(false)
    }
  }, [selectedAccount, selectedStage, selectedIds, queue, settings, runActive])

  // 启动账号对应的模拟器实例（跨服用：PC 客户端点一下就让机端 MuMu 拉起来）。
  const handleStartEmu = useCallback(async () => {
    if (!selectedAccount) return
    setEmuStarting(true)
    setEmuNotice('')
    try {
      const res = await api.startEmu(selectedAccount.id)
      if (res.ok) {
        setEmuNotice(`模拟器 ${res.emu ?? ''}${res.already ? ' 已经在运行' : ' 已启动'}`.trim())
        void reloadService()
      } else {
        setEmuNotice(res.error || '模拟器启动失败')
      }
    } catch (err) {
      setEmuNotice(describeError(err))
    } finally {
      setEmuStarting(false)
    }
  }, [selectedAccount, reloadService])

  const handleStop = useCallback(async () => {
    const id = runId
    if (!id) return
    setStopping(true)
    setRunError('')
    try {
      await api.stopRun(id)
    } catch (err) {
      setRunError(describeError(err))
    } finally {
      setStopping(false)
    }
  }, [runId])

  // 页面刷新/重开客户端后，如果服务端还有没结束的 run，把它接回来（否则停止按钮就没了）。
  useEffect(() => {
    let cancelled = false
    void (async () => {
      try {
        const res = await api.activeRun()
        if (!cancelled && res.run) setRunId(res.run.id)
      } catch {
        // 服务还没起好时忽略
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  // 结束当前任务并清理残留 maa-cli 进程（页面丢了 runId 时的兜底）。
  const handleKillMaa = useCallback(async () => {
    setKillMaaLoading(true)
    setKillMaaNotice('')
    try {
      const res = await api.killMaa()
      setKillMaaNotice(
        res.pids.length > 0
          ? `已结束 ${res.pids.length} 个 MAA 进程（PID ${res.pids.join(', ')}）`
          : '没有发现残留的 maa-cli 进程',
      )
    } catch (err) {
      setKillMaaNotice(describeError(err))
    } finally {
      setKillMaaLoading(false)
    }
  }, [])

  // 把快照/SSE 里的事件合并进日志：按 seq 去重，保证快照轮询和 SSE 双通道不重复。
  const pushEvents = useCallback((incoming: RunEvent[]) => {
    if (incoming.length === 0) return
    setEvents((prev) => {
      const seen = seenEventsRef.current
      const added: RunEvent[] = []
      for (const event of incoming) {
        if (!event || typeof event.seq !== 'number' || seen.has(event.seq)) continue
        seen.add(event.seq)
        added.push(event)
      }
      if (added.length === 0) return prev
      const next = prev.concat(added).sort((a, b) => a.seq - b.seq)
      return next.length > MAX_LOG_LINES ? next.slice(next.length - MAX_LOG_LINES) : next
    })
  }, [])

  // 订阅当前 run：快照轮询兜底状态与截图，SSE 推增量日志；结束时停轮询。
  useEffect(() => {
    if (!runId) return
    const id = runId

    seenEventsRef.current = new Set()
    setEvents([])
    setSnapshot(null)
    setRunError('')
    setStreamError('')
    setShotTs(0)
    setShotFailed(false)

    let cancelled = false
    let pollTimer: number | undefined

    const applySnapshot = (snap: RunSnapshot) => {
      if (cancelled) return
      setSnapshot(snap)
      if (snap.events && snap.events.length > 0) pushEvents(snap.events)
      if (snap.hasShot) {
        setShotTs(Date.now())
        // 上一轮图片加载失败的话，下一轮快照回来时重新尝试。
        setShotFailed(false)
      }
      if (isTerminal(snap.status) && pollTimer !== undefined) {
        window.clearInterval(pollTimer)
        pollTimer = undefined
      }
    }

    const poll = async () => {
      try {
        const snap = await api.getRun(id)
        applySnapshot(snap)
      } catch (err) {
        if (!cancelled) setRunError(describeError(err))
      }
    }

    void poll()
    pollTimer = window.setInterval(() => {
      void poll()
    }, POLL_INTERVAL_MS)

    const unsubscribe = api.subscribeRunEvents(id, {
      onEvent: (event) => pushEvents([event]),
      onDone: () => {
        // 事件流结束：立刻补一次快照，把最终状态/错误收回来；台账也可能更新了。
        void poll()
        void reloadProgress()
      },
      onError: (message) => {
        if (!cancelled) setStreamError(message)
      },
    })

    return () => {
      cancelled = true
      if (pollTimer !== undefined) window.clearInterval(pollTimer)
      unsubscribe()
    }
  }, [runId, pushEvents, reloadProgress])

  // 设置抽屉打开时锁住页面滚动，关闭后恢复。
  useEffect(() => {
    if (!settingsOpen) return
    const previous = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      document.body.style.overflow = previous
    }
  }, [settingsOpen])

  const updateSettings = useCallback((patch: Partial<Settings>) => {
    setSettings((prev) => ({ ...prev, ...patch }))
  }, [])

  const selectedStageText = selectedStage
    ? `${selectedStage.code}${selectedStage.stageName ? ` → ${selectedStage.stageName}` : ''}`
    : ''
  return (
    <div className="app-shell">
      <div className="bg-grid" aria-hidden="true" />

      <PiProjectsPanel />
      <TopBar
        health={health}
        healthLoading={healthLoading}
        maaReady={maaReady}
        onRefresh={() => void reloadService()}
        onOpenSettings={() => setSettingsOpen(true)}
      />

      <ServiceBanner
        down={serviceDown}
        healthError={healthError}
        maaReady={maaReady}
        maaError={maaError}
        retryLoading={healthLoading || accountsLoading}
        onRetry={() => void reloadService()}
      />

      <main className="app-main">
        <div className="workspace">
          <div className="ws-col ws-left">
          <StepCard
            step="01"
            title="账号与名册"
            hint="选机队账号，粘贴或读取 MAA 干员识别结果"
            aside={
              <span className={`step-flag ${selectedAccount ? 'is-ok' : ''}`}>
                {selectedAccount
                  ? `${selectedAccount.name}${selectedAccount.client ? ` · ${clientLabel(selectedAccount.client)}` : ''}`
                  : '未选账号'}
              </span>
            }
          >
            <AccountStep
              accounts={accounts}
              selectedId={selectedAccountId}
              onSelect={setSelectedAccountId}
              loading={accountsLoading}
              error={accountsError}
              onReload={() => void reloadService()}
              onStartEmu={() => void handleStartEmu()}
              emuStarting={emuStarting}
              emuNotice={emuNotice}
              disabled={serviceDown}
              rosterText={rosterText}
              onRosterText={setRosterText}
              roster={roster}
              rosterLoading={rosterLoading}
              rosterError={rosterError}
              onParse={() => void handleParseRoster()}
              onLoadMaa={() => void handleLoadMaa()}
              maaLoading={maaLoading}
              onScanMaa={() => void handleScanMaa()}
              scanLoading={scanLoading}
              onPaste={() => void handlePaste()}
              pasteMessage={pasteMessage}
            />
          </StepCard>

          <StepCard
            step="02"
            title="关卡"
            hint="解析游戏内代号（可多个），多选要打的关卡"
            aside={
              selectedStages.length > 0 ? (
                <span className="step-flag mono is-ok" title={selectedStageText}>
                  已选 {selectedStages.length} 关
                </span>
              ) : (
                <span className="step-flag">未选关卡</span>
              )
            }
          >
            <StageStep
              query={stageQuery}
              onQuery={setStageQuery}
              onResolve={() => void handleResolveStages()}
              loading={stageLoading}
              error={stageError}
              stages={stages}
              selected={selectedStages}
              onToggle={handleToggleStage}
              onSelectAll={handleSelectAllStages}
              onClear={handleClearStages}
              disabled={serviceDown}
              customStages={customStages}
              onQuickStage={(code) => void handleQuickStage(code)}
              onAddCustomStages={handleAddCustomStages}
              onDeleteCustomStage={handleDeleteCustomStage}
            />
          </StepCard>
          </div>

          <div className="ws-col ws-mid">
          <StepCard
            step="03"
            title="作业与执行"
            hint="搜索作业、调整尝试顺序、开始执行"
            aside={<span className="step-flag">已选 {selectedIds.length} 份</span>}
          >
            <JobsStep
              stage={selectedStage}
              stages={selectedStages}
              onSelectActive={handleSelectActiveStage}
              multiSearch={multiSearch}
              multiSelected={multiSelected}
              multiLoading={multiLoading}
              multiError={multiError}
              onSearchAll={() => void handleSearchAllStages()}
              onToggleMulti={handleToggleMultiJob}
              onAddMultiQueue={handleAddMultiToQueue}
              onClearMulti={handleClearMulti}
              rosterParsed={roster !== null}
              search={search}
              loading={searchLoading}
              error={searchError}
              stale={searchStale}
              onSearch={() => void handleSearchJobs()}
              selectedIds={selectedIds}
              onToggle={handleToggleJob}
              onMove={handleMoveJob}
              onStart={() => void handleStart()}
              onAddToQueue={handleAddToQueue}
              onBatchQueue={() => void handleBatchQueue()}
              batchQueueing={batchQueueing}
              batchNotice={batchNotice}
              canQueue={canAddToQueue}
              canStart={canStart}
              queueCount={queue.length}
              startLabel={startLabel}
              starting={starting}
              startError={startError}
              disabled={serviceDown || runActive || searchLoading}
              manualMode={settings.manualMode}
              mode={jobMode}
              onModeChange={handleJobModeChange}
            />
          </StepCard>
          </div>

          <div className="ws-col ws-right">
          <StepCard
            step="04"
            title="执行队列（半自动）"
            hint="人工选好每一关和候选作业，程序按顺序逐关执行；每关失败自动换下一份"
            aside={
              <span className={`step-flag ${queue.length > 0 ? 'is-ok' : ''}`}>
                {queue.length > 0 ? `${queue.length} 关` : '空'}
              </span>
            }
          >
            <QueuePanel
              queue={queue}
              disabled={serviceDown || runActive}
              onRemove={handleRemoveQueue}
              onMove={handleMoveQueue}
              onClear={handleClearQueue}
            />
          </StepCard>

          <StepCard
            step="05"
            title="推关台账"
            hint="执行成功后按「关卡 + 难度」记星；普通 3 星才解锁突袭"
            aside={
              <span className={`step-flag ${progress.length > 0 ? 'is-ok' : ''}`}>
                {progress.length > 0 ? `${progress.length} 条` : '空'}
              </span>
            }
          >
            <ProgressPanel
              items={progress}
              loading={progressLoading}
              error={progressError}
              onRefresh={() => void reloadProgress()}
              disabled={serviceDown}
            />
          </StepCard>

          <aside className="run-panel" aria-label="执行面板">
          <RunPanel
            runId={runId}
            snapshot={snapshot}
            events={events}
            runError={runError}
            streamError={streamError}
            shotTs={shotTs}
            shotFailed={shotFailed}
            stopping={stopping}
            maaLogText={maaLogText}
            maaLogError={maaLogError}
            maaLogMissing={maaLogMissing}
            killMaaLoading={killMaaLoading}
            killMaaNotice={killMaaNotice}
            onStop={() => void handleStop()}
            onShotError={() => setShotFailed(true)}
            onKillMaa={() => void handleKillMaa()}
          />
          </aside>
        </div>
        </div>
      </main>

      <SettingsDrawer
        open={settingsOpen}
        settings={settings}
        clients={clientOptions}
        accountClient={selectedAccount?.client ?? ''}
        onClose={() => setSettingsOpen(false)}
        onChange={updateSettings}
      />
    </div>
  )
}
