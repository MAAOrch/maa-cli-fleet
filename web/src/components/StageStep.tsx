// Step 2：自定义快捷关卡（可增删）+ 手输代号解析 → 多选候选关卡。
import { useState } from 'react'
import type { Stage } from '../types'
import { EmptyHint, Notice, Spinner } from './Feedback'
import { Icon } from './icons'

export interface StageStepProps {
  query: string
  onQuery: (value: string) => void
  onResolve: () => void
  loading: boolean
  error: string
  stages: Stage[]
  selected: Stage[]
  onToggle: (stage: Stage) => void
  onSelectAll: () => void
  onClear: () => void
  disabled: boolean
  /** 自定义的快捷关卡代号（localStorage） */
  customStages: string[]
  /** 点快捷关卡：解析 + 选中 + 自动搜作业 */
  onQuickStage: (code: string) => void
  onAddCustomStages: (codes: string[]) => void
  onDeleteCustomStage: (code: string) => void
}

function sameStage(a: Stage, b: Stage): boolean {
  return a.code === b.code && a.stageName === b.stageName
}

function sourceLabel(source: string): string {
  if (source === 'maa') return 'MAA 资源'
  if (source === 'prts') return '作业站'
  return source
}

export function StageStep({
  query,
  onQuery,
  onResolve,
  loading,
  error,
  stages,
  selected,
  onToggle,
  onSelectAll,
  onClear,
  disabled,
  customStages,
  onQuickStage,
  onAddCustomStages,
  onDeleteCustomStage,
}: StageStepProps) {
  const [codeInput, setCodeInput] = useState('')
  const [addError, setAddError] = useState('')

  const addCodes = () => {
    const codes = codeInput
      .split(/[\s,，;；]+/)
      .map((item) => item.trim())
      .filter(Boolean)
    if (codes.length === 0) {
      setAddError('先输入关卡代号，例如 TL-EX-1')
      return
    }
    onAddCustomStages(codes)
    setCodeInput('')
    setAddError('')
  }

  return (
    <div className="stack">
      <div className="stage-quick">
        <div className="stage-quick-head">
          <span className="field-label">
            <Icon name="spark" size={13} /> 自定义关卡
          </span>
        </div>
        <div className="quick-add">
          <input
            className="input mono"
            type="text"
            placeholder="输入关卡代号，逗号/空格分隔：TL-EX-1 TL-EX-2"
            value={codeInput}
            onChange={(event) => setCodeInput(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') addCodes()
            }}
          />
          <button type="button" className="btn btn-primary btn-sm" onClick={addCodes}>
            添加
          </button>
        </div>
        {addError ? <p className="field-note quick-edit-error">{addError}</p> : null}

        {customStages.length > 0 ? (
          <div className="quick-chips">
            {customStages.map((code) => (
              <span className="quick-chip-wrap" key={code}>
                <button
                  type="button"
                  className="quick-chip is-custom"
                  onClick={() => onQuickStage(code)}
                  disabled={disabled || loading}
                  title={`解析 ${code} 并自动搜作业`}
                >
                  {code}
                </button>
                <button
                  type="button"
                  className="quick-chip-x"
                  onClick={() => onDeleteCustomStage(code)}
                  title="删除这个快捷关卡"
                  aria-label={`删除 ${code}`}
                >
                  ×
                </button>
              </span>
            ))}
          </div>
        ) : (
          <p className="field-note">
            还没有自定义关卡：输入代号点「添加」，之后点按钮就会直接解析 + 搜作业；点 × 可删除。
          </p>
        )}
      </div>

      <div className="field">
        <div className="field-head">
          <label className="field-label" htmlFor="stage-query">
            <Icon name="layers" size={13} /> 关卡代号
          </label>
          <button
            type="button"
            className="btn btn-primary btn-sm"
            onClick={onResolve}
            disabled={disabled || loading}
            title={disabled ? '服务未连接，无法解析' : '解析关卡代号'}
          >
            {loading ? <Spinner /> : <Icon name="search" size={14} />} 解析关卡
          </button>
        </div>
        <input
          id="stage-query"
          className="input mono"
          type="text"
          value={query}
          placeholder="TL-9 / 1-7 / CE-6；可输入多个，用逗号或空格分隔"
          onChange={(event) => onQuery(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && !disabled && !loading) onResolve()
          }}
        />
        <p className="field-note">
          输入一个或多个关卡代号（如 <span className="mono">TL-9 TL-10</span>），解析后可以多选；
          第 3 步按「当前关卡」搜作业，队列按顺序逐关执行。
        </p>
      </div>

      {error ? <Notice tone="error">{error}</Notice> : null}

      {loading ? (
        <p className="loading-line">
          <Spinner /> 正在解析关卡…
        </p>
      ) : null}

      {stages.length > 0 ? (
        <>
          <div className="stage-list">
            {stages.map((stage) => {
              const active = selected.some((item) => sameStage(item, stage))
              return (
                <button
                  type="button"
                  key={`${stage.code}|${stage.stageName}`}
                  className={`stage-card ${active ? 'is-active' : ''}`}
                  onClick={() => onToggle(stage)}
                  aria-pressed={active}
                >
                  <span className="stage-radio" aria-hidden="true">
                    {active ? <Icon name="check" size={12} /> : null}
                  </span>
                  <span className="stage-main">
                    <span className="stage-code mono">{stage.code}</span>
                    <span className="stage-name mono">{stage.stageName || '（无关卡代码）'}</span>
                    {stage.title ? <span className="stage-title">{stage.title}</span> : null}
                  </span>
                  {stage.source ? <span className="tag tag-dim">{sourceLabel(stage.source)}</span> : null}
                </button>
              )
            })}
          </div>
          <div className="row row-tight stage-select-bar">
            <span className="field-note">
              候选 {stages.length} 个 · 已选 <b>{selected.length}</b> 关
            </span>
            <button type="button" className="btn btn-ghost btn-xs" onClick={onSelectAll}>
              全选
            </button>
            <button type="button" className="btn btn-ghost btn-xs" onClick={onClear}>
              清空
            </button>
          </div>
        </>
      ) : !loading ? (
        <EmptyHint>还没有候选关卡：点上面的自定义关卡，或输入代号后点「解析关卡」。</EmptyHint>
      ) : null}
    </div>
  )
}
