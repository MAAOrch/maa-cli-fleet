package copilotpick

// 「编不进队」干员黑名单：MAA 编队报 `"reason":"Unavailable"` 时记下来，
// 之后作业要这些 (干员, 技能序号) 就直接跳过，不用再花 40 秒撞一次编队失败。
//
// 为什么只记 Unavailable 不记 Missing：Missing 是「名册里没有」，属于名册/账号错配；
// Unavailable 是「有但用不了」（练度/专精/模组不够，或者卡在训练室），这才是可学习的
// 账号事实。模组要求现在会被 NormalizeJobContent 去掉，所以能学到的主要是专精/练度。
//
// 落盘 `<root>/unavailable.json`，重启保留；跨服务重启也能继续受益。

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"
)

// unavailablePairRe 从 MAA 编队报错里抓「干员名 + Unavailable」。
var unavailablePairRe = regexp.MustCompile(`\{"name":"([^"]+)","reason":"Unavailable"\}`)

// blockedKey 同一干员不同技能序号算不同条目（作业要的专精不一样）。
func blockedKey(name string, skill int) string { return fmt.Sprintf("%s/s%d", name, skill) }

// blockedPath 黑名单落盘路径。
func (m *Manager) blockedPath() string { return filepath.Join(m.cfg.Root, "unavailable.json") }

// loadBlockedFile 启动时恢复黑名单（文件不存在/坏了就当空）。
func (m *Manager) loadBlockedFile() {
	m.blockMu.Lock()
	defer m.blockMu.Unlock()
	if m.blocked == nil {
		m.blocked = map[string]BlockedOp{}
	}
	b, err := os.ReadFile(m.blockedPath())
	if err != nil || len(b) == 0 {
		return
	}
	var list []BlockedOp
	if err := json.Unmarshal(b, &list); err != nil {
		return
	}
	for _, op := range list {
		if strings.TrimSpace(op.Name) == "" {
			continue
		}
		m.blocked[blockedKey(op.Name, op.Skill)] = op
	}
}

// saveBlockedFileLocked 原子写黑名单（调用方已持锁）。
func (m *Manager) saveBlockedFileLocked() {
	list := make([]BlockedOp, 0, len(m.blocked))
	for _, op := range m.blocked {
		list = append(list, op)
	}
	b, err := json.MarshalIndent(list, "", "  ")
	if err != nil {
		return
	}
	path := m.blockedPath()
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, b, 0o644); err != nil {
		return
	}
	_ = os.Rename(tmp, path)
}

// BlockedOps 返回黑名单快照（按名字排序，给 UI / 驱动脚本看）。
func (m *Manager) BlockedOps() []BlockedOp {
	m.blockMu.RLock()
	defer m.blockMu.RUnlock()
	out := make([]BlockedOp, 0, len(m.blocked))
	for _, op := range m.blocked {
		out = append(out, op)
	}
	return out
}

// IsBlocked 判某干员的某个技能序号是否已被记为编不进队。
func (m *Manager) IsBlocked(name string, skill int) bool {
	m.blockMu.RLock()
	defer m.blockMu.RUnlock()
	_, ok := m.blocked[blockedKey(name, skill)]
	return ok
}

// AddBlocked 记一条（已存在则跳过），返回是否有新增。
func (m *Manager) AddBlocked(name string, skill int) bool {
	name = strings.TrimSpace(name)
	if name == "" {
		return false
	}
	key := blockedKey(name, skill)
	m.blockMu.Lock()
	defer m.blockMu.Unlock()
	if m.blocked == nil {
		m.blocked = map[string]BlockedOp{}
	}
	if _, ok := m.blocked[key]; ok {
		return false
	}
	m.blocked[key] = BlockedOp{Name: name, Skill: skill, At: time.Now().Format(time.RFC3339)}
	m.saveBlockedFileLocked()
	return true
}

// unavailableNames 从 MAA 日志里抓出所有 Unavailable 的干员名（去重、保序）。
func unavailableNames(logText string) []string {
	seen := map[string]bool{}
	var out []string
	for _, m := range unavailablePairRe.FindAllStringSubmatch(logText, -1) {
		name := strings.TrimSpace(m[1])
		if name == "" || seen[name] {
			continue
		}
		seen[name] = true
		out = append(out, name)
	}
	return out
}

// jobOperRef 作业里的一个干员条目。
type jobOperRef struct {
	Name  string `json:"name"`
	Skill int    `json:"skill"`
}

// jobOperRefs 读作业文件里的 opers（本地作业文件是 Normalize 后的标准 copilot JSON）。
func jobOperRefs(jobFile string) []jobOperRef {
	b, err := os.ReadFile(jobFile)
	if err != nil {
		return nil
	}
	var job struct {
		Opers []jobOperRef `json:"opers"`
	}
	if err := json.Unmarshal(b, &job); err != nil {
		return nil
	}
	return job.Opers
}

// learnUnavailable 把 MAA 报 Unavailable 的干员记进黑名单，技能序号取这份作业里的用法。
func (r *Run) learnUnavailable(jobFile, logText string) {
	names := unavailableNames(logText)
	if len(names) == 0 {
		return
	}
	skills := map[string]int{}
	for _, op := range jobOperRefs(jobFile) {
		skills[op.Name] = op.Skill
	}
	var added []string
	for _, name := range names {
		skill, ok := skills[name]
		if !ok {
			continue // 作业里没写这个干员（例如组占位），不猜技能序号
		}
		if r.m.AddBlocked(name, skill) {
			added = append(added, fmt.Sprintf("%s/s%d", name, skill))
		}
	}
	if len(added) > 0 {
		r.event("warn", "attempt", "记下编不进队的干员（以后自动跳过）："+strings.Join(added, "、"))
	}
}

// jobBlocked 判这份作业是否要了黑名单里的 (干员, 技能序号)。
func (r *Run) jobBlocked(jobFile string) (string, bool) {
	for _, op := range jobOperRefs(jobFile) {
		if strings.TrimSpace(op.Name) == "" {
			continue
		}
		if r.m.IsBlocked(op.Name, op.Skill) {
			return fmt.Sprintf("%s/s%d", op.Name, op.Skill), true
		}
	}
	return "", false
}

// clearBlocked 清空黑名单并落盘（换号或补完练度后用）。
func (m *Manager) clearBlocked() {
	m.blockMu.Lock()
	defer m.blockMu.Unlock()
	m.blocked = map[string]BlockedOp{}
	m.saveBlockedFileLocked()
}
