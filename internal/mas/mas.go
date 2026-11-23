// Package mas —— 纯 Go 版 AUTO-MAS 日常快报与状态汇总（取代 tools/mas_daily_brief.py）。
//
// 同时兼容旧版 MAS 字段（DONE / date / result / jsonFile）与新版字段（success / time / message / id）。
package mas

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"sort"
	"strings"
	"time"
)

// HistoryEntry 是 MAS /api/history/search 单条尝试记录。
type HistoryEntry struct {
	ID       string `json:"id,omitempty"`
	JSONFile string `json:"jsonFile,omitempty"`
	Date     string `json:"date,omitempty"`
	Time     string `json:"time,omitempty"`
	Status   string `json:"status,omitempty"`
	Result   string `json:"result,omitempty"`
	Message  string `json:"message,omitempty"`
}

func (e HistoryEntry) Timestamp() string {
	if e.Date != "" {
		return e.Date
	}
	return e.Time
}

func (e HistoryEntry) Reason() string {
	if e.Result != "" {
		return e.Result
	}
	return e.Message
}

// AccountSummary 是单个账号某一天的汇总。
type AccountSummary struct {
	Name       string `json:"name"`
	Bucket     string `json:"bucket"` // ok | failed | done_then_error | unknown
	Attempts   int    `json:"attempts"`
	LastTime   string `json:"lastTime,omitempty"`
	LastStatus string `json:"lastStatus,omitempty"`
	LastResult string `json:"lastResult,omitempty"`
	DoneAny    bool   `json:"doneAny"`
}

// ErrorCluster 按失败原因聚类。
type ErrorCluster struct {
	Reason   string   `json:"reason"`
	Count    int      `json:"count"`
	Accounts []string `json:"accounts"`
}

// Brief 是某游戏日的完整 MAS 日常快报。
type Brief struct {
	AsOf          string           `json:"asOf"`
	MasURL        string           `json:"masUrl"`
	GameDay       string           `json:"gameDay"`
	Counts        map[string]int   `json:"counts"`
	Accounts      []AccountSummary `json:"accounts"`
	ErrorClusters []ErrorCluster   `json:"errorClusters"`
}

// GameDay 返回按凌晨 04:00 换日的游戏日 YYYY-MM-DD。
func GameDay(now time.Time) string {
	return now.Add(-4 * time.Hour).Format("2006-01-02")
}

// Classify 将单个账号当天的尝试列表归类。
func Classify(name string, entries []HistoryEntry) AccountSummary {
	rows := append([]HistoryEntry(nil), entries...)
	sort.SliceStable(rows, func(i, j int) bool {
		return rows[i].Timestamp() < rows[j].Timestamp()
	})
	doneAny := false
	for _, e := range rows {
		if isDoneStatus(e.Status) {
			doneAny = true
		}
	}
	if len(rows) == 0 {
		return AccountSummary{Name: name, Bucket: "unknown"}
	}
	last := rows[len(rows)-1]
	lastStatus := strings.ToUpper(strings.TrimSpace(last.Status))
	bucket := "unknown"
	switch {
	case isDoneStatus(lastStatus):
		bucket = "ok"
	case lastStatus == "ERROR":
		if doneAny {
			bucket = "done_then_error"
		} else {
			bucket = "failed"
		}
	}
	return AccountSummary{
		Name:       name,
		Bucket:     bucket,
		Attempts:   len(rows),
		LastTime:   last.Timestamp(),
		LastStatus: lastStatus,
		LastResult: last.Reason(),
		DoneAny:    doneAny,
	}
}

func isDoneStatus(s string) bool {
	u := strings.ToUpper(strings.TrimSpace(s))
	return u == "DONE" || u == "SUCCESS"
}

