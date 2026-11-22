package copilotpick

// 搜索链路：关卡解析 → 拉作业 → 与名册匹配 → 排序。
//
// 每一步都走 ext 包（HTTP 层只调这里，不直接碰 ext 的细节）。

import (
	"context"
	"fmt"
	"strings"

	"github.com/xiachk083-hub/maa-cli-fleet/internal/copilotpick/ext"
	"github.com/xiachk083-hub/maa-cli-fleet/internal/copilotpick/model"
)

// SearchRequest 是一次「搜作业」的入参。
type SearchRequest struct {
	Query     string      // 用户输入的关卡代号/活动代号/中文名
	StageCode string      // 已在第 2 步选定关卡时直接给代号，跳过解析
	StageName string      // 作业站关卡代码（act55side_09），给了就不再解析
	Roster    model.Roster // 名册（空名册只能列出作业、无法判档）
	Limit     int         // 每个关卡最多返回几份（默认 8）
	Pages     int         // 翻几页（默认 3，每页 50）
	Mode      string      // normal / raid；给了就按作业标题过滤掉不对难度的（可空）
}

// ResolveStages 把关卡关键词解析成候选关卡（最多 12 个）。
//
// 顺序：本地 MAA 关卡表精确匹配 → 前缀匹配 → 问作业站（用返回作业的 stage_name 兜底）。
func (m *Manager) ResolveStages(ctx context.Context, query string) ([]model.Stage, error) {
	q := strings.TrimSpace(query)
	if q == "" {
		return nil, fmt.Errorf("请输入关卡代号（如 TL-9）、活动代号（act55side）或章节号")
	}
	local := m.LocalStages()
	low := strings.ToLower(q)
	var exact, prefix []model.Stage
	for _, s := range local {
		code := strings.ToLower(s.Code)
		name := strings.ToLower(s.StageName)
		switch {
		case code == low || name == low:
			exact = append(exact, s)
		case strings.HasPrefix(code, low) || strings.HasPrefix(name, low):
			prefix = append(prefix, s)
		}
	}
	if len(exact) > 0 {
		return exact, nil
	}
	if len(prefix) > 0 {
		if len(prefix) > 12 {
			prefix = prefix[:12]
		}
		return prefix, nil
	}
	// 本地表里没有（资源没初始化 / 新关卡）：问作业站，用作业的 stage_name 兜底
	stage, err := m.resolveRemote(ctx, q)
	if err != nil {
		return nil, err
	}
	return []model.Stage{stage}, nil
}

// resolveRemote 用作业站的全文检索兜底解析关卡。
func (m *Manager) resolveRemote(ctx context.Context, q string) (model.Stage, error) {
	jobs, _, _, err := m.prts.Query(ctx, ext.QueryOptions{
		Document: q, OrderBy: "views", Desc: true, Page: 1, Limit: 50,
	})
	if err != nil {
		return model.Stage{}, fmt.Errorf("本地关卡表里没有 %q，向作业站查询也失败：%w", q, err)
	}
	for _, j := range jobs {
		name := strings.TrimSpace(j.StageName)
		if name == "" {
			continue
		}
		return model.Stage{Code: q, StageName: name, Title: j.Title, Source: "prts"}, nil
	}
	return model.Stage{}, fmt.Errorf("找不到关卡 %q（试试完整代号如 TL-9，或活动代号 act55side）", q)
}

// SearchJobs 搜作业并按名册分档。
func (m *Manager) SearchJobs(ctx context.Context, req SearchRequest) (model.JobSearch, error) {
	if len(req.Roster.Entries) == 0 {
		return model.JobSearch{}, fmt.Errorf("还没有名册：先在 MAA 里跑一次干员识别（或从 MAA 导入练度）")
	}
	pageLimit := req.Pages
	if pageLimit <= 0 {
		pageLimit = 3
	}
	return m.searchJobs(ctx, req, pageLimit)
}

// SearchJobsAllStages 对一个关键词命中的每个关卡各搜一次（活动/章节批量场景）。
func (m *Manager) SearchJobsAllStages(ctx context.Context, req SearchRequest, maxStages int) ([]model.JobSearch, error) {
	stages, err := m.ResolveStages(ctx, req.Query)
	if err != nil {
		return nil, err
	}
	if maxStages > 0 && len(stages) > maxStages {
		stages = stages[:maxStages]
	}
	out := make([]model.JobSearch, 0, len(stages))
	for _, st := range stages {
		one := req
		one.Query = ""
		one.StageCode = st.Code
		one.StageName = st.StageName
		res, err := m.searchJobs(ctx, one, 1) // 批量时每关只翻 1 页，控制时延
		if err != nil {
			continue
		}
		res.Stage = st
		out = append(out, res)
	}
	if len(out) == 0 {
		return nil, fmt.Errorf("这些关卡都没搜到作业：%s", req.Query)
	}
	return out, nil
}

