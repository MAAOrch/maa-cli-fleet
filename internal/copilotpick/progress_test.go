package copilotpick

// 关卡完成度台账的回归测试：星数只升不降、落盘后能恢复、按难度分开记。

import (
	"testing"

	"github.com/xiachk083-hub/maa-cli-fleet/internal/copilotpick/model"
)

func newProgressManager(t *testing.T) *Manager {
	t.Helper()
	m := &Manager{cfg: Config{Root: t.TempDir()}, runs: map[string]*Run{}}
	m.loadProgressFile()
	return m
}

func TestProgressKeepsMaxStars(t *testing.T) {
	m := newProgressManager(t)
	m.RecordProgress("TL-EX-5", "normal", 2)
	if got := m.StageStars("TL-EX-5", "normal"); got != 2 {
		t.Fatalf("首次记录应为 2 星，实际 %d", got)
	}
	m.RecordProgress("TL-EX-5", "normal", 1) // 退步不覆盖
	if got := m.StageStars("TL-EX-5", "normal"); got != 2 {
		t.Fatalf("星数只升不降，实际 %d", got)
	}
	m.RecordProgress("TL-EX-5", "normal", 3)
	if got := m.StageStars("TL-EX-5", "normal"); got != 3 {
		t.Fatalf("提升到 3 星应生效，实际 %d", got)
	}
}

func TestProgressSeparatesModes(t *testing.T) {
	m := newProgressManager(t)
	m.RecordProgress("TL-EX-5", "normal", 2)
	if got := m.StageStars("TL-EX-5", "raid"); got != -1 {
		t.Fatalf("突袭没记录时应返回 -1，实际 %d", got)
	}
	m.RecordProgress("TL-EX-5", "raid", 3)
	if got := m.StageStars("TL-EX-5", "raid"); got != 3 {
		t.Fatalf("突袭记录应为 3（已通关），实际 %d", got)
	}
}

func TestProgressPersistAndReload(t *testing.T) {
	m := newProgressManager(t)
	m.RecordProgress("TL-EX-6", "normal", 3)

	// 模拟服务重启：同一 root 新建 Manager
	m2 := &Manager{cfg: Config{Root: m.cfg.Root}, runs: map[string]*Run{}}
	m2.loadProgressFile()
	if got := m2.StageStars("TL-EX-6", "normal"); got != 3 {
		t.Fatalf("重启后应从 progress.json 恢复 3 星，实际 %d", got)
	}
	if n := len(m2.Progress()); n != 1 {
		t.Fatalf("台账应有 1 条，实际 %d", n)
	}
}

func TestStageIsRaidOnly(t *testing.T) {
	raid := model.StagePlan{JobIDs: []int{1, 2}, RaidJobIDs: []int{1, 2}}
	if !stageIsRaidOnly(raid) {
		t.Fatal("作业全在 raidJobIds 里应判为只打突袭")
	}
	mixed := model.StagePlan{JobIDs: []int{1, 2}, RaidJobIDs: []int{2}}
	if stageIsRaidOnly(mixed) {
		t.Fatal("混合作业不应判为只打突袭")
	}
}