// FetchBrief 从 MAS API 拉取指定游戏日的日常汇总。
func FetchBrief(ctx context.Context, baseURL, day string, timeout time.Duration) (*Brief, error) {
	baseURL = strings.TrimRight(strings.TrimSpace(baseURL), "/")
	if baseURL == "" {
		baseURL = "http://127.0.0.1:36163"
	}
	if day == "" {
		day = GameDay(time.Now())
	}
	if timeout <= 0 {
		timeout = 20 * time.Second
	}
	body, _ := json.Marshal(map[string]string{
		"mode":       "DAILY",
		"start_date": day,
		"end_date":   day,
	})
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, baseURL+"/api/history/search", bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	client := &http.Client{Timeout: timeout}
	resp, err := client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("连接 MAS（%s）失败：%w", baseURL, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("MAS 返回 HTTP %d", resp.StatusCode)
	}
	var payload struct {
		Data map[string]map[string]struct {
			Index []HistoryEntry `json:"index"`
		} `json:"data"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&payload); err != nil {
		return nil, fmt.Errorf("解析 MAS 响应失败：%w", err)
	}
	return BuildBrief(baseURL, day, payload.Data[day]), nil
}

// BuildBrief 根据单日 history 记录构建快报。
func BuildBrief(baseURL, day string, dayMap map[string]struct {
	Index []HistoryEntry `json:"index"`
}) *Brief {
	counts := map[string]int{"ok": 0, "failed": 0, "done_then_error": 0, "unknown": 0}
	errCounts := map[string]int{}
	errAccs := map[string]map[string]bool{}
	var accounts []AccountSummary

	for name, rec := range dayMap {
		s := Classify(name, rec.Index)
		accounts = append(accounts, s)
		counts[s.Bucket]++
		for _, e := range rec.Index {
			if strings.ToUpper(strings.TrimSpace(e.Status)) == "ERROR" {
				reason := strings.TrimSpace(e.Reason())
				if reason == "" {
					reason = "未知错误"
				}
				errCounts[reason]++
				if errAccs[reason] == nil {
					errAccs[reason] = map[string]bool{}
				}
				errAccs[reason][name] = true
			}
		}
	}
	sort.Slice(accounts, func(i, j int) bool {
		if accounts[i].Bucket != accounts[j].Bucket {
			return bucketRank(accounts[i].Bucket) < bucketRank(accounts[j].Bucket)
		}
		return accounts[i].Name < accounts[j].Name
	})

	var clusters []ErrorCluster
	for reason, cnt := range errCounts {
		var names []string
		for n := range errAccs[reason] {
			names = append(names, n)
		}
		sort.Strings(names)
		clusters = append(clusters, ErrorCluster{Reason: reason, Count: cnt, Accounts: names})
	}
	sort.Slice(clusters, func(i, j int) bool {
		if clusters[i].Count != clusters[j].Count {
			return clusters[i].Count > clusters[j].Count
		}
		return clusters[i].Reason < clusters[j].Reason
	})

	return &Brief{
		AsOf:          time.Now().Format("2006-01-02 15:04:05"),
		MasURL:        baseURL,
		GameDay:       day,
		Counts:        counts,
		Accounts:      accounts,
		ErrorClusters: clusters,
	}
}

func bucketRank(b string) int {
	switch b {
	case "failed":
		return 0
	case "done_then_error":
		return 1
	case "unknown":
		return 2
	case "ok":
		return 3
	default:
		return 4
	}
}

// RenderMarkdown 输出可读摘要。
func (b *Brief) RenderMarkdown() string {
	var sb strings.Builder
	fmt.Fprintf(&sb, "# MAS 日常快报 %s\n", b.GameDay)
	fmt.Fprintf(&sb, "- MAS：%s｜生成时间：%s\n", b.MasURL, b.AsOf)
	fmt.Fprintf(&sb, "- 账号数：%d｜完成 %d｜未完成 %d｜待确认 %d｜未知 %d\n",
		len(b.Accounts), b.Counts["ok"], b.Counts["failed"], b.Counts["done_then_error"], b.Counts["unknown"])
	if len(b.ErrorClusters) > 0 {
		sb.WriteString("\n## 错误聚类\n")
		for _, c := range b.ErrorClusters {
			fmt.Fprintf(&sb, "- %d 次（%d 个账号）：%s\n", c.Count, len(c.Accounts), c.Reason)
		}
	}
	var bad []AccountSummary
	for _, a := range b.Accounts {
		if a.Bucket == "failed" || a.Bucket == "done_then_error" {
			bad = append(bad, a)
		}
	}
	if len(bad) > 0 {
		sb.WriteString("\n## 未完成 / 待确认\n")
		for _, a := range bad {
			fmt.Fprintf(&sb, "- %s（%s，尝试 %d 次，最后时间 %s）：%s\n", a.Name, a.Bucket, a.Attempts, a.LastTime, a.LastResult)
		}
	}
	return sb.String()
}
