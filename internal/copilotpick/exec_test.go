package copilotpick

import (
	"encoding/json"
	"image"
	"image/color"
	"image/png"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/xiachk083-hub/maa-cli-fleet/internal/copilotpick/model"
)

// 真机（2026-11-20，机端 44 号）实测的「假成功」日志：有 TaskChainCompleted，
// 但游戏停在「接管作战」确认框上，一点理智没动。
const fakeSuccessLog = `[05:03:03.604][INF][Px29064][Tx3828] Assistant::append_callback | SubTaskError {"class":"asst::ProcessTask","details":{},"first":["BattleStartPre"],"pre_task":"","subtask":"ProcessTask","taskchain":"Copilot","taskid":1,"uuid":"x"}
[05:03:03.610][INF][Px29064][Tx3828] Assistant::append_callback | TaskChainCompleted {"taskchain":"Copilot","taskid":1,"uuid":"x"}`

// 真打完的形态：完成回调 + 掉落识别。
const realSuccessLog = `[05:14:02.100][INF][Px1][Tx2] Assistant::append_callback | SubTaskExtraInfo {"class":"asst::StageDropsTask","details":{},"taskchain":"Copilot"}
[05:14:02.500][INF][Px1][Tx2] Assistant::append_callback | TaskChainCompleted {"taskchain":"Copilot","taskid":1,"uuid":"y"}`

// 日志层面只能证明任务链跑完了；0 星结算要靠 verifySettlement 看星（真机截图）。
func TestCopilotOutcomeFakeSuccess(t *testing.T) {
	ok, _ := copilotOutcome(fakeSuccessLog)
	if !ok {
		t.Fatal("任务链完成时日志层面应判为「链跑完了」；假成功由结算截图兜")
	}
}

// 回归：MAA 启动时的 StageDropsConfig 加载日志不能被当成「打完了」。
func TestBattleSettledIgnoresStageDropsConfig(t *testing.T) {
	log := `[22:28:42.327][TRC] asst::StageDropsConfig :: asst::AbstractConfig::load | enter`
	if battleSettled(log) {
		t.Fatal("StageDropsConfig 是启动加载日志，不能算战斗结算证据")
	}
}

// MAA 自己判到「任务失败」时必须判失败（FightMissionFailed 是硬证据）。
func TestCopilotOutcomeMissionFailed(t *testing.T) {
	log := realSuccessLog + "\n" +
		`[23:15:57.042][INF][Px1][Tx2] Assistant::append_callback | SubTaskCompleted {"class":"asst::ProcessTask","details":{"result":{"text":"任务失败"},"task":"FightMissionFailed"},"first":["Copilot@WaitUntilEndOfAction"]}` + "\n" +
		`[23:16:24.790][INF][Px1][Tx2] Assistant::append_callback | TaskChainError {"taskchain":"Copilot","taskid":1}`
	ok, why := copilotOutcome(log)
	if ok {
		t.Fatal("FightMissionFailed 不能判成功")
	}
	if !strings.Contains(why, "作战失败") {
		t.Errorf("错误信息要点出作战失败，实际：%s", why)
	}
}

