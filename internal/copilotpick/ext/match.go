package ext

// 作业与名册的匹配：判定「这份作业能不能直接抄 / 差一个人 / 练度不够 / 打不了」。
//
// 口径照作业站前端 models/operatorMatcher.ts 的算法（我们复刻 MAA-PRTS 的实现，语义一致）：
//
//	练度：把「精英阶段 + 等级」折算成进度分再比较，不是逐项死比
//	      （E2 1 级 = 81 分 > E1 80 级 = 80 分，和游戏里的强弱一致）
//	技能：requirements.skill_level >= 8 看该技能的专精（9 = 专三），1~7 看技能等级
//	模组：module > 0 只要求「该槽位模组已解锁（等级 >= 1）」
//	人数：> 13 人或缺 2 人以上 → blocked
//
// 两条我们自己踩出来的口径（来自 MAA-PRTS，见 docs 里的对比）：
//
//   - 作业没写练度（v1 老作业）时按星级补一个保守默认要求，并标 implied——
//     否则压级号会被判成「可打」，实际进去就翻车。
//   - 名册没有技能数据时（MAA 本地识别 / 剪贴板），技能与模组按「已拥有」处理并标
//     Fuzz，不能判成「需补练度」——数据缺失不是账号的错。

import (
	"sort"
	"strings"

	"github.com/xiachk083-hub/maa-cli-fleet/internal/copilotpick/model"
)

// 各星级的满级表（精英 0/1/2 阶段的等级上限），与游戏一致。
var maxLevels = map[int][3]int{
	0: {30, 0, 0},
	1: {30, 0, 0},
	2: {30, 0, 0},
	3: {40, 55, 0},
	4: {45, 60, 70},
	5: {50, 70, 80},
	6: {50, 80, 90},
}

// requirement 是解析后的练度要求；nil 表示作业没写这一项。
type requirement struct {
	elite      *int
	level      *int
	skillLevel *int
	module     *int
	potential  *int
}

// defaultRequirement 是「作业没写练度」时按星级补的保守默认要求。
//
//	六星：精二 60 + 专三（skill_level 9）    五星/四星：精一 60 + 技能 7
//	三星：精一 55 + 技能 7                    一星/二星：精零 30
var defaultRequirement = map[int]requirement{
	6: {elite: intp(2), level: intp(60), skillLevel: intp(9)},
	5: {elite: intp(1), level: intp(60), skillLevel: intp(7)},
	4: {elite: intp(1), level: intp(60), skillLevel: intp(7)},
	3: {elite: intp(1), level: intp(55), skillLevel: intp(7)},
	2: {elite: intp(0), level: intp(30)},
	1: {elite: intp(0), level: intp(30)},
}

// MatchResult 是一份作业与名册的匹配结论。
type MatchResult struct {
	Mode       string   // ready|borrow|train|blocked
	Missing    []string // 缺的干员（含干员组标签）
	LevelShort []string // 有但练度不够的干员（含干员组标签）
	Total      int      // 出场干员数 + 干员组数
	Implied    int      // 作业没写练度、按星级补默认要求的条目数
	Fuzz       bool     // 名册缺技能数据，技能/模组按已拥有处理
}

// Met 表示「可直接抄」。
func (r MatchResult) Met() bool { return r.Mode == "ready" }

// EvaluateJob 比对一份作业与名册。
func EvaluateJob(raw RawJob, roster model.Roster) MatchResult {
	idx := RosterIndex(roster)
	res := MatchResult{}
	for _, op := range raw.Opers {
		name := NormName(op.Name)
		if name == "" {
			continue
		}
		owned, ok := idx[name]
		if !ok {
			res.Missing = append(res.Missing, op.Name)
			continue
		}
		met, implied, fuzz := requirementMet(op, owned)
		if implied {
			res.Implied++
		}
		if fuzz {
			res.Fuzz = true
		}
		if !met {
			res.LevelShort = append(res.LevelShort, op.Name)
		}
	}
	for _, g := range raw.Groups {
		label := groupLabel(g)
		switch groupState(g, idx, &res) {
		case groupMissing:
			res.Missing = append(res.Missing, label)
		case groupTrain:
			res.LevelShort = append(res.LevelShort, label)
		}
	}
	res.Total = len(raw.Opers) + len(raw.Groups)
	res.Mode = classify(res.Missing, res.LevelShort, res.Total)
	return res
}

