package copilotpick

// HTTP 层：只做参数解析、错误包装与事件流，业务全部在 Manager/ext 里。
//
// 接口清单见 docs/COPILOT-PICKER-CONTRACT.md。服务只监听 127.0.0.1，
// 远程用 SSH 隧道访问（本机浏览器 → ssh -L → 机端服务）。

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/xiachk083-hub/maa-cli-fleet/internal/copilotpick/ext"
	"github.com/xiachk083-hub/maa-cli-fleet/internal/copilotpick/model"
	"github.com/xiachk083-hub/maa-cli-fleet/web"
)

// maxBodyBytes 限制请求体大小（名册粘贴文本最多几百 KB）。
const maxBodyBytes = 8 << 20

// NewHandler 组装全部路由（含前端静态资源）。
func NewHandler(m *Manager) http.Handler {
	h := &handler{m: m}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /api/health", h.health)
	mux.HandleFunc("GET /api/pi/projects", h.piProjects)
	mux.HandleFunc("POST /api/pi/enqueue", h.piEnqueue)
	mux.HandleFunc("GET /api/nav/packs", h.navPacksList)
	mux.HandleFunc("POST /api/nav/import", h.navPackImport)
	mux.HandleFunc("POST /api/nav/run", h.navPackRun)
	mux.HandleFunc("DELETE /api/nav/packs/{id}", h.navPackDelete)
	mux.HandleFunc("GET /api/accounts", h.accounts)
	mux.HandleFunc("POST /api/box", h.box)
	mux.HandleFunc("POST /api/box/maa", h.boxFromMaa)
	mux.HandleFunc("POST /api/box/maa/scan", h.boxScan)
	mux.HandleFunc("GET /api/log/maa", h.maaLog)
	mux.HandleFunc("POST /api/stages/resolve", h.resolveStages)
	mux.HandleFunc("GET /api/stages/catalog", h.stageCatalog)
	mux.HandleFunc("POST /api/jobs/search", h.searchJobs)
	mux.HandleFunc("POST /api/run/start", h.startRun)
	mux.HandleFunc("POST /api/auto-event/start", h.startAutoEvent)
	mux.HandleFunc("GET /api/spa/status", h.spaStatus)
	mux.HandleFunc("POST /api/spa/start", h.spaStart)
	mux.HandleFunc("POST /api/spa/stop", h.spaStop)
	mux.HandleFunc("GET /api/spa/log/{emu}", h.spaLog)
	mux.HandleFunc("GET /api/run/active", h.activeRun)
	mux.HandleFunc("GET /api/runs/active", h.activeRuns)
	mux.HandleFunc("GET /api/run/{id}", h.getRun)
	mux.HandleFunc("GET /api/run/{id}/events", h.runEvents)
	mux.HandleFunc("POST /api/run/{id}/stop", h.stopRun)
	mux.HandleFunc("POST /api/run/stop", h.stopActiveRun)
	mux.HandleFunc("POST /api/maa/kill", h.killMaa)
	mux.HandleFunc("POST /api/nav/home", h.navHome)
	mux.HandleFunc("POST /api/nav/restart", h.navRestart)
	mux.HandleFunc("GET /api/progress", h.progressList)
	mux.HandleFunc("GET /api/job-scores", h.jobScoresList)
	mux.HandleFunc("GET /api/unavailable", h.unavailable)
	mux.HandleFunc("POST /api/unavailable/clear", h.clearUnavailable)
	mux.HandleFunc("GET /api/ui-state", h.getUIState)
	mux.HandleFunc("PUT /api/ui-state", h.putUIState)
	mux.HandleFunc("GET /api/run/{id}/shot", h.runShot)
	mux.HandleFunc("POST /api/setup", h.setup)
	mux.HandleFunc("POST /api/emu/start", h.emuStart)
	mux.HandleFunc("POST /api/emu/stop", h.emuStop)
	mux.Handle("/", spaHandler(web.Dist()))
	return mux
}

type handler struct{ m *Manager }

// ---- 基础接口 ----

func (h *handler) health(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, h.m.Health())
}

