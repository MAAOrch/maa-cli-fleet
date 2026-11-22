package copilotpick

// 模拟器开/关：picker 自己不会开模拟器（MAA CLI 只能启动游戏），这里补上
// MuMuManager 的 launch/shutdown，让 PC 客户端可以跨服把实例拉起来。
//
// 注意：MuMu 要求 launch 在交互式桌面会话里执行；picker 服务本身通常就是
// 从登录计划任务拉起的（session 1），所以这里直接调 MuMuManager 即可。

import (
	"context"
	"fmt"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
)

// mumuManagerPath 返回 MuMuManager.exe 路径（<mumuRoot>/nx_main/MuMuManager.exe）。
func (h *handler) mumuManagerPath() (string, error) {
	root := strings.TrimSpace(h.m.cfg.MumuRoot)
	if root == "" {
		return "", fmt.Errorf("配置里没有 MuMu 安装目录（mumuRoot）")
	}
	p := filepath.Join(root, "nx_main", "MuMuManager.exe")
	if _, err := os.Stat(p); err != nil {
		return "", fmt.Errorf("找不到 MuMuManager.exe：%s", p)
	}
	return p, nil
}

// emuStarted 判实例是否已经在运行。
func emuStarted(ctx context.Context, mgr, index string) bool {
	cctx, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()
	cmd := exec.CommandContext(cctx, mgr, "info", "--vmindex", index)
	cmd.SysProcAttr = hideWindow()
	out, err := cmd.Output()
	if err != nil {
		return false
	}
	return strings.Contains(string(out), `"is_android_started": true`)
}

// emuControl 执行一次 MuMuManager control 子命令，返回其输出。
func emuControl(ctx context.Context, mgr, action, index string) (string, error) {
	cctx, cancel := context.WithTimeout(ctx, 60*time.Second)
	defer cancel()
	cmd := exec.CommandContext(cctx, mgr, "control", action, "--vmindex", index)
	cmd.SysProcAttr = hideWindow()
	out, err := cmd.CombinedOutput()
	if err != nil {
		return string(out), fmt.Errorf("MuMuManager %s 失败：%v %s", action, err, strings.TrimSpace(string(out)))
	}
	return string(out), nil
}

// emuStart 启动账号对应的模拟器实例，最多等 120 秒。
func (h *handler) emuStart(w http.ResponseWriter, r *http.Request) {
	var req struct {
		AccountID string `json:"accountId"`
	}
	if err := decodeBody(w, r, &req); err != nil {
		writeErr(w, http.StatusBadRequest, err.Error())
		return
	}
	accounts, err := h.m.Accounts(r.Context())
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	acc, ok := findAccount(accounts, strings.TrimSpace(req.AccountID))
	if !ok {
		writeErr(w, http.StatusNotFound, "没有这个账号")
		return
	}
	index := strings.TrimSpace(acc.Emu)
	if index == "" {
		writeErr(w, http.StatusBadRequest, "账号没有配置模拟器实例号（emu）")
		return
	}
	mgr, err := h.mumuManagerPath()
	if err != nil {
		writeErr(w, http.StatusBadRequest, err.Error())
		return
	}
	if emuStarted(r.Context(), mgr, index) {
		writeJSON(w, http.StatusOK, map[string]any{"ok": true, "already": true, "emu": index})
		return
	}
	if _, err := emuControl(r.Context(), mgr, "launch", index); err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	deadline := time.Now().Add(120 * time.Second)
	for time.Now().Before(deadline) {
		if r.Context().Err() != nil {
			return
		}
		time.Sleep(3 * time.Second)
		if emuStarted(r.Context(), mgr, index) {
			writeJSON(w, http.StatusOK, map[string]any{"ok": true, "started": true, "emu": index})
			return
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"ok":    false,
		"emu":   index,
		"error": "MuMu 实例没有在 120 秒内完成启动（可能需要在机端桌面手动点一次启动）",
	})
}

// emuStop 关闭账号对应的模拟器实例。
func (h *handler) emuStop(w http.ResponseWriter, r *http.Request) {
	var req struct {
		AccountID string `json:"accountId"`
	}
	if err := decodeBody(w, r, &req); err != nil {
		writeErr(w, http.StatusBadRequest, err.Error())
		return
	}
	accounts, err := h.m.Accounts(r.Context())
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	acc, ok := findAccount(accounts, strings.TrimSpace(req.AccountID))
	if !ok {
		writeErr(w, http.StatusNotFound, "没有这个账号")
		return
	}
	index := strings.TrimSpace(acc.Emu)
	if index == "" {
		writeErr(w, http.StatusBadRequest, "账号没有配置模拟器实例号（emu）")
		return
	}
	mgr, err := h.mumuManagerPath()
	if err != nil {
		writeErr(w, http.StatusBadRequest, err.Error())
		return
	}
	if _, err := emuControl(r.Context(), mgr, "shutdown", index); err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "emu": index})
}
