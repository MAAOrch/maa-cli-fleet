package runner

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestPiExecutorSuccess(t *testing.T) {
	dir := t.TempDir()
	logDir := t.TempDir()
	exe := filepath.Join(dir, "MaaPiCli.exe")
	var gotExe, gotDir string
	var gotArgs []string
	e := &piExecutor{
		exe:    exe,
		dir:    dir,
		logDir: logDir,
		run: func(ctx context.Context, exe string, args []string, d string) ([]byte, error) {
			gotExe, gotArgs, gotDir = exe, args, d
			return []byte("### All tasks have been completed ###\n"), nil
		},
	}

	res, err := e.Run(context.Background(), Job{Kind: "pi.demo", TaskFile: "Nav.ToHome"})
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if !res.OK || !strings.Contains(res.Note, "completed") {
		t.Fatalf("Result = %+v, want OK + completed note", res)
	}
	if gotExe != exe || gotDir != dir {
		t.Fatalf("run got exe/dir = %q/%q, want %q/%q", gotExe, gotDir, exe, dir)
	}
	if len(gotArgs) != 1 || gotArgs[0] != "-d" {
		t.Fatalf("args = %v, want [-d]", gotArgs)
	}
	if res.OutFile == "" {
		t.Fatal("OutFile should be set")
	}
	b, err := os.ReadFile(res.OutFile)
	if err != nil {
		t.Fatalf("read log: %v", err)
	}
	if !strings.Contains(string(b), "completed") {
		t.Fatalf("log = %q, want completed", string(b))
	}
}

func TestPiExecutorPerJobConfig(t *testing.T) {
	dir := t.TempDir()
	cfgDir := filepath.Join(dir, "config")
	if err := os.MkdirAll(cfgDir, 0o755); err != nil {
		t.Fatal(err)
	}
	cfgPath := filepath.Join(cfgDir, "maa_pi_config.json")
	initial := `{
  "controller": {"name": "Android"},
  "adb": {"name": "1-MuMuPlayer v5+", "adb_path": "D:/adb.exe", "address": "127.0.0.1:16384"},
  "resource": "Arknights",
  "task": [{"name": "收结算页", "option": []}]
}`
	if err := os.WriteFile(cfgPath, []byte(initial), 0o644); err != nil {
		t.Fatal(err)
	}

	var observed map[string]any
	e := &piExecutor{
		exe:    filepath.Join(dir, "MaaPiCli.exe"),
		dir:    dir,
		logDir: t.TempDir(),
		run: func(ctx context.Context, exe string, args []string, d string) ([]byte, error) {
			raw, err := os.ReadFile(cfgPath)
			if err != nil {
				return nil, err
			}
			_ = json.Unmarshal(raw, &observed)
			return []byte("### All tasks have been completed ###\n"), nil
		},
	}

	job := Job{
		Kind:     "pi.event.nav",
		TaskFile: "进入普通图",
		Device:   DeviceRef{ADBAddr: "127.0.0.1:16416"},
		Params:   map[string]any{"adb_name": "2-MuMuPlayer v5+"},
	}
	res, err := e.Run(context.Background(), job)
	if err != nil || !res.OK {
		t.Fatalf("Run failed: res=%+v err=%v", res, err)
	}
	adb, _ := observed["adb"].(map[string]any)
	if adb["address"] != "127.0.0.1:16416" || adb["name"] != "2-MuMuPlayer v5+" || adb["adb_path"] != "D:/adb.exe" {
		t.Fatalf("adb config mismatch: %+v", adb)
	}
	tasks, _ := observed["task"].([]any)
	if len(tasks) != 1 || tasks[0].(map[string]any)["name"] != "进入普通图" {
		t.Fatalf("task config mismatch: %+v", tasks)
	}
}

func TestRegistryPrefixMatch(t *testing.T) {
	reg := NewRegistry()
	piEx := &piExecutor{exe: "x"}
	if err := reg.Register("pi.*", piEx); err != nil {
		t.Fatal(err)
	}
	for _, kind := range []string{"pi.demo", "pi.event.nav", "pi.shop"} {
		if ex, ok := reg.For(kind); !ok || ex != piEx {
			t.Fatalf("For(%q) = (%v, %v), want piEx", kind, ex, ok)
		}
	}
	if _, ok := reg.For("daily"); ok {
		t.Fatal("For(daily) should not match pi.*")
	}
}

func TestPiExecutorFailure(t *testing.T) {
	e := &piExecutor{
		exe:    filepath.Join(t.TempDir(), "MaaPiCli.exe"),
		logDir: t.TempDir(),
		run: func(context.Context, string, []string, string) ([]byte, error) {
			return []byte("### No task to run ###\n"), errors.New("exit status 255")
		},
	}
	res, err := e.Run(context.Background(), Job{Kind: "pi.demo"})
	if err == nil {
		t.Fatal("Run should return the command error")
	}
	if res.OK {
		t.Fatalf("Result.OK = true, want false (%+v)", res)
	}
	if !strings.Contains(res.Note, "No task to run") || !strings.Contains(res.Note, "exit status 255") {
		t.Fatalf("Note = %q, want output + error", res.Note)
	}
}

func TestPiExecutorCanceled(t *testing.T) {
	called := false
	e := &piExecutor{
		exe: "x",
		run: func(context.Context, string, []string, string) ([]byte, error) {
			called = true
			return nil, nil
		},
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := e.Run(ctx, Job{}); !errors.Is(err, context.Canceled) {
		t.Fatalf("err = %v, want context.Canceled", err)
	}
	if called {
		t.Fatal("run should not be called after context cancellation")
	}
}

func TestPiExecutorEmptyExe(t *testing.T) {
	e := &piExecutor{}
	if _, err := e.Run(context.Background(), Job{}); err == nil {
		t.Fatal("empty exe should fail")
	}
}

func TestPiTailKeepsLastLines(t *testing.T) {
	out := []byte("1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n")
	got := piTail(out)
	if strings.Contains(got, "1\n") || !strings.Contains(got, "10") {
		t.Fatalf("piTail = %q, want only last lines", got)
	}
}

func TestPiLogNameSanitizesKind(t *testing.T) {
	if got := piLogName("pi.demo.Nav.ToHome"); strings.ContainsAny(got, ".:\\/") {
		t.Fatalf("piLogName = %q, still has separators", got)
	}
	if got := piLogName(""); got != "pi" {
		t.Fatalf("piLogName(empty) = %q, want pi", got)
	}
}