// 结算页判定：亮白三星徽章 → 通关；暗屏且徽章/蓝星都不亮 → 失败；
// 全黑帧（过场）与亮屏（地图）都不能下结论。
func TestAnalyzeSettlementFrame(t *testing.T) {
	dir := t.TempDir()
	badge := settlementBadgeROI

	write := func(name string, base uint8, paint func(img *image.RGBA)) string {
		img := image.NewRGBA(image.Rect(0, 0, 1280, 720))
		bg := color.RGBA{base, base, base, 0xff}
		for y := 0; y < 720; y++ {
			for x := 0; x < 1280; x++ {
				img.Set(x, y, bg)
			}
		}
		if paint != nil {
			paint(img)
		}
		path := filepath.Join(dir, name)
		f, err := os.Create(path)
		if err != nil {
			t.Fatal(err)
		}
		defer f.Close()
		if err := png.Encode(f, img); err != nil {
			t.Fatal(err)
		}
		return path
	}

	// 通关：暗底 + 徽章区亮白；蓝星区也可以是蓝的
	win := write("win.png", 60, func(img *image.RGBA) {
		white := color.RGBA{0xf0, 0xf0, 0xf0, 0xff}
		for y := badge[1]; y < badge[3]; y++ {
			for x := badge[0]; x < badge[2]; x++ {
				img.Set(x, y, white)
			}
		}
	})
	f, err := analyzeFrame(win)
	if err != nil {
		t.Fatal(err)
	}
	if f.badgeBright < settlementBadgeBright {
		t.Fatalf("通关帧徽章亮像素应 ≥ %d，实际 %d", settlementBadgeBright, f.badgeBright)
	}
	if f.mean > 100 || f.mean < 18 {
		t.Fatalf("通关帧平均亮度应在 18~100，实际 %.1f", f.mean)
	}

	// 0 星：暗屏，徽章区不亮
	lose := write("lose.png", 45, nil)
	if f, err = analyzeFrame(lose); err != nil {
		t.Fatal(err)
	}
	if f.badgeBright >= settlementBadgeBright || f.blue >= settlementBlueThreshold {
		t.Fatalf("0 星帧不该被判通关：badge=%d blue=%d", f.badgeBright, f.blue)
	}

	// 过场黑帧
	blank := write("blank.png", 3, nil)
	if f, err = analyzeFrame(blank); err != nil {
		t.Fatal(err)
	}
	if f.mean >= 18 {
		t.Fatalf("全黑帧平均亮度应 < 18，实际 %.1f", f.mean)
	}

	// 亮屏（地图）：mean 高，不能当结算
	bright := write("bright.png", 200, nil)
	if f, err = analyzeFrame(bright); err != nil {
		t.Fatal(err)
	}
	if f.mean <= 100 {
		t.Fatalf("亮屏帧平均亮度应 > 100，实际 %.1f", f.mean)
	}
}

func TestCopilotOutcomeRealSuccess(t *testing.T) {
	ok, why := copilotOutcome(realSuccessLog)
	if !ok {
		t.Fatalf("有掉落识别应判成功，实际：%s", why)
	}
}

func TestCopilotOutcomeChainError(t *testing.T) {
	log := realSuccessLog + "\n" +
		`[05:14:03.000][INF] Assistant::append_callback | TaskChainError {"taskchain":"Copilot","taskid":1}`
	ok, why := copilotOutcome(log)
	if ok || !strings.Contains(why, "任务链报错") {
		t.Fatalf("任务链报错必须判失败，实际 ok=%v why=%s", ok, why)
	}
}

func TestCopilotOutcomeNoCompleted(t *testing.T) {
	ok, why := copilotOutcome(`{"taskchain":"Copilot","details":{}}`)
	if ok || !strings.Contains(why, "完成回调") {
		t.Fatalf("没有完成回调应判失败，实际 ok=%v why=%s", ok, why)
	}
}

func TestBattleSettledMarkers(t *testing.T) {
	cases := map[string]bool{
		"asst::StageDropsTask":    true,
		"BattleFinish":            true,
		"战斗结束":                    true,
		"Copilot@EndOfAction":     true,
		"asst::StageDropsConfig":  false,
		"only TaskChainCompleted": false,
	}
	for text, want := range cases {
		if got := battleSettled(text); got != want {
			t.Errorf("battleSettled(%q)=%v，期望 %v", text, got, want)
		}
	}
}

// 默认走作业集模式；只有配置成 uri / file 才回退旧入口。
func TestCopilotMode(t *testing.T) {
	r := &Run{m: &Manager{}}
	if !r.useJobListMode() || r.useURIMode() {
		t.Error("默认应为 joblist 模式")
	}
	r.m.cfg.CopilotMode = "uri"
	if !r.useURIMode() || r.useJobListMode() {
		t.Error("显式 uri 应走旧 maa copilot 入口")
	}
	r.m.cfg.CopilotMode = "FILE"
	if r.useURIMode() || r.useJobListMode() {
		t.Error("file 模式（大小写不敏感）既不是 uri 也不是 joblist")
	}
	r.m.cfg.CopilotMode = "tasklist"
	if !r.useJobListMode() {
		t.Error("tasklist 是 joblist 的别名")
	}
}

