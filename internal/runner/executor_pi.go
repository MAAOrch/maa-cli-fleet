package runner

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

// piCommandFunc 便于测试替换真实的 MaaPiCli 子进程。
type piCommandFunc func(ctx context.Context, exe string, args []string, dir string) ([]byte, error)

// piExecutor 是 MaaPiCli 这条线的执行器：用 `-d` 跑项目根下的
// config/maa_pi_config.json。MaaPiCli 以 exe 所在目录为项目根，所以
// exe 必须放在 pi/<project>/ 里（interface.json / resource / config 同目录）。
//
// 支持按 Job 动态覆写 config/maa_pi_config.json（任务名 / ADB 地址 / 设备名 / 资源名），
// 并通过互斥锁串行化同一项目根下的并发调用，避免配置文件互相覆盖。
type piExecutor struct {
	mu     sync.Mutex
	exe    string
	dir    string
	logDir string
	run    piCommandFunc
}

func newPiExecutor(exe, logDir string) *piExecutor {
	return &piExecutor{exe: exe, dir: filepath.Dir(exe), logDir: logDir, run: runPiCommand}
}

func (e *piExecutor) Name() string { return "maa-pi-cli" }

func (e *piExecutor) Run(ctx context.Context, job Job) (Result, error) {
	if err := ctx.Err(); err != nil {
		return Result{}, err
	}
	if e == nil || strings.TrimSpace(e.exe) == "" {
		return Result{}, fmt.Errorf("pi executor exe is empty")
	}
	start := time.Now()
	run := e.run
	if run == nil {
		run = runPiCommand
	}
	dir := e.dir
	if dir == "" {
		dir = filepath.Dir(e.exe)
	}

	e.mu.Lock()
	defer e.mu.Unlock()
	if err := ctx.Err(); err != nil {
		return Result{}, err
	}
	if err := patchPiConfig(dir, job); err != nil {
		return Result{OK: false, Note: err.Error()}, err
	}

	out, err := run(ctx, e.exe, []string{"-d"}, dir)
	logPath := e.writeLog(job, out)
	res := Result{
		OK:      err == nil,
		OutFile: logPath,
		Note:    piTail(out),
		CostSec: int(time.Since(start).Seconds()),
	}
	if err != nil {
		res.OK = false
		res.Note = strings.TrimSpace(err.Error() + " " + res.Note)
	}
	return res, err
}

// patchPiConfig 在 config/maa_pi_config.json 存在且 Job 携带覆盖参数时原子更新配置。
// 若配置文件不存在（如单元测试使用假命令），则静默跳过保持向后兼容。
func patchPiConfig(dir string, job Job) error {
	taskName := strings.TrimSpace(job.TaskFile)
	if v, ok := job.Params["task"].(string); ok && strings.TrimSpace(v) != "" {
		taskName = strings.TrimSpace(v)
	}
	adbAddr := strings.TrimSpace(job.Device.ADBAddr)
	if v, ok := job.Params["adb_address"].(string); ok && strings.TrimSpace(v) != "" {
		adbAddr = strings.TrimSpace(v)
	}
	adbName, _ := job.Params["adb_name"].(string)
	adbName = strings.TrimSpace(adbName)
	resourceName, _ := job.Params["resource"].(string)
	resourceName = strings.TrimSpace(resourceName)

	if taskName == "" && adbAddr == "" && adbName == "" && resourceName == "" {
		return nil
	}
	cfgPath := filepath.Join(dir, "config", "maa_pi_config.json")
	raw, err := os.ReadFile(cfgPath)
	if err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		return fmt.Errorf("read maa_pi_config.json: %w", err)
	}
	var cfg map[string]any
	if err := json.Unmarshal(raw, &cfg); err != nil {
		return fmt.Errorf("parse maa_pi_config.json: %w", err)
	}
	if cfg == nil {
		cfg = map[string]any{}
	}
	if adbAddr != "" || adbName != "" {
		adb, _ := cfg["adb"].(map[string]any)
		if adb == nil {
			adb = map[string]any{}
		}
		if adbAddr != "" {
			adb["address"] = adbAddr
		}
		if adbName != "" {
			adb["name"] = adbName
		}
		cfg["adb"] = adb
	}
	if resourceName != "" {
		cfg["resource"] = resourceName
	}
	if taskName != "" {
		opts := []any{}
		if v, ok := job.Params["option"].([]any); ok {
			opts = v
		}
		cfg["task"] = []map[string]any{{"name": taskName, "option": opts}}
	}
	b, err := json.MarshalIndent(cfg, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal maa_pi_config.json: %w", err)
	}
	b = append(b, '\n')
	tmp := cfgPath + ".tmp"
	if err := os.WriteFile(tmp, b, 0o644); err != nil {
		return fmt.Errorf("write maa_pi_config.json.tmp: %w", err)
	}
	if err := os.Rename(tmp, cfgPath); err != nil {
		_ = os.Remove(tmp)
		return fmt.Errorf("rename maa_pi_config.json: %w", err)
	}
	return nil
}

func (e *piExecutor) writeLog(job Job, out []byte) string {
	dir := e.logDir
	if dir == "" {
		dir = os.TempDir()
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return ""
	}
	name := fmt.Sprintf("pi_%s_%s.out", piLogName(job.Kind), time.Now().Format("0102_150405"))
	path := filepath.Join(dir, name)
	if err := os.WriteFile(path, out, 0o644); err != nil {
		return ""
	}
	return path
}

func piLogName(kind string) string {
	name := strings.NewReplacer(":", "_", "/", "_", "\\", "_", ".", "_").Replace(strings.TrimSpace(kind))
	if name == "" {
		return "pi"
	}
	return name
}

// piTail 取输出末尾几行给 Result.Note，避免把整份 MaaPiCli 日志塞进台账。
func piTail(out []byte) string {
	s := strings.TrimSpace(string(out))
	if s == "" {
		return ""
	}
	lines := strings.Split(s, "\n")
	if len(lines) > 8 {
		lines = lines[len(lines)-8:]
	}
	s = strings.TrimSpace(strings.Join(lines, "\n"))
	if len(s) > 1200 {
		s = s[len(s)-1200:]
	}
	return s
}

func runPiCommand(ctx context.Context, exe string, args []string, dir string) ([]byte, error) {
	cmd := exec.CommandContext(ctx, exe, args...)
	cmd.Dir = dir
	return cmd.CombinedOutput()
}
