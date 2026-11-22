package copilotpick

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/xiachk083-hub/maa-cli-fleet/internal/copilotpick/ext"
	"github.com/xiachk083-hub/maa-cli-fleet/internal/copilotpick/model"
)

// Manager 持有配置、账号/关卡缓存与正在执行的 run（同一时刻只允许一个）。
type Manager struct {
	cfg      Config
	prts     *ext.PrtsClient
	localMu  sync.Mutex
	local    []model.Stage
	rosterMu sync.RWMutex
	roster   model.Roster
	runMu    sync.Mutex
	runs     map[string]*Run
	activeID string

	// blocked 记录「有名册但编不进队」的 (干员, 技能序号)：来自 MAA 编队报错的
	// `"reason":"Unavailable"`。作业要这些干员时直接跳过，省掉一次 40 秒空跑。
	// 落盘 <root>/unavailable.json，重启保留。
	blockMu sync.RWMutex
	blocked map[string]BlockedOp

	// progress 关卡完成度台账（哪关几星），落盘 <root>/progress.json。
	progress progressStore

	// scores 跨号共享作业评分库，落盘 job_scores.json。
	scores jobScoreStore
}

// BlockedOp 一个「编不进队」的干员条目。
type BlockedOp struct {
	Name  string `json:"name"`
	Skill int    `json:"skill"`
	At    string `json:"at,omitempty"` // 首次记录时间（RFC3339）
}

// NewManager 创建 Manager 并加载本地关卡表（失败不致命，解析时再兜底）。
func NewManager(cfg Config) *Manager {
	m := &Manager{
		cfg:  cfg,
		prts: ext.NewPrtsClient(cfg.PrtsBase),
		runs: map[string]*Run{},
	}
	_ = m.ReloadLocalStages()
	m.loadRosterFile()
	m.loadBlockedFile()
	m.loadProgressFile()
	m.loadJobScoresFile()
	return m
}

// rosterPath 名册落盘位置（服务重启后自动恢复）。
func (m *Manager) rosterPath() string { return filepath.Join(m.cfg.Root, "roster.json") }

// loadRosterFile 启动时恢复上次的名册；文件没有或坏了就当没名册。
func (m *Manager) loadRosterFile() {
	b, err := os.ReadFile(m.rosterPath())
	if err != nil || len(b) == 0 {
		return
	}
	var r model.Roster
	if err := json.Unmarshal(b, &r); err != nil || len(r.Entries) == 0 {
		return
	}
	if r.Count == 0 {
		r.Count = len(r.Entries)
	}
	m.rosterMu.Lock()
	m.roster = r
	m.rosterMu.Unlock()
}

// saveRosterFile 原子写名册（失败只影响下次恢复，不影响本轮）。
func (m *Manager) saveRosterFile(r model.Roster) {
	if len(r.Entries) == 0 {
		return
	}
	b, err := json.MarshalIndent(r, "", "  ")
	if err != nil {
		return
	}
	path := m.rosterPath()
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, b, 0o644); err != nil {
		return
	}
	_ = os.Rename(tmp, path)
}

// ReloadLocalStages 从 MAA 资源读取 stages.json（code→stageId）。
func (m *Manager) ReloadLocalStages() error {
	stages, err := ext.LoadLocalStages(m.cfg.MaaResourceDir)
	if err != nil {
		return err
	}
	m.localMu.Lock()
	m.local = stages
	m.localMu.Unlock()
	return nil
}

// LocalStages 返回本地关卡表副本。
func (m *Manager) LocalStages() []model.Stage {
	m.localMu.Lock()
	defer m.localMu.Unlock()
	out := make([]model.Stage, len(m.local))
	copy(out, m.local)
	return out
}

// Prts 返回作业站客户端。
func (m *Manager) Prts() *ext.PrtsClient { return m.prts }

// SetRoster 记住最近一次解析出的名册。
//
// 名册是「账号级」状态：前端在第 1 步粘贴/导入，搜作业与执行作业都用同一份，
// 所以放在 Manager 上而不是每次请求都传（HTTP 层无状态，但执行器需要它做作业修正）。
// 同时落盘（<root>/roster.json），服务重启后自动恢复——否则每次重编重启都要重新
// 跑 1~2 分钟干员识别。
func (m *Manager) SetRoster(r model.Roster) {
	m.rosterMu.Lock()
	m.roster = r
	m.rosterMu.Unlock()
	m.saveRosterFile(r)
}

