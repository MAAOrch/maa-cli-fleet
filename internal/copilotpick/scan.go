package copilotpick

// 干员扫描：用 maa-cli 跑 MAA 的 OperBox 任务，再从 asst.log 里抓 OperBoxInfo 回调。
//
// 为什么不直接读文件：maa-cli 的 OperBox 任务只把结果作为回调发出来，不落盘
// （落盘是 MAA 桌面端小工具的 C# 逻辑）。所以这里跑完任务后 tail asst.log，
// 找最后一条 what=OperBoxInfo 的 SubTaskExtraInfo，把 own_opers 写成
// OperBoxData 再复用 ext.LoadOperBox 解析。

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"github.com/xiachk083-hub/maa-cli-fleet/internal/copilotpick/ext"
	"github.com/xiachk083-hub/maa-cli-fleet/internal/copilotpick/model"
)

// writeMaaProfile 给某个 adb 写隔离 profile（执行器与干员扫描共用）。
func writeMaaProfile(cfg Config, adb, emu string) (string, error) {
	name := profileName(emu)
	dir := filepath.Join(cfg.MaaConfigDir, "profiles")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return "", err
	}
	adbPath := filepath.ToSlash(cfg.AdbPath)
	body := fmt.Sprintf(`[connection]
adb_path = %q
address = %q
config = "General"

[instance_options]
touch_mode = "MaaTouch"
`, adbPath, adb)
	if err := os.WriteFile(filepath.Join(dir, name+".toml"), []byte(body), 0o644); err != nil {
		return "", err
	}
	return name, nil
}

// ensureGameRunning 确保游戏进程在跑（adb monkey 拉起），不负责模拟器开关。
// 返回是否真的执行了拉起。
func ensureGameRunning(ctx context.Context, cfg Config, adb, client string) bool {
	if cfg.AdbPath == "" || adb == "" {
		return false
	}
	pkg := clientPackage(client)
	check := func() bool {
		cctx, cancel := context.WithTimeout(ctx, 15*time.Second)
		defer cancel()
		cmd := exec.CommandContext(cctx, cfg.AdbPath, "-s", adb, "shell", "pidof", pkg)
		cmd.SysProcAttr = hideWindow()
		out, err := cmd.Output()
		return err == nil && strings.TrimSpace(string(out)) != ""
	}
	if check() {
		return false
	}
	cctx, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()
	cmd := exec.CommandContext(cctx, cfg.AdbPath, "-s", adb, "shell", "monkey",
		"-p", pkg, "-c", "android.intent.category.LAUNCHER", "1")
	cmd.SysProcAttr = hideWindow()
	_, _ = cmd.Output()
	time.Sleep(5 * time.Second)
	return true
}

// operBoxScan 是 OperBoxInfo 回调里我们关心的部分。
type operBoxScan struct {
	Done     bool              `json:"done"`
	OwnOpers []json.RawMessage `json:"own_opers"`
}

// parseOperBoxInfo 从 asst.log 文本里找最后一条 OperBoxInfo，返回 details。
func parseOperBoxInfo(logText string) (operBoxScan, error) {
	const marker = "SubTaskExtraInfo "
	var best operBoxScan
	found := false
	for _, line := range strings.Split(logText, "\n") {
		if !strings.Contains(line, `"what":"OperBoxInfo"`) {
			continue
		}
		var start int
		if i := strings.Index(line, marker); i >= 0 {
			start = i + len(marker)
		} else if i := strings.Index(line, "{"); i >= 0 {
			start = i
		} else {
			continue
		}
		var probe struct {
			What    string      `json:"what"`
			Details operBoxScan `json:"details"`
		}
		if err := json.Unmarshal([]byte(line[start:]), &probe); err != nil {
			continue
		}
		best = probe.Details
		found = true
	}
	if !found {
		return best, fmt.Errorf("maa 跑完了，但日志里没有 OperBoxInfo 回调（MaaCore 版本太旧？）")
	}
	return best, nil
}

