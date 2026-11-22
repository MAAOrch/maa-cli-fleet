// Package tutorialwatch 是 fleet 自带的弹窗兜底：观察运行中的肉鸽车道，
// 模板匹配左上角的教程 [X]，命中才点一下。匹配不到什么都不做——只做加法，
// 不参与 MAA 的流程决策。
package tutorialwatch

import (
	"fmt"
	"image"
	"math"
	"os"

	_ "image/png"
)

// Template 是一张已解码的模板图。
type Template struct {
	Img    image.Image
	W, H   int
	Origin image.Point
}

// LoadTemplate 读取 PNG 模板。
func LoadTemplate(path string) (*Template, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	img, _, err := image.Decode(f)
	if err != nil {
		return nil, fmt.Errorf("解码模板 %s: %w", path, err)
	}
	b := img.Bounds()
	if b.Dx() <= 0 || b.Dy() <= 0 {
		return nil, fmt.Errorf("模板尺寸非法: %s", path)
	}
	return &Template{Img: img, W: b.Dx(), H: b.Dy(), Origin: b.Min}, nil
}

// Score 返回模板与画面 (baseX, baseY) 处的平均像素差（0-255，越小越像）。
// 画面按 1280 宽等比缩放模板，与原 PowerShell 实现一致。
func (t *Template) Score(frame image.Image, baseX, baseY int) float64 {
	fb := frame.Bounds()
	scale := float64(fb.Dx()) / 1280.0
	tw := int(math.Round(float64(t.W) * scale))
	th := int(math.Round(float64(t.H) * scale))
	x := int(math.Round(float64(baseX) * scale))
	y := int(math.Round(float64(baseY) * scale))
	if tw <= 0 || th <= 0 || x < 0 || y < 0 {
		return 999
	}
	if x+tw > fb.Dx() || y+th > fb.Dy() {
		return 999
	}
	var sum float64
	for ty := 0; ty < t.H; ty++ {
		sy := fb.Min.Y + y + ty*th/t.H
		for tx := 0; tx < t.W; tx++ {
			sx := fb.Min.X + x + tx*tw/t.W
			fr, fg, fbl, _ := frame.At(sx, sy).RGBA()
			tr, tg, tbl, _ := t.Img.At(t.Origin.X+tx, t.Origin.Y+ty).RGBA()
			sum += math.Abs(float64(fr>>8) - float64(tr>>8))
			sum += math.Abs(float64(fg>>8) - float64(tg>>8))
			sum += math.Abs(float64(fbl>>8) - float64(tbl>>8))
		}
	}
	return sum / float64(t.W*t.H*3)
}
