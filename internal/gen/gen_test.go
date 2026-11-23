package gen

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/xiachk083-hub/maa-cli-fleet/internal/model"
)

func TestGenRunPreservesEmuAndWritesArtifacts(t *testing.T) {
	tmp := t.TempDir()
	autoMas := filepath.Join(tmp, "automas")
	root := filepath.Join(tmp, "fleet")
	if err := os.MkdirAll(filepath.Join(autoMas, "config"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(root, "runner"), 0o755); err != nil {
		t.Fatal(err)
	}

	scriptCfg := `{
		"s1": {
			"Emulator": {"Index": "3"},
			"Info": {"Name": "script3"},
			"SubConfigsInfo": {
				"UserData": {
					"instances": [{"uid": "u1"}],
					"u1": {
						"Info": {
							"Name": "DoctorA",
							"Server": "Official",
							"Stage": "1-7",
							"StageMode": "normal",
							"Annihilation": "龙门市区"
						},
						"Task": {"IfStartUp": true, "IfFight": true}
					}
				}
			}
		}
	}`
	if err := os.WriteFile(filepath.Join(autoMas, "config", "ScriptConfig.json"), []byte(scriptCfg), 0o644); err != nil {
		t.Fatal(err)
	}
	// Existing accounts.json overrides emu from 3 -> 9
	existing := `{"accounts":[{"id":"a03","emu":"9"}]}`
	if err := os.WriteFile(filepath.Join(root, "runner", "accounts.json"), []byte(existing), 0o644); err != nil {
		t.Fatal(err)
	}

	if err := Run(autoMas, root, nil, false); err != nil {
		t.Fatalf("Run: %v", err)
	}

	rawAcc, err := os.ReadFile(filepath.Join(root, "runner", "accounts.json"))
	if err != nil {
		t.Fatal(err)
	}
	var af model.AccountsFile
	if err := json.Unmarshal(rawAcc, &af); err != nil {
		t.Fatal(err)
	}
	if len(af.Accounts) != 1 || af.Accounts[0].ID != "a03" || af.Accounts[0].Emu != "9" {
		t.Fatalf("accounts = %+v, want a03 with preserved emu=9", af.Accounts)
	}

	annToml, err := os.ReadFile(filepath.Join(root, "config", "tasks", "ann_a03.toml"))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(annToml), "LungmenDowntown@Annihilation") || !strings.Contains(string(annToml), `type = "StartUp"`) {
		t.Fatalf("ann_a03.toml missing expected stage/StartUp:\n%s", string(annToml))
	}
}
