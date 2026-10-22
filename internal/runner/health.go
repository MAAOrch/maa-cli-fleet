package runner

// 机端"上报"职能的设备侧：体检（maa 进程 / 状态日志年龄 / 游戏进程）。
//
// 机端收集这些信息只为两件事：① 随状态一起上报中心；② 看护判据就近取用。
// 设计取舍（2026-10-02 收敛：机端 = 管理环境 + 执行任务 + 上报）：
//   - maa pid 来自 runner 自己的发车记录 —— 收敛后 maa 只由 runner 启动，
//     不去扫全局进程表（快、准、不用猜哪个 maa 属于哪台机）；
//   - 日志年龄读 MAA_STATE_DIR/debug/asst.log 的 mtime（与看护判据同源，见 rogue.go）；
//   - 游戏 pid 用 adb pidof，且**只对"关注机"查**（常驻机 + 有任务在跑的机）：
//     关机的账号不需要体检，全量查会让报告轮次被 adb 超时拖住。

import (
	"math"
	"strconv"
	"time"

	"github.com/xiachk083-hub/maa-cli-fleet/internal/model"
)

// trackMaa 记下这台机最近一次发车的 maa pid（任务结束后不清：最近值本身有参考意义）。
func (r *Runner) trackMaa(accountID string, pid int) {
	r.mu.Lock()
	if r.maaPids == nil {
		r.maaPids = map[string]int{}
	}
	r.maaPids[accountID] = pid
	r.mu.Unlock()
}

// snapshotDevice 一台机的设备体检。ports 是调用方预先取好的 MuMuAll（一次调用查全部实例，别每台机各查一遍）。
func (r *Runner) snapshotDevice(a model.Account, ports map[string]mumuInst) map[string]any {
	out := map[string]any{"sampledAt": time.Now().Format("01-02 15:04:05")}
	if age := r.logAgeMin(a); age < 9990 { // 9999 = 从没跑过/日志不在，不上报这种哨兵值
		out["logAgeMin"] = math.Round(age*10) / 10
	}
	r.mu.Lock()
	pid := r.maaPids[a.ID]
	r.mu.Unlock()
	if pid > 0 {
		out["maaPid"] = strconv.Itoa(pid)
	}
	// 游戏进程：实例端口拿得到才查（拿不到 = 模拟器没跑，不必付 adb 超时的代价）
	if inst, ok := ports[a.Emu]; ok && inst.AdbPort != "" {
		if g := GameRunning(r.cfg, inst.AdbPort, pkgOfClient(a.Client)); g != "" {
			out["game"] = g
		}
	}
	return out
}

// pkgOfClient 客户端 → 游戏包名（与 device.go 的 GamePackage 同一映射表，
// 但不做 adb 探测——体检不重复付"探测包名"的代价）。
func pkgOfClient(client string) string {
	switch client {
	case "Official":
		return "com.hypergryph.arknights"
	case "Bilibili":
		return "com.hypergryph.arknights.bilibili"
	case "YoStarEN":
		return "com.YoStarEN.Arknights"
	case "YoStarJP":
		return "com.YoStarJP.Arknights"
	case "YoStarKR":
		return "com.YoStarKR.Arknights"
	case "txwy":
		return "com.wayi.arknights"
	}
	return "com.hypergryph.arknights"
}
