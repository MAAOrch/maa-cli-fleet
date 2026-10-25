package runner

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/xiachk083-hub/maa-cli-fleet/internal/model"
)

func TestResidentPreCommandEmptyIsNoop(t *testing.T) {
	if err := (&Runner{}).runResidentPre(model.Account{ID: "lx"}, "16672"); err != nil {
		t.Fatalf("empty ResidentPreCommand: %v", err)
	}
}

func TestResidentPreCommandSubstitutesAddr(t *testing.T) {
	dir := t.TempDir()
	out := filepath.Join(dir, "pre.txt")
	cmd := `echo {addr}> "` + out + `"`
	acc := model.Account{ID: "lx", ResidentPreCommand: cmd}

	if err := (&Runner{}).runResidentPre(acc, "16672"); err != nil {
		t.Fatalf("runResidentPre: %v", err)
	}
	b, err := os.ReadFile(out)
	if err != nil {
		t.Fatalf("read output: %v", err)
	}
	if !strings.Contains(string(b), "127.0.0.1:16672") {
		t.Fatalf("output = %q, want 127.0.0.1:16672", string(b))
	}
}

func TestResidentPreCommandFailurePropagates(t *testing.T) {
	err := (&Runner{}).runResidentPre(model.Account{ID: "lx", ResidentPreCommand: `exit /b 3`}, "16672")
	if err == nil {
		t.Fatal("expected non-zero exit to fail")
	}
}

func TestResidentPreCommandTimeout(t *testing.T) {
	old := residentPreTimeout
	residentPreTimeout = 150 * time.Millisecond
	defer func() { residentPreTimeout = old }()

	start := time.Now()
	err := (&Runner{}).runResidentPre(model.Account{ID: "lx", ResidentPreCommand: `ping -n 6 127.0.0.1 >nul`}, "16672")
	if err == nil {
		t.Fatal("expected timeout to fail")
	}
	if elapsed := time.Since(start); elapsed > 3*time.Second {
		t.Fatalf("timeout took %v, want < 3s", elapsed)
	}
}

func TestRotateDueBoundary(t *testing.T) {
	now := time.Now()
	l := &rogueLane{
		tasks:     []string{"ra_cycle_lx", "rogue_jiegarden_lx"},
		rotateMin: 10,
		taskSince: now,
	}
	if l.rotateDue(now.Add(9*time.Minute + 59*time.Second)) {
		t.Fatal("before boundary must not rotate")
	}
	if !l.rotateDue(now.Add(10 * time.Minute)) {
		t.Fatal("exact boundary must rotate")
	}
}

func TestAdvanceTaskUpdatesTaskAndSince(t *testing.T) {
	now := time.Now()
	l := &rogueLane{
		tasks:     []string{"ra_cycle_lx", "rogue_jiegarden_lx"},
		rotateMin: 10,
		taskSince: now.Add(-time.Hour),
	}
	next, ok := l.advanceTask(now)
	if !ok || next != "rogue_jiegarden_lx" {
		t.Fatalf("advanceTask = %q, %v", next, ok)
	}
	if l.task != next {
		t.Fatalf("lane task = %q, want %q", l.task, next)
	}
	if !l.taskSince.Equal(now) {
		t.Fatalf("taskSince = %v, want %v", l.taskSince, now)
	}
	if l.labelText() != "肉鸽" {
		t.Fatalf("label = %q, want 肉鸽", l.labelText())
	}
}

func TestRotationStart(t *testing.T) {
	cases := []struct {
		name    string
		tasks   []string
		current string
		idx     int
		want    string
	}{
		{"no rotation", nil, "rogue_old", 0, "rogue_old"},
		{"current in list", []string{"ra_cycle_lx", "rogue_jiegarden_lx"}, "rogue_jiegarden_lx", 1, "rogue_jiegarden_lx"},
		{"current empty", []string{"ra_cycle_lx", "rogue_jiegarden_lx"}, "", 0, "ra_cycle_lx"},
		{"current unknown", []string{"ra_cycle_lx", "rogue_jiegarden_lx"}, "spa_a32", 0, "ra_cycle_lx"},
	}
	for _, c := range cases {
		idx, task := rotationStart(c.tasks, c.current)
		if idx != c.idx || task != c.want {
			t.Fatalf("%s: rotationStart = (%d, %q), want (%d, %q)", c.name, idx, task, c.idx, c.want)
		}
	}
}