func (h *handler) accounts(w http.ResponseWriter, r *http.Request) {
	accounts, err := h.m.Accounts(r.Context())
	ready, reason := h.m.cfg.MaaReady()
	if err != nil {
		// 账号表读不到也要能开页面：返回空列表 + 错误说明
		writeJSON(w, http.StatusOK, map[string]any{
			"accounts": []model.Account{}, "maaReady": ready, "maaError": reason + "；" + err.Error(),
		})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"accounts": accounts, "maaReady": ready, "maaError": reason,
	})
}

func (h *handler) setup(w http.ResponseWriter, r *http.Request) {
	ok, err := h.m.Setup(r.Context())
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "初始化 MAA 运行目录失败："+err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "maaReady": ok})
}

// ---- 名册 ----

func (h *handler) box(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Text string `json:"text"`
	}
	if err := decodeBody(w, r, &req); err != nil {
		writeErr(w, http.StatusBadRequest, err.Error())
		return
	}
	roster, err := ext.ParseRoster(req.Text)
	if err != nil {
		writeErr(w, http.StatusBadRequest, "解析名册失败："+err.Error())
		return
	}
	h.m.SetRoster(roster)
	writeJSON(w, http.StatusOK, roster)
}

// boxFromMaa 直接读 MAA 小工具写的 data/OperBoxData.json（不用手工粘贴）。
//
// 请求体 {"dir": "E:\\MAA-Multi\\MAA_1", "accountId": "l7"}：
//   - dir 显式给了就用它；
//   - 否则按 accountId 的 emu 找同级的 MAA_<emu>（机端/ERAZER 的 MAA-Multi 布局）；
//   - 再不行就用配置里的 maaCoreSource。
//
// 注意：这个文件由 MAA GUI 的「干员识别」/「更新数据」任务落盘，maa-cli 的
// OperBox 任务只发回调、不写文件，所以扫描动作得在 MAA 侧做一次。
func (h *handler) boxFromMaa(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Dir       string `json:"dir"`
		AccountID string `json:"accountId"`
	}
	if err := decodeBody(w, r, &req); err != nil {
		writeErr(w, http.StatusBadRequest, err.Error())
		return
	}
	dir := strings.TrimSpace(req.Dir)
	if dir == "" && strings.TrimSpace(req.AccountID) != "" {
		if accounts, err := h.m.Accounts(r.Context()); err == nil {
			if acc, ok := findAccount(accounts, req.AccountID); ok {
				dir = h.accountMaaDir(acc.Emu)
			}
		}
	}
	if dir == "" {
		dir = h.m.cfg.MaaCoreSource
	}
	if dir == "" {
		writeErr(w, http.StatusBadRequest, "没有指定 MAA 目录，且配置里也没有 maaCoreSource")
		return
	}
	// 优先读本运行根「扫描干员」的落盘（data_copilot/maa_data/scan_OperBoxData.json）：
	// 双开时外部 MAA 目录的 data/OperBoxData.json 不属于这个实例的账号（可能是别的号），
	// 直接读会把别的号的名册导进来。只有本运行根没扫过时才退回外部 MAA 目录。
	path := filepath.Join(h.m.cfg.MaaDataDir, "scan_OperBoxData.json")
	roster, err := ext.LoadOperBox(path)
	if err != nil {
		extPath := filepath.Join(dir, "data", "OperBoxData.json")
		if r2, e2 := ext.LoadOperBox(extPath); e2 == nil {
			roster, err, path = r2, nil, extPath
		} else {
			writeErr(w, http.StatusBadRequest,
				"导入 MAA 练度失败："+err.Error()+"\n本运行根路径："+path+"\n外部 MAA 路径："+extPath+
					"\n先在「扫描干员」里扫一次（或对应 MAA 小工具跑「干员识别」）再读。")
			return
		}
	}
	h.m.SetRoster(roster)
	writeJSON(w, http.StatusOK, roster)
}

