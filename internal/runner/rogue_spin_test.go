package runner

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestLastCurTask(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "asst.log")
	body := strings.Join([]string{
		`[2026-10-05 11:32:08.100][INF] {"cur_task":"Sarkaz@Roguelike@Begin"}`,
		`[2026-10-05 11:32:09.100][INF] {"cur_task":"RoguelikeBattleExitBegin","details":{}}`,
		`[2026-10-05 11:32:10.100][INF] {"cur_task":""}`,
		`noise without the field`,
	}, "\n") + "\n"
	if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	got, ok := lastCurTask(p)
	if !ok || got != "" {
		t.Fatalf("last empty task: ok=%v got=%q", ok, got)
	}

	// 尾巴超过读取窗口时，仍取窗口内最后一条，不把切到一半的行当结果。
	var b strings.Builder
	b.WriteString("cur_task\":\"CUTOFF\" partial\n")
	for i := 0; i < 4000; i++ {
		b.WriteString("[2026-10-05 15:43:14.000][INF] {\"cur_task\":\"RoguelikeBattleExitBegin\"}\n")
	}
	b.WriteString("[2026-10-05 15:43:15.000][INF] {\"cur_task\":\"Sarkaz@Next\"}\n")
	if err := os.WriteFile(p, []byte(b.String()), 0o644); err != nil {
		t.Fatal(err)
	}
	got, ok = lastCurTask(p)
	if !ok || got != "Sarkaz@Next" {
		t.Fatalf("tail task: ok=%v got=%q", ok, got)
	}

	if _, ok := lastCurTask(filepath.Join(dir, "missing.log")); ok {
		t.Fatal("missing file should not count as a task")
	}
}
