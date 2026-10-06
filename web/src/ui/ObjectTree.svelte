<script lang="ts">
  import Icon from "./Icon.svelte";
  import { api } from "../lib/api";
  import { app } from "../lib/stores.svelte";
  import { dock } from "../lib/dock.svelte";
  import { go, href, type Route } from "../lib/router.svelte";
  import type { Document, DocumentList, Kb } from "../lib/types";

  /**
   * dock 的对象树：知识库 → 文档两层。
   *
   * 三条刻意的设计，都跟"别把 dock 变成第二个列表控件"有关：
   *
   * 1. **库层全量常驻**（/api/kbs 已带 doc_count/chunk_count，一次拉完），
   *    **文档层展开时才拉**，且只取前 TREE_DOC_LIMIT 条。5000 篇的库
   *    不可能在侧栏里全量渲染，也没有分页语义可用。
   * 2. 过滤框走**服务端** `?q=`（documents.py:279 起就支持），不是
   *    在今天那种"客户端对截断后的 200 条做 substring"——后者会在
   *    total=5000 时谎称"没有匹配"。
   * 3. 超出上限时明确说"还有 N 篇，去列表页看"，而不是假装这就是全部。
   */
  let { route }: { route: Route } = $props();

  const TREE_DOC_LIMIT = 200;

  let docs = $state<Record<string, Document[]>>({});
  let totals = $state<Record<string, number>>({});
  let loadingKb = $state("");
  let treeError = $state("");
  let filter = $state("");

  /** 选中态一律从路由的命名参数派生——这样刷新/后退/分享后，
   *  dock 的高亮和内容区显示的是同一个对象，不会各说各话。 */
  const currentKbId = $derived(route.params.kbId ?? "");
  const currentDocId = $derived(route.name === "doc" ? (route.params.docId ?? "") : "");

  const shownKbs = $derived(
    filter && !dock.expandedKb
      ? app.kbs.filter((k) => k.name.toLowerCase().includes(filter.toLowerCase()))
      : app.kbs,
  );

  async function loadDocs(kbId: string, q: string): Promise<void> {
    loadingKb = kbId; treeError = "";
    try {
      const sp = new URLSearchParams({
        kb_id: kbId, limit: String(TREE_DOC_LIMIT),
      });
      if (q) sp.set("q", q);
      const r = await api<DocumentList>(`/api/documents?${sp.toString()}`);
      docs[kbId] = r.items;
      totals[kbId] = r.total;
    } catch (e) {
      treeError = e instanceof Error ? e.message : String(e);
      docs[kbId] = [];
      totals[kbId] = 0;
    } finally {
      loadingKb = "";
    }
  }

  function toggleKb(kb: Kb): void {
    if (dock.expandedKb === kb.kb_id) { dock.expandedKb = ""; return; }
    dock.expandedKb = kb.kb_id;
    go(href("kb", kb.kb_id, "docs"));
    if (!docs[kb.kb_id]) void loadDocs(kb.kb_id, filter);
  }

  /** 过滤词变化：已展开的库走服务端，debounce 一下免得每敲一个字打一次接口 */
  $effect(() => {
    const f = filter;
    const kbId = dock.expandedKb;
    if (!kbId) return;
    const t = setTimeout(() => { void loadDocs(kbId, f); }, 300);
    return () => clearTimeout(t);
  });

  /**
   * 树跟随路由展开。
   *
   * 不管你是点标签条进来的、从 dock 点的、还是贴了个分享链接直接落在
   * 某篇文档上，当前库都应当在树里是展开且高亮的——否则"我在哪个库里"
   * 这件事 dock 和内容区会给出两个答案。取数交给下面那个 effect。
   */
  $effect(() => {
    const id = currentKbId;
    if (id && dock.expandedKb !== id) dock.expandedKb = id;
  });

  /* 展开一个还没拉过的库：首次要取一次 */
  $effect(() => {
    const kbId = dock.expandedKb;
    if (kbId && !docs[kbId] && loadingKb !== kbId) void loadDocs(kbId, filter);
  });
</script>

<div class="dock-search">
  <span class="search-ico"><Icon name="search" /></span>
  <input type="search" bind:value={filter} aria-label="过滤知识库；展开某个库后同时在该库文档内过滤"
    placeholder={dock.expandedKb ? "过滤库名与文档…" : "过滤知识库…"} />
</div>

{#if shownKbs.length === 0 && app.kbs.length > 0}
  <p class="tree-hint">没有名字匹配的知识库。</p>
{/if}

<div class="tree">
  {#each shownKbs as kb (kb.kb_id)}
    {@const expanded = dock.expandedKb === kb.kb_id}
    {@const list = docs[kb.kb_id] ?? []}
    <div class="tree-node">
      <div class="tree-row tree-kb" class:is-current={currentKbId === kb.kb_id}>
        <button class="tree-expander" type="button" aria-expanded={expanded}
          aria-label={expanded ? `收起 ${kb.name} 的文档` : `展开 ${kb.name} 的文档`}
          onclick={() => toggleKb(kb)}>
          <Icon name="chevron" />
        </button>
        <a class="tree-name" href={href("kb", kb.kb_id, "docs")} title={kb.name}>{kb.name}</a>
        <span class="tree-count">{kb.doc_count}</span>
      </div>

      {#if expanded}
        <div class="tree-docs">
          {#if loadingKb === kb.kb_id && list.length === 0}
            <p class="tree-hint">正在取文档…</p>
          {:else if treeError && list.length === 0}
            <p class="tree-hint">取不到文档：{treeError}</p>
            <button class="btn btn-sm" type="button"
              onclick={() => void loadDocs(kb.kb_id, filter)}>重试</button>
          {:else if list.length === 0}
            <p class="tree-hint">{filter ? "该库内没有匹配的文档。" : "这个库还没有文档。"}</p>
          {:else}
            {#each list as d (d.doc_id)}
              <a class="tree-row tree-doc" class:is-current={currentDocId === d.doc_id}
                href={href("kb", kb.kb_id, "doc", d.doc_id)} title={d.title}>
                <Icon name="doc" />
                <span class="tree-name">{d.title || d.doc_id}</span>
              </a>
            {/each}
            {#if list.length < (totals[kb.kb_id] ?? 0)}
              <p class="tree-hint">
                另有 {(totals[kb.kb_id] ?? 0) - list.length} 篇未在此列出——
                <a href={href("kb", kb.kb_id, "docs")}>在列表页查看</a>
              </p>
            {/if}
          {/if}
        </div>
      {/if}
    </div>
  {/each}
</div>

{#if app.kbs.length === 0}
  <p class="tree-hint">还没有知识库。到<a href="#/">知识库页</a>新建一个。</p>
{/if}

<style>
  .tree-node { display: flex; flex-direction: column; }
  .tree-doc { font-size: 12px; }
  .tree-name { color: inherit; min-width: 0; flex: 1; overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap; }
  .tree-row :global(.ico) { width: 14px; height: 14px; }
</style>
