package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"time"

	"github.com/xiachk083-hub/maa-cli-fleet/internal/mas"
	"github.com/xiachk083-hub/maa-cli-fleet/internal/picheck"
)

func runMas(args []string) error {
	sub := "brief"
	if len(args) > 0 && args[0] != "" && args[0][0] != '-' {
		sub = args[0]
		args = args[1:]
	}
	switch sub {
	case "brief", "verify":
		defaultURL := os.Getenv("MAS_API")
		if defaultURL == "" {
			defaultURL = "http://127.0.0.1:36163"
		}
		masURL := flagValue(args, "-mas", defaultURL)
		day := flagValue(args, "-day", mas.GameDay(time.Now()))
		brief, err := mas.FetchBrief(context.Background(), masURL, day, 20*time.Second)
		if err != nil {
			return err
		}
		if hasFlag(args, "-json") {
			b, _ := json.MarshalIndent(brief, "", "  ")
			fmt.Println(string(b))
			return nil
		}
		fmt.Print(brief.RenderMarkdown())
		if sub == "verify" && (brief.Counts["failed"] > 0 || brief.Counts["done_then_error"] > 0) {
			os.Exit(2)
		}
		return nil
	default:
		return fmt.Errorf("用法：fleet mas brief|verify [-mas URL] [-day YYYY-MM-DD] [-json]")
	}
}

func runPi(args []string, root string) error {
	sub := "check"
	if len(args) > 0 && args[0] != "" && args[0][0] != '-' {
		sub = args[0]
		args = args[1:]
	}
	switch sub {
	case "check":
		defaultPi := filepath.Join(root, "pi")
		if _, err := os.Stat(defaultPi); err != nil {
			if st, err2 := os.Stat("pi"); err2 == nil && st.IsDir() {
				defaultPi = "pi"
			}
		}
		piRoot := flagValue(args, "-dir", defaultPi)
		project := flagValue(args, "-project", "")
		strict := hasFlag(args, "-strict")
		var reports []picheck.ProjectReport
		if project != "" {
			reports = []picheck.ProjectReport{picheck.CheckProject(filepath.Join(piRoot, project))}
		} else {
			var err error
			reports, err = picheck.CheckAll(piRoot)
			if err != nil {
				return err
			}
		}
		totalErr, totalWarn := 0, 0
		for _, r := range reports {
			totalErr += len(r.Errors)
			totalWarn += len(r.Warns)
			fmt.Printf("=== pi/%s (%d 层, %d 文件, %d 节点) ===\n", r.Name, r.Layers, r.Files, r.Nodes)
			for _, e := range r.Errors {
				fmt.Printf("  [error] %s\n", e)
			}
			for _, w := range r.Warns {
				fmt.Printf("  [warn]  %s\n", w)
			}
			if len(r.Errors) == 0 && len(r.Warns) == 0 {
				fmt.Println("  [ok]    0 error, 0 warn")
			}
		}
		fmt.Printf("\n== fleet pi check 汇总 (%d 个项目): %d error, %d warn ==\n", len(reports), totalErr, totalWarn)
		if totalErr > 0 || (strict && totalWarn > 0) {
			return fmt.Errorf("PI 静态检查未通过（%d error, %d warn）", totalErr, totalWarn)
		}
		return nil
	default:
		return fmt.Errorf("用法：fleet pi check [-dir 路径] [-project 项目名] [-strict]")
	}
}