// Roster 返回最近一次解析出的名册。
func (m *Manager) Roster() model.Roster {
	m.rosterMu.RLock()
	defer m.rosterMu.RUnlock()
	return m.roster
}

// event 让 Manager 层也能往 run 的事件流里写（finish 等处用）。
func (m *Manager) event(r *Run, level, kind, msg string) {
	if r == nil {
		return
	}
	r.event(level, kind, msg)
}

// Config 返回配置。
func (m *Manager) Config() Config { return m.cfg }

// Health 返回健康信息。
func (m *Manager) Health() model.Health {
	ready, _ := m.cfg.MaaReady()
	n := 0
	if b, err := os.ReadFile(m.cfg.AccountsFile); err == nil {
		var af accountsFile
		if json.Unmarshal(b, &af) == nil {
			n = len(af.Accounts)
		}
	}
	return model.Health{
		OK:       true,
		Version:  Version,
		Root:     m.cfg.Root,
		MaaReady: ready,
		MaaExe:   m.cfg.MaaExe,
		Accounts: n,
		Listen:   m.cfg.Listen,
		PrtsBase: m.cfg.PrtsBase,
	}
}

// Version 由 main 注入。
var Version = "dev"

// Setup 初始化隔离 MAA 运行目录：lib/resource 用 junction 指向 MAA GUI 目录，建 cache/state/config/profiles。
// 已存在则跳过。返回是否就绪。
func (m *Manager) Setup(ctx context.Context) (bool, error) {
	if m.cfg.MaaExe == "" {
		return false, errors.New("找不到 maa-cli 可执行文件，请检查配置 maaExe")
	}
	if m.cfg.MaaCoreSource == "" || m.cfg.MaaResourceSource == "" {
		return false, errors.New("请在配置里指定 maaCoreSource（MAA GUI 目录）和 maaResourceSource（其中的 resource 目录）")
	}
	for _, d := range []string{
		filepath.Join(m.cfg.MaaDataDir),
		filepath.Join(m.cfg.MaaCacheDir),
		filepath.Join(m.cfg.MaaStateDir),
		filepath.Join(m.cfg.MaaConfigDir, "profiles"),
		filepath.Join(m.cfg.RunsDir),
	} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			return false, err
		}
	}
	lib := filepath.Join(m.cfg.MaaDataDir, "lib")
	res := filepath.Join(m.cfg.MaaDataDir, "resource")
	if err := m.ensureJunction(lib, m.cfg.MaaCoreSource); err != nil {
		return false, err
	}
	if err := m.ensureJunction(res, m.cfg.MaaResourceSource); err != nil {
		return false, err
	}
	_ = m.ReloadLocalStages()
	ready, reason := m.cfg.MaaReady()
	if !ready {
		return false, errors.New(reason)
	}
	return true, nil
}

func (m *Manager) ensureJunction(link, target string) error {
	if st, err := os.Stat(link); err == nil && st.IsDir() {
		return nil
	}
	if _, err := os.Stat(target); err != nil {
		return fmt.Errorf("junction 源不存在: %s", target)
	}
	cmd := exec.Command("cmd", "/c", "mklink", "/J", link, target)
	cmd.SysProcAttr = hideWindow()
	if out, err := cmd.CombinedOutput(); err != nil {
		return fmt.Errorf("创建 junction %s -> %s 失败: %v %s", link, target, err, string(out))
	}
	return nil
}

// ---- Run 管理 ----

// ErrBusy 表示已有执行中的 run。
var ErrBusy = errors.New("已有执行中的任务，请先停止或等待完成")

