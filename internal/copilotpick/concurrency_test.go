package copilotpick

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/xiachk083-hub/maa-cli-fleet/internal/copilotpick/model"
)

func TestActiveRunsMultiEmulatorIsolation(t *testing.T) {
	cfg := DefaultConfig(t.TempDir())
	m := NewManager(cfg)

	r1 := newRun(m, "run-1", t.TempDir(), model.Account{ID: "a01", Adb: "127.0.0.1:16384"}, model.RunOptions{
		AccountID: "a01", StageCode: "OS-1", StageName: "act46side_01", JobIDs: []int{1},
	})
	r1.setPhase("main")
	r2 := newRun(m, "run-2", t.TempDir(), model.Account{ID: "a02", Adb: "127.0.0.1:16416"}, model.RunOptions{
		AccountID: "a02", StageCode: "OS-EX-1", StageName: "act46side_ex01", JobIDs: []int{2},
	})
	r2.setPhase("ex")

	m.runMu.Lock()
	m.runs[r1.ID] = r1
	m.runs[r2.ID] = r2
	m.activeByAcc["a01"] = r1.ID
	m.activeByAcc["a02"] = r2.ID
	m.activeID = r2.ID
	m.runMu.Unlock()

	active := m.ActiveRuns()
	if len(active) != 2 {
		t.Fatalf("ActiveRuns = %d, want 2", len(active))
	}
	if active[0].AccountID != "a01" || active[0].Phase != "main" || active[1].AccountID != "a02" || active[1].Phase != "ex" {
		t.Fatalf("unexpected active runs: %+v", active)
	}

	// Verify GET /api/runs/active endpoint
	mux := NewHandler(m)
	rec := httptest.NewRecorder()
	mux.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/api/runs/active", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("GET /api/runs/active status=%d", rec.Code)
	}
	var payload struct {
		Runs []model.RunSnapshot `json:"runs"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &payload); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if len(payload.Runs) != 2 {
		t.Fatalf("HTTP /api/runs/active len=%d, want 2", len(payload.Runs))
	}

	// Finishing r1 leaves only r2 active
	r1.finish("success", "")
	if got := m.ActiveRuns(); len(got) != 1 || got[0].AccountID != "a02" {
		t.Fatalf("after r1 done, ActiveRuns = %+v", got)
	}
}
