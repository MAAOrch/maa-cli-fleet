package runner

import (
	"encoding/json"
	"fmt"
	"log"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/xiachk083-hub/maa-cli-fleet/internal/model"
)

// ---- 班表（roster）------------------------------------------------------------
//
// 现状（旧行为）：日常是"理智自循环"——跑完按 理智差×6 分钟 排下一次；
// 于是每台机的时间点由上一次跑的时刻决定，会永远带着历史相位漂移，
// 结果就是一茬号全挤在同一个早高峰、其余时段空闲（10-03 实测峰值 12 台重叠 @06:00）。
//
// 班表模式：给每台机定一个**班次锚点**（一天三班，锚点错开 8 小时），
// 日常只跑在「锚点 + cycleHours 整数倍」的网格点上 —— 每台机仍是 16h 一班，
// 但相位被钉死在它自己的班次上：一天里 00/08/16 三个时段各一批，全天铺开。
//
//	锚点 00:00 的号：00:00 → 16:00 → 次日 08:00 → …（3 天轮完三个时段）
//	锚点 08:00 的号：08:00 → 次日 00:00 → 次日 16:00 → …
//	锚点 16:00 的号：16:00 → 次日 08:00 → 次日 00:00 → …
//
// 与理智的关系：仍然是"接近满理智才跑"（16h 回 160 点，不到上限 200 不浪费），
// 只是把"什么时候算满"这件事对齐到班次；间隔 ≥ minGapHours（默认 12h）防止一天跑两次。
//
// minGap 不能设得太大：同一班 18 台在 4 个并发位上排队，最后一台可能比锚点晚 2~3h 才跑，
// 若 minGap 逼近 cycleHours，下一班（+16h）就会被跳过 → 该号变成 32h 一轮。12h 留足余量。
//
// 生效方式：**下一次跑完开始对齐**（不回头改已排好的 NextDue，避免首日早跑/晚跑浪费理智）。
// roster.json 不存在 / enabled=false → 完全退回旧行为（理智自循环），不影响现网。
//
// 两种排法（二选一）：
//
//	① 连续模式 order：n 台按顺序均匀铺在 cycleHours 轮盘上（间隔 = cycle/n），
//	   起点由 base 给 —— 全网变成一条"每 ~18 分钟一班"的连续流水线；
//	② 班次模式 shifts：几班倒（每班一个锚点时刻，班内多台）。
//
// 文件（默认与该机 state.json 同目录的 roster.json）：
//
//	{
//	  "enabled": true,
//	  "cycleHours": 16,
//	  "minGapHours": 12,
//	  "reseed": false,
//	  "shifts": [
//	    {"name": "零点班",   "at": "00:00", "accounts": ["a01", "a04"]},
//	    {"name": "八点班",   "at": "08:00", "accounts": ["a02", "a05"]},
//	    {"name": "十六点班", "at": "16:00", "accounts": ["a03", "a06"]}
//	  ]
//	}
//
// 不在 shifts 里的账号（新号、没排班的号）= 旧行为，逐台平滑过渡。
type Roster struct {
	Enabled     bool     `json:"enabled"`
	CycleHours  int      `json:"cycleHours"`
	MinGapHours int      `json:"minGapHours"`
	Reseed      bool     `json:"reseed,omitempty"` // true = 部署时立即把已排好的 NextDue 对齐到班次
	Base        string   `json:"base,omitempty"`   // 连续模式：第 0 位的时刻（HH:MM，默认 00:00）
	Order       []string `json:"order,omitempty"`  // 连续模式：按顺序均匀铺在 cycleHours 轮盘上
	Shifts      []Shift  `json:"shifts"`

	anchor map[string]slotAnchor
}

// Shift 是一班：名字 + 锚点时刻 + 这班的账号。
type Shift struct {
	Name     string   `json:"name"`
	At       string   `json:"at"`
	Accounts []string `json:"accounts"`
}

