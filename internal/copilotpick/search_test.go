package copilotpick

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/xiachk083-hub/maa-cli-fleet/internal/copilotpick/ext"
	"github.com/xiachk083-hub/maa-cli-fleet/internal/copilotpick/model"
)

// 用假的作业站验一遍 搜作业 → 过滤关卡 → 判档 → 排序 的串联。
func TestSearchJobsWiring(t *testing.T) {
	mk := func(id int, stageTitle, stageName string, opers []map[string]any) map[string]any {
		content := map[string]any{
			"stage_name": stageName,
			"difficulty": 1,
			"doc":        map[string]any{"title": stageTitle},
			"opers":      opers,
			"groups":     []any{},
			"actions":    []any{},
		}
		b, _ := json.Marshal(content)
		return map[string]any{
			"id": id, "views": 1000 - id*10, "rating_level": 10,
			"upload_time": "2026-11-17T00:00:00", "content": string(b),
		}
	}
	oper := func(name string, elite, level int) map[string]any {
		return map[string]any{"name": name, "skill": 1,
			"requirements": map[string]any{"elite": elite, "level": level}}
	}
	jobs := []map[string]any{
		mk(1, "TL-9 银灰单核", "act55side_09", []map[string]any{oper("银灰", 2, 60)}),
		mk(2, "TL-9 银灰能天使", "act55side_09", []map[string]any{oper("银灰", 2, 60), oper("能天使", 2, 60)}),
		mk(3, "TL-9 双核", "act55side_09", []map[string]any{oper("能天使", 2, 60), oper("塞雷娅", 2, 60)}),
		mk(4, "别的关", "act55side_08", []map[string]any{oper("银灰", 2, 60)}),
	}
	payload := map[string]any{
		"status_code": 200,
		"data": map[string]any{"has_next": false, "page": 1, "total": 42, "data": jobs},
	}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(payload)
	}))
	defer srv.Close()

	m := &Manager{prts: ext.NewPrtsClient(srv.URL)}
	roster := model.Roster{Entries: []model.RosterEntry{{
		Name: "银灰", Rarity: 6, Elite: 2, Level: 90,
		SkillLevel: 7, Skills: []int{3, 3, 3}, SkillKnown: true,
	}}, Count: 1}

	res, err := m.SearchJobs(context.Background(), SearchRequest{
		Query: "TL-9", Roster: roster, Limit: 8, Pages: 1,
	})
	if err != nil {
		t.Fatalf("搜作业失败：%v", err)
	}
	if res.Stage.StageName != "act55side_09" {
		t.Fatalf("关卡解析不对：%+v", res.Stage)
	}
	if res.Total != 42 {
		t.Errorf("作业站总数应透传：%d", res.Total)
	}
	if res.Fetched != 3 {
		t.Errorf("只该统计本关的三份作业（第四份是别的关）：%d", res.Fetched)
	}
	if len(res.Jobs) != 2 || len(res.Blocked) != 1 {
		t.Fatalf("应为 2 份可打 + 1 份打不了，实际 jobs=%d blocked=%d", len(res.Jobs), len(res.Blocked))
	}
	if res.Jobs[0].ID != 1 || res.Jobs[0].Mode != "ready" {
		t.Errorf("可直接抄的应排第一：%+v", res.Jobs[0])
	}
	if res.Jobs[1].ID != 2 || res.Jobs[1].Mode != "borrow" {
		t.Errorf("差一人可借排第二：id=%d mode=%s", res.Jobs[1].ID, res.Jobs[1].Mode)
	}
	if res.Jobs[0].Title != "TL-9 银灰单核" || res.Jobs[0].Total != 1 {
		t.Errorf("标题/出场人数没带上：%+v", res.Jobs[0])
	}
}

// 没有名册时不该去拉作业，直接报错并说清下一步。
func TestSearchJobsRequiresRoster(t *testing.T) {
	m := &Manager{prts: ext.NewPrtsClient("http://127.0.0.1:1")}
	if _, err := m.SearchJobs(context.Background(), SearchRequest{Query: "TL-9"}); err == nil {
		t.Fatal("空名册应报错")
	}
}

// 难度过滤按作业站 difficulty 数字走（1 普通 / 2 突袭 / 3 磨难），
// 不能只看标题——OS-S-3 实测“磨难”作业标题里没有“突袭”二字。
func TestFilterJobsByModeUsesDifficulty(t *testing.T) {
	jobs := []model.Job{
		{ID: 1, Title: "普通作业", Difficulty: 1},
		{ID: 2, Title: "突袭作业", Difficulty: 2},
		{ID: 3, Title: "磨难作业", Difficulty: 3},
		{ID: 4, Title: "未标注-突袭可用", Difficulty: 0},
		{ID: 5, Title: "未标注-仅普通可用", Difficulty: 0},
	}
	ids := func(in []model.Job) []int {
		out := make([]int, 0, len(in))
		for _, j := range in {
			out = append(out, j.ID)
		}
		return out
	}
	if got, want := ids(filterJobsByMode(jobs, "normal")), []int{1, 5}; !equalInts(got, want) {
		t.Errorf("normal 过滤：got=%v want=%v", got, want)
	}
	if got, want := ids(filterJobsByMode(jobs, "raid")), []int{2, 3, 4}; !equalInts(got, want) {
		t.Errorf("raid 过滤：got=%v want=%v", got, want)
	}
	if got := ids(filterJobsByMode(jobs, "")); len(got) != 5 {
		t.Errorf("不传 mode 应返回全部：got=%v", got)
	}
}

func equalInts(a, b []int) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