// accountMaaDir 按 emu 找同级的 MAA_<emu> 目录（MAA-Multi 布局），找不到回退 maaCoreSource。
func (h *handler) accountMaaDir(emu string) string {
	base := strings.TrimSpace(h.m.cfg.MaaCoreSource)
	emu = strings.TrimSpace(emu)
	if base == "" || emu == "" {
		return base
	}
	cand := filepath.Join(filepath.Dir(base), "MAA_"+emu)
	if st, err := os.Stat(cand); err == nil && st.IsDir() {
		return cand
	}
	return base
}

// maaLog 返回 MAA asst.log 的尾巴（纯文本），给页面里的「MAA 日志」弹层用。
func (h *handler) maaLog(w http.ResponseWriter, r *http.Request) {
	path := h.m.cfg.AsstLogPath()
	f, err := os.Open(path)
	if err != nil {
		writeErr(w, http.StatusNotFound, "MAA 日志还没生成："+path)
		return
	}
	defer f.Close()
	st, err := f.Stat()
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	const maxTail int64 = 256 << 10
	size := st.Size()
	if size > maxTail {
		if _, err := f.Seek(size-maxTail, io.SeekStart); err != nil {
			writeErr(w, http.StatusInternalServerError, err.Error())
			return
		}
	}
	b, err := io.ReadAll(io.LimitReader(f, maxTail))
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	_, _ = w.Write(b)
}

// boxScan 用 maa-cli 跑一次 MAA 的 OperBox 任务（干员识别），从日志抓回调结果。
//
// 请求体 {"accountId":"l7"}；返回 {"roster":model.Roster,"path":"..."}。
func (h *handler) boxScan(w http.ResponseWriter, r *http.Request) {
	var req struct {
		AccountID string `json:"accountId"`
	}
	if err := decodeBody(w, r, &req); err != nil {
		writeErr(w, http.StatusBadRequest, err.Error())
		return
	}
	if strings.TrimSpace(req.AccountID) == "" {
		writeErr(w, http.StatusBadRequest, "缺少账号（accountId）")
		return
	}
	roster, path, err := h.m.ScanOperBox(r.Context(), req.AccountID)
	if err != nil {
		writeErr(w, http.StatusBadRequest, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"roster": roster, "path": path})
}

// ---- 关卡与作业 ----

func (h *handler) resolveStages(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Query string `json:"query"`
	}
	if err := decodeBody(w, r, &req); err != nil {
		writeErr(w, http.StatusBadRequest, err.Error())
		return
	}
	stages, err := h.m.ResolveStages(r.Context(), req.Query)
	if err != nil {
		writeErr(w, http.StatusBadRequest, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"stages": stages})
}

func (h *handler) searchJobs(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Query      string             `json:"query"`
		StageCode  string             `json:"stageCode"`
		StageName  string             `json:"stageName"`
		Roster     []model.RosterEntry `json:"roster"`
		RosterText string             `json:"rosterText"`
		Limit      int                `json:"limit"`
		Mode       string             `json:"mode"`
	}
	if err := decodeBody(w, r, &req); err != nil {
		writeErr(w, http.StatusBadRequest, err.Error())
		return
	}
	roster := h.m.Roster()
	switch {
	case strings.TrimSpace(req.RosterText) != "":
		parsed, err := ext.ParseRoster(req.RosterText)
		if err != nil {
			writeErr(w, http.StatusBadRequest, "解析名册失败："+err.Error())
			return
		}
		roster = parsed
		h.m.SetRoster(roster)
	case len(req.Roster) > 0:
		roster = rosterFromEntries(req.Roster)
		h.m.SetRoster(roster)
	}
	res, err := h.m.SearchJobs(r.Context(), SearchRequest{
		Query: req.Query, StageCode: req.StageCode, StageName: req.StageName,
		Roster: roster, Limit: req.Limit, Mode: req.Mode,
	})
	if err != nil {
		writeErr(w, http.StatusBadRequest, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, res)
}

// rosterFromEntries 把前端直接传的干员数组包成 Roster。
func rosterFromEntries(entries []model.RosterEntry) model.Roster {
	r := model.Roster{Entries: entries, Count: len(entries), Format: "inline"}
	for i := range r.Entries {
		// 前端不会传技能数据，除飞来的都是「未知」——按已拥有处理
		r.Entries[i].SkillKnown = len(r.Entries[i].Skills) > 0 || r.Entries[i].SkillLevel > 0
		if r.Entries[i].SkillKnown {
			r.SkillKnown = true
		}
		switch r.Entries[i].Rarity {
		case 6:
			r.Six++
		case 5:
			r.Five++
		}
	}
	return r
}

