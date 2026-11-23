package copilotpick

// 阶段分流执行引擎：把多关作业集按活动地图页（第 1 阶段普通图 / 第 2 阶段 EX 图 / 第 3 阶段 S 图）
// 自动分组。同一阶段内的关卡全部交给 MAA 的 copilot_list（onMap 模式）自己找关连续推完，
// 仅在进入新阶段时执行一次页面级 PI 导航（进入普通图 / 进入EX图 / 进入S图，或用户导入的自定义导航）。

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/xiachk083-hub/maa-cli-fleet/internal/copilotpick/model"
)

const defaultPhasePiProject = "arknights-event"

var (
	reStageEX = regexp.MustCompile(`(?i)(^|[-_])ex([-_]?\d|$)`)
	reStageS  = regexp.MustCompile(`(?i)(^|[-_])s([-_]?\d+$)`)
	rePiLogTS = regexp.MustCompile(`\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\.\d+\]`)
	piCfgMu   sync.Mutex
)

// InferStagePhase 按关卡代号或作业站 stageName 推断所属活动地图页：main | ex | s。
func InferStagePhase(code, stageName string) string {
	for _, raw := range []string{code, stageName} {
		s := strings.TrimSpace(raw)
		if s == "" {
			continue
		}
		if reStageEX.MatchString(s) {
			return "ex"
		}
		if reStageS.MatchString(s) {
			return "s"
		}
	}
	return "main"
}

// NormalizePhase 归一化阶段名。
func NormalizePhase(phase string) string {
	switch strings.ToLower(strings.TrimSpace(phase)) {
	case "main", "normal", "1", "phase1":
		return "main"
	case "ex", "2", "phase2":
		return "ex"
	case "s", "3", "phase3":
		return "s"
	default:
		return strings.ToLower(strings.TrimSpace(phase))
	}
}

// PhaseOrder 返回阶段执行顺序：第 1 阶段普通图 → 第 2 阶段 EX 图 → 第 3 阶段 S 图。
func PhaseOrder(phase string) int {
	switch NormalizePhase(phase) {
	case "main":
		return 1
	case "ex":
		return 2
	case "s":
		return 3
	default:
		return 4
	}
}

// PhaseLabel 返回阶段的人类可读名称。
func PhaseLabel(phase string) string {
	switch NormalizePhase(phase) {
	case "main":
		return "第 1 阶段（普通图）"
	case "ex":
		return "第 2 阶段（EX 图）"
	case "s":
		return "第 3 阶段（S 图）"
	default:
		return "阶段 " + phase
	}
}

// PhaseNavTask 返回该阶段对应的默认页面级 PI 导航任务名。
func PhaseNavTask(phase string) string {
	switch NormalizePhase(phase) {
	case "main":
		return "进入普通图"
	case "ex":
		return "进入EX图"
	case "s":
		return "进入S图"
	default:
		return ""
	}
}

// GroupStagesByPhase 为缺失 Phase 的关卡自动补齐阶段，并按阶段顺序稳定分组，
// 保证同一地图页的关卡集中连续执行，阶段内原有顺序保持不变。
func GroupStagesByPhase(stages []model.StagePlan) []model.StagePlan {
	out := make([]model.StagePlan, len(stages))
	for i, st := range stages {
		p := NormalizePhase(st.Phase)
		if p == "" {
			p = InferStagePhase(st.StageCode, st.StageName)
		}
		st.Phase = p
		out[i] = st
	}
	sort.SliceStable(out, func(i, j int) bool {
		return PhaseOrder(out[i].Phase) < PhaseOrder(out[j].Phase)
	})
	return out
}

