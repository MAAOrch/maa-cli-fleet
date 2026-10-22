package runner

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func mkRoster(t *testing.T, body string) *Roster {
	t.Helper()
	p := filepath.Join(t.TempDir(), "roster.json")
	if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	ro := LoadRoster(p)
	if ro == nil {
		t.Fatal("roster 没加载（应为启用）")
	}
	return ro
}

const sample = `{
  "enabled": true,
  "cycleHours": 16,
  "minGapHours": 12,
  "shifts": [
    {"name": "零点班",   "at": "00:00", "accounts": ["a01"]},
    {"name": "八点班",   "at": "08:00", "accounts": ["a02"]},
    {"name": "十六点班", "at": "16:00", "accounts": ["a03"]}
  ]
}`

func at(s string) time.Time {
	x, _ := time.ParseInLocation(stampLayout, s, time.Local)
	return x
}

// 网格：锚点 + 16h 整数倍（锚点 00:00 → 00:00 / 16:00 / 次日 08:00 / …）
func TestNextSlotGrid(t *testing.T) {
	ro := mkRoster(t, sample)
	cases := []struct{ from, want string }{
		{"2026-10-03 00:00:00", "2026-10-03 00:00:00"},
		{"2026-10-03 00:00:01", "2026-10-03 16:00:00"},
		{"2026-10-03 12:00:00", "2026-10-03 16:00:00"},
		{"2026-10-03 16:00:00", "2026-10-03 16:00:00"},
		{"2026-10-03 23:59:00", "2026-10-04 08:00:00"}, // 00:00+16h=16:00 → 再 +16h=次日 08:00
	}
	for _, c := range cases {
		got, ok := ro.nextSlot("a01", at(c.from))
		if !ok || got.Format(stampLayout) != c.want {
			t.Errorf("a01 from %s：got %s（%v），want %s", c.from, got.Format(stampLayout), ok, c.want)
		}
	}
	if _, ok := ro.nextSlot("zz", at("2026-10-03 00:00:00")); ok {
		t.Error("没排班的号不该拿到网格点")
	}
}

// 排下一次：至少隔 minGap，且落在网格上；班内排队（跑晚 2h）不跳班
func TestSnapDue(t *testing.T) {
	ro := mkRoster(t, sample)
	now := at("2026-10-03 10:00:00")
	cases := []struct{ id, last, want string }{
		// a01 锚点 00:00：昨晚 00:20 跑完（班内排队 20 分钟）→ 下一格是今天 16:00
		{"a01", "2026-10-03 00:20:00", "2026-10-03 16:00:00"},
		// 跑晚 2h（02:00）也不该跳过今天 16:00（16:00-02:00=14h ≥ 12h）
		{"a01", "2026-10-03 02:00:00", "2026-10-03 16:00:00"},
		// 跑晚 5h（05:00）→ 16:00 只剩 11h < 12h → 顺到下一格（次日 08:00）
		{"a01", "2026-10-03 05:00:00", "2026-10-04 08:00:00"},
		// a02 锚点 08:00：09:00 跑完 → 下一格 次日 00:00（+15h）
		{"a02", "2026-10-03 09:00:00", "2026-10-04 00:00:00"},
	}
	for _, c := range cases {
		got, shift, ok := ro.SnapDue(c.id, at(c.last), now)
		if !ok || got.Format(stampLayout) != c.want {
			t.Errorf("%s last=%s：got %s（%v shift=%s），want %s", c.id, c.last, got.Format(stampLayout), ok, shift, c.want)
		}
	}
	// reseed：lastRun 为零 → 从现在起的第一个网格点（忽略 minGap）
	if d, _, ok := ro.SnapDue("a01", time.Time{}, at("2026-10-03 10:00:00")); !ok || d.Format(stampLayout) != "2026-10-03 16:00:00" {
		t.Errorf("reseed-next：got %s（%v），want 2026-10-03 16:00:00", d.Format(stampLayout), ok)
	}
	// 没排班的号 → 不接管（调用方退回理智自循环）
	if _, _, ok := ro.SnapDue("zz", now, now); ok {
		t.Error("没排班的号不该有班表时刻")
	}
}

