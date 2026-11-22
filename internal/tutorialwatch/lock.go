package tutorialwatch

import (
	"fmt"
	"os"
	"time"
)

// Lock 是带心跳的进程锁：锁文件 mtime 超过 stale 即视为陈旧，可被抢占。
type Lock struct{ path string }

// AcquireLock 尝试拿锁；已有新鲜锁时返回 ok=false。
func AcquireLock(path string, stale time.Duration) (*Lock, bool) {
	if info, err := os.Stat(path); err == nil {
		if time.Since(info.ModTime()) < stale {
			return nil, false
		}
	}
	f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o644)
	if err != nil {
		return nil, false
	}
	fmt.Fprintf(f, "%d\n", os.Getpid())
	_ = f.Close()
	return &Lock{path: path}, true
}

// Touch 刷新心跳（每个循环调用一次）。
func (l *Lock) Touch() {
	now := time.Now()
	_ = os.Chtimes(l.path, now, now)
}

// Release 删除锁文件。
func (l *Lock) Release() {
	_ = os.Remove(l.path)
}
