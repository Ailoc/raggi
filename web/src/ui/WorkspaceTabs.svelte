<script lang="ts">
  import Icon from "./Icon.svelte";
  import { href } from "../lib/router.svelte";
  import { kbName } from "../lib/stores.svelte";

  /**
   * 工作区标签条：把"文档 / 分块 / 设置"从三次下钻变成同一层的三个视图。
   *
   * 旧结构是 #/kb/{id} → #/kb/{id}/doc/{id} → #/kb/{id}/standalone →
   * #/kb/{id}/settings 四级下钻，彼此靠 backlink 与面包屑来回跳。标签条
   * 让它们在**同一位置**切换，位置感由"当前库 + 当前标签"承担，
   * 不再需要为每个子页单独想一个返回入口。
   *
   * 用 `<a href>` 而不是按钮：中键新开、右键复制链接、状态栏预览都要保留，
   * 而且这三个地址本来就该是可分享的。
   */
  let { kbId, tab }: { kbId: string; tab: string } = $props();

  const tabs = [
    { key: "docs", label: "文档", icon: "files" as const },
    { key: "chunks", label: "分块", icon: "layers" as const },
    { key: "settings", label: "设置", icon: "gear" as const },
  ];
</script>

<nav class="wstab" aria-label="知识库视图">
  <span class="wstab-kb" title={kbName(kbId)}>
    <Icon name="folder" />
    <span class="wstab-name">{kbName(kbId)}</span>
  </span>
  {#each tabs as t (t.key)}
    <a class="wstab-item" href={href("kb", kbId, t.key)}
      class:is-current={tab === t.key}
      aria-current={tab === t.key ? "page" : undefined}>
      <Icon name={t.icon} />
      {t.label}
    </a>
  {/each}
</nav>

<style>
  .wstab {
    display: flex; align-items: center; gap: var(--u1);
    margin-bottom: var(--u4); padding-bottom: var(--u2);
    border-bottom: 1px solid var(--line);
  }
  .wstab-kb {
    display: inline-flex; align-items: center; gap: var(--u2);
    min-width: 0; margin-inline-end: var(--u3);
    font-size: 13px; font-weight: 600; color: var(--text-2);
  }
  .wstab-name {
    max-width: 22ch; overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap;
  }
  .wstab-item {
    display: inline-flex; align-items: center; gap: var(--u2);
    padding: var(--u2) var(--u3);
    border-radius: var(--r-md) var(--r-md) 0 0;
    color: var(--text-2); font-size: 13px;
    transition: background 100ms var(--ease), color 100ms var(--ease);
  }
  .wstab-item:hover { background: var(--surface-2); color: var(--text); }
  .wstab-item.is-current {
    background: var(--surface); color: var(--text); font-weight: 600;
    box-shadow: inset 0 -2px 0 var(--accent);
  }
</style>
