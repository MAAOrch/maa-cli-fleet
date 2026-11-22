// 前端入口：把单页应用挂到 index.html 的 #root 上。
//
// 注意：样式表由 index.html 的 <link href="/assets/app.css"> 加载，这里不 import CSS，
// 否则 tsc 会在没有 CSS module 声明时报错。构建产物路径见 web/build.mjs。
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { App } from './App'

const container = document.getElementById('root')

if (!container) {
  throw new Error('页面缺少 #root 容器：web/index.html 与入口脚本不匹配。')
}

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
