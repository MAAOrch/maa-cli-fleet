package runner

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
)

// piCommandFunc 便于测试替换真实的 MaaPiCli 子进程。
type piCommandFunc func(ctx context.Context, exe string, args []string, dir string) ([]byte, error)

// piExecutor 是 MaaPiCli 这条线的执行器：用 `-d` 跑项目根下已保存的
// config/maa_pi_config.json。MaaPiCli 以 exe 所在目录为项目根，所以
// exe 必须放在 pi/<project>/ 里（interface.json / resource / config 同目录）。
type piExecutor struct {
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
