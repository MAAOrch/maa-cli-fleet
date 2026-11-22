package copilotpick

import (
	"strings"
	"testing"

	"github.com/xiachk083-hub/maa-cli-fleet/internal/copilotpick/model"
)

// 单关调用（不传 Stages）要能用旧字段补成一条队列。
func TestNormalizeStagePlansSingle(t *testing.T) {
	opts := model.RunOptions{StageCode: "TL-9", StageName: "act55side_09", JobIDs: []int{1, 2}}
	if err := normalizeStagePlans(&opts); err != nil {
		t.Fatalf("单关不应报错：%v", err)
	}
	if len(opts.Stages) != 1 {
		t.Fatalf("应补成 1 关，实际 %d", len(opts.Stages))
	}
	st := opts.Stages[0]
	if st.StageCode != "TL-9" || st.StageName != "act55side_09" || len(st.JobIDs) != 2 {
		t.Errorf("补出来的关卡不对：%+v", st)
	}
	if opts.StageName != "act55side_09" || len(opts.JobIDs) != 2 {
		t.Errorf("单关字段应指向第一关：%+v", opts)
	}
}

// 多关队列：原样保留顺序，并把 StageCode/StageName/JobIDs 指向第一关（兼容快照）。
func TestNormalizeStagePlansQueue(t *testing.T) {
	opts := model.RunOptions{Stages: []model.StagePlan{
		{StageCode: " TL-9 ", StageName: " act55side_09 ", JobIDs: []int{11}},
		{StageCode: "TL-10", StageName: "act55side_10", JobIDs: []int{22, 33}},
	}}
	if err := normalizeStagePlans(&opts); err != nil {
		t.Fatalf("多关不应报错：%v", err)
	}
	if len(opts.Stages) != 2 {
		t.Fatalf("应保留 2 关，实际 %d", len(opts.Stages))
	}
	if opts.Stages[0].StageCode != "TL-9" || opts.Stages[0].StageName != "act55side_09" {
		t.Errorf("应去掉字段首尾空白：%+v", opts.Stages[0])
	}
	if opts.StageCode != "TL-9" || len(opts.JobIDs) != 1 {
		t.Errorf("单关字段应指向第一关：%+v", opts)
	}
}

// 手动模式只支持单关：多关队列要拒绝，并说清原因。
func TestNormalizeStagePlansManualMultiRejects(t *testing.T) {
	opts := model.RunOptions{ManualMode: true, Stages: []model.StagePlan{
		{StageName: "act55side_09", JobIDs: []int{1}},
		{StageName: "act55side_10", JobIDs: []int{2}},
	}}
	err := normalizeStagePlans(&opts)
	if err == nil || !strings.Contains(err.Error(), "手动模式") {
		t.Fatalf("多关 + 手动模式应报错，实际：%v", err)
	}
}

// 每关必须有作业站关卡代码和至少一份作业。
func TestNormalizeStagePlansValidation(t *testing.T) {
	cases := []struct {
		name string
		opts model.RunOptions
		want string
	}{
		{
			name: "缺关卡代码",
			opts: model.RunOptions{StageCode: "TL-9", JobIDs: []int{1}},
			want: "缺少作业站关卡代码",
		},
		{
			name: "队列里某关缺作业",
			opts: model.RunOptions{Stages: []model.StagePlan{
				{StageName: "act55side_09", JobIDs: []int{1}},
				{StageName: "act55side_10"},
			}},
			want: "没有选中作业",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			err := normalizeStagePlans(&tc.opts)
			if err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("应报含 %q 的错误，实际：%v", tc.want, err)
			}
		})
	}
}


// 突袭作业表只能包含本关实际会尝试的作业，并去重。
func TestNormalizeStagePlansRaidIDs(t *testing.T) {
	opts := model.RunOptions{Stages: []model.StagePlan{
		{StageName: "act55side_ex06", JobIDs: []int{1, 2, 3}, RaidJobIDs: []int{2, 2, 9}},
	}}
	if err := normalizeStagePlans(&opts); err != nil {
		t.Fatal(err)
	}
	got := opts.Stages[0].RaidJobIDs
	if len(got) != 1 || got[0] != 2 {
		t.Errorf("应只保留 jobIds 里的 #2 且去重，实际：%v", got)
	}
}
