// Package ext 是 copilot-picker 的外部能力层：解析 MAA 干员识别、解析关卡、
// 访问作业站（prts.maa.plus）、把作业与名册做匹配。
//
// 本文件的导出签名是 Lead 与 core 队友冻结的接口；实现由 core 补充/替换，
// HTTP 层（internal/copilotpick/server.go）只通过这里调用。
package ext

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/xiachk083-hub/maa-cli-fleet/internal/copilotpick/model"
)

const (
	// PrtsBaseURL 是作业站 API 根地址。
	PrtsBaseURL = "https://prts.maa.plus"
	// prtsTimeout 是单次 HTTP 请求的超时时间。
	prtsTimeout = 30 * time.Second
	// prtsMaxBody 限制响应体大小，避免异常响应耗尽内存。
	prtsMaxBody int64 = 16 << 20
)

// PrtsClient 访问 prts.maa.plus 的只读 API。
type PrtsClient struct {
	BaseURL   string
	HTTP      *http.Client
	UserAgent string
}

// NewPrtsClient 创建默认客户端。
func NewPrtsClient(base string) *PrtsClient {
	base = strings.TrimSpace(base)
	if base == "" {
		base = PrtsBaseURL
	}
	return &PrtsClient{
		BaseURL:   strings.TrimRight(base, "/"),
		HTTP:      &http.Client{Timeout: prtsTimeout, Transport: prtsTransport()},
		UserAgent: "maa-cli-fleet/copilot-picker",
	}
}

// prtsTransport 给 prts.maa.plus 直连，不走系统 HTTP(S)_PROXY。
//
// 实测（2026-11-21）：本机开着 Clash（127.0.0.1:7890）时，Go 走代理请求
// prts 会 TLS handshake timeout，而直连 600ms 就返回。作业站是国内服务，
// 直连更可靠；其它地址仍按环境变量走代理。
func prtsTransport() *http.Transport {
	return &http.Transport{
		Proxy: func(req *http.Request) (*url.URL, error) {
			if req.URL != nil && strings.HasSuffix(req.URL.Hostname(), "maa.plus") {
				return nil, nil
			}
			return http.ProxyFromEnvironment(req)
		},
		TLSHandshakeTimeout: 15 * time.Second,
		ForceAttemptHTTP2:   true,
	}
}

// baseURL 返回去掉尾部斜杠的根地址，空值时回落到官方地址。
func (c *PrtsClient) baseURL() string {
	base := strings.TrimSpace(c.BaseURL)
	if base == "" {
		base = PrtsBaseURL
	}
	return strings.TrimRight(base, "/")
}

// httpClient 返回可用的 HTTP 客户端；零值客户端也能工作（带超时）。
func (c *PrtsClient) httpClient() *http.Client {
	if c.HTTP != nil {
		return c.HTTP
	}
	return &http.Client{Timeout: prtsTimeout}
}

// get 发起一次带 UA/超时的 GET，返回响应体；失败信息为中文。
func (c *PrtsClient) get(ctx context.Context, endpoint string) ([]byte, error) {
	var lastErr error
	// 作业站偶尔 TLS 握手超时/5xx，重试 3 次，避免一次抖动就把整轮执行判失败。
	for attempt := 0; attempt < 3; attempt++ {
		if attempt > 0 {
			select {
			case <-ctx.Done():
				return nil, ctx.Err()
			case <-time.After(time.Duration(attempt) * 700 * time.Millisecond):
			}
		}
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
		if err != nil {
			return nil, fmt.Errorf("构造作业站请求失败：%w", err)
		}
		req.Header.Set("Accept", "application/json")
		if ua := strings.TrimSpace(c.UserAgent); ua != "" {
			req.Header.Set("User-Agent", ua)
		}
		resp, err := c.httpClient().Do(req)
		if err != nil {
			lastErr = fmt.Errorf("请求作业站失败：%w", err)
			continue
		}
		body, readErr := io.ReadAll(io.LimitReader(resp.Body, prtsMaxBody))
		resp.Body.Close()
		if readErr != nil {
			lastErr = fmt.Errorf("读取作业站响应失败：%w", readErr)
			continue
		}
		if resp.StatusCode != http.StatusOK {
			lastErr = fmt.Errorf("作业站返回 HTTP %d：%s", resp.StatusCode, snippet(string(body), 200))
			if resp.StatusCode == http.StatusTooManyRequests || resp.StatusCode >= 500 {
				continue
			}
			return nil, lastErr
		}
		return body, nil
	}
	return nil, lastErr
}

// snippet 取一段单行摘要用于错误信息，按 rune 截断避免切坏中文。
func snippet(s string, n int) string {
	s = strings.TrimSpace(strings.ReplaceAll(s, "\n", " "))
	if n <= 0 {
		return s
	}
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	return string(r[:n]) + "…"
}

