package ext

// 名册（box）解析：把「账号有哪些干员、什么练度」变成 model.Roster。
//
// 支持四种来源，按内容自动识别（ParseRoster）：
//
//  1. MAA 小工具「干员识别」写在本机的 data/OperBoxData.json（LoadOperBox）——
//     不用手工粘贴，机队每台 MAA 的 data 目录下都有；source=local 时不含技能/模组。
//  2. MAA 干员识别的「剪贴板」JSON 数组（PascalCase：Name/Rarity/Elite/Level/Own/Potential/Id）。
//  3. Markdown/CSV 表格（首行是表头的，按表头取列；没有表头时按「名 星 精英 等级」猜）。
//  4. 纯干员名清单（每行一个）。
//
// 后两种拿不到技能与模组，SkillKnown 一律为 false。

import (
	"encoding/json"
	"fmt"
	"os"
	"sort"
	"strconv"
	"strings"

	"github.com/xiachk083-hub/maa-cli-fleet/internal/copilotpick/model"
)

// 名册来源格式，写进 model.Roster.Format。
const (
	FormatOperBox   = "operbox"   // MAAdata/OperBoxData.json
	FormatClipboard = "clipboard" // MAA 干员识别剪贴板 JSON
	FormatTable     = "table"     // Markdown/CSV 表格
	FormatNames     = "names"     // 纯名字清单
)

// OperBoxData 是 MAA「干员识别」落盘的 JSON（<data>/OperBoxData.json）。
//
// 旧版（2025-05-29 前）是裸干员数组，没有 source/syncTime；LoadOperBox 两种都认。
type OperBoxData struct {
	Done     *bool        `json:"done"`
	Source   string       `json:"source"`
	SyncTime string       `json:"syncTime"`
	OwnOpers []OperBoxOper `json:"own_opers"`
}

// OperBoxOper 是 OperBoxData 里的一条干员。
//
// MainSkillLevel/Skills/Equips 只有 source=yituliu 才有；MAA 本地识别整块缺失。
type OperBoxOper struct {
	ID             string `json:"id"`
	Name           string `json:"name"`
	Own            bool   `json:"own"`
	Rarity         int    `json:"rarity"`
	Elite          int    `json:"elite"`
	Level          int    `json:"level"`
	Potential      int    `json:"potential"`
	MainSkillLevel int    `json:"mainSkillLevel"`
	Skills         []struct {
		ID    string `json:"id"`
		Level int    `json:"level"`
	} `json:"skills"`
	Equips []struct {
		ID    string `json:"id"`
		Level int    `json:"level"`
	} `json:"equips"`
}

// LoadOperBox 读 MAA 的 OperBoxData.json，生成名册。
//
// 拒绝三种情况（错误信息直接给用户看）：文件不存在、识别未完成（done=false）、
// own_opers 全是未拥有。source=local 时 Warning 里会写清「技能/模组按已拥有处理」。
func LoadOperBox(path string) (model.Roster, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return model.Roster{}, fmt.Errorf("读取 MAA 干员识别结果失败（%s）：%w", path, err)
	}
	return ParseOperBoxData(b)
}

