package runner

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/xiachk083-hub/maa-cli-fleet/internal/model"
)

type stubExecutor struct {
	name string
	job  Job
	res  Result
	err  error
}

func (s *stubExecutor) Name() string { return s.name }

func (s *stubExecutor) Run(_ context.Context, job Job) (Result, error) {
	s.job = job
	return s.res, s.err
}

func TestRegistry(t *testing.T) {
	reg := NewRegistry()
	e := &stubExecutor{name: "stub"}

	if err := reg.Register("daily", e); err != nil {
		t.Fatalf("Register: %v", err)
	}
	if got, ok := reg.For("daily"); !ok || got != e {
		t.Fatalf("For(daily) = %v, %v; want stub, true", got, ok)
	}
	if _, ok := reg.For("ann"); ok {
		t.Fatal("For(ann) should be false")
	}
	if got := reg.Kinds(); !slices.Equal(got, []string{"daily"}) {
		t.Fatalf("Kinds = %v, want [daily]", got)
	}
	if err := reg.Register("daily", e); err == nil {
		t.Fatal("duplicate Register should fail")
	}
	if err := reg.Register("", e); err == nil {
		t.Fatal("empty kind should fail")
	}
	if err := reg.Register("x", nil); err == nil {
		t.Fatal("nil executor should fail")
	}
	var nilReg *Registry
	if _, ok := nilReg.For("daily"); ok {
		t.Fatal("nil registry should not find anything")
	}
}

func TestMaaExecutorMapsJobAndResult(t *testing.T) {
	var gotAcc model.Account
	var gotPort, gotTask string
	ex := &maaExecutor{run: func(a model.Account, port, task string) (bool, string, error) {
		gotAcc, gotPort, gotTask = a, port, task
		return true, "out.log", nil
	}}

	res, err := ex.Run(context.Background(), Job{
		Key:      "daily:a01",
		Kind:     "daily",
		Account:  model.Account{ID: "a01"},
		Device:   DeviceRef{ADBAddr: "16672"},
		TaskFile: "daily_a01",
	})
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if !res.OK || res.OutFile != "out.log" || res.Note != "" {
		t.Fatalf("Result = %+v, want OK/out.log/empty note", res)
	}
	if gotAcc.ID != "a01" || gotPort != "16672" || gotTask != "daily_a01" {
		t.Fatalf("executor got (%q, %q, %q), want (a01, 16672, daily_a01)", gotAcc.ID, gotPort, gotTask)
	}
	if ex.Name() != "maa-cli" {
		t.Fatalf("Name = %q, want maa-cli", ex.Name())
	}
}

func TestMaaExecutorPropagatesError(t *testing.T) {
	ex := &maaExecutor{run: func(model.Account, string, string) (bool, string, error) {
		return false, "out.log", errors.New("boom")
	}}
	res, err := ex.Run(context.Background(), Job{})
	if err == nil || err.Error() != "boom" {
		t.Fatalf("err = %v, want boom", err)
	}
	if res.OK || res.Note != "boom" || res.OutFile != "out.log" {
		t.Fatalf("Result = %+v, want failure + note + out", res)
	}
}

func TestMaaExecutorHonorsCanceledContext(t *testing.T) {
	called := false
	ex := &maaExecutor{run: func(model.Account, string, string) (bool, string, error) {
		called = true
		return true, "", nil
	}}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := ex.Run(ctx, Job{}); !errors.Is(err, context.Canceled) {
		t.Fatalf("err = %v, want context.Canceled", err)
	}
	if called {
		t.Fatal("executor should not run after context cancellation")
	}
}

// newRunnerFixture 写最小的 runner.json/accounts.json/state.json，供 New() 回归用。
func newRunnerFixture(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	accounts := `{"slots":1,"accounts":[{"id":"a01","name":"t","client":"Official",` +
		`"stage":"1-7","fallback":"1-7","emu":"1","daily":"daily_a01","enabled":true}]}`
	state := `{"tasks":{}}`
	cfg := `{"slots":1,"maxConcurrent":1,"maaExe":"x","configDir":"` + filepath.ToSlash(dir) +
		`","stateRoot":"` + filepath.ToSlash(dir) + `","stateFile":"` +
		filepath.ToSlash(filepath.Join(dir, "state.json")) + `","accountsFile":"` +
		filepath.ToSlash(filepath.Join(dir, "accounts.json")) + `"}`
	for name, body := range map[string]string{
		"accounts.json": accounts,
		"state.json":    state,
		"runner.json":   cfg,
	} {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	return filepath.Join(dir, "runner.json")
}

func TestNewRegistersMaaDailyAndAnn(t *testing.T) {
	r, err := New(newRunnerFixture(t))
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	if got := r.executors.Kinds(); !slices.Equal(got, []string{"ann", "daily"}) {
		t.Fatalf("registered kinds = %v, want [ann daily]", got)
	}
	if _, ok := r.executors.For("rogue"); ok {
		t.Fatal("Phase 1 keeps the resident lane on startMaa; rogue must not be registered yet")
	}
}

func TestRunTaskUnknownKindFailsClosed(t *testing.T) {
	r, err := New(newRunnerFixture(t))
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	task := &Task{
		Key: "pi.demo.nav:a01", AccountID: "a01", Kind: "pi.demo.nav",
		File: "nav", State: "queued",
	}
	r.state.Tasks[task.Key] = task

	r.runTask(task)

	if task.State != "failed" {
		t.Fatalf("state = %q, want failed", task.State)
	}
	if task.Attempts != maxAttempts {
		t.Fatalf("attempts = %d, want %d (terminal, no retry loop)", task.Attempts, maxAttempts)
	}
	if !strings.Contains(task.Note, "没有注册执行器") {
		t.Fatalf("note = %q, want 没有注册执行器", task.Note)
	}
}
