package copilotpick

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"
)

// Config 是 copilot-picker 的运行配置。
//
// 默认值按「机队 E:\maa-cli-fleet」布局推导，也兼容本机仓库；
// 也可以用 -conf 显式指定 JSON 文件覆盖。字段缺省时回落到推导值。
type Config struct {
	Listen           string `json:"listen"`            // 监听地址，默认 127.0.0.1:8787（浏览器经 SSH 隧道访问）
	Root             string `json:"root"`              // 仓库/exe 根目录
	MaaExe           string `json:"maaExe"`            // maa-cli 可执行文件
	MumuRoot         string `json:"mumuRoot"`          // MuMu 安装目录（含 nx_main）
	AdbPath          string `json:"adbPath"`           // adb 可执行文件
	AccountsFile     string `json:"accountsFile"`      // 机队 runner/accounts.json
	PrtsBase         string `json:"prtsBase"`          // 作业站 API
	MaaDataDir       string `json:"maaDataDir"`        // 隔离的 MAA data（lib/resource 用 junction）
	MaaConfigDir     string `json:"maaConfigDir"`      // 隔离的 MAA config（profiles）
	MaaCacheDir      string `json:"maaCacheDir"`       // 隔离的 MAA cache
	MaaStateDir      string `json:"maaStateDir"`       // 隔离的 MAA state（asst.log/截图）
	MaaCoreSource    string `json:"maaCoreSource"`     // junction 源：MAA GUI 目录（含 MaaCore.dll）
	MaaResourceSource string `json:"maaResourceSource"` // junction 源：MAA GUI 的 resource 目录
	RunsDir          string `json:"runsDir"`           // 每次执行的证据/日志目录
	MaaResourceDir   string `json:"maaResourceDir"`    // 读 stages.json 的 resource 目录
	DefaultClient    string `json:"defaultClient"`     // 账号缺 client 时用
	StartupTimeoutSec int   `json:"startupTimeoutSec"` // 启动游戏超时
	NavTimeoutSec     int   `json:"navTimeoutSec"`     // 自动导航超时
	CopilotTimeoutMin int   `json:"copilotTimeoutMin"` // 单份作业超时
	NavMode           string `json:"navMode"`          // 旧模式导航：fight-kill（默认）| skip；joblist 模式不用
	// CopilotMode 决定执行入口：
	//   joblist（默认）单份作业也包成 copilot_list 走 `maa run`，MaaCore 多作业分支
	//                  会自己导航（含活动图）——2026-11-21 本地实测核心接受单条
	//                  copilot_list 并进入 Copilot@FullStageNavigation；
	//   uri           旧行为：`maa copilot maa://<id>`，单作业模式不导航，靠 navMode 抢导航；
	//   file          旧行为：`maa copilot <本地文件>`，能先 normalize 修坏作业，同样不导航。
	CopilotMode string `json:"copilotMode"`
}

// DefaultConfig 返回按 root 推导的默认配置。
func DefaultConfig(root string) Config {
	abs, err := filepath.Abs(root)
	if err == nil {
		root = abs
	}
	cos := filepath.Join(root, "data_copilot")
	cfg := Config{
		Listen:            "127.0.0.1:8787",
		Root:              root,
		PrtsBase:          "https://prts.maa.plus",
		MaaDataDir:        filepath.Join(cos, "maa_data"),
		MaaConfigDir:      filepath.Join(cos, "maa_config"),
		MaaCacheDir:       filepath.Join(cos, "maa_cache"),
		MaaStateDir:       filepath.Join(cos, "maa_state"),
		MaaCoreSource:     `E:\MAA-Multi\MAA_1`,
		MaaResourceSource: `E:\MAA-Multi\MAA_1\resource`,
		RunsDir:           filepath.Join(cos, "runs"),
		DefaultClient:     "Official",
		StartupTimeoutSec: 300,
		NavTimeoutSec:     240,
		CopilotTimeoutMin: 20,
		NavMode:           "fight-kill",
		CopilotMode:       "joblist",
	}
	// maa-cli：优先仓库 bin，其次 WinGet 链接。
	cfg.MaaExe = firstExisting(
		filepath.Join(root, "bin", "maa.exe"),
		filepath.Join(root, "bin", "maa-cli.exe"),
		filepath.Join(os.Getenv("LOCALAPPDATA"), "Microsoft", "WinGet", "Links", "maa-cli.exe"),
	)
	// MuMu：机队 E:\MuMu Player 12 或本机 D:\Xiach\MuMuPlayer。
	mumu := firstDir(
		`E:\MuMu Player 12`,
		`D:\Xiach\MuMuPlayer`,
	)
	cfg.MumuRoot = mumu
	if mumu != "" {
		cfg.AdbPath = filepath.Join(mumu, "nx_main", "adb.exe")
	}
	cfg.AccountsFile = filepath.Join(root, "runner", "accounts.json")
	cfg.MaaResourceDir = filepath.Join(cfg.MaaDataDir, "resource")
	return cfg
}

