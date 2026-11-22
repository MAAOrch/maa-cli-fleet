// Step 1：账号选择 + 干员识别结果解析。
import type { Account, Roster } from '../types'
import { Notice, Spinner } from './Feedback'
import { Icon } from './icons'

export interface AccountStepProps {
  accounts: Account[]
  selectedId: string
  onSelect: (id: string) => void
  loading: boolean
  error: string
  onReload: () => void
  /** 启动当前账号的模拟器（MuMuManager launch） */
  onStartEmu: () => void
  emuStarting: boolean
  emuNotice: string
  disabled: boolean
  rosterText: string
  onRosterText: (text: string) => void
  roster: Roster | null
  rosterLoading: boolean
  rosterError: string
  onParse: () => void
  onPaste: () => void
  pasteMessage: string
  /** 读该账号 MAA 目录里「干员识别」落盘的 OperBoxData.json */
  onLoadMaa: () => void
  maaLoading: boolean
  /** 用 maa-cli 跑一次 OperBox 任务（干员识别） */
  onScanMaa: () => void
  scanLoading: boolean
}

function accountOptionLabel(account: Account): string {
  const online = account.online ? '在线' : '离线'
  const enabled = account.enabled ? '' : ' · 已停用'
  return `${account.name} · ${account.id} · ${account.client || '未知客户端'} · emu${account.emu || '-'} · ${online}${enabled}`
}

function AccountSummary({ account }: { account: Account }) {
  return (
    <div className="account-card">
      <span
        className={`online-dot ${account.online ? 'is-online' : 'is-offline'}`}
        title={account.online ? '模拟器在线' : '模拟器离线'}
      />
      <span className="mono account-id">{account.id}</span>
      <span className="account-name">{account.name}</span>
      <span className="tag">{account.client || '未知客户端'}</span>
      <span className="tag tag-dim mono">emu {account.emu || '—'}</span>
      <span className={`tag ${account.enabled ? 'tag-ok' : 'tag-warn'}`}>
        {account.enabled ? '启用' : '已停用'}
      </span>
      {account.online ? null : <span className="tag tag-warn">模拟器离线</span>}
      <span className="mono account-adb">{account.adb || '未解析 adb'}</span>
      {account.enabled ? null : (
        <span className="field-note account-note">
          该账号在机队里已停用，copilot-picker 直接拉起 MAA，仍可执行。
        </span>
      )}
    </div>
  )
}

function RosterSummary({ roster }: { roster: Roster }) {
  return (
    <div className="roster-summary">
      <div className="chip-row">
        <span className="chip">
          <b>{roster.count}</b> 名干员
        </span>
        <span className="chip chip-six">
          <b>{roster.six}</b> 六星
        </span>
        <span className="chip chip-five">
          <b>{roster.five}</b> 五星
        </span>
        {roster.format ? <span className="chip chip-format mono">{roster.format}</span> : null}
      </div>
      {roster.warnings && roster.warnings.length > 0 ? (
        <ul className="warn-list">
          {roster.warnings.map((warning, index) => (
            <li key={`${index}-${warning}`}>{warning}</li>
          ))}
        </ul>
      ) : null}
    </div>
  )
}

export function AccountStep({
  accounts,
  selectedId,
  onSelect,
  loading,
  error,
  onReload,
  onStartEmu,
  emuStarting,
  emuNotice,
  disabled,
  rosterText,
  onRosterText,
  roster,
  rosterLoading,
  rosterError,
  onParse,
  onPaste,
  pasteMessage,
  onLoadMaa,
  maaLoading,
  onScanMaa,
  scanLoading,
}: AccountStepProps) {
  const selected = accounts.find((account) => account.id === selectedId) ?? null

  return (
    <div className="stack">
      <div className="field">
        <div className="field-head">
          <label className="field-label" htmlFor="account-select">
            <Icon name="user" size={13} /> 账号
          </label>
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={onReload}
            disabled={loading}
            title="重新读取机队账号"
          >
            {loading ? <Spinner /> : <Icon name="refresh" size={14} />} 刷新
          </button>
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={onStartEmu}
            disabled={disabled || emuStarting || !selectedId}
            title="用 MuMuManager 启动这个账号对应的模拟器实例"
          >
            {emuStarting ? <Spinner /> : <Icon name="play" size={14} />}
            {emuStarting ? '启动中…' : '启动模拟器'}
          </button>
        </div>
        {emuNotice ? <p className="field-note">{emuNotice}</p> : null}
        <select
          id="account-select"
          className="select"
          value={selectedId}
          onChange={(event) => onSelect(event.target.value)}
          disabled={accounts.length === 0}
        >
          {accounts.length === 0 ? (
            <option value="">{loading ? '读取账号中…' : '暂无账号（服务未连接或未部署）'}</option>
          ) : null}
          {accounts.map((account) => (
            <option key={account.id} value={account.id}>
              {accountOptionLabel(account)}
            </option>
          ))}
        </select>
        {selected ? <AccountSummary account={selected} /> : null}
        {error ? <Notice tone="error">{error}</Notice> : null}
      </div>

      <div className="field">
        <div className="field-head">
          <label className="field-label" htmlFor="roster-text">
            <Icon name="clipboard" size={13} /> 干员识别结果
          </label>
          <div className="row row-tight">
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              onClick={onScanMaa}
              disabled={disabled || scanLoading || maaLoading || !selectedId}
              title={
                disabled
                  ? '服务未连接，无法扫描'
                  : !selectedId
                    ? '先选账号'
                    : '用 maa-cli 跑 MAA 的 OperBox 任务（干员识别）：会先开始唤醒，再做识别（约 1–2 分钟）'
              }
            >
              {scanLoading ? <Spinner /> : <Icon name="spark" size={14} />}
              {scanLoading ? '扫描中…' : '扫描干员'}
            </button>
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              onClick={onLoadMaa}
              disabled={disabled || maaLoading || !selectedId}
              title={
                disabled
                  ? '服务未连接，无法读取'
                  : !selectedId
                    ? '先选账号'
                    : '读取该账号 MAA 目录下 data/OperBoxData.json（需先在 MAA 小工具跑过「干员识别」）'
              }
            >
              {maaLoading ? <Spinner /> : <Icon name="radar" size={14} />} 读 MAA 识别
            </button>
            <button type="button" className="btn btn-ghost btn-sm" onClick={onPaste}>
              <Icon name="clipboard" size={14} /> 读取剪贴板
            </button>
            <button
              type="button"
              className="btn btn-primary btn-sm"
              onClick={onParse}
              disabled={disabled || rosterLoading}
              title={disabled ? '服务未连接，无法解析' : '解析干员文本'}
            >
              {rosterLoading ? <Spinner /> : <Icon name="spark" size={14} />} 解析
            </button>
          </div>
        </div>
        <textarea
          id="roster-text"
          className="textarea mono"
          rows={6}
          spellCheck={false}
          placeholder={
            '粘贴 MAA 干员识别结果，支持：\n· MAA「干员识别」复制的 JSON 数组\n· OperBox 回调 JSON（own_opers）\n· Markdown / CSV 表格\n· 每行一个干员名'
          }
          value={rosterText}
          onChange={(event) => onRosterText(event.target.value)}
        />
        {pasteMessage ? <p className="field-note">{pasteMessage}</p> : null}
        {rosterError ? <Notice tone="error">{rosterError}</Notice> : null}
        {roster ? (
          <RosterSummary roster={roster} />
        ) : (
          <p className="field-note">
            未解析干员时仍可只按热度搜作业，但不会过滤缺干员，也不保证编队。
          </p>
        )}
      </div>
    </div>
  )
}
