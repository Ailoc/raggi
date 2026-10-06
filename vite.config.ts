import { svelte } from "@sveltejs/vite-plugin-svelte";
import { defineConfig } from "vite";

/**
 * 前端构建：Svelte 5 → web/dist，由 FastAPI 的 StaticFiles 托管。
 *
 * 为什么产物放 web/dist 而不是就地覆盖 web/：
 * 原地覆盖会让「源码目录」与「产物目录」混在一起，`npm run build` 的
 * 清空动作可能误删源码。分成两棵树后，构建永远只写 dist。
 *
 * base 用 "./"：产物要被任意路径前缀托管（反代/子路径）而不改配置。
 */
export default defineConfig({
  root: "web",
  plugins: [svelte()],
  base: "./",
  build: {
    outDir: "dist",
    emptyOutDir: true,
    target: "es2022",
    sourcemap: false,
    // 单页应用只有一个入口，拆成多 chunk 反而增加请求数
    rollupOptions: { output: { manualChunks: undefined } },
  },
});
