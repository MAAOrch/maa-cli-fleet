package copilotpick

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"image/png"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"github.com/xiachk083-hub/maa-cli-fleet/internal/copilotpick/ext"
	"github.com/xiachk083-hub/maa-cli-fleet/internal/copilotpick/model"
)

// run 是执行主循环：写隔离 profile → 确保游戏 → 开始唤醒 → 逐份作业尝试。
//
// 失败不中断：一份作业跑挂（作业 4 分钟没打完、编队失败、MaaCore 报错）就换下一份，
// 直到 MaxAttempts 用完。每份作业开跑前都截一张图存进 run 目录当证据。
//
// 导航来源随 copilotMode 变化：
//
//	joblist（默认）  每份作业包成单条 copilot_list 走 `maa run`，由 MAA 自己导航；
//	uri / file      旧行为：`maa copilot` 单作业模式不导航，得先 fight-kill 抢导航。
func (r *Run) run() {
	defer func() {
		if p := recover(); p != nil {
			msg := fmt.Sprintf("执行器内部错误：%v", p)
			r.event("error", "status", msg)
			r.finish("failed", msg)
		}
	}()

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() {
		select {
		case <-r.stopCh:
			cancel()
		case <-ctx.Done():
		}
	}()

	r.event("info", "status", "任务开始")

	// 不管哪种模式，先把非目标服的舟游客户端 force-stop：MaaCore/MaaFw 的模板匹配不认客户端，
	// 别的服在前台就会被当成目标号唤醒（2026-11-22 05:32 官服白打 3 场）。
	if note, err := isolateClient(ctx, r.m.cfg, r.adb, r.opts.Client); err == nil && note != "" {
		r.event("info", "status", note)
	}

	// 预检：adb 截不到图就直接收工。否则模拟器掉线时会把整队作业一次性烧光
	// （2026-11-21 23:41 实测：模拟器崩了，5 份作业在 1 秒内全判失败）。
	preflight := filepath.Join(r.dir, "preflight.png")
	if err := r.m.Screenshot(ctx, r.adb, preflight); err != nil {
		r.finish("failed", "运行环境异常：adb 截图失败（模拟器没起或 adb 掉线），先把模拟器拉起来再跑："+err.Error())
		return
	}

	profile, err := r.writeProfile()
	if err != nil {
		r.finish("failed", "写 MAA 连接配置失败："+err.Error())
		return
	}
	r.event("info", "status", "使用隔离 profile："+profile)

	// 手动模式的前提是「人已经把游戏停在关卡详情页」，这里绝不能 startup/导航去动界面。
	// OnMap 同理：游戏已经被 driver 停在活动地图上，startup 会把它带回主界面，
	// 之后 MAA 只会在章节列表里滑屏（活动图不在主章节列表）。
	if !r.opts.ManualMode && !r.opts.OnMap {
		r.ensureGame(ctx)
		if err := r.maaStartup(ctx, profile); err != nil {
			if r.isStopped() {
				r.finish("stopped", "")
				return
			}
			r.event("warn", "status", "开始唤醒失败（继续尝试导航）："+err.Error())
		}
	}

	if r.opts.ManualMode {
		r.event("info", "status", "手动模式：跳过导航，按当前界面直接开抄")
	} else if r.opts.OnMap {
		r.event("info", "status", "活动地图模式：停在活动图，关卡由 MAA 的 find_stage 识别并点进")
	} else if r.useJobListMode() {
		r.event("info", "status", "作业集模式：导航交给 MAA（copilot_list），不再 fight-kill 抢导航")
	}

	// 作业原件里的历史遗留错误（技能序号/精英下限/坏动作）在这里修掉再喂 MaaCore，
	// 否则会出现「作业启动失败 / 读取文件失败」——MAA 桌面端会自己修，直跑不会。
	// 注意：uri 模式由 MAA 自己下载，改不了原件；joblist / file 模式用修正版本地文件。
	chars := ext.CharInfos(r.roster)

	// 半自动多关队列：人工选好的关卡依次跑，每关失败自动换下一份作业。
	stages := r.opts.Stages
	if len(stages) == 0 { // StartRun 已补齐，这里只做防御
		stages = []model.StagePlan{{
			StageCode: r.opts.StageCode,
			StageName: r.opts.StageName,
			JobIDs:    r.opts.JobIDs,
		}}
	}
	if len(stages) > 1 {
		r.event("info", "stage", fmt.Sprintf("半自动队列：共 %d 关，逐关执行", len(stages)))
	}

	var failed []string
	for si := range stages {
		st := stages[si]
		if r.isStopped() {
			r.finish("stopped", "")
			return
		}
		// 环境坏了（模拟器掉线 / maa 起不来）：别再拿后面的关卡撞同一个错。
		if msg := r.infraError(); msg != "" {
			r.finish("failed", "运行环境异常："+msg+"；已中止本次执行，先修模拟器/adb 再重跑")
			return
		}
		r.setStage(si+1, len(stages), st.StageCode, st.StageName)
		r.event("stage", "status", fmt.Sprintf("第 %d/%d 关：%s", si+1, len(stages), st.StageCode))
		// 台账里已经 3 星的普通关别再打——白花理智，还会把「已完成」搞糊。
		if stars := r.m.StageStars(st.StageCode, "normal"); stars == 3 && !stageIsRaidOnly(st) && !r.opts.NoProgress && r.opts.CopilotFile == "" {
			r.event("warn", "stage", "台账里这关普通已经 3 星，重复打只会浪费理智（要打突袭请只排突袭作业）")
		}

		if si > 0 && !r.opts.ManualMode && !r.opts.OnMap {
			// 上一关打完/打挂后停在结算或确认框，先回主界面再让 MAA 导航。
			if err := r.maaStartup(ctx, profile); err != nil {
				r.event("warn", "status", "回主界面失败（继续尝试）："+err.Error())
			}
		}
		if !r.opts.ManualMode && !r.useJobListMode() {
			// 旧入口：单作业模式不导航，得先把界面推到关卡页。
			if err := r.navigate(ctx, profile, st.StageCode); err != nil {
				if r.isStopped() {
					r.finish("stopped", "")
					return
				}
				r.event("warn", "stage", fmt.Sprintf("第 %d 关导航失败：%s", si+1, err.Error()))
				failed = append(failed, fmt.Sprintf("%s（导航失败：%s）", st.StageCode, err.Error()))
				continue
			}
		}

		attempts := r.opts.MaxAttempts
		if attempts <= 0 || attempts > len(st.JobIDs) {
			attempts = len(st.JobIDs)
		}
		r.setProgress(0, attempts)
		raidSet := make(map[int]bool, len(st.RaidJobIDs))
		for _, id := range st.RaidJobIDs {
			raidSet[id] = true
		}

		lastErr := ""
		stageOK := false
		for i := 0; i < attempts; i++ {
			if r.isStopped() {
				r.finish("stopped", "")
				return
			}
			id := st.JobIDs[i]
			r.setProgress(i+1, attempts)
			r.event("info", "attempt", fmt.Sprintf("第 %d/%d 关 第 %d/%d 份作业：#%d%s",
				si+1, len(stages), i+1, attempts, id, raidLabel(raidSet[id])))

			// 同一关换作业前：游戏可能停在结算/确认框，回主界面再交给 MAA 导航。
			if i > 0 && r.useJobListMode() && !r.opts.ManualMode && !r.opts.OnMap {
				if err := r.maaStartup(ctx, profile); err != nil {
					r.event("warn", "status", "回主界面失败（继续尝试）："+err.Error())
				}
			}

			var content string
			if r.opts.CopilotFile != "" {
				// Plan B：直接吃本地自己写的作业 JSON，不下载、不导航。
				b, ferr := os.ReadFile(r.opts.CopilotFile)
				if ferr != nil {
					lastErr = fmt.Sprintf("读本地作业失败：%v", ferr)
					r.event("error", "attempt", lastErr)
					continue
				}
				content = string(b)
			} else {
				var err error
				content, err = r.m.Prts().FetchJobContent(ctx, id)
				if err != nil {
					lastErr = fmt.Sprintf("下载作业 #%d 失败：%v", id, err)
					r.event("warn", "attempt", lastErr+"，换下一份")
					continue
				}
			}
			r.setJob(id, ext.JobTitle(content))

			jobFile := ""
			if r.useURIMode() {
				// 旧入口：MAA 下载原件，单作业模式不导航。
				r.event("info", "attempt", fmt.Sprintf("入口 maa://%d（单作业模式：MAA 下载）", id))
			} else {
				normalized, notes, err := ext.NormalizeJobContent(content, chars)
				if err != nil {
					lastErr = fmt.Sprintf("作业 #%d 无法使用：%v", id, err)
					r.event("warn", "attempt", lastErr+"，换下一份")
					continue
				}
				for _, n := range notes {
					r.event("info", "attempt", "作业修正："+n)
				}
				jobFile = filepath.Join(r.dir, fmt.Sprintf("job-%d.json", id))
				if err := os.WriteFile(jobFile, []byte(normalized), 0o644); err != nil {
					lastErr = fmt.Sprintf("写作业文件 job-%d.json 失败：%v", id, err)
					r.event("error", "attempt", lastErr)
					continue
				}
			}
			// 作业要的干员已被记成「编不进队」：直接跳过，别浪费一次 40 秒的编队失败。
			if label, blocked := r.jobBlocked(jobFile); blocked {
				lastErr = fmt.Sprintf("作业 #%d 要 %s，这个号编不进队（已记 unavailable），跳过", id, label)
				r.event("warn", "attempt", lastErr)
				continue
			}
			r.snapShot(ctx, "作业前")

			var ok bool
			var msg string
			navFailed := false
			// 手动模式必须走单文件 copilot（不再导航），这样人停在关卡详情页就能直接抄；
			// 作业集模式会先 navigate_to_stage，把界面带走。
			if r.useJobListMode() && !r.opts.ManualMode {
				ok, navFailed, msg = r.runJobList(ctx, profile, id, jobFile, st.StageCode, raidSet[id])
			} else {
				ok, msg = r.runCopilot(ctx, profile, id, jobFile)
			}
			if ok {
				stageOK = true
				mode := "normal"
				if raidSet[id] {
					mode = "raid"
				}
				stars := 3
				if r.opts.NoProgress {
					r.event("info", "attempt", "演习/试错模式：本次通关不记台账")
				} else {
					stars = r.recordProgress(st.StageCode, mode, r.opts.OnMap)
				}
				// 普通关必须 3 星才算达标（3 星才解锁突袭/代理指挥，且结算页判星在这里最准）：
				// 1★/2★ 记进台账（记最高星），但像失败一样换下一份作业重打；突袭只认「通关」。
				if mode == "normal" && stars < 3 {
					lastErr = fmt.Sprintf("作业 #%d 只拿到 %d 星（普通关要 3 星才解锁突袭，台账已记最高星），换下一份作业重打", id, stars)
					r.event("warn", "attempt", lastErr)
					stageOK = false
					continue
				}
				break
			}
			if r.isStopped() {
				r.finish("stopped", "")
				return
			}
			if infra := r.infraError(); infra != "" {
				r.finish("failed", "运行环境异常："+infra+"；已中止本次执行（这不是作业问题），先修模拟器/adb 再重跑")
				return
			}
			if navFailed {
				// 活动 EX 关这类不在主章节列表里的关卡，MAA 作业集导航找不到；
				// 继续换作业只会重复滑屏，直接判本关失败并提示用手动模式。
				lastErr = fmt.Sprintf("自动导航找不到 %s（MAA 作业集只会在主章节列表里找关）", st.StageCode)
				r.event("error", "stage", lastErr+"；该关剩余的作业不再尝试。活动 EX 关请改用「手动模式」：先把游戏停在该关详情页再开始")
				break
			}
			lastErr = fmt.Sprintf("作业 #%d 失败：%s", id, msg)
			r.event("warn", "attempt", lastErr+"，换下一份")
		}
		if stageOK {
			r.event("info", "stage", fmt.Sprintf("第 %d 关完成：%s", si+1, st.StageCode))
			continue
		}
		if lastErr == "" {
			lastErr = "没有可尝试的作业"
		}
		failed = append(failed, fmt.Sprintf("%s（%s）", st.StageCode, lastErr))
		r.event("warn", "stage", fmt.Sprintf("第 %d 关失败：%s", si+1, st.StageCode))
	}

	if len(failed) > 0 {
		r.finish("failed", "失败关卡："+strings.Join(failed, "；"))
		return
	}
	r.finish("success", "")
}

