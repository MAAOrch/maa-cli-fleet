package tutorialwatch

import (
	"bytes"
	"context"
	"fmt"
	"image"
	"os"
	"os/exec"
	"strconv"
	"time"
)

// Options 是 watcher 的可注入依赖，方便单测。
type Options struct {
	Adb       string
	Tpl       *Template
	Interval  time.Duration
	Threshold float64
	StopFile  string
	BaseX     int
	BaseY     int
	TapX      int
	TapY      int
	Throttle  time.Duration
	Logf      func(format string, args ...any)

	ListSerials func(context.Context) ([]string, error)
	Capture     func(context.Context, string) (image.Image, error)
	Tap         func(context.Context, string, int, int) error
}

func (o *Options) fillDefaults() {
	if o.Interval <= 0 {
		o.Interval = 800 * time.Millisecond
	}
	if o.Threshold <= 0 {
		o.Threshold = 10
	}
	if o.Throttle <= 0 {
		o.Throttle = 2500 * time.Millisecond
	}
	if o.BaseX == 0 && o.BaseY == 0 {
		o.BaseX, o.BaseY = 95, 85
	}
	if o.TapX == 0 && o.TapY == 0 {
		o.TapX, o.TapY = 152, 109
	}
	if o.Logf == nil {
		o.Logf = func(string, ...any) {}
	}
	if o.ListSerials == nil {
		o.ListSerials = func(ctx context.Context) ([]string, error) {
			c, cancel := context.WithTimeout(ctx, 5*time.Second)
			defer cancel()
			return ListSerials(c, o.Adb)
		}
	}
	if o.Capture == nil {
		o.Capture = func(ctx context.Context, serial string) (image.Image, error) {
			c, cancel := context.WithTimeout(ctx, 3*time.Second)
			defer cancel()
			out, err := exec.CommandContext(c, o.Adb, "-s", serial, "exec-out", "screencap", "-p").Output()
			if err != nil {
				return nil, err
			}
			img, _, err := image.Decode(bytes.NewReader(out))
			if err != nil {
				return nil, fmt.Errorf("screencap 解码: %w", err)
			}
			return img, nil
		}
	}
	if o.Tap == nil {
		o.Tap = func(ctx context.Context, serial string, x, y int) error {
			c, cancel := context.WithTimeout(ctx, 5*time.Second)
			defer cancel()
			return exec.CommandContext(c, o.Adb, "-s", serial, "shell", "input", "tap",
				strconv.Itoa(x), strconv.Itoa(y)).Run()
		}
	}
}

// Probe 对所有 127.0.0.1 设备各打一次分（不点击），用于验证模板。
func (o *Options) Probe(ctx context.Context) error {
	o.fillDefaults()
	if o.Tpl == nil {
		return fmt.Errorf("tutorialwatch: 缺少模板")
	}
	serials, err := o.ListSerials(ctx)
	if err != nil {
		return err
	}
	for _, serial := range serials {
		img, err := o.Capture(ctx, serial)
		if err != nil {
			o.Logf("%s capture=FAIL %v", serial, err)
			continue
		}
		o.Logf("%s score=%.2f", serial, o.Tpl.Score(img, o.BaseX, o.BaseY))
	}
	return nil
}

// Run 持续观察设备；命中教程 [X] 才点，匹配不到什么都不做。
func (o *Options) Run(ctx context.Context) error {
	o.fillDefaults()
	if o.Tpl == nil {
		return fmt.Errorf("tutorialwatch: 缺少模板")
	}
	lastTap := map[string]time.Time{}
	fails := map[string]int{}
	skipUntil := map[string]time.Time{}
	lastLog := time.Now()
	loop := 0
	for {
		loop++
		if o.StopFile != "" {
			if _, err := os.Stat(o.StopFile); err == nil {
				o.Logf("发现停止文件 %s，退出", o.StopFile)
				return nil
			}
		}
		serials, err := o.ListSerials(ctx)
		if err != nil {
			o.Logf("adb devices 失败：%v", err)
		}
		now := time.Now()
		for _, serial := range serials {
			if ctx.Err() != nil {
				return nil
			}
			if until, ok := skipUntil[serial]; ok && now.Before(until) {
				continue
			}
			img, err := o.Capture(ctx, serial)
			if err != nil {
				fails[serial]++
				if fails[serial] >= 3 {
					skipUntil[serial] = now.Add(60 * time.Second)
					fails[serial] = 0
					o.Logf("%s 连续抓图失败，冷却 60s：%v", serial, err)
				}
				continue
			}
			fails[serial] = 0
			score := o.Tpl.Score(img, o.BaseX, o.BaseY)
			if score >= o.Threshold {
				continue
			}
			if last, ok := lastTap[serial]; ok && now.Sub(last) < o.Throttle {
				continue
			}
			if err := o.Tap(ctx, serial, o.TapX, o.TapY); err != nil {
				o.Logf("点击失败 %s：%v", serial, err)
				continue
			}
			lastTap[serial] = now
			o.Logf("CLICK %s score=%.2f", serial, score)
		}
		if time.Since(lastLog) >= 2*time.Minute {
			o.Logf("alive loop=%d devices=%d", loop, len(serials))
			lastLog = time.Now()
		}
		wait := o.Interval
		if len(serials) == 0 && wait < 2*time.Second {
			wait = 2 * time.Second
		}
		select {
		case <-ctx.Done():
			return nil
		case <-time.After(wait):
		}
	}
}
