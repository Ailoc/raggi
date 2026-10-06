<script lang="ts">
  import type { Snippet } from "svelte";

  /**
   * 双栏对照视图（原文 / 分块，或任何"左边看内容右边看列表"的场景）。
   *
   * 抽出来的原因很具体：文档详情页曾经有**两套** `.docsplit/.pane/.pane-head/
   * .pane-body` 定义——全局 app.css 一份、组件 scoped 一份。scoped 的
   * `.pane-body{min-height:320px}` 把全局的 `flex:1;min-height:0;overflow:auto`
   * 压掉了，于是两栏各自又长出滚动条；断点也一边 1024px 一边 1100px，
   * 1024–1100 之间同一屏幕宽度有两套答案。
   *
   * 高度不再用 `calc(100vh - … - 210px)` 这种魔法数猜：由壳层给 `.main`
   * 加 is-full（见 App.svelte），整条 flex 链把剩余高度交下来，两栏内部
   * 各自滚动，外层不参与——否则长文档会把分块列表顶出视口。
   */
  let {
    leftLabel, rightLabel, leftHead, leftBody, rightHead, rightBody,
    cols = "minmax(0, 1.15fr) minmax(0, 1fr)",
  }: {
    leftLabel: string;
    rightLabel: string;
    leftHead: Snippet;
    leftBody: Snippet;
    rightHead: Snippet;
    rightBody: Snippet;
    /** 宽屏下的两栏比例 */
    cols?: string;
  } = $props();
</script>

<div class="split" style={`--split-cols: ${cols}`}>
  <section class="pane" aria-label={leftLabel}>
    <header class="pane-head">{@render leftHead()}</header>
    <div class="pane-body">{@render leftBody()}</div>
  </section>
  <section class="pane" aria-label={rightLabel}>
    <header class="pane-head">{@render rightHead()}</header>
    <div class="pane-body">{@render rightBody()}</div>
  </section>
</div>

<style>
  .split {
    display: grid;
    grid-template-columns: var(--split-cols);
    gap: var(--u4);
    /* 由外层 flex 链给高度；min-height:0 是让滚动子项能收缩的前提 */
    flex: 1;
    min-height: 0;
  }
  .pane {
    display: flex; flex-direction: column;
    min-width: 0; min-height: 0;
    background: var(--surface);
    border-radius: var(--r-lg);
    box-shadow: var(--elev-2), var(--elev-edge);
    overflow: hidden;
  }
  .pane-head {
    display: flex; align-items: center; gap: var(--u3);
    flex: none;
    padding: var(--u2) var(--u4);
    background: var(--surface-2);
  }
  .pane-body {
    /* 这一层**不滚动**：它是 flex 列，把高度交给槽位里的滚动区。
       如果这里也 overflow:auto，右栏就会套出两层滚动容器（内层找不到
       稳定的 scrollTop，虚拟化窗口也就量不准）——那正是这次要消掉的东西。
       内边距同样交给内容自己定，滚动条才不会被 padding 挤进去。 */
    flex: 1; min-height: 0;
    display: flex; flex-direction: column;
    overflow: hidden;
  }
</style>