// StartRun 校验参数、建 run 目录、异步启动执行。
func (m *Manager) StartRun(opts model.RunOptions) (*Run, error) {
	if ready, reason := m.cfg.MaaReady(); !ready {
		return nil, errors.New(reason)
	}
	accounts, err := m.Accounts(context.Background())
	if err != nil {
		return nil, err
	}
	acc, ok := findAccount(accounts, opts.AccountID)
	if !ok {
		return nil, fmt.Errorf("账号 %s 不存在", opts.AccountID)
	}
	if adb := strings.TrimSpace(opts.Adb); adb != "" {
		acc.Adb = adb
	}
	if acc.Adb == "" {
		return nil, fmt.Errorf("账号 %s 解析不出 adb 地址（emu=%s）", acc.ID, acc.Emu)
	}
	if opts.Client == "" {
		opts.Client = acc.Client
	}
	if opts.Client == "" {
		opts.Client = m.cfg.DefaultClient
	}
	// 半自动多关队列：Stages 为空时用单关字段补一条；每关都要有作业站关卡代码和作业。
	if err := normalizeStagePlans(&opts); err != nil {
		return nil, err
	}
	if opts.TimeoutMin <= 0 {
		opts.TimeoutMin = m.cfg.CopilotTimeoutMin
	}

	m.runMu.Lock()
	if m.activeID != "" {
		if r := m.runs[m.activeID]; r != nil && !r.IsDone() {
			m.runMu.Unlock()
			return nil, ErrBusy
		}
	}
	id := fmt.Sprintf("%s-%s", time.Now().Format("20060102-150405"), randSuffix(4))
	dir := filepath.Join(m.cfg.RunsDir, id)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		m.runMu.Unlock()
		return nil, err
	}
	r := newRun(m, id, dir, acc, opts)
	m.runs[id] = r
	m.activeID = id
	m.runMu.Unlock()

	if b, err := json.MarshalIndent(opts, "", "  "); err == nil {
		_ = os.WriteFile(filepath.Join(dir, "options.json"), b, 0o644)
	}
	go r.run()
	return r, nil
}

func findAccount(list []model.Account, id string) (model.Account, bool) {
	for _, a := range list {
		if a.ID == id {
			return a, true
		}
	}
	return model.Account{}, false
}

// normalizeStagePlans 把单关字段补成统一的 Stages 列表，并校验半自动队列。
//
// 规则：Stages 非空就按队列跑；为空则用 StageCode/StageName/JobIDs 补一条。
// 手动模式（用户自己导航）只支持单关——多关时中间不可能人工换界面。
func normalizeStagePlans(opts *model.RunOptions) error {
	stages := opts.Stages
	if len(stages) == 0 {
		stages = []model.StagePlan{{
			StageCode: opts.StageCode,
			StageName: opts.StageName,
			JobIDs:    opts.JobIDs,
		}}
	}
	if len(stages) > 1 && opts.ManualMode {
		return errors.New("手动模式只支持单关，不能排队多关")
	}
	if len(stages) > 1 && opts.OnMap {
		return errors.New("活动地图模式只支持单关，不能排队多关")
	}
	for i := range stages {
		stages[i].StageCode = strings.TrimSpace(stages[i].StageCode)
		stages[i].StageName = strings.TrimSpace(stages[i].StageName)
		if stages[i].StageName == "" {
			return fmt.Errorf("第 %d 关缺少作业站关卡代码（stageName）", i+1)
		}
		if len(stages[i].JobIDs) == 0 {
			return fmt.Errorf("第 %d 关（%s）没有选中作业", i+1, stages[i].StageName)
		}
		// 突袭作业必须同时出现在 JobIDs 里；顺手去重。
		inJobs := make(map[int]bool, len(stages[i].JobIDs))
		for _, id := range stages[i].JobIDs {
			inJobs[id] = true
		}
		raid := make([]int, 0, len(stages[i].RaidJobIDs))
		seen := make(map[int]bool, len(stages[i].RaidJobIDs))
		for _, id := range stages[i].RaidJobIDs {
			if inJobs[id] && !seen[id] {
				raid = append(raid, id)
				seen[id] = true
			}
		}
		stages[i].RaidJobIDs = raid
	}
	opts.Stages = stages
	first := stages[0]
	opts.StageCode, opts.StageName, opts.JobIDs = first.StageCode, first.StageName, first.JobIDs
	return nil
}

// GetRun 按 id 取 run。
func (m *Manager) GetRun(id string) (*Run, bool) {
	m.runMu.Lock()
	defer m.runMu.Unlock()
	r, ok := m.runs[id]
	return r, ok
}

// CurrentRun 返回正在执行的 run。
func (m *Manager) CurrentRun() *Run {
	m.runMu.Lock()
	defer m.runMu.Unlock()
	if m.activeID == "" {
		return nil
	}
	return m.runs[m.activeID]
}

// ActiveRunSnapshot 返回还没结束的 run 快照（没有就 nil）。
// 页面刷新/重开客户端后用它把“正在跑”的 run 找回来，才能继续点停止。
func (m *Manager) ActiveRunSnapshot() *model.RunSnapshot {
	r := m.CurrentRun()
	if r == nil || r.IsDone() {
		return nil
	}
	snap := r.Snapshot(false)
	return &snap
}

