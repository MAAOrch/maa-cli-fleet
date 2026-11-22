// 执行队列（半自动）：人工选好多关 + 每关候选作业，程序按顺序逐关执行。
import type { Stage } from '../types'
import { Icon } from './icons'

/** 队列里的一项：一关 + 该关按顺序尝试的作业（及其中要按突袭执行的作业）。 */
export interface QueueEntry {
  stage: Stage
  jobIds: number[]
  /** 作业站 difficulty=2/3（突袭/磨难）的作业：执行时 MAA 会切突袭难度 */
  raidJobIds?: number[]
}

export interface QueuePanelProps {
  queue: QueueEntry[]
  disabled: boolean
  onRemove: (index: number) => void
  onMove: (index: number, direction: -1 | 1) => void
  onClear: () => void
}

export function QueuePanel({ queue, disabled, onRemove, onMove, onClear }: QueuePanelProps) {
  const totalJobs = queue.reduce((sum, item) => sum + item.jobIds.length, 0)

  if (queue.length === 0) {
    return (
      <p className="field-note">
        队列为空：在 03 里选好关卡与作业后点「加入队列」，再点「按队列执行」。
        不加队列也可以直接单关执行。
      </p>
    )
  }

  return (
    <div className="stack">
      <ol className="queue-list">
        {queue.map((item, index) => (
          <li className="queue-row" key={`${item.stage.code}|${item.stage.stageName}|${index}`}>
            <span className="queue-index mono">{index + 1}</span>
            <span className="queue-main">
              <span className="queue-code mono">
                {item.stage.code}
                <span className="queue-arrow">→</span>
                <span className="queue-stage-name mono">{item.stage.stageName}</span>
              </span>
              <span className="queue-jobs mono" title={item.jobIds.map((id) => `#${id}`).join(' ')}>
                {item.jobIds.length} 份作业：{item.jobIds.map((id) => `#${id}`).join(' ')}
                {(item.raidJobIds ?? []).length > 0
                  ? ` · 突袭 ${(item.raidJobIds ?? []).join(',')}`
                  : ''}
              </span>
            </span>
            <span className="queue-actions">
              <button
                type="button"
                className="btn btn-ghost btn-xs"
                disabled={disabled || index === 0}
                onClick={() => onMove(index, -1)}
                title="上移一关"
                aria-label="上移一关"
              >
                <Icon name="arrow-up" size={12} />
              </button>
              <button
                type="button"
                className="btn btn-ghost btn-xs"
                disabled={disabled || index === queue.length - 1}
                onClick={() => onMove(index, 1)}
                title="下移一关"
                aria-label="下移一关"
              >
                <Icon name="arrow-down" size={12} />
              </button>
              <button
                type="button"
                className="btn btn-ghost btn-xs"
                disabled={disabled}
                onClick={() => onRemove(index)}
                title="移出队列"
                aria-label="移出队列"
              >
                <Icon name="close" size={12} />
              </button>
            </span>
          </li>
        ))}
      </ol>
      <div className="queue-foot">
        <span className="field-note">
          共 {queue.length} 关 · {totalJobs} 份作业，按此顺序逐关执行；每关失败自动换下一份。
        </span>
        <button type="button" className="btn btn-ghost btn-sm" onClick={onClear} disabled={disabled}>
          清空队列
        </button>
      </div>
    </div>
  )
}
