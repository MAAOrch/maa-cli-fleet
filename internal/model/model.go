// Package model —— 中心/机端/调度器共用的数据结构（JSON 形状与旧 PowerShell 端保持一致）。
package model

import (
	"bytes"
	"encoding/json"
	"os"
	"strings"
)

// ReadFileBOM 读文件并剥掉 UTF-8 BOM（PowerShell 的 Set-Content -Encoding UTF8 会带 BOM，
// 而 json.Unmarshal 不吃 BOM —— 这里是所有"读配置/状态"的唯一入口）。
func ReadFileBOM(path string) ([]byte, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	return bytes.TrimPrefix(b, []byte{0xEF, 0xBB, 0xBF}), nil
}

// Heartbeat 心跳载荷。
type Heartbeat struct {
	TS   string          `json:"ts"`
	Data json.RawMessage `json:"data,omitempty"`
}

// Node 机端台账条目。
type Node struct {
	ID        string          `json:"id"`
	Ver       string          `json:"ver,omitempty"`
	Host      string          `json:"host,omitempty"`
	Machines  json.RawMessage `json:"machines,omitempty"`
	State     map[string]any  `json:"state,omitempty"`
	Heartbeat *Heartbeat      `json:"heartbeat,omitempty"`
	LastSeen  string          `json:"last_seen,omitempty"`
	LastPoll  string          `json:"last_poll,omitempty"`
	Online    bool            `json:"online"`
}

// MachineState 一台机的状态（机端上报）。
type MachineState struct {
	Maa       string  `json:"maa"`
	LogAgeMin float64 `json:"logAgeMin"`
	Tunnel    string  `json:"tunnel"`
	Game      string  `json:"game"`
	Health    string  `json:"health"`
	SampledAt string  `json:"sampledAt"`
}

// Account 轮转器里的一个账号（runner/accounts.json）。
type Account struct {
	ID       string `json:"id"`
	Name     string `json:"name"`
	Client   string `json:"client"`
	Stage    string `json:"stage"`
	Mode     string `json:"mode,omitempty"`
	Fallback string `json:"fallback"`
	Ann      string `json:"ann,omitempty"`
	Emu      string `json:"emu"`
	Port     string `json:"port,omitempty"`
	Daily    string `json:"daily"`
	AnnTask  string `json:"annTask,omitempty"`
	State    string `json:"state"`
	Enabled  bool   `json:"enabled"`

	// 常驻任务（与日常/剿灭同一套任务模型，只是策略不同）：
	// ResidentTask 非空 → 这台机跑常驻任务（跑完/挂了立刻重来，实例不关机），
	// 例如肉鸽 rogue_*、卫戍协议 spa_*；RogueTask 是旧字段，继续兼容。
	// Resident 标记常驻机（不参与“跑完停机”的轮转）。
	ResidentTask string `json:"residentTask,omitempty"`
	RogueTask    string `json:"rogueTask,omitempty"`
	Resident     bool   `json:"resident,omitempty"`
	StateDir     string `json:"stateDir,omitempty"` // MAA_STATE_DIR 覆盖（绝对路径；默认 stateRoot/state_<id>）

	// ResidentPreCommand 每轮常驻任务发车前执行的外部命令，{addr} 会替换成 127.0.0.1:<端口>。
	// 用途：生息演算 mode 0 之前删档（MAA 遇到存档只会停下，不会删）。
	// 命令在 force-stop 游戏之前执行，此时游戏仍停在上一轮结束的画面。
	ResidentPreCommand string `json:"residentPreCommand,omitempty"`

	// 24h 轮换：ResidentTasks 非空时按顺序轮换这些任务，每个任务跑 ResidentRotateMinutes 分钟；
	// 0 表示不按时间轮换（只在当前任务自然退出后切下一个）。
	ResidentTasks         []string `json:"residentTasks,omitempty"`
	ResidentRotateMinutes int      `json:"residentRotateMinutes,omitempty"`
}

// RotationTasks 返回轮换任务列表（去空）；没有轮换配置时退化为单任务。
func (a Account) RotationTasks() []string {
	out := make([]string, 0, len(a.ResidentTasks)+1)
	for _, t := range a.ResidentTasks {
		if s := strings.TrimSpace(t); s != "" {
			out = append(out, s)
		}
	}
	if len(out) == 0 {
		if s := a.ResidentTaskFile(); s != "" {
			out = append(out, s)
		}
	}
	return out
}

// ResidentTaskFile 返回常驻任务文件名：新字段优先，旧的 rogueTask 兜底。
func (a Account) ResidentTaskFile() string {
	if s := strings.TrimSpace(a.ResidentTask); s != "" {
		return s
	}
	return strings.TrimSpace(a.RogueTask)
}

// AccountsFile 是 accounts.json 的顶层。
type AccountsFile struct {
	GeneratedAt string    `json:"generatedAt"`
	Slots       int       `json:"slots"`
	Accounts    []Account `json:"accounts"`
}
