package runner

import (
	"github.com/xiachk083-hub/maa-cli-fleet/internal/model"
	"testing"
	"time"
)

func TestInitialDailyPlanUsesStableEnqueueAnchor(t *testing.T) {
	r := testRunner(t)
	zero := 0
	r.cfg.PlanEarlyMin = &zero
	r.accounts = []model.Account{{ID: "l1", Enabled: true, Daily: "daily_l1"}}
	queued := time.Now().Add(-2 * time.Hour).Truncate(time.Second)
	task := &Task{Key: "daily:l1", AccountID: "l1", Kind: "daily", State: "queued", Enqueued: queued.Format("2006-01-02 15:04:05")}
	r.state.Tasks[task.Key] = task
	want := queued.Add(30 * time.Minute)
	for i := 0; i < 3; i++ {
		plan := r.BuildPlan()
		if len(plan) != 1 || !plan[0].Start.Equal(want) {
			t.Fatalf("initial task postponed: got %+v want %s", plan, want)
		}
	}
	if len(r.claimTasks(time.Now())) != 1 {
		t.Fatal("overdue initial daily not claimed")
	}
}
