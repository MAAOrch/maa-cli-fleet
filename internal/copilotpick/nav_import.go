package copilotpick

// 可视化导航文件导入与管理：
// 支持直接导入 MAA 可视化编辑器 / MaaPipelineEditor (MPE) 导出的单文件 .json 或资源包 .zip，
// 自动解析入口节点、同步到 PI 项目的 resource/imported 叠加层并在 interface.json 注册任务名，
// 前端可在「一键全自动推活动」里为第 1 / 2 / 3 阶段直接绑定导入的导航。

import (
	"archive/zip"
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

// NavPack 描述一份已导入的可视化导航包。
type NavPack struct {
	ID        string   `json:"id"`
	Name      string   `json:"name"`
	Phase     string   `json:"phase"`     // main | ex | s | ""
	TaskName  string   `json:"taskName"`  // 写入 PI interface.json 的任务名，如 "导入:新活动1阶段"
	Entry     string   `json:"entry"`     // 入口节点名
	Nodes     []string `json:"nodes"`     // 包含的全部节点名
	Images    int      `json:"images"`    // 包含的模板图片数
	CreatedAt string   `json:"createdAt"` // RFC3339
}

const maxNavUploadBytes = 32 << 20 // 32 MiB

func (m *Manager) navPacksIndexPath() string {
	return filepath.Join(m.cfg.Root, "nav_packs", "index.json")
}

func (m *Manager) navPacksDir() string {
	return filepath.Join(m.cfg.Root, "nav_packs")
}

func (m *Manager) ListNavPacks() []NavPack {
	b, err := os.ReadFile(m.navPacksIndexPath())
	if err != nil {
		return []NavPack{}
	}
	var packs []NavPack
	if json.Unmarshal(b, &packs) != nil || packs == nil {
		return []NavPack{}
	}
	return packs
}

func (m *Manager) saveNavPacks(packs []NavPack) error {
	if err := os.MkdirAll(m.navPacksDir(), 0o755); err != nil {
		return err
	}
	b, err := json.MarshalIndent(packs, "", "  ")
	if err != nil {
		return err
	}
	path := m.navPacksIndexPath()
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, append(b, '\n'), 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

func (h *handler) navPacksList(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"packs": h.m.ListNavPacks(),
	})
}

func (h *handler) navPackImport(w http.ResponseWriter, r *http.Request) {
	if err := r.ParseMultipartForm(maxNavUploadBytes); err != nil {
		writeErr(w, http.StatusBadRequest, "解析上传文件失败："+err.Error())
		return
	}
	file, header, err := r.FormFile("file")
	if err != nil {
		writeErr(w, http.StatusBadRequest, "缺少上传文件字段 file")
		return
	}
	defer file.Close()

	data, err := io.ReadAll(io.LimitReader(file, maxNavUploadBytes+1))
	if err != nil {
		writeErr(w, http.StatusBadRequest, "读取上传文件失败："+err.Error())
		return
	}
	if len(data) == 0 || len(data) > maxNavUploadBytes {
		writeErr(w, http.StatusBadRequest, "文件为空或超过 32MB 限制")
		return
	}

	name := strings.TrimSpace(r.FormValue("name"))
	if name == "" {
		base := filepath.Base(header.Filename)
		name = strings.TrimSuffix(base, filepath.Ext(base))
	}
	phase := NormalizePhase(r.FormValue("phase"))
	wantEntry := strings.TrimSpace(r.FormValue("entry"))
	project := strings.TrimSpace(r.FormValue("project"))
	if project == "" {
		project = defaultPhasePiProject
	}

	pack, err := h.m.ImportNavPack(header.Filename, data, name, phase, wantEntry, project)
	if err != nil {
		writeErr(w, http.StatusBadRequest, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"ok":   true,
		"pack": pack,
	})
}

func (h *handler) navPackDelete(w http.ResponseWriter, r *http.Request) {
	id := strings.TrimSpace(r.PathValue("id"))
	if id == "" {
		writeErr(w, http.StatusBadRequest, "缺少导航包 id")
		return
	}
	if err := h.m.DeleteNavPack(id, defaultPhasePiProject); err != nil {
		writeErr(w, http.StatusBadRequest, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "id": id})
}