// searchJobs 是 ResolveStages 之后的主流程。
func (m *Manager) searchJobs(ctx context.Context, req SearchRequest, pages int) (model.JobSearch, error) {
	stage := model.Stage{Code: req.StageCode, StageName: req.StageName}
	if stage.StageName == "" {
		stages, err := m.ResolveStages(ctx, req.Query)
		if err != nil {
			return model.JobSearch{}, err
		}
		stage = stages[0]
	}
	if stage.Code == "" {
		stage.Code = stage.StageName
	}

	limit := req.Limit
	if limit <= 0 {
		limit = 8
	}
	seen := map[int]bool{}
	var jobs, blocked, train []model.Job
	total, fetched := 0, 0

	for page := 1; page <= pages; page++ {
		rows, hasNext, tot, err := m.prts.Query(ctx, ext.QueryOptions{
			Document: stage.Code, OrderBy: "views", Desc: true, Page: page, Limit: 50,
		})
		if err != nil {
			if page == 1 {
				return model.JobSearch{}, err
			}
			break
		}
		if tot > total {
			total = tot
		}
		for _, raw := range rows {
			if !stageMatches(stage, raw.StageName) {
				continue
			}
			if seen[raw.ID] {
				continue
			}
			seen[raw.ID] = true
			fetched++
			j := toJob(raw, req.Roster)
			switch j.Mode {
			case "blocked":
				blocked = append(blocked, j)
			case "train":
				// 有干员但练度不够：不能直接抄，也不能当候选，单独给用户看。
				train = append(train, j)
			default:
				jobs = append(jobs, j)
			}
		}
		if !hasNext {
			break
		}
	}

	ext.RankJobs(jobs)
	ext.RankJobs(blocked)
	ext.RankJobs(train)
	jobs = filterJobsByMode(jobs, req.Mode)
	if len(jobs) > limit {
		jobs = jobs[:limit]
	}
	return model.JobSearch{
		Stage:   stage,
		Jobs:    jobs,
		Blocked: blocked,
		Train:   train,
		Total:   total,
		Fetched: fetched,
	}, nil
}

// stageMatches 判一份作业是不是这个关卡的（stage_name 与代号都算）。
func stageMatches(stage model.Stage, jobStage string) bool {
	js := strings.ToLower(strings.TrimSpace(jobStage))
	if js == "" {
		return false
	}
	return js == strings.ToLower(stage.StageName) || js == strings.ToLower(stage.Code)
}

// filterJobsByMode 按难度过滤作业标题。
//
// 作业站标题里会写明适用难度：「仅突袭」= 突袭专用、「仅普通」/「普通」= 普通专用，
// 不带字样的通常是通用轴。打普通时拿突袭作业、或反之，基本必 0 星（2026-11-22 实测
// S-3 突袭连试 3 份普通作业全 0 星白花理智），所以在搜索阶段直接筛掉。
func filterJobsByMode(jobs []model.Job, mode string) []model.Job {
	switch mode {
	case "normal":
		out := jobs[:0]
		for _, j := range jobs {
			if strings.Contains(j.Title, "突袭") {
				continue
			}
			out = append(out, j)
		}
		return out
	case "raid":
		out := jobs[:0]
		for _, j := range jobs {
			t := j.Title
			if strings.Contains(t, "仅普通") || (strings.Contains(t, "普通") && !strings.Contains(t, "突袭")) {
				continue
			}
			out = append(out, j)
		}
		return out
	default:
		return jobs
	}
}

// toJob 把作业站的一条记录 + 匹配结论转成前端的 Job。
func toJob(raw ext.RawJob, roster model.Roster) model.Job {
	res := ext.EvaluateJob(raw, roster)
	names := make([]string, 0, len(raw.Opers))
	for _, op := range raw.Opers {
		names = append(names, op.Name)
	}
	for _, g := range raw.Groups {
		names = append(names, groupName(g))
	}
	idx := ext.RosterIndex(roster)
	raritySum := 0
	for _, op := range raw.Opers {
		if e, ok := idx[ext.NormName(op.Name)]; ok {
			raritySum += e.Rarity
		}
	}
	return model.Job{
		ID:              raw.ID,
		Title:           raw.Title,
		StageName:       raw.StageName,
		Difficulty:      raw.Difficulty,
		Rating:          raw.Rating,
		Views:           raw.Views,
		MinimumRequired: raw.MinimumRequired,
		UploadTime:      raw.UploadTime,
		Details:         raw.Details,
		Opers:           names,
		Missing:         res.Missing,
		LevelShort:      res.LevelShort,
		Met:             res.Met(),
		RaritySum:       raritySum,
		Mode:            res.Mode,
		Total:           res.Total,
		ImpliedMissing:  res.Implied,
		RequirementFuzz: res.Fuzz,
	}
}

// groupName 是干员组在作业列表里的显示名（与 ext 内部口径一致）。
func groupName(g ext.Group) string {
	if strings.TrimSpace(g.Name) != "" {
		return g.Name
	}
	names := make([]string, 0, len(g.Opers))
	for _, c := range g.Opers {
		if n := strings.TrimSpace(c.Name); n != "" {
			names = append(names, n)
		}
	}
	if len(names) == 0 {
		return "组合"
	}
	return strings.Join(names, "/")
}
