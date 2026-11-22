package copilotpick

// 「回主界面」这一件事交给 MAA 的 startup：它会点「开始唤醒」并处理弹窗/公告，
// 比在驱动脚本里盲点返回键可靠得多（2026-11-22 实测：在卫戍协议小游戏里点返回
// 根本出不来，最后是靠 maa startup 拉回主界面的）。

import (
	"context"
	"fmt"
	"os/exec"
	"strings"
	"time"
)

// arknightsPackages 是本机可能共存的所有《明日方舟》客户端包名。
//
// 2026-11-22 05:32 事故：模拟器里官服客户端在前台，`maa startup Bilibili` 的模板匹配
// 不分客户端，直接把**官服账号**唤醒了，白打 3 场、把理智算错。修法：任何要动游戏
// （startup / 拉起 / 重启）之前，先把非目标客户端全部 force-stop，保证前台只有目标号。
var arknightsPackages = []string{
	"com.hypergryph.arknights",           // 官服
	"com.hypergryph.arknights.bilibili",  // B服
	"com.YoStarJP.Arknights",             // 日服
	"com.YoStarEN.Arknights",             // 国际服
	"com.YoStarKR.Arknights",             // 韩服
}

// isolateClient 关掉目标客户端以外的所有舟游客户端，避免 maa startup 唤醒别的号。
// 失败只忽略（adb 抖动/包没装都不致命），但会把结果记进 tail 方便排查。
func isolateClient(ctx context.Context, cfg Config, adb, client string) (string, error) {
	if cfg.AdbPath == "" || adb == "" {
		return "", fmt.Errorf("缺少 adb 路径或地址")
	}
	keep := clientPackage(client)
	var stopped []string
	for _, pkg := range arknightsPackages {
		if pkg == keep {
			continue
		}
		cctx, cancel := context.WithTimeout(ctx, 20*time.Second)
		cmd := exec.CommandContext(cctx, cfg.AdbPath, "-s", adb, "shell", "am", "force-stop", pkg)
		cmd.SysProcAttr = hideWindow()
		_ = cmd.Run()
		cancel()
		stopped = append(stopped, pkg)
	}
	return "已隔离其它客户端：" + strings.Join(stopped, ","), nil
}

// ReturnHome 用 `maa startup <client>` 把游戏带回主界面，返回 maa 的尾部输出。
func (m *Manager) ReturnHome(ctx context.Context, accountID string) (string, error) {
	if r := m.CurrentRun(); r != nil && !r.IsDone() {
		return "", fmt.Errorf("有执行中的任务，先停掉再回主界面")
	}
	accounts, err := m.Accounts(ctx)
	if err != nil {
		return "", err
	}
	acc, ok := findAccount(accounts, accountID)
	if !ok {
		return "", fmt.Errorf("账号 %s 不存在", accountID)
	}
	if acc.Adb == "" {
		return "", fmt.Errorf("账号 %s 解析不出 adb 地址", accountID)
	}
	client := acc.Client
	if client == "" {
		client = m.cfg.DefaultClient
	}
	profile, err := writeMaaProfile(m.cfg, acc.Adb, acc.Emu)
	if err != nil {
		return "", fmt.Errorf("写 MAA 连接配置失败：%w", err)
	}
	if note, err := isolateClient(ctx, m.cfg, acc.Adb, client); err == nil {
		_ = note // 静默：正常路径不需要日志噪音
	}
	if ensureGameRunning(ctx, m.cfg, acc.Adb, client) {
		time.Sleep(3 * time.Second)
	}
	cctx, cancel := context.WithTimeout(ctx, 4*time.Minute)
	defer cancel()
	cmd := exec.CommandContext(cctx, m.cfg.MaaExe,
		"startup", client, "-p", profile, "--batch", "--no-summary")
	cmd.Env = m.cfg.Env()
	cmd.SysProcAttr = hideWindow()
	out, err := cmd.CombinedOutput()
	tail := lastLines(string(out), 6)
	if err != nil {
		return tail, fmt.Errorf("maa startup 失败：%v %s", err, tail)
	}
	return tail, nil
}

// defaultAccountID 取第一个启用账号（请求没带 accountId 时兜底）。
func (m *Manager) defaultAccountID(ctx context.Context) string {
	accounts, err := m.Accounts(ctx)
	if err != nil {
		return ""
	}
	for _, a := range accounts {
		if a.Enabled {
			return a.ID
		}
	}
	return ""
}

// RestartGame 关掉游戏再拉起来：这是从「小游戏/弹层/奇怪页面」里脱身最快最稳的办法
// （2026-11-22 实测：在卫戍协议小游戏里按返回键、按 maa startup 都出不来）。
// 拉起来后停在标题页，后面接 MAA 的 startup 就能回主界面。
func (m *Manager) RestartGame(ctx context.Context, accountID string) (string, error) {
	if r := m.CurrentRun(); r != nil && !r.IsDone() {
		return "", fmt.Errorf("有执行中的任务，先停掉再重启游戏")
	}
	accounts, err := m.Accounts(ctx)
	if err != nil {
		return "", err
	}
	acc, ok := findAccount(accounts, accountID)
	if !ok {
		return "", fmt.Errorf("账号 %s 不存在", accountID)
	}
	if acc.Adb == "" {
		return "", fmt.Errorf("账号 %s 解析不出 adb 地址", accountID)
	}
	client := acc.Client
	if client == "" {
		client = m.cfg.DefaultClient
	}
	pkg := clientPackage(client)
	run := func(args ...string) error {
		cctx, cancel := context.WithTimeout(ctx, 60*time.Second)
		defer cancel()
		cmd := exec.CommandContext(cctx, m.cfg.AdbPath, append([]string{"-s", acc.Adb}, args...)...)
		cmd.SysProcAttr = hideWindow()
		return cmd.Run()
	}
	if m.cfg.AdbPath == "" {
		return "", fmt.Errorf("配置里没有 adbPath")
	}
	// 重启前先隔离其它客户端，避免启动后前台是别的服、MAA 模板认到别的号
	_, _ = isolateClient(ctx, m.cfg, acc.Adb, client)
	_ = run("shell", "am", "force-stop", pkg)
	time.Sleep(2 * time.Second)
	if err := run("shell", "monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1"); err != nil {
		return "", fmt.Errorf("拉起 %s 失败：%w", pkg, err)
	}
	// 等 Unity 加载出标题页（冷启动实测 20~30s，首帧还可能黑屏）。
	time.Sleep(28 * time.Second)
	return "已重启 " + pkg, nil
}
