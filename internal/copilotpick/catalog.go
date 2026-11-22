package copilotpick

// 关卡快捷按钮的数据源：把 MAA 资源里 stages.json 的关卡按章节/活动/资源关分组。
//
// 这只是给页面做快捷选择的“默认清单”，用户还可以在页面里自定义分组（存浏览器）。

import (
	"net/http"
	"regexp"
	"sort"
	"strconv"
	"strings"

	"github.com/xiachk083-hub/maa-cli-fleet/internal/copilotpick/model"
)

var (
	mainStageRe  = regexp.MustCompile(`^(\d+)-\d+`)
	eventStageRe = regexp.MustCompile(`^([A-Za-z]{2,})(?:-EX)?-\d+`)
)

var resourceLabels = map[string]string{
	"CE":  "龙门币",
	"LS":  "经验书",
	"AP":  "采购凭证",
	"CA":  "技巧概要",
	"SK":  "碳",
	"PR":  "芯片",
	"ANN": "剿灭",
}

// stageGroupName 把关卡代号归到一个人能看懂的分组名。
func stageGroupName(code string) string {
	if m := mainStageRe.FindStringSubmatch(code); m != nil {
		return "主线 第" + m[1] + "章"
	}
	if m := eventStageRe.FindStringSubmatch(code); m != nil {
		prefix := strings.ToUpper(m[1])
		if label, ok := resourceLabels[prefix]; ok {
			return "资源关 · " + label
		}
		return prefix + " 活动"
	}
	return "其它"
}

// groupOrder 让活动/主线/资源按页面直觉排序。
func groupOrder(name string) (int, int) {
	if strings.Contains(name, "活动") {
		return 0, 0
	}
	if strings.HasPrefix(name, "主线 第") {
		n, _ := strconv.Atoi(strings.TrimSuffix(strings.TrimPrefix(name, "主线 第"), "章"))
		return 1, n
	}
	if strings.HasPrefix(name, "资源关") {
		return 2, 0
	}
	return 3, 0
}

// stageGroups 把关卡表分组；同一关卡只出现一次。
func stageGroups(stages []model.Stage) []stageGroup {
	byName := map[string][]string{}
	seen := map[string]bool{}
	for _, s := range stages {
		code := strings.TrimSpace(s.Code)
		if code == "" || seen[code] {
			continue
		}
		seen[code] = true
		name := stageGroupName(code)
		byName[name] = append(byName[name], code)
	}
	names := make([]string, 0, len(byName))
	for name := range byName {
		names = append(names, name)
	}
	sort.Slice(names, func(i, j int) bool {
		oi, ni := groupOrder(names[i])
		oj, nj := groupOrder(names[j])
		if oi != oj {
			return oi < oj
		}
		if ni != nj {
			return ni < nj
		}
		return names[i] < names[j]
	})
	out := make([]stageGroup, 0, len(names))
	for _, name := range names {
		codes := byName[name]
		sort.Strings(codes)
		out = append(out, stageGroup{Name: name, Stages: codes})
	}
	return out
}

type stageGroup struct {
	Name   string   `json:"name"`
	Stages []string `json:"stages"`
}

// stageCatalog 返回按分组整理好的关卡清单（MAA stages.json）。
func (h *handler) stageCatalog(w http.ResponseWriter, r *http.Request) {
	groups := stageGroups(h.m.LocalStages())
	writeJSON(w, http.StatusOK, map[string]any{"groups": groups})
}
