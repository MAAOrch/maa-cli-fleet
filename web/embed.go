// Package web 承载 copilot-picker 的前端构建产物（web/dist，由 Vite 生成）。
package web

import (
	"embed"
	"io/fs"
)

//go:embed all:dist
var dist embed.FS

// Dist 返回前端静态资源（已去掉 dist 前缀）。
func Dist() fs.FS {
	sub, err := fs.Sub(dist, "dist")
	if err != nil {
		return dist
	}
	return sub
}
