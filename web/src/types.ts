// 与 internal/copilotpick/model/model.go 对齐：JSON tag 就是 HTTP 契约。
// 改字段前先看 docs/COPILOT-PICKER-CONTRACT.md 与 model.go。

export interface Account {
  id: string
  name: string
  client: string
  emu: string
  daily?: string
  enabled: boolean
  adb: string
  online: boolean
}

export interface RosterEntry {
  name: string
  operId?: string
  rarity: number
  elite: number
  level: number
  potential: number
  /** 主技能等级（1~7），只有带技能数据的名册才有 */
  skillLevel?: number
  /** 各技能专精等级（0~3），序号即技能位 */
  skills?: number[]
  /** 模组槽位 → 等级（1~3） */
  modules?: Record<number, number>
  /** false = 数据源不含技能/模组（MAA 本地识别、剪贴板），匹配时这两维按已拥有处理 */
  skillKnown: boolean
}

export interface Roster {
  entries: RosterEntry[]
  count: number
  six: number
  five: number
  /** operbox|clipboard|table|names */
  format: string
  source?: string
  syncedAt?: string
  skillKnown: boolean
  warnings?: string[]
}

export interface Stage {
  code: string
  stageName: string
  title?: string
  source?: string
}

export interface Job {
  id: number
  title: string
  stageName: string
  /** 1=普通 2=突袭 3=磨难 0=未标注 */
  difficulty: number
  rating: number
  views: number
  minimumRequired?: string
  uploadTime?: string
  details?: string
  opers: string[]
  missing?: string[]
  levelShort?: string[]
  /** 出场干员数 + 干员组数 */
  total: number
  met: boolean
  raritySum: number
  /** ready|borrow|train|blocked */
  mode?: string
  /** 作业没写练度、按星级补默认要求的条目数 */
  impliedMissing?: number
  /** 名册缺技能数据，技能/模组按已拥有处理 */
  requirementFuzz?: boolean
  /** 跨号共享评分库（job_scores.json）综合分 */
  localScore?: number
  /** 跨号实测 3★/通关次数 */
  localPass?: number
  /** 跨号实测失败/低星次数 */
  localFail?: number
}

export interface JobSearch {
  stage: Stage
  jobs: Job[]
  blocked?: Job[]
  /** 干员都有、但练度不够的作业（不能直接抄） */
  train?: Job[]
  /** 该关卡在作业站上的作业总数 */
  total: number
  /** 本次实际拉取并解析的作业数 */
  fetched: number
}

/** 半自动多关队列里的一关：人工选好关卡与该关候选作业顺序。 */
export interface StagePlan {
  /** 导航用显示名，如 TL-9（可为空） */
  stageCode: string
  /** 作业站关卡代码，如 act55side_09 */
  stageName: string
  /** 所属活动地图页：main | ex | s */
  phase?: string
  jobIds: number[]
  /** difficulty=2/3（突袭/磨难）的作业：执行时按突袭（is_raid=true） */
  raidJobIds?: number[]
}

export interface RunOptions {
  accountId: string
  client: string
  /** 导航用显示名，如 TL-9 */
  stageCode: string
  /** 作业站关卡代码，如 act55side_09 */
  stageName: string
  /** 按用户排序的顺序尝试 */
  jobIds: number[]
  /** 多关队列；不传则服务端用上面的单关字段补一条 */
  stages?: StagePlan[]
  formation: boolean
  addTrust: boolean
  ignoreRequirements: boolean
  manualMode: boolean
  onMap?: boolean
  /** 阶段分流模式：按 main → ex → s 自动分组，切阶段时才做页面级 PI 导航 */
  phaseNav?: boolean
  piProject?: string
  maxAttempts: number
  supportUnitUsage: number
  timeoutMin: number
}

export type RunStatus = 'idle' | 'starting' | 'navigating' | 'running' | 'success' | 'failed' | 'stopped'

export type EventLevel = 'info' | 'warn' | 'error' | 'stage'
export type EventKind = 'log' | 'status' | 'attempt' | 'result'

export interface RunEvent {
  seq: number
  at: string
  level: EventLevel
  kind: EventKind
  attempt?: number
  jobId?: number
  message: string
}

export interface RunSnapshot {
  id: string
  status: RunStatus
  accountId: string
  accountName?: string
  client: string
  stageCode: string
  stageName: string
  /** 当前阶段：main | ex | s */
  phase?: string
  /** 当前是队列里的第几关（1 起） */
  stageIndex?: number
  /** 队列总关数（1 = 单关） */
  stageTotal?: number
  attempt: number
  total: number
  currentJobId?: number
  currentJobTitle?: string
  startedAt?: string
  finishedAt?: string
  error?: string
  events?: RunEvent[]
  hasShot: boolean
}

/** 推关台账（GET /api/progress，落盘 <root>/progress.json）。 */
export interface StageProgress {
  stageCode: string
  /** normal | raid */
  mode: string
  /** 0..3；突袭不数星（记 3 = 已通关） */
  stars: number
  clearedAt?: string
}

export interface Health {
  ok: boolean
  version: string
  root: string
  maaReady: boolean
  maaExe: string
  accounts: number
  listen: string
  prtsBase: string
}

export interface NavPack {
  id: string
  name: string
  phase: string
  taskName: string
  entry: string
  nodes: string[]
  images: number
  createdAt: string
}

export interface AccountsResponse {
  accounts: Account[]
  maaReady: boolean
  maaError: string
}

// ---------------------------------------------------------------------------
// 以下是前端本地设置（不进 model，进 RunOptions）
// ---------------------------------------------------------------------------

export interface Settings {
  /** '' = 跟随账号的 client */
  client: string
  formation: boolean
  addTrust: boolean
  ignoreRequirements: boolean
  maxAttempts: number
  timeoutMin: number
  manualMode: boolean
  /** 缺 1 个干员时借用助战（MAA --support-unit-usage 1） */
  useSupport: boolean
}

export const DEFAULT_SETTINGS: Settings = {
  client: '',
  formation: true,
  addTrust: true,
  ignoreRequirements: true,
  maxAttempts: 5,
  timeoutMin: 20,
  manualMode: false,
  useSupport: true,
}

/** POST /api/jobs/search 的请求体（服务端私有结构，见契约第 5 条）。 */
/** 作业难度过滤：'' = 不过滤，normal/raid 交给服务端按作业标题过滤。 */
export type JobMode = '' | 'normal' | 'raid'

export interface JobSearchRequest {
  query?: string
  stageCode?: string
  stageName?: string
  roster?: RosterEntry[] | null
  rosterText?: string
  limit?: number
  /** 只返回普通/突袭作业（服务端 filterJobsByMode） */
  mode?: JobMode
}
