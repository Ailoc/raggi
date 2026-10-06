<script lang="ts">
  import { api, apiText, getDoc } from "../lib/api";
  import { isTypingTarget, reveal } from "../lib/actions";
  import { tick } from "svelte";
  import { app } from "../lib/stores.svelte";
  import { go, href, rememberSearch } from "../lib/router.svelte";
  import { DEFAULTS, PARAMS, searchUrl, toBody } from "../lib/searchparams";
  import { setCrumbs } from "../lib/crumbs.svelte";
  import { loader } from "../lib/loadstate.svelte";
  import { pending } from "../ui/dialog.svelte";
  import { formPending } from "../ui/form.svelte";
  import StateBlock from "../ui/StateBlock.svelte";
  import AnswerPanel from "../ui/AnswerPanel.svelte";
  import SnippetView from "../ui/Snippet.svelte";
  import Icon from "../ui/Icon.svelte";
  import { enterItem } from "../lib/motion";
  import { fly } from "svelte/transition";
  import { toast } from "../ui/toast.svelte";
  import type { SearchHit, SearchResponse, Snippet } from "../lib/types";

  let { query }: { query: URLSearchParams } = $props();

  const q = $derived(query.get("q") ?? "");
  const ld = loader();

  /**
   * 参数改动的暂存区。
   *
   * 契约是：**URL 只描述产生了当前这批结果的参数**。控件读写 draft，
   * 只有「应用」才把 draft 提交进 URL 并重新检索。
   *
   * 这条契约是这次修的核心问题：旧实现里 12 个 onchange 都直接改 hash，
   * 而触发检索的 effect 只依赖 `q`——于是改 top_k / 模式 / 阈值 等任何一项
   * 都不会重跑，屏幕上同时存在"参数面板显示新值"和"结果集来自旧参数"，
   * 且没有任何提示。它不是慢，是**在说谎**。
   * 让 URL 等于"已生效"，分享出去的链接也就一定能复现你看到的这批结果。
   */
  let draft = $state<Record<string, string>>({});

  /** 读参数：暂存值优先，其次 URL，最后默认值 */
  function val(key: string): string {
    const d = draft[key];
    if (d !== undefined) return d;
    return query.get(key) ?? DEFAULTS[key] ?? "";
  }

  const dirtyKeys = $derived(
    Object.keys(draft).filter(
      (k) => (draft[k] ?? "") !== (query.get(k) ?? DEFAULTS[k] ?? ""),
    ),
  );
  const dirty = $derived(dirtyKeys.length > 0);

  function setParam(key: string, value: string): void {
    draft[key] = value;
  }

  function discardParams(): void { draft = {}; }

  /** 把暂存区的改动提交进 URL 并重新检索 */
  function apply(): void { commit(); }

  /** 把当前应生效的参数写进 URL 并检索；extra 用于"移除某过滤"这类即时操作。 */
  function commit(extra: Record<string, string> = {}, text?: string): void {
    const values: Record<string, string> = { q: (text ?? q).trim() };
    for (const p of PARAMS) {
      values[p.key] = extra[p.key] ?? draft[p.key] ?? query.get(p.key) ?? "";
    }
    draft = {};
    const next = searchUrl(q, values);
    // 与当前 hash 相同则不会有 hashchange，得自己重跑一次
    if ("#" + next === location.hash) void run();
    else go(next);
  }

  // ---- 原文检视（右栏） ----
  // snippet 类型来自 lib/types（与后端 SnippetOut 一致）：
  // 纯文本 + 高亮区间，由 Snippet 组件原生渲染，无需 {@html}。
  interface ViewerState {
    kind: "empty" | "loading" | "standalone" | "doc" | "error";
    title?: string;
    sub?: string;
    snippet?: Snippet;
    context?: [string, string][];
    ordinal?: number;
    full?: string;
    markStart?: number;
    markEnd?: number;
    error?: string;
  }
  let viewer = $state<ViewerState>({ kind: "empty" });
  let viewerSeq = 0;

  /**
   * 依赖整个查询串而不是只看 q：「应用」之后即使关键词没变，
   * 也必须重跑——这正是要修的那个洞。
   */
  const sig = $derived(query.toString());

  $effect(() => {
    const s = sig;
    if (!q) {
      res = null;
      selectedIdx = -1;
      viewer = { kind: "empty" };
      return;
    }
    rememberSearch(location.hash);
    void run(s);
  });

  let res = $state<SearchResponse | null>(null);
  let selectedIdx = $state(-1);
  /** 查询框元素。以前是 `form.querySelector("input[name=q]")`——按名字在子树里
   *  搜一个元素，改名或包一层就静默拿到 null（`?.` 兜住之后就是"按了没反应"）。 */
  let qInput = $state<HTMLInputElement | null>(null);
  /** 命中按钮元素表：键盘导航靠 chunk_id 认人，不遍历 children。 */
  const hitEls: Record<string, HTMLButtonElement> = {};
  let reporting = $state(false);
  let showAnswer = $state(false);

  async function run(_sig?: string): Promise<boolean> {
    return ld.run(
      () => api<SearchResponse>("/api/search", { method: "POST", body: toBody(q, query) }),
      (r) => {
        res = r;
        setCrumbs([
          { label: "检索", href: "#/search" },
          { label: `“${q}”` },
        ]);
        if (r.results.length > 0) void select(0);
        else { selectedIdx = -1; viewer = { kind: "empty" }; }
      },
    );
  }

  function submit(e: SubmitEvent): void {
    e.preventDefault();
    const text = (qInput?.value ?? "").trim();
    // 换关键词是一次明确的重新检索：连同暂存的参数一起生效，
    // 否则"改了参数又搜一次"会悄悄用上一次的参数
    commit({}, text);
  }

  /**
   * 斜杠聚焦查询框。
   *
   * 旧实现只排除了 INPUT/TEXTAREA，于是焦点在 `<select>` 上、按 Ctrl+/、
   * 或确认框/表单框开着时它照样拦截——用户按了键却没填进该填的地方。
   * "什么时候不接管"这份清单收在 `lib/actions.ts` 的 `isTypingTarget`，
   * 与 App 的 1–5 分节快捷键共用同一条判断，才不会出现"一个页面里两种避让"。
   */
  function onkeydown(e: KeyboardEvent): void {
    if (e.key !== "/" || e.ctrlKey || e.metaKey || e.altKey) return;
    if (pending.current || formPending.current) return;
    if (isTypingTarget(document.activeElement)) return;
    e.preventDefault();
    qInput?.focus();
  }

  async function select(idx: number): Promise<void> {
    if (!res || !res.results[idx]) return;
    selectedIdx = idx;
    const r = res.results[idx];
    const seq = ++viewerSeq;
    viewer = { kind: "loading" };

    // 独立分块：没有 doc_id，只展示自身内容
    if (!r.doc_id) {
      viewer = {
        kind: "standalone",
        title: "独立分块",
        sub: "该分块直接挂在知识库下，不属于任何文档。",
        // snippet 已是结构化（纯文本 + 高亮区间）。兜底也必须是结构化：
        // context 是**原始未转义**文本，早前 `r.snippet || r.context[...]`
        // 会把未转义正文喂进 {@html} —— snippet 一旦为空就是存储型 XSS。
        snippet: r.snippet?.text
          ? r.snippet
          : { text: r.context?.[String(r.ordinal)] ?? "", marks: [] },
      };
      return;
    }

    try {
      const doc = await getDoc(r.doc_id);
      // 过期响应：用户已切到别的命中，直接丢弃（否则 A 文档的全文
      // 会覆盖 B 的，标题与原文错配且用户完全无从察觉）
      if (seq !== viewerSeq) return;
      const keys = Object.keys(r.context || {}).sort((a, b) => Number(a) - Number(b));
      const context = keys.map((k) => [k, r.context[k]] as [string, string]);
      const full = doc.text || "";
      const cs = r.char_start ?? -1;
      const ce = r.char_end ?? -1;
      // offset_valid=false 表示改写导致偏移失效，降级为整篇展示（避免错误高亮）
      const canMark = r.offset_valid !== false && cs >= 0 && ce > cs && ce <= full.length;
      viewer = {
        kind: "doc",
        title: doc.title,
        sub: `${r.heading_path || ""}${r.page ? (r.heading_path ? " · " : "") + "第 " + r.page + " 页" : ""}`,
        context,
        ordinal: r.ordinal,
        full,
        markStart: canMark ? cs : -1,
        markEnd: canMark ? ce : -1,
      };
    } catch (e) {
      if (seq !== viewerSeq) return;
      viewer = { kind: "error", error: e instanceof Error ? e.message : String(e) };
    }
  }

  /**
   * ↑/↓ 在命中之间移动。
   *
   * 旧写法是 `e.currentTarget.parentElement.children[i ± 1].querySelector("button")`
   * ——用**元素在 DOM 里的位置**去找逻辑上的下一个命中。模板只要多包一层、
   * 或某条命中改成不渲染按钮，它就跳错行，而且跳错了也不报错，用户只会长期
   * 觉得"方向键怪怪的"。现在按 `chunk_id` 认人：结构变了这里不用改。
   */
  function onResultKey(e: KeyboardEvent, i: number): void {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    const list = res?.results;
    if (!list) return;
    const next = i + (e.key === "ArrowDown" ? 1 : -1);
    if (next < 0 || next >= list.length) return;
    e.preventDefault();
    const id = list[next]?.chunk_id ?? "";
    void (async (): Promise<void> => {
      await select(next);
      await tick();
      hitEls[id]?.focus();
    })();
  }

  /**
   * 命中 → 文档详情的深链。
   *
   * 以前结果只能进右栏检视器，**永远跳不到文档页**：想改这块分块、想看
   * 它前后的兄弟块、想把这篇文档发给别人，都得先回列表页再一层层找回来。
   * 独立分块没有 doc_id，不给这个链接（否则跳过去是死路）。
   */
  const openDoc = (hit: SearchHit): string =>
    hit.doc_id && hit.kb_id
      ? `${href("kb", hit.kb_id, "doc", hit.doc_id)}?chunk=${encodeURIComponent(hit.chunk_id)}`
      : "";

  async function exportReport(): Promise<void> {
    reporting = true;
    try {
      // 报告是 Markdown，不该带 <mark> —— 高亮在纯文本里只是噪声
      const body = { ...toBody(q, query), highlight: false };
      // 走 apiText：以前这里绕过 api() 直接 fetch，于是没有鉴权头也没有
      // 统一错误解析——后端一开密钥，导出就变成裸 401
      const text = await apiText("/api/search/report", { method: "POST", body });
      const url = URL.createObjectURL(new Blob([text], { type: "text/markdown" }));
      const a = document.createElement("a");
      a.href = url;
      a.download = `rag-report-${new Date().toISOString().slice(0, 10)}.md`;
      a.click();
      URL.revokeObjectURL(url);
      toast("报告已导出");
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), "err");
    } finally {
      reporting = false;
    }
  }

  // 生效中的过滤必须可见：它们跨会话保留，不显示的话 0 命中时
  // 用户会以为库里没有，而不是被隐形过滤挡住了
  const activeFilters = $derived(
    PARAMS.filter((p) => p.group === "filter"
      && query.get(p.key) !== null
      && query.get(p.key) !== (DEFAULTS[p.key] ?? ""))
      .map((p) => {
        const raw = query.get(p.key) ?? "";
        let value = raw;
        if (p.key === "kb_id") value = app.kbs.find((k) => k.kb_id === raw)?.name ?? raw;
        else if (p.key === "include_disabled") value = raw === "true" ? "开" : "关";
        return { key: p.key, label: p.label, value };
      }),
  );

  const base = PARAMS.filter((p) => p.group === "base");
  const filter = PARAMS.filter((p) => p.group === "filter");
  const advanced = PARAMS.filter((p) => p.group === "advanced");
  const modeLabel: Record<string, string> = {
    hybrid: "混合", vector: "语义", fts: "关键词",
  };

  function resetParams(): void {
    // 恢复默认 = 把参数清成默认并立刻生效（这是用户对"当前结果"的操作，
    // 不是又一次待应用的编辑）
    commit(Object.fromEntries(PARAMS.map((p) => [p.key, ""])));
  }

  /**
   * 结果条上的"范围"必须读**已生效**的值，不能读暂存区。
   *
   * 这行元信息描述的是"这批结果是怎么来的"；读 val() 的话，用户改了范围
   * 下拉但还没应用时，这里就会提前显示新范围——正是这次要修的那个
   * "参数是新的、结果是旧的"的毛病，别在文案里再犯一次。
   */
  const kbScope = $derived.by(() => {
    const applied = query.get("kb_id") ?? DEFAULTS.kb_id ?? "";
    if (!applied) return "全部知识库";
    return app.kbs.find((k) => k.kb_id === applied)?.name ?? "指定知识库";
  });

  const kindLabel: Record<string, string> = {
    cosine_similarity: "余弦相似度", bm25: "BM25", rrf: "RRF",
  };
