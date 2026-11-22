package ext

import (
	"testing"

	"github.com/xiachk083-hub/maa-cli-fleet/internal/copilotpick/model"
)

// 已拥有、有技能数据（normalize 之后的形态）
func owned(name string, rarity, elite, level int) model.RosterEntry {
	return model.RosterEntry{
		Name: name, Rarity: rarity, Elite: elite, Level: level,
		SkillLevel: 7, Skills: []int{3, 3, 3}, SkillKnown: true,
	}
}

// 已拥有、没有技能数据（MAA 本地识别）
func ownedLocal(name string, rarity, elite, level int) model.RosterEntry {
	return model.RosterEntry{Name: name, Rarity: rarity, Elite: elite, Level: level}
}

func rosterOf(entries ...model.RosterEntry) model.Roster {
	return model.Roster{Entries: entries, Count: len(entries)}
}

func job(opers ...OperSpec) RawJob { return RawJob{Opers: opers} }

func TestEvaluateJobReady(t *testing.T) {
	r := rosterOf(owned("银灰", 6, 2, 90), owned("艾雅法拉", 6, 2, 60))
	j := job(
		OperSpec{Name: "银灰", Skill: 1, Requirements: map[string]int{"elite": 2, "level": 80}},
		OperSpec{Name: "艾雅法拉", Skill: 1, Requirements: map[string]int{"elite": 2, "level": 60}},
	)
	res := EvaluateJob(j, r)
	if res.Mode != "ready" || !res.Met() {
		t.Fatalf("应可直接抄，实际 mode=%s missing=%v train=%v", res.Mode, res.Missing, res.LevelShort)
	}
	if res.Fuzz || res.Implied != 0 {
		t.Errorf("不该有 fuzz/implied：%+v", res)
	}
}

func TestEvaluateJobBorrowAndBlocked(t *testing.T) {
	r := rosterOf(owned("银灰", 6, 2, 90))
	// 差一个人 → borrow
	res := EvaluateJob(job(
		OperSpec{Name: "银灰", Requirements: map[string]int{"elite": 2, "level": 80}},
		OperSpec{Name: "能天使", Requirements: map[string]int{"elite": 2, "level": 60}},
	), r)
	if res.Mode != "borrow" || len(res.Missing) != 1 {
		t.Fatalf("应差一人可借，实际 mode=%s missing=%v", res.Mode, res.Missing)
	}
	// 差两个 → blocked
	res = EvaluateJob(job(
		OperSpec{Name: "能天使"}, OperSpec{Name: "塞雷娅"},
	), r)
	if res.Mode != "blocked" {
		t.Fatalf("缺两人应打不了，实际 %s（missing=%v）", res.Mode, res.Missing)
	}
}

func TestEvaluateJobTooManyOpersBlocked(t *testing.T) {
	entries := make([]model.RosterEntry, 0, 14)
	opers := make([]OperSpec, 0, 14)
	for i := 0; i < 14; i++ {
		name := string(rune('甲' + i))
		entries = append(entries, owned(name, 6, 2, 90))
		opers = append(opers, OperSpec{Name: name, Requirements: map[string]int{"elite": 2, "level": 60}})
	}
	res := EvaluateJob(job(opers...), rosterOf(entries...))
	if res.Mode != "blocked" {
		t.Fatalf("14 人作业应判打不了（MAA 只能 13 人），实际 %s", res.Mode)
	}
}

// 关键口径：名册没有技能数据时，专三要求不能把人判成「需补练度」。
func TestEvaluateJobSkillUnknownIsLenient(t *testing.T) {
	r := rosterOf(ownedLocal("银灰", 6, 2, 90))
	res := EvaluateJob(job(OperSpec{
		Name: "银灰", Skill: 3,
		Requirements: map[string]int{"elite": 2, "level": 60, "skill_level": 9, "module": 2},
	}), r)
	if !res.Met() {
		t.Fatalf("缺技能数据时应按已拥有处理，实际 mode=%s train=%v", res.Mode, res.LevelShort)
	}
	if !res.Fuzz {
		t.Error("应标记 Fuzz（技能数据缺失导致的放宽）")
	}
}

// 名册有技能数据时，专精不够要判「练度不足」。
func TestEvaluateJobMasteryShort(t *testing.T) {
	e1 := owned("银灰", 6, 2, 90)
	e1.Skills = []int{3, 0, 0} // 技能 2 没专精
	e2 := owned("艾雅法拉", 6, 2, 90)
	e2.Skills = []int{0, 0, 0}
	r := rosterOf(e1, e2)
	res := EvaluateJob(job(
		OperSpec{Name: "银灰", Skill: 2, Requirements: map[string]int{"elite": 2, "level": 60, "skill_level": 9}},
		OperSpec{Name: "艾雅法拉", Skill: 1, Requirements: map[string]int{"elite": 2, "level": 60, "skill_level": 9}},
	), r)
	if res.Mode != "train" {
		t.Fatalf("两人都缺专精应要补练度，实际 %s", res.Mode)
	}
	if len(res.LevelShort) != 2 {
		t.Errorf("应有两个练度不足：%v", res.LevelShort)
	}
}

