<script lang="ts">
  import { api } from "../lib/api";
  import { kbName } from "../lib/stores.svelte";
  import { setCrumbs } from "../lib/crumbs.svelte";
  import { fly } from "svelte/transition";
  import { untrack } from "svelte";
  import { enterItem } from "../lib/motion";
  import { loader, Pager } from "../lib/loadstate.svelte";
  import { addChunkDialog, deleteChunk, editChunkDialog, setChunkEnabled } from "../lib/chunkops";
  import StateBlock from "../ui/StateBlock.svelte";
  import ListMore from "../ui/ListMore.svelte";
  import Icon from "../ui/Icon.svelte";
  import { go, href } from "../lib/router.svelte";
  import type { Chunk, ChunkList } from "../lib/types";

  /**
   * @param scope ""=该库全部分块；"standalone"=只看独立分块
   *
   * 旧结构里"独立分块"是一个单独页面（#/kb/{id}/standalone），而且**只有**
   * 那一个视图、永远 only_standalone=true——所以"这个库里所有分块"在界面上
   * 根本没有入口。合并成一个标签 + 范围切换后，两种视图都成立。
   */
  let { kbId, scope = "" }: { kbId: string; scope?: string } = $props();

  const standalone = $derived(scope === "standalone");

  let items = $state<Chunk[]>([]);
  const ld = loader();
  const pager = new Pager(200);
  let filter = $state("");

  function setScope(next: string): void {
    go(next === "standalone"
      ? `${href("kb", kbId, "chunks")}?scope=standalone`
      : href("kb", kbId, "chunks"));
  }

  $effect(() => {
    // scope 是**服务端**参数（only_standalone），不是客户端筛选，
    // 所以它变了必须重取：这里读 standalone 就是为了建立依赖。
    const id = kbId;
    const only = standalone;
    if (!id) return;
    untrack(() => {
      ld.reset();
      pager.restart();
      items = [];
      void load(false, only);
    });
  });

  /**
   * @param append true=往后追加一页；false=替换
   * @param only   只看独立分块；默认跟随当前 scope
   * 增删改之后调用它（不传 append）只会静默换数据：已有数据时 phase
   * 保持 ready，骨架屏不再闪，滚动位置与过滤词都留在原地。
   */
  async function load(append = false, only = standalone): Promise<boolean> {
    return ld.run(async () => {
      // 范围过滤下推到服务端（doc_id=''）：先取全库再本地筛，会让文档分块
      // 占满上限，独立分块整个不可见——这正是当初单独开一页的原因
      const sp = new URLSearchParams({
        kb_id: kbId, only_standalone: String(only),
        include_disabled: "true",
        limit: String(pager.size),
        offset: String(append ? pager.offset : 0),
      });
      // 过滤词用 lastQuery 而不是 filter：两者在 debounce 触发时一致，但读到
      // 这里的可能是用户刚敲下、还没提交的半个词——用"已经发过的那个值"才能让
      // 结果和过滤条件严格是同一批
      if (lastQuery) sp.set("q", lastQuery);
      const r = await api<ChunkList>(`/api/chunks?${sp.toString()}`);
      return { r, append };
    }, ({ r, append }) => {
      if (append) items = [...items, ...r.items];
      else items = r.items;
      pager.advance(r.items.length, r.total ?? 0);
      setCrumbs([
        { label: "知识库", href: "#/" },
        { label: kbName(kbId), href: href("kb", kbId, "docs") },
        { label: only ? "独立分块" : "全部分块" },
      ]);
    });
  }

  /**
   * 内容过滤下推到服务端。
   *
   * 以前 `GET /api/chunks` 没有 `q`，只能在**已取回的那一屏**里筛——于是文案
   * 必须写成"在已加载的 N 块中匹配 M 块"，而用户真正想知道的是"这个库里有没有"。
   * 现在服务端在分页前过滤，`total` 就是命中块数，`items` 已经是筛过的结果，
   * 界面不需要再自己筛第二遍（两处各筛一次迟早给出两个答案）。
   */
  let lastQuery = "";

  /** 过滤词变化：debounce 后交给服务端，而不是每敲一个字打一次接口 */
  $effect(() => {
    // 依赖要在同步阶段读出来（阶段 6 的教训）：读在 setTimeout 回调里的话
    // 这个 effect 根本不会重跑，"过滤"看起来就像没生效
    const q = filter.trim();
    const t = setTimeout(() => {
      if (q === lastQuery) return;
      lastQuery = q;
      pager.restart();
      void load(false);
    }, 300);
    return () => clearTimeout(t);
  });

  function isOn(c: Chunk): boolean {
    return c.enabled !== false;
  }

  async function onAdd(): Promise<void> {
    if (await addChunkDialog({ kbId })) { pager.restart(); await load(); }
  }

  async function onEdit(c: Chunk): Promise<void> {
    if (await editChunkDialog(c)) await load();
  }

  async function onDelete(c: Chunk): Promise<void> {
    if (await deleteChunk(c)) await load();
  }

  async function onToggle(c: Chunk): Promise<void> {
    if (await setChunkEnabled(c, !isOn(c))) await load();
  }
