package model

import (
	"os"
	"path/filepath"
	"reflect"
	"testing"
)

func TestReadFileBOM(t *testing.T) {
	tmp := t.TempDir()
	p := filepath.Join(tmp, "bom.json")
	if err := os.WriteFile(p, append([]byte{0xEF, 0xBB, 0xBF}, []byte(`{"ok":true}`)...), 0o644); err != nil {
		t.Fatal(err)
	}
	got, err := ReadFileBOM(p)
	if err != nil {
		t.Fatalf("ReadFileBOM: %v", err)
	}
	if string(got) != `{"ok":true}` {
		t.Fatalf("ReadFileBOM = %q, want stripped BOM", string(got))
	}
}

func TestAccountResidentAndRotation(t *testing.T) {
	a1 := Account{ResidentTask: " spa_a ", RogueTask: "rogue_old"}
	if got := a1.ResidentTaskFile(); got != "spa_a" {
		t.Fatalf("ResidentTaskFile = %q, want spa_a", got)
	}
	if got := a1.RotationTasks(); !reflect.DeepEqual(got, []string{"spa_a"}) {
		t.Fatalf("RotationTasks = %v, want [spa_a]", got)
	}

	a2 := Account{RogueTask: "rogue_sarkaz"}
	if got := a2.ResidentTaskFile(); got != "rogue_sarkaz" {
		t.Fatalf("fallback RogueTask = %q", got)
	}

	a3 := Account{ResidentTasks: []string{" ", "task_a", "task_b "}}
	if got := a3.ResidentTaskFile(); got != "task_a" {
		t.Fatalf("first non-empty ResidentTasks = %q, want task_a", got)
	}
	if got := a3.RotationTasks(); !reflect.DeepEqual(got, []string{"task_a", "task_b"}) {
		t.Fatalf("RotationTasks = %v, want [task_a task_b]", got)
	}
}
