package agent

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestParseMaaLogTracksLatestTaskChain(t *testing.T) {
	lines := []string{
		`[2026-10-26 04:00:00] Assistant::append_callback | TaskChainStart {"taskchain":"Copilot","taskid":1}`,
		`[2026-10-26 04:00:01] Assistant::append_callback | SubTaskError {"task":"BattleStartPre","why":"formation"}`,
		`[2026-10-26 04:00:02] Assistant::append_callback | TaskChainError {"taskchain":"Copilot","taskid":1}`,
	}
	st := parseMaaLog(lines)
	if st.Status != "error" || st.TaskChain != "Copilot" {
		t.Fatalf("state = %#v", st)
	}
	if !strings.Contains(st.LastError, "TaskChainError") {
		t.Fatalf("LastError = %q", st.LastError)
	}
	if len(st.Lines) != 3 {
		t.Fatalf("Lines = %d, want 3", len(st.Lines))
	}
}

func TestMaaLogEyeReadsTail(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "asst.log")
	content := strings.Join([]string{
		`[old] TaskChainError {"taskchain":"Old"}`,
		`[new] TaskChainStart {"taskchain":"Copilot"}`,
		`[new] TaskChainCompleted {"taskchain":"Copilot"}`,
	}, "\n")
	if err := os.WriteFile(p, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	eye := &MaaLogEye{Path: p}
	raw, err := eye.Observe(context.Background())
	if err != nil {
		t.Fatalf("Observe: %v", err)
	}
	st := raw.(MaaLogState)
	if st.Status != "completed" || st.TaskChain != "Copilot" {
		t.Fatalf("state = %#v", st)
	}
}

func TestMaaLogEyeMissingFile(t *testing.T) {
	eye := &MaaLogEye{Path: filepath.Join(t.TempDir(), "missing.log")}
	if _, err := eye.Observe(context.Background()); err == nil {
		t.Fatal("expected error for missing log")
	}
}

func TestCopilotTaskTOML(t *testing.T) {
	got := copilotTaskTOML(`C:\jobs\2-9.json`)
	if !strings.Contains(got, `type = "Copilot"`) {
		t.Fatalf("missing Copilot type: %s", got)
	}
	if !strings.Contains(got, `C:\\jobs\\2-9.json`) {
		t.Fatalf("path not escaped: %s", got)
	}
}

func TestMaaCopilotHandRequiresJob(t *testing.T) {
	h := &MaaCopilotHand{MaaExe: "maa.exe"}
	err := h.Do(context.Background(), Command{Device: "127.0.0.1:16384", Hand: "maa-copilot"})
	if err == nil || !strings.Contains(err.Error(), "job is empty") {
		t.Fatalf("err = %v, want job empty", err)
	}
}

func TestMaaCopilotHandWritesTaskBeforeExec(t *testing.T) {
	dir := t.TempDir()
	job := filepath.Join(dir, "job.json")
	if err := os.WriteFile(job, []byte(`{"stage_name":"main_00-02"}`), 0o644); err != nil {
		t.Fatal(err)
	}
	h := &MaaCopilotHand{
		MaaExe:  filepath.Join(dir, "missing-maa.exe"),
		TaskDir: filepath.Join(dir, "tasks"),
		Timeout: 2 * time.Second,
	}
	err := h.Do(context.Background(), Command{
		Device: "127.0.0.1:16384",
		Hand:   "maa-copilot",
		Args:   map[string]any{"job": job},
	})
	if err == nil {
		t.Fatal("expected exec error for missing maa")
	}
	matches, err := filepath.Glob(filepath.Join(dir, "tasks", "agent_copilot_*.toml"))
	if err != nil || len(matches) != 1 {
		t.Fatalf("task files = %v, err = %v", matches, err)
	}
	b, err := os.ReadFile(matches[0])
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(b), `type = "Copilot"`) {
		t.Fatalf("task content = %s", b)
	}
}
