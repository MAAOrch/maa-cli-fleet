package agent

import (
	"bytes"
	"context"
	"fmt"
	"os/exec"
	"strconv"
)

var pngMagic = []byte{0x89, 'P', 'N', 'G', '\r', '\n', 0x1a, '\n'}

// AdbEye captures a raw PNG screenshot from one device. It is intentionally
// dumb: no OCR, no interpretation. The planner decides whether to send the
// bytes to a vision model or to a template matcher.
type AdbEye struct {
	Adb    string
	Serial string
	Label  string
}

func (e *AdbEye) Name() string {
	if e.Label != "" {
		return e.Label
	}
	if e.Serial != "" {
		return "adb:" + e.Serial
	}
	return "adb"
}

func (e *AdbEye) Observe(ctx context.Context) (any, error) {
	if e.Adb == "" {
		return nil, fmt.Errorf("adb eye: adb path is empty")
	}
	if e.Serial == "" {
		return nil, fmt.Errorf("adb eye: serial is empty")
	}
	cmd := exec.CommandContext(ctx, e.Adb, "-s", e.Serial, "exec-out", "screencap", "-p")
	out, err := cmd.Output()
	if err != nil {
		return nil, fmt.Errorf("adb eye: screencap: %w", err)
	}
	if !bytes.HasPrefix(out, pngMagic) {
		return nil, fmt.Errorf("adb eye: screencap returned %d bytes, not a PNG", len(out))
	}
	return out, nil
}

// AdbHand performs taps, swipes, text input and key events on one device.
// It is the fallback hand for UI that MAA does not cover: tutorial dialogs,
// event shops, base layout, etc.
type AdbHand struct {
	Adb string
}

func (h *AdbHand) Name() string { return "adb" }
func (h *AdbHand) Risk() Risk   { return RiskMedium }

func (h *AdbHand) Do(ctx context.Context, cmd Command) error {
	if h.Adb == "" {
		return fmt.Errorf("adb hand: adb path is empty")
	}
	if cmd.Device == "" {
		return fmt.Errorf("adb hand: device is empty")
	}
	action, _ := cmd.Args["action"].(string)
	if action == "" {
		return fmt.Errorf("adb hand: action is empty")
	}

	args := []string{"-s", cmd.Device, "shell", "input"}
	switch action {
	case "tap":
		x, err := intArg(cmd, "x")
		if err != nil {
			return err
		}
		y, err := intArg(cmd, "y")
		if err != nil {
			return err
		}
		args = append(args, "tap", strconv.Itoa(x), strconv.Itoa(y))
	case "swipe":
		x1, err := intArg(cmd, "x1")
		if err != nil {
			return err
		}
		y1, err := intArg(cmd, "y1")
		if err != nil {
			return err
		}
		x2, err := intArg(cmd, "x2")
		if err != nil {
			return err
		}
		y2, err := intArg(cmd, "y2")
		if err != nil {
			return err
		}
		duration := 300
		if v, ok := intArgOptional(cmd, "duration"); ok {
			duration = v
		}
		args = append(args, "swipe", strconv.Itoa(x1), strconv.Itoa(y1), strconv.Itoa(x2), strconv.Itoa(y2), strconv.Itoa(duration))
	case "text":
		text, _ := cmd.Args["text"].(string)
		if text == "" {
			return fmt.Errorf("adb hand: text is empty")
		}
		// `adb shell input text` uses %s for spaces.
		text = replaceSpaces(text)
		args = append(args, "text", text)
	case "keyevent":
		key, _ := cmd.Args["key"].(string)
		if key == "" {
			return fmt.Errorf("adb hand: key is empty")
		}
		args = append(args, "keyevent", key)
	default:
		return fmt.Errorf("adb hand: unknown action %q", action)
	}

	out, err := exec.CommandContext(ctx, h.Adb, args...).CombinedOutput()
	if err != nil {
		return fmt.Errorf("adb hand %s: %w: %s", action, err, trim(out, 300))
	}
	return nil
}

func intArg(cmd Command, key string) (int, error) {
	v, ok := intArgOptional(cmd, key)
	if !ok {
		return 0, fmt.Errorf("adb hand: %s is required", key)
	}
	return v, nil
}

func intArgOptional(cmd Command, key string) (int, bool) {
	v, ok := cmd.Args[key]
	if !ok {
		return 0, false
	}
	switch n := v.(type) {
	case int:
		return n, true
	case int64:
		return int(n), true
	case float64:
		return int(n), true
	case string:
		i, err := strconv.Atoi(n)
		return i, err == nil
	default:
		return 0, false
	}
}

func replaceSpaces(s string) string {
	out := make([]rune, 0, len(s))
	for _, r := range s {
		if r == ' ' {
			out = append(out, '%', 's')
			continue
		}
		out = append(out, r)
	}
	return string(out)
}

func trim(b []byte, n int) string {
	if len(b) > n {
		b = b[len(b)-n:]
	}
	return string(b)
}
