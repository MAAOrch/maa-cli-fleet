package copilotpick

// 跨号共享作业评分库（job_scores.json）与编队期 SubTaskExtraInfo 回调解析的单元测试。

import (
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/xiachk083-hub/maa-cli-fleet/internal/copilotpick/model"
)

const realFormationExtraLog = `[04:12:03.112][INF][Px1234][Tx5678] Assistant::append_callback | SubTaskExtraInfo {"class":"asst::BattleFormationTask","details":{"oper_name":"维什戴尔","requirement_type":"SkillLevel"},"taskchain":"Copilot","what":"BattleFormationOperUnavailable"}
[04:12:04.220][INF][Px1234][Tx5678] Assistant::append_callback | SubTaskExtraInfo {"class":"asst::BattleFormationTask","details":{"oper_name":"玛恩纳","requirement_type":"Elite"},"taskchain":"Copilot","what":"BattleFormationOperUnavailable"}
[04:12:05.001][INF][Px1234][Tx5678] Assistant::append_callback | SubTaskExtraInfo {"class":"asst::BattleFormationTask","details":{"oper_name":"维什戴尔","requirement_type":"SkillLevel"},"taskchain":"Copilot","what":"BattleFormationOperUnavailable"}`

func TestParseFormationUnavailable(t *testing.T) {
	evs := parseFormationUnavailable(realFormationExtraLog)
	if len(evs) != 2 {
		t.Fatalf("应去重得到 2 个不可用干员，实际 %d: %+v", len(evs), evs)
	}
	if evs[0].OperName != "维什戴尔" || evs[0].RequirementType != "SkillLevel" {
		t.Errorf("第 1 条解析异常：%+v", evs[0])
	}
	if evs[1].OperName != "玛恩纳" || evs[1].RequirementType != "Elite" {
		t.Errorf("第 2 条解析异常：%+v", evs[1])
	}

	// unavailableNames 也应把 BattleFormationOperUnavailable 合并进去
	names := unavailableNames(realFormationExtraLog)
	if len(names) != 2 || names[0] != "维什戴尔" || names[1] != "玛恩纳" {
		t.Fatalf("unavailableNames 应包含 SubTaskExtraInfo 里的干员，实际 %v", names)
	}
}

func TestJobScoresCrossInstanceAndRanking(t *testing.T) {
	shared := filepath.Join(t.TempDir(), "shared_job_scores.json")

	// 模拟 main 与 slot2 两个独立 Manager 指向同一份共享 job_scores.json
	mMain := &Manager{cfg: Config{Root: t.TempDir(), JobScoresFile: shared}, runs: map[string]*Run{}}
	mSlot2 := &Manager{cfg: Config{Root: t.TempDir(), JobScoresFile: shared}, runs: map[string]*Run{}}
	mMain.loadJobScoresFile()
	mSlot2.loadJobScoresFile()

	// main 打通 #200（3★），踩雷 #100（0★作战失败）
	mMain.RecordJobOutcome(200, "OS-EX-8", "raid", "local0b", OutcomePass3, "")
	mMain.RecordJobOutcome(100, "OS-EX-8", "raid", "local0b", OutcomeFailBattle, "作战失败")

	// 确保 mtime 推进，slot2 搜作业时自动重读并排序
	jobs := []model.Job{
		{ID: 100, Mode: "ready", Rating: 10, Views: 9999}, // 原排第 1，但实测翻车
		{ID: 300, Mode: "ready", Rating: 10, Views: 5000}, // 未测过
		{ID: 200, Mode: "ready", Rating: 9, Views: 1000},  // 原排第 3，但实测 3★ 通过
		{ID: 400, Mode: "borrow", Rating: 10, Views: 8000},
	}
	mSlot2.applyJobScores(jobs)

	if jobs[0].ID != 200 || jobs[0].LocalPass != 1 || jobs[0].LocalScore <= 0 {
		t.Fatalf("实测 3★ 通过的 #200 应顶到最前，实际 jobs[0]=%+v", jobs[0])
	}
	if jobs[1].ID != 300 {
		t.Fatalf("未测过的 #300 应排在中间，实际 jobs[1]=%+v", jobs[1])
	}
	if jobs[2].ID != 100 || jobs[2].LocalFail != 1 || jobs[2].LocalScore >= 0 {
		t.Fatalf("实测翻车的 #100 应沉到 ready 档末尾避雷，实际 jobs[2]=%+v", jobs[2])
	}
	if jobs[3].ID != 400 {
		t.Fatalf("borrow 档不应被错误顶到 ready 档前面，实际 jobs[3]=%+v", jobs[3])
	}

	// 验证磁盘重读合并：slot2 再记一条，不能覆盖 main 的记录
	time.Sleep(20 * time.Millisecond)
	mSlot2.RecordJobOutcome(300, "OS-EX-8", "raid", "local1", OutcomeFailFormation, "编队熔断")
	all := mMain.JobScores()
	if len(all) != 3 {
		t.Fatalf("共享评分库应合并保留 3 条记录，实际 %d: %+v", len(all), all)
	}
	if _, err := os.Stat(shared); err != nil {
		t.Fatalf("共享评分文件应已落盘：%v", err)
	}
}