// copilotMode 归一化配置里的执行模式。
//
//	joblist（默认，也接受 tasklist/multi）：单份作业也包成 copilot_list 走 maa run，
//	                                        MaaCore 多作业分支会自己导航到关卡；
//	uri / file                            旧行为（maa copilot 单作业模式，不导航）。
func (r *Run) copilotMode() string {
	switch strings.ToLower(strings.TrimSpace(r.m.cfg.CopilotMode)) {
	case "uri":
		return "uri"
	case "file":
		return "file"
	case "joblist", "tasklist", "multi":
		return "joblist"
	default:
		return "joblist"
	}
}

// useJobListMode 判是否用「单条作业集的 maa run」执行（导航由 MAA 自己做）。
func (r *Run) useJobListMode() bool { return r.copilotMode() == "joblist" }

// useURIMode 判是否把作业 ID 直接交给 maa copilot（旧行为，单作业模式、不导航）。
func (r *Run) useURIMode() bool { return r.copilotMode() == "uri" }

// copilotOutcome 只判日志层面「任务链跑完了、没报错」。
//
// 它**不能**判「真的打完了」：
//   - 2026-11-20 机端 44 号：MAA 在「接管作战」确认框没点时照样发 TaskChainCompleted
//     （游戏停原地、理智没动），见 docs/EVENT-PUSH-RETRO-20261106.md；
//   - 2026-11-21 本机：日志里的 "StageDrops" 每次启动都有，来自
//     `asst::StageDropsConfig::parse`（任务资源加载），拿它当结算证据等于没证据——
//     0 星的 TL-EX-4 突袭被记成「完成」，一晚 5 场全绿。
//
// 真实结果一律看 verifySettlement（结算页三颗星）。
func copilotOutcome(logText string) (bool, string) {
	// MAA 的 Copilot@WaitUntilEndOfAction 轮询时会把 `Copilot@FightMissionFailed` 列进
	// `to_be_recognized` 候选列表，日志里每场战斗都带着这两个字样。直接
	// strings.Contains(logText,"FightMissionFailed") 会把**每一场**作业集战斗都判成
	// 0 星失败（2026-11-22 05:52 OS-4 就是：结算页都到 EndOfAction 了，还被判失败、
	// 白白重打一次）。只有任务真的命中时才有不带 Copilot@ 前缀的 task 名。
	if strings.Contains(logText, `"task":"FightMissionFailed"`) ||
		strings.Contains(logText, `"task":"Copilot@FightMissionFailed"`) ||
		strings.Contains(logText, "match_templ | FightMissionFailed") {
		return false, "MAA 判到「任务失败」页：作战失败（0 星，理智会返还）"
	}
	hasChain := strings.Contains(logText, `"taskchain":"Copilot"`)
	if hasChain && strings.Contains(logText, "TaskChainError") {
		return false, "Copilot 任务链报错：" + lastErrorLines(logText, 3)
	}
	if !hasChain || !strings.Contains(logText, "TaskChainCompleted") {
		return false, "未拿到完成回调：" + lastErrorLines(logText, 4)
	}
	return true, ""
}

