// Package center —— 机队后端（中心服务）。
//
// 职责（2026-10-02 收敛后）：台账（节点/机台）+ 事件 + 对外只读 API。
// **机端只上报，没有指令通道**——旧 /poll /command /result（遥控 ops 命令）已删：
// 自建成以来 0 使用（events.jsonl 无 command 记录），机端收敛为"管理环境+执行任务+上报"后无消费者。
// 将来若要远程下单，正路是"中心存任务队列、runner 轮询拉队列"（面向任务，不是面向 ops 命令）。
//
//	POST /register  {node:{id,ver,host,machines:[...]}}      机端上线登记
//	POST /report    {node_id,kind:"state|event",data}       状态/事件上报
//	GET  /fleet     聚合视图（节点/机台/最近事件）
//	GET  /health    存活（无鉴权）
package center

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/xiachk083-hub/maa-cli-fleet/internal/model"
)

// Server 是中心服务实例。
type Server struct {
	mu       sync.Mutex
	stateDir string
	token    string
	nodes    map[string]*model.Node
}

// New 创建中心服务；stateDir 不存在时自动建，token 缺失时自动生成。
func New(stateDir, token string) (*Server, error) {
	if err := os.MkdirAll(stateDir, 0o755); err != nil {
		return nil, err
	}
	s := &Server{
		stateDir: stateDir,
		token:    token,
		nodes:    map[string]*model.Node{},
	}
	if s.token == "" {
		if b, err := model.ReadFileBOM(filepath.Join(stateDir, "token.txt")); err == nil {
			s.token = strings.TrimSpace(string(b))
		}
	}
	if s.token == "" {
		s.token = randomToken()
		_ = os.WriteFile(filepath.Join(stateDir, "token.txt"), []byte(s.token), 0o600)
	}
	s.load()
	return s, nil
}

// Token 返回当前共享密钥（部署脚本会读它）。
func (s *Server) Token() string { return s.token }

type registryFile struct {
	Nodes map[string]*model.Node `json:"nodes"`
}

func (s *Server) load() {
	b, err := model.ReadFileBOM(filepath.Join(s.stateDir, "registry.json"))
	if err != nil {
		return
	}
	var r registryFile
	if json.Unmarshal(b, &r) == nil && r.Nodes != nil {
		s.nodes = r.Nodes
	}
}

// save 落盘（调用方需持锁）。
func (s *Server) saveLocked() {
	b, _ := json.MarshalIndent(registryFile{Nodes: s.nodes}, "", "  ")
	tmp := filepath.Join(s.stateDir, "registry.tmp")
	if os.WriteFile(tmp, b, 0o644) == nil {
		_ = os.Rename(tmp, filepath.Join(s.stateDir, "registry.json"))
	}
}

func (s *Server) event(rec map[string]any) {
	rec["ts"] = now()
	b, _ := json.Marshal(rec)
	f, err := os.OpenFile(filepath.Join(s.stateDir, "events.jsonl"), os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		return
	}
	defer f.Close()
	_, _ = f.Write(append(b, '\n'))
}

func now() string { return time.Now().Format("2006-01-02 15:04:05") }

func randomToken() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

// ---- HTTP ------------------------------------------------------------------

// Handler 返回中心的 http.Handler。
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/health", s.handleHealth)
	mux.HandleFunc("/fleet", s.auth(s.handleFleet))
	mux.HandleFunc("/register", s.auth(s.handleRegister))
	mux.HandleFunc("/report", s.auth(s.handleReport))
	return mux
}

// ListenAndServe 启动服务。
func (s *Server) ListenAndServe(addr string) error {
	log.Printf("[center] listening on %s（state=%s）", addr, s.stateDir)
	log.Printf("[center] token 文件：%s", filepath.Join(s.stateDir, "token.txt"))
	return http.ListenAndServe(addr, s.Handler())
}

func (s *Server) auth(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-Fleet-Token") != s.token {
			writeJSON(w, 401, map[string]any{"ok": false, "error": "bad_token"})
			return
		}
		next(w, r)
	}
}

func (s *Server) handleHealth(w http.ResponseWriter, _ *http.Request) {
	s.mu.Lock()
	total, online := len(s.nodes), 0
	for _, n := range s.nodes {
		if onlineNow(n) {
			online++
		}
	}
	s.mu.Unlock()
	writeJSON(w, 200, map[string]any{"ok": true, "ts": now(), "nodes": total, "online": online, "impl": "go"})
}

