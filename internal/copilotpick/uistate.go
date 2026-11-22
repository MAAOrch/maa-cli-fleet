package copilotpick

// 界面状态持久化：账号/名册/关卡/搜索/队列/自定义快捷关卡/设置。
//
// 为什么不放浏览器 localStorage：PC 客户端用 iframe 嵌 picker，顶层是 pywebview 的
// html= 页面（不透明 origin），iframe 里的 localStorage 会被 Chromium 按第三方存储
// 分区处理，刷新后就没了。所以状态存服务端 <root>/ui_state.json，任何客户端/刷新都能恢复。

import (
	"encoding/json"
	"io"
	"net/http"
	"os"
	"path/filepath"
)

// maxUIStateBytes 限制上传体积（名册 + 搜索结果 + 队列，正常 < 1MB）。
const maxUIStateBytes = 8 << 20

func (h *handler) uiStatePath() string {
	return filepath.Join(h.m.cfg.Root, "ui_state.json")
}

// getUIState 返回上次保存的界面状态；没有就是 null。
func (h *handler) getUIState(w http.ResponseWriter, r *http.Request) {
	b, err := os.ReadFile(h.uiStatePath())
	if err != nil || len(b) == 0 {
		writeJSON(w, http.StatusOK, map[string]any{"state": nil})
		return
	}
	var state any
	if err := json.Unmarshal(b, &state); err != nil {
		writeJSON(w, http.StatusOK, map[string]any{"state": nil})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"state": state})
}

// putUIState 覆盖保存界面状态（原子写：先写 .tmp 再 rename）。
func (h *handler) putUIState(w http.ResponseWriter, r *http.Request) {
	body, err := io.ReadAll(io.LimitReader(r.Body, maxUIStateBytes+1))
	if err != nil {
		writeErr(w, http.StatusBadRequest, "读取界面状态失败："+err.Error())
		return
	}
	if len(body) > maxUIStateBytes {
		writeErr(w, http.StatusRequestEntityTooLarge, "界面状态太大（超过 8MB）")
		return
	}
	if len(body) == 0 || !json.Valid(body) {
		writeErr(w, http.StatusBadRequest, "界面状态不是合法 JSON")
		return
	}
	path := h.uiStatePath()
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, body, 0o644); err != nil {
		writeErr(w, http.StatusInternalServerError, "写界面状态失败："+err.Error())
		return
	}
	if err := os.Rename(tmp, path); err != nil {
		_ = os.Remove(tmp)
		writeErr(w, http.StatusInternalServerError, "保存界面状态失败："+err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}
