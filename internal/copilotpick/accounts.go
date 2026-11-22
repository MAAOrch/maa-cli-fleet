package copilotpick

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/xiachk083-hub/maa-cli-fleet/internal/copilotpick/model"
)

// accountsFile 是 runner/accounts.json 的结构（只取需要的字段）。
type accountsFile struct {
	Accounts []struct {
		ID      string `json:"id"`
		Name    string `json:"name"`
		Client  string `json:"client"`
		Emu     string `json:"emu"`
		Daily   string `json:"daily"`
		Enabled bool   `json:"enabled"`
		Port    string `json:"port"`
	} `json:"accounts"`
}

// Accounts 读机队账号表，解析每个账号的 adb 地址与在线状态。
func (m *Manager) Accounts(ctx context.Context) ([]model.Account, error) {
	b, err := os.ReadFile(m.cfg.AccountsFile)
	if err != nil {
		return nil, fmt.Errorf("读账号表 %s 失败: %w", m.cfg.AccountsFile, err)
	}
	var af accountsFile
	if err := json.Unmarshal(b, &af); err != nil {
		return nil, fmt.Errorf("解析账号表失败: %w", err)
	}
	online := m.adbDevices(ctx)
	out := make([]model.Account, 0, len(af.Accounts))
	for _, a := range af.Accounts {
		adb := m.resolveAdb(ctx, a.Emu, a.Port, online)
		out = append(out, model.Account{
			ID:      a.ID,
			Name:    a.Name,
			Client:  a.Client,
			Emu:     a.Emu,
			Daily:   a.Daily,
			Enabled: a.Enabled,
			Adb:     adb,
			Online:  adb != "" && online[adb],
		})
	}
	sort.SliceStable(out, func(i, j int) bool {
		if out[i].Enabled != out[j].Enabled {
			return out[i].Enabled
		}
		return naturalLess(out[i].ID, out[j].ID)
	})
	return out, nil
}

func naturalLess(a, b string) bool {
	if len(a) != len(b) {
		return len(a) < len(b)
	}
	return a < b
}

// resolveAdb 解析账号对应的 adb 地址。
//
// 优先级：accounts.json 的 port → vm_config 的端口映射 → MuMu 端口公式，
// **并且只取 adb devices 里真正在线的那一个**；静态候选都不在线时，再问
// MuMuManager 要实时 adb_port——MuMu 重启后端口会漂（实测 16384 → 16385），
// 只信配置文件会连不上（2026-11-22 踩到）。
func (m *Manager) resolveAdb(ctx context.Context, emu, portOverride string, online map[string]bool) string {
	cands := m.staticAdbCandidates(emu, portOverride)
	for _, c := range cands {
		if online[c] {
			return c
		}
	}
	idx, err := strconv.Atoi(strings.TrimSpace(emu))
	if err != nil {
		return cands[0]
	}
	if live := m.mumuLivePort(ctx, idx); live != "" {
		return live
	}
	return cands[0]
}

// staticAdbCandidates 返回端口候选（accounts.json → vm_config → 端口公式），已去重。
func (m *Manager) staticAdbCandidates(emu, portOverride string) []string {
	var out []string
	add := func(p string) {
		if p == "" {
			return
		}
		addr := "127.0.0.1:" + strings.TrimPrefix(p, "127.0.0.1:")
		for _, x := range out {
			if x == addr {
				return
			}
		}
		out = append(out, addr)
	}
	add(portOverride)
	idx, err := strconv.Atoi(strings.TrimSpace(emu))
	if err != nil {
		if len(out) == 0 {
			return []string{""}
		}
		return out
	}
	if m.cfg.MumuRoot != "" {
		cfgPath := filepath.Join(m.cfg.MumuRoot, "vms",
			fmt.Sprintf("MuMuPlayer-12.0-%d", idx), "configs", "vm_config.json")
		if b, err := os.ReadFile(cfgPath); err == nil {
			var v struct {
				VM struct {
					Nat struct {
						PortForward struct {
							Adb struct {
								HostPort json.Number `json:"host_port"`
							} `json:"adb"`
						} `json:"port_forward"`
					} `json:"nat"`
				} `json:"vm"`
			}
			if json.Unmarshal(b, &v) == nil {
				if p := v.VM.Nat.PortForward.Adb.HostPort.String(); p != "" && p != "0" {
					add(p)
				}
			}
		}
	}
	// MuMu 12 端口分段：1..31 → 16384+32*idx；32.. → 16388+32*(idx-32)。
	var p int
	if idx <= 31 {
		p = 16384 + 32*idx
	} else {
		p = 16388 + 32*(idx-32)
	}
	add(strconv.Itoa(p))
	return out
}

