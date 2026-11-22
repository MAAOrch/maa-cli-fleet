package ext

import (
	"strings"
	"testing"
)

// 真实形态：MAA 本地识别（source=local）——有星级/精英/等级，没有技能与模组。
const operBoxLocal = `{
  "done": true,
  "source": "local",
  "syncTime": "2026-09-15T20:52:48.5952948+08:00",
  "own_opers": [
    {"id": "char_147_shining", "name": "闪灵", "elite": 2, "level": 90, "own": true, "potential": 3, "rarity": 6},
    {"id": "char_291_aglina", "name": "安洁莉娜", "elite": 1, "level": 50, "own": true, "potential": 0, "rarity": 6},
    {"id": "char_123_fang", "name": "芬", "elite": 0, "level": 1, "own": false, "potential": 1, "rarity": 3}
  ]
}`

func TestParseOperBoxDataLocal(t *testing.T) {
	r, err := ParseOperBoxData([]byte(operBoxLocal))
	if err != nil {
		t.Fatalf("解析失败：%v", err)
	}
	if len(r.Entries) != 2 {
		t.Fatalf("只应保留 own=true 的干员，实际 %d 条", len(r.Entries))
	}
	if r.Entries[0].Name != "闪灵" {
		t.Errorf("排序应为星级/精英/等级降序，第一条是 %q", r.Entries[0].Name)
	}
	if r.SkillKnown {
		t.Error("source=local 不含技能数据，SkillKnown 应为 false")
	}
	if r.Six != 2 {
		t.Errorf("六星数应为 2，实际 %d", r.Six)
	}
	if r.Format != FormatOperBox || r.Source != "local" || r.SyncedAt == "" {
		t.Errorf("格式/来源/识别时间没带上：%+v", r)
	}
	if len(r.Warnings) == 0 || !strings.Contains(r.Warnings[0], "source=local") {
		t.Errorf("缺「技能按已拥有处理」的警告：%v", r.Warnings)
	}
}

// source=yituliu 才带技能与模组。
func TestParseOperBoxDataYituliu(t *testing.T) {
	raw := `{"done": true, "source": "yituliu", "own_opers": [
      {"id": "char_147_shining", "name": "闪灵", "elite": 2, "level": 90, "own": true,
       "potential": 3, "rarity": 6, "mainSkillLevel": 7,
       "skills": [{"id": "skchr_shining_2", "level": 3}],
       "equips": [{"id": "uniequip_001_shining", "level": 2}]}
    ]}`
	r, err := ParseOperBoxData([]byte(raw))
	if err != nil {
		t.Fatalf("解析失败：%v", err)
	}
	e := r.Entries[0]
	if !r.SkillKnown || !e.SkillKnown {
		t.Error("source=yituliu 应视为技能数据已知")
	}
	if e.SkillLevel != 7 || len(e.Skills) != 1 || e.Skills[0] != 3 {
		t.Errorf("技能等级/专精没解析出来：%+v", e)
	}
	if e.Modules[1] != 2 {
		t.Errorf("模组槽位 1 应为 2 级，实际 %v", e.Modules)
	}
	if len(r.Warnings) != 0 {
		t.Errorf("有技能数据时不该有警告：%v", r.Warnings)
	}
}

func TestParseOperBoxRejectsIncomplete(t *testing.T) {
	_, err := ParseOperBoxData([]byte(`{"done": false, "own_opers": []}`))
	if err == nil || !strings.Contains(err.Error(), "未完成") {
		t.Fatalf("识别未完成时应报错并说清楚，实际：%v", err)
	}
}

func TestParseOperBoxLegacyArray(t *testing.T) {
	// 2025-05-29 前是裸数组
	raw := `[{"id": "char_147_shining", "name": "闪灵", "elite": 2, "level": 90, "own": true, "rarity": 6}]`
	r, err := ParseOperBoxData([]byte(raw))
	if err != nil {
		t.Fatalf("旧格式应兼容：%v", err)
	}
	if len(r.Entries) != 1 || r.Source != "local" {
		t.Errorf("旧格式解析不对：%+v", r)
	}
}

func TestParseClipboard(t *testing.T) {
	raw := `[
      {"Name": "银灰", "Rarity": 6, "Elite": 2, "Level": 90, "Own": true, "Potential": 2, "Id": "char_172_svrash"},
      {"Name": "克洛丝", "Rarity": 3, "Elite": 1, "Level": 55, "Own": false}
    ]`
	r, err := ParseRoster(raw)
	if err != nil {
		t.Fatalf("解析剪贴板失败：%v", err)
	}
	if r.Format != FormatClipboard || len(r.Entries) != 1 {
		t.Fatalf("应只保留 Own=true 的一条：format=%s count=%d", r.Format, len(r.Entries))
	}
	e := r.Entries[0]
	if e.Name != "银灰" || e.Rarity != 6 || e.Elite != 2 || e.Level != 90 || e.Potential != 2 {
		t.Errorf("字段没解析对：%+v", e)
	}
	if e.OperID != "char_172_svrash" {
		t.Errorf("charId 没取到：%q", e.OperID)
	}
	if r.SkillKnown {
		t.Error("剪贴板不含技能数据，SkillKnown 应为 false")
	}
}

func TestParseRosterNames(t *testing.T) {
	r, err := ParseRoster("银灰\r\n艾雅法拉\r\n\n能天使\n")
	if err != nil {
		t.Fatalf("解析名单失败：%v", err)
	}
	if r.Format != FormatNames || len(r.Entries) != 3 {
		t.Fatalf("应是 3 个纯名字：format=%s count=%d", r.Format, len(r.Entries))
	}
	if len(r.Warnings) == 0 || !strings.Contains(r.Warnings[0], "无法核对练度") {
		t.Errorf("纯名单应提示无法核对练度：%v", r.Warnings)
	}
}

func TestParseRosterTable(t *testing.T) {
	raw := "| 干员 | 星级 | 精英 | 等级 |\n|---|---|---|---|\n| 银灰 | 6 | 2 | 90 |\n| 艾雅法拉 | 6 | 2 | 60 |\n"
	r, err := ParseRoster(raw)
	if err != nil {
		t.Fatalf("解析表格失败：%v", err)
	}
	if r.Format != FormatTable || len(r.Entries) != 2 {
		t.Fatalf("表格解析不对：format=%s count=%d", r.Format, len(r.Entries))
	}
	got := r.Entries[0]
	if got.Name != "银灰" || got.Rarity != 6 || got.Elite != 2 || got.Level != 90 {
		t.Errorf("列没对上：%+v", got)
	}
	if r.SkillKnown {
		t.Error("表格没有技能列，SkillKnown 应为 false")
	}
}

func TestParseRosterEmpty(t *testing.T) {
	if _, err := ParseRoster("   \n\t "); err == nil {
		t.Fatal("空内容应报错")
	}
}

func TestRosterIndexAndCharInfos(t *testing.T) {
	r, err := ParseOperBoxData([]byte(operBoxLocal))
	if err != nil {
		t.Fatal(err)
	}
	idx := RosterIndex(r)
	if _, ok := idx["闪灵"]; !ok {
		t.Error("索引里应有闪灵")
	}
	// 名字带空格也要能命中（作业站的名字偶尔带空格）
	if _, ok := idx[NormName(" 闪 灵 ")]; !ok {
		t.Error("归一化名字没生效")
	}
	infos := CharInfos(r)
	if infos["闪灵"].Stars != 6 || infos["闪灵"].ID != "char_147_shining" {
		t.Errorf("CharInfos 不对：%+v", infos["闪灵"])
	}
}