type groupStatus int

const (
	groupReady groupStatus = iota
	groupTrain
	groupMissing
)

// groupState 判干员组：组里有人在且有人达标 → ready；有人但都不达标 → train；一个都没有 → missing。
func groupState(g Group, idx map[string]model.RosterEntry, res *MatchResult) groupStatus {
	owned := 0
	for _, c := range g.Opers {
		e, ok := idx[NormName(c.Name)]
		if !ok {
			continue
		}
		owned++
		met, _, fuzz := requirementMet(c, e)
		if fuzz {
			res.Fuzz = true
		}
		if met {
			return groupReady
		}
	}
	if owned > 0 {
		return groupTrain
	}
	return groupMissing
}

// groupLabel 是干员组在界面上的名字：有 name 用 name，否则把成员名字连起来。
func groupLabel(g Group) string {
	if name := strings.TrimSpace(g.Name); name != "" {
		return name
	}
	names := make([]string, 0, len(g.Opers))
	for _, c := range g.Opers {
		if n := strings.TrimSpace(c.Name); n != "" {
			names = append(names, n)
		}
	}
	if len(names) == 0 {
		return "组合"
	}
	return strings.Join(names, " / ")
}

// classify 按「缺几个人 / 几个人练度不够」定档，规则同作业站前端。
func classify(missing, training []string, total int) string {
	if total > 13 || len(missing) >= 2 {
		return "blocked"
	}
	if len(missing) == 1 {
		if len(training) > 0 {
			return "train"
		}
		return "borrow"
	}
	if len(training) == 0 {
		return "ready"
	}
	// 练度不够就是练度不够，不能当成“借一个人就能打”。
	return "train"
}

// requirementMet 判一个干员条目是否达标。
//
// 返回 (是否达标, 是否补了默认要求, 是否因缺技能数据放宽)。
func requirementMet(op OperSpec, owned model.RosterEntry) (met, implied, fuzz bool) {
	req, implied := effectiveRequirement(op.Requirements, rarityOr(owned.Rarity, 6), owned.SkillKnown)
	fuzz = !owned.SkillKnown

	if req.potential != nil && owned.Potential > 0 && owned.Potential < *req.potential {
		return false, implied, fuzz
	}
	if req.elite != nil || req.level != nil {
		rarity := rarityOr(owned.Rarity, 6)
		reqLevel := intAt(req.level, 1)
		reqElite := inferEliteRarity(rarity, reqLevel, req.elite)
		if progressScore(rarity, owned.Elite, owned.Level) < progressScore(rarity, reqElite, reqLevel) {
			return false, implied, fuzz
		}
	}
	if req.skillLevel != nil {
		sl := *req.skillLevel
		if sl >= 8 {
			idx := op.Skill - 1
			if idx < 0 {
				idx = 0
			}
			mastery := 0
			if idx < len(owned.Skills) {
				mastery = owned.Skills[idx]
			}
			if mastery < sl-7 {
				return false, implied, fuzz
			}
		} else if owned.SkillLevel < sl {
			return false, implied, fuzz
		}
	}
	// 模组：只知道「有哪些模组、几级」，槽位归不到 X/Y/A/D，因此按
	// 「任意一个模组已解锁」判定（比逐槽位宽松，方向上是安全的）。
	if req.module != nil && *req.module > 0 {
		best := 0
		for _, lv := range owned.Modules {
			if lv > best {
				best = lv
			}
		}
		if best < 1 {
			return false, implied, fuzz
		}
	}
	return true, implied, fuzz
}

