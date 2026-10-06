<script lang="ts">
  import type { IconName } from "./icons";

  /**
   * 统一图标组件。
   *
   * 全部图标来自 web/index.html 里的 SVG 精灵（24×24 / 统一描边），
   * 不再混用 emoji 或 unicode 字符——字符图标在不同字体下粗细、
   * 基线、占位各不相同，界面会显得杂。
   */
  let { name, size = "ico" }: { name: IconName; size?: "ico" | "ico-lg" } = $props();
</script>

<svg class={size} aria-hidden="true"><use href={`#i-${name}`} /></svg>
