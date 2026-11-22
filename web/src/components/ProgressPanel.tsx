// 推关台账：GET /api/progress 的只读列表（哪一关、普通/突袭、几星、何时通）。
import type { StageProgress } from '../types'
import { EmptyHint, Notice, Spinner } from './Feedback'
import { Icon } from './icons'

export interface ProgressPanelProps {
  items: StageProgress[]
  loading: boolean
  error: string
  onRefresh: () => void
  disabled: boolean
}

function modeLabel(mode: string): string {
  if (mode === 'raid') return '突袭'
  if (mode === 'normal') return '普通'
  return mode || '未知'
}

function starLabel(item: StageProgress): string {
  if (item.mode === 'raid') return item.stars > 0 ? '已通关' : '—'
  const stars = Math.max(0, Math.min(3, item.stars))
  return '★★★'.slice(0, stars) || '—'
}

export function ProgressPanel({ items, loading, error, onRefresh, disabled }: ProgressPanelProps) {
  return (
    <div className="stack">
      <div className="field-head">
        <span className="field-note">
          执行成功后按「关卡 + 难度」记星；普通关要 3 星才会解锁突袭。
        </span>
        <button
          type="button"
          className="btn btn-ghost btn-sm"
          onClick={onRefresh}
          disabled={disabled || loading}
          title="重新读取 progress.json"
        >
          {loading ? <Spinner /> : <Icon name="refresh" size={14} />} 刷新
        </button>
      </div>

      {error ? <Notice tone="error">{error}</Notice> : null}

      {items.length === 0 && !loading && !error ? (
        <EmptyHint>
          台账还是空的：这一关还没有成功记录。执行一次推关后，这里会按「关卡 + 普通/突袭」列出星数与通关时间。
        </EmptyHint>
      ) : null}

      {items.length > 0 ? (
        <div className="ledger">
          <div className="ledger-head">
            <span>关卡</span>
            <span>难度</span>
            <span>评价</span>
            <span>通关时间</span>
          </div>
          {items.map((item) => (
            <div className="ledger-row" key={`${item.stageCode}|${item.mode}`}>
              <span className="mono ledger-stage">{item.stageCode}</span>
              <span className={`tag ${item.mode === 'raid' ? 'diff-hard' : 'diff-normal'}`}>
                {modeLabel(item.mode)}
              </span>
              <span className="ledger-stars">{starLabel(item)}</span>
              <span className="mono ledger-time">{item.clearedAt || '—'}</span>
            </div>
          ))}
          <p className="field-note ledger-count">共 {items.length} 条记录</p>
        </div>
      ) : null}
    </div>
  )
}
