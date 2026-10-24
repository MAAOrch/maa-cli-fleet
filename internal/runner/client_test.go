package runner

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/xiachk083-hub/maa-cli-fleet/internal/model"
)

func writeTask(t *testing.T, dir, name, body string) {
	t.Helper()
	tasks := filepath.Join(dir, "tasks")
	if err := os.MkdirAll(tasks, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(tasks, name+".toml"), []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
}

func writeProfile(t *testing.T, dir, client string) {
	t.Helper()
	profiles := filepath.Join(dir, "profiles")
	if err := os.MkdirAll(profiles, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(profiles, client+".toml"), []byte("[resource]\n"), 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestProfileForPrefersAccountClient(t *testing.T) {
	dir := t.TempDir()
	writeTask(t, dir, "daily_a01", `params = { client_type = "YoStarJP" }`)
	writeProfile(t, dir, "Bilibili")

	r := &Runner{cfg: Config{ConfigDir: dir}}
	got, err := r.profileFor(model.Account{ID: "a01", Client: "Bilibili"}, "daily_a01")
	if err != nil {
		t.Fatalf("profileFor: %v", err)
	}
	if got != "Bilibili" {
		t.Fatalf("profileFor = %q, want Bilibili", got)
	}
}

func TestProfileForFallsBackToTaskClientType(t *testing.T) {
	dir := t.TempDir()
	writeTask(t, dir, "rogue_jiegarden_l2", `params = { client_type = "YoStarJP", start_game_enabled = true }`)
	writeProfile(t, dir, "YoStarJP")

	r := &Runner{cfg: Config{ConfigDir: dir}}
	got, err := r.profileFor(model.Account{ID: "l2"}, "rogue_jiegarden_l2")
	if err != nil {
		t.Fatalf("profileFor: %v", err)
	}
	if got != "YoStarJP" {
		t.Fatalf("profileFor = %q, want YoStarJP", got)
	}
}

func TestProfileForRejectsEmptyClient(t *testing.T) {
	dir := t.TempDir()
	writeTask(t, dir, "rogue_no_client", `params = { start_game_enabled = true }`)

	r := &Runner{cfg: Config{ConfigDir: dir}}
	if _, err := r.profileFor(model.Account{ID: "l9"}, "rogue_no_client"); err == nil {
		t.Fatal("profileFor should reject an account/task without client")
	}
}

func TestProfileForRejectsMissingProfile(t *testing.T) {
	dir := t.TempDir()
	writeTask(t, dir, "rogue_jp", `params = { client_type = "YoStarJP" }`)

	r := &Runner{cfg: Config{ConfigDir: dir}}
	if _, err := r.profileFor(model.Account{ID: "l2"}, "rogue_jp"); err == nil {
		t.Fatal("profileFor should reject a client without a profile file")
	}
}
