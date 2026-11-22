// 顶部栏：品牌、服务健康徽标、MAA 就绪徽标、刷新/设置。
import type { Health } from '../types'
import { Spinner } from './Feedback'
import { Icon } from './icons'

export interface TopBarProps {
  health: Health | null
  healthLoading: boolean
  maaReady: boolean
  onRefresh: () => void
  onOpenSettings: () => void
}

export function TopBar({ health, healthLoading, maaReady, onRefresh, onOpenSettings }: TopBarProps) {
  const up = health?.ok === true
  const version = health?.version?.trim() ?? ''

  return (
    <header className="topbar">
      <div className="brand">
        <span className="brand-mark">
          <Icon name="radar" size={20} />
        </span>
        <div className="brand-text">
          <h1 className="brand-title">copilot-picker</h1>
          <p className="brand-sub">MAA 作业自动挑选与执行</p>
        </div>
      </div>

      <div className="topbar-spacer" />

      <div className="topbar-status">
        <span
          className={`pill ${up ? 'pill-ok' : 'pill-bad'}`}
          title={up ? `服务在线 · ${health?.listen ?? ''}` : '服务离线'}
        >
          <span className="pill-dot" />
          {up ? '服务在线' : '服务离线'}
        </span>
        <span className={`pill ${!up ? 'pill-idle' : maaReady ? 'pill-ok' : 'pill-warn'}`}>
          {!up ? 'MAA 未知' : maaReady ? 'MAA 就绪' : 'MAA 未就绪'}
        </span>
        <span className="mono topbar-ver" title={health?.root ?? ''}>
          {version ? `v${version}` : '—'} · {health?.listen || '127.0.0.1:8787'}
        </span>
      </div>

      <button
        type="button"
        className="btn btn-icon btn-ghost"
        onClick={onRefresh}
        title="重新检测服务状态"
        aria-label="重新检测服务状态"
        disabled={healthLoading}
      >
        {healthLoading ? <Spinner /> : <Icon name="refresh" size={17} />}
      </button>
      <button
        type="button"
        className="btn btn-icon btn-ghost"
        onClick={onOpenSettings}
        title="执行设置"
        aria-label="打开执行设置"
      >
        <Icon name="settings" size={18} />
      </button>
    </header>
  )
}
