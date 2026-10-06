<script lang="ts">
  import { onDestroy, tick, untrack } from "svelte";
  import { api } from "../lib/api";
  import { reveal, syncScroll } from "../lib/actions";
  import { kbName, loadKbs } from "../lib/stores.svelte";
  import { setCrumbs } from "../lib/crumbs.svelte";
  import { downloadUrl, pageUrl, segment, sourceKind } from "../lib/source";
  import { fly } from "svelte/transition";
  import { SvelteSet } from "svelte/reactivity";
  import { enterItem } from "../lib/motion";
  import { ROW_H, windowOf } from "../lib/virtual";
  import { loader, Pager } from "../lib/loadstate.svelte";
  import { stageLabel, waitForJob } from "../lib/jobs";
  import { addChunkDialog, batchSetEnabled, deleteChunk, editChunkDialog, setChunkEnabled } from "../lib/chunkops";
  import { deleteDocDialog, editDocPlanDialog, moveDocDialog, renameDocDialog, reparseDoc, resplitDoc } from "../lib/docops";
  import SplitView from "../ui/SplitView.svelte";
  import StateBlock from "../ui/StateBlock.svelte";
  import ListMore from "../ui/ListMore.svelte";
  import Icon from "../ui/Icon.svelte";
  import { toast } from "../ui/toast.svelte";
  import type { Chunk, ChunkList, DocPlan, Document } from "../lib/types";

  let { kbId, docId, chunk = "" }:
    { kbId: string; docId: string; chunk?: string } = $props();

  /**
   * 深链进来的那一块（检索页的「在文档中打开」会带 ?chunk=）。
   *
   * 只消费一次：之后用户点哪块就是哪块，不能被 URL 上的旧值反复拽回去。
   * 这里读 `chunk` 必须发生在函数体内（闭包）而不是初始化时赋值一次——
   * 后者只会拿到挂载时的值，换一个深链进来就失效了。
   */
  let chunkConsumed = false;

  let doc = $state<Document | null>(null);
  let chunks = $state<Chunk[]>([]);
  let plan = $state<DocPlan | null>(null);
  const ld = loader();
  const pager = new Pager(200);
  let selected = $state<Chunk | null>(null);
  let chunkFilter = $state("");

  // 批量勾选：SvelteSet 的增删是响应式的，不需要每次重建集合
  const picked = new SvelteSet<string>();
  let selAllBox = $state<HTMLInputElement | null>(null);

  // 原文：pdf/image 走 URL，text 需要抓取后再按偏移渲染
  let textBody = $state("");
  let textError = $state("");

  const kind = $derived(doc ? sourceKind(doc.mime ?? "", doc.stored_file ?? "") : "download");
  const fileUrl = $derived(doc?.file_url ?? "");
  const page = $derived(selected?.page ?? null);

  $effect(() => {
    const id = docId;
    if (!id) return;
    untrack(() => {
      ld.reset();
      pager.restart();
      selected = null;
      picked.clear();
      chunkConsumed = false;   // 换文档 = 新的深链要重新消费
      textBody = ""; textError = "";
      void load();
    });
  });

  /**
   * 取文档、分块与方案。
   *
   * @param append true=往后追加一页（「再加载」）；false=取第一页并替换
   *
   * append 曾经是不存在的参数：模板里的「再加载 N 块」调的是 `load()`，
   * 而 `load()` 写死 `offset=0` 并**替换** chunks——于是超过一页的文档
   * 后面的块在界面上永远加载不出来，点一次只是把第一页重新拉一遍，
   * `pager.advance` 却照常累加，"已加载 400 / 210 块"这种鬼话就是这么来的。
   */
  async function load(append = false): Promise<boolean> {
    const offset = append ? pager.offset : 0;
    return ld.run(async () => {
      const r = await api<ChunkList>(
        `/api/chunks?doc_id=${encodeURIComponent(docId)}&limit=${pager.size}`
        + `&offset=${offset}&include_disabled=true`);
      if (append) return { d: doc, r, p: plan, append };
      const [d, p] = await Promise.all([
        api<Document>(`/api/documents/${encodeURIComponent(docId)}`),
        api<DocPlan>(`/api/documents/${encodeURIComponent(docId)}/plan`).catch(() => null),
      ]);
      return { d, r, p, append };
    }, ({ d, r, p, append: ap }) => {
      if (d) doc = d;
      if (p) plan = p;
      chunks = ap ? [...chunks, ...r.items] : r.items;
      pager.advance(r.items.length, r.total ?? 0);
      if (ap) return;   // 追加不动选中项、不清多选、不重读原文
      // 深链优先：从检索页「在文档中打开」进来时要落在这一块上，
      // 而不是永远回到第一块。只消费一次，之后用户点哪块就是哪块。
      const want = !chunkConsumed && chunk
        ? chunks.find((c) => c.chunk_id === chunk) ?? null
        : null;
      if (want) chunkConsumed = true;
      selected = want ?? chunks[0] ?? null;
      picked.clear();
      if (kind === "text" && fileUrl) void loadText();
      setCrumbs([
        { label: "知识库", href: "#/" },
        { label: kbName(kbId), href: `#/kb/${encodeURIComponent(kbId)}/docs` },
        { label: d?.title ?? "" },
      ]);
    });
  }

  async function loadText(): Promise<void> {
    // file_url 是**签名链接**（iframe/img 无法带 Authorization 头），TTL 1 小时。
    // 页面开久了再触发就会 403，而这里之前只留一句裸 "HTTP 403"，
    // 用户既不知道为什么也没法重试。签名可在 403 时重新申领一次。
    try {
      await fetchText(fileUrl);
    } catch (e) {
      if (e instanceof ExpiredSignature || isAuthStatus(e)) {
        try {
          // 重新取文档 → 后端会签发新的 file_url
          const fresh = await api<Document>(
            `/api/documents/${encodeURIComponent(docId)}`);
          if (fresh.file_url) {
            doc = fresh;
            await fetchText(fresh.file_url);
            return;
          }
        } catch (inner) {
          textError = inner instanceof Error ? inner.message : String(inner);
          return;
        }
      }
      // 原文件取不到不是致命错误：解析全文还在，用它兜底
      textError = e instanceof Error ? e.message : String(e);
    }
  }

  /** 签名过期：后端对过期/无效签名统一回 403。 */
  class ExpiredSignature extends Error {}
  const isAuthStatus = (e: unknown): boolean =>
    e instanceof Error && /^HTTP (401|403)$/.test(e.message);

  async function fetchText(url: string): Promise<void> {
    if (!url) throw new Error("该文档没有留档原文");
    const r = await fetch(url);
    if (r.status === 403) throw new ExpiredSignature("签名已过期或无效");
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    textBody = await r.text();
  }

  /* ---------- 异步任务订阅 ----------
   *
   * 重新解析 / 重新切分都是 `wait=false` 提交的：接口回来时流水线才刚启动。
   * 以前这里只 load() 一次，于是页面停在"仍在处理"的旧状态，**只能离开
   * 再回来**才看得到结果——而当时的提示文案写的却是"状态自动更新"。
   * 现在拿到 job_id 就盯到终态，完成即刷新；视图被销毁时中止轮询，
   * 否则用户早走了，循环还在后台打接口（上限约 10 分钟）。
   */
  let jobAbort: AbortController | null = null;
  let jobNote = $state("");

  async function watchJob(jobId: string, label: string): Promise<void> {
    jobAbort?.abort();
    const ac = new AbortController();
    jobAbort = ac;
    jobNote = `${label}：${stageLabel("queued")}`;
    const job = await waitForJob(
      jobId,
      (j) => { jobNote = `${label}：${stageLabel(j.stage)}`; },
      ac.signal,
    );
    if (ac.signal.aborted) return;
    jobNote = "";
    if (!job) {
      toast(`${label}仍在后台执行，稍后可手动刷新查看结果`, "err");
      return;
    }
    if (job.stage === "failed") {
      toast(`${label}失败：${job.error || "未知原因"}`, "err");
    } else if (job.stage === "cancelled") {
      toast(`${label}已取消`);
    } else {
      toast(`${label}完成`);
    }
    await load();
  }

  onDestroy(() => { jobAbort?.abort(); });

  /* ---------- 分块表窗口化 ---------- */
  let scrollEl = $state<HTMLDivElement | null>(null);
  let pdfFrame = $state<HTMLIFrameElement | null>(null);
  let scrollTop = $state(0);
  /** 视口高度**实测**：以前是写死的 560px，而栏高由 CSS 决定，两者迟早不一致 */
  let viewportH = $state(0);

  $effect(() => {
    const el = scrollEl;
    if (!el) return;
    const measure = (): void => { viewportH = el.clientHeight; };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  });

  const shown = $derived(
    chunkFilter.trim()
      ? chunks.filter((c) => (c.text ?? "").toLowerCase().includes(chunkFilter.trim().toLowerCase()))
      : chunks,
  );

  const win = $derived(windowOf(scrollTop, viewportH, shown.length));
  const visibleChunks = $derived(shown.slice(win.start, win.end));

  /** 行元素表：键盘移动选中时要能把焦点交给新行 */
  const rowEls: Record<string, HTMLElement> = {};

  function selectChunk(c: Chunk): void {
    selected = c;
    // 尊重 offset_valid：改写过的分块偏移可能失效，此时只跳页，不假装能定位
    if (kind === "pdf") revealChunk(c);
  }

  /**
   * 把 PDF 内嵌查看器跳回该分块所在页。
   *
   * 页码本身是 `$derived(selected?.page)`，`src` 由模板声明式绑定——所以多数
   * 情况下根本不用管这里。这个函数只处理一种声明式表达不出来的情形：**页码没变
   * 但 iframe 被用户手动翻走了**，此时要再"跳一次"。写 `f.src` 是这个功能唯一
   * 的实现方式，留着；但元素引用改成 `bind:this`，不再按 id 全页搜——
   * `document.getElementById` 拿不到时是静默的 `if (f)` 跳过，表现为"点块不跳页"。
   */
  function revealChunk(c: Chunk): void {
    if (!c.page || c.page <= 0) return;
    if (pdfFrame) pdfFrame.src = pageUrl(fileUrl, c.page);
  }

  /**
   * ↑/↓ 在分块之间移动选中。
   *
   * 行是定高的，所以"让它可见"就是把它算进 scrollTop——不需要查 DOM
   * 也能保证目标行已经渲染，随后再把焦点交过去。
   */
  async function moveSelection(delta: number): Promise<void> {
    const list = shown;
    if (list.length === 0) return;
    const cur = selected ? list.findIndex((c) => c.chunk_id === selected!.chunk_id) : -1;
    const next = Math.min(list.length - 1, Math.max(0, cur + delta));
    const c = list[next];
    if (!c) return;
    selectChunk(c);
    const top = next * ROW_H;
    if (top < scrollTop) scrollTop = top;
    else if (top + ROW_H > scrollTop + viewportH) scrollTop = top + ROW_H - viewportH;
    await tick();
    rowEls[c.chunk_id]?.focus();
  }

  async function renameDoc(): Promise<void> {
    if (!doc) return;
    if (await renameDocDialog(docId, doc.title)) await load();
  }

  async function editPlan(): Promise<void> {
    try {
      if (await editDocPlanDialog(docId)) await load();
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), "err");
    }
  }

  async function reparse(): Promise<void> {
    await reparseDoc(docId, (r) => { if (r.job_id) void watchJob(r.job_id, "重新解析"); });
  }

  async function resplit(): Promise<void> {
    await resplitDoc(docId, (r) => { if (r.job_id) void watchJob(r.job_id, "重新切分"); });
  }

  async function removeDoc(): Promise<void> {
    if (!doc) return;
    if (await deleteDocDialog(docId, doc.title, pager.total)) {
      location.hash = `#/kb/${encodeURIComponent(kbId)}/docs`;
    }
  }

  /**
   * 移动到其它知识库。移动后要跟着改路由：文档已不属于当前库，
   * 留在原路径下会让面包屑、左侧树与「按库检索」三者互相矛盾。
   */
  async function moveDoc(): Promise<void> {
    if (!doc) return;
    // invalidateKbs 由 moveDocDialog 在底层做（删除文档时同样会失效）
    await moveDocDialog(docId, doc.kb_id ?? "", (newKbId) => {
      void loadKbs();   // 立刻重拉，让左侧树的计数跟上
      void load();
      if (newKbId) {
        location.hash = `#/kb/${encodeURIComponent(newKbId)}/doc/${encodeURIComponent(docId)}`;
      } else {
        // 移出到未分组后没有所属库的详情页可去，回该库的文档列表
        location.hash = `#/kb/${encodeURIComponent(kbId)}/docs`;
      }
    });
  }

  // ---- 批量勾选 ----
  const allPicked = $derived(shown.length > 0 && shown.every((c) => picked.has(c.chunk_id)));
  const somePicked = $derived(shown.some((c) => picked.has(c.chunk_id)));
  const pickedCount = $derived(shown.filter((c) => picked.has(c.chunk_id)).length);

  $effect(() => {
    if (selAllBox) selAllBox.indeterminate = somePicked && !allPicked;
  });

  function toggleAll(on: boolean): void {
    // 只作用于当前过滤结果：过滤后再全选，语义才符合直觉
    for (const c of shown) {
      if (on) picked.add(c.chunk_id);
      else picked.delete(c.chunk_id);
    }
  }

  function isOn(c: Chunk): boolean {
    return c.enabled !== false;
  }

  async function onEdit(c: Chunk): Promise<void> {
    if (await editChunkDialog(c)) await reloadChunks();
  }

  async function onAdd(): Promise<void> {
    if (await addChunkDialog({ kbId, docId })) await reloadChunks();
  }

  async function onDelete(c: Chunk): Promise<void> {
    if (await deleteChunk(c)) await reloadChunks();
  }

  async function onToggle(c: Chunk): Promise<void> {
    if (await setChunkEnabled(c, !isOn(c))) await reloadChunks();
  }

  async function onBatch(enabled: boolean): Promise<void> {
    const list = chunks.filter((c) => picked.has(c.chunk_id));
    if (list.length === 0) return;
    if (await batchSetEnabled(list, enabled)) {
      picked.clear();
      await reloadChunks();
    }
  }

  /** 分块操作后的轻量刷新：文档与方案不动，只重取分块。 */
  async function reloadChunks(): Promise<void> {
    try {
      const r = await api<ChunkList>(
        `/api/chunks?doc_id=${encodeURIComponent(docId)}&limit=${pager.size}`
        + `&offset=0&include_disabled=true`);
      chunks = r.items;
      pager.advance(r.items.length, r.total ?? 0);
      if (selected) {
        selected = chunks.find((c) => c.chunk_id === selected!.chunk_id) ?? null;
      }
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), "err");
    }
  }

  // 方案“需重切”提示：入库时的快照与当前生效值不一致
  const planStale = $derived.by(() => {
    const s = plan?.snapshot;
    if (!s || !plan) return false;
    return s.chunk_size !== plan.effective.chunk_size
      || Number(s.overlap_ratio) !== plan.effective.overlap_ratio;
  });
  const planSrc = $derived(
    plan?.effective.source === "doc" ? "本文档"
      : plan?.effective.source === "kb" ? "知识库默认" : "系统默认");

  /* scrollTop 双向同步用的是 lib/actions.ts 的 syncScroll：
     Svelte 5 的普通元素没有 bind:scrollTop，而这层如果留在视图里，就等于
     在"副作用只有一份"的边界上开一个例外——例外清单正是漂移的起点。 */

  /* 「高亮块出现后滚进视区」用的是 lib/actions.ts 的 reveal——它在 Search 里
     曾有一份逐字相同的副本，两份的唯一区别是变量名。 */

  const segs = $derived(
    selected && textBody && kind !== "pdf"
      && (selected as unknown as { offset_valid?: boolean }).offset_valid !== false
      ? segment(textBody, selected.char_start ?? 0, selected.char_end ?? 0)
      : null,
  );
