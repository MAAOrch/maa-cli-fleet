//go:build windows

package copilotpick

import "syscall"

// hideWindow 让 adb/maa 子进程不弹黑框。
func hideWindow() *syscall.SysProcAttr {
	return &syscall.SysProcAttr{HideWindow: true}
}