func (h *handler) navPackRun(w http.ResponseWriter, r *http.Request) {
	var req struct {
		AccountID string `json:"accountId"`
		TaskName  string `json:"taskName"`
		Project   string `json:"project,omitempty"`
	}
	if err := decodeBody(w, r, &req); err != nil {
		writeErr(w, http.StatusBadRequest, err.Error())
		return
	}
	req.AccountID = strings.TrimSpace(req.AccountID)
	req.TaskName = strings.TrimSpace(req.TaskName)
	if req.AccountID == "" || req.TaskName == "" {
		writeErr(w, http.StatusBadRequest, "缺少 accountId 或 taskName")
		return
	}
	accounts, err := h.m.Accounts(r.Context())
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	acc, ok := findAccount(accounts, req.AccountID)
	if !ok {
		writeErr(w, http.StatusBadRequest, "账号不存在："+req.AccountID)
		return
	}
	if acc.Adb == "" {
		writeErr(w, http.StatusBadRequest, "账号解析不出 adb 地址："+req.AccountID)
		return
	}
	tail, err := h.m.RunPiNavTask(r.Context(), acc.Adb, req.Project, req.TaskName)
	if err != nil {
		writeErr(w, http.StatusBadRequest, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"ok":        true,
		"accountId": req.AccountID,
		"taskName":  req.TaskName,
		"output":    tail,
	})
}

// ImportNavPack 解析 .json 或 .zip 导航文件，落盘并同步到 PI 项目 overlay。
func (m *Manager) ImportNavPack(filename string, data []byte, name, phase, wantEntry, piProject string) (NavPack, error) {
	extName := strings.ToLower(filepath.Ext(filename))
	if extName != ".json" && extName != ".zip" {
		return NavPack{}, fmt.Errorf("仅支持导入 .json（MPE/MAA 导出的 pipeline）或 .zip 资源包，当前为 %q", extName)
	}
	id := fmt.Sprintf("nav-%s-%s", time.Now().Format("20060102-150405"), randSuffix(4))
	packDir := filepath.Join(m.navPacksDir(), id)
	pipeDir := filepath.Join(packDir, "pipeline")
	imgDir := filepath.Join(packDir, "image")
	if err := os.MkdirAll(pipeDir, 0o755); err != nil {
		return NavPack{}, err
	}

	allNodes := map[string]map[string]any{}
	imageCount := 0

	if extName == ".json" {
		nodes, err := parsePipelineNodes(data)
		if err != nil {
			_ = os.RemoveAll(packDir)
			return NavPack{}, fmt.Errorf("JSON 格式不合法（需为 MAA pipeline 节点对象）：%w", err)
		}
		if err := os.WriteFile(filepath.Join(pipeDir, id+".json"), data, 0o644); err != nil {
			_ = os.RemoveAll(packDir)
			return NavPack{}, err
		}
		for k, v := range nodes {
			allNodes[k] = v
		}
	} else {
		zr, err := zip.NewReader(bytes.NewReader(data), int64(len(data)))
		if err != nil {
			_ = os.RemoveAll(packDir)
			return NavPack{}, fmt.Errorf("zip 文件损坏：%w", err)
		}
		for _, zf := range zr.File {
			if zf.FileInfo().IsDir() {
				continue
			}
			clean := filepath.ToSlash(filepath.Clean(zf.Name))
			if strings.HasPrefix(clean, "../") || clean == ".." || strings.HasPrefix(clean, "/") {
				_ = os.RemoveAll(packDir)
				return NavPack{}, fmt.Errorf("zip 包含非法路径条目：%q", zf.Name)
			}
			rc, err := zf.Open()
			if err != nil {
				_ = os.RemoveAll(packDir)
				return NavPack{}, err
			}
			body, err := io.ReadAll(io.LimitReader(rc, maxNavUploadBytes))
			rc.Close()
			if err != nil {
				_ = os.RemoveAll(packDir)
				return NavPack{}, err
			}
			low := strings.ToLower(clean)
			switch {
			case strings.HasSuffix(low, ".json") && !strings.HasSuffix(low, "interface.json"):
				nodes, perr := parsePipelineNodes(body)
				if perr != nil {
					continue
				}
				dst := filepath.Join(pipeDir, filepath.Base(clean))
				if err := os.WriteFile(dst, body, 0o644); err != nil {
					_ = os.RemoveAll(packDir)
					return NavPack{}, err
				}
				for k, v := range nodes {
					allNodes[k] = v
				}
			case strings.HasSuffix(low, ".png") || strings.HasSuffix(low, ".jpg") || strings.HasSuffix(low, ".jpeg"):
				relImg := clean
				if idx := strings.Index(low, "image/"); idx >= 0 {
					relImg = clean[idx+len("image/"):]
				}
				dst := filepath.Join(imgDir, filepath.FromSlash(relImg))
				if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
					_ = os.RemoveAll(packDir)
					return NavPack{}, err
				}
				if err := os.WriteFile(dst, body, 0o644); err != nil {
					_ = os.RemoveAll(packDir)
					return NavPack{}, err
				}
				imageCount++
			}
		}
	}

	if len(allNodes) == 0 {
		_ = os.RemoveAll(packDir)
		return NavPack{}, fmt.Errorf("未在导入文件中解析到任何 MAA pipeline 节点")
	}

	nodeList := make([]string, 0, len(allNodes))
	for k := range allNodes {
		nodeList = append(nodeList, k)
	}
	sort.Strings(nodeList)

	entry := wantEntry
	if entry == "" || allNodes[entry] == nil {
		entry = inferEntryNode(allNodes, nodeList)
	}

	taskName := "导入:" + name
	pack := NavPack{
		ID:        id,
		Name:      name,
		Phase:     phase,
		TaskName:  taskName,
		Entry:     entry,
		Nodes:     nodeList,
		Images:    imageCount,
		CreatedAt: time.Now().Format(time.RFC3339),
	}

	// 若 PI 项目目录存在，自动同步到 resource/imported 并注册到 interface.json
	if err := m.syncNavPackToPi(pack, packDir, piProject); err != nil {
		_ = os.RemoveAll(packDir)
		return NavPack{}, err
	}

	packs := m.ListNavPacks()
	packs = append(packs, pack)
	if err := m.saveNavPacks(packs); err != nil {
		return NavPack{}, err
	}
	return pack, nil
}

