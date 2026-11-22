// copilot-picker：本机浏览器 → 机队 → MAA 抄作业的服务端。
//
// 用法：
//
//	copilot-picker                      # 读 <exe 目录>/config/copilot-picker.json，监听 127.0.0.1:8787
//	copilot-picker -conf E:\x.json      # 指定配置
//	copilot-picker -listen 127.0.0.1:9000
//	copilot-picker -check               # 只打印自检 JSON 后退出（部署后先用它验）
//
// 远程访问：ssh -L 8787:127.0.0.1:8787 <机端>，然后本机浏览器开 http://127.0.0.1:8787/。
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"net"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"github.com/xiachk083-hub/maa-cli-fleet/internal/copilotpick"
)

// version 由构建时注入（-ldflags "-X main.version=..."），默认 dev。
var version = "dev"

func main() {
	var (
		conf        = flag.String("conf", "", "配置文件（默认 <root>/config/copilot-picker.json）")
		root        = flag.String("root", "", "运行根目录（默认 exe 所在目录）")
		listen      = flag.String("listen", "", "监听地址，覆盖配置（默认 127.0.0.1:8787）")
		checkOnly   = flag.Bool("check", false, "只打印自检 JSON 后退出")
		doSetup     = flag.Bool("setup", false, "初始化隔离 MAA 运行目录（junction）后退出")
		showVersion = flag.Bool("version", false, "打印版本后退出")
	)
	flag.Parse()

	if *showVersion {
		fmt.Printf("copilot-picker %s\n", version)
		return
	}

	base := *root
	if base == "" {
		base = projectRoot()
	}
	cfg, cfgErr := copilotpick.LoadConfig(*conf, base)
	if *listen != "" {
		cfg.Listen = *listen
	}
	copilotpick.Version = version

	mgr := copilotpick.NewManager(cfg)
	health := mgr.Health()
	if cfgErr != nil {
		fmt.Fprintf(os.Stderr, "[warn] %v\n", cfgErr)
	}

	// -setup：把 lib/resource 的 junction 与各隔离目录建好（等价于 POST /api/setup）
	if *doSetup {
		ok, err := mgr.Setup(context.Background())
		if err != nil {
			fmt.Fprintf(os.Stderr, "初始化失败：%v\n", err)
			os.Exit(1)
		}
		fmt.Printf("MAA 运行目录已就绪：%v\n", ok)
		return
	}

	if *checkOnly {
		out := map[string]any{"config": cfg, "health": health}
		b, _ := json.MarshalIndent(out, "", "  ")
		fmt.Println(string(b))
		if !health.MaaReady {
			fmt.Fprintln(os.Stderr, "[warn] MAA 运行目录未就绪：先用 POST /api/setup 或检查 maaExe/maaCoreSource")
			os.Exit(2)
		}
		return
	}

	srv := &http.Server{
		Handler:           copilotpick.NewHandler(mgr),
		ReadHeaderTimeout: 10 * time.Second,
	}
	addr := cfg.Listen
	ln, err := net.Listen("tcp", addr)
	if err != nil {
		fmt.Fprintf(os.Stderr, "监听 %s 失败：%v\n", addr, err)
		os.Exit(1)
	}

	fmt.Printf("copilot-picker %s\n", version)
	fmt.Printf("  监听    http://%s/\n", ln.Addr().String())
	fmt.Printf("  根目录  %s\n", cfg.Root)
	fmt.Printf("  账号表  %s\n", cfg.AccountsFile)
	fmt.Printf("  maa-cli %s\n", emptyDash(cfg.MaaExe))
	fmt.Printf("  MAA 目录 %s（%s）\n", emptyDash(cfg.MaaCoreSource), readyText(health.MaaReady))
	fmt.Println("  远程访问：ssh -L 8787:127.0.0.1:8787 <机端>，再开本机浏览器")

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	go func() {
		<-ctx.Done()
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		_ = srv.Shutdown(shutdownCtx)
	}()

	if err := srv.Serve(ln); err != nil && !errors.Is(err, http.ErrServerClosed) {
		fmt.Fprintf(os.Stderr, "服务异常退出：%v\n", err)
		os.Exit(1)
	}
	fmt.Println("已退出")
}

// projectRoot 返回 exe 所在目录（与服务端项目里其它命令一致：产物就地读 config/）。
func projectRoot() string {
	exe, err := os.Executable()
	if err != nil {
		return "."
	}
	return filepath.Dir(exe)
}

func emptyDash(s string) string {
	if s == "" {
		return "（未设置）"
	}
	return s
}

func readyText(ready bool) string {
	if ready {
		return "就绪"
	}
	return "未就绪"
}
