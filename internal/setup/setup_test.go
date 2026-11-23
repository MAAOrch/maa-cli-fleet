package setup

import (
	"archive/zip"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func writeZip(t *testing.T, path string, entries map[string]string) {
	t.Helper()
	f, err := os.Create(path)
	if err != nil {
		t.Fatal(err)
	}
	zw := zip.NewWriter(f)
	for name, body := range entries {
		w, err := zw.Create(name)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := w.Write([]byte(body)); err != nil {
			t.Fatal(err)
		}
	}
	if err := zw.Close(); err != nil {
		t.Fatal(err)
	}
	if err := f.Close(); err != nil {
		t.Fatal(err)
	}
}

func TestUnzipAllNormalAndRejectsZipSlip(t *testing.T) {
	tmp := t.TempDir()
	goodZip := filepath.Join(tmp, "good.zip")
	writeZip(t, goodZip, map[string]string{
		"a.txt":         "hello",
		"sub/b.txt":     "world",
	})
	outDir := filepath.Join(tmp, "out")
	if err := unzipAll(goodZip, outDir); err != nil {
		t.Fatalf("unzipAll good: %v", err)
	}
	got, err := os.ReadFile(filepath.Join(outDir, "sub", "b.txt"))
	if err != nil || string(got) != "world" {
		t.Fatalf("extracted sub/b.txt = %q, %v", got, err)
	}

	evilZip := filepath.Join(tmp, "evil.zip")
	writeZip(t, evilZip, map[string]string{
		"../escaped.txt": "pwn",
	})
	err = unzipAll(evilZip, filepath.Join(tmp, "safe"))
	if err == nil || !strings.Contains(err.Error(), "路径穿越") {
		t.Fatalf("expected zip-slip error, got %v", err)
	}
}

func TestDownloadToChecksHTTPStatus(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/ok" {
			_, _ = w.Write([]byte("payload"))
			return
		}
		http.Error(w, "not found", http.StatusNotFound)
	}))
	defer srv.Close()

	tmp := t.TempDir()
	okDst := filepath.Join(tmp, "ok.bin")
	if err := downloadTo(srv.URL+"/ok", okDst); err != nil {
		t.Fatalf("downloadTo /ok: %v", err)
	}
	b, err := os.ReadFile(okDst)
	if err != nil || string(b) != "payload" {
		t.Fatalf("downloaded content = %q, %v", b, err)
	}

	errDst := filepath.Join(tmp, "err.bin")
	if err := downloadTo(srv.URL+"/missing", errDst); err == nil || !strings.Contains(err.Error(), "HTTP 404") {
		t.Fatalf("expected HTTP 404 error, got %v", err)
	}
}