// KillStrayMaa 杀掉本服务启动、但已经没人管理的 maa-cli 进程。
//
// 只匹配命令行里带 copilot-picker profile（`-p copilot-picker-*`）或作业集任务名
// （`run cp-*`）的 maa.exe / maa.real.exe，不碰 MAA 桌面端。
func (m *Manager) KillStrayMaa(ctx context.Context) ([]int, error) {
	const script = `Get-CimInstance Win32_Process -Filter "Name='maa.exe' OR Name='maa.real.exe'" | ` +
		`Where-Object { $_.CommandLine -match 'copilot-picker-' -or $_.CommandLine -match 'cp-[0-9]{8}-' } | ` +
		`Select-Object -ExpandProperty ProcessId`
	cmd := exec.CommandContext(ctx, "powershell", "-NoProfile", "-Command", script)
	cmd.SysProcAttr = hideWindow()
	out, err := cmd.Output()
	if err != nil {
		return nil, fmt.Errorf("枚举 maa 进程失败：%w", err)
	}
	var pids []int
	for _, line := range strings.Split(string(out), "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		pid, err := strconv.Atoi(line)
		if err != nil {
			continue
		}
		killTree(pid)
		pids = append(pids, pid)
	}
	return pids, nil
}

// Run 是一次执行。
type Run struct {
	m    *Manager
	ID   string
	dir  string
	adb  string
	acc  model.Account
	opts model.RunOptions

	// roster 是开始执行时冻结的名册：执行途中换账号/重新导入不影响这一轮。
	roster model.Roster

	mu     sync.Mutex
	snap   model.RunSnapshot
	events []model.RunEvent
	subs   map[chan model.RunEvent]struct{}
	done   bool

	stopOnce sync.Once
	stopCh   chan struct{}

	procMu  sync.Mutex
	proc    *os.Process
	procTag string

	// infra：运行环境（模拟器 / adb / maa 二进制）坏了的原因。一旦置上就中止整轮，
	// 免得拿一堆作业挨个撞同一个环境错误。
	infraMu sync.Mutex
	infra   string

	// navStuckFlag：作业集模式的导航看门狗掐掉了当前这份作业。
	navStuckFlag bool

	// formationTrippedFlag：编队期实时熔断看门狗掐掉了当前这份作业。
	formationTrippedFlag bool
	formationTrippedMsg  string
}

func newRun(m *Manager, id, dir string, acc model.Account, opts model.RunOptions) *Run {
	return &Run{
		m:      m,
		ID:     id,
		dir:    dir,
		adb:    acc.Adb,
		acc:    acc,
		opts:   opts,
		roster: m.Roster(),
		subs:   map[chan model.RunEvent]struct{}{},
		stopCh: make(chan struct{}),
		snap: model.RunSnapshot{
			ID:          id,
			Status:      "idle",
			AccountID:   acc.ID,
			AccountName: acc.Name,
			Client:      opts.Client,
			StageCode:   opts.StageCode,
			StageName:   opts.StageName,
			StageIndex:  1,
			StageTotal:  len(opts.Stages),
			Total:       len(opts.JobIDs),
		},
	}
}

// Dir 返回证据目录。
func (r *Run) Dir() string { return r.dir }

// Done 返回是否结束。
func (r *Run) IsDone() bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.done
}

// Stop 请求停止当前执行（杀当前 maa 子进程并退出循环）。
func (r *Run) Stop() {
	r.stopOnce.Do(func() { close(r.stopCh) })
	r.procMu.Lock()
	if r.proc != nil {
		killTree(r.proc.Pid)
	}
	r.procMu.Unlock()
}

// Snapshot 返回状态快照。
func (r *Run) Snapshot(withEvents bool) model.RunSnapshot {
	r.mu.Lock()
	defer r.mu.Unlock()
	s := r.snap
	s.HasShot = fileExists(filepath.Join(r.dir, "shot.png"))
	if withEvents {
		n := len(r.events)
		start := 0
		if n > 200 {
			start = n - 200
		}
		s.Events = append([]model.RunEvent(nil), r.events[start:]...)
	}
	return s
}