// effectiveRequirement 返回实际生效的练度要求。
//
// 没写精英/等级（或都写 null）时按星级补默认要求并置 implied；strictSkill=false
// （名册没有技能数据）时把技能/模组两维整个摘掉，避免出现永远满足不了的「专三」。
func effectiveRequirement(req map[string]int, rarity int, strictSkill bool) (requirement, bool) {
	out := requirement{}
	implied := false
	get := func(keys ...string) *int {
		for _, k := range keys {
			if v, ok := req[k]; ok {
				n := v
				return &n
			}
		}
		return nil
	}
	out.elite = get("elite")
	out.level = get("level")
	out.skillLevel = get("skill_level", "skillLevel")
	out.module = get("module")
	out.potential = get("potential")

	if out.elite == nil && out.level == nil {
		d, ok := defaultRequirement[rarityOr(rarity, 6)]
		if ok {
			// 作者写明的键优先，默认值只补缺
			if out.elite == nil {
				out.elite = d.elite
			}
			if out.level == nil {
				out.level = d.level
			}
			if out.skillLevel == nil {
				out.skillLevel = d.skillLevel
			}
			implied = true
		}
	}
	if !strictSkill {
		out.skillLevel = nil
		out.module = nil
	}
	return out, implied
}

// progressScore 把「精英阶段 + 等级」折算成一个可比较的进度分。
func progressScore(rarity, elite, level int) int {
	lv := maxLevels[rarityOr(rarity, 6)]
	score := level
	if score < 1 {
		score = 1
	}
	for i := 0; i < elite && i < len(lv); i++ {
		score += lv[i]
	}
	return score
}

// inferEliteRarity 由等级反推精英阶段（作业只写了等级时用）；有精英要求就直接用。
func inferEliteRarity(rarity int, level int, elite *int) int {
	if elite != nil {
		return *elite
	}
	lv := maxLevels[rarityOr(rarity, 6)]
	e := 0
	for e < len(lv)-1 && level > lv[e] {
		e++
	}
	return e
}

// RankJobs 按「能打的优先、档内评分优先」就地排序：
//
//	0  可直接抄且评分 >= 9（唯一能压过「差一人可借」的一档）
//	1  可直接抄但分不高 / 差一人可借
//	2  需补练度
//	3  打不了
//
// 档内：评分 → 浏览量 → 出场人数（少的优先）。
func RankJobs(jobs []model.Job) {
	sort.SliceStable(jobs, func(i, j int) bool {
		bi, bj := modeBand(jobs[i]), modeBand(jobs[j])
		if bi != bj {
			return bi < bj
		}
		if jobs[i].Rating != jobs[j].Rating {
			return jobs[i].Rating > jobs[j].Rating
		}
		if jobs[i].Views != jobs[j].Views {
			return jobs[i].Views > jobs[j].Views
		}
		return jobs[i].Total < jobs[j].Total
	})
}

// readyMinRating 是「可直接抄」压过「差一人可借」所需的评分线。
const readyMinRating = 9

func modeBand(j model.Job) int {
	switch j.Mode {
	case "ready":
		if j.Rating >= readyMinRating {
			return 0
		}
		return 1
	case "borrow":
		return 1
	case "train":
		return 2
	}
	return 3
}

// ModeLabel 返回分档的中文名（日志/界面用）。
func ModeLabel(mode string) string {
	switch mode {
	case "ready":
		return "可直接抄"
	case "borrow":
		return "差一人可借"
	case "train":
		return "需补练度"
	case "blocked":
		return "打不了"
	}
	return mode
}

// ---- 小工具 ----

func intp(v int) *int { return &v }

func intAt(p *int, def int) int {
	if p == nil {
		return def
	}
	return *p
}

func rarityOr(r, def int) int {
	if r <= 0 {
		return def
	}
	return r
}
