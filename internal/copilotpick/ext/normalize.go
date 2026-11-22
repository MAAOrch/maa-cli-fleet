package ext

// 交给 MaaCore 之前的作业结构修正。
//
// MAA 桌面端在「添加作业」时会自己修一遍作业 JSON 再存盘（MaaWpfGui 的
// CopilotViewModel.ParseCopilotAsync），而 `maa copilot <file>` 直接喂 MaaCore
// 时没人修——历史上「作业启动失败 / 读取文件失败」里有一整类是这么来的：
//
//	技能序号与星级不匹配（四星写了 skill=3）
//	精英要求填 0，但技能等级要求专三
//	Click 动作缺 rect/location、Swipe 动作缺 begin/end
//
// 这里把桌面端那套修正复刻一遍。修正只增不减地写进 notes，执行器会把它打进事件流，
// 便于事后对账「这份作业我们动过什么」。
//
// 与 MAA-PRTS 的差别：它拿完整的 character_table 做星级校验，我们只有名册里的干员
// （自己的号），查不到星级时跳过这项检查——MaaCore 对查不到的条目同样会跳过。

import (
	"encoding/json"
	"fmt"
	"strconv"
	"strings"
)

// CharInfo 是干员的结构信息（来自名册），作业修正只需要星级与 charId。
type CharInfo struct {
	Stars int
	ID    string
}

// actionTypesNeedingName 是「同时给了名字和坐标时 MAA 按坐标执行、名字必须清掉」的动作。
var actionTypesNeedingName = map[string]bool{
	"Skill": true, "Retreat": true, "BulletTime": true, "SkillUsage": true,
}

// NormalizeJobContent 把作业站原始 JSON 修成 MaaCore 能解析的形状。
//
// chars 是「归一化干员名 → 星级/charId」，来自名册；为空时只做结构与动作修正，
// 不做星级校验。返回修正后的 JSON 与修正说明列表。
func NormalizeJobContent(raw string, chars map[string]CharInfo) (string, []string, error) {
	var content map[string]any
	if err := json.Unmarshal([]byte(raw), &content); err != nil {
		return "", nil, fmt.Errorf("作业 JSON 解析失败：%w", err)
	}
	var notes []string

	// 顶层 opers 与各组内的 opers 一起修（组里的干员也是真干员）
	targets := asSlice(content["opers"])
	for _, g := range asSlice(content["groups"]) {
		if gm := asMap(g); gm != nil {
			targets = append(targets, asSlice(gm["opers"])...)
		}
	}
	for _, op := range targets {
		if m := asMap(op); m != nil {
			fixOper(m, chars, &notes)
		}
	}

	if actions, ok := content["actions"]; ok {
		content["actions"] = fixActions(asSlice(actions), &notes)
	} else if _, present := content["actions"]; present {
		content["actions"] = []any{}
	}

	out, err := json.MarshalIndent(content, "", "  ")
	if err != nil {
		return "", nil, fmt.Errorf("作业 JSON 序列化失败：%w", err)
	}
	return string(out), notes, nil
}

// JobTitle 取作业标题（doc.title），日志里标出「在试哪一份」。
func JobTitle(raw string) string {
	var content map[string]any
	if err := json.Unmarshal([]byte(raw), &content); err != nil {
		return ""
	}
	doc := asMap(content["doc"])
	if doc == nil {
		return ""
	}
	if s, ok := doc["title"].(string); ok {
		return strings.TrimSpace(s)
	}
	return ""
}

// JobStageName 取作业自己的关卡代码（stage_name）。
func JobStageName(raw string) string {
	var content map[string]any
	if err := json.Unmarshal([]byte(raw), &content); err != nil {
		return ""
	}
	if s, ok := content["stage_name"].(string); ok {
		return strings.TrimSpace(s)
	}
	return ""
}

// difficultyFlags 把作业的 difficulty 规范成位标志：0/缺失按普通算（跟 MAA 一致）。
func difficultyFlags(raw int) int {
	const (
		diffNormal = 1
		diffRaid   = 2
	)
	if raw <= 0 {
		return diffNormal
	}
	flags := 0
	if raw&diffNormal != 0 {
		flags |= diffNormal
	}
	if raw&diffRaid != 0 {
		flags |= diffRaid
	}
	if flags == 0 {
		return diffNormal
	}
	return flags
}

// EntryVariants 返回一份作业在 MAA 里要写成几条、每条 IsRaid 是 true 还是 false。
//
// variant 是用户在前端选的那一行（"normal"/"raid"，空表示从卡片直接加的）；
// stageName 是作业自己的关卡代码，优先于 difficulty 位标志：
//
//	15+ 章险地（#s 结尾）→ 只落 IsRaid=true（MaaCore 在 15+ 章就是用它切险地）
//	磨难/简单（easy_/tough_ 开头）→ 只落 IsRaid=false，没有突袭概念
//
// 与 MAA-PRTS 的差别：它顺手处理了「选了突袭但作业位标志只标了普通」的坑——
// 明确选了突袭就写突袭，不再被位标志静默改回普通。
func EntryVariants(flags int, variant string, stageName string) []bool {
	if stageName != "" {
		if strings.HasSuffix(stageName, "#s") {
			return []bool{true}
		}
		if strings.HasPrefix(stageName, "easy_") || strings.HasPrefix(stageName, "tough_") {
			return []bool{false}
		}
	}
	switch variant {
	case "raid":
		return []bool{true}
	case "normal":
		return []bool{false}
	}
	flags = difficultyFlags(flags)
	var out []bool
	if flags&1 != 0 {
		out = append(out, false)
	}
	if flags&2 != 0 {
		out = append(out, true)
	}
	return out
}

