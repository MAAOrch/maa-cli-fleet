package runner

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
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
// 支持动态 Kind pi.<project>.<entry>：按 Job 合并/生成 config/maa_pi_config.json
// （任务名 / ADB 地址 / 设备名 / 资源名 / option），通过按项目互斥锁串行化后调用 `-d`。
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
	if tf := strings.TrimSpace(job.TaskFile); tf != "" {
		entry = tf
	}
	if v, ok := job.Params["task"].(string); ok && strings.TrimSpace(v) != "" {
		entry = strings.TrimSpace(v)
	}
	if entry == "" {
		return Result{}, fmt.Errorf("pi job missing entry: kind=%q taskFile=%q", job.Kind, job.TaskFile)
	}

	dir := e.dir
	if dir == "" {
		dir = filepath.Dir(e.exe)
	}

	unlock := e.lockProject(project)
	defer unlock()

	if err := ctx.Err(); err != nil {
		return Result{}, err
	}
	if err := e.writeConfig(dir, project, entry, job); err != nil {
		return Result{OK: false, Note: err.Error()}, fmt.Errorf("write pi config: %w", err)
	}

	start := time.Now()
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

// writeConfig 原子更新 dir/config/maa_pi_config.json。
// 若已有配置文件则保留既有字段（如 adb_path / controller / resource），按 Job 参数覆写。
func (e *piExecutor) writeConfig(dir, project, entry string, job Job) error {
	cfgDir := filepath.Join(dir, "config")
	if err := os.MkdirAll(cfgDir, 0o755); err != nil {
		return err
	}
	path := filepath.Join(cfgDir, "maa_pi_config.json")

	cfg := map[string]any{}
	if raw, err := os.ReadFile(path); err == nil {
		_ = json.Unmarshal(raw, &cfg)
	}
	if cfg == nil {
		cfg = map[string]any{}
	}
	if _, ok := cfg["controller"]; !ok {
		cfg["controller"] = map[string]any{"name": "Android"}
	}

	resourceName, _ := job.Params["resource"].(string)
	resourceName = strings.TrimSpace(resourceName)
	if resourceName != "" {
		cfg["resource"] = resourceName
	} else if cur, _ := cfg["resource"].(string); strings.TrimSpace(cur) == "" {
		res := loadPiResource(project)
		if res == "" {
			res = "Demo"
		}
		cfg["resource"] = res
	}

	adbAddr := strings.TrimSpace(job.Device.ADBAddr)
	if v, ok := job.Params["adb_address"].(string); ok && strings.TrimSpace(v) != "" {
		adbAddr = strings.TrimSpace(v)
	}
	adbName, _ := job.Params["adb_name"].(string)
	adbName = strings.TrimSpace(adbName)
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

	var opt any = map[string]any{}
	if job.Params != nil {
		if v, ok := job.Params["option"]; ok {
			opt = v
		} else {
			opt = job.Params
		}
	}
	cfg["task"] = []any{
		map[string]any{
			"name":   entry,
			"option": opt,
		},
	}

	b, err := json.MarshalIndent(cfg, "", "  ")
	if err != nil {
		return err
	}
	b = append(b, '\n')
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, b, 0o644); err != nil {
		return err
	}
	if err := os.Rename(tmp, path); err != nil {
		_ = os.Remove(tmp)
		return err
	}
	return nil
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

// loadPiResource 从 pi/<project>/interface.json 读取第一个 resource.name。
func loadPiResource(project string) string {
	if project == "" {
		return ""
	}
	// 优先在工作区 pi/ 下查找（测试/开发）
	candidates := []string{
		filepath.Join("pi", project, "interface.json"),
		filepath.Join(".", "pi", project, "interface.json"),
	}
	for _, p := range candidates {
		b, err := os.ReadFile(p)
		if err != nil {
			continue
		}
		var iface struct {
			Resource []struct {
				Name string `json:"name"`
			} `json:"resource"`
		}
		if err := json.Unmarshal(b, &iface); err != nil {
			continue
		}
		if len(iface.Resource) > 0 && iface.Resource[0].Name != "" {
			return iface.Resource[0].Name
		}
	}
	return ""
}

// PiProject 描述一个可用的 PI 项目。
type PiProject struct {
	Name     string
	Resource string
	Entries  []string
}

// ScanPiProjects 扫描工作区 pi/ 目录，返回带 interface.json 的项目及其 entry。
func ScanPiProjects() []PiProject {
	var out []PiProject
	root := "pi"
	entries, err := os.ReadDir(root)
	if err != nil {
		return out
	}
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		name := e.Name()
		ifacePath := filepath.Join(root, name, "interface.json")
		b, err := os.ReadFile(ifacePath)
		if err != nil {
			continue
		}
		var iface struct {
			Resource []struct {
				Name string `json:"name"`
			} `json:"resource"`
			Task []struct {
				Name string `json:"name"`
			} `json:"task"`
		}
		if err := json.Unmarshal(b, &iface); err != nil {
			continue
		}
		res := ""
		if len(iface.Resource) > 0 {
			res = iface.Resource[0].Name
		}
		var tasks []string
		for _, t := range iface.Task {
			if t.Name != "" {
				tasks = append(tasks, t.Name)
			}
		}
		out = append(out, PiProject{Name: name, Resource: res, Entries: tasks})
	}
	return out
}

// LogPiProjects 启动时打印发现的 PI 项目。
func LogPiProjects() {
	projects := ScanPiProjects()
	if len(projects) == 0 {
		log.Println("[pi] 未发现 pi/ 下的项目")
		return
	}
	for _, p := range projects {
		log.Printf("[pi] project=%s resource=%s entries=%d %v", p.Name, p.Resource, len(p.Entries), p.Entries)
	}
}