// DeleteNavPack 删除已导入的导航包并从索引中移除。
func (m *Manager) DeleteNavPack(id, piProject string) error {
	packs := m.ListNavPacks()
	kept := make([]NavPack, 0, len(packs))
	var removed *NavPack
	for i := range packs {
		if packs[i].ID == id {
			cp := packs[i]
			removed = &cp
			continue
		}
		kept = append(kept, packs[i])
	}
	if removed == nil {
		return fmt.Errorf("未找到导航包 %s", id)
	}
	_ = os.RemoveAll(filepath.Join(m.navPacksDir(), id))
	_ = m.removeNavPackFromPi(*removed, piProject)
	return m.saveNavPacks(kept)
}

func parsePipelineNodes(raw []byte) (map[string]map[string]any, error) {
	trimmed := bytes.TrimPrefix(raw, []byte{0xEF, 0xBB, 0xBF})
	var doc map[string]map[string]any
	if err := json.Unmarshal(trimmed, &doc); err != nil {
		return nil, err
	}
	return doc, nil
}

// inferEntryNode 推断最可能的入口节点（优先选没有被其他节点 next 引用的节点，否则取排序第一个）。
func inferEntryNode(nodes map[string]map[string]any, sortedNames []string) string {
	referenced := map[string]bool{}
	for _, body := range nodes {
		for _, field := range []string{"next", "on_error", "interrupt"} {
			switch v := body[field].(type) {
			case string:
				referenced[strings.TrimSpace(v)] = true
			case []any:
				for _, item := range v {
					if s, ok := item.(string); ok {
						referenced[strings.TrimSpace(s)] = true
					}
				}
			}
		}
	}
	for _, name := range sortedNames {
		if !referenced[name] {
			return name
		}
	}
	return sortedNames[0]
}

