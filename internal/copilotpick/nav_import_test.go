package copilotpick

import (
	"bytes"
	"encoding/json"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
)

func TestNavPackImportListAndDelete(t *testing.T) {
	tmp := t.TempDir()
	piDir := filepath.Join(tmp, "pi")
	projDir := filepath.Join(piDir, "arknights-event")
	if err := os.MkdirAll(projDir, 0o755); err != nil {
		t.Fatal(err)
	}
	iface := `{"resource":[{"name":"Arknights","path":["resource"]}],"task":[{"name":"进入普通图","entry":"NavMainStart"}]}`
	if err := os.WriteFile(filepath.Join(projDir, "interface.json"), []byte(iface), 0o644); err != nil {
		t.Fatal(err)
	}

	cfg := DefaultConfig(tmp)
	cfg.PiDir = piDir
	m := NewManager(cfg)
	mux := NewHandler(m)

	// 1. Upload a pipeline JSON exported from MPE/MAA visual editor
	var buf bytes.Buffer
	mw := multipart.NewWriter(&buf)
	_ = mw.WriteField("name", "新活动1阶段")
	_ = mw.WriteField("phase", "main")
	fw, _ := mw.CreateFormFile("file", "phase1.json")
	_, _ = fw.Write([]byte(`{"EventStart":{"next":["EventMap"]},"EventMap":{"action":"DoNothing"}}`))
	_ = mw.Close()

	req := httptest.NewRequest(http.MethodPost, "/api/nav/import", &buf)
	req.Header.Set("Content-Type", mw.FormDataContentType())
	rec := httptest.NewRecorder()
	mux.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("POST /api/nav/import status=%d body=%s", rec.Code, rec.Body.String())
	}
	var resp struct {
		OK   bool    `json:"ok"`
		Pack NavPack `json:"pack"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatal(err)
	}
	if !resp.OK || resp.Pack.Entry != "EventStart" || resp.Pack.TaskName != "导入:新活动1阶段" {
		t.Fatalf("unexpected pack: %+v", resp.Pack)
	}

	// 2. Verify interface.json now has resource/imported and the imported task
	rawIface, err := os.ReadFile(filepath.Join(projDir, "interface.json"))
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Contains(rawIface, []byte("resource/imported")) || !bytes.Contains(rawIface, []byte("导入:新活动1阶段")) {
		t.Fatalf("interface.json not synced: %s", rawIface)
	}

	// 3. List packs
	rec2 := httptest.NewRecorder()
	mux.ServeHTTP(rec2, httptest.NewRequest(http.MethodGet, "/api/nav/packs", nil))
	if rec2.Code != http.StatusOK || !bytes.Contains(rec2.Body.Bytes(), []byte(resp.Pack.ID)) {
		t.Fatalf("GET /api/nav/packs = %d %s", rec2.Code, rec2.Body.String())
	}

	// 4. Delete pack
	rec3 := httptest.NewRecorder()
	mux.ServeHTTP(rec3, httptest.NewRequest(http.MethodDelete, "/api/nav/packs/"+resp.Pack.ID, nil))
	if rec3.Code != http.StatusOK {
		t.Fatalf("DELETE /api/nav/packs status=%d body=%s", rec3.Code, rec3.Body.String())
	}
	if got := m.ListNavPacks(); len(got) != 0 {
		t.Fatalf("expected 0 packs after delete, got %d", len(got))
	}
}