// RawJob 是作业站 /copilot/query 返回的一条作业（content 已解析的关键字段）。
type RawJob struct {
	ID              int
	Title           string
	StageName       string
	Details         string
	MinimumRequired string
	UploadTime      string
	Difficulty      int
	Rating          int
	Views           int
	Opers           []OperSpec
	Groups          []Group
}

// OperSpec 是作业里的一个干员要求。
//
// Skill 是技能序号（1~3，作业 JSON 里在条目顶层而非 requirements 内）：
// requirements.skill_level >= 8 时表示要看「第 Skill 个技能」的专精等级。
type OperSpec struct {
	Name         string
	Skill        int
	Requirements map[string]int
}

// Group 是作业里的干员组（占位符）。
type Group struct {
	Name  string
	Opers []OperSpec
}

// QueryOptions 是 /copilot/query 的查询参数。
//
// 注意：作业站目前只认 Document（全文搜索标题/简介）；stage_name 之类的
// 参数会被服务端忽略（返回全站作业），因此过滤必须在本地做（见 SearchJobs）。
type QueryOptions struct {
	Document string
	OrderBy  string // id|views|rating_level
	Desc     bool
	Page     int
	Limit    int
}

// jobContentDoc 是作业 JSON 的 doc 段。
type jobContentDoc struct {
	Title   string `json:"title"`
	Details string `json:"details"`
}

// jobContentOper 是作业 JSON 的一个干员要求。
type jobContentOper struct {
	Name         string         `json:"name"`
	Skill        int            `json:"skill"`
	Requirements map[string]int `json:"requirements"`
}

// jobContentGroup 是作业 JSON 的一个干员组。
type jobContentGroup struct {
	Name  string           `json:"name"`
	Opers []jobContentOper `json:"opers"`
}

// jobContent 是作业站 content 字符串里我们关心的字段（裁剪版作业 JSON）。
type jobContent struct {
	StageName       string           `json:"stage_name"`
	Difficulty      int              `json:"difficulty"`
	MinimumRequired string           `json:"minimum_required"`
	Doc             jobContentDoc    `json:"doc"`
	Opers           []jobContentOper `json:"opers"`
	Groups          []jobContentGroup `json:"groups"`
}

// Query 调 /copilot/query，返回解析后的作业列表。
//
// hasNext/total 原样来自作业站响应；jobs 里 content 解析失败的条目会被跳过。
func (c *PrtsClient) Query(ctx context.Context, opt QueryOptions) (jobs []RawJob, hasNext bool, total int, err error) {
	if c == nil {
		return nil, false, 0, errors.New("作业站客户端未初始化")
	}
	if ctx == nil {
		ctx = context.Background()
	}
	page := opt.Page
	if page <= 0 {
		page = 1
	}
	limit := opt.Limit
	if limit <= 0 {
		limit = 50
	}
	orderBy := strings.TrimSpace(opt.OrderBy)
	if orderBy == "" {
		orderBy = "views"
	}
	q := url.Values{}
	q.Set("page", strconv.Itoa(page))
	q.Set("limit", strconv.Itoa(limit))
	q.Set("order_by", orderBy)
	if opt.Desc {
		q.Set("desc", "true")
	} else {
		q.Set("desc", "false")
	}
	// 只有 document 是有效的检索参数，其余 stage 参数服务端会忽略。
	if doc := strings.TrimSpace(opt.Document); doc != "" {
		q.Set("document", doc)
	}
	endpoint := c.baseURL() + "/copilot/query?" + q.Encode()

	body, err := c.get(ctx, endpoint)
	if err != nil {
		return nil, false, 0, err
	}

	var payload struct {
		Data struct {
			HasNext bool `json:"has_next"`
			Page    int  `json:"page"`
			Total   int  `json:"total"`
			Data    []struct {
				ID          int             `json:"id"`
				Views       int             `json:"views"`
				RatingLevel int             `json:"rating_level"`
				UploadTime  string          `json:"upload_time"`
				Content     json.RawMessage `json:"content"`
			} `json:"data"`
		} `json:"data"`
	}
	if err := json.Unmarshal(body, &payload); err != nil {
		return nil, false, 0, fmt.Errorf("解析作业站查询结果失败：%w", err)
	}

	out := make([]RawJob, 0, len(payload.Data.Data))
	for _, row := range payload.Data.Data {
		content, ok := parseJobContent(row.Content)
		if !ok {
			// content 坏了就跳过这一条，不影响整页。
			continue
		}
		out = append(out, RawJob{
			ID:              row.ID,
			Title:           content.Doc.Title,
			StageName:       content.StageName,
			Details:         content.Doc.Details,
			MinimumRequired: content.MinimumRequired,
			UploadTime:      row.UploadTime,
			Difficulty:      content.Difficulty,
			Rating:          row.RatingLevel,
			Views:           row.Views,
			Opers:           convertOpers(content.Opers),
			Groups:          convertGroups(content.Groups),
		})
	}
	return out, payload.Data.HasNext, payload.Data.Total, nil
}

