package agent

import (
	"context"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"
)

const defaultMaaTailBytes int64 = 256 * 1024

// MaaLogState is the latest task-chain state parsed from an asst.log. It is a
// cheap ground-truth eye: no screenshot, no OCR, just what MAA already knows.
type MaaLogState struct {
	TaskChain string
	Status    string // started | completed | error | unknown
	LastError string
	Lines     []string
}

// MaaLogEye tails one asst.log and extracts the most recent task-chain event.
type MaaLogEye struct {
	Path      string
	Label     string
	TailBytes int64
}

func (e *MaaLogEye) Name() string {
	if e.Label != "" {
		return e.Label
	}
	return "maa-log"
}

func (e *MaaLogEye) Observe(ctx context.Context) (any, error) {
	if e.Path == "" {
		return nil, fmt.Errorf("maa log eye: path is empty")
	}
	select {
	case <-ctx.Done():
		return nil, ctx.Err()
	default:
	}

	tail := e.TailBytes
	if tail <= 0 {
		tail = defaultMaaTailBytes
	}
	f, err := os.Open(e.Path)
	if err != nil {
		return nil, fmt.Errorf("maa log eye: open: %w", err)
	}
	defer f.Close()

	info, err := f.Stat()
	if err != nil {
		return nil, fmt.Errorf("maa log eye: stat: %w", err)
	}
	offset := info.Size() - tail
	if offset < 0 {
		offset = 0
	}
	if _, err := f.Seek(offset, io.SeekStart); err != nil {
		return nil, fmt.Errorf("maa log eye: seek: %w", err)
	}
	b, err := io.ReadAll(f)
	if err != nil {
		return nil, fmt.Errorf("maa log eye: read: %w", err)
	}

	lines := strings.Split(string(b), "\n")
	if offset > 0 && len(lines) > 0 {
		lines = lines[1:] // drop the partial first line
	}
	return parseMaaLog(lines), nil
}

var reTaskChain = regexp.MustCompile(`"taskchain"\s*:\s*"([^"]+)"`)

func parseMaaLog(lines []string) MaaLogState {
	st := MaaLogState{Status: "unknown"}
	for _, line := range lines {
		if strings.TrimSpace(line) == "" {
			continue
		}
		if len(st.Lines) >= 5 {
			st.Lines = st.Lines[1:]
		}
		st.Lines = append(st.Lines, strings.TrimSpace(line))

		switch {
		case strings.Contains(line, "TaskChainStart"):
			st.Status = "started"
			if m := reTaskChain.FindStringSubmatch(line); len(m) == 2 {
				st.TaskChain = m[1]
			}
		case strings.Contains(line, "TaskChainCompleted"):
			st.Status = "completed"
			if m := reTaskChain.FindStringSubmatch(line); len(m) == 2 {
				st.TaskChain = m[1]
			}
		case strings.Contains(line, "TaskChainError"):
			st.Status = "error"
			st.LastError = strings.TrimSpace(line)
			if m := reTaskChain.FindStringSubmatch(line); len(m) == 2 {
				st.TaskChain = m[1]
			}
		case strings.Contains(line, "SubTaskError"):
			st.LastError = strings.TrimSpace(line)
		}
	}
	return st
}

// MaaCopilotHand runs a MAA Copilot job through maa-cli. It writes a small
// task TOML, starts `maa --batch run <task> -a <serial> -p <profile>`, and
// treats a Completed summary as success.
type MaaCopilotHand struct {
	MaaExe    string
	ConfigDir string
	DataDir   string
	CacheDir  string
	StateDir  string
	Profile   string
	TaskDir   string
	Timeout   time.Duration
}

func (h *MaaCopilotHand) Name() string { return "maa-copilot" }
func (h *MaaCopilotHand) Risk() Risk   { return RiskMedium }

func (h *MaaCopilotHand) Do(ctx context.Context, cmd Command) error {
	if h.MaaExe == "" {
		return fmt.Errorf("maa copilot: maa path is empty")
	}
	if cmd.Device == "" {
		return fmt.Errorf("maa copilot: device is empty")
	}
	job, _ := cmd.Args["job"].(string)
	if job == "" {
		return fmt.Errorf("maa copilot: job is empty")
	}
	if _, err := os.Stat(job); err != nil {
		return fmt.Errorf("maa copilot: job %s: %w", job, err)
	}

	taskName := "agent_copilot_" + strconv.FormatInt(time.Now().UnixNano(), 10)
	taskDir := h.TaskDir
	if taskDir == "" {
		taskDir = filepath.Join(h.ConfigDir, "tasks")
	}
	if err := os.MkdirAll(taskDir, 0o755); err != nil {
		return fmt.Errorf("maa copilot: task dir: %w", err)
	}
	taskPath := filepath.Join(taskDir, taskName+".toml")
	if err := os.WriteFile(taskPath, []byte(copilotTaskTOML(job)), 0o644); err != nil {
		return fmt.Errorf("maa copilot: write task: %w", err)
	}

	timeout := h.Timeout
	if timeout <= 0 {
		timeout = 20 * time.Minute
	}
	cctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	args := []string{"--batch", "run", taskName, "-a", cmd.Device}
	if h.Profile != "" {
		args = append(args, "-p", h.Profile)
	}
	command := exec.CommandContext(cctx, h.MaaExe, args...)
	command.Dir = filepath.Dir(h.MaaExe)
	command.Env = append(os.Environ(),
		"MAA_CONFIG_DIR="+h.ConfigDir,
		"MAA_DATA_DIR="+h.DataDir,
		"MAA_CACHE_DIR="+h.CacheDir,
		"MAA_STATE_DIR="+h.StateDir,
	)

	out, err := command.CombinedOutput()
	text := string(out)
	if err != nil {
		return fmt.Errorf("maa copilot: %w: %s", err, trim(out, 800))
	}
	if strings.Contains(text, "ExceptionCode 0x") || strings.Contains(text, "TOML parse error") {
		return fmt.Errorf("maa copilot: hard failure: %s", trim(out, 800))
	}
	if !strings.Contains(text, "Completed") {
		return fmt.Errorf("maa copilot: no Completed summary: %s", trim(out, 800))
	}
	return nil
}

func copilotTaskTOML(job string) string {
	escaped := strings.ReplaceAll(job, `\`, `\\`)
	escaped = strings.ReplaceAll(escaped, `"`, `\"`)
	return fmt.Sprintf(`[[tasks]]
name = "Copilot"
type = "Copilot"
params = { filename = "%s", formation = true, formation_index = 0, loop_times = 1, use_sanity_potion = false, ignore_requirements = true }
`, escaped)
}