// 作业集任务文件必须把单份作业包成 copilot_list，MaaCore 才会走多作业导航分支。
func TestJobListTaskJSON(t *testing.T) {
	jobFile := filepath.Join(t.TempDir(), "job-7.json")
	if err := os.WriteFile(jobFile, []byte(`{"stage_name":"x"}`), 0o644); err != nil {
		t.Fatal(err)
	}
	opts := model.RunOptions{
		Formation:          true,
		AddTrust:           true,
		IgnoreRequirements: true,
	}
	b, err := jobListTaskJSON(opts, "TL-9", jobFile, false)
	if err != nil {
		t.Fatalf("生成任务文件失败：%v", err)
	}
	var got maaTaskFile
	if err := json.Unmarshal(b, &got); err != nil {
		t.Fatalf("任务文件不是合法 JSON：%v", err)
	}
	if len(got.Tasks) != 1 || got.Tasks[0].Type != "Copilot" {
		t.Fatalf("应只有一条 Copilot 任务，实际：%+v", got.Tasks)
	}
	p := got.Tasks[0].Params
	if !p.Formation || !p.AddTrust || !p.IgnoreRequirements {
		t.Errorf("编队参数没带全：%+v", p)
	}
	// maa-cli 的任务文件 Copilot 段不认 support_unit_usage，必须保持 0（手动模式才用 CLI 传）。
	if p.SupportUnitUsage != 0 {
		t.Errorf("作业集任务文件不应带 support_unit_usage：%+v", p)
	}
	if len(p.CopilotList) != 1 {
		t.Fatalf("应只有一条 copilot_list 项，实际：%+v", p.CopilotList)
	}
	item := p.CopilotList[0]
	if item.StageName != "TL-9" || item.IsRaid || item.ID != 0 {
		t.Errorf("导航字段不对：%+v", item)
	}
	if !filepath.IsAbs(item.Filename) || strings.Contains(item.Filename, `\`) {
		t.Errorf("文件名应为绝对路径且用正斜杠：%q", item.Filename)
	}
	if !strings.HasSuffix(item.Filename, "job-7.json") {
		t.Errorf("文件名没指到作业：%q", item.Filename)
	}
}

// 没编队时不该带 add_trust / ignore_requirements；缺关卡显示名时留给 MAA 按 Tile code 导航。
func TestJobListTaskJSONEdgeCases(t *testing.T) {
	jobFile := filepath.Join(t.TempDir(), "job-1.json")
	if err := os.WriteFile(jobFile, []byte(`{}`), 0o644); err != nil {
		t.Fatal(err)
	}
	b, err := jobListTaskJSON(model.RunOptions{}, "1-7", jobFile, false)
	if err != nil {
		t.Fatal(err)
	}
	var got maaTaskFile
	if err := json.Unmarshal(b, &got); err != nil {
		t.Fatal(err)
	}
	p := got.Tasks[0].Params
	if p.AddTrust || p.IgnoreRequirements {
		t.Errorf("没开自动编队不该带这些参数：%+v", p)
	}
	raw, err := jobListTaskJSON(model.RunOptions{}, "  ", jobFile, false)
	if err != nil {
		t.Fatalf("缺 stageCode 不应报错：%v", err)
	}
	if strings.Contains(string(raw), `"stage_name"`) {
		t.Errorf("stageCode 为空时不应写 stage_name：%s", raw)
	}
}


// 突袭作业要写 is_raid=true，MAA 进关后会切突袭难度。
func TestJobListTaskJSONRaid(t *testing.T) {
	jobFile := filepath.Join(t.TempDir(), "job-9.json")
	if err := os.WriteFile(jobFile, []byte(`{}`), 0o644); err != nil {
		t.Fatal(err)
	}
	b, err := jobListTaskJSON(model.RunOptions{}, "TL-EX-6", jobFile, true)
	if err != nil {
		t.Fatal(err)
	}
	var got maaTaskFile
	if err := json.Unmarshal(b, &got); err != nil {
		t.Fatal(err)
	}
	if !got.Tasks[0].Params.CopilotList[0].IsRaid {
		t.Error("突袭作业应写 is_raid=true")
	}
}

// 自动导航在章节列表里一直滑、且没进过编队 → 判为“导航找不到关”。
func TestNavFailureLog(t *testing.T) {
	navFail := `exec times exceeded the limit {"class":"asst::ProcessTask","details":{"task":"Copilot@FullStageNavigation"}}`
	if !navFailureLog(navFail) {
		t.Error("FullStageNavigation 超限且没进编队，应判导航失败")
	}
	withFormation := navFail + `\n"class":"asst::BattleFormationTask"`
	if navFailureLog(withFormation) {
		t.Error("进过编队说明导航成功，不应判导航失败")
	}
	if navFailureLog(`{"task":"Copilot@FullStageNavigation"}`) {
		t.Error("没有 exec times exceeded 不应判导航失败")
	}
}
