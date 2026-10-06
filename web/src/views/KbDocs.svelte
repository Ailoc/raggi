<script lang="ts">
  import { api } from "../lib/api";
  import { kbName } from "../lib/stores.svelte";
  import { setCrumbs } from "../lib/crumbs.svelte";
  import { toast } from "../ui/toast.svelte";
  import StateBlock from "../ui/StateBlock.svelte";
  import ListMore from "../ui/ListMore.svelte";
  import Icon from "../ui/Icon.svelte";
  import UploadPanel from "../ui/UploadPanel.svelte";
  import { fly } from "svelte/transition";
  import { untrack } from "svelte";
  import { SvelteSet } from "svelte/reactivity";
  import { enterItem } from "../lib/motion";
  import { loader, Pager } from "../lib/loadstate.svelte";
  import { fmtTime } from "../lib/format";
  import {
    deleteDocDialog, editDocPlanDialog, renameDocDialog, reparseDoc, resplitDoc,
  } from "../lib/docops";
  import { deleteDocsBatch } from "../lib/docbatch";
  import { cancelJob } from "../lib/jobcancel";
  import { stageLabel } from "../lib/jobs";
  import { confirmDialog } from "../ui/dialog.svelte";
  import type { DocPlan, Document, DocumentList, Kb } from "../lib/types";

  /** 命名参数由路由的模式表给出；不再用 parts[1] 这种位置索引取库 ID */
  let { kbId }: { kbId: string } = $props();

  let kb = $state<Kb | null>(null);
  let items = $state<Document[]>([]);
  let plans = $state<Record<string, DocPlan>>({});
  /** doc_id → 正在处理的任务（阶段 + job_id），来自 /api/jobs?active_only=true */
  let jobs = $state<Record<string, { stage: string; job_id: string }>>({});
  /** 尚未产出 doc_id 的入库任务（新建文档在切分前没有 doc_id） */
  let orphanJobs = $state<{ stage: string; job_id: string }[]>([]);

  /**
   * 加载状态：phase 决定要不要骨架屏，refreshing 只是"数据在更新"。
   *
   * 旧实现是一个 loading 布尔 + 轮询调同一个 load()，结果
   * **有任务在跑时每 2.5 秒把表格换成骨架屏一次**，而过滤工具栏当时被
   * `{#if !loading}` 门控，于是也被一起卸载——用户正在敲的过滤词丢焦点。
   * 现在只要已经有数据，任何重载都只置 refreshing，DOM 不动。
   */
  const ld = loader();
  /** 分页游标。不再用 `limit += 200`：服务端把 limit 夹在 500，第三次点击会被静默压回 */
  const pager = new Pager(200);

  let filter = $state("");
  let composeOpen = $state(false);

  // 多选：SvelteSet 的增删是响应式的
  const picked = new SvelteSet<string>();
  let selAllBox = $state<HTMLInputElement | null>(null);

  let pollTimer: ReturnType<typeof setTimeout> | undefined;

  $effect(() => {
    // 依赖 kbId：切换知识库时自动重载，不需要调用方记得手动刷新。
    //
    // 重置块必须包在 untrack 里：它读 `filter`（同步 lastQuery），
    // 不加 untrack 的话"用户敲了一个字"也会让这个 effect 重跑——
    // 于是每敲一次就 reset 一遍、清空 items、并且 cleanup 把 pollTimer
    // 清掉，轮询链当场断掉。实测就是这样把轮询弄没的。
    const id = kbId;
    if (!id) return;
    untrack(() => {
      ld.reset();
      pager.restart();
      plans = {};
      picked.clear();
      items = [];
      lastQuery = filter.trim();
      void load();
      void refreshJobs();
    });
    return () => clearTimeout(pollTimer);
  });

  /**
   * @param append true=往后追加一页（"再加载"），false=替换当前列表
   */
  async function load(append = false): Promise<boolean> {
    const q = filter.trim();
    return ld.run(async () => {
      const sp = new URLSearchParams(append ? {} : { kb_id: kbId });
      // 过滤交给服务端：documents.py 的 q 在**分页之前**过滤，
      // 且返回的 total 是过滤后的条数——本地过滤只能覆盖已取回的那一屏，
      // 在 5000 篇的库上会把"你没取到"说成"没有匹配"
      if (q) sp.set("q", q);
      sp.set("limit", String(pager.size));
      sp.set("offset", String(append ? pager.offset : 0));
      const [k, r] = await Promise.all([
        append ? Promise.resolve(null) : api<Kb>(`/api/kbs/${encodeURIComponent(kbId)}`),
        api<DocumentList>(`/api/documents?${sp.toString()}`),
      ]);
      return { k, r, q };
    }, ({ k, r, q }) => {
      if (k) {
        kb = k;
        setCrumbs([{ label: "知识库", href: "#/" }, { label: k.name }]);
      }
      if (append) items = [...items, ...r.items];
      else items = r.items;
      // 用后端 total 而不是取回条数：只比较「取回条数 < 请求上限」
      // 会漏掉「刚好取满」的情况——恰好 200 篇时用户看不到任何提示
      pager.advance(r.items.length, r.total ?? 0);
      // 过滤态下不重取方案：/documents/plans 不接受 q，按 q 过滤后的行
      // 未必落在它的窗口里；方案徽标是辅助信息，保留已取到的那份即可
      if (!q) void loadPlans();
    });
  }

  /**
   * 方案摘要一次取一窗。
   *
   * /documents/plans 只按 kb_id + limit/offset 给（无 q、无 doc_ids），
   * 而它内部又不排序，所以按页对齐是不可靠的——这里固定取前 500 篇
   * （服务端上限）并按 doc_id 建表。超过 500 篇的库，后面的行标 "—"：
   * 这是**已知的覆盖边界**，比旧实现按 limit=200 取要宽，也比静默缺失诚实。
   */
  async function loadPlans(): Promise<void> {
    try {
      const r = await api<{ items: Record<string, DocPlan> }>(
        `/api/documents/plans?kb_id=${encodeURIComponent(kbId)}&limit=500`);
      plans = r.items ?? {};
    } catch {
      // 方案标注是辅助信息：取不到就整列降级为 "—"，不该把列表页打成错误态
      plans = {};
    }
  }

  /**
   * 正在进行的入库/重切任务：驱动列表轮询。
   *
   * 注意 doc_id 可能为空：新建文档的 doc_id 要等流水线跑到「切分前」
   * 才生成并回写任务行，而新入库任务在最初几百毫秒里 doc_id 是空的。
   * 因此这里**也把 kb_id 带上来**——空 doc_id 的任务按 kb 归属，
   * 否则用户在列表上既看不到「处理中」，也没有取消入口。
   */
  async function refreshJobs(): Promise<void> {
    clearTimeout(pollTimer);
    try {
      const r = await api<{
        items: { doc_id: string; stage: string; job_id: string }[];
      }>("/api/jobs?active_only=true&limit=50");
      const m: Record<string, { stage: string; job_id: string }> = {};
      const loose: { stage: string; job_id: string }[] = [];
      for (const j of r.items) {
        const item = { stage: j.stage, job_id: j.job_id };
        if (j.doc_id) m[j.doc_id] = item;
        else loose.push(item);
      }
      // doc_id 尚未生成的入库任务：按顺序挂在列表末尾，直到它们拿到 doc_id
      orphanJobs = loose;
      jobs = m;
      // 有任务在跑就低频刷新：否则用户不知道该等还是该手动刷新。
      // 刷新走同一套三态——已有数据时只置 refreshing，表格不重挂载，
      // 过滤框与光标因此能留在原地
      if (r.items.length > 0) {
        pollTimer = setTimeout(() => { void load(); void refreshJobs(); }, 2500);
      }
    } catch {
      jobs = {};
      orphanJobs = [];
    }
  }

  /** 过滤词变化：debounce 后交给服务端，而不是每敲一个字打一次接口 */
  $effect(() => {
    const q = filter.trim();
    const t = setTimeout(() => {
      if (q !== lastQuery) { lastQuery = q; pager.restart(); void load(); }
    }, 300);
    return () => clearTimeout(t);
  });
  let lastQuery = "";

  function jobStage(docId: string): string {
    const j = jobs[docId];
    return j ? stageLabel(j.stage) : "";
  }

  /**
   * 取消某文档正在跑的任务。
   *
   * 服务端会如实区分「排队中（真取消）」与「运行中（当前阶段结束后停）」，
   * 文案由服务端给出——前端不自己编一套，否则两边说法不一致。
   */
  async function cancelDocJob(docId: string): Promise<void> {
    const j = jobs[docId];
    if (!j) return;
    await cancelJob(j.job_id, () => { void refreshJobs(); });
  }

  function statusClass(status: string): string {
    const s = (status || "").toLowerCase();
    if (/ok|done|ready|success/.test(s)) return "badge-ok";
    if (/fail|error/.test(s)) return "badge-err";
    if (/pending|queued|process|running/.test(s)) return "badge-warn";
    return "";
  }

  /** 入库快照与当前生效方案不一致 → 该文档需要重新切分才会用上新方案。 */
  function planState(docId: string): { stale: boolean; custom: boolean; text: string; title: string } {
    const p = plans[docId];
    if (!p) return { stale: false, custom: false, text: "—", title: "" };
    const s = p.snapshot;
    const e = p.effective;
    const stale = !!s && (s.chunk_size !== e.chunk_size
      || Number(s.overlap_ratio) !== Number(e.overlap_ratio));
    const text = `${e.chunk_size} · ${e.overlap_ratio}%`;
    const title = stale
      ? `入库时用的是 ${s!.chunk_size} 字符 · ${s!.overlap_ratio}%，当前方案是 ${e.chunk_size} 字符 · ${e.overlap_ratio}%，点「重新切分」应用`
      : p.own.custom ? "本文档单独覆盖了分块方案" : `来自${e.source === "kb" ? "知识库默认" : "系统默认"}`;
    return { stale, custom: p.own.custom, text: stale ? "需重切" : text, title };
  }

  async function rename(d: Document): Promise<void> {
    if (await renameDocDialog(d.doc_id, d.title)) await load();
  }

  async function editPlan(d: Document): Promise<void> {
    try {
      if (await editDocPlanDialog(d.doc_id)) await load();
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), "err");
    }
  }

  async function reparse(d: Document): Promise<void> {
    if (await reparseDoc(d.doc_id)) { await load(); void refreshJobs(); }
  }

  async function resplit(d: Document): Promise<void> {
    if (await resplitDoc(d.doc_id)) await load();
  }

  async function remove(d: Document): Promise<void> {
    if (await deleteDocDialog(d.doc_id, d.title, d.chunk_count ?? 0)) {
      picked.delete(d.doc_id);
      await load();
    }
  }

  async function removePicked(): Promise<void> {
    const docs = items.filter((d) => picked.has(d.doc_id));
    if (docs.length === 0) return;
    const chunks = docs.reduce((n, d) => n + (d.chunk_count ?? 0), 0);
    const ok = await confirmDialog({
      title: `删除 ${docs.length} 篇文档`,
      body: docs.length <= 5
        ? `将删除「${docs.map((d) => d.title).join("」「")}」及其 ${chunks} 个分块与留档原文。此操作不可撤销。`
        : `将删除这 ${docs.length} 篇文档及其 ${chunks} 个分块与留档原文。此操作不可撤销。`,
      confirmLabel: "删除",
      destructive: true,
      requireTyping: docs.length > 5 ? String(docs.length) : undefined,
    });
    if (!ok) return;
    if (await deleteDocsBatch(docs)) {
      picked.clear();
      await load();
    }
  }

  /* 删除知识库的入口收敛到「设置 → 危险操作」一处，所以这里不再 import
     removeKbDialog：入口越少，用户学到的"删除长什么样"就越一致 */

  /* 过滤已在服务端完成（?q= 且 total 是过滤后的数），所以"取到的"就是
     "该显示的"。旧实现里这里还有一层 `items.filter(标题包含)`——那是在
     已截断的一屏之内再筛一遍，5000 篇的库会把"没取到"说成"没有匹配"。 */

  // ---- 多选 ----
  const allPicked = $derived(items.length > 0 && items.every((d) => picked.has(d.doc_id)));
  const somePicked = $derived(items.some((d) => picked.has(d.doc_id)));
  const pickedCount = $derived(items.filter((d) => picked.has(d.doc_id)).length);

  $effect(() => {
    if (selAllBox) selAllBox.indeterminate = somePicked && !allPicked;
  });

  function toggleAll(on: boolean): void {
    // 作用于当前（已按 q 过滤的）列表：过滤后再全选，语义才符合直觉
    for (const d of items) {
      if (on) picked.add(d.doc_id);
      else picked.delete(d.doc_id);
    }
  }

  /* 时间格式化统一走 lib/format.ts */
