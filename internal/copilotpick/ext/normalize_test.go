package ext

import (
	"encoding/json"
	"strings"
	"testing"
)

func charsOf(kv map[string]int) map[string]CharInfo {
	out := map[string]CharInfo{}
	for name, stars := range kv {
		out[name] = CharInfo{Stars: stars}
	}
	return out
}

// 四星写了 skill=3：MaaCore 解析会判错，必须降成 0。
func TestNormalizeSkillFix(t *testing.T) {
	raw := `{"stage_name":"main_01-07","opers":[
      {"name":"阿米娅","skill":1},
      {"name":"芬","skill":3}
    ],"actions":[]}`
	out, notes, err := NormalizeJobContent(raw, charsOf(map[string]int{"阿米娅": 5, "芬": 3}))
	if err != nil {
		t.Fatalf("修正失败：%v", err)
	}
	var c struct {
		Opers []struct {
			Name  string `json:"name"`
			Skill int    `json:"skill"`
		} `json:"opers"`
	}
	if err := json.Unmarshal([]byte(out), &c); err != nil {
		t.Fatalf("输出不是合法 JSON：%v", err)
	}
	if c.Opers[1].Skill != 0 {
		t.Errorf("三星的 skill=3 应降为 0，实际 %d", c.Opers[1].Skill)
	}
	if c.Opers[0].Skill != 1 {
		t.Errorf("三星写 skill=1 是合法的，不该动（实际 %d）", c.Opers[0].Skill)
	}
	if len(notes) != 1 || !strings.Contains(notes[0], "芬") {
		t.Errorf("修正说明应点名芬：%v", notes)
	}
}

// 六星 skill=3 合法（专三），但要求专精 → 精英下限被补到 2。
func TestNormalizeEliteFloorFromSkill(t *testing.T) {
	raw := `{"opers":[{"name":"银灰","skill":3}],"actions":[]}`
	out, notes, err := NormalizeJobContent(raw, charsOf(map[string]int{"银灰": 6}))
	if err != nil {
		t.Fatalf("修正失败：%v", err)
	}
	var c struct {
		Opers []struct {
			Requirements map[string]int `json:"requirements"`
		} `json:"opers"`
	}
	if err := json.Unmarshal([]byte(out), &c); err != nil {
		t.Fatal(err)
	}
	if c.Opers[0].Requirements["elite"] != 2 {
		t.Errorf("专三要求精二，应补 elite=2，实际 %v", c.Opers[0].Requirements)
	}
	if len(notes) == 0 {
		t.Error("补默认要求应留修正说明")
	}
}

// 写了 skill_level 9 但精英要求是 0：精英下限抬到 2（保留其他键）。
func TestNormalizeEliteFloorFromSkillLevel(t *testing.T) {
	raw := `{"opers":[{"name":"银灰","skill":0,"requirements":{"elite":0,"level":50,"skill_level":9}}],"actions":[]}`
	out, _, err := NormalizeJobContent(raw, charsOf(map[string]int{"银灰": 6}))
	if err != nil {
		t.Fatal(err)
	}
	var c struct {
		Opers []struct {
			Requirements map[string]int `json:"requirements"`
		} `json:"opers"`
	}
	if err := json.Unmarshal([]byte(out), &c); err != nil {
		t.Fatal(err)
	}
	req := c.Opers[0].Requirements
	if req["elite"] != 2 || req["level"] != 50 || req["skill_level"] != 9 {
		t.Errorf("只该抬 elite，其余键要原样保留：%v", req)
	}
}

// 动作里 MaaCore 会判错的三类写法：清名字、丢坏 Click、丢坏 Swipe。
func TestNormalizeActions(t *testing.T) {
	raw := `{"opers":[],"actions":[
      {"type":"Skill","name":"银灰","location":[100,200],"skill":3},
      {"type":"Click","rect":[0,0,10,10],"location":[5,5]},
      {"type":"Click"},
      {"type":"Swipe","begin":[1,1]},
      {"type":"Swipe","begin":[1,1],"end":[2,2]}
    ]}`
	out, notes, err := NormalizeJobContent(raw, nil)
	if err != nil {
		t.Fatal(err)
	}
	var c struct {
		Actions []map[string]any `json:"actions"`
	}
	if err := json.Unmarshal([]byte(out), &c); err != nil {
		t.Fatal(err)
	}
	if len(c.Actions) != 3 {
		t.Fatalf("坏动作应被丢弃，剩 3 条，实际 %d 条", len(c.Actions))
	}
	if _, has := c.Actions[0]["name"]; has {
		t.Error("同时给名字与坐标的 Skill 动作要清掉名字（MAA 按坐标执行）")
	}
	if _, has := c.Actions[1]["location"]; has {
		t.Error("Click 同时有 rect 与 location 时应删 location（Core 按 rect）")
	}
	if len(notes) != 2 {
		t.Errorf("两个坏动作各留一条说明，实际 %v", notes)
	}
}

// 名册里没有的干员（装置/召唤物）原样放过，不改也不报错。
func TestNormalizeUnknownOperUntouched(t *testing.T) {
	raw := `{"opers":[{"name":"装置","skill":3}],"actions":[]}`
	out, notes, err := NormalizeJobContent(raw, charsOf(map[string]int{"银灰": 6}))
	if err != nil {
		t.Fatal(err)
	}
	if len(notes) != 0 {
		t.Errorf("名册外的条目不该产生修正：%v", notes)
	}
	var c struct {
		Opers []struct {
			Skill int `json:"skill"`
		} `json:"opers"`
	}
	if err := json.Unmarshal([]byte(out), &c); err != nil {
		t.Fatal(err)
	}
	if c.Opers[0].Skill != 3 {
		t.Errorf("名册外条目不该被改：skill=%d", c.Opers[0].Skill)
	}
}

func TestNormalizeKeepsUnknownFields(t *testing.T) {
	raw := `{"stage_name":"act55side_09","doc":{"title":"时间啊，向前！"},"opers":[{"name":"银灰","skill":1}],"actions":[],"extra":{"k":1}}`
	out, _, err := NormalizeJobContent(raw, nil)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(out, "act55side_09") || !strings.Contains(out, "时间啊，向前！") ||
		!strings.Contains(out, "\"extra\"") {
		t.Errorf("修正不能丢字段：%s", out)
	}
}

func TestNormalizeBadJSON(t *testing.T) {
	if _, _, err := NormalizeJobContent("{not json", nil); err == nil {
		t.Fatal("坏 JSON 应报错")
	}
}

func TestJobTitleAndStageName(t *testing.T) {
	raw := `{"stage_name":"act55side_09","doc":{"title":" 时间啊，向前！ "}}`
	if got := JobTitle(raw); got != "时间啊，向前！" {
		t.Errorf("标题解析不对：%q", got)
	}
	if got := JobStageName(raw); got != "act55side_09" {
		t.Errorf("关卡代码解析不对：%q", got)
	}
	if JobTitle("{bad") != "" || JobStageName("") != "" {
		t.Error("坏输入应返回空串")
	}
}
