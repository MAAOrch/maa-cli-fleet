package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"time"

	"github.com/xiachk083-hub/maa-cli-fleet/internal/agent"
)

// runAgent is the small CLI wrapper around internal/agent. It is deliberately
// stateless: each subcommand observes or acts once. The long-running loop will
// live in the runner/agent daemon later.
func runAgent(args []string, root string) error {
	if len(args) == 0 {
		return fmt.Errorf("usage: fleet agent <shot|state|tap|swipe|text|key|copilot> ...")
	}
	sub := args[0]
	rest := args[1:]

	switch sub {
	case "shot":
		adb := flagValue(rest, "-adb", "adb")
		serial := flagValue(rest, "-serial", "")
		out := flagValue(rest, "-out", "")
		ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
		defer cancel()
		raw, err := (&agent.AdbEye{Adb: adb, Serial: serial}).Observe(ctx)
		if err != nil {
			return err
		}
		png, ok := raw.([]byte)
		if !ok {
			return fmt.Errorf("shot: unexpected type %T", raw)
		}
		if out == "" {
			out = filepath.Join(root, "shots", "agent_"+time.Now().Format("20060102_150405")+".png")
		}
		if err := os.MkdirAll(filepath.Dir(out), 0o755); err != nil {
			return err
		}
		if err := os.WriteFile(out, png, 0o644); err != nil {
			return err
		}
		fmt.Println(out)

	case "state":
		path := flagValue(rest, "-log", "")
		ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
		defer cancel()
		raw, err := (&agent.MaaLogEye{Path: path}).Observe(ctx)
		if err != nil {
			return err
		}
		return json.NewEncoder(os.Stdout).Encode(raw)

	case "tap":
		ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
		defer cancel()
		return (&agent.AdbHand{Adb: flagValue(rest, "-adb", "adb")}).Do(ctx, agent.Command{
			Device: flagValue(rest, "-serial", ""),
			Hand:   "adb",
			Args: map[string]any{
				"action": "tap",
				"x":      intValue(rest, "-x", 0),
				"y":      intValue(rest, "-y", 0),
			},
		})

	case "swipe":
		ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
		defer cancel()
		return (&agent.AdbHand{Adb: flagValue(rest, "-adb", "adb")}).Do(ctx, agent.Command{
			Device: flagValue(rest, "-serial", ""),
			Hand:   "adb",
			Args: map[string]any{
				"action":   "swipe",
				"x1":       intValue(rest, "-x1", 0),
				"y1":       intValue(rest, "-y1", 0),
				"x2":       intValue(rest, "-x2", 0),
				"y2":       intValue(rest, "-y2", 0),
				"duration": intValue(rest, "-duration", 300),
			},
		})

	case "text":
		ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
		defer cancel()
		return (&agent.AdbHand{Adb: flagValue(rest, "-adb", "adb")}).Do(ctx, agent.Command{
			Device: flagValue(rest, "-serial", ""),
			Hand:   "adb",
			Args: map[string]any{
				"action": "text",
				"text":   flagValue(rest, "-text", ""),
			},
		})

	case "key":
		ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
		defer cancel()
		return (&agent.AdbHand{Adb: flagValue(rest, "-adb", "adb")}).Do(ctx, agent.Command{
			Device: flagValue(rest, "-serial", ""),
			Hand:   "adb",
			Args: map[string]any{
				"action": "keyevent",
				"key":    flagValue(rest, "-key", ""),
			},
		})

	case "battle":
		cfg := flagValue(rest, "-config", filepath.Join(root, "tools", "arknights_agent", "example_config.json"))
		args := []string{"-m", "tools.arknights_agent.runtime", "--config", cfg}
		if hasFlag(rest, "-dry-run") {
			args = append(args, "--dry-run")
		}
		if v := flagValue(rest, "-replay", ""); v != "" {
			args = append(args, "--replay", v)
		}
		if hasFlag(rest, "-once") {
			args = append(args, "--once")
		}
		dir := root
		if _, err := os.Stat(filepath.Join(dir, "tools", "arknights_agent", "runtime.py")); err != nil {
			if wd, err := os.Getwd(); err == nil {
				dir = wd
			}
		}
		cmd := exec.CommandContext(context.Background(), "python", args...)
		cmd.Dir = dir
		cmd.Stdout = os.Stdout
		cmd.Stderr = os.Stderr
		return cmd.Run()

	case "copilot":
		job := flagValue(rest, "-job", "")
		serial := flagValue(rest, "-serial", "")
		timeout := intValue(rest, "-timeout", 20*60)
		ctx, cancel := context.WithTimeout(context.Background(), time.Duration(timeout)*time.Second)
		defer cancel()
		return (&agent.MaaCopilotHand{
			MaaExe:    flagValue(rest, "-maa", ""),
			ConfigDir: flagValue(rest, "-config", ""),
			DataDir:   flagValue(rest, "-data", ""),
			CacheDir:  flagValue(rest, "-cache", ""),
			StateDir:  flagValue(rest, "-state", ""),
			Profile:   flagValue(rest, "-profile", ""),
			TaskDir:   flagValue(rest, "-taskdir", ""),
			Timeout:   time.Duration(timeout) * time.Second,
		}).Do(ctx, agent.Command{
			Device: serial,
			Hand:   "maa-copilot",
			Args:   map[string]any{"job": job},
		})

	default:
		return fmt.Errorf("unknown agent subcommand %q", sub)
	}
	return nil
}

func intValue(args []string, key string, def int) int {
	raw := flagValue(args, key, "")
	if raw == "" {
		return def
	}
	n, err := strconv.Atoi(raw)
	if err != nil {
		return def
	}
	return n
}