// ---- 执行 ----

func (h *handler) startRun(w http.ResponseWriter, r *http.Request) {
	var opts model.RunOptions
	if err := decodeBody(w, r, &opts); err != nil {
		writeErr(w, http.StatusBadRequest, err.Error())
		return
	}
	run, err := h.m.StartRun(opts)
	if err != nil {
		status := http.StatusBadRequest
		if errors.Is(err, ErrBusy) {
			status = http.StatusConflict
		}
		writeErr(w, status, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"runId": run.ID})
}

func (h *handler) getRun(w http.ResponseWriter, r *http.Request) {
	run, ok := h.m.GetRun(r.PathValue("id"))
	if !ok {
		writeErr(w, http.StatusNotFound, "没有这个执行记录")
		return
	}
	writeJSON(w, http.StatusOK, run.Snapshot(true))
}

func (h *handler) stopRun(w http.ResponseWriter, r *http.Request) {
	run, ok := h.m.GetRun(r.PathValue("id"))
	if !ok {
		writeErr(w, http.StatusNotFound, "没有这个执行记录")
		return
	}
	run.Stop()
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}

// activeRun 返回还没结束的 run；页面刷新/重开客户端后用它恢复「停止」按钮。
func (h *handler) activeRun(w http.ResponseWriter, r *http.Request) {
	snap := h.m.ActiveRunSnapshot()
	if snap == nil {
		writeJSON(w, http.StatusOK, map[string]any{"run": nil})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"run": snap})
}

// stopActiveRun 停掉当前正在跑的 run（不依赖页面是否还记着 runId）。
func (h *handler) stopActiveRun(w http.ResponseWriter, r *http.Request) {
	stopped := false
	if run := h.m.CurrentRun(); run != nil && !run.IsDone() {
		run.Stop()
		stopped = true
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "stopped": stopped})
}

// killMaa 停掉当前 run，并清掉本服务启动的残留 maa-cli 进程。
func (h *handler) killMaa(w http.ResponseWriter, r *http.Request) {
	if run := h.m.CurrentRun(); run != nil && !run.IsDone() {
		run.Stop()
	}
	pids, err := h.m.KillStrayMaa(r.Context())
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "pids": pids})
}

// runEvents 推 SSE：`data: <RunEvent>`，结束时发 `event: done`。
func (h *handler) runEvents(w http.ResponseWriter, r *http.Request) {
	run, ok := h.m.GetRun(r.PathValue("id"))
	if !ok {
		writeErr(w, http.StatusNotFound, "没有这个执行记录")
		return
	}
	flusher, ok := w.(http.Flusher)
	if !ok {
		writeErr(w, http.StatusInternalServerError, "当前连接不支持流式输出")
		return
	}
	w.Header().Set("Content-Type", "text/event-stream; charset=utf-8")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")
	w.Header().Set("X-Accel-Buffering", "no")
	w.WriteHeader(http.StatusOK)
	flusher.Flush()

	ch, cancel := run.Subscribe()
	defer cancel()

	// 已经结束的执行：Subscribe 不会再收事件，直接补一个 done 让前端收尾
	if run.IsDone() {
		fmt.Fprint(w, "event: done\ndata: {}\n\n")
		flusher.Flush()
		return
	}

	heartbeat := time.NewTicker(15 * time.Second)
	defer heartbeat.Stop()
	for {
		select {
		case <-r.Context().Done():
			return
		case ev, open := <-ch:
			if !open {
				fmt.Fprint(w, "event: done\ndata: {}\n\n")
				flusher.Flush()
				return
			}
			b, err := json.Marshal(ev)
			if err != nil {
				continue
			}
			fmt.Fprintf(w, "data: %s\n\n", b)
			flusher.Flush()
		case <-heartbeat.C:
			fmt.Fprint(w, ": ping\n\n")
			flusher.Flush()
		}
	}
}