// syncNavPackToPi 将导入包的 pipeline/image 复制到 <piDir>/<project>/resource/imported，
// 并确保 interface.json 的 resource[0].path 含 resource/imported 且注册了对应 task。
func (m *Manager) syncNavPackToPi(pack NavPack, packDir, piProject string) error {
	if piProject == "" {
		piProject = defaultPhasePiProject
	}
	projDir := filepath.Join(m.cfg.PiDirPath(), piProject)
	ifacePath := filepath.Join(projDir, "interface.json")
	raw, err := os.ReadFile(ifacePath)
	if err != nil {
		// 本地没有 PI 项目目录时仅保留在 nav_packs，不报错阻断导入
		return nil
	}

	importedRoot := filepath.Join(projDir, "resource", "imported")
	importedPipe := filepath.Join(importedRoot, "pipeline")
	importedImg := filepath.Join(importedRoot, "image")
	if err := os.MkdirAll(importedPipe, 0o755); err != nil {
		return err
	}
	// 复制 pipeline/*.json（加 pack.ID 前缀避免覆盖同名文件）
	if entries, err := os.ReadDir(filepath.Join(packDir, "pipeline")); err == nil {
		for _, e := range entries {
			if e.IsDir() {
				continue
			}
			b, err := os.ReadFile(filepath.Join(packDir, "pipeline", e.Name()))
			if err != nil {
				return err
			}
			dstName := pack.ID + "_" + e.Name()
			if err := os.WriteFile(filepath.Join(importedPipe, dstName), b, 0o644); err != nil {
				return err
			}
		}
	}
	// 复制 image/**/*
	srcImgRoot := filepath.Join(packDir, "image")
	if st, err := os.Stat(srcImgRoot); err == nil && st.IsDir() {
		_ = filepath.Walk(srcImgRoot, func(p string, info os.FileInfo, werr error) error {
			if werr != nil || info.IsDir() {
				return nil
			}
			rel, _ := filepath.Rel(srcImgRoot, p)
			dst := filepath.Join(importedImg, rel)
			_ = os.MkdirAll(filepath.Dir(dst), 0o755)
			if b, err := os.ReadFile(p); err == nil {
				_ = os.WriteFile(dst, b, 0o644)
			}
			return nil
		})
	}

	// 更新 interface.json（保留原有全部字段）
	var iface map[string]any
	if err := json.Unmarshal(raw, &iface); err != nil {
		return fmt.Errorf("解析 PI interface.json 失败：%w", err)
	}
	if resources, ok := iface["resource"].([]any); ok && len(resources) > 0 {
		if r0, ok := resources[0].(map[string]any); ok {
			paths, _ := r0["path"].([]any)
			hasImported := false
			for _, p := range paths {
				if s, ok := p.(string); ok && s == "resource/imported" {
					hasImported = true
					break
				}
			}
			if !hasImported {
				r0["path"] = append(paths, "resource/imported")
			}
		}
	}
	tasks, _ := iface["task"].([]any)
	updated := false
	newTask := map[string]any{
		"name":        pack.TaskName,
		"label":       pack.TaskName,
		"entry":       pack.Entry,
		"description": "通过 Web GUI 导入的可视化导航文件（" + pack.ID + "）",
	}
	for i, t := range tasks {
		if tm, ok := t.(map[string]any); ok && tm["name"] == pack.TaskName {
			tasks[i] = newTask
			updated = true
			break
		}
	}
	if !updated {
		tasks = append(tasks, newTask)
	}
	iface["task"] = tasks

	out, err := json.MarshalIndent(iface, "", "  ")
	if err != nil {
		return err
	}
	tmp := ifacePath + ".tmp"
	if err := os.WriteFile(tmp, append(out, '\n'), 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, ifacePath)
}

func (m *Manager) removeNavPackFromPi(pack NavPack, piProject string) error {
	if piProject == "" {
		piProject = defaultPhasePiProject
	}
	projDir := filepath.Join(m.cfg.PiDirPath(), piProject)
	importedPipe := filepath.Join(projDir, "resource", "imported", "pipeline")
	if entries, err := os.ReadDir(importedPipe); err == nil {
		for _, e := range entries {
			if strings.HasPrefix(e.Name(), pack.ID+"_") {
				_ = os.Remove(filepath.Join(importedPipe, e.Name()))
			}
		}
	}
	ifacePath := filepath.Join(projDir, "interface.json")
	raw, err := os.ReadFile(ifacePath)
	if err != nil {
		return nil
	}
	var iface map[string]any
	if json.Unmarshal(raw, &iface) != nil {
		return nil
	}
	tasks, _ := iface["task"].([]any)
	kept := make([]any, 0, len(tasks))
	for _, t := range tasks {
		if tm, ok := t.(map[string]any); ok && tm["name"] == pack.TaskName {
			continue
		}
		kept = append(kept, t)
	}
	iface["task"] = kept
	out, err := json.MarshalIndent(iface, "", "  ")
	if err != nil {
		return err
	}
	tmp := ifacePath + ".tmp"
	if err := os.WriteFile(tmp, append(out, '\n'), 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, ifacePath)
}
