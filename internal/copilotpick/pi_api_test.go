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

func TestPiProjectsAndEnqueueConfigPaths(t *testing.T) {
	tmp := t.TempDir()
	piDir := filepath.Join(tmp, "custom_pi")
	projDir := filepath.Join(piDir, "arknights-event")
	if err := os.MkdirAll(projDir, 0o755); err != nil {
		t.Fatal(err)
	}
	iface := `{"resource":[{"name":"Arknights"}],"task":[{"name":"收结算页"},{"name":"进入普通图"}]}`
	if err := os.WriteFile(filepath.Join(projDir, "interface.json"), []byte(iface), 0o644); err != nil {
		t.Fatal(err)
	}
	queueFile := filepath.Join(tmp, "runner", "queue.in")

	cfg := DefaultConfig(tmp)
	cfg.PiDir = piDir
	cfg.QueueInFile = queueFile
	mux := NewHandler(NewManager(cfg))

	// 1. GET /api/pi/projects reads configured PiDir
	rec := httptest.NewRecorder()
	mux.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/api/pi/projects", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("GET /api/pi/projects status=%d body=%s", rec.Code, rec.Body.String())
	}
	var projects []struct {
		Name     string   `json:"name"`
		Resource string   `json:"resource"`
		Entries  []string `json:"entries"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &projects); err != nil {
		t.Fatalf("decode projects: %v", err)
	}
	if len(projects) != 1 || projects[0].Name != "arknights-event" || projects[0].Resource != "Arknights" || len(projects[0].Entries) != 2 {
		t.Fatalf("unexpected projects: %+v", projects)
	}

	// 2. POST /api/pi/enqueue writes valid JSON line to QueueInFile
	body := bytes.NewBufferString(`{"project":"arknights-event","entry":"收结算页","account":"a01"}`)
	rec2 := httptest.NewRecorder()
	mux.ServeHTTP(rec2, httptest.NewRequest(http.MethodPost, "/api/pi/enqueue", body))
	if rec2.Code != http.StatusOK {
		t.Fatalf("POST /api/pi/enqueue status=%d body=%s", rec2.Code, rec2.Body.String())
	}
	raw, err := os.ReadFile(queueFile)
	if err != nil {
		t.Fatalf("read queue.in: %v", err)
	}
	var op map[string]string
	if err := json.Unmarshal([]byte(strings.TrimSpace(string(raw))), &op); err != nil {
		t.Fatalf("queue.in line invalid json %q: %v", string(raw), err)
	}
	if op["op"] != "enqueue" || op["kind"] != "pi.arknights-event.收结算页" || op["account"] != "a01" {
		t.Fatalf("unexpected op: %+v", op)
	}

	// 3. Invalid characters rejected
	bad := bytes.NewBufferString(`{"project":"bad.proj","entry":"x","account":"a01"}`)
	rec3 := httptest.NewRecorder()
	mux.ServeHTTP(rec3, httptest.NewRequest(http.MethodPost, "/api/pi/enqueue", bad))
	if rec3.Code != http.StatusBadRequest {
		t.Fatalf("expected 400 for invalid project, got %d", rec3.Code)
	}
}