func firstExisting(paths ...string) string {
	for _, p := range paths {
		if p == "" {
			continue
		}
		if st, err := os.Stat(p); err == nil && !st.IsDir() {
			return p
		}
	}
	return ""
}

func firstDir(paths ...string) string {
	for _, p := range paths {
		if p == "" {
			continue
		}
		if st, err := os.Stat(p); err == nil && st.IsDir() {
			return p
		}
	}
	return ""
}

// LoadConfig 读 JSON 配置并补齐默认值；文件不存在时返回默认配置和错误说明（不致命）。
func LoadConfig(path, root string) (Config, error) {
	cfg := DefaultConfig(root)
	if path == "" {
		path = filepath.Join(root, "config", "copilot-picker.json")
	}
	b, err := os.ReadFile(path)
	if err != nil {
		return cfg, fmt.Errorf("读取配置 %s 失败（使用默认值）: %w", path, err)
	}
	var override Config
	if err := json.Unmarshal(b, &override); err != nil {
		return cfg, fmt.Errorf("配置 %s 不是合法 JSON（使用默认值）: %w", path, err)
	}
	mergeConfig(&cfg, &override)
	return cfg, nil
}

func mergeConfig(dst *Config, src *Config) {
	set := func(p *string, v string) {
		if v != "" {
			*p = v
		}
	}
	set(&dst.Listen, src.Listen)
	set(&dst.Root, src.Root)
	set(&dst.MaaExe, src.MaaExe)
	set(&dst.MumuRoot, src.MumuRoot)
	set(&dst.AdbPath, src.AdbPath)
	set(&dst.AccountsFile, src.AccountsFile)
	set(&dst.PrtsBase, src.PrtsBase)
	set(&dst.MaaDataDir, src.MaaDataDir)
	set(&dst.MaaConfigDir, src.MaaConfigDir)
	set(&dst.MaaCacheDir, src.MaaCacheDir)
	set(&dst.MaaStateDir, src.MaaStateDir)
	set(&dst.MaaCoreSource, src.MaaCoreSource)
	set(&dst.MaaResourceSource, src.MaaResourceSource)
	set(&dst.RunsDir, src.RunsDir)
	set(&dst.MaaResourceDir, src.MaaResourceDir)
	set(&dst.DefaultClient, src.DefaultClient)
	set(&dst.NavMode, src.NavMode)
	set(&dst.CopilotMode, src.CopilotMode)
	if src.StartupTimeoutSec > 0 {
		dst.StartupTimeoutSec = src.StartupTimeoutSec
	}
	if src.NavTimeoutSec > 0 {
		dst.NavTimeoutSec = src.NavTimeoutSec
	}
	if src.CopilotTimeoutMin > 0 {
		dst.CopilotTimeoutMin = src.CopilotTimeoutMin
	}
	// 派生字段在覆盖 lib/资源目录后重算一次。
	if src.MaaResourceDir == "" && src.MaaDataDir != "" {
		dst.MaaResourceDir = filepath.Join(dst.MaaDataDir, "resource")
	}
}

// MaaReady 判断隔离 MAA 运行目录是否就绪（lib 与 resource 都能读到）。
func (c Config) MaaReady() (bool, string) {
	if c.MaaExe == "" {
		return false, "找不到 maa-cli 可执行文件（bin\\maa.exe 或 WinGet 链接），可在 config/copilot-picker.json 指定 maaExe"
	}
	if _, err := os.Stat(filepath.Join(c.MaaDataDir, "lib")); err != nil {
		return false, "MAA 运行目录未初始化：缺少 " + filepath.Join(c.MaaDataDir, "lib") + "，请点「初始化 MAA 运行目录」"
	}
	if _, err := os.Stat(filepath.Join(c.MaaDataDir, "resource", "tasks", "tasks.json")); err != nil {
		return false, "MAA 资源目录不可用：" + filepath.Join(c.MaaDataDir, "resource") + "，请检查 maaResourceSource 或点初始化"
	}
	return true, ""
}

// Env 返回调 maa-cli 时必须带的环境变量（隔离目录）。
func (c Config) Env() []string {
	// 去掉代理环境变量：本机开着 Clash 时，maa-cli 走代理访问 maa.plus 会
	// TLS handshake timeout / unexpected EOF（2026-11-21 实测），直连正常。
	base := os.Environ()
	env := make([]string, 0, len(base)+4)
	for _, kv := range base {
		name := kv
		if i := strings.IndexByte(kv, '='); i >= 0 {
			name = kv[:i]
		}
		switch strings.ToUpper(name) {
		case "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY":
			continue
		}
		env = append(env, kv)
	}
	env = append(env,
		"MAA_CONFIG_DIR="+c.MaaConfigDir,
		"MAA_DATA_DIR="+c.MaaDataDir,
		"MAA_CACHE_DIR="+c.MaaCacheDir,
		"MAA_STATE_DIR="+c.MaaStateDir,
	)
	return env
}

// AsstLogPath 是 MAA 的 asst.log 路径。
func (c Config) AsstLogPath() string {
	return filepath.Join(c.MaaStateDir, "debug", "asst.log")
}

// OSName 便于日志输出。
func OSName() string { return runtime.GOOS }