// OnGrid：只认精确落在网格点的 NextDue（理智自循环写的是带秒的，不算对齐）
func TestOnGrid(t *testing.T) {
	ro := mkRoster(t, sample)
	if !ro.OnGrid("a01", at("2026-10-03 16:00:00")) {
		t.Error("16:00 应在网格上")
	}
	if ro.OnGrid("a01", at("2026-10-03 08:46:27")) {
		t.Error("08:46:27 不在网格上")
	}
}

// 关掉/缺文件 → 完全退回旧行为（nil）
func TestRosterDisabled(t *testing.T) {
	p := filepath.Join(t.TempDir(), "roster.json")
	_ = os.WriteFile(p, []byte(`{"enabled": false, "shifts": []}`), 0o644)
	if LoadRoster(p) != nil {
		t.Error("enabled=false 应返回 nil")
	}
	if LoadRoster(filepath.Join(t.TempDir(), "nope.json")) != nil {
		t.Error("文件不存在应返回 nil")
	}
}

// 连续模式：order 里 n 台按顺序均匀铺在一个 cycle 轮盘上
func TestOrderMode(t *testing.T) {
	body := `{"enabled": true, "cycleHours": 16, "base": "00:15", "order": ["a01","a02","a03"], "reseed": true}`
	ro := mkRoster(t, body)
	cases := []struct{ id, want string }{
		{"a01", "00:15"},
		{"a02", "05:35"}, // +320 分钟
		{"a03", "10:55"}, // +640 分钟
	}
	for _, c := range cases {
		got, ok := ro.nextSlot(c.id, at("2026-10-03 00:00:00"))
		if !ok || got.Format("15:04") != c.want {
			t.Errorf("%s：got %s（%v），want %s", c.id, got.Format("15:04"), ok, c.want)
		}
	}
	if ro.shiftOf("a02") != "连续班" {
		t.Errorf("shiftOf = %q，want 连续班", ro.shiftOf("a02"))
	}
	// reseed：从现在起第一个网格点（忽略 minGap）
	if d, _, ok := ro.SnapDue("a01", time.Time{}, at("2026-10-03 10:00:00")); !ok || d.Format(stampLayout) != "2026-10-03 16:15:00" {
		t.Errorf("reseed-next：got %s（%v），want 2026-10-03 16:15:00", d.Format(stampLayout), ok)
	}
}

// 回归：网格原点必须固定 —— 跨午夜（日期变了）相位不能翻 8h
func TestGridStableAcrossMidnight(t *testing.T) {
	ro := mkRoster(t, sample) // a01 锚点 00:00，周期 16h
	// 10-02 23:59 与 10-03 00:00 算出来的"下一个网格点"必须一致
	a, _ := ro.nextSlot("a01", at("2026-10-02 23:59:00"))
	b, _ := ro.nextSlot("a01", at("2026-10-03 00:00:00"))
	if !a.Equal(b) {
		t.Fatalf("跨午夜相位漂移：10-02 23:59 → %s，10-03 00:00 → %s", a.Format(stampLayout), b.Format(stampLayout))
	}
	// 同一天里连续两次/下一次的关系：跑完一次后的下一次，必在上次 +16h 的网格上
	first, _ := ro.nextSlot("a01", at("2026-10-03 00:00:00"))
	second, _ := ro.nextSlot("a01", first.Add(time.Minute))
	if d := second.Sub(first); d != 16*time.Hour {
		t.Fatalf("下一次间隔 %v，want 16h", d)
	}
}

// 回归：state 里的时间戳是本地墙钟，解析必须按本地时区（老 bug：按 UTC 解析 → 定时整体晚 8h）
func TestParseStampLocal(t *testing.T) {
	got, ok := parseStamp("2026-10-03 00:15:00")
	want := time.Date(2026, 10, 3, 0, 15, 0, 0, time.Local)
	if !ok || !got.Equal(want) {
		t.Fatalf("parseStamp = %s（%v），want %s（本地）", got, ok, want)
	}
	if _, ok := parseStamp("2026-10-03 00:15"); ok {
		t.Error("缺秒的时间戳应当解析失败")
	}
	// 用本地时间比较：本地 00:20 时，"00:15" 的班次点应该已经到点
	if !time.Date(2026, 10, 3, 0, 20, 0, 0, time.Local).After(got) {
		t.Error("本地比较：00:20 应晚于 00:15")
	}
}