// parseOperBoxInfoBest 在日志里找“拥有干员最多”的 OperBoxInfo。
//
// 扫描时 MaaCore 可能连续发多条：第一条是完整识别结果，后面可能因为界面
// 不在干员列表而发空结果（own 全 false）。只取最后一条会把好结果覆盖成 0 人。
func parseOperBoxInfoBest(logText string) (operBoxScan, error) {
	const marker = "SubTaskExtraInfo "
	var best operBoxScan
	found := false
	for _, line := range strings.Split(logText, "\n") {
		if !strings.Contains(line, `"what":"OperBoxInfo"`) {
			continue
		}
		var start int
		if i := strings.Index(line, marker); i >= 0 {
			start = i + len(marker)
		} else if i := strings.Index(line, "{"); i >= 0 {
			start = i
		} else {
			continue
		}
		var probe struct {
			What    string      `json:"what"`
			Details operBoxScan `json:"details"`
		}
		if err := json.Unmarshal([]byte(line[start:]), &probe); err != nil {
			continue
		}
		// 优先 done=true 的完整结果；同为 done 时取拥有干员最多的。
		better := !found ||
			(probe.Details.Done && !best.Done) ||
			(probe.Details.Done == best.Done && len(probe.Details.OwnOpers) > len(best.OwnOpers))
		if better {
			best = probe.Details
			found = true
		}
	}
	if !found {
		return best, fmt.Errorf("maa 跑完了，但日志里没有 OperBoxInfo 回调（MaaCore 版本太旧？）")
	}
	return best, nil
}

// forceToMain 用 adb 点两下把游戏弄回主界面。
//
// MAA 的 startup 从编队/关卡界面回主界面并不稳；OperBox 任务又要求真的在
// 干员列表页，否则会识别出 0 人。主界面入口固定是左上角「主界面」图标 +
// 弹层里的「首页」，分辨率按 1280x720 设计，这里按实际 wm size 等比缩放。
func forceToMain(ctx context.Context, cfg Config, adb string) {
	if cfg.AdbPath == "" || adb == "" {
		return
	}
	run := func(args ...string) string {
		cctx, cancel := context.WithTimeout(ctx, 10*time.Second)
		defer cancel()
		cmd := exec.CommandContext(cctx, cfg.AdbPath, append([]string{"-s", adb}, args...)...)
		cmd.SysProcAttr = hideWindow()
		out, _ := cmd.Output()
		return string(out)
	}
	scaleX, scaleY := 1.0, 1.0
	if out := run("shell", "wm", "size"); out != "" {
		var w, h int
		for _, line := range strings.Split(out, "\n") {
			if !strings.Contains(line, "Physical size") {
				continue
			}
			if _, err := fmt.Sscanf(strings.TrimSpace(line), "Physical size: %dx%d", &w, &h); err == nil && w > 0 && h > 0 {
				scaleX = float64(w) / 1280.0
				scaleY = float64(h) / 720.0
			}
		}
	}
	tap := func(x, y int) {
		rx := int(float64(x) * scaleX)
		ry := int(float64(y) * scaleY)
		run("shell", "input", "tap", fmt.Sprint(rx), fmt.Sprint(ry))
	}
	tap(238, 38) // 左上角主界面图标（打开菜单）
	time.Sleep(1200 * time.Millisecond)
	tap(90, 240) // 菜单里的「首页」
	time.Sleep(1500 * time.Millisecond)
}

