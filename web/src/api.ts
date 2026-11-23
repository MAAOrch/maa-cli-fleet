// 类型化 fetch 封装：统一中文错误信息 + SSE 订阅。
import type {
  AccountsResponse,
  Health,
  JobSearch,
  JobSearchRequest,
  NavPack,
  Roster,
  RunEvent,
  RunOptions,
  RunSnapshot,
  Stage,
  StageProgress,
} from './types'

const JSON_HEADERS: Record<string, string> = { 'Content-Type': 'application/json' }

/** 带 HTTP 状态码的请求错误；中文 message 直接给用户看。 */
export class ApiError extends Error {
  readonly status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

/** 把任意异常转成可展示的中文提示。 */
export function describeError(err: unknown): string {
  if (err instanceof ApiError) return err.message
  if (err instanceof Error && err.message) return err.message
  return '未知错误，请查看 copilot-picker 服务端日志。'
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response
  try {
    res = await fetch(path, init)
  } catch {
    throw new ApiError(
      '无法连接 copilot-picker 服务。请确认服务已启动，或 SSH 隧道已建立。',
      0,
    )
  }

  const raw = await res.text()
  let payload: unknown = null
  if (raw) {
    try {
      payload = JSON.parse(raw) as unknown
    } catch {
      payload = null
    }
  }

  if (!res.ok) {
    const serverError = isRecord(payload) ? payload['error'] : undefined
    const message =
      typeof serverError === 'string' && serverError.trim()
        ? serverError
        : `请求失败（HTTP ${res.status}）`
    throw new ApiError(message, res.status)
  }

  return payload as T
}

function post<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, {
    method: 'POST',
    headers: JSON_HEADERS,
    body: JSON.stringify(body),
  })
}

export interface RunEventHandlers {
  onEvent: (event: RunEvent) => void
  /** 服务端发来 `event: done`（执行结束） */
  onDone: () => void
  /** 事件流断开；快照轮询仍可兜底 */
  onError: (message: string) => void
}

/**
 * 订阅 GET /api/run/{id}/events（SSE，`data: <RunEvent JSON>`）。
 * 返回取消订阅函数，组件卸载/切换 run 时必须调用。
 */
export function subscribeRunEvents(runId: string, handlers: RunEventHandlers): () => void {
  const source = new EventSource(`/api/run/${encodeURIComponent(runId)}/events`)
  let closed = false

  const close = () => {
    if (closed) return
    closed = true
    source.close()
  }

  source.onmessage = (message: MessageEvent<string>) => {
    if (closed || !message.data) return
    try {
      const event = JSON.parse(message.data) as RunEvent
      handlers.onEvent(event)
    } catch {
      // 忽略坏帧，不影响后续事件
    }
  }

  source.addEventListener('done', () => {
    if (closed) return
    close()
    handlers.onDone()
  })

  source.onerror = () => {
    if (closed) return
    // EventSource 会自动重连；只有彻底关闭才认为事件流结束。
    if (source.readyState === EventSource.CLOSED) {
      close()
      handlers.onError('事件流已断开，正在用状态快照兜底。')
    }
  }

  return close
}