// ParseOperBoxData 供测试/离线使用：解析 OperBoxData.json 的字节。
func ParseOperBoxData(b []byte) (model.Roster, error) {
	var data OperBoxData
	if err := json.Unmarshal(b, &data); err != nil {
		// 旧格式：裸干员数组
		var legacy []OperBoxOper
		if err2 := json.Unmarshal(b, &legacy); err2 != nil {
			return model.Roster{}, fmt.Errorf("OperBoxData.json 不是合法 JSON：%w", err)
		}
		data = OperBoxData{OwnOpers: legacy, Source: "local"}
	}
	if data.Done != nil && !*data.Done {
		return model.Roster{}, fmt.Errorf("MAA 干员识别未完成（done=false），请在 MAA 小工具里重跑一次识别再导入")
	}

	entries := make([]model.RosterEntry, 0, len(data.OwnOpers))
	fuzz := false
	for _, op := range data.OwnOpers {
		if !op.Own || strings.TrimSpace(op.Name) == "" {
			continue
		}
		skills := make([]int, 0, len(op.Skills))
		for _, s := range op.Skills {
			skills = append(skills, s.Level)
		}
		modules := map[int]int{}
		for i, e := range op.Equips {
			if e.Level > 0 {
				modules[i+1] = e.Level
			}
		}
		skillKnown := op.MainSkillLevel > 0 || len(skills) > 0 || len(modules) > 0
		if !skillKnown {
			fuzz = true
		}
		entries = append(entries, model.RosterEntry{
			Name:       strings.TrimSpace(op.Name),
			OperID:     op.ID,
			Rarity:     op.Rarity,
			Elite:      op.Elite,
			Level:      op.Level,
			Potential:  op.Potential,
			SkillLevel: op.MainSkillLevel,
			Skills:     skills,
			Modules:    modules,
			SkillKnown: skillKnown,
		})
	}
	if len(entries) == 0 {
		return model.Roster{}, fmt.Errorf("OperBoxData.json 里没有已拥有的干员（own_opers 为空或全是 own=false）")
	}

	sortRoster(entries)
	r := buildRoster(entries, FormatOperBox)
	r.Source = data.Source
	r.SyncedAt = data.SyncTime
	r.SkillKnown = !fuzz
	if fuzz {
		src := data.Source
		if src == "" {
			src = "local"
		}
		r.Warnings = append(r.Warnings, fmt.Sprintf(
			"source=%s：MAA 本地识别不含技能/专精/模组数据，作业匹配只比精英化与等级（技能模组按已拥有处理）", src))
	}
	return r, nil
}

// ParseRoster 按内容自动识别格式解析一段粘贴文本。
func ParseRoster(text string) (model.Roster, error) {
	t := strings.TrimSpace(strings.TrimPrefix(text, "\ufeff"))
	if t == "" {
		return model.Roster{}, fmt.Errorf("名册内容为空")
	}
	switch t[0] {
	case '{':
		// OperBoxData 对象（MAA 小工具导出的文件内容）
		if strings.Contains(t, "\"own_opers\"") {
			return ParseOperBoxData([]byte(t))
		}
		return model.Roster{}, fmt.Errorf("无法识别的 JSON 对象：需要 OperBoxData.json 的 own_opers 段")
	case '[':
		// 剪贴板（MAA 干员识别的 PascalCase 数组）与旧版 OperBoxData（裸 own_opers 数组）
		// 形状接近，用键名大小写区分：PascalCase 的是剪贴板。
		if strings.Contains(t, "\"Name\"") || strings.Contains(t, "\"Own\"") {
			return parseClipboard(t)
		}
		return ParseOperBoxData([]byte(t))
	default:
		return parseTableOrNames(t)
	}
}

// parseClipboard 解析 MAA 干员识别「剪贴板」JSON 数组。
//
// 键名大小写混用都认（PascalCase/camelCase/蛇形），星级/精英/等级缺失时按 0 处理。
func parseClipboard(text string) (model.Roster, error) {
	var rows []map[string]any
	if err := json.Unmarshal([]byte(text), &rows); err != nil {
		return model.Roster{}, fmt.Errorf("剪贴板 JSON 解析失败（应为干员数组）：%w", err)
	}
	entries := make([]model.RosterEntry, 0, len(rows))
	for _, m := range rows {
		// Own 缺省视为拥有；显式 own=false 的条目跳过
		if own, ok := pickBool(m, "own"); ok && !own {
			continue
		}
		name, _ := pickString(m, "name")
		name = strings.TrimSpace(name)
		if name == "" {
			continue
		}
		rarity, _ := pickInt(m, "rarity")
		elite, _ := pickInt(m, "elite")
		level, _ := pickInt(m, "level")
		potential, _ := pickInt(m, "potential")
		id, _ := pickString(m, "id", "operid", "charid", "oper_id")
		skillLevel, _ := pickInt(m, "skilllevel", "mainskilllevel")
		entries = append(entries, model.RosterEntry{
			Name:       name,
			OperID:     id,
			Rarity:     rarity,
			Elite:      elite,
			Level:      level,
			Potential:  potential,
			SkillLevel: skillLevel,
			SkillKnown: skillLevel > 0,
		})
	}
	if len(entries) == 0 {
		return model.Roster{}, fmt.Errorf("剪贴板 JSON 里没有可用干员（名字为空或 own=false）")
	}
	sortRoster(entries)
	r := buildRoster(entries, FormatClipboard)
	r.SkillKnown = false
	for _, e := range entries {
		if e.SkillKnown {
			r.SkillKnown = true
			break
		}
	}
	if !r.SkillKnown {
		r.Warnings = append(r.Warnings, "剪贴板名册不含技能/专精/模组数据：匹配时这两维按已拥有处理")
	}
	return r, nil
}

