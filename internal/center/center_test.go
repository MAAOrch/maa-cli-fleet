package center

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestCenterLifecycleAuthAndPersistence(t *testing.T) {
	dir := t.TempDir()
	srv, err := New(dir, "secret-token")
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	h := srv.Handler()

	// 1. /health is public
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/health", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("/health status=%d", rec.Code)
	}

	// 2. /fleet requires X-Fleet-Token
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/fleet", nil))
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("/fleet without token status=%d, want 401", rec.Code)
	}

	// 3. /register node
	regReq := httptest.NewRequest(http.MethodPost, "/register", bytes.NewBufferString(`{"node":{"id":"n1","ver":"v2","host":"win-box"}}`))
	regReq.Header.Set("X-Fleet-Token", "secret-token")
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, regReq)
	if rec.Code != http.StatusOK {
		t.Fatalf("/register status=%d body=%s", rec.Code, rec.Body.String())
	}

	// 4. /report state + PI event
	evReq := httptest.NewRequest(http.MethodPost, "/report", bytes.NewBufferString(`{"node_id":"n1","kind":"event","data":{"task":"pi.arknights-event.收结算页:a01","ok":true}}`))
	evReq.Header.Set("X-Fleet-Token", "secret-token")
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, evReq)
	if rec.Code != http.StatusOK {
		t.Fatalf("/report event status=%d body=%s", rec.Code, rec.Body.String())
	}

	// 5. /fleet returns online node + recorded event
	fleetReq := httptest.NewRequest(http.MethodGet, "/fleet", nil)
	fleetReq.Header.Set("X-Fleet-Token", "secret-token")
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, fleetReq)
	if rec.Code != http.StatusOK {
		t.Fatalf("/fleet status=%d", rec.Code)
	}
	var snap struct {
		OK     bool                    `json:"ok"`
		Nodes  map[string]map[string]any `json:"nodes"`
		Events []map[string]any        `json:"events"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &snap); err != nil {
		t.Fatalf("decode /fleet: %v", err)
	}
	if !snap.OK || snap.Nodes["n1"] == nil || snap.Nodes["n1"]["online"] != true {
		t.Fatalf("unexpected /fleet nodes: %+v", snap.Nodes)
	}
	if len(snap.Events) < 2 {
		t.Fatalf("expected >=2 events (register + event), got %d", len(snap.Events))
	}

	// 6. Reload from disk preserves token and registered node
	srv2, err := New(dir, "")
	if err != nil {
		t.Fatalf("reload New: %v", err)
	}
	if srv2.Token() == "" {
		t.Fatal("reloaded token should not be empty")
	}
	if srv2.nodes["n1"] == nil || srv2.nodes["n1"].Host != "win-box" {
		t.Fatalf("reloaded node missing: %+v", srv2.nodes)
	}
}