// runShot 返回执行中最新的一帧截图（前端 2s 轮询，靠 ts 破缓存）。
func (h *handler) runShot(w http.ResponseWriter, r *http.Request) {
	run, ok := h.m.GetRun(r.PathValue("id"))
	if !ok {
		writeErr(w, http.StatusNotFound, "没有这个执行记录")
		return
	}
	path := filepath.Join(run.Dir(), "shot.png")
	st, err := os.Stat(path)
	if err != nil || !st.Mode().IsRegular() {
		writeErr(w, http.StatusNotFound, "还没有截图")
		return
	}
	w.Header().Set("Content-Type", "image/png")
	w.Header().Set("Cache-Control", "no-store")
	http.ServeFile(w, r, path)
}

// ---- 静态资源 ----

// spaHandler 提供 web/dist；不是文件的路径回落到 index.html（前端路由）。
func spaHandler(dist fs.FS) http.Handler {
	files := http.FileServerFS(dist)
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		p := strings.TrimPrefix(r.URL.Path, "/")
		if p == "" {
			p = "index.html"
		}
		if _, err := fs.Stat(dist, p); err != nil {
			index, err := fs.ReadFile(dist, "index.html")
			if err != nil {
				http.Error(w, "前端资源缺失：web/dist 还没构建（pnpm --dir web build）", http.StatusInternalServerError)
				return
			}
			w.Header().Set("Content-Type", "text/html; charset=utf-8")
			_, _ = w.Write(index)
			return
		}
		files.ServeHTTP(w, r)
	})
}

// ---- 工具 ----

func decodeBody(w http.ResponseWriter, r *http.Request, dst any) error {
	body, err := io.ReadAll(io.LimitReader(r.Body, maxBodyBytes))
	if err != nil {
		return fmt.Errorf("读取请求体失败：%w", err)
	}
	if len(strings.TrimSpace(string(body))) == 0 {
		return nil // 允许空体（stop 之类）
	}
	if err := json.Unmarshal(body, dst); err != nil {
		return fmt.Errorf("请求体不是合法 JSON：%w", err)
	}
	return nil
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeErr(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"error": msg})
}

// ---- 编不进队黑名单 ----

// unavailable 返回「有名册但编不进队」的 (干员, 技能序号) 列表。
//
// 来源：执行作业时 MAA 编队报 `"reason":"Unavailable"`，服务端自动记录并落盘
// （<root>/unavailable.json）。作业要这些干员时执行器会直接跳过。
func (h *handler) unavailable(w http.ResponseWriter, r *http.Request) {
	list := h.m.BlockedOps()
	sort.Slice(list, func(i, j int) bool {
		if list[i].Name != list[j].Name {
			return list[i].Name < list[j].Name
		}
		return list[i].Skill < list[j].Skill
	})
	writeJSON(w, http.StatusOK, map[string]any{"ops": list, "count": len(list)})
}

// clearUnavailable 清空黑名单（换号/补练度之后用）。
func (h *handler) clearUnavailable(w http.ResponseWriter, r *http.Request) {
	h.m.clearBlocked()
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}

// ---- 关卡完成度台账 ----

// progressList 返回「哪关哪个难度几星」的台账（重启保留，落盘 <root>/progress.json）。
func (h *handler) progressList(w http.ResponseWriter, r *http.Request) {
	list := h.m.Progress()
	writeJSON(w, http.StatusOK, map[string]any{"stages": list, "count": len(list)})
}

// jobScoresList 返回跨号共享作业评分库（job_scores.json）。
func (h *handler) jobScoresList(w http.ResponseWriter, r *http.Request) {
	list := h.m.JobScores()
	writeJSON(w, http.StatusOK, map[string]any{
		"scores": list,
		"count":  len(list),
		"path":   h.m.cfg.JobScoresPath(),
	})
}

// ---- 导航 ----

