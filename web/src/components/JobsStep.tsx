// Step 3：搜索作业 → 勾选/排序 → 开始执行。
import type { Job, JobMode, JobSearch, Stage } from '../types'
import { EmptyHint, Notice, Spinner } from './Feedback'
import { Icon } from './icons'
import { DIFFICULTY, JobCard } from './JobCard'

export interface JobsStepProps {
  /** 当前正在编辑作业的关卡 */
  stage: Stage | null
  /** 第 2 步选中的所有关卡（用于切换当前关卡/批量入队） */
  stages: Stage[]
  onSelectActive: (stage: Stage) => void
  /** 批量搜索：所有选中关卡的作业结果（按关卡分组） */
  multiSearch: { stage: Stage; result: JobSearch }[] | null
  multiSelected: Record<string, number[]>
  multiLoading: boolean
  multiError: string
  onSearchAll: () => void
  onToggleMulti: (stageKey: string, id: number) => void
  onAddMultiQueue: () => void
  onClearMulti: () => void
  rosterParsed: boolean
  search: JobSearch | null
  loading: boolean
  error: string
  stale: boolean
  onSearch: () => void
  selectedIds: number[]
  onToggle: (id: number) => void
  onMove: (id: number, direction: -1 | 1) => void
  onStart: () => void
  onAddToQueue: () => void
  onBatchQueue: () => void
  batchQueueing: boolean
  batchNotice: string
  canQueue: boolean
  canStart: boolean
  queueCount: number
  startLabel: string
  starting: boolean
  startError: string
  disabled: boolean
  manualMode: boolean
  /** 难度过滤：'' 全部 / normal 普通 / raid 突袭（服务端 mode 参数） */
  mode: JobMode
  onModeChange: (mode: JobMode) => void
}

function sameStage(a: Stage, b: Stage): boolean {
  return a.code === b.code && a.stageName === b.stageName
}

function BlockedJobs({ jobs }: { jobs: Job[] }) {
  if (jobs.length === 0) return null
  return (
    <details className="blocked-box">
      <summary>
        被过滤的作业 {jobs.length} 份（缺干员，不参与尝试）
      </summary>
      <ul className="blocked-list">
        {jobs.map((job) => (
          <li key={job.id}>
            <span className="mono">ID {job.id}</span>
            <span className="blocked-title">{job.title || '（无标题作业）'}</span>
            <span className="blocked-missing">{(job.missing ?? []).join('、') || '未知'}</span>
          </li>
        ))}
      </ul>
    </details>
  )
}