// 结算页判定的三块区域（1280x720 设计坐标，实际截图尺寸不同会等比缩放）：
//
//	settlementBadgeROI  活动主题的「三星徽章」位置（通关=亮白徽章，0 星=三个暗六边形）
//	settlementStarROI   通用主题的三颗蓝星位置（部分页面是蓝星）
//
// 实测（2026-11-21 本机 TL-S-1 突袭首通 / TL-EX-4 突袭 0 星 / 任务失败遮罩）：
//
//	页面              mean   badge亮像素  star蓝像素
//	通关结算          60.7   5680        0
//	0 星结算          44.4    647        0
//	「任务失败」遮罩    58.1   1115     2967（遮罩的 X 落进 star 区域，不能只看星）
//	活动地图         154.5   6541    12724（用 mean 当亮屏护栏挡掉）
var (
	settlementBadgeROI = [4]int{55, 250, 175, 395}
	settlementStarROI  = [4]int{40, 300, 290, 370}
)

const (
	// settlementBadgeBright 亮白徽章像素下限（通关 5680，0 星 647，遮罩 1115）。
	settlementBadgeBright = 3000
	// settlementBlueThreshold 通用主题三颗蓝星的像素下限。
	settlementBlueThreshold = 300
)

// verifySettlement 战斗结束后截结算页，按「三星徽章 / 蓝星」判真实结果。
//
// 两个坑（2026-11-21 实测）：
//   - 失败先弹「任务失败」再弹「行动结束（0 星）」，两者都不能算通关；
//   - MAA 退出瞬间可能在过场（截到全黑帧）或已经回到亮屏地图，必须跳过重试，
//     否则通关会被误判失败（TL-S-1 首通就这样被误判过一次）。
func (r *Run) verifySettlement(ctx context.Context) (bool, string) {
	path := filepath.Join(r.dir, "settle.png")
	lastErr := ""
	judged := 0
	for i := 0; i < 12; i++ {
		if i > 0 {
			time.Sleep(1500 * time.Millisecond)
		}
		if err := r.m.Screenshot(ctx, r.adb, path); err != nil {
			lastErr = err.Error()
			continue
		}
		f, err := analyzeFrame(path)
		if err != nil {
			lastErr = err.Error()
			continue
		}
		if f.mean > 100 || f.mean < 18 {
			continue // 亮屏（地图/主页）或过场黑帧：都不能下结论
		}
		if f.blue >= settlementBlueThreshold || f.badgeBright >= settlementBadgeBright {
			return true, ""
		}
		judged++
		if judged >= 3 { // 连续 3 帧都是「暗屏但不亮」＝0 星/失败页
			return false, "结算页三星徽章/蓝星都没亮（0 星或没打到结算），按失败处理"
		}
	}
	msg := "没截到可判定的结算页（一直黑屏/亮屏），无法确认是否通关"
	if lastErr != "" {
		msg += "；截图/读图出错：" + lastErr
	}
	return false, msg
}

