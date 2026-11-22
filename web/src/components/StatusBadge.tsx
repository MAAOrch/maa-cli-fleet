// 执行状态徽标：中文映射 + 进行中呼吸动画。
import type { RunStatus } from '../types'

const LABELS: Record<RunStatus, string> = {
  idle: '待开始',
  starting: '启动游戏',
  navigating: '导航中',
  running: '执行中',
  success: '成功',
  failed: '失败',
  stopped: '已停止',
}

const TONES: Record<RunStatus, string> = {
  idle: 'status-idle',
  starting: 'status-busy',
  navigating: 'status-busy',
  running: 'status-busy',
  success: 'status-ok',
  failed: 'status-bad',
  stopped: 'status-warn',
}

const PULSING: Record<RunStatus, boolean> = {
  idle: false,
  starting: true,
  navigating: true,
  running: true,
  success: false,
  failed: false,
  stopped: false,
}

export function StatusBadge({ status }: { status: RunStatus }) {
  return (
    <span className={`status-badge ${TONES[status]} ${PULSING[status] ? 'is-pulsing' : ''}`}>
      <span className="status-dot" />
      {LABELS[status]}
    </span>
  )
}
