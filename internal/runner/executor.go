package runner

import (
	"context"
	"fmt"
	"sort"

	"github.com/xiachk083-hub/maa-cli-fleet/internal/model"
)

// Job 是一次执行请求：控制面已经把设备准备好，executor 只负责“按类型跑完”。
type Job struct {
	Key      string         // "daily:a07" / "pi.maaend.daily:a07"
	Kind     string         // daily | ann | pi.<project>.<entry>
	Account  model.Account  // 账号快照（client/stage/stateDir 等）
	Device   DeviceRef      // 已就绪的设备
	TaskFile string         // MAA 任务文件名；PI 任务可为空
	Params   map[string]any // PI 的 option 值；MAA 任务可为空
}

// DeviceRef 是执行器需要知道的最小设备信息。
type DeviceRef struct {
	EmuIndex string // MuMu 实例索引（账号表里的 emu）
	ADBAddr  string // 127.0.0.1:<port>
}

// Result 是一次执行的结果与证据。
type Result struct {
	OK       bool
	Note     string
	OutFile  string
	CostSec  int
	Evidence []string       // 截图 / 日志路径
	Metrics  map[string]any // 理智、结算数等，按 Kind 解释
}

// Executor 只认一种（或一组）Kind；设备起停、常驻暂停、重试、上报仍归 runner。
type Executor interface {
	Name() string
	Run(ctx context.Context, job Job) (Result, error)
}

// ResidentExecutor 是常驻车道的扩展契约：Start 返回可被看护/轮换/暂停 kill 的运行句柄，
// Judge 在自然退出后判定 out。普通排队任务只实现 Executor 即可。
type ResidentExecutor interface {
	Executor
	Start(job Job) (*maaRun, error)
	Judge(outFile string) (bool, string)
}

// Registry 是 Kind → Executor 的注册表；启动时注册，未知 Kind 必须报错而不是静默跳过。
type Registry struct {
	m map[string]Executor
}

func NewRegistry() *Registry {
	return &Registry{m: map[string]Executor{}}
}

func (r *Registry) Register(kind string, e Executor) error {
	if r == nil {
		return fmt.Errorf("executor registry is nil")
	}
	if kind == "" {
		return fmt.Errorf("executor kind is empty")
	}
	if e == nil {
		return fmt.Errorf("executor %s is nil", kind)
	}
	if r.m == nil {
		r.m = map[string]Executor{}
	}
	if _, exists := r.m[kind]; exists {
		return fmt.Errorf("executor kind %s already registered", kind)
	}
	r.m[kind] = e
	return nil
}

func (r *Registry) For(kind string) (Executor, bool) {
	if r == nil {
		return nil, false
	}
	e, ok := r.m[kind]
	return e, ok
}

// Kinds 返回已注册的 Kind（排序后，便于启动日志/测试）。
func (r *Registry) Kinds() []string {
	if r == nil {
		return nil
	}
	out := make([]string, 0, len(r.m))
	for k := range r.m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}
