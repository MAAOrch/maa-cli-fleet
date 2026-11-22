package tutorialwatch

import (
	"context"
	"image"
	"image/color"
	"image/draw"
	"sync/atomic"
	"testing"
	"time"
)

func patternFrame() *image.RGBA {
	img := image.NewRGBA(image.Rect(0, 0, 1280, 720))
	for y := 0; y < 720; y++ {
		for x := 0; x < 1280; x++ {
			v := uint8((x*131 + y*197 + x*y) % 251)
			img.Set(x, y, color.RGBA{v, uint8(255 - v), uint8((x*3 + y) % 256), 255})
		}
	}
	return img
}

func TestScoreExactRegionIsZero(t *testing.T) {
	frame := patternFrame()
	tplImg := image.NewRGBA(image.Rect(0, 0, 36, 40))
	draw.Draw(tplImg, tplImg.Bounds(), frame, image.Pt(95, 85), draw.Src)
	tpl := &Template{Img: tplImg, W: 36, H: 40}

	if got := tpl.Score(frame, 95, 85); got != 0 {
		t.Fatalf("同位置分数 = %v，期望 0", got)
	}
	if got := tpl.Score(frame, 400, 300); got < 10 {
		t.Fatalf("异位分数 = %v，期望 >= 10", got)
	}
	if got := tpl.Score(frame, 1270, 700); got != 999 {
		t.Fatalf("越界分数 = %v，期望 999", got)
	}
}

func TestRunTapsOnlyOnMatch(t *testing.T) {
	frame := patternFrame()
	popup := image.NewRGBA(image.Rect(0, 0, 36, 40))
	draw.Draw(popup, popup.Bounds(), frame, image.Pt(95, 85), draw.Src)
	tpl := &Template{Img: popup, W: 36, H: 40}

	var taps int32
	ctx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
	defer cancel()
	o := &Options{
		Tpl:       tpl,
		Interval:  10 * time.Millisecond,
		Threshold: 10,
		ListSerials: func(context.Context) ([]string, error) {
			return []string{"127.0.0.1:16512"}, nil
		},
		Capture: func(context.Context, string) (image.Image, error) { return frame, nil },
		Tap: func(context.Context, string, int, int) error {
			atomic.AddInt32(&taps, 1)
			return nil
		},
	}
	if err := o.Run(ctx); err != nil {
		t.Fatalf("Run: %v", err)
	}
	if atomic.LoadInt32(&taps) == 0 {
		t.Fatal("命中弹窗却没有点击")
	}

	// 换一张不匹配的画面：一次都不该点。
	plain := image.NewRGBA(image.Rect(0, 0, 1280, 720))
	draw.Draw(plain, plain.Bounds(), image.NewUniform(color.RGBA{200, 200, 200, 255}), image.Point{}, draw.Src)
	atomic.StoreInt32(&taps, 0)
	ctx2, cancel2 := context.WithTimeout(context.Background(), 200*time.Millisecond)
	defer cancel2()
	o.Capture = func(context.Context, string) (image.Image, error) { return plain, nil }
	if err := o.Run(ctx2); err != nil {
		t.Fatalf("Run: %v", err)
	}
	if n := atomic.LoadInt32(&taps); n != 0 {
		t.Fatalf("未命中却点击了 %d 次", n)
	}
}
