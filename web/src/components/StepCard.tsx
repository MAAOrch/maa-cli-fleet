// 步骤卡片：编号 + 标题 + 说明 + 右侧附加信息。
import type { ReactNode } from 'react'

export interface StepCardProps {
  step: string
  title: string
  hint?: string
  aside?: ReactNode
  children: ReactNode
}

export function StepCard({ step, title, hint, aside, children }: StepCardProps) {
  return (
    <section className="card step">
      <header className="step-head">
        <span className="step-index mono">{step}</span>
        <div className="step-titles">
          <h2 className="step-title">{title}</h2>
          {hint ? <p className="step-hint">{hint}</p> : null}
        </div>
        {aside ? <div className="step-aside">{aside}</div> : null}
      </header>
      <div className="step-body">{children}</div>
    </section>
  )
}
