package copilotpick

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"time"
)

// SpaBatchAccount matches an entry in config/spa_cn_batch.json.
type SpaBatchAccount struct {
	ID     string `json:"id"`
	Name   string `json:"name"`
	Client string `json:"client"`
	Emu    string `json:"emu"`
	Task   string `json:"task,omitempty"`
}

// SpaBatchConfig matches config/spa_cn_batch.json.
type SpaBatchConfig struct {
	Description string            `json:"description,omitempty"`
	Host        string            `json:"host,omitempty"`
	MasURL      string            `json:"masUrl,omitempty"`
	Accounts    []SpaBatchAccount `json:"accounts"`
}

// SpaStartRequest is the JSON payload for POST /api/spa/start.
type SpaStartRequest struct {
	Mode         string   `json:"mode"`         // "badge" | "score"
	Target       int      `json:"target"`       // 3 (gold) | 6 (rainbow)
	Runs         int      `json:"runs"`         // default 60
	Attempts     int      `json:"attempts"`     // default 0 (unlimited)
	Concurrency  int      `json:"concurrency"`  // default 3
	MaxRetries   int      `json:"maxRetries"`   // default 3
	StallMinutes int      `json:"stallMinutes"` // default 12
	OnlyEmus     []string `json:"onlyEmus"`     // optional whitelist
	SkipEmus     []string `json:"skipEmus"`     // optional skiplist
}

func (m *Manager) spaConfigPath() string {
	return filepath.Join(m.cfg.Root, "config", "spa_cn_batch.json")
}

func (m *Manager) spaBatchRunnerPath() string {
	return filepath.Join(m.cfg.Root, "tools", "spa_batch_runner.py")
}

func (m *Manager) spaBadgeRunnerPath() string {
	return filepath.Join(m.cfg.Root, "tools", "spa_badge_runner.py")
}

func (m *Manager) spaMaaCorePath() string {
	return filepath.Join(m.cfg.Root, "maa_v623", "MaaCore.dll")
}

func (m *Manager) spaLogDir() string {
	return filepath.Join(m.cfg.Root, "logs")
}

func (m *Manager) spaStatusPath() string {
	return filepath.Join(m.spaLogDir(), "spa_batch_status.json")
}

func findSpaPython() string {
	cands := []string{
		`C:\Program Files\Python312\python.exe`,
		`C:\Program Files\Python311\python.exe`,
	}
	for _, c := range cands {
		if st, err := os.Stat(c); err == nil && !st.IsDir() {
			return c
		}
	}
	if p, err := exec.LookPath("python"); err == nil {
		return p
	}
	if p, err := exec.LookPath("python3"); err == nil {
		return p
	}
	return "python"
}

// listSpaPids returns PIDs of active spa_batch_runner.py / spa_badge_runner.py processes.
func listSpaPids(ctx context.Context) []int {
	if runtime.GOOS != "windows" {
		return nil
	}
	cctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	cmd := exec.CommandContext(
		cctx,
		"powershell", "-NoProfile", "-Command",
		`Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like '*spa_*runner.py*' } | Select-Object -ExpandProperty ProcessId`,
	)
	out, err := cmd.Output()
	if err != nil {
		return nil
	}
	var pids []int
	for _, line := range strings.Split(string(out), "\n") {
		s := strings.TrimSpace(line)
		if s == "" {
			continue
		}
		if pid, err := strconv.Atoi(s); err == nil && pid > 0 {
			pids = append(pids, pid)
		}
	}
	return pids
}

func stopSpaPids(ctx context.Context) []int {
	pids := listSpaPids(ctx)
	if len(pids) == 0 {
		return nil
	}
	var stopped []int
	for _, pid := range pids {
		if runtime.GOOS == "windows" {
			_ = exec.CommandContext(ctx, "taskkill", "/PID", strconv.Itoa(pid), "/T", "/F").Run()
			stopped = append(stopped, pid)
		}
	}
	return stopped
}

func (h *handler) spaStatus(w http.ResponseWriter, r *http.Request) {
	var cfg SpaBatchConfig
	cfgErr := ""
	if b, err := os.ReadFile(h.m.spaConfigPath()); err == nil {
		if jerr := json.Unmarshal(b, &cfg); jerr != nil {
			cfgErr = "解析 spa_cn_batch.json 失败：" + jerr.Error()
		}
	} else {
		cfgErr = "未找到 config/spa_cn_batch.json"
	}

	var statusRaw json.RawMessage
	if b, err := os.ReadFile(h.m.spaStatusPath()); err == nil && len(strings.TrimSpace(string(b))) > 0 {
		if json.Valid(b) {
			statusRaw = json.RawMessage(b)
		}
	}

	_, maaErr := os.Stat(h.m.spaMaaCorePath())
	_, runnerErr := os.Stat(h.m.spaBatchRunnerPath())
	pids := listSpaPids(r.Context())

	resp := map[string]any{
		"running":        len(pids) > 0,
		"pids":           pids,
		"maaV623Ready":   maaErr == nil,
		"runnerReady":    runnerErr == nil,
		"configError":    cfgErr,
		"configAccounts": cfg.Accounts,
	}
	if len(statusRaw) > 0 {
		resp["status"] = statusRaw
	} else {
		resp["status"] = nil
	}
	writeJSON(w, http.StatusOK, resp)
}