type slotAnchor struct {
	hour, min int
	shift     string
}

const stampLayout = "2006-01-02 15:04:05"

// parseStamp：state/roster 里的时间戳一律是**本地墙钟**（time.Now().Format(stampLayout) 写的），
// 解析必须用 ParseInLocation(time.Local)。用 time.Parse（无时区 → 按 UTC）会让所有定时
// 整体晚 8 小时（CST）—— 2026-10-03 挖出来的老 bug，派车判据一直"看着对、实际晚 8h"。
func parseStamp(s string) (time.Time, bool) {
	t, err := time.ParseInLocation(stampLayout, s, time.Local)
	if err != nil {
		return time.Time{}, false
	}
	return t, true
}

// LoadRoster：读班表（读不到/关着 = nil，调用方按旧行为走）。
func LoadRoster(path string) *Roster {
	b, err := model.ReadFileBOM(path)
	if err != nil {
		return nil
	}
	var ro Roster
	if err := json.Unmarshal(b, &ro); err != nil {
		log.Printf("[班表] %s 解析失败（按无班表处理）：%v", path, err)
		return nil
	}
	if !ro.Enabled || (len(ro.Shifts) == 0 && len(ro.Order) == 0) {
		return nil
	}
	if ro.CycleHours <= 0 {
		ro.CycleHours = 16
	}
	if ro.MinGapHours <= 0 {
		ro.MinGapHours = 12
	}
	if ro.MinGapHours >= ro.CycleHours { // 防呆：间隔 ≥ 周期会跳过整班
		log.Printf("[班表] minGapHours=%dh ≥ cycleHours=%dh，已收窄为 %dh", ro.MinGapHours, ro.CycleHours, ro.CycleHours-4)
		ro.MinGapHours = ro.CycleHours - 4
	}
	ro.anchor = map[string]slotAnchor{}
	// 连续模式（order）：n 台按顺序均匀铺在 cycleHours 轮盘上，间隔 = cycle/n
	if len(ro.Order) > 0 {
		h, m, ok := parseHM(ro.Base)
		if ro.Base == "" {
			h, m, ok = 0, 0, true
		}
		if !ok {
			log.Printf("[班表] base=%q 不合法（要 HH:MM），按 00:00", ro.Base)
			h, m = 0, 0
		}
		cycleMin := ro.CycleHours * 60
		n := len(ro.Order)
		for i, id := range ro.Order {
			id = strings.TrimSpace(id)
			if id == "" {
				continue
			}
			off := (i*cycleMin + n/2) / n // 四舍五入到分钟
			tot := (h*60 + m + off) % 1440
			ro.anchor[id] = slotAnchor{tot / 60, tot % 60, "连续班"}
		}
		log.Printf("[班表] 连续模式：%d 台，间隔 %.1f 分钟，起点 %02d:%02d", n,
			float64(cycleMin)/float64(n), h, m)
	}
	for _, s := range ro.Shifts {
		h, m, ok := parseHM(s.At)
		if !ok {
			log.Printf("[班表] 班次 %q 的时刻 %q 不合法（要 HH:MM），跳过", s.Name, s.At)
			continue
		}
		for _, id := range s.Accounts {
			id = strings.TrimSpace(id)
			if id != "" {
				ro.anchor[id] = slotAnchor{h, m, s.Name}
			}
		}
	}
	if len(ro.anchor) == 0 {
		return nil
	}
	return &ro
}

func parseHM(s string) (int, int, bool) {
	var h, m int
	if _, err := fmt.Sscanf(strings.TrimSpace(s), "%d:%d", &h, &m); err != nil {
		return 0, 0, false
	}
	if h < 0 || h > 23 || m < 0 || m > 59 {
		return 0, 0, false
	}
	return h, m, true
}

// Has 这台机排了班没有。
func (ro *Roster) Has(accountID string) bool {
	if ro == nil {
		return false
	}
	_, ok := ro.anchor[accountID]
	return ok
}

