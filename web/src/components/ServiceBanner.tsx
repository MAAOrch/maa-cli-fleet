// 顶部横幅：服务离线 / MAA 运行目录未就绪。其余情况不渲染。
import { Spinner } from './Feedback'
import { Icon } from './icons'

export interface ServiceBannerProps {
  down: boolean
  healthError: string
  maaReady: boolean
  maaError: string
  retryLoading: boolean
  onRetry: () => void
}

export function ServiceBanner({
  down,
  healthError,
  maaReady,
  maaError,
  retryLoading,
  onRetry,
}: ServiceBannerProps) {
  const showMaa = !down && !maaReady
  if (!down && !showMaa) return null

  return (
    <div className={`banner ${down ? 'banner-warn' : 'banner-note'}`} role="status">
      <span className="banner-icon">
        <Icon name="alert" size={18} />
      </span>
      <div className="banner-body">
        {down ? (
          <>
            <strong>未连接到 copilot-picker 服务，请求按钮已禁用</strong>
            <p>
              请先在机队启动 copilot-picker 服务，或在本机建立 SSH 隧道（
              <code className="mono">ssh -L 8787:127.0.0.1:8787 用户@机队</code>
              ）后点「重试连接」。界面仍可编辑，干员文本不会丢。
            </p>
            {healthError ? <p className="mono banner-detail">{healthError}</p> : null}
          </>
        ) : (
          <>
            <strong>MAA 运行目录未就绪</strong>
            <p>
              服务已连接，但隔离运行目录（data_copilot/maa_data）里的 MAA 数据链接可能缺失，开始执行会失败。请先按部署文档初始化运行目录。
            </p>
            {maaError ? <p className="mono banner-detail">{maaError}</p> : null}
          </>
        )}
      </div>
      <button
        type="button"
        className="btn btn-sm btn-ghost banner-retry"
        onClick={onRetry}
        disabled={retryLoading}
      >
        {retryLoading ? <Spinner /> : <Icon name="refresh" size={14} />}
        重试连接
      </button>
    </div>
  )
}