// frameFeature 一帧截图在结算判定里用到的特征。
type frameFeature struct {
	blue        int     // 蓝星像素
	badgeBright int     // 三星徽章亮像素
	mean        float64 // 全图平均亮度（判黑帧/亮屏）
}

// analyzeFrame 按 1280x720 设计坐标算结算页特征。
func analyzeFrame(path string) (frameFeature, error) {
	var out frameFeature
	f, err := os.Open(path)
	if err != nil {
		return out, err
	}
	defer f.Close()
	img, err := png.Decode(f)
	if err != nil {
		return out, err
	}
	b := img.Bounds()
	if b.Dx() <= 0 || b.Dy() <= 0 {
		return out, fmt.Errorf("截图尺寸异常：%dx%d", b.Dx(), b.Dy())
	}
	sx := float64(b.Dx()) / 1280.0
	sy := float64(b.Dy()) / 720.0
	box := func(roi [4]int) (int, int, int, int) {
		return b.Min.X + int(float64(roi[0])*sx), b.Min.Y + int(float64(roi[1])*sy),
			b.Min.X + int(float64(roi[2])*sx), b.Min.Y + int(float64(roi[3])*sy)
	}

	x0, y0, x1, y1 := box(settlementStarROI)
	for y := y0; y < y1; y++ {
		for x := x0; x < x1; x++ {
			cr, cg, cb, _ := img.At(x, y).RGBA()
			rr, gg, bb := int(cr>>8), int(cg>>8), int(cb>>8)
			if bb > 150 && bb-rr > 40 && gg > 110 {
				out.blue++
			}
		}
	}
	x0, y0, x1, y1 = box(settlementBadgeROI)
	for y := y0; y < y1; y++ {
		for x := x0; x < x1; x++ {
			cr, cg, cb, _ := img.At(x, y).RGBA()
			if (int(cr>>8)+int(cg>>8)+int(cb>>8))/3 > 170 {
				out.badgeBright++
			}
		}
	}
	sum, n := 0.0, 0
	for y := b.Min.Y; y < b.Max.Y; y += 8 {
		for x := b.Min.X; x < b.Max.X; x += 8 {
			cr, cg, cb, _ := img.At(x, y).RGBA()
			sum += float64(int(cr>>8)+int(cg>>8)+int(cb>>8)) / 3.0
			n++
		}
	}
	if n > 0 {
		out.mean = sum / float64(n)
	}
	return out, nil
}

// battleSettled 判日志里有没有「打完了」的痕迹（只给刷图语义的历史日志用）。
//
// 注意：**不要往这里加 "StageDrops"**——MAA 每次启动都会打
// `asst::StageDropsConfig::parse`，加了就是 100% 假成功（2026-11-21 踩过）。
func battleSettled(logText string) bool {
	for _, marker := range []string{
		"asst::StageDropsTask", // 掉落识别任务（不是启动时的 StageDropsConfig）
		"BattleFinish",         // 战斗结束节点
		"战斗结束",
		"Copilot@EndOfAction",
	} {
		if strings.Contains(logText, marker) {
			return true
		}
	}
	return false
}


// starSlotROIs 结算页三颗星的格子（1280x720）。实测（2026-11-22）：
// 亮星每格蓝像素 ~1900~2100，未亮那格 0；突袭通关显示白色主题徽章（蓝 0、首格亮 ~2700~2800）。
var starSlotROIs = [3][4]int{{55, 298, 128, 358}, {140, 298, 212, 358}, {215, 298, 288, 358}}

// settlementStars 数结算页亮了几颗星；themed=true 表示是突袭主题徽章（数不出星数）。
func settlementStars(path string) (stars int, themed bool, err error) {
	f, err := os.Open(path)
	if err != nil {
		return 0, false, err
	}
	defer f.Close()
	img, err := png.Decode(f)
	if err != nil {
		return 0, false, err
	}
	b := img.Bounds()
	sx := float64(b.Dx()) / 1280.0
	sy := float64(b.Dy()) / 720.0
	for i, roi := range starSlotROIs {
		blue, bright := 0, 0
		for y := b.Min.Y + int(float64(roi[1])*sy); y < b.Min.Y+int(float64(roi[3])*sy); y++ {
			for x := b.Min.X + int(float64(roi[0])*sx); x < b.Min.X+int(float64(roi[2])*sx); x++ {
				cr, cg, cb, _ := img.At(x, y).RGBA()
				rr, gg, bb := int(cr>>8), int(cg>>8), int(cb>>8)
				if bb > 150 && bb-rr > 40 && gg > 110 {
					blue++
				}
				if (rr+gg+bb)/3 > 170 {
					bright++
				}
			}
		}
		if i == 0 && blue == 0 && bright >= 2000 {
			return 0, true, nil // 突袭主题徽章
		}
		if blue >= 400 {
			stars++
		}
	}
	return stars, false, nil
}

