import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

/**
 * 前端构建：React 19 + MUI 6 → web/dist，由 FastAPI 的 StaticFiles 托管。
 *
 * 2026-10-10 从 Svelte 5 迁到 React：迁移的驱动是"要用真的 MUI 组件库"
 * （MUI 只有 React 实现）。URL 结构、后端 API 契约、深链语义一律不变——
 * 迁移只换渲染层。
 *
 * base 用 "./"：产物要被任意路径前缀托管（反代/子路径）而不改配置。
 */
export default defineConfig({
  root: "web",
  plugins: [react()],
  base: "./",
  build: {
    outDir: "dist",
    emptyOutDir: true,
    target: "es2022",
    sourcemap: false,
    rollupOptions: {
      output: {
        // 依赖合并成**一个** vendor 块：MUI 内部 import React，拆成两块会
        // 形成 react ↔ mui 循环（rollup 会警告并可能把模块塞进任意一侧）。
        // 应用码与依赖分开仍然成立——日常改动的只有 index 那块。
        manualChunks: {
          vendor: [
            "react", "react-dom",
            "@mui/material", "@mui/icons-material",
            "@emotion/react", "@emotion/styled",
          ],
        },
      },
    },
  },
});
