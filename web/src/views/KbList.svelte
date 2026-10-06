<script lang="ts">
  import { flip } from "svelte/animate";
  import { fly } from "svelte/transition";
  import { enterItem } from "../lib/motion";
  import { app, loadKbs } from "../lib/stores.svelte";
  import { fmtTime } from "../lib/format";
  import { setCrumbs } from "../lib/crumbs.svelte";
  import { loader } from "../lib/loadstate.svelte";
  import StateBlock from "../ui/StateBlock.svelte";
  import Icon from "../ui/Icon.svelte";
  import { createKbDialog } from "../lib/kbops";
  import { href } from "../lib/router.svelte";

  const ld = loader();

  $effect(() => {
    setCrumbs([]);
    void load();
  });

  function load(): Promise<boolean> {
    return ld.run(() => loadKbs(), () => { /* 列表在 app.kbs 里，视图直接读 */ });
  }

  async function create(): Promise<void> {
    if (await createKbDialog()) await load();
  }
</script>

<header class="page-head">
  <div class="page-head-main">
    <h1 class="page-title">知识库</h1>
    <p class="page-sub">按主题组织文档。分块方案等进阶参数建库后在「知识库设置」里单独配置。</p>
  </div>
  <div class="page-head-actions">
    {#if ld.state.refreshing}
      <span class="muted" role="status">更新中…</span>
    {/if}
    <button class="btn" type="button" onclick={() => void load()}>
      <Icon name="refresh" /> 刷新
    </button>
    <button class="btn btn-primary" type="button" onclick={create}>
      <Icon name="plus" /> 新建知识库
    </button>
  </div>
</header>

{#if ld.state.phase === "failed"}
  <StateBlock kind="error" title="加载知识库失败" text={ld.state.error}
    action="重试" onaction={() => void load()} />
{:else if ld.state.phase === "initial"}
  <div class="kbcards" aria-busy="true">
    {#each [1, 2, 3] as i (i)}
      <div class="kbcard skeleton" aria-hidden="true"></div>
    {/each}
  </div>
{:else if app.kbs.length === 0}
  <StateBlock title="还没有知识库"
    text="知识库是文档的容器。创建第一个，然后上传 PDF、Markdown 或直接粘贴文本。"
    action="新建知识库" onaction={create} />
{:else}
  <!-- keyed：知识库增删时 Svelte 复用未变化的卡片，而不是重建整个网格 -->
  <div class="kbcards">
    {#each app.kbs as kb, i (kb.kb_id)}
      <!-- animate:flip 让增删时其余卡片平滑让位，而不是瞬间重排 -->
      <article class="kbcard" animate:flip={{ duration: 220 }} in:fly={enterItem(i)}>
        <a class="kbcard-link" href={href("kb", kb.kb_id, "docs")}>
          <span class="kbcard-ico"><Icon name="folder" size="ico-lg" /></span>
          <span class="kbcard-head">
            <span class="kbcard-name">{kb.name}</span>
            <span class="kbcard-meta">
              <span>{kb.doc_count} 文档</span>
              <span>{kb.chunk_count} 分块</span>
              <span>{fmtTime(kb.updated_at)}</span>
            </span>
          </span>
        </a>
        <p class="kbcard-desc" class:is-empty={!kb.description}>
          {kb.description || "还没有描述，可在「设置」中补充"}
        </p>
        <div class="kbcard-foot">
          <!-- plan_custom 的口径是"与系统默认是否不同"（rag/api/kbs.py:18-31），
               所以这里不能说"继承"：两级模型下每个库都持有具体数值，
               改了系统默认不会自动传到这里。写"继承"就是承诺一个做不到的联动。 -->
          <span class="planbadge" class:custom={kb.plan_custom}
            class:badge-inherit={!kb.plan_custom}
            title={kb.plan_custom ? "本知识库自定义分块方案" : "与系统默认一致的分块方案"}>
            {kb.plan_custom ? `${kb.chunk_size} 字符 · 重叠 ${kb.overlap_ratio}%` : `同系统默认 ${kb.chunk_size} · ${kb.overlap_ratio}%`}
          </span>
          <!-- 卡片上只留"进入设置"，不留删除。
               删除库是全站唯一的破坏性动作，入口收敛到设置页的危险区：
               三个入口 + 两套确认路径只会让用户学到"哪儿都能删，
               但弹窗长得不一样"。
               kbcard-acts / .kbcard-foot 的 z-index 仍要保留——
               .kbcard-link::after 是铺满整卡的覆盖层，会吃掉按钮点击。 -->
          <div class="kbcard-acts">
            <a class="icon-btn" href={href("kb", kb.kb_id, "settings")}
              title="知识库设置" aria-label="打开 {kb.name} 的设置"><Icon name="gear" /></a>
          </div>
        </div>
      </article>
    {/each}
  </div>
{/if}

<style>
  .skeleton { height: 132px; animation: pulse 1.4s ease-in-out infinite; }
  @keyframes pulse { 50% { opacity: 0.55; } }
  @media (prefers-reduced-motion: reduce) { .skeleton { animation: none; } }
</style>
