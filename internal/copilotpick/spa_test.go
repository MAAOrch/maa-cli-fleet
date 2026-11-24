package copilotpick

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestSpaEndpoints(t *testing.T) {
	root := t.TempDir()
	if err := os.MkdirAll(filepath.Join(root, "config"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(root, "logs"), 0o755); err != nil {
		t.Fatal(err)
	}

	batchCfg := `{
		"accounts": [
			{"id":"spa_04","name":"皮埃尔商店","client":"Official","emu":"4"},
			{"id":"spa_08","name":"摸摸摸摸鱼","client":"Bilibili","emu":"8"}
		]
	}`
	if err := os.WriteFile(filepath.Join(root, "config", "spa_cn_batch.json"), []byte(batchCfg), 0o644); err != nil {
		t.Fatal(err)
	}
	statusJSON := `{"started_at":"2026-11-24 17:10:00","accounts":{"4":{"name":"皮埃尔商店","emu":4,"status":"running"}}}`
	if err := os.WriteFile(filepath.Join(root, "logs", "spa_batch_status.json"), []byte(statusJSON), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "logs", "spa_emu04.log"), []byte("[emu=4] [PROGRESS] Strategy=歌利亚 passed=1/3\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	cfg := DefaultConfig(root)
	m := NewManager(cfg)
	h := NewHandler(m)

	// 1. GET /api/spa/status
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/spa/status", nil)
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("status code = %d, want 200; body=%s", rec.Code, rec.Body.String())
	}
	var got struct {
		MaaV623Ready   bool              `json:"maaV623Ready"`
		RunnerReady    bool              `json:"runnerReady"`
		ConfigAccounts []SpaBatchAccount `json:"configAccounts"`
		Status         map[string]any    `json:"status"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil {
		t.Fatalf("decode status: %v", err)
	}
	if len(got.ConfigAccounts) != 2 {
		t.Fatalf("configAccounts = %d, want 2", len(got.ConfigAccounts))
	}
	if got.MaaV623Ready || got.RunnerReady {
		t.Fatalf("expected maaV623Ready=false and runnerReady=false in temp dir")
	}
	if got.Status == nil {
		t.Fatalf("expected non-nil status")
	}

	// 2. GET /api/spa/log/4 -> reads spa_emu04.log
	rec = httptest.NewRecorder()
	req = httptest.NewRequest(http.MethodGet, "/api/spa/log/4", nil)
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK || !strings.Contains(rec.Body.String(), "passed=1/3") {
		t.Fatalf("log/4 code=%d body=%q", rec.Code, rec.Body.String())
	}

	// 3. Invalid emu rejected (path traversal guard)
	rec = httptest.NewRecorder()
	req = httptest.NewRequest(http.MethodGet, "/api/spa/log/bad_emu", nil)
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("invalid emu code=%d, want 400", rec.Code)
	}

	// 4. POST /api/spa/start fails cleanly when runner script missing
	rec = httptest.NewRecorder()
	req = httptest.NewRequest(http.MethodPost, "/api/spa/start", bytes.NewBufferString(`{"mode":"badge","target":3}`))
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("start missing runner code=%d, want 400; body=%s", rec.Code, rec.Body.String())
	}
}
