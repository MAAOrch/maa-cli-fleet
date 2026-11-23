// Package model 定义 copilot-picker 前后端共享的数据结构。
// JSON tag 即 HTTP API 契约，改字段前先看 docs/COPILOT-PICKER-CONTRACT.md。
package model

// Account 是机队 runner/accounts.json 里的一行 + 本机解析出的 adb 地址/在线状态。
type Account struct {
	ID      string `json:"id"`
	Name    string `json:"name"`
	Client  string `json:"client"`
	Emu     string `json:"emu"`
	Daily   string `json:"daily,omitempty"`
	Enabled bool   `json:"enabled"`
	Adb     string `json:"adb"`
	Online  bool   `json:"online"`
}

// RosterEntry 是 MAA 干员识别结果里的一个干员（只保留已拥有）。
//
// Skills/Modules/SkillKnown 只有部分数据源有：MAA 本地识别（data/OperBoxData.json
// 的 source=local）拿不到技能与模组，SkillKnown=false——匹配时这两维按「已拥有」
// 处理，否则整份名单会被判成满屏「需补练度」。
type RosterEntry struct {
	Name       string      `json:"name"`
	OperID     string      `json:"operId,omitempty"`
	Rarity     int         `json:"rarity"`
	Elite      int         `json:"elite"`
	Level      int         `json:"level"`
	Potential  int         `json:"potential"`
	SkillLevel int         `json:"skillLevel,omitempty"` // 主技能等级（1~7）
	Skills     []int       `json:"skills,omitempty"`     // 各技能专精等级（0~3，序号即技能位）
	Modules    map[int]int `json:"modules,omitempty"`    // 模组槽位 → 等级（1~3）
	SkillKnown bool        `json:"skillKnown"`
}

// Roster 是解析后的名册。
type Roster struct {
	Entries    []RosterEntry `json:"entries"`
	Count      int           `json:"count"`
	Six        int           `json:"six"`
	Five       int           `json:"five"`
	Format     string        `json:"format"` // operbox|clipboard|table|names
	Source     string        `json:"source,omitempty"`
	SyncedAt   string        `json:"syncedAt,omitempty"`
	SkillKnown bool          `json:"skillKnown"`
	Warnings   []string      `json:"warnings,omitempty"`
}

// Stage 是关卡候选：Code 是用户/游戏里看到的名字（TL-9），StageName 是作业站的关卡代码（act55side_09）。
type Stage struct {
	Code      string `json:"code"`
	StageName string `json:"stageName"`
	Title     string `json:"title,omitempty"`
	Source    string `json:"source,omitempty"`
}

// Job 是一份作业站的作业 + 与本账号名册的匹配情况。
type Job struct {
	ID             int      `json:"id"`
	Title          string   `json:"title"`
	StageName      string   `json:"stageName"`
	Difficulty     int      `json:"difficulty"` // 1=普通 2=突袭 3=磨难（S关高难，执行按突袭） 0=未标注
	Rating         int      `json:"rating"`
	Views          int      `json:"views"`
	MinimumRequired string  `json:"minimumRequired,omitempty"`
	UploadTime     string   `json:"uploadTime,omitempty"`
	Details        string   `json:"details,omitempty"`
	Opers           []string `json:"opers"`
	Missing         []string `json:"missing,omitempty"`
	LevelShort      []string `json:"levelShort,omitempty"`
	Total           int      `json:"total"` // 出场干员数 + 干员组数
	Met             bool     `json:"met"`
	RaritySum       int      `json:"raritySum"`
	Mode            string   `json:"mode,omitempty"`            // ready|borrow|train|blocked
	ImpliedMissing  int      `json:"impliedMissing,omitempty"`  // 作业未写练度、按星级补默认要求的条目数
	RequirementFuzz bool     `json:"requirementFuzz,omitempty"` // 名册缺技能数据，技能/模组按已拥有处理
	LocalScore      float64  `json:"localScore,omitempty"`      // 跨号共享评分库（job_scores.json）综合分
	LocalPass       int      `json:"localPass,omitempty"`       // 跨号实测 3★/通关次数
	LocalFail       int      `json:"localFail,omitempty"`       // 跨号实测失败/低星次数
}

// JobSearch 是一次“搜作业”的结果。
type JobSearch struct {
	Stage   Stage `json:"stage"`
	Jobs    []Job `json:"jobs"`
	Blocked []Job `json:"blocked,omitempty"`
	// Train 是“干员都有、但练度不够”的作业：不能直接抄，单列出来给用户看。
	Train []Job `json:"train,omitempty"`
	Total   int   `json:"total"`   // 该关卡在作业站上的作业总数（按翻页估算/统计）
	Fetched int   `json:"fetched"` // 本次实际拉取并解析的作业数
}

// StagePlan 是半自动多关队列里的一关：人工选好关卡与该关的候选作业顺序。
//
// 队列由前端拼好后一次下发（RunOptions.Stages）；不传 Stages 时服务端用
// StageCode/StageName/JobIDs 补成单关，保持旧调用兼容。
type StagePlan struct {
	StageCode string `json:"stageCode"` // 导航用显示名，如 TL-9；可为空，MAA 按 Tile code 导航
	StageName string `json:"stageName"` // 作业站关卡代码，如 act55side_09
	// Phase 所属活动地图页（main=第1阶段普通图 / ex=第2阶段EX图 / s=第3阶段S图）；
	// 留空时由阶段分流引擎按 StageCode/StageName 自动推断。
	Phase  string `json:"phase,omitempty"`
	JobIDs []int  `json:"jobIds"` // 按顺序尝试
	// RaidJobIDs 是需要按突袭执行的作业（作业站 difficulty=2 突袭 / 3 磨难）。
	// 执行器对这几份作业在 copilot_list 里写 is_raid=true。
	RaidJobIDs []int `json:"raidJobIds,omitempty"`
}

