package copilotpick

// 一键全自动推活动 API（POST /api/auto-event/start）与多模拟器并发活跃列表（GET /api/runs/active）。
//
// 流程：选账号 + 选关卡/阶段 → 可选自动扫干员名册 → 自动为每关匹配最优作业 →
// 按活动地图页（main → ex → s）自动分流，开启 PhaseNav + OnMap 一次启动。

import (
	"context"
	"fmt"
	"net/http"
	"strings"

	"github.com/xiachk083-hub/maa-cli-fleet/internal/copilotpick/model"
)

type autoEventRequest struct {
	AccountID      string   `json:"accountId"`
	Client         string   `json:"client,omitempty"`
	Adb            string   `json:"adb,omitempty"`
	StageCodes     []string `json:"stageCodes"`
	Phases         []string `json:"phases,omitempty"` // 默认 ["main","ex","s"]
	AutoScanBox    bool     `json:"autoScanBox,omitempty"`
	IncludeCleared bool     `json:"includeCleared,omitempty"`
	UseSupport     *bool    `json:"useSupport,omitempty"`
	MaxAttempts    int      `json:"maxAttempts,omitempty"`
	TimeoutMin     int      `json:"timeoutMin,omitempty"`
	PiProject      string   `json:"piProject,omitempty"`
}

func (h *handler) activeRuns(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"runs": h.m.ActiveRuns(),
	})
}

func (h *handler) startAutoEvent(w http.ResponseWriter, r *http.Request) {
	var req autoEventRequest
	if err := decodeBody(w, r, &req); err != nil {
		writeErr(w, http.StatusBadRequest, err.Error())
		return
	}
	req.AccountID = strings.TrimSpace(req.AccountID)
	if req.AccountID == "" {
		writeErr(w, http.StatusBadRequest, "缺少账号 accountId")
		return
	}
	if len(req.StageCodes) == 0 {
		writeErr(w, http.StatusBadRequest, "至少需要提供一个关卡代号（stageCodes）")
		return
	}

	phaseFilter := map[string]bool{}
	for _, p := range req.Phases {
		if np := NormalizePhase(p); np != "" {
			phaseFilter[np] = true
		}
	}

	// 1. 干员名册：显式要求扫箱或当前无名册时自动扫箱（失败时保留已有缓存）。
	roster := h.m.Roster()
	scannedBox := false
	if req.AutoScanBox || roster.Count == 0 {
		if r2, _, err := h.m.ScanOperBox(r.Context(), req.AccountID); err == nil && r2.Count > 0 {
			roster = r2
			scannedBox = true
		}
	}

	maxPick := req.MaxAttempts
	if maxPick <= 0 {
		maxPick = 5
	}
	useSupport := true
	if req.UseSupport != nil {
		useSupport = *req.UseSupport
	}

	// 2. 逐关解析 + 按阶段过滤 + 自动挑作业
	plans, skipped, err := h.m.buildAutoEventPlans(r.Context(), req.StageCodes, phaseFilter, roster, maxPick, req.IncludeCleared)
	if err != nil {
		writeErr(w, http.StatusBadRequest, err.Error())
		return
	}
	if len(plans) == 0 {
		writeErr(w, http.StatusBadRequest, "没有可执行的关卡（可能已全部 3★ 通关，或所选阶段未匹配到可用作业）")
		return
	}

	supportUsage := 0
	if useSupport {
		supportUsage = 1
	}
	opts := model.RunOptions{
		AccountID:          req.AccountID,
		Client:             req.Client,
		Adb:                req.Adb,
		Stages:             plans,
		Formation:          true,
		AddTrust:           true,
		IgnoreRequirements: true,
		PhaseNav:           true,
		PiProject:          req.PiProject,
		MaxAttempts:        maxPick,
		SupportUnitUsage:   supportUsage,
		TimeoutMin:         req.TimeoutMin,
	}
	run, err := h.m.StartRun(opts)
	if err != nil {
		code := http.StatusBadRequest
		if isBusyErr(err) {
			code = http.StatusConflict
		}
		writeErr(w, code, err.Error())
		return
	}

	phaseCounts := map[string]int{}
	for _, st := range run.opts.Stages {
		phaseCounts[st.Phase]++
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"runId":      run.ID,
		"accountId":  req.AccountID,
		"stageCount": len(run.opts.Stages),
		"phases":     phaseCounts,
		"scannedBox": scannedBox,
		"rosterSize": roster.Count,
		"skipped":    skipped,
	})
}

func isBusyErr(err error) bool {
	return err != nil && strings.Contains(err.Error(), ErrBusy.Error())
}

func (m *Manager) buildAutoEventPlans(
	ctx context.Context,
	stageCodes []string,
	phaseFilter map[string]bool,
	roster model.Roster,
	maxPick int,
	includeCleared bool,
) ([]model.StagePlan, []string, error) {
	var plans []model.StagePlan
	var skipped []string
	seen := map[string]bool{}

	for _, raw := range stageCodes {
		code := strings.TrimSpace(raw)
		if code == "" || seen[strings.ToUpper(code)] {
			continue
		}
		seen[strings.ToUpper(code)] = true

		stages, err := m.ResolveStages(ctx, code)
		if err != nil || len(stages) == 0 {
			skipped = append(skipped, fmt.Sprintf("%s（未解析到关卡）", code))
			continue
		}
		st := stages[0]
		phase := InferStagePhase(st.Code, st.StageName)
		if len(phaseFilter) > 0 && !phaseFilter[phase] {
			continue
		}
		if !includeCleared && m.StageStars(st.Code, "normal") == 3 {
			skipped = append(skipped, fmt.Sprintf("%s（台账已 3★）", st.Code))
			continue
		}

		res, err := m.SearchJobs(ctx, SearchRequest{
			StageCode: st.Code,
			StageName: st.StageName,
			Roster:    roster,
			Limit:     maxPick * 2,
		})
		if err != nil || len(res.Jobs) == 0 {
			skipped = append(skipped, fmt.Sprintf("%s（未找到可用作业）", st.Code))
			continue
		}
		var jobIDs []int
		var raidIDs []int
		for _, j := range res.Jobs {
			jobIDs = append(jobIDs, j.ID)
			if j.Difficulty == 2 || j.Difficulty == 3 {
				raidIDs = append(raidIDs, j.ID)
			}
			if len(jobIDs) >= maxPick {
				break
			}
		}
		plans = append(plans, model.StagePlan{
			StageCode:  st.Code,
			StageName:  st.StageName,
			Phase:      phase,
			JobIDs:     jobIDs,
			RaidJobIDs: raidIDs,
		})
	}
	return GroupStagesByPhase(plans), skipped, nil
}