export function JobsStep({
  stage,
  stages,
  onSelectActive,
  multiSearch,
  multiSelected,
  multiLoading,
  multiError,
  onSearchAll,
  onToggleMulti,
  onAddMultiQueue,
  onClearMulti,
  rosterParsed,
  search,
  loading,
  error,
  stale,
  onSearch,
  selectedIds,
  onToggle,
  onMove,
  onStart,
  onAddToQueue,
  onBatchQueue,
  batchQueueing,
  batchNotice,
  canQueue,
  canStart,
  queueCount,
  startLabel,
  starting,
  startError,
  disabled,
  manualMode,
  mode,
  onModeChange,
}: JobsStepProps) {
  const jobList = search?.jobs ?? []
  const blocked = search?.blocked ?? []
  const train = search?.train ?? []
  const selectedJobs = selectedIds
    .map((id) => jobList.find((job) => job.id === id))
    .filter((job): job is Job => Boolean(job))

  return (
    <div className="stack">
      {stages.length > 0 ? (
        <div className="stage-chips">
          <span className="field-label">当前关卡</span>
          {stages.map((item) => (
            <button
              type="button"
              key={`${item.code}|${item.stageName}`}
              className={`stage-chip ${stage && sameStage(item, stage) ? 'is-active' : ''}`}
              onClick={() => onSelectActive(item)}
              title={item.stageName}
            >
              <span className="mono">{item.code}</span>
            </button>
          ))}
          <span className="field-note">点关卡切换要搜作业的那一关</span>
        </div>
      ) : null}

      <div className="jobs-toolbar">
        <div className="jobs-toolbar-main">
          <button
            type="button"
            className="btn btn-primary"
            onClick={onSearch}
            disabled={disabled || loading || !stage}
            title={!stage ? '先在第 2 步选中关卡' : disabled ? '服务未连接' : '搜索作业站'}
          >
            {loading ? <Spinner /> : <Icon name="search" size={16} />}
            {loading ? '搜索中…' : '搜索作业'}
          </button>
          <button
            type="button"
            className="btn btn-ghost"
            onClick={onSearchAll}
            disabled={disabled || multiLoading || stages.length === 0}
            title={stages.length === 0 ? '先在第 2 步选中关卡' : '对第 2 步选中的每关都搜一次作业，按关卡分组列出'}
          >
            {multiLoading ? <Spinner /> : <Icon name="layers" size={15} />}
            {multiLoading ? '批量搜索中…' : `搜全部关卡（${stages.length}）`}
          </button>
          {stage ? (
            <span className="jobs-target">
              目标 <span className="mono">{stage.code}</span> →{' '}
              <span className="mono">{stage.stageName}</span>
            </span>
          ) : (
            <span className="field-note">先在第 2 步选中一个关卡。</span>
          )}
        </div>
        {search ? (
          <div className="jobs-count">
            候选 <b>{jobList.length}</b> 份 · 已选 <b>{selectedIds.length}</b> 份
          </div>
        ) : null}
      </div>

      <div className="jobs-mode" role="group" aria-label="作业难度过滤">
        <span className="field-label">难度</span>
        {(
          [
            ['', '全部'],
            ['normal', '普通'],
            ['raid', '突袭'],
          ] as [JobMode, string][]
        ).map(([value, label]) => (
          <button
            key={value || 'all'}
            type="button"
            className={`mode-btn ${mode === value ? 'is-active' : ''}`}
            aria-pressed={mode === value}
            disabled={disabled || loading}
            onClick={() => onModeChange(value)}
            title={
              value === 'normal'
                ? '只保留普通难度作业（按作业标题过滤）'
                : value === 'raid'
                  ? '只保留突袭/磨难作业（按作业标题过滤）'
                  : '不过滤难度'
            }
          >
            {label}
          </button>
        ))}
        {mode !== '' ? (
          <span className="field-note">
            {mode === 'raid' ? '仅突袭/磨难' : '仅普通'}：切难度会按当前关卡重新搜索作业。
          </span>
        ) : null}
      </div>

      {!rosterParsed ? (
        <Notice tone="warn">
          未填干员：只按热度排序，不保证编队。建议先在第 1 步解析 MAA 干员识别结果。
        </Notice>
      ) : null}
      {stale ? (
        <Notice tone="warn">干员名册已更新，建议重新搜索作业以刷新匹配结果。</Notice>
      ) : null}
      {stage && /(-EX-|_ex)/i.test(`${stage.code} ${stage.stageName}`) ? (
        <Notice tone="warn">
          活动 EX 关：MAA 作业集只会在主章节列表里找关，不会自己进活动图，自动导航会一直滑屏。
          建议先把游戏停在该关的关卡详情页，并在设置里打开「手动模式」再执行（突袭关记得切到突袭）。
        </Notice>
      ) : null}
      {error ? <Notice tone="error">{error}</Notice> : null}

      {multiError ? <Notice tone="error">{multiError}</Notice> : null}
      {multiLoading ? (
        <p className="loading-line">
          <Spinner /> 正在逐关搜索作业…
        </p>
      ) : null}

      {multiSearch ? (
        <div className="multi-search">
          <div className="multi-search-head">
            <span className="field-label">批量搜索结果（{multiSearch.length} 关）</span>
            <span className="row row-tight">
              <button type="button" className="btn btn-ghost btn-xs" onClick={onClearMulti}>
                清空
              </button>
              <button
                type="button"
                className="btn btn-primary btn-sm"
                onClick={onAddMultiQueue}
                disabled={disabled || starting}
                title="把每组里勾选的作业按顺序加入执行队列（已在队列里的关卡会跳过）"
              >
                加入队列
              </button>
            </span>
          </div>
          {multiSearch.map((group) => {
            const key = `${group.stage.code}|${group.stage.stageName}`
            const picked = multiSelected[key] ?? []
            return (
              <div className="multi-stage" key={key}>
                <div className="multi-stage-head">
                  <span className="mono multi-stage-code">{group.stage.code}</span>
                  <span className="field-note mono">{group.stage.stageName}</span>
                  <span className="field-note">已选 {picked.length} 份</span>
                </div>
                {group.result.jobs.length === 0 ? (
                  <p className="field-note">这一关没有可用作业。</p>
                ) : (
                  <div className="multi-jobs">
                    {group.result.jobs.map((job, index) => {
                      const on = picked.includes(job.id)
                      const diff = DIFFICULTY[job.difficulty] ?? DIFFICULTY[0]
                      return (
                        <button
                          type="button"
                          key={job.id}
                          className={`multi-job ${on ? 'is-selected' : ''}`}
                          onClick={() => onToggleMulti(key, job.id)}
                          title={job.title}
                        >
                          <span className={`checkbox ${on ? 'is-checked' : ''}`}>
                            {on ? <Icon name="check" size={11} /> : null}
                          </span>
                          <span className="mono multi-job-index">{String(index + 1).padStart(2, '0')}</span>
                          <span className="multi-job-title">{job.title || '（无标题作业）'}</span>
                          <span className={`tag ${diff.tone}`}>{diff.label}</span>
                          <span className="job-stat">★ {job.rating}</span>
                        </button>
                      )
                    })}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      ) : null}

      {!multiSearch ? (
        <>
      {search ? (
        <p className="field-note jobs-stat">
          作业站约 {search.total} 份，本次解析 {search.fetched} 份，可按账号直接抄 {jobList.length} 份
          {train.length > 0 ? `；另有 ${train.length} 份练度不足` : ''}
          {blocked.length > 0 ? `；${blocked.length} 份缺干员被过滤` : ''}。
        </p>
      ) : null}

      {loading ? (
        <p className="loading-line">
          <Spinner /> 正在拉取并匹配作业…
        </p>
      ) : null}

      {search && jobList.length === 0 && !loading ? (
        <EmptyHint>
          这个关卡没有“能直接抄”的作业
          {train.length > 0 ? `（另有 ${train.length} 份练度不足）` : ''}
          {blocked.length > 0 ? `（另有 ${blocked.length} 份缺干员）` : ''}。
          换个关卡、补练度/补干员后再试。
        </EmptyHint>
      ) : null}

      {!search && !loading ? (
        <EmptyHint>还没有搜索结果：选好关卡后点「搜索作业」。</EmptyHint>
      ) : null}

      <div className="job-list">
        {jobList.map((job, index) => {
          const orderIndex = selectedIds.indexOf(job.id)
          return (
            <JobCard
              key={job.id}
              job={job}
              position={index + 1}
              order={orderIndex + 1}
              selected={orderIndex >= 0}
              canUp={orderIndex > 0}
              canDown={orderIndex >= 0 && orderIndex < selectedIds.length - 1}
              onToggle={() => onToggle(job.id)}
              onMoveUp={() => onMove(job.id, -1)}
              onMoveDown={() => onMove(job.id, 1)}
            />
          )
        })}
      </div>

      <BlockedJobs jobs={blocked} />

      {train.length > 0 ? (
        <details className="blocked-box">
          <summary>练度不足的作业 {train.length} 份（干员都有，但等级/技能/模组不够，不能直接抄）</summary>
          <ul className="blocked-list">
            {train.map((job) => (
              <li key={job.id}>
                <span className="mono">ID {job.id}</span>
                <span className="blocked-title">{job.title || '（无标题作业）'}</span>
                <span className="blocked-missing">
                  {(job.levelShort ?? job.missing ?? []).join('、') || '练度不足'}
                </span>
              </li>
            ))}
          </ul>
        </details>
      ) : null}
        </>
      ) : null}

      <div className="start-bar">
        <div className="start-summary">
          <span className="field-label">执行顺序</span>
          {selectedJobs.length === 0 ? (
            <span className="field-note">尚未勾选作业。</span>
          ) : (
            <ol className="order-list">
              {selectedJobs.map((job) => (
                <li key={job.id}>
                  <span className="mono order-id">#{job.id}</span>
                  <span className="order-title">{job.title || '（无标题作业）'}</span>
                </li>
              ))}
            </ol>
          )}
          <span className="field-note">
            {manualMode
              ? '手动模式已开启：跳过自动导航，请先让游戏停在关卡编队/关卡详情页。'
              : queueCount > 0
                ? `队列里已有 ${queueCount} 关：点「加入队列」把当前关卡追加到末尾，点「按队列执行」一次跑完。`
                : '失败会自动换下一份；全部失败才算失败。也可以把本关加入队列，最后一次性逐关执行。'}
          </span>
        </div>
        <div className="start-actions">
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={onBatchQueue}
            disabled={disabled || starting || batchQueueing || stages.length === 0}
            title={
              stages.length === 0
                ? '先在第 2 步选中关卡'
                : '对选中关卡逐关搜索作业，每关取前 3 份按顺序加入队列（已在队列里的关卡会跳过）'
            }
          >
            {batchQueueing ? <Spinner /> : null}
            {batchQueueing ? '批量入队中…' : `批量入队（${stages.length} 关）`}
          </button>
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={onAddToQueue}
            disabled={disabled || starting || !canQueue}
            title={
              !canQueue
                ? '先选好关卡并至少勾选一份作业'
                : '把当前关卡与勾选的作业追加到执行队列末尾'
            }
          >
            加入队列
          </button>
          <button
            type="button"
            className="btn btn-primary btn-lg start-btn"
            onClick={onStart}
            disabled={disabled || starting || !canStart}
            title={
              disabled
                ? '服务未连接，无法开始'
                : !canStart
                  ? '先选好关卡并至少勾选一份作业'
                  : '开始执行'
            }
          >
            {starting ? <Spinner /> : <Icon name="play" size={16} />}
            {starting ? '启动中…' : startLabel}
          </button>
        </div>
      </div>
      {startError ? <Notice tone="error">{startError}</Notice> : null}
      {batchNotice ? <Notice tone="info">{batchNotice}</Notice> : null}
    </div>
  )
}
