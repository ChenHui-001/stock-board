// 股票看板前端 Vite 配置。
// 约定：
//   - root = frontend/  （Vite 以 index.html 作为入口）
//   - source: frontend/static/js/*.js  （已经是 ESM，不需 babel 转换）
//   - dev server:  5173 端口，对外暴露 0.0.0.0
//   - 生产构建:   输出到 frontend/static/dist/
//                  （保留在 /static/ 下，main.py 的 /static/ StaticFiles 挂载可直接服务）
//                  文件名带 hash（app-[hash].js），彻底解决 ESM import 路径的缓存锁定
//   - vendor/echarts.min.js 走 publicDir，原样复制到 dist/vendor/echarts.min.js
//   - CSS 走 Vite 处理（虽然当前只有 app.css 一个，暂走 vite-plugin 之外的 copy）
import { defineConfig } from 'vite';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const FRONTEND = resolve(__dirname, 'frontend');

export default defineConfig({
  base: '/static/dist/',
  root: FRONTEND,
  publicDir: resolve(FRONTEND, 'static/public'),
  server: {
    host: '0.0.0.0',
    port: 5173,
    strictPort: false,
    open: false,
  },
  build: {
    outDir: resolve(FRONTEND, 'static/dist'),
    emptyOutDir: true,
    target: 'es2020',
    minify: 'esbuild',
    cssCodeSplit: true,
    rollupOptions: {
      input: resolve(FRONTEND, 'index.html'),
      output: {
        entryFileNames: 'assets/[name]-[hash].js',
        chunkFileNames: 'assets/[name]-[hash].js',
        assetFileNames: 'assets/[name]-[hash][extname]',
      },
    },
  },
});