export const api = {
  health: () => request<Health>('/api/health'),

  accounts: () => request<AccountsResponse>('/api/accounts'),

  parseBox: (text: string) => post<Roster>('/api/box', { text }),

  /** 读该账号对应 MAA 目录里由「干员识别」落盘的 data/OperBoxData.json。 */
  loadBoxFromMaa: (accountId: string) => post<Roster>('/api/box/maa', { accountId }),

  /** 用 maa-cli 跑一次 OperBox 任务（干员识别），从日志抓结果。可能要一两分钟。 */
  scanBoxFromMaa: (accountId: string) =>
    post<{ roster: Roster; path: string }>('/api/box/maa/scan', { accountId }),

  resolveStages: (query: string) => post<{ stages: Stage[] }>('/api/stages/resolve', { query }),

  /** MAA stages.json 分好组的快捷关卡清单。 */
  stageCatalog: () => request<{ groups: { name: string; stages: string[] }[] }>('/api/stages/catalog'),

  /** 启动账号对应的模拟器实例（MuMuManager）。 */
  startEmu: (accountId: string) =>
    post<{ ok: boolean; already?: boolean; started?: boolean; emu?: string; error?: string }>(
      '/api/emu/start',
      { accountId },
    ),

  /** 关闭账号对应的模拟器实例。 */
  stopEmu: (accountId: string) => post<{ ok: boolean; emu?: string }>('/api/emu/stop', { accountId }),

  /** 服务端保存的界面状态（刷新/重开客户端后恢复）。 */
  uiState: () => request<{ state: unknown }>('/api/ui-state'),

  /** 覆盖保存界面状态。 */
  saveUiState: (state: unknown) =>
    request<{ ok: boolean }>('/api/ui-state', {
      method: 'PUT',
      headers: JSON_HEADERS,
      body: JSON.stringify(state),
    }),

  searchJobs: (body: JobSearchRequest) => post<JobSearch>('/api/jobs/search', body),

  /** MAA 的 asst.log 实时尾巴（纯文本，最多约 256KB）。 */
  maaLogTail: async (): Promise<string> => {
    let res: Response
    try {
      res = await fetch('/api/log/maa', { cache: 'no-store' })
    } catch {
      throw new ApiError('无法连接 copilot-picker 服务（127.0.0.1:8787）。', 0)
    }
    const raw = await res.text()
    if (!res.ok) {
      let message = `请求失败（HTTP ${res.status}）`
      try {
        const payload = JSON.parse(raw) as { error?: unknown }
        if (typeof payload.error === 'string' && payload.error.trim()) message = payload.error
      } catch {
        // 非 JSON，保留默认错误
      }
      throw new ApiError(message, res.status)
    }
    return raw
  },

  startRun: (options: RunOptions) => post<{ runId: string }>('/api/run/start', options),

  /** 一键全自动推活动：自动扫箱 + 挑作业 + 按阶段（普通/EX/S）分流启动。 */
  startAutoEvent: (body: {
    accountId: string
    stageCodes: string[]
    phases?: string[]
    phaseNavTasks?: Record<string, string>
    autoScanBox?: boolean
    includeCleared?: boolean
    useSupport?: boolean
    maxAttempts?: number
    timeoutMin?: number
    piProject?: string
  }) =>
    post<{
      runId: string
      accountId: string
      stageCount: number
      phases: Record<string, number>
      scannedBox: boolean
      rosterSize: number
      skipped?: string[]
    }>('/api/auto-event/start', body),

  /** 列出已导入的可视化导航包（MPE / MAA 导出的 .json 或 .zip）。 */
  navPacks: () => request<{ packs: NavPack[] }>('/api/nav/packs'),

  /** 导入 MAA 可视化编辑器 / MPE 导出的导航文件。 */
  importNavPack: async (file: File, name: string, phase: string): Promise<{ ok: boolean; pack: NavPack }> => {
    const fd = new FormData()
    fd.append('file', file)
    if (name.trim()) fd.append('name', name.trim())
    if (phase) fd.append('phase', phase)
    return request<{ ok: boolean; pack: NavPack }>('/api/nav/import', {
      method: 'POST',
      body: fd,
    })
  },

  deleteNavPack: (id: string) =>
    request<{ ok: boolean; id: string }>(`/api/nav/packs/${encodeURIComponent(id)}`, {
      method: 'DELETE',
    }),

  /** 立即调用 MaaFramework（MaaPiCli）在指定模拟器上执行一条导航任务（用于实测导入的导航）。 */
  runNavTask: (accountId: string, taskName: string) =>
    post<{ ok: boolean; accountId: string; taskName: string; output?: string }>('/api/nav/run', {
      accountId,
      taskName,
    }),

  /** 推关台账（哪关/难度/几星），空数组 = 还没打过。 */
  progress: () => request<{ stages: StageProgress[]; count: number }>('/api/progress'),

  activeRun: () => request<{ run: RunSnapshot | null }>('/api/run/active'),

  /** 所有并发运行中的模拟器任务快照（多模拟器并发视图）。 */
  activeRuns: () => request<{ runs: RunSnapshot[] }>('/api/runs/active'),

  /** 不依赖 runId，直接停掉当前正在跑的 run。 */
  stopActiveRun: () => post<{ ok: boolean; stopped: boolean }>('/api/run/stop', {}),

  /** 停掉当前 run 并清理本服务启动的残留 maa-cli 进程。 */
  killMaa: () => post<{ ok: boolean; pids: number[] }>('/api/maa/kill', {}),

  getRun: (runId: string) => request<RunSnapshot>(`/api/run/${encodeURIComponent(runId)}`),

  stopRun: (runId: string) => post<{ ok: boolean }>(`/api/run/${encodeURIComponent(runId)}/stop`, {}),

  shotUrl: (runId: string, ts: number) =>
    `/api/run/${encodeURIComponent(runId)}/shot?ts=${ts}`,

  subscribeRunEvents,
}