// navHome 用 MAA 的 startup 把游戏带回主界面。
//
// 驱动脚本要导航进活动时，先调这个把状态复位到「确定的主界面」，再按坐标逐跳点击——
// 比在任意页面上盲点返回键可靠（小游戏/弹层里根本退不出来）。
func (h *handler) navHome(w http.ResponseWriter, r *http.Request) {
	var req struct {
		AccountID string `json:"accountId"`
	}
	if err := decodeBody(w, r, &req); err != nil {
		writeErr(w, http.StatusBadRequest, err.Error())
		return
	}
	if strings.TrimSpace(req.AccountID) == "" {
		req.AccountID = h.m.defaultAccountID(r.Context())
	}
	tail, err := h.m.ReturnHome(r.Context(), req.AccountID)
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "tail": tail})
}

// navRestart 关游戏再拉起（脱离小游戏/弹层最快），之后接 navHome 回主界面。
func (h *handler) navRestart(w http.ResponseWriter, r *http.Request) {
	var req struct {
		AccountID string `json:"accountId"`
	}
	if err := decodeBody(w, r, &req); err != nil {
		writeErr(w, http.StatusBadRequest, err.Error())
		return
	}
	if strings.TrimSpace(req.AccountID) == "" {
		req.AccountID = h.m.defaultAccountID(r.Context())
	}
	msg, err := h.m.RestartGame(r.Context(), req.AccountID)
	if err != nil {
		writeErr(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "detail": msg})
}

func (h *handler) piProjects(w http.ResponseWriter, r *http.Request) {
	type proj struct {
		Name     string   `json:"name"`
		Resource string   `json:"resource,omitempty"`
		Entries  []string `json:"entries"`
	}
	piDir := h.m.cfg.PiDirPath()
	out := make([]proj, 0)
	entries, err := os.ReadDir(piDir)
	if err != nil {
		writeJSON(w, http.StatusOK, out)
		return
	}
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		b, err := os.ReadFile(filepath.Join(piDir, e.Name(), "interface.json"))
		if err != nil {
			continue
		}
		var iface struct {
			Resource []struct {
				Name string `json:"name"`
			} `json:"resource"`
			Task []struct {
				Name string `json:"name"`
			} `json:"task"`
		}
		if json.Unmarshal(b, &iface) != nil {
			continue
		}
		res := ""
		if len(iface.Resource) > 0 {
			res = iface.Resource[0].Name
		}
		tasks := make([]string, 0, len(iface.Task))
		for _, t := range iface.Task {
			if t.Name != "" {
				tasks = append(tasks, t.Name)
			}
		}
		out = append(out, proj{Name: e.Name(), Resource: res, Entries: tasks})
	}
	writeJSON(w, http.StatusOK, out)
}

func (h *handler) piEnqueue(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Project string `json:"project"`
		Entry   string `json:"entry"`
		Account string `json:"account"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"error": "invalid json payload"})
		return
	}
	req.Project = strings.TrimSpace(req.Project)
	req.Entry = strings.TrimSpace(req.Entry)
	req.Account = strings.TrimSpace(req.Account)
	if req.Project == "" || req.Entry == "" || req.Account == "" {
		writeJSON(w, http.StatusBadRequest, map[string]any{"error": "missing project/entry/account"})
		return
	}
	if strings.ContainsAny(req.Project, ".:/\\\r\n") || strings.ContainsAny(req.Account, ":\r\n") || strings.ContainsAny(req.Entry, "\r\n") {
		writeJSON(w, http.StatusBadRequest, map[string]any{"error": "invalid characters in project/entry/account"})
		return
	}
	q := h.m.cfg.QueueInPath()
	if dir := filepath.Dir(q); dir != "" && dir != "." {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			writeJSON(w, http.StatusInternalServerError, map[string]any{"error": err.Error()})
			return
		}
	}
	kind := "pi." + req.Project + "." + req.Entry
	payload, err := json.Marshal(map[string]string{
		"op":      "enqueue",
		"kind":    kind,
		"account": req.Account,
	})
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]any{"error": err.Error()})
		return
	}
	f, err := os.OpenFile(q, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]any{"error": err.Error()})
		return
	}
	defer f.Close()
	if _, err := f.Write(append(payload, '\n')); err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]any{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "kind": kind, "account": req.Account, "queueFile": q})
}
