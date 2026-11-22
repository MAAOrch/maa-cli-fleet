//go:build !windows

package copilotpick

import "syscall"

func hideWindow() *syscall.SysProcAttr { return nil }
