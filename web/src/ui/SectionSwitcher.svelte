<script lang="ts">
  import Icon from "./Icon.svelte";
  import { SECTIONS } from "../lib/nav";

  /**
   * section 切换器。
   *
   * 用 `<a href>` 而不是 `<button onclick="location.hash=…">`：
   * 导航项必须是链接，否则中键新开标签、右键复制链接、状态栏预览、
   * 浏览器键盘习惯全都会失效——这条教训本项目已经付过一次学费
   * （见 tests/test_frontend_wiring.py::test_views_use_native_controls）。
   * onclick 只用来在窄屏收起浮层，不拦截默认跳转。
   */
  let { current, onnavigate }: { current: string; onnavigate?: () => void } = $props();
</script>

<nav class="section-list" aria-label="视图">
  {#each SECTIONS as s, i (s.key)}
    <a class="section-item" href={s.href}
      class:is-current={current === s.key}
      aria-current={current === s.key ? "page" : undefined}
      title={s.label}
      onclick={onnavigate}>
      <Icon name={s.icon} />
      <span class="section-label">{s.label}</span>
      <span class="section-key" aria-hidden="true">{i + 1}</span>
    </a>
  {/each}
</nav>

<style>
  .section-list { display: flex; flex-direction: column; gap: 2px; }
</style>