// parseTableOrNames 解析 Markdown/CSV 表格或纯名字清单。
func parseTableOrNames(text string) (model.Roster, error) {
	lines := strings.Split(strings.ReplaceAll(text, "\r\n", "\n"), "\n")

	// 表头列位：-1 表示没有这一列
	colName, colRarity, colElite, colLevel, colSkill := -1, -1, -1, -1, -1
	headerSeen := false
	entries := make([]model.RosterEntry, 0, len(lines))
	positional := false

	for _, raw := range lines {
		line := strings.TrimSpace(raw)
		if line == "" || isTableSeparator(line) {
			continue
		}
		cells := splitCells(line)
		if len(cells) == 0 {
			continue
		}
		if !headerSeen && looksLikeHeader(cells) {
			for i, c := range cells {
				low := strings.ToLower(c)
				switch {
				case colName < 0 && containsAny(low, "干员", "名称", "name", "角色"):
					colName = i
				case colRarity < 0 && containsAny(low, "星级", "稀有", "rarity", "star"):
					colRarity = i
				case colElite < 0 && containsAny(low, "精英", "elite", "精"):
					colElite = i
				case colLevel < 0 && containsAny(low, "等级", "level", "lv"):
					colLevel = i
				case colSkill < 0 && containsAny(low, "技能", "skill"):
					colSkill = i
				}
			}
			headerSeen = true
			continue
		}

		var e model.RosterEntry
		e.Name = cleanCell(cells[0])
		if colName >= 0 && colName < len(cells) {
			e.Name = cleanCell(cells[colName])
		}
		if colName < 0 && !headerSeen && len(cells) >= 4 {
			// 没表头、每行 4 列以上：按「名 星 精英 等级」猜
			positional = true
		}
		if headerSeen || positional {
			if colName < 0 && positional && len(cells) >= 4 {
				e.Rarity = cleanInt(cells[1])
				e.Elite = cleanInt(cells[2])
				e.Level = cleanInt(cells[3])
				if len(cells) >= 5 {
					e.SkillLevel = cleanInt(cells[4])
				}
			} else {
				if colRarity >= 0 && colRarity < len(cells) {
					e.Rarity = cleanInt(cells[colRarity])
				}
				if colElite >= 0 && colElite < len(cells) {
					e.Elite = cleanInt(cells[colElite])
				}
				if colLevel >= 0 && colLevel < len(cells) {
					e.Level = cleanInt(cells[colLevel])
				}
				if colSkill >= 0 && colSkill < len(cells) {
					e.SkillLevel = cleanInt(cells[colSkill])
				}
			}
		}
		if e.Name == "" {
			continue
		}
		e.SkillKnown = e.SkillLevel > 0
		entries = append(entries, e)
	}

	if len(entries) == 0 {
		return model.Roster{}, fmt.Errorf("没从文本里解析出任何干员名")
	}
	sortRoster(entries)
	format := FormatNames
	for _, e := range entries {
		if e.Rarity > 0 || e.Elite > 0 || e.Level > 0 {
			format = FormatTable
			break
		}
	}
	r := buildRoster(entries, format)
	r.SkillKnown = false
	for _, e := range entries {
		if e.SkillKnown {
			r.SkillKnown = true
			break
		}
	}
	if format == FormatNames {
		r.Warnings = append(r.Warnings, "只解析出干员名：没有星级/精英/等级，作业匹配无法核对练度")
	} else if !r.SkillKnown {
		r.Warnings = append(r.Warnings, "表格名册不含技能/专精/模组数据：匹配时这两维按已拥有处理")
	}
	return r, nil
}

// buildRoster 统计六星/五星数量并组装 Roster。
func buildRoster(entries []model.RosterEntry, format string) model.Roster {
	r := model.Roster{Entries: entries, Count: len(entries), Format: format}
	for _, e := range entries {
		switch e.Rarity {
		case 6:
			r.Six++
		case 5:
			r.Five++
		}
	}
	return r
}

