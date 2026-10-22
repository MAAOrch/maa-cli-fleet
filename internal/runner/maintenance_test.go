package runner

import (
	"os"
	"path/filepath"
	"testing"
)

// 维护模式：一条已经在队列里的任务，开着维护必须原地不动（不派车、不进 active）。
// 事故背景（2026-10-03 13:0x）：用户手工开模拟器登号，sweep 每几分钟把它当孤儿关掉。
func TestMaintenanceBlocksDispatch(t *testing.T) {
	dir := t.TempDir()
	acc := `{"slots":1,"accounts":[{"id":"a01","name":"t","client":"Official","stage":"1-7",` +
		`"fallback":"1-7","emu":"1","daily":"daily_a01","state":"state_a01","enabled":true}]}`
	if err := os.WriteFile(filepath.Join(dir, "accounts.json"), []byte(acc), 0o644); err != nil {
		t.Fatal(err)
	}
	st := `{"tasks":{"daily:a01":{"key":"daily:a01","accountId":"a01","kind":"daily","file":"daily_a01",` +
		`"state":"queued","attempts":0,"day":"2000-01-01"}}}`
	if err := os.WriteFile(filepath.Join(dir, "state.json"), []byte(st), 0o644); err != nil {
		t.Fatal(err)
	}
	cfg := `{"slots":1,"maxConcurrent":1,"mumuManager":"x","adbExe":"x","maaExe":"x","configDir":"` +
		filepath.ToSlash(dir) + `","stateRoot":"` + filepath.ToSlash(dir) + `","stateFile":"` +
		filepath.ToSlash(filepath.Join(dir, "state.json")) + `","accountsFile":"` +
		filepath.ToSlash(filepath.Join(dir, "accounts.json")) + `"}`
	cfgPath := filepath.Join(dir, "runner.json")
	if err := os.WriteFile(cfgPath, []byte(cfg), 0o644); err != nil {
		t.Fatal(err)
	}
	r, err := New(cfgPath)
	if err != nil {
		t.Fatal(err)
	}
	if r.MaintenanceOn() {
		t.Fatal("默认不该是维护模式")
	}
	if err := SetMaintenance(cfgPath, "on"); err != nil {
		t.Fatal(err)
	}
	if !r.MaintenanceOn() {
		t.Fatal("维护文件建了但没认出来")
	}
	r.Tick()
	if n := len(r.active); n != 0 {
		t.Fatalf("维护模式下不该派车，active=%d", n)
	}
	if st := r.state.Tasks["daily:a01"].State; st != "queued" {
		t.Fatalf("维护模式下任务该原地排队，实际 state=%s", st)
	}
	if err := SetMaintenance(cfgPath, "off"); err != nil {
		t.Fatal(err)
	}
	if r.MaintenanceOn() {
		t.Fatal("删了维护文件还认成维护中")
	}
}
