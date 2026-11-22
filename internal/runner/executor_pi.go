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

// piExecutor 是 MaaPiCli 这条线的执行器。
// 支持动态 Kind pi.<project>.<entry>：按 job 生成 config/maa_pi_config.json 后调用 -d。
type piExecutor struct {
	exe    string
	dir    string
	logDir string
	run    piCommandFunc
	locks  sync.Map // project -> *sync.Mutex
}

func newPiExecutor(exe, logDir string) *piExecutor {
	return &piExecutor{
		exe:    exe,
		dir:    filepath.Dir(exe),
		logDir: logDir,
		run:    runPiCommand,
	}
}

func (e *piExecutor) Name() string { return "maa-pi-cli" }

func (e *piExecutor) Run(ctx context.Context, job Job) (Result, error) {
	if err := ctx.Err(); err != nil {
		return Result{}, err
	}
	if e == nil || strings.TrimSpace(e.exe) == "" {
		return Result{}, fmt.Errorf("pi executor exe is empty")
	}

	project, entry := parsePiKind(job.Kind)
	if entry == "" {
		entry = strings.TrimSpace(job.TaskFile)
	}
	if entry == "" {
		return Result{}, fmt.Errorf("pi job missing entry: kind=%q taskFile=%q", job.Kind, job.TaskFile)
	}

	unlock := e.lockProject(project)
	defer unlock()

	if err := e.writeConfig(project, entry, job.Params); err != nil {
		return Result{}, fmt.Errorf("write pi config: %w", err)
	}

	start := time.Now()
	dir := e.dir
	if dir == "" {
		dir = filepath.Dir(e.exe)
	}
	run := e.run
	if run == nil {
		run = runPiCommand
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

func parsePiKind(kind string) (project, entry string) {
	parts := strings.SplitN(strings.TrimSpace(kind), ".", 3)
	if len(parts) < 3 || parts[0] != "pi" {
		return "", ""
	}
	return parts[1], parts[2]
}

func (e *piExecutor) writeConfig(project, entry string, params map[string]any) error {
	if params == nil {
		params = map[string]any{}
	}
	cfg := map[string]any{
		"controller": map[string]any{"name": "Android"},
		"resource":   "Demo",
		"task": []any{
			map[string]any{
				"name":   entry,
				"option": params,
			},
		},
	}
	b, err := json.MarshalIndent(cfg, "", "  ")
	if err != nil {
		return err
	}
	dir := filepath.Join(e.dir, "config")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	path := filepath.Join(dir, "maa_pi_config.json")
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, b, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

func (e *piExecutor) lockProject(project string) func() {
	if project == "" {
		project = "_default"
	}
	v, _ := e.locks.LoadOrStore(project, &sync.Mutex{})
	mu := v.(*sync.Mutex)
	mu.Lock()
	return mu.Unlock
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