// sortRoster 与 MAA 的展示顺序一致：星级 → 精英 → 等级 降序，同档按名字。
func sortRoster(entries []model.RosterEntry) {
	sort.SliceStable(entries, func(i, j int) bool {
		a, b := entries[i], entries[j]
		if a.Rarity != b.Rarity {
			return a.Rarity > b.Rarity
		}
		if a.Elite != b.Elite {
			return a.Elite > b.Elite
		}
		if a.Level != b.Level {
			return a.Level > b.Level
		}
		return a.Name < b.Name
	})
}

// RosterIndex 把名册转成「归一化名字 → 干员」的索引，匹配与修正都用它。
func RosterIndex(roster model.Roster) map[string]model.RosterEntry {
	out := make(map[string]model.RosterEntry, len(roster.Entries))
	for _, e := range roster.Entries {
		out[NormName(e.Name)] = e
	}
	return out
}

// CharInfos 把名册转成作业修正用的结构信息（星级 + charId）。
func CharInfos(roster model.Roster) map[string]CharInfo {
	out := make(map[string]CharInfo, len(roster.Entries))
	for _, e := range roster.Entries {
		if e.Rarity <= 0 {
			continue
		}
		out[NormName(e.Name)] = CharInfo{Stars: e.Rarity, ID: e.OperID}
	}
	return out
}

// NormName 归一化干员名：去掉所有空白（作业站与 MAA 的名字偶尔带空格）。
func NormName(name string) string {
	return strings.Join(strings.Fields(name), "")
}

// ---- 文本小工具 ----

func isTableSeparator(line string) bool {
	t := strings.NewReplacer("|", "", "-", "", ":", "", " ", "", "\t", "").Replace(line)
	return t == ""
}

func splitCells(line string) []string {
	line = strings.TrimSpace(line)
	line = strings.TrimPrefix(line, "- ")
	line = strings.TrimPrefix(line, "* ")
	var parts []string
	switch {
	case strings.Contains(line, "|"):
		parts = strings.Split(line, "|")
	case strings.Contains(line, "\t"):
		parts = strings.Split(line, "\t")
	default:
		parts = []string{line}
	}
	out := make([]string, 0, len(parts))
	for _, p := range parts {
		p = cleanCell(p)
		if p != "" {
			out = append(out, p)
		}
	}
	return out
}

func cleanCell(s string) string {
	s = strings.TrimSpace(s)
	s = strings.Trim(s, "*`_ ")
	return strings.TrimSpace(s)
}

func cleanInt(s string) int {
	var b strings.Builder
	for _, r := range s {
		if r >= '0' && r <= '9' {
			b.WriteRune(r)
		}
	}
	n, _ := strconv.Atoi(b.String())
	return n
}

func looksLikeHeader(cells []string) bool {
	joined := strings.ToLower(strings.Join(cells, " "))
	return containsAny(joined, "干员", "名称", "name", "星级", "rarity", "精英", "elite", "等级", "level")
}

func containsAny(s string, subs ...string) bool {
	for _, sub := range subs {
		if strings.Contains(s, sub) {
			return true
		}
	}
	return false
}

// ---- JSON 键名容错 ----

func pickKey(m map[string]any, keys ...string) (any, bool) {
	for _, k := range keys {
		if v, ok := m[k]; ok {
			return v, true
		}
	}
	// 大小写不敏感兜底
	for mk, v := range m {
		low := strings.ToLower(mk)
		for _, k := range keys {
			if low == strings.ToLower(k) {
				return v, true
			}
		}
	}
	return nil, false
}

func pickString(m map[string]any, keys ...string) (string, bool) {
	if v, ok := pickKey(m, keys...); ok {
		if s, ok := v.(string); ok {
			return s, true
		}
	}
	return "", false
}

func pickInt(m map[string]any, keys ...string) (int, bool) {
	if v, ok := pickKey(m, keys...); ok {
		switch t := v.(type) {
		case float64:
			return int(t), true
		case int:
			return t, true
		case string:
			n, err := strconv.Atoi(strings.TrimSpace(t))
			if err == nil {
				return n, true
			}
		}
	}
	return 0, false
}

func pickBool(m map[string]any, keys ...string) (bool, bool) {
	if v, ok := pickKey(m, keys...); ok {
		switch t := v.(type) {
		case bool:
			return t, true
		case float64:
			return t != 0, true
		}
	}
	return false, false
}