// recordProgress 把这次通关记进台账（突袭/主题徽章只记「已通关」=3），返回记下的星数。
//
// trust=true（onMap 模式）：MAA 打完会把结算页点掉回关卡图，没有 settle.png 可读，
// 任务链完成即认为 3 星（失败会走 TaskChainError，外部已经拦掉）。
// 普通模式返回真实星数，供调用方判「是否够 3 星」（1★/2★ 要继续换作业）。
func (r *Run) recordProgress(code, mode string, trust bool) int {
	if trust {
		r.m.RecordProgress(code, mode, 3)
		r.event("info", "stage", fmt.Sprintf("进度已记：%s %s 3 星（活动地图模式，按任务链完成）", code, modeLabel(mode)))
		return 3
	}
	stars, themed, err := settlementStars(filepath.Join(r.dir, "settle.png"))
	if err != nil {
		r.event("warn", "stage", "进度没记上（读结算图失败）："+err.Error())
		return 0
	}
	if mode == "raid" || themed {
		stars = 3
	}
	r.m.RecordProgress(code, mode, stars)
	r.event("info", "stage", fmt.Sprintf("进度已记：%s %s %s", code, modeLabel(mode), progressLabel(mode, stars)))
	return stars
}

// modeLabel 普通/突袭的中文名。
func modeLabel(mode string) string {
	if mode == "raid" {
		return "突袭"
	}
	return "普通"
}

// stageIsRaidOnly 判这一关这次是不是只打突袭（所有作业都在 raidJobIds 里）。
func stageIsRaidOnly(st model.StagePlan) bool {
	if len(st.JobIDs) == 0 || len(st.RaidJobIDs) == 0 {
		return false
	}
	raid := map[int]bool{}
	for _, id := range st.RaidJobIDs {
		raid[id] = true
	}
	for _, id := range st.JobIDs {
		if !raid[id] {
			return false
		}
	}
	return true
}

// isStopped 判用户是否点了停止。
func (r *Run) isStopped() bool {
	select {
	case <-r.stopCh:
		return true
	default:
		return false
	}
}

// markInfra 记下运行环境故障（模拟器掉线 / maa 起不来），整轮随后中止。
func (r *Run) markInfra(msg string) {
	r.infraMu.Lock()
	if r.infra == "" {
		r.infra = msg
	}
	r.infraMu.Unlock()
}

// infraError 返回已记录的环境故障（空 = 正常）。
func (r *Run) infraError() string {
	r.infraMu.Lock()
	defer r.infraMu.Unlock()
	return r.infra
}

// clientPackage 返回各客户端在设备上的包名（用于确保游戏已启动）。
func clientPackage(client string) string {
	switch strings.ToLower(client) {
	case "bilibili":
		return "com.hypergryph.arknights.bilibili"
	case "yostarjp", "txwy":
		return "com.YoStarJP.Arknights"
	case "yostaren":
		return "com.YoStarEN.Arknights"
	case "yostarkr":
		return "com.YoStarKR.Arknights"
	default:
		return "com.hypergryph.arknights"
	}
}

// profileName 给每个实例一个隔离 profile，避免污染机队里的其它 profile。
func profileName(emu string) string { return "copilot-picker-emu" + strings.TrimSpace(emu) }

// writeProfile 写 MAA 连接配置。
func (r *Run) writeProfile() (string, error) {
	return writeMaaProfile(r.m.cfg, r.adb, r.acc.Emu)
}

// ensureGame 确保游戏进程在跑（adb monkey 拉起），不负责模拟器开关。
func (r *Run) ensureGame(ctx context.Context) {
	if ensureGameRunning(ctx, r.m.cfg, r.adb, r.opts.Client) {
		r.event("info", "status", "游戏未启动，用 adb 拉起 "+clientPackage(r.opts.Client))
	}
}

// maaStartup 启动游戏并回到主界面。
func (r *Run) maaStartup(ctx context.Context, profile string) error {
	r.setStatus("starting")
	r.event("info", "status", fmt.Sprintf("开始唤醒（%s）", r.opts.Client))
	res, err := r.runMaa(ctx, time.Duration(r.m.cfg.StartupTimeoutSec)*time.Second,
		"startup", r.opts.Client, "-p", profile, "--batch", "--no-summary")
	if err != nil {
		return err
	}
	if res.rc != 0 {
		return fmt.Errorf("maa startup 失败（rc=%d）：%s", res.rc, lastLines(res.out, 6))
	}
	r.event("info", "status", "已回到主界面")
	return nil
}

