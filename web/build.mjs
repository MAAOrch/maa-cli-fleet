// 构建：tsc 把 src/*.tsx 编译成 .build/*.js（ESM），rollup 打包成 dist/assets/app.js，
// 再复制 index.html / styles.css。不用 Vite——沙箱里 esbuild 会 spawn EPERM。
import { rollup } from 'rollup'
import nodeResolve from '@rollup/plugin-node-resolve'
import commonjs from '@rollup/plugin-commonjs'
import replace from '@rollup/plugin-replace'
import { cpSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'

const root = import.meta.dirname
const dist = join(root, 'dist')

rmSync(dist, { recursive: true, force: true })
mkdirSync(join(dist, 'assets'), { recursive: true })

const bundle = await rollup({
  input: join(root, '.build', 'main.js'),
  plugins: [
    replace({
      preventAssignment: true,
      'process.env.NODE_ENV': JSON.stringify('production'),
    }),
    nodeResolve({ browser: true }),
    commonjs(),
  ],
  onwarn: () => {},
})
await bundle.write({
  dir: join(dist, 'assets'),
  format: 'es',
  entryFileNames: 'app.js',
  chunkFileNames: 'chunk-[name].js',
})
await bundle.close()

// index.html：把入口改成构建产物
let html = readFileSync(join(root, 'index.html'), 'utf8')
html = html.replace(/<script type="module" src="[^"]*"><\/script>/, '<script type="module" src="/assets/app.js"></script>')
writeFileSync(join(dist, 'index.html'), html)

// 样式
try {
  cpSync(join(root, 'src', 'styles.css'), join(dist, 'assets', 'app.css'))
  html = readFileSync(join(dist, 'index.html'), 'utf8')
  if (!html.includes('/assets/app.css')) {
    html = html.replace('</head>', '  <link rel="stylesheet" href="/assets/app.css" />\n  </head>')
    writeFileSync(join(dist, 'index.html'), html)
  }
} catch {
  console.warn('warn: src/styles.css 不存在，跳过样式复制')
}

rmSync(join(root, '.build'), { recursive: true, force: true })
console.log('built -> web/dist')
