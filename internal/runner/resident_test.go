package runner

import (
	"testing"
	"time"

	"github.com/xiachk083-hub/maa-cli-fleet/internal/model"
)

func TestAccountResidentTaskFile(t *testing.T) {
	cases := []struct {
		name string
		acc  model.Account
		want string
	}{
		{"new field wins", model.Account{ResidentTask: "spa_a32", RogueTask: "rogue_old"}, "spa_a32"},
		{"legacy field fallback", model.Account{RogueTask: "rogue_old"}, "rogue_old"},
		{"rotation first", model.Account{ResidentTasks: []string{" ", "ra_cycle_lx", "rogue_jiegarden_lx"}}, "ra_cycle_lx"},
		{"trim", model.Account{ResidentTask: "  spa_a32  "}, "spa_a32"},
		{"empty", model.Account{}, ""},
	}
	for _, c := range cases {
		if got := c.acc.ResidentTaskFile(); got != c.want {
			t.Fatalf("%s: ResidentTaskFile() = %q, want %q", c.name, got, c.want)
		}
	}
}

func TestResidentLabel(t *testing.T) {
	cases := map[string]string{
		"rogue_jiegarden_l7": "肉鸽",
		"spa_a32":            "卫戍",
		"SPA_TXWY":           "卫戍",
		"spa-stronghold":     "卫戍",
	}
	for task, want := range cases {
		if got := residentLabel(task); got != want {
			t.Fatalf("residentLabel(%q) = %q, want %q", task, got, want)
		}
	}
}

func TestIsBlankRun(t *testing.T) {
	cases := map[string]bool{
		"completed=0 errored=0 hardFail=false": true,
		"completed=1 errored=0 hardFail=false": false,
		"completed=0 errored=2 hardFail=false": false,
		"completed=0 errored=0 hardFail=true":  false,
	}
	for why, want := range cases {
		if got := isBlankRun(why); got != want {
			t.Fatalf("isBlankRun(%q) = %v, want %v", why, got, want)
		}
	}
}

func TestSpaSpinLimitIsLonger(t *testing.T) {
	spa := &rogueLane{label: "卫戍"}
	if got := spa.spinLimit(); got != spaSpinMin*time.Minute {
		t.Fatalf("SPA spinLimit = %v", got)
	}
	rogue := &rogueLane{label: "肉鸽"}
	if got := rogue.spinLimit(); got != rogueSpinMin*time.Minute {
		t.Fatalf("rogue spinLimit = %v", got)
	}
}

func TestResidentLabelReclamation(t *testing.T) {
	for _, task := range []string{"ra_cycle_lx", "reclamation_tales_lx", "reclamation_tales"} {
		if got := residentLabel(task); got != "生息" {
			t.Fatalf("residentLabel(%q) = %q, want 生息", task, got)
		}
	}
	ra := &rogueLane{label: "生息"}
	if got := ra.spinLimit(); got != spaSpinMin*time.Minute {
		t.Fatalf("reclamation spinLimit = %v", got)
	}
}

func TestRotationTasks(t *testing.T) {
	cases := []struct {
		name string
		acc  model.Account
		want []string
	}{
		{"rotation list", model.Account{ResidentTasks: []string{"ra_cycle_lx", " ", "rogue_jiegarden_lx"}}, []string{"ra_cycle_lx", "rogue_jiegarden_lx"}},
		{"single fallback", model.Account{ResidentTask: "spa_a32"}, []string{"spa_a32"}},
		{"empty", model.Account{}, nil},
	}
	for _, c := range cases {
		got := c.acc.RotationTasks()
		if len(got) != len(c.want) {
			t.Fatalf("%s: %v want %v", c.name, got, c.want)
		}
		for i := range got {
			if got[i] != c.want[i] {
				t.Fatalf("%s: %v want %v", c.name, got, c.want)
			}
		}
	}
}

func TestRotateDueAndAdvance(t *testing.T) {
	tasks := []string{"ra_cycle_lx", "rogue_jiegarden_lx"}
	l := &rogueLane{tasks: tasks, rotateMin: 240, taskSince: time.Now()}
	if l.rotateDue(time.Now().Add(239 * time.Minute)) {
		t.Fatal("239 分钟不该轮换")
	}
	if !l.rotateDue(time.Now().Add(241 * time.Minute)) {
		t.Fatal("241 分钟应该轮换")
	}
	next, ok := l.advanceTask(time.Now())
	if !ok || next != "rogue_jiegarden_lx" {
		t.Fatalf("advanceTask = %q,%v", next, ok)
	}
	if l.labelText() != "肉鸽" {
		t.Fatalf("label = %q", l.labelText())
	}
	next, ok = l.advanceTask(time.Now())
	if !ok || next != "ra_cycle_lx" {
		t.Fatalf("wrap advanceTask = %q,%v", next, ok)
	}
	if l.labelText() != "生息" {
		t.Fatalf("label = %q", l.labelText())
	}
	// 单任务不轮换
	single := &rogueLane{tasks: []string{"ra_cycle_lx"}, rotateMin: 1, taskSince: time.Now().Add(-24 * time.Hour)}
	if single.rotateDue(time.Now()) {
		t.Fatal("单任务不该轮换")
	}
	if _, ok := single.advanceTask(time.Now()); ok {
		t.Fatal("单任务 advanceTask 应返回 false")
	}
}