// navigate 用 fight-kill 把界面推到关卡详情页（旧 uri/file 模式用；作业集模式不走这里）。
//
// MAA 没有「只导航不开战」的 API（times=0 会整体跳过任务），所以这里用
// fight --times 1 让 MAA 完成导航，日志一出现关卡详情页标志就杀进程，
// 把游戏留在「关卡详情页」——单作业模式的 Copilot 正好可以从这里开始。
func (r *Run) navigate(ctx context.Context, profile, stageCode string) error {
	r.setStatus("navigating")
	r.event("stage", "status", "自动导航到 "+stageCode)
	if r.m.cfg.NavMode == "skip" {
		r.event("warn", "status", "配置 navMode=skip，跳过自动导航")
		return nil
	}
	// 每次导航前先回主界面，避免上一份作业失败后停在奇怪页面。
	if err := r.maaStartup(ctx, profile); err != nil {
		r.event("warn", "status", "回主界面失败（继续尝试导航）："+err.Error())
	}

	tail := newLogTail(r.m.cfg.AsstLogPath())
	tail.SkipAll()
	args := []string{"fight", stageCode, "--times", "1", "-p", profile, "--batch", "--no-summary"}
	cmd := exec.Command(r.m.cfg.MaaExe, args...)
	cmd.Env = r.m.cfg.Env()
	cmd.SysProcAttr = hideWindow()
	var out bytes.Buffer
	cmd.Stdout = &out
	cmd.Stderr = &out
	if err := cmd.Start(); err != nil {
		return fmt.Errorf("启动导航进程失败: %w", err)
	}
	r.setProc(cmd.Process)
	defer r.setProc(nil)

	markers := []string{"\"task\":\"StartButton1\"", "match_templ | StartButton1"}
	timeout := time.Duration(r.m.cfg.NavTimeoutSec) * time.Second
	deadline := time.Now().Add(timeout)
	hit := ""
	var exited error
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	select {
	case exited = <-done:
		// 进程自然结束（可能已经开打了）
	case <-r.stopCh:
		killTree(cmd.Process.Pid)
		exited = <-done
	case <-ctx.Done():
		killTree(cmd.Process.Pid)
		exited = <-done
	case <-time.After(100 * time.Millisecond):
		// 进入轮询
	}
	if exited == nil {
		for time.Now().Before(deadline) {
			select {
			case exited = <-done:
			case <-r.stopCh:
				killTree(cmd.Process.Pid)
				exited = <-done
			default:
				chunk := tail.Read()
				for _, mk := range markers {
					if strings.Contains(chunk, mk) {
						hit = mk
						break
					}
				}
				if hit != "" {
					killTree(cmd.Process.Pid)
					exited = <-done
					break
				}
				time.Sleep(80 * time.Millisecond)
			}
			if exited != nil {
				break
			}
		}
	}
	if hit == "" && exited == nil {
		killTree(cmd.Process.Pid)
		<-done
		return fmt.Errorf("导航超时（%d 秒内没到关卡详情页）", r.m.cfg.NavTimeoutSec)
	}
	if hit == "" {
		return fmt.Errorf("导航进程提前退出：%s", lastLines(out.String(), 6))
	}
	time.Sleep(1500 * time.Millisecond)
	r.snapShot(ctx, "导航后")
	r.event("info", "status", "已到达关卡详情页（标记 "+hit+"）")
	return nil
}

// runCopilot 跑一份作业（旧入口：maa copilot 单作业模式，MAA 不导航）。
//
// 注意：单作业模式（filename / maa:// 单 URI）MaaCore 不会 navigate_to_stage，
// 必须人/外部先把界面推到关卡详情页；新默认走 runJobList，不再用这里。
func (r *Run) runCopilot(ctx context.Context, profile string, jobID int, jobFile string) (bool, string) {
	target := fmt.Sprintf("maa://%d", jobID)
	if !r.useURIMode() {
		if jobFile == "" {
			return false, "file 模式缺少本地作业文件"
		}
		target = jobFile
	}
	args := []string{"copilot", target, "-p", profile, "--batch"}
	if r.opts.Formation {
		args = append(args, "--formation")
		if r.opts.AddTrust {
			args = append(args, "--add-trust")
		}
		if r.opts.IgnoreRequirements {
			args = append(args, "--ignore-requirements")
		}
	}
	if r.opts.SupportUnitUsage > 0 {
		args = append(args, "--support-unit-usage", fmt.Sprint(r.opts.SupportUnitUsage))
	}
	tail := newLogTail(r.m.cfg.AsstLogPath())
	tail.SkipAll()
	res, err := r.runMaa(ctx, time.Duration(r.opts.TimeoutMin)*time.Minute, args...)
	logText := tail.Read()
	if err != nil {
		return false, "作业执行出错：" + err.Error()
	}
	// 完成回调只证明任务链跑完了；真结果看结算页三颗星（0 星也说「完成」是历史 bug）。
	ok, why := copilotOutcome(logText)
	if !ok {
		r.learnUnavailable(jobFile, logText)
		return false, fmt.Sprintf("%s（maa rc=%d）", why, res.rc)
	}
	if settled, why := r.verifySettlement(ctx); !settled {
		return false, why
	}
	return true, ""
}

// maa 任务文件（tasks/<name>.json）的形状。maa-cli 的 `maa run <TASK>` 会读它。
type maaTaskFile struct {
	Tasks []maaTask `json:"tasks"`
}

type maaTask struct {
	Type   string           `json:"type"`
	Params maaCopilotParams `json:"params"`
}

type maaCopilotParams struct {
	Formation          bool             `json:"formation"`
	AddTrust           bool             `json:"add_trust,omitempty"`
	IgnoreRequirements bool             `json:"ignore_requirements,omitempty"`
	SupportUnitUsage   int              `json:"support_unit_usage,omitempty"`
	CopilotList        []maaCopilotItem `json:"copilot_list"`
}

// maaCopilotItem 对应 MaaCore CopilotTask 的 copilot_list 单项。
// stage_name 是导航用的关卡显示名；留空时 MaaCore 按 Tile 表里的关卡 code 导航。
type maaCopilotItem struct {
	ID        int    `json:"id"`
	Filename  string `json:"filename"`
	StageName string `json:"stage_name,omitempty"`
	IsRaid    bool   `json:"is_raid"`
}

