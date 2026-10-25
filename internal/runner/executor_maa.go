package runner

import (
	"context"
	"fmt"

	"github.com/xiachk083-hub/maa-cli-fleet/internal/model"
)

// maaExecutor 是 MaaCore / maa-cli 这条线的执行器：排队任务 Run 一次；
// 常驻车道通过 Start 拿到可 kill 的句柄，自然退出后用 Judge 判 out。
type maaExecutor struct {
	run   func(model.Account, string, string) (bool, string, error)
	start func(model.Account, string, string) (*maaRun, error)
	judge func(string) (bool, string)
}

func (e *maaExecutor) Name() string { return "maa-cli" }

func (e *maaExecutor) Run(ctx context.Context, job Job) (Result, error) {
	if err := ctx.Err(); err != nil {
		return Result{}, err
	}
	if e.run == nil {
		return Result{}, fmt.Errorf("maa executor run is nil")
	}
	ok, out, err := e.run(job.Account, job.Device.ADBAddr, job.TaskFile)
	res := Result{OK: ok, OutFile: out}
	if err != nil {
		res.Note = err.Error()
	}
	return res, err
}

// Start 给常驻车道发车；返回的句柄仍由 residentLoop 看护/轮换/暂停时 kill。
func (e *maaExecutor) Start(job Job) (*maaRun, error) {
	if e.start == nil {
		return nil, fmt.Errorf("maa executor start is nil")
	}
	return e.start(job.Account, job.Device.ADBAddr, job.TaskFile)
}

// Judge 在常驻任务自然退出后判定 out；被 kill 的 run 由车道丢弃结果，不调用。
func (e *maaExecutor) Judge(outFile string) (bool, string) {
	if e.judge == nil {
		return false, "maa executor judge is nil"
	}
	return e.judge(outFile)
}

// ensureExecutors：New() 会在启动时注册；测试/一次性命令手工构造 Runner 时惰性补齐。
// 不让“没走 New”变成“daily 没有执行器”的生产事故。
func (r *Runner) ensureExecutors() *Registry {
	r.execOnce.Do(func() {
		if r.executors == nil {
			r.executors = NewRegistry()
		}
		maa := &maaExecutor{run: r.execMaa, start: r.startMaa, judge: r.judgeOutFile}
		for _, kind := range []string{"daily", "ann", "rogue"} {
			if _, ok := r.executors.For(kind); !ok {
				_ = r.executors.Register(kind, maa)
			}
		}
	})
	return r.executors
}

// startResident / judgeResident 让 residentLoop 走注册表；没注册 ResidentExecutor 时回退原路径。
func (r *Runner) startResident(ex Executor, a model.Account, port, task string) (*maaRun, error) {
	if re, ok := ex.(ResidentExecutor); ok {
		return re.Start(Job{
			Key:      "rogue:" + a.ID,
			Kind:     "rogue",
			Account:  a,
			Device:   DeviceRef{EmuIndex: a.Emu, ADBAddr: port},
			TaskFile: task,
		})
	}
	return r.startMaa(a, port, task)
}

func (r *Runner) judgeResident(ex Executor, outFile string) (bool, string) {
	if re, ok := ex.(ResidentExecutor); ok {
		return re.Judge(outFile)
	}
	return r.judgeOutFile(outFile)
}