// convertOpers 把作业 JSON 的干员列表转成 RawJob 用的结构。
func convertOpers(in []jobContentOper) []OperSpec {
	if len(in) == 0 {
		return nil
	}
	out := make([]OperSpec, 0, len(in))
	for _, op := range in {
		out = append(out, OperSpec{Name: op.Name, Skill: op.Skill, Requirements: op.Requirements})
	}
	return out
}

// convertGroups 把作业 JSON 的干员组转成 RawJob 用的结构。
func convertGroups(in []jobContentGroup) []Group {
	if len(in) == 0 {
		return nil
	}
	out := make([]Group, 0, len(in))
	for _, g := range in {
		out = append(out, Group{Name: g.Name, Opers: convertOpers(g.Opers)})
	}
	return out
}

// decodeRawContent 把 content 字段解成字符串：作业站给的是 JSON 字符串，
// 个别镜像可能直接给对象，这里两种都兼容。
func decodeRawContent(raw json.RawMessage) string {
	t := strings.TrimSpace(string(raw))
	if t == "" || t == "null" {
		return ""
	}
	if t[0] == '"' {
		var s string
		if err := json.Unmarshal(raw, &s); err != nil {
			return ""
		}
		return s
	}
	if t[0] == '{' || t[0] == '[' {
		return t
	}
	return ""
}

// parseJobContent 解析一条 content；解析失败返回 ok=false。
func parseJobContent(raw json.RawMessage) (jobContent, bool) {
	s := decodeRawContent(raw)
	if s == "" {
		return jobContent{}, false
	}
	var c jobContent
	if err := json.Unmarshal([]byte(s), &c); err != nil {
		return jobContent{}, false
	}
	return c, true
}

// FetchJobContent 调 /copilot/get/<id>，返回完整作业 JSON（字符串）。
//
// 返回的字符串是作业站给出的原始 content（可直接落盘喂 MAA），不做任何改写。
func (c *PrtsClient) FetchJobContent(ctx context.Context, id int) (string, error) {
	if c == nil {
		return "", errors.New("作业站客户端未初始化")
	}
	if id <= 0 {
		return "", fmt.Errorf("作业 ID 无效：%d", id)
	}
	if ctx == nil {
		ctx = context.Background()
	}
	endpoint := c.baseURL() + "/copilot/get/" + strconv.Itoa(id)
	body, err := c.get(ctx, endpoint)
	if err != nil {
		return "", err
	}
	var payload struct {
		StatusCode int `json:"status_code"`
		Data       struct {
			Content json.RawMessage `json:"content"`
		} `json:"data"`
	}
	if err := json.Unmarshal(body, &payload); err != nil {
		return "", fmt.Errorf("解析作业 %d 详情失败：%w", id, err)
	}
	if payload.StatusCode != 0 && payload.StatusCode != http.StatusOK {
		return "", fmt.Errorf("获取作业 %d 失败：作业站返回状态 %d", id, payload.StatusCode)
	}
	content := decodeRawContent(payload.Data.Content)
	if strings.TrimSpace(content) == "" {
		return "", fmt.Errorf("获取作业 %d 失败：作业站返回空内容", id)
	}
	return content, nil
}

// LoadLocalStages 从 MAA 资源目录（<data>/resource）读 stages.json，生成 code→stageName 的本地关卡表。
//
// 返回的切片按 Code 升序，Source 统一为 "local"。
func LoadLocalStages(maaResourceDir string) ([]model.Stage, error) {
	path := filepath.Join(maaResourceDir, "stages.json")
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("读取 MAA 关卡表失败（%s）：%w", path, err)
	}
	stages, err := ParseStagesJSON(b)
	if err != nil {
		return nil, fmt.Errorf("解析 MAA 关卡表失败（%s）：%w", path, err)
	}
	sort.Slice(stages, func(i, j int) bool { return stages[i].Code < stages[j].Code })
	return stages, nil
}

// ParseStagesJSON 供测试/离线使用：把 stages.json 的字节解析成关卡表。
func ParseStagesJSON(b []byte) ([]model.Stage, error) {
	var rows []struct {
		Code    string `json:"code"`
		StageID string `json:"stageId"`
	}
	if err := json.Unmarshal(b, &rows); err != nil {
		return nil, fmt.Errorf("解析关卡表 JSON 失败：%w", err)
	}
	out := make([]model.Stage, 0, len(rows))
	for _, r := range rows {
		if r.Code == "" || r.StageID == "" {
			continue
		}
		out = append(out, model.Stage{Code: r.Code, StageName: r.StageID, Source: "local"})
	}
	return out, nil
}

// atoi 小工具：解析失败时返回 0。
func atoi(s string) int {
	n, _ := strconv.Atoi(strings.TrimSpace(s))
	return n
}
