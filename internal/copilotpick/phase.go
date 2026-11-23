package copilotpick

// 阶段分流执行引擎：把多关作业集按活动地图页（第 1 阶段普通图 / 第 2 阶段 EX 图 / 第 3 阶段 S 图）
// 自动分组。同一阶段内的关卡全部交给 MAA 的 copilot_list（onMap 模式）自己找关连续推完，
// 仅在进入新阶段时执行一次页面级 PI 导航（进入普通图 / 进入EX图 / 进入S图），不再逐关写导航。

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

// PhaseNavTask 返回该阶段对应的页面级 PI 导航任务名。
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
	cand := filepath.Join(c.PiDirPath(), project, "MaaPiCli.exe")
	if st, err := os.Stat(cand); err == nil && !st.IsDir() {
		return cand
	}
	return ""
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
	project := strings.TrimSpace(r.opts.PiProject)
	if project == "" {
		project = defaultPhasePiProject
	}
	exe := r.m.cfg.resolvePiExe(project)
	if exe == "" {
		return fmt.Errorf("未找到 MaaPiCli.exe（可在配置指定 piExe 或 piDir，默认 %s）", filepath.Join(r.m.cfg.PiDirPath(), project, "MaaPiCli.exe"))
	}
	dir := filepath.Dir(exe)

	piCfgMu.Lock()
	defer piCfgMu.Unlock()

	if err := patchPiNavConfig(dir, r.adb, task); err != nil {
		return fmt.Errorf("更新 PI 导航配置失败：%w", err)
	}
	cctx, cancel := context.WithTimeout(ctx, 3*time.Minute)
	defer cancel()
	cmd := exec.CommandContext(cctx, exe, "-d")
	cmd.Dir = dir
	cmd.SysProcAttr = hideWindow()
	out, err := cmd.CombinedOutput()
	if err != nil {
		return fmt.Errorf("PI 导航 %s 失败：%v %s", task, err, lastLines(string(out), 4))
	}
	return nil
}

// patchPiNavConfig 原子更新 dir/config/maa_pi_config.json，保留既有 adb_path / resource / controller。
func patchPiNavConfig(dir, adbAddr, taskName string) error {
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
	if adbAddr != "" {
		adb, _ := cfg["adb"].(map[string]any)
		if adb == nil {
			adb = map[string]any{}
		}
		adb["address"] = adbAddr
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
