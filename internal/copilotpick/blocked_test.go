package copilotpick

// 「编不进队」黑名单的回归测试：抓 Unavailable、按作业技能序号记账、按作业拦截。

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"github.com/xiachk083-hub/maa-cli-fleet/internal/copilotpick/model"
)

const unavailableLog = `[23:25:57][INF] SubTaskError {"class":"asst::BattleFormationTask","details":{"opers":{"塔露拉":[{"name":"塔露拉","reason":"Unavailable"}],"澄闪":[{"name":"澄闪","reason":"Missing"}]}},"why":"OperatorMissing"}`

func TestUnavailableNames(t *testing.T) {
	got := unavailableNames(unavailableLog)
	if len(got) != 1 || got[0] != "塔露拉" {
		t.Fatalf("只应抓到 Unavailable 的塔露拉（Missing 不算），实际 %v", got)
	}
}

func TestBlockedPersistAndMatch(t *testing.T) {
	dir := t.TempDir()
	m := &Manager{cfg: Config{Root: dir}, runs: map[string]*Run{}}
	m.loadBlockedFile()

	if !m.AddBlocked("塔露拉", 2) {
		t.Fatal("首次记录应返回 true")
	}
	if m.AddBlocked("塔露拉", 2) {
		t.Fatal("重复记录应返回 false")
	}
	if !m.IsBlocked("塔露拉", 2) {
		t.Fatal("记录后应判为 blocked")
	}
	if m.IsBlocked("塔露拉", 1) {
		t.Fatal("不同技能序号不该被连带")
	}

	// 落盘后新 Manager 能恢复（服务重启场景）
	m2 := &Manager{cfg: Config{Root: dir}, runs: map[string]*Run{}}
	m2.loadBlockedFile()
	if !m2.IsBlocked("塔露拉", 2) {
		t.Fatal("重启后应从 unavailable.json 恢复")
	}
}

func TestJobBlockedUsesJobSkillIndex(t *testing.T) {
	dir := t.TempDir()
	jobFile := filepath.Join(dir, "job-1.json")
	job := map[string]any{
		"opers": []map[string]any{
			{"name": "圣聆初雪", "skill": 2},
			{"name": "遥", "skill": 2},
		},
	}
	b, _ := json.Marshal(job)
	if err := os.WriteFile(jobFile, b, 0o644); err != nil {
		t.Fatal(err)
	}

	m := &Manager{cfg: Config{Root: dir}, runs: map[string]*Run{}}
	m.loadBlockedFile()
	m.AddBlocked("圣聆初雪", 2)
	r := &Run{m: m, roster: model.Roster{}}

	label, blocked := r.jobBlocked(jobFile)
	if !blocked || label != "圣聆初雪/s2" {
		t.Fatalf("应命中 圣聆初雪/s2，实际 blocked=%v label=%s", blocked, label)
	}

	// 同一干员别的技能序号不该拦（作业里换成 s1 就放过）
	m.clearBlocked()
	m.AddBlocked("圣聆初雪", 3)
	if _, blocked := r.jobBlocked(jobFile); blocked {
		t.Fatal("技能序号不同时不该拦截")
	}
}