</script>

<header class="page-head">
  <div class="page-head-main">
    <h1 class="page-title">{kbName(kbId)}</h1>
    <p class="page-sub">{kb?.description || ""}</p>
  </div>
  <div class="page-head-actions">
    <button class="btn btn-primary" type="button" aria-expanded={composeOpen}
      aria-controls="compose" onclick={() => (composeOpen = !composeOpen)}>
      <Icon name="upload" /> 入库
    </button>
    <!-- 不能写 onclick={load}：load 的第一个参数是 append，
         直接把事件对象传进去会被当成 true，刷新变成"追加一页" -->
    <button class="btn" type="button" onclick={() => { void load(); void refreshJobs(); }}>
      <Icon name="refresh" /> 刷新
    </button>
    <!-- 设置 / 分块 由工作区标签条承担；删除库**只**留在「设置 → 危险操作」一处。
         旧结构里删除库有 3 个入口（卡片脚、本页页头、设置页），还走 2 套
         确认路径——同一个破坏性动作教给用户的是"哪儿都能删，但弹窗长得不一样" -->
  </div>
</header>

{#if kb}
  <dl class="statline">
    <div><dt>文档</dt><dd>{kb.doc_count}</dd></div>
    <div><dt>分块</dt><dd>{kb.chunk_count}</dd></div>
    <div><dt>更新时间</dt><dd>{fmtTime(kb.updated_at)}</dd></div>
    <div>
      <dt>分块方案</dt>
      <dd>
        <span class="planbadge" class:custom={kb.plan_custom}
          class:badge-inherit={!kb.plan_custom}>
          {kb.chunk_size} 字符 · 重叠 {kb.overlap_ratio}%
        </span>
      </dd>
    </div>
  </dl>
{/if}

<UploadPanel {kbId} open={composeOpen} onclose={() => (composeOpen = false)}
  ondone={() => { void load(); void refreshJobs(); }} />

{#if kb && (kb.doc_count > 0 || filter)}
  <!-- 工具栏不再被"正在加载"门控。旧写法是 `{#if !loading && …}`，
       于是轮询每 2.5 秒把它卸载重建一次，正在敲的过滤词随之丢焦点。

       条件用 kb.doc_count（库内真实篇数）而不是 items.length：
       items 会在"清空过滤→请求返回"之间短暂为空，那一刻 gate 变假、
       输入框被抽走再重建，焦点和已敲的字一起没了。实测就是这么发现的。
       doc_count 不随过滤变化，所以过渡期不会闪。 -->
  <div class="toolbar">
    <input class="filter" bind:value={filter} type="search"
      placeholder="过滤标题…" aria-label="按标题过滤文档" />
    {#if filter}
      <span class="muted">匹配 {items.length} / 库内 {kb?.doc_count ?? "?"} 篇</span>
    {/if}
    {#if ld.state.refreshing}
      <span class="refreshing" role="status">更新中…</span>
    {/if}
  </div>
{/if}

{#if orphanJobs.length > 0}
  <!-- 尚未产出 doc_id 的入库任务：单列一行，既让「正在入库」可见，
       也给一个停止入口——否则用户在文档还看不到时无从取消 -->
  <div class="inflight" role="status" aria-live="polite">
    <Icon name="pulse" />
    <span>
      {orphanJobs.length} 个文件正在入库
      （{stageLabel(orphanJobs[0].stage)}…）
    </span>
    <div class="inflight-acts">
      {#each orphanJobs as j (j.job_id)}
        <button class="btn btn-sm btn-danger" type="button"
          title={`停止第 ${orphanJobs.indexOf(j) + 1} 个（${stageLabel(j.stage)}）`}
          onclick={() => cancelJob(j.job_id, () => { void refreshJobs(); })}>
          停止 {orphanJobs.indexOf(j) + 1}
        </button>
      {/each}
    </div>
  </div>
{/if}

{#if ld.state.phase === "failed"}
  <StateBlock kind="error" title="加载文档失败" text={ld.state.error}
    action="重试" onaction={() => void load()} />
{:else if ld.state.phase === "initial"}
  <div class="rows" aria-busy="true">
    {#each [1, 2, 3, 4] as i (i)}<div class="row skeleton" aria-hidden="true"></div>{/each}
  </div>
{:else}
  <!-- 刷新失败但手上还有数据：保留表格，只加一条横幅。
       旧实现会把 error 直接换成整页错误态，用户刚看到的一屏内容凭空消失，
       而后端重启时这几乎必然发生 -->
  {#if ld.state.error}
    <div class="banner-err" role="alert">
      <Icon name="alert" />
      <span>刷新失败，下面是上一次取到的结果：{ld.state.error}</span>
      <button class="btn btn-sm" type="button" onclick={() => void load()}>重试</button>
    </div>
  {/if}

  {#if items.length === 0}
    <!-- 服务端过滤之后必须分清这两种空：都是"零行"，但意思完全不同 -->
    {#if filter}
      <StateBlock title="没有匹配的文档"
        text="「{filter}」在该知识库的标题里没有命中。换个关键词，或清空过滤条件。"
        action="清空过滤" onaction={() => (filter = "")} />
    {:else if ld.state.refreshing}
      <div class="rows" aria-busy="true">
        {#each [1, 2, 3, 4] as i (i)}<div class="row skeleton" aria-hidden="true"></div>{/each}
      </div>
    {:else}
      <StateBlock title="这个知识库还是空的"
        text="点右上角「入库」上传 PDF、Word，或直接粘贴一段文本。文档入库后即可被检索。"
        action="打开入库面板" onaction={() => (composeOpen = true)} />
    {/if}
  {:else}
  {#if pickedCount > 0}
    <!-- 选中时才出现的批量操作条：放在表格上方而不是行内菜单，
         避免每行都摆 7 个按钮把表格挤成一团 -->
    <div class="bulkbar" role="region" aria-label="批量操作">
      <span class="muted">已选 {pickedCount} 篇</span>
      <div class="bulkbar-actions">
        <button class="btn btn-sm" type="button" onclick={() => picked.clear()}>
          取消选择
        </button>
        <button class="btn btn-sm btn-danger" type="button" onclick={removePicked}>
          <Icon name="trash" /> 删除所选
        </button>
      </div>
    </div>
  {/if}

  <div class="table-wrap">
    <div class="table-scroll">
      <table class="docs">
        <thead>
          <tr>
            <th class="sel">
              <input type="checkbox" bind:this={selAllBox} checked={allPicked}
                aria-label="全选当前列表"
                onchange={(e) => toggleAll(e.currentTarget.checked)} />
            </th>
            <th>标题</th><th>分块方案</th><th>引擎</th>
            <th class="num">块数</th><th>状态</th><th>入库时间</th>
            <th class="actions"><span class="sr-only">操作</span></th>
          </tr>
        </thead>
        <tbody>
          {#each items as d, i (d.doc_id)}
            {@const ps = planState(d.doc_id)}
            <tr in:fly={enterItem(i)}>
              <td class="sel">
                <input type="checkbox" checked={picked.has(d.doc_id)}
                  aria-label={`选择 ${d.title}`}
                  onchange={(e) => {
                    if (e.currentTarget.checked) picked.add(d.doc_id);
                    else picked.delete(d.doc_id);
                  }} />
              </td>
              <td>
                <a class="doc-title" href={`#/kb/${encodeURIComponent(kbId)}/doc/${encodeURIComponent(d.doc_id)}`}
                  title={d.title}>{d.title}</a>
              </td>
              <td>
                <span class="planbadge" class:custom={ps.custom && !ps.stale}
                  class:stale={ps.stale} title={ps.title}>{ps.text}</span>
              </td>
              <td><span class="badge badge-engine">{d.parser_engine || "—"}</span></td>
              <td class="num">{d.chunk_count ?? 0}</td>
              <td>
                {#if jobStage(d.doc_id)}
                  <span class="job-cell">
                    <span class="badge badge-warn">{jobStage(d.doc_id)}</span>
                    <!-- 处理中才能停：终态任务点它只会得到
                         "已结束，无需取消"，徒增困惑 -->
                    <button class="icon-btn job-stop" type="button"
                      title="停止这个任务" aria-label="停止 {d.title} 的处理"
                      onclick={() => cancelDocJob(d.doc_id)}>
                      <Icon name="close" />
                    </button>
                  </span>
                {:else}
                  <span class="badge {statusClass(d.status)}">{d.status}</span>
                {/if}
              </td>
              <td class="time">{fmtTime(d.created_at)}</td>
              <td class="actions">
                <div class="row-actions">
                  <a class="icon-btn" href={`#/kb/${encodeURIComponent(kbId)}/doc/${encodeURIComponent(d.doc_id)}`}
                    title="查看分块" aria-label="查看分块"><Icon name="grid" /></a>
                  <button class="icon-btn" type="button" title="覆盖分块方案" aria-label="覆盖分块方案"
                    onclick={() => editPlan(d)}><Icon name="sliders" /></button>
                  <button class="icon-btn" type="button" title="重命名" aria-label="重命名"
                    onclick={() => rename(d)}><Icon name="edit" /></button>
                  <button class="icon-btn" type="button" title="重新解析（可换引擎）" aria-label="重新解析"
                    onclick={() => reparse(d)}><Icon name="upload" /></button>
                  <button class="icon-btn" type="button" title="重新切分" aria-label="重新切分"
                    onclick={() => resplit(d)}><Icon name="refresh" /></button>
                  <button class="icon-btn danger" type="button" title="删除文档" aria-label="删除文档"
                    onclick={() => remove(d)}><Icon name="trash" /></button>
                </div>
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  </div>
  {#if pager.more}
    <ListMore loaded={pager.loaded} total={pager.total} size={pager.size}
      remaining={pager.remaining} unit="篇" label="已显示" busy={ld.state.refreshing}
      onmore={() => void load(true)} />
  {/if}
  {/if}
{/if}

<style>
  .toolbar { display: flex; align-items: center; gap: var(--u2); margin-bottom: var(--u3); }
  .filter { max-width: 320px; }
  /* 后台刷新提示：轻，不打断阅读 */
  .refreshing { font-size: 12px; color: var(--text-3); }
  /* 刷新失败但仍有数据：横幅而不是整页错误态 */
  .banner-err {
    display: flex; align-items: center; gap: var(--u2);
    margin-bottom: var(--u3); padding: var(--u2) var(--u3);
    border: 1px solid var(--red); border-radius: var(--r-md);
    background: color-mix(in oklch, var(--red) 10%, transparent);
    font-size: 12.5px;
  }
  .banner-err span { flex: 1; min-width: 0; }
  /* 批量操作条：选中时出现，紧贴表格上方 */
  .bulkbar {
    display: flex; align-items: center; justify-content: space-between;
    gap: var(--u3); margin-bottom: var(--u2);
    padding: var(--u2) var(--u3);
    border: 1px solid var(--amber-line); border-radius: var(--r-md);
    background: var(--amber-soft); font-size: 12.5px;
  }
  .bulkbar-actions { display: flex; gap: var(--u2); }
  /* 尚未产出 doc_id 的入库任务条 */
  .inflight {
    display: flex; align-items: center; gap: var(--u2);
    margin-bottom: var(--u3); padding: var(--u2) var(--u3);
    border: 1px solid var(--line); border-radius: var(--r-md);
    background: var(--surface); font-size: 12.5px; color: var(--text-2);
  }
  .inflight-acts { margin-inline-start: auto; display: flex; gap: var(--u1); }
  /* 处理中：阶段徽标 + 停止按钮并排 */
  .job-cell { display: inline-flex; align-items: center; gap: var(--u1); }
  .job-stop { width: 22px; height: 22px; color: var(--text-3); }
  .job-stop:hover { background: var(--red); color: var(--bg); }
  .rows { display: flex; flex-direction: column; gap: var(--u2); }
  .row { height: 40px; }
  .skeleton { animation: pulse 1.4s ease-in-out infinite; }
  @keyframes pulse { 50% { opacity: 0.55; } }
  @media (prefers-reduced-motion: reduce) { .skeleton { animation: none; } }
</style>