</script>

<header class="page-head">
  <div class="page-head-main">
    <h1 class="page-title">{standalone ? "独立分块" : "全部分块"}</h1>
    <p class="page-sub">
      {#if standalone}
        不属于任何文档、直接挂在「{kbName(kbId)}」下的分块
      {:else}
        「{kbName(kbId)}」里的所有分块，含不属于任何文档的独立分块
      {/if}
      · 共 {pager.total} 块
    </p>
  </div>
  <div class="page-head-actions">
    <!-- 范围切换：旧结构里"独立分块"是唯一视图，全库分块反而没有入口。
         现在它只是一个筛选，两个方向都到得了 -->
    <div class="scope" role="group" aria-label="分块范围">
      <button class="scope-btn" type="button" aria-pressed={!standalone}
        class:is-on={!standalone} onclick={() => setScope("")}>全部</button>
      <button class="scope-btn" type="button" aria-pressed={standalone}
        class:is-on={standalone} onclick={() => setScope("standalone")}>仅独立块</button>
    </div>
    <button class="btn btn-primary" type="button" onclick={onAdd}>
      <Icon name="plus" /> 新增独立分块
    </button>
  </div>
</header>

{#if ld.state.phase === "failed"}
  <StateBlock kind="error" title={standalone ? "加载独立分块失败" : "加载分块失败"}
    text={ld.state.error} action="重试" onaction={() => void load()} />
{:else if ld.state.phase === "initial"}
  <!-- 与兄弟页一致：首屏给骨架屏，而不是一行"加载中…" -->
  <div class="rows" aria-busy="true">
    {#each [1, 2, 3] as i (i)}<div class="row skeleton" aria-hidden="true"></div>{/each}
  </div>
{:else if items.length === 0 && !filter.trim()}
  <!-- 这一支必须同时要求"没有过滤词"：只看 items.length===0 的话，
       筛到 0 命中时页面会说"这个库里还没有分块"（其实有 210 块），
       而且过滤框连同「清空过滤」会被整块换掉——用户既看不见自己筛了什么，
       也没有任何出口撤销，只能刷新。实测过。 -->
  <StateBlock title={standalone ? "还没有独立分块" : "这个库里还没有分块"}
    text={standalone
      ? "独立分块不属于任何文档，直接挂在知识库下，适合补充术语表、口径说明等内容。"
      : "文档入库并切分后，分块会出现在这里。"}
    action="新增独立分块" onaction={onAdd} />
{:else}
  {#if ld.state.error}
    <div class="banner-err" role="alert">
      <Icon name="alert" />
      <span>刷新失败，下面是上一次取到的结果：{ld.state.error}</span>
      <button class="btn btn-sm" type="button" onclick={() => void load()}>重试</button>
    </div>
  {/if}

  <div class="chunkfilter">
    <input type="search" bind:value={filter}
      placeholder="过滤分块内容…" aria-label="按内容过滤分块" />
    {#if filter.trim()}
      <!-- 服务端在分页前过滤，所以这就是**全库**的命中块数，不再是
           "已加载的 N 块里筛出 M 块"那种半截答案 -->
      <span class="muted">匹配 {pager.total} 块</span>
    {/if}
  </div>

  {#if items.length === 0}
    <StateBlock title={filter.trim() ? "没有匹配的分块" : "这个范围里还没有分块"}
      text={filter.trim()
        ? `全库 ${standalone ? "独立分块" : "分块"}里没有包含「${filter.trim()}」的块。`
        : "入库或新增分块后这里会列出来。"}
      action={filter.trim() ? "清空过滤" : ""} onaction={() => (filter = "")} />
  {:else}
    <div class="table-wrap">
      <div class="table-scroll">
        <table class="docs">
          <thead>
            <tr>
              <th class="num">#</th><th>内容</th><th>状态</th>
              <th class="actions"><span class="sr-only">操作</span></th>
            </tr>
          </thead>
          <tbody>
            {#each items as c, i (c.chunk_id)}
              <tr in:fly={enterItem(i, 8)}
                class:is-disabled={!isOn(c)} class:is-edited={c.edited}>
                <td class="num mono">{c.ordinal ?? 0}</td>
                <td><span class="chunk-text" title={c.text}>{c.text.slice(0, 200)}{c.text.length > 200 ? "…" : ""}</span></td>
                <td>
                  <span class="badge-row">
                    {#if c.edited}<span class="badge badge-warn">已编辑</span>{/if}
                    {#if !isOn(c)}<span class="badge badge-err">已停用</span>{/if}
                  </span>
                </td>
                <td class="actions">
                  <div class="row-actions">
                    <button class="icon-btn" type="button" title="编辑内容"
                      aria-label={`编辑第 ${c.ordinal ?? 0} 块`} onclick={() => onEdit(c)}>
                      <Icon name="edit" />
                    </button>
                    <button class="icon-btn" type="button"
                      title={isOn(c) ? "停用（退出检索）" : "启用"}
                      aria-label={isOn(c) ? `停用第 ${c.ordinal ?? 0} 块` : `启用第 ${c.ordinal ?? 0} 块`}
                      onclick={() => onToggle(c)}>
                      <Icon name={isOn(c) ? "eye-off" : "eye"} />
                    </button>
                    <button class="icon-btn danger" type="button" title="删除"
                      aria-label={`删除第 ${c.ordinal ?? 0} 块`} onclick={() => onDelete(c)}>
                      <Icon name="trash" />
                    </button>
                  </div>
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    </div>

    {#if pager.more}
      <!-- 过滤态下 total 是**命中**块数，所以前缀要说清"已加载命中"，
           否则用户以为后面还有和全库一样多的东西 -->
      <ListMore loaded={pager.loaded} total={pager.total} size={pager.size}
        remaining={pager.remaining} unit="块" busy={ld.state.refreshing}
        label={filter.trim() ? "已加载命中" : "已加载"}
        onmore={() => void load(true)} />
    {/if}
  {/if}
{/if}

<style>
  /* 范围切换：一组互斥筛选，用 aria-pressed 表达"当前选中" */
  .scope {
    display: inline-flex; padding: 2px;
    background: var(--bg-sunken); border-radius: var(--r-md);
  }
  .scope-btn {
    padding: var(--u1) var(--u3); border: 0; border-radius: var(--r-sm);
    background: none; color: var(--text-2); font-size: 12.5px; cursor: pointer;
    transition: background 100ms var(--ease), color 100ms var(--ease);
  }
  .scope-btn:hover { color: var(--text); }
  .scope-btn.is-on {
    background: var(--surface); color: var(--text); font-weight: 600;
    box-shadow: var(--elev-1);
  }
  .chunkfilter { display: flex; align-items: center; gap: var(--u2); margin-bottom: var(--u3); }
  .chunkfilter input { max-width: 320px; }
  .banner-err {
    display: flex; align-items: center; gap: var(--u2);
    margin-bottom: var(--u3); padding: var(--u2) var(--u3);
    border: 1px solid var(--red); border-radius: var(--r-md);
    background: color-mix(in oklch, var(--red) 10%, transparent);
    font-size: 12.5px;
  }
  .banner-err span { flex: 1; min-width: 0; }
  .rows { display: flex; flex-direction: column; gap: var(--u2); }
  .row { height: 40px; }
  .skeleton { animation: pulse 1.4s ease-in-out infinite; }
  @keyframes pulse { 50% { opacity: 0.55; } }
</style>