// resolvePiExe 解析 MaaPiCli.exe 路径。
// 优先顺序：COPILOT_PI_EXE > 配置 piExe > <piDir 同级>/runtime/maa_cli_event/MaaPiCli.exe >
// <root>/runtime/maa_cli_event/MaaPiCli.exe > <piDir>/<project>/MaaPiCli.exe。
func (c Config) resolvePiExe(project string) string {
	if p := strings.TrimSpace(os.Getenv("COPILOT_PI_EXE")); p != "" {
		return p
	}
	if p := strings.TrimSpace(c.PiExe); p != "" {
		return p
	}
	if project == "" {
		project = defaultPhasePiProject
	}
	piDir := c.PiDirPath()
	cands := []string{
		filepath.Join(filepath.Dir(piDir), "runtime", "maa_cli_event", "MaaPiCli.exe"),
		filepath.Join(c.Root, "runtime", "maa_cli_event", "MaaPiCli.exe"),
		filepath.Join(piDir, project, "MaaPiCli.exe"),
	}
	for _, cand := range cands {
		if cand == "" {
			continue
		}
		if st, err := os.Stat(cand); err == nil && !st.IsDir() {
			return cand
		}
	}
	return ""
}

// RunPiNavTask 直接调用 MaaFramework（MaaPiCli.exe）在指定模拟器上执行一条导航任务。
// 执行前同步 interface.json 并覆写 maa_pi_config.json；结束后按 debug/maafw.log 的
// Tasker.Task.Succeeded / Failed 判定真结果（避免 MaaPiCli stdout 假成功）。
func (m *Manager) RunPiNavTask(ctx context.Context, adbAddr, project, taskName string) (string, error) {
	if strings.TrimSpace(taskName) == "" {
		return "", fmt.Errorf("缺少导航任务名")
	}
	if strings.TrimSpace(project) == "" {
		project = defaultPhasePiProject
	}
	exe := m.cfg.resolvePiExe(project)
	if exe == "" {
		return "", fmt.Errorf("未找到 MaaPiCli.exe（请确认已执行 pi/%s/setup_pi.ps1，或在配置指定 piExe/piDir）", project)
	}
	dir := filepath.Dir(exe)

	piCfgMu.Lock()
	defer piCfgMu.Unlock()

	// 1. 同步最新的 interface.json（包含刚通过 Web GUI 导入的导航任务）到运行目录
	srcIface := filepath.Join(m.cfg.PiDirPath(), project, "interface.json")
	dstIface := filepath.Join(dir, "interface.json")
	if b, err := os.ReadFile(srcIface); err == nil {
		if absSrc, _ := filepath.Abs(srcIface); absSrc != "" {
			if absDst, _ := filepath.Abs(dstIface); absSrc != absDst {
				_ = os.WriteFile(dstIface, b, 0o644)
			}
		}
	}

	// 2. 更新运行目录的 config/maa_pi_config.json（保留既有 adb_path/name/resource，覆写 address 与 task）
	if err := patchPiNavConfig(dir, adbAddr, m.cfg.AdbPath, taskName); err != nil {
		return "", fmt.Errorf("更新 PI 导航配置失败：%w", err)
	}

	since := time.Now().Add(-2 * time.Second)
	cctx, cancel := context.WithTimeout(ctx, 3*time.Minute)
	defer cancel()
	cmd := exec.CommandContext(cctx, exe, "-d")
	cmd.Dir = dir
	cmd.SysProcAttr = hideWindow()
	out, err := cmd.CombinedOutput()
	tail := lastLines(string(out), 6)
	if err != nil {
		return tail, fmt.Errorf("PI 导航 %s 执行出错：%v %s", taskName, err, tail)
	}

	// 3. 按 debug/maafw.log 校验真实成败（防止 MaaPiCli 超时失败却退出码 0）
	logPath := filepath.Join(dir, "debug", "maafw.log")
	if ok, found := judgePiLog(logPath, since); found {
		if !ok {
			return tail, fmt.Errorf("PI 导航 %s 未能到达目标界面（maafw.log 报告 Tasker.Task.Failed）", taskName)
		}
	}
	return tail, nil
}

