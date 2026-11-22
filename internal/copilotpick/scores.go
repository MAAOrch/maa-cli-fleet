package copilotpick

// 跨号共享作业评分库（job_scores.json）：
//
// 记录每份作业在真实推关里的表现（3★ 通关 / 低星 / 作战失败 / 编队不可用 / 其它失败），
// 并在 SearchJobs 时把实测通过的作业顶到前面、把反复翻车的雷作业压到后面。
//
// 跨实例共享机制：多个 copilot-picker 实例（如 main:8790 与 slot2:8791）把
// jobScoresFile（或环境变量 COPILOT_JOB_SCORES_FILE）指向同一个 JSON 文件即可。
// 读写前都会按 mtime 自动重读磁盘并合并更新、原子重命名落盘，互相即时可见。

import (
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/xiachk083-hub/maa-cli-fleet/internal/copilotpick/model"
)

// JobOutcome 是一次作业尝试的归因分类。
type JobOutcome string

const (
	OutcomePass3         JobOutcome = "pass3"
	OutcomePassLow       JobOutcome = "pass_low"
	OutcomeFailBattle    JobOutcome = "fail_battle"
	OutcomeFailFormation JobOutcome = "fail_formation"
	OutcomeFailOther     JobOutcome = "fail_other"
)

// JobScore 是一份作业的跨号实测统计。
type JobScore struct {
	JobID         int     `json:"jobId"`
	StageCode     string  `json:"stageCode,omitempty"`
	Mode          string  `json:"mode,omitempty"` // normal | raid
	Pass3         int     `json:"pass3"`
	PassLow       int     `json:"passLow"`
	FailBattle    int     `json:"failBattle"`
	FailFormation int     `json:"failFormation"`
	FailOther     int     `json:"failOther"`
	Score         float64 `json:"score"`
	LastAccount   string  `json:"lastAccount,omitempty"`
	LastReason    string  `json:"lastReason,omitempty"`
	UpdatedAt     string  `json:"updatedAt"`
}

type jobScoreStore struct {
	mu      sync.RWMutex
	items   map[int]JobScore
	modTime time.Time
}

// computeJobScore 根据实测统计计算综合分：
//   - 3★/突袭通关：+3.0（强正向，优先复用）
//   - 低星（1★/2★）：-1.0（普通关不达标）
//   - 作战失败（0★/任务失败）：-2.5（强避雷）
//   - 编队不可用（BattleFormationOperUnavailable）：-0.5（多半与账号练度有关，轻惩罚）
//   - 其它失败（超时/未知）：-1.0
func computeJobScore(s JobScore) float64 {
	return float64(s.Pass3)*3.0 -
		float64(s.PassLow)*1.0 -
		float64(s.FailBattle)*2.5 -
		float64(s.FailFormation)*0.5 -
		float64(s.FailOther)*1.0
}

func (m *Manager) loadJobScoresFile() {
	m.scores.mu.Lock()
	defer m.scores.mu.Unlock()
	m.reloadJobScoresLocked()
}

// reloadJobScoresLocked 若磁盘上的 job_scores.json 更新过则重读（调用方已持写锁）。
func (m *Manager) reloadJobScoresLocked() {
	if m.scores.items == nil {
		m.scores.items = map[int]JobScore{}
	}
	path := m.cfg.JobScoresPath()
	st, err := os.Stat(path)
	if err != nil {
		return
	}
	if !m.scores.modTime.IsZero() && !st.ModTime().After(m.scores.modTime) {
		return
	}
	b, err := os.ReadFile(path)
	if err != nil || len(b) == 0 {
		return
	}
	var list []JobScore
	if err := json.Unmarshal(b, &list); err != nil {
		return
	}
	fresh := make(map[int]JobScore, len(list))
	for _, it := range list {
		if it.JobID <= 0 {
			continue
		}
		it.Score = computeJobScore(it)
		fresh[it.JobID] = it
	}
	m.scores.items = fresh
	m.scores.modTime = st.ModTime()
}

