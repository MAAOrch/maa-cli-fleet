package runner

import (
	"log"
	"strconv"
	"time"
)

// shutdownNode：维护模式开启时，把本节点正在跑的 MAA 与模拟器全部关掉。
//
// 场景：MAS 的队列要开跑（队列级"任务前脚本"打开维护开关），
// 本节点必须把机器完整让出去——包括常驻车道（肉鸽/卫戍）、在跑的日常任务、
// 以及它们占着的模拟器。
//
// 只按 PID 杀 MAA（killPidTree 走 taskkill /T），不按进程名杀，避免误伤别的 MAA。
// 模拟器只关本节点账号表里启用的那些，不碰别人的机器。
func (r *Runner) shutdownNode() {
	// 1) 常驻车道：先停循环（车道 goroutine 会自己收尾），再杀它手里的 maa
	r.stopResidents()

	// 2) 在跑的日常/剿灭：按登记的 pid 杀进程树
	r.mu.Lock()
	pids := make([]int, 0, len(r.maaPids))
	for _, pid := range r.maaPids {
		if pid > 0 {
			pids = append(pids, pid)
		}
	}
	r.mu.Unlock()
	for _, pid := range pids {
		killPidTree(pid)
	}

	// 3) 模拟器：本节点负责的全部关掉。
	// 不先用 EmuRunning 判断——MuMu 的 info 在高负载下会误报 False（2026-10-29 实测：
	// 12 台同时在跑时 info 返回“0 台在线”），关机会被整个跳过。shutdown 本身幂等。
	closed := 0
	for i := range r.accounts {
		a := r.accounts[i]
		if !a.Enabled || a.Emu == "" {
			continue
		}
		DeviceDown(r.cfg, a.Emu)
		closed++
	}
	log.Printf("[维护] 已关闭本节点：MAA %d 个、模拟器 %d 台", len(pids), closed)
}

// killPidTree 按 PID 杀进程树；pid 非法时什么都不做。
func killPidTree(pid int) {
	if pid <= 0 {
		return
	}
	_, _ = execOut(20*time.Second, "taskkill", "/PID", strconv.Itoa(pid), "/T", "/F")
}