// judgePiLog 从 maafw.log 读取 since 之后的 Tasker.Task.Succeeded / Failed 判定结果。
func judgePiLog(logPath string, since time.Time) (succeeded bool, found bool) {
	b, err := os.ReadFile(logPath)
	if err != nil {
		return false, false
	}
	for _, line := range strings.Split(string(b), "\n") {
		if !strings.Contains(line, "Tasker.Task.") {
			continue
		}
		if m := rePiLogTS.FindStringSubmatch(line); len(m) == 2 {
			if ts, err := time.ParseInLocation("2006-01-02 15:04:05", m[1], time.Local); err == nil {
				if ts.Before(since.Truncate(time.Second)) {
					continue
				}
			}
		}
		if strings.Contains(line, "[msg=Tasker.Task.Succeeded]") {
			succeeded, found = true, true
		} else if strings.Contains(line, "[msg=Tasker.Task.Failed]") {
			succeeded, found = false, true
		}
	}
	return succeeded, found
}

// phaseNavTaskFor 返回本次运行在指定阶段应执行的 PI 导航任务名（优先使用用户绑定的导入导航）。
func (r *Run) phaseNavTaskFor(phase string) string {
	p := NormalizePhase(phase)
	if r.opts.PhaseNavTasks != nil {
		if custom := strings.TrimSpace(r.opts.PhaseNavTasks[p]); custom != "" {
			return custom
		}
		if custom := strings.TrimSpace(r.opts.PhaseNavTasks[phase]); custom != "" {
			return custom
		}
	}
	return PhaseNavTask(p)
}

// navigatePhase 在切换活动地图页时执行一次页面级 PI 导航。
func (r *Run) navigatePhase(ctx context.Context, phase string) error {
	task := r.phaseNavTaskFor(phase)
	if task == "" {
		return fmt.Errorf("未知阶段 %q，无对应页面导航任务", phase)
	}
	_, err := r.m.RunPiNavTask(ctx, r.adb, r.opts.PiProject, task)
	return err
}

// patchPiNavConfig 原子更新 dir/config/maa_pi_config.json，保留既有 adb_path / name / resource / controller。
func patchPiNavConfig(dir, adbAddr, fallbackAdbPath, taskName string) error {
	cfgDir := filepath.Join(dir, "config")
	if err := os.MkdirAll(cfgDir, 0o755); err != nil {
		return err
	}
	cfgPath := filepath.Join(cfgDir, "maa_pi_config.json")
	cfg := map[string]any{}
	if raw, err := os.ReadFile(cfgPath); err == nil {
		_ = json.Unmarshal(raw, &cfg)
	}
	if cfg == nil {
		cfg = map[string]any{}
	}
	if _, ok := cfg["controller"]; !ok {
		cfg["controller"] = map[string]any{"name": "Android"}
	}
	if _, ok := cfg["resource"]; !ok {
		cfg["resource"] = "Arknights"
	}
	if adbAddr != "" || fallbackAdbPath != "" {
		adb, _ := cfg["adb"].(map[string]any)
		if adb == nil {
			adb = map[string]any{}
		}
		if adbAddr != "" {
			adb["address"] = adbAddr
		}
		if curPath, _ := adb["adb_path"].(string); strings.TrimSpace(curPath) == "" && fallbackAdbPath != "" {
			adb["adb_path"] = filepath.ToSlash(fallbackAdbPath)
		}
		cfg["adb"] = adb
	}
	cfg["task"] = []map[string]any{{"name": taskName, "option": []any{}}}
	b, err := json.MarshalIndent(cfg, "", "  ")
	if err != nil {
		return err
	}
	b = append(b, '\n')
	tmp := cfgPath + ".tmp"
	if err := os.WriteFile(tmp, b, 0o644); err != nil {
		return err
	}
	if err := os.Rename(tmp, cfgPath); err != nil {
		_ = os.Remove(tmp)
		return err
	}
	return nil
}
