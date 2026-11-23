// Package picheck —— 纯 Go 版 MaaFramework PI 管线静态检查（取代 tools/pi_precheck.py）。
//
// 检查内容：
//  1. interface.json 与所有 resource.path 叠加层下 pipeline/*.json 的 JSON 合法性；
//  2. task[].entry 在合并节点表中存在；
//  3. next / on_error / interrupt 引用（自动剥离 [JumpBack]/[Anchor] 前缀）均存在；
//  4. template 引用的图片在任一叠加层 image/ 目录下存在。
package picheck

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
)

var reRefPrefix = regexp.MustCompile(`^(?:\[[^\]]+\])+`)

// ProjectReport 是单个 PI 项目的静态检查结果。
type ProjectReport struct {
	Name   string   `json:"name"`
	Dir    string   `json:"dir"`
	Layers int      `json:"layers"`
	Files  int      `json:"files"`
	Nodes  int      `json:"nodes"`
	Errors []string `json:"errors,omitempty"`
	Warns  []string `json:"warns,omitempty"`
}

// CheckAll 扫描 piRoot 下所有含 interface.json 的项目。
func CheckAll(piRoot string) ([]ProjectReport, error) {
	entries, err := os.ReadDir(piRoot)
	if err != nil {
		return nil, fmt.Errorf("读 PI 根目录 %s 失败：%w", piRoot, err)
	}
	var names []string
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		if _, err := os.Stat(filepath.Join(piRoot, e.Name(), "interface.json")); err == nil {
			names = append(names, e.Name())
		}
	}
	sort.Strings(names)
	out := make([]ProjectReport, 0, len(names))
	for _, name := range names {
		out = append(out, CheckProject(filepath.Join(piRoot, name)))
	}
	return out, nil
}

// CheckProject 静态检查单个 PI 项目目录。
func CheckProject(projDir string) ProjectReport {
	rep := ProjectReport{
		Name: filepath.Base(projDir),
		Dir:  projDir,
	}
	ifacePath := filepath.Join(projDir, "interface.json")
	raw, err := os.ReadFile(ifacePath)
	if err != nil {
		rep.Errors = append(rep.Errors, fmt.Sprintf("读 interface.json 失败：%v", err))
		return rep
	}
	var iface struct {
		Resource []struct {
			Name string   `json:"name"`
			Path []string `json:"path"`
		} `json:"resource"`
		Task []struct {
			Name  string `json:"name"`
			Entry string `json:"entry"`
		} `json:"task"`
	}
	if err := json.Unmarshal(raw, &iface); err != nil {
		rep.Errors = append(rep.Errors, fmt.Sprintf("interface.json JSON 非法：%v", err))
		return rep
	}

	var layerDirs []string
	seenLayer := map[string]bool{}
	for _, r := range iface.Resource {
		for _, p := range r.Path {
			clean := filepath.Clean(filepath.Join(projDir, filepath.FromSlash(strings.TrimPrefix(p, "{PROJECT_DIR}/"))))
			if !seenLayer[clean] {
				seenLayer[clean] = true
				layerDirs = append(layerDirs, clean)
			}
		}
	}
	if len(layerDirs) == 0 {
		layerDirs = []string{filepath.Join(projDir, "resource")}
	}
	rep.Layers = len(layerDirs)

	merged := map[string]map[string]any{}
	imageRoots := make([]string, 0, len(layerDirs))
	for _, layer := range layerDirs {
		imgDir := filepath.Join(layer, "image")
		if st, err := os.Stat(imgDir); err == nil && st.IsDir() {
			imageRoots = append(imageRoots, imgDir)
		}
		pipeDir := filepath.Join(layer, "pipeline")
		if st, err := os.Stat(pipeDir); err != nil || !st.IsDir() {
			continue
		}
		_ = filepath.Walk(pipeDir, func(p string, info os.FileInfo, werr error) error {
			if werr != nil || info.IsDir() || !strings.HasSuffix(strings.ToLower(info.Name()), ".json") {
				return nil
			}
			rep.Files++
			b, err := os.ReadFile(p)
			if err != nil {
				rep.Errors = append(rep.Errors, fmt.Sprintf("读 %s 失败：%v", relPath(projDir, p), err))
				return nil
			}
			var doc map[string]map[string]any
			if err := json.Unmarshal(b, &doc); err != nil {
				rep.Errors = append(rep.Errors, fmt.Sprintf("pipeline JSON 非法 %s：%v", relPath(projDir, p), err))
				return nil
			}
			for nodeName, body := range doc {
				merged[nodeName] = body
			}
			return nil
		})
	}
	rep.Nodes = len(merged)

	// 1. 校验 task[].entry
	for _, t := range iface.Task {
		if t.Entry == "" {
			rep.Errors = append(rep.Errors, fmt.Sprintf("任务 %q 缺少 entry", t.Name))
			continue
		}
		if _, ok := merged[t.Entry]; !ok {
			rep.Errors = append(rep.Errors, fmt.Sprintf("任务 %q 的 entry %q 不存在", t.Name, t.Entry))
		}
	}

	// 2. 校验节点引用与模板图片
	nodeNames := make([]string, 0, len(merged))
	for k := range merged {
		nodeNames = append(nodeNames, k)
	}
	sort.Strings(nodeNames)
	for _, name := range nodeNames {
		node := merged[name]
		for _, field := range []string{"next", "on_error", "interrupt"} {
			for _, ref := range extractRefs(node[field]) {
				target := stripRefPrefix(ref)
				if target == "" {
					continue
				}
				if _, ok := merged[target]; !ok {
					rep.Errors = append(rep.Errors, fmt.Sprintf("节点 %s.%s 引用不存在：%q", name, field, ref))
				}
			}
		}
		for _, tpl := range extractStrings(node["template"]) {
			if !imageExists(imageRoots, tpl) {
				rep.Errors = append(rep.Errors, fmt.Sprintf("节点 %s 缺少模板图片：%q", name, tpl))
			}
		}
	}
	return rep
}

func stripRefPrefix(ref string) string {
	return strings.TrimSpace(reRefPrefix.ReplaceAllString(strings.TrimSpace(ref), ""))
}

func extractRefs(v any) []string {
	switch x := v.(type) {
	case string:
		return []string{x}
	case []any:
		var out []string
		for _, item := range x {
			switch it := item.(type) {
			case string:
				out = append(out, it)
			case map[string]any:
				if name, ok := it["name"].(string); ok {
					out = append(out, name)
				}
			}
		}
		return out
	}
	return nil
}

func extractStrings(v any) []string {
	switch x := v.(type) {
	case string:
		if strings.TrimSpace(x) != "" {
			return []string{x}
		}
	case []any:
		var out []string
		for _, item := range x {
			if s, ok := item.(string); ok && strings.TrimSpace(s) != "" {
				out = append(out, s)
			}
		}
		return out
	}
	return nil
}

func imageExists(roots []string, rel string) bool {
	p := filepath.FromSlash(rel)
	for _, r := range roots {
		if st, err := os.Stat(filepath.Join(r, p)); err == nil && !st.IsDir() {
			return true
		}
	}
	return false
}

func relPath(base, target string) string {
	if r, err := filepath.Rel(base, target); err == nil {
		return filepath.ToSlash(r)
	}
	return target
}
