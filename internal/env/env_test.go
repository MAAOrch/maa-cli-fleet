package env

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestGetOneMatchesExactKey(t *testing.T) {
	orig := runFunc
	defer func() { runFunc = orig }()

	runFunc = func(timeout time.Duration, name string, args ...string) string {
		return `{"other_key": "999", "max_frame_rate": "30", "player_name": "emu1"}`
	}
	got := getOne(Cfg{MuMuManager: "fake"}, "1", "max_frame_rate")
	if got != "30" {
		t.Fatalf("getOne(max_frame_rate) = %q, want 30", got)
	}
}

func TestSetOneAndSpecOverride(t *testing.T) {
	orig := runFunc
	defer func() { runFunc = orig }()

	cur := "60"
	runFunc = func(timeout time.Duration, name string, args ...string) string {
		for i, a := range args {
			if a == "--value" && i+1 < len(args) {
				cur = args[i+1]
				return `{"ok": "true"}`
			}
		}
		return `{"max_frame_rate": "` + cur + `"}`
	}
	old, err := SetOne(Cfg{MuMuManager: "fake"}, "2", "max_frame_rate", "30")
	if err != nil {
		t.Fatalf("SetOne: %v", err)
	}
	if old != "60" || cur != "30" {
		t.Fatalf("old=%q cur=%q, want 60/30", old, cur)
	}

	tmp := t.TempDir()
	specPath := filepath.Join(tmp, "desired.json")
	if err := os.WriteFile(specPath, []byte("\ufeff"+`{"default":{"max_frame_rate":"30"},"perInstance":{"2":{"max_frame_rate":"60"}}}`), 0o644); err != nil {
		t.Fatal(err)
	}
	spec, err := loadSpec(specPath)
	if err != nil {
		t.Fatalf("loadSpec: %v", err)
	}
	if got := spec.defaultFor("1")["max_frame_rate"]; got != "30" {
		t.Fatalf("defaultFor(1)=%q, want 30", got)
	}
	if got := spec.defaultFor("2")["max_frame_rate"]; got != "60" {
		t.Fatalf("defaultFor(2)=%q, want 60", got)
	}
}