// Subscribe 返回事件通道（带历史事件回放）。
func (r *Run) Subscribe() (chan model.RunEvent, func()) {
	ch := make(chan model.RunEvent, 64)
	r.mu.Lock()
	for _, e := range r.events {
		select {
		case ch <- e:
		default:
		}
	}
	if !r.done {
		r.subs[ch] = struct{}{}
	}
	r.mu.Unlock()
	return ch, func() {
		r.mu.Lock()
		delete(r.subs, ch)
		r.mu.Unlock()
	}
}

func (r *Run) event(level, kind, msg string) {
	r.mu.Lock()
	r.events = append(r.events, model.RunEvent{
		Seq:     len(r.events) + 1,
		At:      time.Now().Format("15:04:05"),
		Level:   level,
		Kind:    kind,
		Attempt: r.snap.Attempt,
		JobID:   r.snap.CurrentJobID,
		Message: msg,
	})
	e := r.events[len(r.events)-1]
	for ch := range r.subs {
		select {
		case ch <- e:
		default:
		}
	}
	r.mu.Unlock()
	_ = os.WriteFile(filepath.Join(r.dir, "events.log"),
		[]byte(renderEvents(r.events)), 0o644)
}

func renderEvents(evs []model.RunEvent) string {
	var b strings.Builder
	for _, e := range evs {
		b.WriteString(e.At)
		b.WriteByte(' ')
		b.WriteString(strings.ToUpper(e.Level))
		b.WriteByte(' ')
		b.WriteString(e.Message)
		b.WriteByte('\n')
	}
	return b.String()
}

func (r *Run) setStatus(status string) {
	r.mu.Lock()
	r.snap.Status = status
	r.mu.Unlock()
}

func (r *Run) setProgress(attempt, total int) {
	r.mu.Lock()
	r.snap.Attempt = attempt
	r.snap.Total = total
	r.mu.Unlock()
}

// setStage 切到队列里的某一关（单关队列时 StageIndex=StageTotal=1）。
func (r *Run) setStage(index, total int, code, name string) {
	r.mu.Lock()
	r.snap.StageIndex = index
	r.snap.StageTotal = total
	r.snap.StageCode = code
	r.snap.StageName = name
	r.mu.Unlock()
}

func (r *Run) setJob(id int, title string) {
	r.mu.Lock()
	r.snap.CurrentJobID = id
	r.snap.CurrentJobTitle = title
	r.mu.Unlock()
}

func (r *Run) finish(status, errMsg string) {
	r.mu.Lock()
	r.snap.Status = status
	r.snap.Error = errMsg
	r.snap.FinishedAt = time.Now().Format("2006-01-02 15:04:05")
	r.done = true
	for ch := range r.subs {
		close(ch)
	}
	r.subs = map[chan model.RunEvent]struct{}{}
	r.mu.Unlock()
	r.m.event(r, "info", "status", "任务结束："+statusLabel(status))
}

func (r *Run) statusLabel() string { return statusLabel(r.snap.Status) }

func statusLabel(s string) string {
	switch s {
	case "idle":
		return "空闲"
	case "starting":
		return "启动游戏"
	case "navigating":
		return "导航中"
	case "running":
		return "执行作业"
	case "success":
		return "成功"
	case "failed":
		return "失败"
	case "stopped":
		return "已停止"
	}
	return s
}

func fileExists(p string) bool {
	st, err := os.Stat(p)
	return err == nil && st.Mode().IsRegular()
}

// ---- 小工具 ----

func randSuffix(n int) string {
	const letters = "abcdefghijkmnpqrstuvwxyz23456789"
	b := make([]byte, n)
	seed := time.Now().UnixNano()
	for i := range b {
		seed = seed*6364136223846793005 + 1442695040888963407
		b[i] = letters[uint64(seed)>>33%uint64(len(letters))]
	}
	return string(b)
}

// killTree 用 taskkill 杀进程树（Windows）；失败时退回 Process.Kill。
func killTree(pid int) {
	if pid <= 0 {
		return
	}
	cmd := exec.Command("taskkill", "/PID", fmt.Sprint(pid), "/T", "/F")
	cmd.SysProcAttr = hideWindow()
	if err := cmd.Run(); err != nil {
		if p, err2 := os.FindProcess(pid); err2 == nil {
			_ = p.Kill()
		}
	}
}

// ---- 排序辅助（测试用） ----

func sortAccounts(list []model.Account) {
	sort.SliceStable(list, func(i, j int) bool { return list[i].ID < list[j].ID })
}
