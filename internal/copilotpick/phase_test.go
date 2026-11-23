package copilotpick

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"github.com/xiachk083-hub/maa-cli-fleet/internal/copilotpick/model"
)

func TestInferStagePhase(t *testing.T) {
	cases := []struct {
		code, stageName, want string
	}{
		{"OS-1", "act46side_01", "main"},
		{"OS-10", "act46side_10", "main"},
		{"OS-EX-1", "act46side_ex01", "ex"},
		{"OS-EX-8", "act46side_ex08", "ex"},
		{"OS-S-1", "act46side_s01", "s"},
		{"OS-S-5", "act46side_s05", "s"},
		{"", "act55side_ex03", "ex"},
		{"", "act55side_s02", "s"},
		{"TL-9", "act55side_09", "main"},
	}
	for _, tc := range cases {
		if got := InferStagePhase(tc.code, tc.stageName); got != tc.want {
			t.Errorf("InferStagePhase(%q, %q) = %q, want %q", tc.code, tc.stageName, got, tc.want)
		}
	}
}

func TestGroupStagesByPhaseAndNormalize(t *testing.T) {
	opts := model.RunOptions{
		PhaseNav: true,
		Stages: []model.StagePlan{
			{StageCode: "OS-EX-2", StageName: "act46side_ex02", JobIDs: []int{202}},
			{StageCode: "OS-2", StageName: "act46side_02", JobIDs: []int{102}},
			{StageCode: "OS-S-1", StageName: "act46side_s01", JobIDs: []int{301}},
			{StageCode: "OS-1", StageName: "act46side_01", JobIDs: []int{101}},
			{StageCode: "OS-EX-1", StageName: "act46side_ex01", JobIDs: []int{201}},
		},
	}
	if err := normalizeStagePlans(&opts); err != nil {
		t.Fatalf("normalizeStagePlans: %v", err)
	}
	if !opts.OnMap {
		t.Fatal("PhaseNav should automatically enable OnMap")
	}
	wantCodes := []string{"OS-2", "OS-1", "OS-EX-2", "OS-EX-1", "OS-S-1"}
	wantPhases := []string{"main", "main", "ex", "ex", "s"}
	for i, st := range opts.Stages {
		if st.StageCode != wantCodes[i] || st.Phase != wantPhases[i] {
			t.Fatalf("stage[%d] = (%s, %s), want (%s, %s)", i, st.StageCode, st.Phase, wantCodes[i], wantPhases[i])
		}
	}
}

func TestPatchPiNavConfigPreservesAdbPath(t *testing.T) {
	dir := t.TempDir()
	cfgDir := filepath.Join(dir, "config")
	if err := os.MkdirAll(cfgDir, 0o755); err != nil {
		t.Fatal(err)
	}
	initial := `{"controller":{"name":"Android"},"adb":{"adb_path":"D:/adb.exe","address":"127.0.0.1:16384"},"resource":"Arknights"}`
	if err := os.WriteFile(filepath.Join(cfgDir, "maa_pi_config.json"), []byte(initial), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := patchPiNavConfig(dir, "127.0.0.1:16416", "", "进入EX图"); err != nil {
		t.Fatalf("patchPiNavConfig: %v", err)
	}
	raw, err := os.ReadFile(filepath.Join(cfgDir, "maa_pi_config.json"))
	if err != nil {
		t.Fatal(err)
	}
	var got map[string]any
	if err := json.Unmarshal(raw, &got); err != nil {
		t.Fatal(err)
	}
	adb := got["adb"].(map[string]any)
	if adb["adb_path"] != "D:/adb.exe" || adb["address"] != "127.0.0.1:16416" {
		t.Fatalf("adb = %+v", adb)
	}
	tasks := got["task"].([]any)
	if len(tasks) != 1 || tasks[0].(map[string]any)["name"] != "进入EX图" {
		t.Fatalf("tasks = %+v", tasks)
	}
}