// jobListTaskJSON 生成「单份作业的作业集」任务文件内容。
//
// 为什么必须包成 copilot_list（哪怕只有一份作业）：MaaCore 的 Copilot 任务
// 只有多作业分支（MultiCopilotTaskPlugin）会先 navigate_to_stage；单作业
// （filename）分支要求人已经在关卡详情页。单条 copilot_list 一样走多作业分支。
//
// isRaid=true 时 MAA 会在进关后切突袭难度（作业站 difficulty=3 的作业）。
func jobListTaskJSON(opts model.RunOptions, stageCode, jobFile string, isRaid bool) ([]byte, error) {
	abs, err := filepath.Abs(jobFile)
	if err != nil {
		return nil, err
	}
	params := maaCopilotParams{
		Formation: opts.Formation,
		CopilotList: []maaCopilotItem{{
			ID:        0,
			Filename:  filepath.ToSlash(abs),
			StageName: strings.TrimSpace(stageCode),
			IsRaid:    isRaid,
		}},
	}
	if opts.Formation {
		params.AddTrust = opts.AddTrust
		params.IgnoreRequirements = opts.IgnoreRequirements
	}
	// 注意：maa-cli 的任务文件 Copilot 段不认 support_unit_usage（会直接加任务失败），
	// 助战只在手动模式的单文件 copilot 里用 --support-unit-usage 传。
	return json.MarshalIndent(maaTaskFile{Tasks: []maaTask{{Type: "Copilot", Params: params}}}, "", "  ")
}

// raidLabel 给事件日志加个「突袭」后缀。
func raidLabel(isRaid bool) string {
	if isRaid {
		return "（突袭）"
	}
	return ""
}

// navLoopWatch 盯作业集模式的导航死循环：MAA 在章节列表里反复滑（活动 EX/S 关不在
// 列表里）时，15s 采样一次，命中就把 maa 进程掐掉，并打上 navStuck 标记。
// 日志尾巴用独立的 logTail，不抢主流程的偏移。
func (r *Run) navLoopWatch(ctx context.Context, stop <-chan struct{}) {
	watch := newLogTail(r.m.cfg.AsstLogPath())
	watch.SkipAll()
	ticker := time.NewTicker(15 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-stop:
			return
		case <-ticker.C:
			chunk := watch.Read()
			if chunk == "" {
				continue
			}
			swipe := strings.Count(chunk, "ChapterSwipeToTheRightAndPlot") +
				strings.Count(chunk, "StageNavigationSlowlySwipeLeft")
			nav := strings.Count(chunk, "Copilot@FullStageNavigation")
			if swipe+nav < 8 {
				continue
			}
			r.infraMu.Lock()
			r.navStuckFlag = true
			r.infraMu.Unlock()
			r.event("warn", "attempt", "检测到导航死循环（章节列表滑屏），提前中止这份作业")
			if p := r.currentProc(); p != nil {
				_ = p.Kill()
			}
			return
		}
	}
}

// navStuck 判这一轮是不是被导航看门狗掐掉的。
func (r *Run) navStuck() bool {
	r.infraMu.Lock()
	defer r.infraMu.Unlock()
	return r.navStuckFlag
}

// currentProc 取当前 maa 子进程（没有则 nil）。
func (r *Run) currentProc() *os.Process {
	r.procMu.Lock()
	defer r.procMu.Unlock()
	return r.proc
}

// navFailureLog 判这次失败是不是「MAA 在章节列表里一直滑、找不到关卡」。
//
// 典型（2026-11-21 本机 TL-EX-6）：
//
//	Copilot@FullStageNavigation 连续 10 次 exec times exceeded
//	Copilot@ChapterSwipeToTheRightAndPlot 超限
//	而且没进入 BattleFormationTask（说明根本没到关卡详情页）
func navFailureLog(logText string) bool {
	if !strings.Contains(logText, "Copilot@FullStageNavigation") &&
		!strings.Contains(logText, "Copilot@ChapterSwipeToTheRightAndPlot") {
		return false
	}
	navExceeded := strings.Contains(logText, "FullStageNavigation") &&
		strings.Contains(logText, "exec times exceeded")
	if !navExceeded {
		return false
	}
	// 如果已经进过编队/战斗，说明导航成功过，只是战斗失败。
	return !strings.Contains(logText, "BattleFormationTask")
}

// writeJobListTask 把当前尝试写成 maa-cli 任务文件，返回任务名与路径。
// 任务名按 run ID 唯一，避免同一 config 目录里并发 run 互相覆盖。
func (r *Run) writeJobListTask(jobFile, stageCode string, isRaid bool) (name, path string, err error) {
	b, err := jobListTaskJSON(r.opts, stageCode, jobFile, isRaid)
	if err != nil {
		return "", "", err
	}
	dir := filepath.Join(r.m.cfg.MaaConfigDir, "tasks")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return "", "", err
	}
	name = "cp-" + r.ID
	path = filepath.Join(dir, name+".json")
	if err := os.WriteFile(path, b, 0o644); err != nil {
		return "", "", err
	}
	return name, path, nil
}