func (s *Server) handleFleet(w http.ResponseWriter, _ *http.Request) {
	s.mu.Lock()
	defer s.mu.Unlock()
	// online 要按"现在"实时判：存储里的 Online 是写入时刻的快照，机端停掉后会永远停在 true
	nodes := make(map[string]*model.Node, len(s.nodes))
	for id, n := range s.nodes {
		cp := *n
		cp.Online = onlineNow(n)
		nodes[id] = &cp
	}
	writeJSON(w, 200, map[string]any{
		"ok": true, "ts": now(), "nodes": nodes,
		"events": tailEvents(s.stateDir, 50),
	})
}

// onlineNow：最近上报新鲜度（机端上报周期 60s，90s 给一拍余量）。
func onlineNow(n *model.Node) bool {
	ts, err := time.ParseInLocation("2006-01-02 15:04:05", n.LastSeen, time.Local)
	if err != nil {
		return false
	}
	return time.Since(ts) < 90*time.Second
}

func (s *Server) handleRegister(w http.ResponseWriter, r *http.Request) {
	body, err := readBody(r)
	if err != nil {
		writeJSON(w, 400, map[string]any{"ok": false, "error": "bad_body"})
		return
	}
	var req struct {
		Node *model.Node `json:"node"`
	}
	if err := json.Unmarshal(body, &req); err != nil || req.Node == nil || req.Node.ID == "" {
		writeJSON(w, 400, map[string]any{"ok": false, "error": "node.id_required"})
		return
	}
	s.mu.Lock()
	prev := s.nodes[req.Node.ID]
	if prev != nil { // 保留已有状态，合并新信息
		req.Node.State = prev.State
		if req.Node.Host == "" {
			req.Node.Host = prev.Host
		}
	}
	req.Node.LastSeen = now()
	req.Node.Online = true
	s.nodes[req.Node.ID] = req.Node
	s.saveLocked()
	s.mu.Unlock()
	s.event(map[string]any{"kind": "register", "node_id": req.Node.ID, "ver": req.Node.Ver, "host": req.Node.Host})
	writeJSON(w, 200, map[string]any{"ok": true, "ts": now()})
}

func (s *Server) handleReport(w http.ResponseWriter, r *http.Request) {
	body, err := readBody(r)
	if err != nil {
		writeJSON(w, 400, map[string]any{"ok": false, "error": "bad_body"})
		return
	}
	var req struct {
		NodeID string          `json:"node_id"`
		Kind   string          `json:"kind"`
		Data   json.RawMessage `json:"data"`
	}
	if err := json.Unmarshal(body, &req); err != nil || req.NodeID == "" {
		writeJSON(w, 400, map[string]any{"ok": false, "error": "node_id_required"})
		return
	}
	s.mu.Lock()
	n, ok := s.nodes[req.NodeID]
	if !ok {
		n = &model.Node{ID: req.NodeID}
		s.nodes[req.NodeID] = n
	}
	n.LastSeen = now()
	n.Online = true
	switch req.Kind {
	case "state":
		_ = json.Unmarshal(req.Data, &n.State)
	case "heartbeat":
		n.Heartbeat = &model.Heartbeat{TS: now(), Data: req.Data}
	}
	s.saveLocked()
	s.mu.Unlock()

	ev := map[string]any{"kind": req.Kind, "node_id": req.NodeID}
	if req.Kind == "event" {
		var d any
		_ = json.Unmarshal(req.Data, &d)
		ev["data"] = d
	}
	s.event(ev)
	writeJSON(w, 200, map[string]any{"ok": true})
}

// ---- 工具 ------------------------------------------------------------------

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(code)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}

func readBody(r *http.Request) ([]byte, error) {
	defer r.Body.Close()
	buf := make([]byte, 0, 4096)
	tmp := make([]byte, 4096)
	for {
		n, err := r.Body.Read(tmp)
		buf = append(buf, tmp[:n]...)
		if err != nil {
			break
		}
	}
	if len(buf) == 0 {
		return nil, fmt.Errorf("empty")
	}
	return buf, nil
}

func tailEvents(stateDir string, n int) []map[string]any {
	b, err := os.ReadFile(filepath.Join(stateDir, "events.jsonl"))
	if err != nil {
		return nil
	}
	lines := strings.Split(strings.TrimRight(string(b), "\n"), "\n")
	if len(lines) > n {
		lines = lines[len(lines)-n:]
	}
	out := make([]map[string]any, 0, len(lines))
	for _, ln := range lines {
		if strings.TrimSpace(ln) == "" {
			continue
		}
		var m map[string]any
		if json.Unmarshal([]byte(ln), &m) == nil {
			out = append(out, m)
		}
	}
	return out
}
