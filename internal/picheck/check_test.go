package picheck

import (
	"os"
	"path/filepath"
	"testing"
)

func TestCheckProjectOverlayAndJumpBack(t *testing.T) {
	tmp := t.TempDir()
	proj := filepath.Join(tmp, "sample")
	basePipe := filepath.Join(proj, "resource", "pipeline")
	ovPipe := filepath.Join(proj, "resource", "overlay", "pipeline")
	ovImg := filepath.Join(proj, "resource", "overlay", "image")
	for _, d := range []string{basePipe, ovPipe, ovImg} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	iface := `{
		"resource": [{"name": "Test", "path": ["resource", "resource/overlay"]}],
		"task": [{"name": "Start", "entry": "NodeA"}]
	}`
	if err := os.WriteFile(filepath.Join(proj, "interface.json"), []byte(iface), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(basePipe, "base.json"), []byte(`{"NodeA":{"next":["[JumpBack]NodeB"]}}`), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(ovPipe, "ov.json"), []byte(`{"NodeB":{"template":"ok.png"}}`), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(ovImg, "ok.png"), []byte("png"), 0o644); err != nil {
		t.Fatal(err)
	}

	rep := CheckProject(proj)
	if len(rep.Errors) != 0 {
		t.Fatalf("expected 0 errors, got %v", rep.Errors)
	}
	if rep.Layers != 2 || rep.Files != 2 || rep.Nodes != 2 {
		t.Fatalf("unexpected counts: %+v", rep)
	}

	// Missing ref should fail
	if err := os.WriteFile(filepath.Join(basePipe, "base.json"), []byte(`{"NodeA":{"next":["MissingNode"]}}`), 0o644); err != nil {
		t.Fatal(err)
	}
	rep2 := CheckProject(proj)
	if len(rep2.Errors) == 0 {
		t.Fatal("expected error for missing ref")
	}
}