// fixOper 修一个干员条目：技能合法性、练度下限。查不到的条目（装置、召唤物）直接放过。
func fixOper(op map[string]any, chars map[string]CharInfo, notes *[]string) {
	name, _ := op["name"].(string)
	name = strings.TrimSpace(name)
	if name == "" {
		return
	}
	info, known := chars[NormName(name)]
	if !known || info.Stars <= 0 {
		// 装置/召唤物 MAA 也会跳过检查；名册外的干员同样跳过
		return
	}
	stars := info.Stars

	skill := intOf(op["skill"])
	if skill > 0 {
		bad := (skill == 3 && stars < 6 && info.ID != "char_002_amiya") ||
			(skill == 2 && stars < 4) ||
			(skill == 1 && stars < 3)
		if bad {
			*notes = append(*notes, fmt.Sprintf("%s 是 %d 星，没有技能 %d，已按 0 处理", name, stars, skill))
			op["skill"] = 0
			skill = 0
		}
	}

	need := skill - 1
	if need < 0 {
		need = 0
	}
	req := asMap(op["requirements"])
	if req != nil {
		if v, ok := req["skill_level"]; ok && v != nil {
			sl := intOf(v)
			if sl > 7 {
				need = maxInt(need, 2) // 专精要求精二
			} else if sl > 4 {
				need = maxInt(need, 1) // 7 级技能要求精一
			}
		}
		if v, ok := req["module"]; ok && v != nil {
			if intOf(v) > 0 {
				need = maxInt(need, 2) // 带模组要求精二
				// 账号大多没解锁作业要的模组：MAA 会卡在「模组选择」页
				// （SubTaskError first=BattleQuickFormationModule-N），编队直接失败。
				// 去掉模组要求（-1 = 不要求），先让它能进战斗（输出会弱一点）。
				*notes = append(*notes, fmt.Sprintf("%s 去掉模组要求（作业要模组 %d）", name, intOf(v)))
				req["module"] = -1
			}
		}
	}
	if need <= 0 {
		return
	}
	if req == nil {
		op["requirements"] = map[string]any{"elite": need}
		*notes = append(*notes, fmt.Sprintf("%s 缺练度字段，补上精英 %d", name, need))
		return
	}
	if intOf(req["elite"]) < need {
		*notes = append(*notes, fmt.Sprintf("%s 精英要求 %v → %d", name, req["elite"], need))
		req["elite"] = need
	}
}

// fixActions 清掉动作里 MaaCore 会判错的写法。
func fixActions(actions []any, notes *[]string) []any {
	out := make([]any, 0, len(actions))
	for _, a := range actions {
		m := asMap(a)
		if m == nil {
			continue
		}
		t, _ := m["type"].(string)
		if actionTypesNeedingName[t] {
			// 名字与坐标同时给时 MAA 按坐标执行，名字会被清掉——这里提前清干净
			if _, hasName := m["name"]; hasName && len(asSlice(m["location"])) > 0 {
				delete(m, "name")
				delete(m, "role")
			}
		}
		if t == "Click" {
			hasRect := len(asSlice(m["rect"])) > 0
			hasLoc := len(asSlice(m["location"])) > 0
			if hasRect && hasLoc {
				delete(m, "location") // Core 按 rect 执行
			} else if !hasRect && !hasLoc {
				*notes = append(*notes, "丢弃缺少 rect/location 的点击动作")
				continue
			}
		}
		if t == "Swipe" {
			if len(asSlice(m["begin"])) == 0 || len(asSlice(m["end"])) == 0 {
				*notes = append(*notes, "丢弃缺少 begin/end 的滑动动作")
				continue
			}
		}
		out = append(out, m)
	}
	return out
}

// ---- 泛型 JSON 小工具 ----

func asMap(v any) map[string]any {
	m, _ := v.(map[string]any)
	return m
}

func asSlice(v any) []any {
	s, _ := v.([]any)
	return s
}

// intOf 把 JSON 里的数字/字符串转 int（解析失败为 0）。
func intOf(v any) int {
	switch t := v.(type) {
	case float64:
		return int(t)
	case int:
		return t
	case json.Number:
		n, _ := t.Int64()
		return int(n)
	case string:
		n, err := strconv.Atoi(strings.TrimSpace(t))
		if err == nil {
			return n
		}
	}
	return 0
}

func maxInt(a, b int) int {
	if a > b {
		return a
	}
	return b
}
