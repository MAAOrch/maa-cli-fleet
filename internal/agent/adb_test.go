package agent

import (
	"context"
	"strings"
	"testing"
)

func TestAdbHandRejectsUnknownAction(t *testing.T) {
	h := &AdbHand{Adb: "adb"}
	err := h.Do(context.Background(), Command{
		Device: "127.0.0.1:16384",
		Hand:   "adb",
		Args:   map[string]any{"action": "nope"},
	})
	if err == nil || !strings.Contains(err.Error(), "unknown action") {
		t.Fatalf("err = %v, want unknown action", err)
	}
}

func TestAdbHandRejectsMissingTapCoords(t *testing.T) {
	h := &AdbHand{Adb: "adb"}
	err := h.Do(context.Background(), Command{
		Device: "127.0.0.1:16384",
		Hand:   "adb",
		Args:   map[string]any{"action": "tap", "x": 10},
	})
	if err == nil || !strings.Contains(err.Error(), "y is required") {
		t.Fatalf("err = %v, want y required", err)
	}
}

func TestAdbHandRejectsEmptyDevice(t *testing.T) {
	h := &AdbHand{Adb: "adb"}
	err := h.Do(context.Background(), Command{Hand: "adb", Args: map[string]any{"action": "tap", "x": 1, "y": 2}})
	if err == nil || !strings.Contains(err.Error(), "device is empty") {
		t.Fatalf("err = %v, want device empty", err)
	}
}

func TestReplaceSpaces(t *testing.T) {
	if got := replaceSpaces("hello world"); got != "hello%sworld" {
		t.Fatalf("replaceSpaces = %q", got)
	}
}