</script>

<svelte:window onkeydown={onkeydown} />

<header class="page-head">
  <div class="page-head-main">
    <h1 class="page-title">检索</h1>
    <p class="page-sub">跨知识库全文与语义检索，可调召回、过滤与呈现参数。</p>
  </div>
  <div class="page-head-actions">
    {#if res && res.results.length > 0}
      <button class="btn" type="button" onclick={exportReport} disabled={reporting}>
        <Icon name="download" /> {reporting ? "导出中…" : "导出报告"}
      </button>
    {/if}
    <button class="btn" class:btn-primary={showAnswer} type="button"
      aria-expanded={showAnswer} onclick={() => (showAnswer = !showAnswer)}>
      <Icon name="pulse" /> {showAnswer ? "收起问答" : "问答"}
    </button>
  </div>
</header>

<!-- 问答：与检索同页，但用问句而不是关键词。默认收起——多数时候
     用户只是检索，问答是可选能力，不该占掉首屏。 -->
{#if showAnswer}
  <AnswerPanel defaultQuestion={q} />
{/if}

<!-- 查询区：检索的主入口。放在页内而非顶栏——顶栏是全局 chrome，
     放页面级操作会让所有页面都为它让位，也放不下这些参数。 -->
<form class="qbar" role="search" onsubmit={submit}>
  <span class="qbar-ico"><Icon name="search" size="ico" /></span>
  <input name="q" type="search" bind:this={qInput} value={q} autocomplete="off"
    placeholder="输入关键词或一句话…（按 / 聚焦，Enter 检索）"
    aria-label="检索关键词" />
  <kbd class="qbar-kbd" aria-hidden="true">/</kbd>
  <button class="btn btn-primary" type="submit">检索</button>
</form>

<div class="params-inline">
  <fieldset class="params-group">
    <legend>范围与模式</legend>
    <div class="params-grid">
      {#each base as p (p.id)}
        <label class="field">
          <span class="field-label">{p.label}</span>
          {#if p.key === "kb_id"}
            <select id={p.id} value={val(p.key)} onchange={(e) => setParam(p.key, e.currentTarget.value)}>
              <option value="">全部知识库</option>
              {#each app.kbs as kb (kb.kb_id)}<option value={kb.kb_id}>{kb.name}</option>{/each}
            </select>
          {:else if p.key === "mode"}
            <select id={p.id} value={val(p.key)} onchange={(e) => setParam(p.key, e.currentTarget.value)}>
              <option value="hybrid">混合 · 语义 + 关键词</option>
              <option value="vector">语义 · 意思相近</option>
              <option value="fts">关键词 · 专有名词</option>
            </select>
          {:else if p.key === "group_by_doc"}
            <select id={p.id} value={val(p.key)} onchange={(e) => setParam(p.key, e.currentTarget.value)}>
              <option value="false">每块独立（默认）</option>
              <option value="true">每篇文档只留最高分</option>
            </select>
          {:else}
            <input id={p.id} type="number" min="1" max="200" step="1" value={val(p.key)}
              onchange={(e) => setParam(p.key, e.currentTarget.value)} />
          {/if}
        </label>
      {/each}
    </div>
  </fieldset>
</div>

<details class="params">
  <summary class="params-summary">
    <Icon name="sliders" />
    <span>高级参数</span>
    <span class="params-summary-hint">
      {filter.length + advanced.length} 项
    </span>
  </summary>
  <div class="params-body">
    <fieldset class="params-group">
      <legend>过滤</legend>
      <div class="params-grid">
        {#each filter as p (p.id)}
          <label class="field" class:field-check={p.kind === "bool"}>
            {#if p.kind === "bool"}
              <input id={p.id} type="checkbox" checked={val(p.key) === "true"}
                onchange={(e) => setParam(p.key, e.currentTarget.checked ? "true" : "false")} />
              <span>包含已停用的分块</span>
            {:else}
              <span class="field-label">{p.label}</span>
              {#if p.key === "origin"}
                <select id={p.id} value={val(p.key)} onchange={(e) => setParam(p.key, e.currentTarget.value)}>
                  <option value="">不限</option>
                  <option value="parsed">解析生成</option>
                  <option value="manual">手工新增</option>
                </select>
              {:else if p.key === "parser_engine"}
                <select id={p.id} value={val(p.key)} onchange={(e) => setParam(p.key, e.currentTarget.value)}>
                  <option value="">不限</option>
                  {#each ["native", "pymupdf4llm", "docling", "unstructured"] as e (e)}
                    <option value={e}>{e}</option>
                  {/each}
                </select>
              {:else}
                <input id={p.id} type="text" value={val(p.key)} placeholder="如 application/pdf"
                  onchange={(e) => setParam(p.key, e.currentTarget.value)} />
              {/if}
            {/if}
          </label>
        {/each}
      </div>
    </fieldset>

    <fieldset class="params-group">
      <legend>质量与呈现</legend>
      <div class="params-grid">
        {#each advanced.filter((p) => ["score_threshold", "window", "snippet_chars", "highlight"].includes(p.key)) as p (p.id)}
          <label class="field" class:field-check={p.kind === "bool"}>
            {#if p.kind === "bool"}
              <input id={p.id} type="checkbox" checked={val(p.key) === "true"}
                onchange={(e) => setParam(p.key, e.currentTarget.checked ? "true" : "false")} />
              <span>摘要中高亮命中词</span>
            {:else}
              <span class="field-label">{p.label}</span>
              <input id={p.id} type="number" value={val(p.key)} placeholder="默认"
                onchange={(e) => setParam(p.key, e.currentTarget.value)} />
              {#if p.key === "score_threshold"}
                <span class="field-note">统一为「越大越相关」，见结果区口径</span>
              {:else if p.key === "window"}
                <span class="field-note">命中块前后各取 N 块</span>
              {:else if p.key === "snippet_chars"}
                <span class="field-note">字符数，纯文本部分</span>
              {/if}
            {/if}
          </label>
        {/each}
      </div>
    </fieldset>

    <fieldset class="params-group">
      <legend>召回调优</legend>
      <p class="params-note">调大召回池通常能提高召回率但更慢；一般无需改动。</p>
      <div class="params-grid">
        {#each advanced.filter((p) => ["candidate_k", "nprobes", "refine_factor", "k_rrf", "rerank"].includes(p.key)) as p (p.id)}
          <label class="field">
            <span class="field-label">{p.label}</span>
            {#if p.key === "rerank"}
              <select id={p.id} value={val(p.key)} onchange={(e) => setParam(p.key, e.currentTarget.value)}>
                <option value="">按服务端配置</option>
                <option value="false">关闭</option>
                <option value="true">开启</option>
              </select>
            {:else}
              <input id={p.id} type="number" value={val(p.key)} placeholder="默认"
                onchange={(e) => setParam(p.key, e.currentTarget.value)} />
            {/if}
          </label>
        {/each}
      </div>
    </fieldset>

    <div class="params-foot">
      <button class="btn btn-sm" type="button" onclick={resetParams}>恢复默认</button>
    </div>
  </div>
</details>

{#if dirty}
  <!-- 待应用的参数改动必须**挡在结果前面**。
       旧行为是改了参数直接改 URL 但不重跑：屏幕上参数面板是新值、
       结果集是旧值，且没有任何提示——界面在说谎。
       现在改动先进暂存区，这里明说"有几项还没生效"，并给出去除它们的出口。 -->
  <div class="pending" role="status">
    <Icon name="alert" />
    <span>
      已修改 {dirtyKeys.length} 项参数（{dirtyKeys.map((k) => PARAMS.find((p) => p.key === k)?.label ?? k).join("、")}），
      下面的结果还是上一次检索的
    </span>
    <div class="pending-acts">
      <button class="btn btn-sm" type="button" onclick={discardParams}>放弃改动</button>
      <button class="btn btn-sm btn-primary" type="button" onclick={apply}>应用并重查</button>
    </div>
  </div>
{/if}

{#if !q}
  <StateBlock title="输入关键词开始检索"
    text="输入关键词或一句话即可。混合模式同时走语义与关键词，适合大多数场景；专有名词用「关键词」，找意思相近的内容用「语义」。参数面板可进一步限定范围与质量。" />
{:else if ld.state.phase === "failed"}
  <StateBlock kind="error" title="检索请求失败" text={ld.state.error}
    action="重试" onaction={() => void run()} />
{:else if ld.state.phase === "initial"}
  <div class="rows" aria-busy="true">
    {#each [1, 2, 3] as i (i)}<div class="row skeleton" aria-hidden="true"></div>{/each}
  </div>
{:else if res && res.results.length === 0}
  <StateBlock title="没有命中"
    text={activeFilters.length
      ? "先看看是不是下面的过滤条件挡住了。"
      : "换个说法、放宽过滤条件，或调低「最低相关度」。语义相近但用词不同的内容，用「语义」模式更容易命中；专有名词用「关键词」更准。"}
    action={activeFilters.length ? "清空过滤" : undefined}
    onaction={activeFilters.length
      ? () => commit(Object.fromEntries(activeFilters.map((f) => [f.key, ""])))
      : undefined} />
{:else if res}
  <div class="resultbar">
    <p class="resultbar-meta">
      范围 {kbScope} · 模式 {modeLabel[res.mode] ?? res.mode} · {res.results.length} / {res.total_candidates} 条命中
      · {Math.round(res.took_ms)} ms · 分数口径 {kindLabel[res.score_kind] ?? res.score_kind}
      {#if res.degraded_reason}<span class="warn"> · 已降级：{res.degraded_reason}</span>{/if}
    </p>
    <div class="chips">
      {#each activeFilters as f (f.key)}
        <button class="chip chip-x" type="button" onclick={() => commit({ [f.key]: "" })}
          title="移除该过滤条件并重新检索">
          {f.label}：{f.value} <Icon name="close" />
        </button>
      {/each}
    </div>
  </div>

  <div class="searchsplit">
    <div class="resultlist">
      {#each res.results as hit, i (hit.chunk_id)}
        <!-- 卡片本身是 div：主操作是那个按钮，"在文档中打开"是它的**兄弟**
             链接。<a> 嵌在 <button> 里是无效 HTML，而且点击语义会互相吞。 -->
        <div class="result" class:active={selectedIdx === i} in:fly={enterItem(i)}>
          <button class="result-main" type="button" aria-pressed={selectedIdx === i}
            bind:this={hitEls[hit.chunk_id]}
            onclick={() => select(i)} onkeydown={(e) => onResultKey(e, i)}>
            <div class="result-head">
              <span class="result-title">{hit.title || "独立分块"}</span>
              <span class="result-score mono">{hit.score.toFixed(4)}</span>
            </div>
            <div class="result-sub muted">
              {#if hit.heading_path || hit.page}
                <span class="result-path">
                  {hit.heading_path || ""}{hit.page ? `${hit.heading_path ? " · " : ""}第 ${hit.page} 页` : ""}
                </span>
              {/if}
              <span class="mono">#{hit.ordinal}</span>
              {#if hit.origin === "manual"}<span class="badge">手工</span>{/if}
              {#if hit.edited}<span class="badge badge-warn">已编辑</span>{/if}
              {#if hit.enabled === false}<span class="badge badge-err">已停用</span>{/if}
            </div>
            <div class="result-snip">
              <SnippetView snippet={hit.snippet} />
            </div>
          </button>
          {#if openDoc(hit)}
            <a class="result-open" href={openDoc(hit)}
              title="在文档详情页打开（可编辑分块、看前后块）">
              <Icon name="doc" /> 在文档中打开
            </a>
          {/if}
        </div>
      {/each}
    </div>

    <aside class="split-doc" aria-label="原文检视">
      <div class="docreader">
        {#if viewer.kind === "empty"}
          <div class="docreader-empty">
            <Icon name="search" size="ico-lg" />
            <p>选择左侧命中项，在此比对原文上下文。</p>
          </div>
        {:else if viewer.kind === "loading"}
          <div class="docreader-empty"><p>正在加载原文…</p></div>
        {:else if viewer.kind === "error"}
          <StateBlock kind="error" title="无法加载原文" text={viewer.error ?? ""} />
        {:else if viewer.kind === "standalone"}
          <h2 class="docreader-title">{viewer.title}</h2>
          <p class="docreader-sub">{viewer.sub}</p>
          <div class="fulltext"><SnippetView snippet={viewer.snippet} /></div>
        {:else}
          <h2 class="docreader-title">{viewer.title}</h2>
          {#if viewer.sub}<p class="docreader-sub">{viewer.sub}</p>{/if}
          {#if viewer.context && viewer.context.length > 0}
            <p class="docreader-sub">命中与上下文</p>
            {#each viewer.context as [ord, text] (ord)}
              <div class="ctx" class:is-hit={Number(ord) === Number(viewer.ordinal)}>{text}</div>
            {/each}
          {/if}
          <p class="docreader-sub">整篇文档</p>
          {#if viewer.markStart !== undefined && viewer.markStart >= 0}
            <div class="fulltext">
              {viewer.full!.slice(0, viewer.markStart)}<mark use:reveal={{ key: String(viewer.markStart) }}>{viewer.full!.slice(viewer.markStart, viewer.markEnd)}</mark>{viewer.full!.slice(viewer.markEnd)}
            </div>
          {:else if viewer.full}
            <div class="fulltext">{viewer.full}</div>
          {:else}
            <StateBlock title="没有可显示的原文" text="该文档未保存解析后的文本内容。" />
          {/if}
        {/if}
      </div>
    </aside>
  </div>
{/if}

<style>
  .qbar { margin-bottom: var(--u4); }
  .params { margin-bottom: var(--u4); }
  .params-inline { margin-bottom: var(--u3); }
  .resultbar { justify-content: space-between; }
  .chips { display: flex; gap: var(--u2); flex-wrap: wrap; }
  .warn { color: var(--yellow); }

  /* 待应用参数横幅：出现在结果之上，因为它描述的是"下面的结果不是最新的" */
  .pending {
    display: flex; align-items: center; gap: var(--u2); flex-wrap: wrap;
    margin-bottom: var(--u3); padding: var(--u2) var(--u3);
    border: 1px solid var(--accent-line); border-radius: var(--r-md);
    background: var(--accent-soft); font-size: 12.5px;
  }
  .pending > span { flex: 1; min-width: 0; }
  .pending-acts { display: flex; gap: var(--u2); flex: none; }

  /* 结果卡片：主按钮 + 深链兄弟 */
  .result-main {
    display: block; width: 100%;
    padding: var(--u4); padding-bottom: var(--u2);
    background: none; border: 0; border-radius: var(--r-md);
    color: inherit; font: inherit; text-align: start; cursor: pointer;
  }
  .result-open {
    display: inline-flex; align-items: center; gap: var(--u1);
    margin: 0 var(--u4) var(--u3); padding: 2px var(--u2);
    border-radius: var(--r-sm);
    font-size: 11.5px; color: var(--text-3);
    transition: background 100ms var(--ease), color 100ms var(--ease);
  }
  .result-open:hover { background: var(--surface-3); color: var(--accent); }

  .rows { display: flex; flex-direction: column; gap: var(--u2); }
  .row { height: 76px; }
  .skeleton { animation: pulse 1.4s ease-in-out infinite; }
  @keyframes pulse { 50% { opacity: 0.55; } }
  @media (prefers-reduced-motion: reduce) {
    .skeleton { animation: none; }
  }
</style>
