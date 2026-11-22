package tutorialwatch

import (
	"context"
	"os/exec"
	"strings"
)

// ParseAdbDevices 从 `adb devices` 的输出里取出 127.0.0.1:<port> 且状态为 device 的序列号。
// 忽略 emulator-* 之类的别名，避免同一台设备抓两次。
func ParseAdbDevices(out string) []string {
	var serials []string
	for _, line := range strings.Split(out, "\n") {
		f := strings.Fields(line)
		if len(f) < 2 || f[1] != "device" {
			continue
		}
		if !strings.HasPrefix(f[0], "127.0.0.1:") {
			continue
		}
		serials = append(serials, f[0])
	}
	return serials
}

// ListSerials 执行 `adb devices` 并解析。
func ListSerials(ctx context.Context, adb string) ([]string, error) {
	out, err := exec.CommandContext(ctx, adb, "devices").Output()
	if err != nil {
		return nil, err
	}
	return ParseAdbDevices(string(out)), nil
}
