package copilotpick

// 关卡完成度台账：哪一关、哪个难度、几星，何时通的。
//
// 为什么需要：突袭要靠**普通 3 星**才解锁（2026-11-22 实测 TL-EX-5：2 星通关只开下一关，
// 突袭仍带锁，游戏提示「获得三星评价解锁突袭作战」）。所以「通关」不等于完成，必须记住星数，
// 否则会反复重打 2 星关、也说不清哪些关还差三星。
//
// 落盘 `<root>/progress.json`，重启保留；key 是 `关卡代号|难度`。

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"sync"
	"time"
)

// StageProgress 一条完成记录。
type StageProgress struct {
	StageCode string `json:"stageCode"`
	Mode      string `json:"mode"`  // normal | raid
	Stars     int    `json:"stars"` // 0..3；突袭不数星（记 3 = 已通关）
	ClearedAt string `json:"clearedAt"`
}

type progressStore struct {
	mu    sync.RWMutex
	items map[string]StageProgress
}

// progressKey 同一关不同难度分开记。
func progressKey(code, mode string) string { return code + "|" + mode }

// progressPath 台账落盘路径。
func (m *Manager) progressPath() string { return filepath.Join(m.cfg.Root, "progress.json") }

// loadProgressFile 启动时恢复台账。
func (m *Manager) loadProgressFile() {
	if m.progress.items == nil {
		m.progress.items = map[string]StageProgress{}
	}
	b, err := os.ReadFile(m.progressPath())
	if err != nil || len(b) == 0 {
		return
	}
	var list []StageProgress
	if err := json.Unmarshal(b, &list); err != nil {
		return
	}
	m.progress.mu.Lock()
	defer m.progress.mu.Unlock()
	for _, it := range list {
		if it.StageCode == "" || it.Mode == "" {
			continue
		}
		m.progress.items[progressKey(it.StageCode, it.Mode)] = it
	}
}

// saveProgressFileLocked 原子写台账（调用方已持锁）。
func (m *Manager) saveProgressFileLocked() {
	list := make([]StageProgress, 0, len(m.progress.items))
	for _, it := range m.progress.items {
		list = append(list, it)
	}
	sort.Slice(list, func(i, j int) bool {
		if list[i].StageCode != list[j].StageCode {
			return list[i].StageCode < list[j].StageCode
		}
		return list[i].Mode < list[j].Mode
	})
	b, err := json.MarshalIndent(list, "", "  ")
	if err != nil {
		return
	}
	path := m.progressPath()
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, b, 0o644); err != nil {
		return
	}
	_ = os.Rename(tmp, path)
}

// RecordProgress 记一条完成（星数取历史最大值；突袭只记「已通关」）。
func (m *Manager) RecordProgress(code, mode string, stars int) {
	if code == "" || mode == "" {
		return
	}
	if stars > 3 {
		stars = 3
	}
	m.progress.mu.Lock()
	defer m.progress.mu.Unlock()
	if m.progress.items == nil {
		m.progress.items = map[string]StageProgress{}
	}
	key := progressKey(code, mode)
	old, ok := m.progress.items[key]
	if ok && old.Stars >= stars && stars >= 0 {
		// 星数没提升就不动（保留最早的 ClearedAt）
		return
	}
	m.progress.items[key] = StageProgress{
		StageCode: code,
		Mode:      mode,
		Stars:     stars,
		ClearedAt: time.Now().Format(time.RFC3339),
	}
	m.saveProgressFileLocked()
}

// Progress 返回台账快照（按关卡代号排序）。
func (m *Manager) Progress() []StageProgress {
	m.progress.mu.RLock()
	defer m.progress.mu.RUnlock()
	out := make([]StageProgress, 0, len(m.progress.items))
	for _, it := range m.progress.items {
		out = append(out, it)
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].StageCode != out[j].StageCode {
			return out[i].StageCode < out[j].StageCode
		}
		return out[i].Mode < out[j].Mode
	})
	return out
}

// StageStars 取某关某难度已记录的星数（-1 = 没记录过）。
func (m *Manager) StageStars(code, mode string) int {
	m.progress.mu.RLock()
	defer m.progress.mu.RUnlock()
	it, ok := m.progress.items[progressKey(code, mode)]
	if !ok {
		return -1
	}
	return it.Stars
}

// progressLabel 给事件日志用的中文标签。
func progressLabel(mode string, stars int) string {
	if mode == "raid" {
		return "突袭已通关"
	}
	if stars < 0 {
		return "未记录"
	}
	return fmt.Sprintf("%d 星", stars)
}