func (h *handler) spaStart(w http.ResponseWriter, r *http.Request) {
	var req SpaStartRequest
	if err := decodeBody(w, r, &req); err != nil {
		writeErr(w, http.StatusBadRequest, err.Error())
		return
	}
	if _, err := os.Stat(h.m.spaBatchRunnerPath()); err != nil {
		writeErr(w, http.StatusBadRequest, "缺少批量脚本："+h.m.spaBatchRunnerPath())
		return
	}
	if _, err := os.Stat(h.m.spaMaaCorePath()); err != nil {
		writeErr(w, http.StatusBadRequest, "缺少 MAA v6.23+ 核心："+h.m.spaMaaCorePath())
		return
	}
	if pids := listSpaPids(r.Context()); len(pids) > 0 {
		writeErr(w, http.StatusConflict, fmt.Sprintf("卫戍协议批量任务已在运行中（PID %v），请先停止再重启", pids))
		return
	}

	mode := strings.TrimSpace(req.Mode)
	if mode != "badge" && mode != "score" {
		mode = "badge"
	}
	target := req.Target
	if target != 3 && target != 6 {
		target = 3
	}
	runs := req.Runs
	if runs <= 0 || runs > 999 {
		runs = 60
	}
	attempts := req.Attempts
	if attempts < 0 {
		attempts = 0
	}
	conc := req.Concurrency
	if conc <= 0 {
		conc = 3
	}
	if conc > 6 {
		conc = 6
	}
	maxRetries := req.MaxRetries
	if maxRetries < 0 {
		maxRetries = 3
	}
	if maxRetries > 10 {
		maxRetries = 10
	}
	stallMin := req.StallMinutes
	if stallMin <= 0 {
		stallMin = 12
	}

	if err := os.MkdirAll(h.m.spaLogDir(), 0o755); err != nil {
		writeErr(w, http.StatusInternalServerError, "创建日志目录失败："+err.Error())
		return
	}

	py := findSpaPython()
	args := []string{
		"-u", h.m.spaBatchRunnerPath(),
		"--config", h.m.spaConfigPath(),
		"--runner", h.m.spaBadgeRunnerPath(),
		"--python", py,
		"--log-dir", h.m.spaLogDir(),
		"--concurrency", strconv.Itoa(conc),
		"--mode", mode,
		"--target", strconv.Itoa(target),
		"--runs", strconv.Itoa(runs),
		"--attempts", strconv.Itoa(attempts),
		"--max-retries", strconv.Itoa(maxRetries),
		"--stall-minutes", strconv.Itoa(stallMin),
	}
	if h.m.cfg.MumuRoot != "" {
		args = append(args, "--mumu-mgr", filepath.Join(h.m.cfg.MumuRoot, "nx_main", "MuMuManager.exe"))
	}
	if len(req.OnlyEmus) > 0 {
		args = append(args, "--only-emus", strings.Join(cleanEmuList(req.OnlyEmus), ","))
		args = append(args, "--skip-emus", "")
	} else {
		args = append(args, "--skip-emus", strings.Join(cleanEmuList(req.SkipEmus), ","))
	}

	mainLogPath := filepath.Join(h.m.spaLogDir(), "spa_batch_main.log")
	lf, err := os.OpenFile(mainLogPath, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o644)
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "打开批量日志文件失败："+err.Error())
		return
	}

	cmd := exec.Command(py, args...)
	cmd.Dir = h.m.cfg.Root
	cmd.Env = append(os.Environ(), "PYTHONUTF8=1")
	cmd.Stdout = lf
	cmd.Stderr = lf
	if err := cmd.Start(); err != nil {
		_ = lf.Close()
		writeErr(w, http.StatusInternalServerError, "启动卫戍协议批量脚本失败："+err.Error())
		return
	}
	pid := cmd.Process.Pid
	go func() {
		_ = cmd.Wait()
		_ = lf.Close()
	}()

	writeJSON(w, http.StatusOK, map[string]any{
		"ok":  true,
		"pid": pid,
	})
}

func (h *handler) spaStop(w http.ResponseWriter, r *http.Request) {
	stopped := stopSpaPids(r.Context())
	writeJSON(w, http.StatusOK, map[string]any{
		"ok":          true,
		"stoppedPids": stopped,
	})
}

func (h *handler) spaLog(w http.ResponseWriter, r *http.Request) {
	emu := strings.TrimSpace(r.PathValue("emu"))
	var path string
	if emu == "main" {
		path = filepath.Join(h.m.spaLogDir(), "spa_batch_main.log")
	} else {
		n, err := strconv.Atoi(emu)
		if err != nil || n < 0 || n > 999 {
			writeErr(w, http.StatusBadRequest, "非法的模拟器编号")
			return
		}
		path = filepath.Join(h.m.spaLogDir(), fmt.Sprintf("spa_emu%02d.log", n))
	}

	f, err := os.Open(path)
	if err != nil {
		writeErr(w, http.StatusNotFound, "暂无该账号日志："+filepath.Base(path))
		return
	}
	defer f.Close()

	st, err := f.Stat()
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	const maxTail int64 = 64 << 10
	if st.Size() > maxTail {
		_, _ = f.Seek(st.Size()-maxTail, io.SeekStart)
	}
	b, err := io.ReadAll(io.LimitReader(f, maxTail))
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	_, _ = w.Write(b)
}

func cleanEmuList(in []string) []string {
	out := make([]string, 0, len(in))
	for _, s := range in {
		s = strings.TrimSpace(s)
		if _, err := strconv.Atoi(s); err == nil {
			out = append(out, s)
		}
	}
	return out
}