// ScanOperBox 对指定账号跑一次干员识别，返回名册与落盘路径。
func (m *Manager) ScanOperBox(ctx context.Context, accountID string) (model.Roster, string, error) {
	if r := m.CurrentRun(); r != nil && !r.IsDone() {
		return model.Roster{}, "", fmt.Errorf("有执行中的任务，先等它结束或停止再扫描")
	}
	accounts, err := m.Accounts(ctx)
	if err != nil {
		return model.Roster{}, "", err
	}
	acc, ok := findAccount(accounts, accountID)
	if !ok {
		return model.Roster{}, "", fmt.Errorf("账号 %s 不存在", accountID)
	}
	if strings.TrimSpace(acc.Adb) == "" {
		return model.Roster{}, "", fmt.Errorf("账号 %s 解析不出 adb 地址", accountID)
	}
	client := strings.TrimSpace(acc.Client)
	if client == "" {
		client = m.cfg.DefaultClient
	}
	profile, err := writeMaaProfile(m.cfg, acc.Adb, acc.Emu)
	if err != nil {
		return model.Roster{}, "", fmt.Errorf("写 MAA 连接配置失败：%w", err)
	}

	tasksDir := filepath.Join(m.cfg.MaaConfigDir, "tasks")
	if err := os.MkdirAll(tasksDir, 0o755); err != nil {
		return model.Roster{}, "", err
	}
	taskFile := filepath.Join(tasksDir, "operbox.json")
	if err := os.WriteFile(taskFile, []byte(`{"tasks":[{"type":"OperBox","params":{}}]}`), 0o644); err != nil {
		return model.Roster{}, "", fmt.Errorf("写 OperBox 任务文件失败：%w", err)
	}

	if ensureGameRunning(ctx, m.cfg, acc.Adb, client) {
		// 刚用 adb 拉起游戏，给模拟器几秒钟进入前台
	}

	run := func(timeout time.Duration, args ...string) (string, error) {
		cctx, cancel := context.WithTimeout(ctx, timeout)
		defer cancel()
		cmd := exec.CommandContext(cctx, m.cfg.MaaExe, args...)
		cmd.Env = m.cfg.Env()
		cmd.SysProcAttr = hideWindow()
		var buf bytes.Buffer
		cmd.Stdout = &buf
		cmd.Stderr = &buf
		err := cmd.Run()
		return buf.String(), err
	}

	// 单次扫描：startup + OperBox，从日志里取拥有干员最多的一条回调。
	scanOnce := func() (operBoxScan, error) {
		// 扫描前先确定把游戏送回主界面：MAA 的 OperBox 任务默认从主界面导航到
		// 干员页，停在编队/关卡界面时会把编队列表当干员列表，识别出个位数的假结果。
		forceToMain(ctx, m.cfg, acc.Adb)
		out, err := run(4*time.Minute, "startup", client, "-p", profile, "--batch", "--no-summary")
		if err != nil {
			return operBoxScan{}, fmt.Errorf("maa startup 失败：%v %s", err, lastLines(out, 6))
		}
		tail := newLogTail(m.cfg.AsstLogPath())
		tail.SkipAll()
		out, runErr := run(3*time.Minute, "run", "operbox", "-p", profile, "--batch", "--no-summary")
		logText := tail.Read()
		info, err := parseOperBoxInfoBest(logText)
		if err != nil {
			msg := err.Error()
			if strings.Contains(logText, `"taskchain":"OperBox"`) && strings.Contains(logText, "TaskChainError") {
				msg += "\nMaaCore 报 OperBox 任务链错误：确认游戏停在干员界面、且这个 MAA 目录没被其它进程占用"
			}
			if runErr != nil {
				msg += fmt.Sprintf("（maa rc=%v）", runErr)
			}
			return operBoxScan{}, fmt.Errorf("%s\n%s", msg, lastLines(out, 8))
		}
		if !info.Done {
			return operBoxScan{}, fmt.Errorf("干员识别没有完成（日志里最后一条 done=false）")
		}
		if len(info.OwnOpers) < 40 {
			return operBoxScan{}, fmt.Errorf("干员识别结果异常（只有 %d 人）：游戏可能不在干员列表页", len(info.OwnOpers))
		}
		return info, nil
	}

	info, err := scanOnce()
	if err != nil {
		// 常见原因：游戏停在编队/关卡界面，MAA 把编队列表当成干员列表识别出错。
		// 再强制回主界面重扫一次。
		retryInfo, retryErr := scanOnce()
		if retryErr == nil {
			info, err = retryInfo, nil
		} else {
			err = fmt.Errorf("%v\n重试（强制回主界面）仍失败：%v", err, retryErr)
		}
	}
	if err != nil {
		return model.Roster{}, "", err
	}

	if err := os.MkdirAll(m.cfg.MaaDataDir, 0o755); err != nil {
		return model.Roster{}, "", err
	}
	path := filepath.Join(m.cfg.MaaDataDir, "scan_OperBoxData.json")
	payload := map[string]any{
		"done":      true,
		"source":    "local",
		"syncTime":  time.Now().Format("2006-01-02T15:04:05Z07:00"),
		"own_opers": info.OwnOpers,
	}
	b, err := json.Marshal(payload)
	if err != nil {
		return model.Roster{}, "", err
	}
	if err := os.WriteFile(path, b, 0o644); err != nil {
		return model.Roster{}, "", fmt.Errorf("写扫描结果失败：%w", err)
	}

	roster, err := ext.LoadOperBox(path)
	if err != nil {
		return model.Roster{}, "", fmt.Errorf("解析扫描结果失败：%w", err)
	}
	m.SetRoster(roster)
	return roster, path, nil
}