</script>

{#if ld.state.phase === "failed"}
  <StateBlock kind="error" title="加载文档失败" text={ld.state.error}
    action="重试" onaction={() => void load()} />
{:else if ld.state.phase === "initial"}
  <div class="rows" aria-busy="true">
    {#each [1, 2, 3] as i (i)}<div class="row skeleton" aria-hidden="true"></div>{/each}
  </div>
{:else if doc}
  <header class="page-head">
    <div class="page-head-main">
      <h1 class="page-title">{doc.title}</h1>
      <p class="page-sub">
        {doc.parser_engine || "—"} · {pager.total} 个分块 · {doc.char_count ?? 0} 字符
        {#if plan}
          <button class="btn btn-sm planbadge-btn" class:custom={plan.own.custom}
            class:stale={planStale} type="button" onclick={editPlan}
            title="点击编辑本文档的分块方案">
            {#if planStale}<Icon name="alert" />{/if}
            方案 {plan.effective.chunk_size} 字符 / {plan.effective.overlap_ratio}% · {planSrc}{planStale ? " · 需重切" : ""}
          </button>
        {/if}
        {#if jobNote}<span class="jobnote" role="status"><Icon name="pulse" /> {jobNote}</span>{/if}
      </p>
    </div>
    <div class="page-head-actions">
      <button class="btn" type="button" onclick={renameDoc}>
        <Icon name="edit" /> 重命名
      </button>
      <button class="btn" type="button" onclick={moveDoc}>
        <Icon name="folder" /> 移动
      </button>
      <button class="btn" type="button" onclick={editPlan}>
        <Icon name="sliders" /> 分块方案
      </button>
      <button class="btn" type="button" disabled={!!jobNote} onclick={reparse}>
        <Icon name="upload" /> 重新解析
      </button>
      <button class="btn" type="button" disabled={!!jobNote} onclick={resplit}>
        <Icon name="refresh" /> 重新切分
      </button>
      <button class="btn btn-danger" type="button" onclick={removeDoc}>
        <Icon name="trash" /> 删除文档
      </button>
    </div>
  </header>

  <SplitView leftLabel="原文" rightLabel="分块列表">
    {#snippet leftHead()}
      <h2 class="pane-title"><Icon name="doc" /> 原文</h2>
      {#if fileUrl}
        <a class="btn btn-sm" href={downloadUrl(fileUrl)} download>
          <Icon name="download" /> 下载
        </a>
      {/if}
    {/snippet}

    {#snippet leftBody()}
      <div class="srcscroll">
        {#if kind === "pdf" && fileUrl}
          <iframe id="pdfFrame" class="pdf" bind:this={pdfFrame} src={pageUrl(fileUrl, page)}
            title={`${doc?.title ?? "文档"} 原文`}></iframe>
        {:else if kind === "image" && fileUrl}
          <img class="srcimg" src={fileUrl} alt={`${doc?.title ?? "文档"} 原文`} />
        {:else if textBody && segs}
          <pre class="srctext">{#each segs as s, i (i)}{#if s.hit}<mark use:reveal={{ key: selected?.chunk_id ?? "" }}>{s.text}</mark>{:else}{s.text}{/if}{/each}</pre>
        {:else if textBody}
          <pre class="srctext">{textBody}</pre>
        {:else if doc?.text}
          <!-- 没有留档原文件（或抓取失败）时用解析全文兜底，而不是空白 -->
          <p class="fallback">原文件不可用{textError ? `（${textError}）` : ""}，以下为解析后的全文。</p>
          <pre class="srctext">{doc.text}</pre>
        {:else if fileUrl}
          <StateBlock title="这个格式无法内嵌预览"
            text="二进制原文无法在浏览器里直接渲染，可下载后查看。"
            action="下载原文" onaction={() => window.open(downloadUrl(fileUrl), "_blank")} />
        {:else}
          <StateBlock title="没有可显示的原文"
            text="这份文档既没有留档原文件，也没有解析后的文本。" />
        {/if}
      </div>
    {/snippet}

    {#snippet rightHead()}
      <h2 class="pane-title"><Icon name="layers" /> 分块</h2>
      <span class="muted">{shown.length} / {pager.total}</span>
      <span class="pane-hint">点任一块定位原文，↑ ↓ 换块</span>
    {/snippet}

    {#snippet rightBody()}
      <div class="chunkbar">
        <input type="search" bind:value={chunkFilter}
          placeholder="过滤分块内容…" aria-label="按内容过滤分块" />
        {#if chunkFilter}<span class="muted">{shown.length} / {chunks.length} 块匹配</span>{/if}
        <button class="btn btn-sm" type="button" disabled={pickedCount === 0}
          onclick={() => onBatch(false)}>
          <Icon name="eye-off" /> 批量停用
        </button>
        <button class="btn btn-sm" type="button" disabled={pickedCount === 0}
          onclick={() => onBatch(true)}>
          <Icon name="eye" /> 批量启用
        </button>
        <button class="btn btn-sm btn-primary add-chunk" type="button" onclick={onAdd}>
          <Icon name="plus" /> 新增分块
        </button>
      </div>

      <div class="chunkscroll" aria-label="分块列表"
        bind:this={scrollEl} use:syncScroll={{ value: scrollTop, sink: (v) => (scrollTop = v) }}>
        <table class="docs">
          <thead>
            <tr>
              <th class="sel">
                <input type="checkbox" bind:this={selAllBox} checked={allPicked}
                  aria-label="全选当前列表" onchange={(e) => toggleAll(e.currentTarget.checked)} />
              </th>
              <th class="num">#</th><th>内容</th>
              <th class="num">页</th><th>状态</th>
              <th class="actions"><span class="sr-only">操作</span></th>
            </tr>
          </thead>
          <tbody>
            <!-- 占位行撑出真实滚动高度；只渲染视口内的行，
                 节点数与总块数脱钩 -->
            {#if win.padTop > 0}
              <tr class="spacer" aria-hidden="true"><td colspan="6"
                style="height:{win.padTop}px"></td></tr>
            {/if}
            {#each visibleChunks as c, i (c.chunk_id)}
              <tr class="chunk-row" in:fly={enterItem(i, 8)}
                bind:this={rowEls[c.chunk_id]}
                class:is-selected={selected?.chunk_id === c.chunk_id}
                class:is-disabled={!isOn(c)}
                class:is-edited={c.edited}
                tabindex="0"
                aria-current={selected?.chunk_id === c.chunk_id ? "true" : undefined}
                onclick={(e) => {
                  // 行内按钮/勾选框的点击不该顺带选中整行：用户点的是
                  // "停用"或"勾选"，不是"查看这一块"
                  if ((e.target as HTMLElement).closest("button, input, a")) return;
                  selectChunk(c);
                }}
                onkeydown={(e) => {
                  // 只处理行自身获得焦点的情况；来自行内控件的冒泡
                  // 必须放行，否则 Enter 的默认点击行为被吃掉
                  if (e.target !== e.currentTarget) return;
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    selectChunk(c);
                  } else if (e.key === "ArrowDown") {
                    e.preventDefault();
                    void moveSelection(1);
                  } else if (e.key === "ArrowUp") {
                    e.preventDefault();
                    void moveSelection(-1);
                  }
                }}>
                <td class="sel">
                  <input type="checkbox" checked={picked.has(c.chunk_id)}
                    aria-label={`选择第 ${c.ordinal ?? 0} 块`}
                    onchange={(e) => {
                      if (e.currentTarget.checked) picked.add(c.chunk_id);
                      else picked.delete(c.chunk_id);
                    }} />
                </td>
                <td class="num mono">{c.ordinal ?? 0}</td>
                <td class="chunktext"><span class="chunk-text" title={c.text}>{c.text.slice(0, 160)}{c.text.length > 160 ? "…" : ""}</span></td>
                <td class="num">{c.page ?? "—"}</td>
                <td>
                  <span class="badge-row">
                    {#if c.origin === "manual"}<span class="badge">手工</span>{/if}
                    {#if c.edited}<span class="badge badge-warn">已编辑</span>{/if}
                    {#if !isOn(c)}<span class="badge badge-err">已停用</span>{/if}
                  </span>
                </td>
                <td class="actions">
                  <div class="row-actions">
                    <button class="icon-btn" type="button" title="编辑内容"
                      aria-label={`编辑第 ${c.ordinal ?? 0} 块`}
                      onclick={() => onEdit(c)}><Icon name="edit" /></button>
                    <button class="icon-btn" type="button"
                      title={isOn(c) ? "停用（退出检索）" : "启用"}
                      aria-label={isOn(c) ? `停用第 ${c.ordinal ?? 0} 块` : `启用第 ${c.ordinal ?? 0} 块`}
                      onclick={() => onToggle(c)}><Icon name={isOn(c) ? "eye-off" : "eye"} /></button>
                    <button class="icon-btn danger" type="button" title="删除"
                      aria-label={`删除第 ${c.ordinal ?? 0} 块`}
                      onclick={() => onDelete(c)}><Icon name="trash" /></button>
                  </div>
                </td>
              </tr>
            {/each}
            {#if win.padBottom > 0}
              <tr class="spacer" aria-hidden="true"><td colspan="6"
                style="height:{win.padBottom}px"></td></tr>
            {/if}
          </tbody>
        </table>
      </div>

      {#if pager.more}
        <ListMore loaded={pager.loaded} total={pager.total} size={pager.size}
          remaining={pager.remaining} unit="块" busy={ld.state.refreshing}
          onmore={() => void load(true)} />
      {/if}
    {/snippet}
  </SplitView>
{/if}

<style>
  .rows { display: flex; flex-direction: column; gap: var(--u2); }
  .row { height: 40px; }
  .skeleton { animation: pulse 1.4s ease-in-out infinite; }
  @keyframes pulse { 50% { opacity: 0.55; } }

  .pane-title {
    display: flex; align-items: center; gap: var(--u2);
    font-size: 13px; font-weight: 600;
  }
  .pane-title :global(.ico) { color: var(--text-3); }
  .pane-hint { font-size: 11.5px; color: var(--text-3); margin-inline-start: auto; }

  /* 左栏：整块可滚，PDF/图片/文本各自撑满 */
  .srcscroll { flex: 1; min-height: 0; overflow: auto; padding: var(--u4); }
  .pdf { width: 100%; height: 100%; min-height: 60vh; border: 0; border-radius: var(--r-sm); }
  .srcimg { max-width: 100%; border-radius: var(--r-sm); }
  .srctext {
    margin: 0;
    background: var(--bg-sunken); border-radius: var(--r-sm); padding: var(--u4);
    font-family: var(--font-mono); font-size: 12px; line-height: 1.7;
    color: var(--text-2); white-space: pre-wrap; word-break: break-word;
  }
  .srctext :global(mark) {
    background: var(--hit-soft); color: var(--hit);
    border-radius: 2px; padding: 1px 0;
  }
  .fallback { margin: 0 0 var(--u2); font-size: 12.5px; color: var(--text-3); }

  /* 右栏：工具条固定，只有表格滚 */
  .chunkbar {
    display: flex; align-items: center; gap: var(--u2); flex-wrap: wrap;
    flex: none; padding: var(--u3) var(--u4);
    border-bottom: 1px solid var(--line);
  }
  .chunkbar input[type="search"] { flex: 1 1 160px; max-width: 260px; }
  .add-chunk { margin-inline-start: auto; }
  .chunkscroll { flex: 1; min-height: 0; overflow: auto; outline-offset: -2px; }
  .chunk-row { cursor: pointer; }
  .chunk-row.is-selected { background: var(--surface-3); box-shadow: inset 2px 0 0 var(--hit); }
  .chunktext { max-width: 1px; }
  /* 行高必须固定：windowOf 的数学以 ROW_H=34 为前提 */
  tbody tr { height: 34px; }
  .spacer td { padding: 0; border: 0; }

  .jobnote {
    display: inline-flex; align-items: center; gap: var(--u1);
    margin-inline-start: var(--u3);
    color: var(--accent); font-size: 12.5px;
  }
  /* 方案徽标做成按钮：点它就能改方案，不必再找一个隐藏入口 */
  .planbadge-btn {
    cursor: pointer; font: inherit; background: transparent;
    margin-inline-start: var(--u2);
  }
  .planbadge-btn:hover { border-color: var(--line-strong); color: var(--text); }

  @media (prefers-reduced-motion: reduce) { .skeleton { animation: none; } }
</style>