// shiftOf 班次名（没排班 → ""）。
func (ro *Roster) shiftOf(accountID string) string {
	if ro == nil {
		return ""
	}
	return ro.anchor[accountID].shift
}

// nextSlot：从 from 起（含）第一个"锚点 + cycle 整数倍"的时刻。
func (ro *Roster) nextSlot(accountID string, from time.Time) (time.Time, bool) {
	a, ok := ro.anchor[accountID]
	if !ok {
		return time.Time{}, false
	}
	// 原点必须是**固定时刻**：16h 不整除 24h，若用"当天的锚点"当原点，
	// 相位会随参考日期翻 8h（10-02 一次、10-03 另一次）—— 2026-10-03 踩过。
	origin := time.Date(2000, 1, 1, a.hour, a.min, 0, 0, from.Location())
	step := time.Duration(ro.CycleHours) * time.Hour
	d := from.Sub(origin)
	if d <= 0 {
		return origin, true
	}
	k := (d + step - 1) / step // 向上取整到网格
	return origin.Add(k * step), true
}

// SnapDue：这台机的下一次日常时刻。
//
//	lastRun 非零 → 至少要晚于 lastRun+minGapHours（防一天两跑），再落到班次网格；
//	lastRun 为零 → 直接从 now 起落到网格（首次铺班 / reseed“清掉旧排班”用）。
func (ro *Roster) SnapDue(accountID string, lastRun, now time.Time) (time.Time, string, bool) {
	if ro == nil || !ro.Has(accountID) {
		return time.Time{}, "", false
	}
	from := now
	if !lastRun.IsZero() {
		if g := lastRun.Add(time.Duration(ro.MinGapHours) * time.Hour); g.After(from) {
			from = g
		}
	}
	t, ok := ro.nextSlot(accountID, from)
	if !ok {
		return time.Time{}, "", false
	}
	return t, ro.shiftOf(accountID), true
}

// OnGrid：t 是否正好落在该账号的班次网格点上（用来判断“已对齐”）。
func (ro *Roster) OnGrid(accountID string, t time.Time) bool {
	if ro == nil || t.IsZero() {
		return false
	}
	p, ok := ro.nextSlot(accountID, t)
	return ok && p.Equal(t)
}

// rosterPath：班表默认与该机 state.json 同目录（runner/roster.json）。
func rosterPath(cfg Config) string {
	if cfg.RosterFile != "" {
		return cfg.RosterFile
	}
	return filepath.Join(filepath.Dir(cfg.StateFile), "roster.json")
}

// orderLine：连续模式下的排位表（用于 -status 之外的自检/文档）。
func (ro *Roster) OrderLine() string {
	if ro == nil || len(ro.Order) == 0 {
		return ""
	}
	var sb strings.Builder
	fmt.Fprintf(&sb, "连续班 %d 台（%.1f 分钟一位）", len(ro.Order), float64(ro.CycleHours*60)/float64(len(ro.Order)))
	return sb.String()
}

// logLine：启动时打一行，说明班表生效方式。
func (ro *Roster) logLine(path string) {
	if ro == nil {
		return
	}
	type row struct {
		name string
		n    int
		at   string
	}
	var rows []row
	for _, s := range ro.Shifts {
		rows = append(rows, row{s.Name, len(s.Accounts), s.At})
	}
	sort.SliceStable(rows, func(i, j int) bool { return rows[i].at < rows[j].at })
	var sb strings.Builder
	for i, r := range rows {
		if i > 0 {
			sb.WriteString("；")
		}
		fmt.Fprintf(&sb, "%s(%s) %d 台", r.name, r.at, r.n)
	}
	mode := "下次跑完开始对齐"
	if ro.Reseed {
		mode = "启动即对齐（reseed）"
	}
	log.Printf("[班表] %s：周期 %dh / 最少间隔 %dh｜%s｜%s", path, ro.CycleHours, ro.MinGapHours, sb.String(), mode)
}