type livePortEntry struct {
	port string
	at   time.Time
}

var (
	livePortMu    sync.Mutex
	livePortCache = map[int]livePortEntry{}
)

// mumuLivePort 问 MuMuManager 当前实例的实时 adb_port（带 20s 缓存，避免每次列账号都起进程）。
func (m *Manager) mumuLivePort(ctx context.Context, idx int) string {
	livePortMu.Lock()
	if e, ok := livePortCache[idx]; ok && time.Since(e.at) < 20*time.Second {
		livePortMu.Unlock()
		return e.port
	}
	livePortMu.Unlock()

	exe := filepath.Join(m.cfg.MumuRoot, "nx_main", "MuMuManager.exe")
	if _, err := os.Stat(exe); err != nil {
		return ""
	}
	cctx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	cmd := exec.CommandContext(cctx, exe, "info", "-v", strconv.Itoa(idx))
	cmd.SysProcAttr = hideWindow()
	b, err := cmd.Output()
	port := ""
	if err == nil {
		var v struct {
			AdbPort int `json:"adb_port"`
		}
		// 输出形如 {"0": {...}}；拿不到就用正则兜底
		var all map[string]struct {
			AdbPort int `json:"adb_port"`
		}
		if json.Unmarshal(b, &all) == nil {
			for _, item := range all {
				if item.AdbPort > 0 {
					v = item
					break
				}
			}
		}
		if v.AdbPort > 0 {
			port = "127.0.0.1:" + strconv.Itoa(v.AdbPort)
		}
	}
	livePortMu.Lock()
	livePortCache[idx] = livePortEntry{port: port, at: time.Now()}
	livePortMu.Unlock()
	return port
}

// adbDevices 跑一次 adb devices，返回在线 serial 集合。
func (m *Manager) adbDevices(ctx context.Context) map[string]bool {
	out := map[string]bool{}
	if m.cfg.AdbPath == "" {
		return out
	}
	cctx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	cmd := exec.CommandContext(cctx, m.cfg.AdbPath, "devices")
	cmd.SysProcAttr = hideWindow()
	b, err := cmd.Output()
	if err != nil {
		return out
	}
	for _, line := range strings.Split(string(b), "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "List of") || strings.HasPrefix(line, "*") {
			continue
		}
		fields := strings.Fields(line)
		if len(fields) >= 2 && fields[1] == "device" {
			out[fields[0]] = true
		}
	}
	return out
}

var shotMu sync.Mutex

// Screenshot 用 adb 截一张图写到 dst（串行，避免多进程 screencap 抢设备）。
func (m *Manager) Screenshot(ctx context.Context, adb, dst string) error {
	if m.cfg.AdbPath == "" || adb == "" {
		return fmt.Errorf("缺少 adb 路径或设备地址")
	}
	shotMu.Lock()
	defer shotMu.Unlock()
	if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
		return err
	}
	f, err := os.Create(dst)
	if err != nil {
		return err
	}
	defer f.Close()
	cctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	cmd := exec.CommandContext(cctx, m.cfg.AdbPath, "-s", adb, "exec-out", "screencap", "-p")
	cmd.SysProcAttr = hideWindow()
	cmd.Stdout = f
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("adb 截图失败: %w", err)
	}
	if st, err := f.Stat(); err != nil || st.Size() < 1024 {
		return fmt.Errorf("adb 截图异常（文件过小）")
	}
	return nil
}