// 只有一个人练度不足：不能算「差一人可借」——助战只能补缺失的干员，
// 补不了练度；MAA 编队也会因精英化不够直接拒用。
func TestEvaluateJobSingleTrainingIsTrain(t *testing.T) {
	e := owned("银灰", 6, 1, 40)
	r := rosterOf(e, owned("艾雅法拉", 6, 2, 60))
	res := EvaluateJob(job(
		OperSpec{Name: "银灰", Requirements: map[string]int{"elite": 2, "level": 60}},
		OperSpec{Name: "艾雅法拉", Requirements: map[string]int{"elite": 2, "level": 60}},
	), r)
	if res.Mode != "train" {
		t.Fatalf("只差一人练度应判练度不足，实际 %s（train=%v）", res.Mode, res.LevelShort)
	}
}

// 作业没写练度 → 按星级补默认要求（六星默认精二 60 专三），并记 implied。
func TestEvaluateJobImpliesDefaultRequirement(t *testing.T) {
	r := rosterOf(owned("银灰", 6, 2, 60))
	res := EvaluateJob(job(OperSpec{Name: "银灰"}), r)
	if !res.Met() || res.Implied != 1 {
		t.Fatalf("E2 60 专三应满足六星默认要求，实际 mode=%s implied=%d train=%v",
			res.Mode, res.Implied, res.LevelShort)
	}
	// 只到精一 60：默认要求精二 60 → 不达标
	r2 := rosterOf(owned("银灰", 6, 1, 60))
	res2 := EvaluateJob(job(OperSpec{Name: "银灰"}), r2)
	if len(res2.LevelShort) != 1 || res2.LevelShort[0] != "银灰" {
		t.Fatalf("精一 60 不该满足六星默认要求，实际 mode=%s train=%v", res2.Mode, res2.LevelShort)
	}
	if res2.Implied != 1 {
		t.Errorf("补默认要求要计数，实际 %d", res2.Implied)
	}
}

func TestEvaluateJobLevelTooLow(t *testing.T) {
	r := rosterOf(owned("银灰", 6, 1, 40), owned("艾雅法拉", 6, 2, 90))
	res := EvaluateJob(job(
		OperSpec{Name: "银灰", Requirements: map[string]int{"elite": 2, "level": 60}},
		OperSpec{Name: "艾雅法拉", Requirements: map[string]int{"elite": 2, "level": 60}},
	), r)
	if len(res.LevelShort) != 1 || res.LevelShort[0] != "银灰" {
		t.Fatalf("精一 40 达不到精二 60，应记练度不足：mode=%s train=%v", res.Mode, res.LevelShort)
	}
}

// 干员组：组里只要有人达标就算过。
func TestEvaluateJobGroup(t *testing.T) {
	r := rosterOf(owned("银灰", 6, 2, 90))
	res := EvaluateJob(RawJob{Groups: []Group{{
		Name: "近卫",
		Opers: []OperSpec{
			{Name: "银灰", Requirements: map[string]int{"elite": 2, "level": 60}},
			{Name: "煌", Requirements: map[string]int{"elite": 2, "level": 60}},
		},
	}}}, r)
	if !res.Met() {
		t.Fatalf("组里银灰达标应算过，实际 mode=%s", res.Mode)
	}
}

func TestProgressScoreOrdering(t *testing.T) {
	// 精二 1 级 > 精一 80 级（游戏里的强弱，不是逐项死比）
	if progressScore(6, 2, 1) <= progressScore(6, 1, 80) {
		t.Fatalf("精二 1 级(%d) 应高于 精一 80 级(%d)",
			progressScore(6, 2, 1), progressScore(6, 1, 80))
	}
}

func TestRankJobs(t *testing.T) {
	jobs := []model.Job{
		{ID: 1, Mode: "ready", Rating: 10, Views: 100, Total: 4},
		{ID: 2, Mode: "ready", Rating: 5, Views: 900, Total: 4},
		{ID: 3, Mode: "borrow", Rating: 10, Views: 500, Total: 3},
		{ID: 4, Mode: "train", Rating: 10, Views: 900, Total: 4},
		{ID: 5, Mode: "blocked", Rating: 10, Views: 900, Total: 4},
	}
	RankJobs(jobs)
	want := []int{1, 3, 2, 4, 5}
	for i, id := range want {
		if jobs[i].ID != id {
			got := make([]int, 0, len(jobs))
			for _, j := range jobs {
				got = append(got, j.ID)
			}
			t.Fatalf("排序应为 %v，实际 %v", want, got)
		}
	}
}

func TestEntryVariants(t *testing.T) {
	cases := []struct {
		name   string
		flags  int
		variant, stage string
		want   []bool
	}{
		{"未标注按普通", 0, "", "", []bool{false}},
		{"通用位展开两条", 3, "", "", []bool{false, true}},
		{"只标突袭", 2, "", "", []bool{true}},
		{"明确选突袭就写突袭", 1, "raid", "", []bool{true}},
		{"明确选普通就写普通", 3, "normal", "", []bool{false}},
		{"15+ 章险地", 0, "", "main_15-03#s", []bool{true}},
		{"磨难没有突袭概念", 3, "", "tough_10-01", []bool{false}},
		{"简单同理", 3, "", "easy_11-02", []bool{false}},
	}
	for _, c := range cases {
		got := EntryVariants(c.flags, c.variant, c.stage)
		if len(got) != len(c.want) {
			t.Errorf("%s：条数 %v，期望 %v", c.name, got, c.want)
			continue
		}
		for i := range got {
			if got[i] != c.want[i] {
				t.Errorf("%s：第 %d 条 IsRaid=%v，期望 %v", c.name, i, got[i], c.want[i])
			}
		}
	}
}