// saveJobScoresLocked 原子写盘（调用方已持写锁）。
func (m *Manager) saveJobScoresLocked() {
	list := make([]JobScore, 0, len(m.scores.items))
	for _, it := range m.scores.items {
		list = append(list, it)
	}
	sort.Slice(list, func(i, j int) bool {
		if list[i].Score != list[j].Score {
			return list[i].Score > list[j].Score
		}
		return list[i].JobID < list[j].JobID
	})
	b, err := json.MarshalIndent(list, "", "  ")
	if err != nil {
		return
	}
	path := m.cfg.JobScoresPath()
	if dir := filepath.Dir(path); dir != "" {
		_ = os.MkdirAll(dir, 0o755)
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, b, 0o644); err != nil {
		return
	}
	if err := os.Rename(tmp, path); err == nil {
		if st, err2 := os.Stat(path); err2 == nil {
			m.scores.modTime = st.ModTime()
		}
	}
}

// RecordJobOutcome 记录一次作业实测结果并原子落盘（先按 mtime 合并外部实例写入）。
func (m *Manager) RecordJobOutcome(jobID int, stageCode, mode, accountID string, outcome JobOutcome, reason string) {
	if jobID <= 0 {
		return
	}
	m.scores.mu.Lock()
	defer m.scores.mu.Unlock()
	m.reloadJobScoresLocked()

	cur := m.scores.items[jobID]
	cur.JobID = jobID
	if stageCode != "" {
		cur.StageCode = stageCode
	}
	if mode != "" {
		cur.Mode = mode
	}
	switch outcome {
	case OutcomePass3:
		cur.Pass3++
	case OutcomePassLow:
		cur.PassLow++
	case OutcomeFailBattle:
		cur.FailBattle++
	case OutcomeFailFormation:
		cur.FailFormation++
	default:
		cur.FailOther++
	}
	cur.Score = computeJobScore(cur)
	if accountID != "" {
		cur.LastAccount = accountID
	}
	reason = strings.TrimSpace(reason)
	if len(reason) > 200 {
		reason = reason[:200]
	}
	cur.LastReason = reason
	cur.UpdatedAt = time.Now().Format(time.RFC3339)
	m.scores.items[jobID] = cur
	m.saveJobScoresLocked()
}

// JobScores 返回当前共享评分库快照（按分数降序）。
func (m *Manager) JobScores() []JobScore {
	m.scores.mu.Lock()
	m.reloadJobScoresLocked()
	out := make([]JobScore, 0, len(m.scores.items))
	for _, it := range m.scores.items {
		out = append(out, it)
	}
	m.scores.mu.Unlock()
	sort.Slice(out, func(i, j int) bool {
		if out[i].Score != out[j].Score {
			return out[i].Score > out[j].Score
		}
		return out[i].JobID < out[j].JobID
	})
	return out
}

// applyJobScores 给搜索结果挂上跨号实测评分，并在同一分档内把「实测高分」顶前、「雷作业」沉底。
func (m *Manager) applyJobScores(jobs []model.Job) {
	if len(jobs) == 0 {
		return
	}
	hasScore := false
	m.scores.mu.Lock()
	m.reloadJobScoresLocked()
	for i := range jobs {
		if sc, ok := m.scores.items[jobs[i].ID]; ok {
			jobs[i].LocalScore = sc.Score
			jobs[i].LocalPass = sc.Pass3
			jobs[i].LocalFail = sc.PassLow + sc.FailBattle + sc.FailFormation + sc.FailOther
			if sc.Score != 0 {
				hasScore = true
			}
		}
	}
	m.scores.mu.Unlock()
	if !hasScore {
		return
	}

	tier := func(score float64) int {
		if score > 0 {
			return 0 // 实测通过优先
		}
		if score < 0 {
			return 2 // 实测翻车沉底避雷
		}
		return 1 // 未测过保持原序
	}
	band := func(j model.Job) int {
		switch j.Mode {
		case "ready":
			if j.Rating >= 9 {
				return 0
			}
			return 1
		case "borrow":
			return 1
		case "train":
			return 2
		default:
			return 3
		}
	}
	sort.SliceStable(jobs, func(i, j int) bool {
		bi, bj := band(jobs[i]), band(jobs[j])
		if bi != bj {
			return bi < bj
		}
		ti, tj := tier(jobs[i].LocalScore), tier(jobs[j].LocalScore)
		if ti != tj {
			return ti < tj
		}
		if jobs[i].LocalScore != jobs[j].LocalScore {
			return jobs[i].LocalScore > jobs[j].LocalScore
		}
		return false
	})
}
