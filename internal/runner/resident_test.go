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