// RunOptions 是点击“开始”时下发的参数。
type RunOptions struct {
	AccountID          string `json:"accountId"`
	Client             string `json:"client"`
	// Adb 覆盖账号表解析出的 adb 地址：机队 accounts.json 的 emu 绑定偶尔与实际
	// 在线实例不一致（实例换号后没同步），这时按实际地址跑，不要改账号表。
	Adb                string `json:"adb,omitempty"`
	StageCode          string `json:"stageCode"`   // 导航用显示名，如 TL-9
	StageName          string `json:"stageName"`   // 作业站关卡代码，如 act55side_09
	JobIDs             []int  `json:"jobIds"`      // 按顺序尝试
	// Stages 是多关队列（半自动：人工选关/选作业，程序逐关执行）。
	// 为空时服务端用上面的单关字段补一条。
	Stages             []StagePlan `json:"stages,omitempty"`
	Formation          bool   `json:"formation"`   // 自动编队
	AddTrust           bool   `json:"addTrust"`    // 补低信赖
	IgnoreRequirements bool   `json:"ignoreRequirements"`
	ManualMode         bool   `json:"manualMode"` // 用户已停在关卡编队/详情页，跳过导航
	// OnMap 表示「游戏已被外部工具停在活动地图上」：执行器不 startup、不做章节导航，
	// 直接走作业集入口，让 MaaCore 的 find_stage 在当前屏幕 OCR 找关卡并点进去。
	// 活动图不在 MAA 主章节列表里，从主界面起导航必然在章节列表里滑屏死循环。
	OnMap bool `json:"onMap"`
	// PhaseNav 阶段分流模式：自动把 Stages 按活动地图页分组（main → ex → s），
	// 同阶段内走 OnMap 连续多关（由 MAA 自己找关），仅在切换阶段时调用一次 PI 进页导航。
	PhaseNav  bool   `json:"phaseNav,omitempty"`
	PiProject string `json:"piProject,omitempty"` // 阶段导航使用的 PI 项目名（默认 arknights-event）
	// PhaseNavTasks 按阶段覆盖页面级 PI 导航任务名（如 {"main":"导入:新活动普通图","ex":"导入:新活动EX"}）；
	// 未指定的阶段回退到默认「进入普通图 / 进入EX图 / 进入S图」。
	PhaseNavTasks map[string]string `json:"phaseNavTasks,omitempty"`
	MaxAttempts        int    `json:"maxAttempts"`
	SupportUnitUsage   int    `json:"supportUnitUsage"` // 0 不用助战
	TimeoutMin         int    `json:"timeoutMin"`
	// CopilotFile 直接跑本地 copilot JSON（Plan B 自研轴用）：给了就不从作业站下载，
	// 仍走 ManualMode 单文件 copilot 路径。JobIDs 只需占位一个（如 0）。
	CopilotFile string `json:"copilotFile,omitempty"`
	// NoProgress 只打不记台账（演习试错专用，避免把演习结果写进正式进度）。
	NoProgress bool `json:"noProgress,omitempty"`
}

// RunEvent 是一条执行事件（日志/阶段/尝试）。
type RunEvent struct {
	Seq     int    `json:"seq"`
	At      string `json:"at"`
	Level   string `json:"level"` // info|warn|error|stage
	Kind    string `json:"kind"`  // log|status|attempt|result
	Attempt int    `json:"attempt,omitempty"`
	JobID   int    `json:"jobId,omitempty"`
	Message string `json:"message"`
}

// RunSnapshot 是执行状态快照。
type RunSnapshot struct {
	ID              string     `json:"id"`
	Status          string     `json:"status"` // idle|starting|navigating|running|success|failed|stopped
	AccountID       string     `json:"accountId"`
	AccountName     string     `json:"accountName,omitempty"`
	Client          string     `json:"client"`
	StageCode       string     `json:"stageCode"`
	StageName       string     `json:"stageName"`
	Phase           string     `json:"phase,omitempty"`      // 当前阶段：main | ex | s
	StageIndex      int        `json:"stageIndex,omitempty"` // 当前是队列里的第几关（1 起）
	StageTotal      int        `json:"stageTotal,omitempty"` // 队列总关数（1 = 单关）
	Attempt         int        `json:"attempt"`
	Total           int        `json:"total"`
	CurrentJobID    int        `json:"currentJobId,omitempty"`
	CurrentJobTitle string     `json:"currentJobTitle,omitempty"`
	StartedAt       string     `json:"startedAt,omitempty"`
	FinishedAt      string     `json:"finishedAt,omitempty"`
	Error           string     `json:"error,omitempty"`
	Events          []RunEvent `json:"events,omitempty"`
	HasShot         bool       `json:"hasShot"`
}

// Health 是健康检查。
type Health struct {
	OK        bool   `json:"ok"`
	Version   string `json:"version"`
	Root      string `json:"root"`
	MaaReady  bool   `json:"maaReady"`
	MaaExe    string `json:"maaExe"`
	Accounts  int    `json:"accounts"`
	Listen    string `json:"listen"`
	PrtsBase  string `json:"prtsBase"`
}