// runJobList 用作业集模式执行一份作业：导航、编队、开战、结算全交给 MAA。
// 第三个返回值表示「是不是卡在自动导航找不到关卡」。
func (r *Run) runJobList(ctx context.Context, profile string, jobID int, jobFile, stageCode string, isRaid bool) (bool, bool, string) {
	if jobFile == "" {
		return false, false, "作业集模式缺少本地作业文件"
	}
	name, path, err := r.writeJobListTask(jobFile, stageCode, isRaid)
	if err != nil {
		return false, false, "写作业集任务文件失败：" + err.Error()
	}
	defer os.Remove(path)

	r.setStatus("running")
	r.event("info", "attempt", fmt.Sprintf("作业 #%d 走作业集入口：maa run %s%s（MAA 自己导航＋执行）",
		jobID, name, raidLabel(isRaid)))
	tail := newLogTail(r.m.cfg.AsstLogPath())
	tail.SkipAll()
	// 导航看门狗：活动 EX/S 关不在 MAA 的主章节列表里，`navigate_to_stage` 会一直
	// ChapterSwipeToTheRightAndPlot / StageNavigationSlowlySwipeLeft 死循环。2026-11-21
	// 实测一次卡到 20 分钟超时（TL-S-2，滑屏 160 次）。这里发现循环就掐掉。
	watchStop := make(chan struct{})
	go r.navLoopWatch(ctx, watchStop)
	res, err := r.runMaa(ctx, time.Duration(r.opts.TimeoutMin)*time.Minute,
		"run", name, "-p", profile, "--batch", "--no-summary")
	close(watchStop)
	logText := tail.Read()
	if r.navStuck() {
		return false, true, "MAA 在章节列表里找不到该关（活动 EX/S 图不在主章节列表），" +
			"已提前中止；这条线请用「手动模式 + tools/picker_push」推"
	}
	if err != nil {
		return false, false, "作业集执行出错：" + err.Error()
	}
	ok, why := copilotOutcome(logText)
	if !ok {
		r.learnUnavailable(jobFile, logText)
		return false, navFailureLog(logText), fmt.Sprintf("%s（maa rc=%d）", why, res.rc)
	}
	// 活动地图模式（OnMap）：MAA 的 Copilot 链打完会自己把结算页点掉、回到关卡图，
	// 等这里再截图只剩亮屏地图，verifySettlement 永远判「没截到结算页」
	// （2026-11-22 06:06 OS-4 实测）。这条线只信任务链：MAA 的 Copilot 在 0 星时会
	// 命中 Copilot@FightMissionFailed 报 TaskChainError（上面已经拦掉），能走到
	// TaskChainCompleted 就是真打完了。
	if r.opts.OnMap {
		r.event("info", "stage", "活动地图模式：结算页由 MAA 自己点掉，按任务链完成判定")
		return true, false, ""
	}
	// 作业集模式同样以结算页三颗星为准（MAA 只在日志里报任务链完成）。
	if settled, why := r.verifySettlement(ctx); !settled {
		return false, false, why
	}
	return true, false, ""
}

// runMaa 执行一次 maa-cli 命令，记录到事件流，可被 Stop 中断。
func (r *Run) runMaa(ctx context.Context, timeout time.Duration, args ...string) (cmdResult, error) {
	cctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	cmd := exec.CommandContext(cctx, r.m.cfg.MaaExe, args...)
	cmd.Env = r.m.cfg.Env()
	cmd.SysProcAttr = hideWindow()
	var buf bytes.Buffer
	cmd.Stdout = &buf
	cmd.Stderr = &buf
	start := time.Now()
	if err := cmd.Start(); err != nil {
		return cmdResult{}, err
	}
	r.setProc(cmd.Process)
	err := cmd.Wait()
	r.setProc(nil)
	res := cmdResult{rc: cmd.ProcessState.ExitCode(), out: buf.String(), took: time.Since(start)}
	// 0xC0000142 = STATUS_DLL_INIT_FAILED：Windows 上 maa 进程起不来（常见于模拟器/adb
	// 刚崩、系统资源烂）。这不是作业问题，记成环境故障，整轮中止。
	if uint32(res.rc) == infraBrokenRC {
		r.markInfra(fmt.Sprintf("maa 退出码 %d（0xC0000142 初始化失败），模拟器/adb 多半掉线了", res.rc))
		r.event("error", "status", "运行环境异常："+r.infraError())
	}
	if cctx.Err() == context.DeadlineExceeded {
		return res, fmt.Errorf("超时（%s）", timeout)
	}
	if err != nil && res.rc == 0 {
		res.rc = -1
	}
	return res, nil
}

// infraBrokenRC Windows 上 maa/adb 进程起不来时的退出码（STATUS_DLL_INIT_FAILED）。
const infraBrokenRC = 3221225794

type cmdResult struct {
	rc   int
	out  string
	took time.Duration
}

// setProc 记录当前子进程，供 Stop 杀。
func (r *Run) setProc(p *os.Process) {
	r.procMu.Lock()
	r.proc = p
	r.procMu.Unlock()
}

// snapShot 截当前屏幕存到 run 目录的 shot.png（失败只记日志）。
func (r *Run) snapShot(ctx context.Context, tag string) {
	p := filepath.Join(r.dir, "shot.png")
	if err := r.m.Screenshot(ctx, r.adb, p); err != nil {
		r.event("warn", "status", "截图失败："+err.Error())
	}
	_ = tag
}

// ---- 日志工具 ----

type logTail struct {
	path string
	off  int64
}

func newLogTail(path string) *logTail { return &logTail{path: path} }

// SkipAll 把偏移移到文件末尾（只关心之后的日志）。
func (t *logTail) SkipAll() {
	if st, err := os.Stat(t.path); err == nil {
		t.off = st.Size()
	}
}

// Read 读取自上次以来新增的日志。
func (t *logTail) Read() string {
	f, err := os.Open(t.path)
	if err != nil {
		return ""
	}
	defer f.Close()
	st, err := f.Stat()
	if err != nil {
		return ""
	}
	if st.Size() < t.off {
		t.off = 0
	}
	if st.Size() == t.off {
		return ""
	}
	if _, err := f.Seek(t.off, 0); err != nil {
		return ""
	}
	b := make([]byte, st.Size()-t.off)
	n, _ := f.Read(b)
	t.off += int64(n)
	return string(b[:n])
}

func lastLines(s string, n int) string {
	lines := strings.Split(strings.TrimSpace(s), "\n")
	if len(lines) > n {
		lines = lines[len(lines)-n:]
	}
	return strings.Join(lines, " | ")
}

func lastErrorLines(s string, n int) string {
	var keep []string
	for _, line := range strings.Split(s, "\n") {
		low := strings.ToLower(line)
		if strings.Contains(line, "ERROR") || strings.Contains(low, "error") ||
			strings.Contains(line, "SubTaskError") || strings.Contains(line, "失败") {
			keep = append(keep, strings.TrimSpace(line))
			if len(keep) > n*3 {
				keep = keep[len(keep)-n*3:]
			}
		}
	}
	if len(keep) == 0 {
		return lastLines(s, n)
	}
	if len(keep) > n {
		keep = keep[len(keep)-n:]
	}
	return strings.Join(keep, " | ")
}
