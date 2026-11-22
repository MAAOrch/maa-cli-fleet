// 通用反馈组件：加载指示、提示条、空态。
import type { ReactNode } from 'react'

export function Spinner() {
  return <span className="spinner" aria-hidden="true" />
}

export type NoticeTone = 'info' | 'warn' | 'error'

export function Notice({ tone = 'info', children }: { tone?: NoticeTone; children: ReactNode }) {
  return (
    <div className={`notice notice-${tone}`} role={tone === 'error' ? 'alert' : undefined}>
      {children}
    </div>
  )
}

export function EmptyHint({ children }: { children: ReactNode }) {
  return <div className="empty-hint">{children}</div>
}
