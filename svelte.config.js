import { vitePreprocess } from "@sveltejs/vite-plugin-svelte";

/**
 * svelte-check / 编辑器用的 Svelte 配置。
 *
 * 构建本身由 vite.config.ts 驱动（插件默认已启用 vitePreprocess）；
 * 这个文件存在的意义是让静态检查工具能解析 `<script lang="ts">`，
 * 并与构建保持同一套预处理行为——否则会出现「Vite 能构建、
 * svelte-check 却报错」的两套事实。
 */
export default {
  preprocess: vitePreprocess(),
};
