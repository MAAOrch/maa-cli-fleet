package runner

import (
	"context"

	"github.com/xiachk083-hub/maa-cli-fleet/internal/model"
)

// maaExecutor 是 MaaCore / maa-cli 这条线的执行器：发车、等结束、判 out。
// Phase 1 只接管 daily / ann；常驻车道仍走 rogue.go 的 startMaa + 看护逻辑。
type maaExecutor struct {
	run func(model.Account, string, string) (bool, string, error)
}

func (e *maaExecutor) Name() string { return "maa-cli" }

func (e *maaExecutor) Run(ctx context.Context, job Job) (Result, error) {
	if err := ctx.Err(); err != nil {
		return Result{}, err
	}
	ok, out, err := e.run(job.Account, job.Device.ADBAddr, job.TaskFile)
	res := Result{OK: ok, OutFile: out}
	if err != nil {
		res.Note = err.Error()
	}
	return res, err
}

// ensureExecutors：New() 会在启动时注册；测试/一次性命令手工构造 Runner 时惰性补齐。
// 不让“没走 New”变成“daily 没有执行器”的生产事故。
func (r *Runner) ensureExecutors() *Registry {
	r.execOnce.Do(func() {
		if r.executors == nil {
			r.executors = NewRegistry()
		}
		maa := &maaExecutor{run: r.execMaa}
		for _, kind := range []string{"daily", "ann"} {
			if _, ok := r.executors.For(kind); !ok {
				_ = r.executors.Register(kind, maa)
			}
		}
	})
	return r.executors
}
