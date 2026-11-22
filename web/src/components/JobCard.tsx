// 作业卡片：难度/评分/观看/干员匹配情况 + 勾选与排序。
import type { Job } from '../types'
import { Icon } from './icons'

export interface JobCardProps {
  job: Job
  /** 选中后的尝试序号（1 起），未选中为 0 */
  order: number
  /** 在搜索结果里的位置（1 起，仅展示） */
  position: number
  selected: boolean
  canUp: boolean
  canDown: boolean
  onToggle: () => void
  onMoveUp: () => void
  onMoveDown: () => void
}

export const DIFFICULTY: Record<number, { label: string; tone: string }> = {
  1: { label: '普通', tone: 'diff-normal' },
  2: { label: '突袭', tone: 'diff-hard' },
  3: { label: '磨难', tone: 'diff-hard' },
  0: { label: '未标注', tone: 'diff-none' },
}

function formatViews(views: number): string {
  if (!Number.isFinite(views) || views <= 0) return '0'
  if (views >= 10000) {
    const wan = views / 10000
    return `${wan >= 10 ? wan.toFixed(0) : wan.toFixed(1)}万`
  }
  return String(views)
}

export function JobCard({
  job,
  order,
  position,
  selected,
  canUp,
  canDown,
  onToggle,
  onMoveUp,
  onMoveDown,
}: JobCardProps) {
  const difficulty = DIFFICULTY[job.difficulty] ?? DIFFICULTY[0]
  const opers = job.opers ?? []
  const missing = new Set(job.missing ?? [])
  const levelShort = new Set(job.levelShort ?? [])
  const extraMissing = [...missing].filter((name) => !opers.includes(name))

  return (
    <article className={`job-card ${selected ? 'is-selected' : ''}`}>
      <div className="job-top">
        <button
          type="button"
          className={`checkbox ${selected ? 'is-checked' : ''}`}
          onClick={onToggle}
          aria-pressed={selected}
          aria-label={selected ? `取消选择作业 ${job.id}` : `选择作业 ${job.id}`}
          title={selected ? '取消选择' : '选择该作业'}
        >
          {selected ? <Icon name="check" size={12} /> : null}
        </button>

        <span className="job-index mono">{String(position).padStart(2, '0')}</span>

        <div className="job-headline">
          <div className="job-title-row">
            {selected ? <span className="job-order mono">#{order}</span> : null}
            <h3 className="job-title" title={job.title}>
              {job.title || '（无标题作业）'}
            </h3>
          </div>
          <div className="job-meta">
            <span className="mono job-id">ID {job.id}</span>
            <span className={`tag ${difficulty.tone}`}>{difficulty.label}</span>
            <span className="job-stat">
              <span className="star">★</span> {job.rating}
            </span>
            <span className="job-stat">
              <Icon name="eye" size={12} /> {formatViews(job.views)}
            </span>
            {job.minimumRequired ? (
              <span className="job-stat job-req">最低练度 {job.minimumRequired}</span>
            ) : null}
            {missing.size > 0 ? <span className="tag tag-bad">缺 {missing.size} 名</span> : null}
            {levelShort.size > 0 ? (
              <span className="tag tag-warn">练度不足 {levelShort.size}</span>
            ) : null}
            {job.met ? <span className="tag tag-ok">可编队</span> : null}
            {!job.met && job.mode === 'borrow' ? (
              <span className="tag tag-warn" title="缺 1 名干员，可借助战通过">
                可借助战
              </span>
            ) : null}
            {(job.localPass || job.localFail) ? (
              <span
                className={`tag ${(job.localScore ?? 0) > 0 ? 'tag-ok' : (job.localScore ?? 0) < 0 ? 'tag-bad' : 'tag-warn'}`}
                title={`跨号共享评分库：3★/通关 ${job.localPass ?? 0} 次，失败/低星 ${job.localFail ?? 0} 次，综合分 ${(job.localScore ?? 0).toFixed(1)}`}
              >
                {(job.localScore ?? 0) > 0
                  ? `实测过 ${job.localPass ?? 0}`
                  : (job.localScore ?? 0) < 0
                    ? `避雷 败${job.localFail ?? 0}`
                    : `实测 ${job.localPass ?? 0}胜/${job.localFail ?? 0}败`}
                {job.localScore ? ` (${job.localScore > 0 ? '+' : ''}${job.localScore.toFixed(1)})` : ''}
              </span>
            ) : null}
          </div>
        </div>

        <div className="job-move">
          <button
            type="button"
            className="btn btn-icon btn-ghost btn-xs"
            onClick={onMoveUp}
            disabled={!selected || !canUp}
            title="上移（提前尝试）"
            aria-label="上移该作业"
          >
            <Icon name="arrow-up" size={14} />
          </button>
          <button
            type="button"
            className="btn btn-icon btn-ghost btn-xs"
            onClick={onMoveDown}
            disabled={!selected || !canDown}
            title="下移（延后尝试）"
            aria-label="下移该作业"
          >
            <Icon name="arrow-down" size={14} />
          </button>
        </div>
      </div>

      <div className="opers">
        {opers.map((name) => (
          <span
            key={name}
            className={`oper ${missing.has(name) ? 'oper-missing' : ''} ${
              !missing.has(name) && levelShort.has(name) ? 'oper-short' : ''
            }`}
            title={missing.has(name) ? '缺少该干员' : levelShort.has(name) ? '练度不足' : name}
          >
            {name}
          </span>
        ))}
        {extraMissing.map((name) => (
          <span key={`missing-${name}`} className="oper oper-missing" title="缺少该干员">
            {name}
          </span>
        ))}
        {opers.length === 0 && extraMissing.length === 0 ? (
          <span className="field-note">作业未标注干员</span>
        ) : null}
      </div>

      {job.details ? <p className="job-details">{job.details}</p> : null}
    </article>
  )
}
