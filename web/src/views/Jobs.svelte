<script lang="ts">
  /**
   * 任务列表：入库队列的全景。
   *
   * 为什么单独一页而不是藏在文档列表里：任务与文档是**不同维度**——
   * 一个入库任务可能还没产出文档（此时文档页上看不到任何东西），
   * 失败的任务也不会留下文档。卡住的任务在此处可见、可停止，
   * 而不是让人猜「到底是在跑还是死了」。
   */
  import { api } from "../lib/api";
  import { setCrumbs } from "../lib/crumbs.svelte";
  import { stageLabel } from "../lib/jobs";
  import { cancelJob } from "../lib/jobcancel";
  import { loader, Pager } from "../lib/loadstate.svelte";
  import { fmtTimeShort, fmtSpan } from "../lib/format";
  import Icon from "../ui/Icon.svelte";
  import ListMore from "../ui/ListMore.svelte";
  import StateBlock from "../ui/StateBlock.svelte";
  import type { Job, JobList } from "../lib/types";

  type Filter = "all" | "active" | "done" | "failed";

  const ld = loader();
  /** offset 分页。旧实现是 limit=100 一屏定死且没有"再加载"，超出的任务静默不可见 */
  const pager = new Pager(100);
  let rows = $state<Job[]>([]);
  let queueStats = $state<{ workers: number; pending: number; max_pending: number } | null>(null);
  let filter = $state<Filter>("all");
  let autoRefresh = $state(true);

  let timer: ReturnType<typeof setInterval> | undefined;

  $effect(() => {
    setCrumbs([{ label: "任务" }]);
    void load();
    // 有任务在跑时轮询，全部终态就停——不长期空转打接口。
    // 用户手动关掉自动刷新后也不再自动恢复：那是一个明确的选择。
    // 标签页不可见时**跳过本轮**（不是停掉定时器）：每轮还会带
    // loadTitles() 的十几次 /documents 请求，后台标签页每 2 秒打一轮
    // 是在给没人看的界面付账单。回到前台由下面的 visibilitychange 补一次。
    timer = setInterval(() => {
      if (!autoRefresh) return;
      if (document.visibilityState === "hidden") return;
      if (rows.some((j) => !j.terminal)) void load();
    }, 2000);
    const onVisible = (): void => {
      if (document.visibilityState === "visible" && autoRefresh) void load();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  });

  /**
   * 取任务列表。已有数据时只置 refreshing：表格不重挂载，
   * 用户正在读的那一行不会每 2 秒被换掉。
   */
  async function load(append = false): Promise<boolean> {
    return ld.run(async () => {
      // 前缀统一走 /api。原先这里是 /api/v1 而全站其余用 /api，
      // 后端双挂载让它俩都能跑，于是这个不一致一直没人发现
      const sp = new URLSearchParams({
        limit: String(pager.size), offset: String(append ? pager.offset : 0),
      });
      return api<JobList>(`/api/jobs?${sp.toString()}`);
    }, (r) => {
      rows = append ? [...rows, ...r.items] : r.items;
      pager.advance(r.items.length, r.total ?? 0);
      queueStats = r.queue ?? null;
      void loadTitles();
    });
  }

  /** doc_id → 标题。UUID 对用户毫无意义，必须换成能认出的标题。 */
  let titles = $state<Record<string, string>>({});

  /**
   * 只补**没见过**的标题，而不是每 2 秒重取 500 篇文档。
   *
   * 旧实现在每次轮询里都 `/documents?limit=500` 全量拉一遍来建标题表：
   * 2 秒一次、每次 500 行，只为给十几行任务补标题——单机上这是白给的读放大。
   * 后端没有按 id 批量取文档的接口，所以这里逐个取，且每轮限量，
   * 取不到的（文档已删）保持 UUID 显示。
   */
  const TITLES_PER_ROUND = 12;

  async function loadTitles(): Promise<void> {
    const missing = [...new Set(rows.map((j) => j.doc_id).filter(Boolean))]
      .filter((id) => titles[id] === undefined)
      .slice(0, TITLES_PER_ROUND);
    if (missing.length === 0) return;
    // 先占位，避免下一轮轮询又把同一批 id 当成缺失反复请求
    for (const id of missing) titles[id] = "";
    await Promise.all(missing.map(async (id) => {
      try {
        const d = await api<{ doc_id: string; title: string }>(
          `/api/documents/${encodeURIComponent(id)}`);
        titles[id] = d.title || "";
      } catch {
        // 文档可能已被删除：留空，渲染时回落到 UUID 前缀
      }
    }));
  }

  function match(j: Job, f: Filter): boolean {
    switch (f) {
      case "active": return !j.terminal;
      case "done": return j.stage === "done";
      case "failed": return j.stage === "failed" || j.stage === "cancelled";
      default: return true;
    }
  }

  const shown = $derived(rows.filter((j) => match(j, filter)));

  /**
   * 计数只覆盖**已加载的这些行**。
   *
   * 后端 /jobs 不接受按状态过滤，所以"全部 137"这种写法在超过一屏时
   * 是在说谎。文案里明确标"已加载 N 条中"，宁可啰嗦也不要假精确。
   */
  const counts = $derived({
    all: rows.length,
    active: rows.filter((j) => match(j, "active")).length,
    done: rows.filter((j) => match(j, "done")).length,
    failed: rows.filter((j) => match(j, "failed")).length,
  });

  function statusClass(j: Job): string {
    if (j.stage === "done") return "badge-ok";
    if (j.stage === "failed") return "badge-err";
    if (j.stage === "cancelled") return "badge-warn";
    if (j.stage === "cancelling") return "badge-warn";
    return "badge-warn";
  }

  /* 时间格式化统一走 lib/format.ts；任务表用紧凑式（同一天内年份是噪声） */

  function elapsed(j: Job): string {
    const start = Date.parse(j.started_at);
    const end = j.ended_at ? Date.parse(j.ended_at) : Date.now();
    if (!Number.isFinite(start) || !Number.isFinite(end)) return "—";
    return fmtSpan(Math.max(0, (end - start) / 1000));
  }

  const docHref = (j: Job): string | null => {
    // doc_id 要到切分前才生成；kb_id 是入队时就有的。两者都有才能链回去。
    // 旧实现在这里写反了：`if (!j.kb_id) return "#/kb/" + kb_id`——kb_id 为空
    // 时拼出 "#/kb/"，点上去跳到知识库列表，看着像链接坏了
    if (!j.doc_id || !j.kb_id) return null;
    return `#/kb/${encodeURIComponent(j.kb_id)}/doc/${encodeURIComponent(j.doc_id)}`;
  };

  const filters: { key: Filter; label: string }[] = [
    { key: "all", label: "全部" },
    { key: "active", label: "进行中" },
    { key: "done", label: "完成" },
    { key: "failed", label: "失败/取消" },
  ];
</script>

<header class="page-head">
  <div class="page-head-main">
    <h1 class="page-title">任务</h1>
    <p class="page-sub">
      入库与重切分的队列记录。进行中的任务会自动刷新，全部结束后停止。
    </p>
  </div>
  <div class="page-head-actions">
    {#if queueStats}
      <span class="queue-stat" title="入库队列">
        队列 {queueStats.pending}/{queueStats.max_pending} · {queueStats.workers} 工人
      </span>
    {/if}
    <label class="switch auto">
      <input type="checkbox" bind:checked={autoRefresh} />
      <span class="switch-track"></span>
      <span class="switch-label">自动刷新</span>
    </label>
    <button class="btn" type="button" onclick={() => void load()}>
      <Icon name="refresh" /> 刷新
    </button>
  </div>
</header>

<nav class="tabs" aria-label="任务筛选">
  {#each filters as f (f.key)}
    <button class="tab" class:active={filter === f.key} type="button"
      aria-pressed={filter === f.key} onclick={() => (filter = f.key)}>
      {f.label}
      <span class="tab-count">{counts[f.key]}</span>
    </button>
  {/each}
  {#if pager.more}
    <!-- 计数只覆盖已加载的行，这一点必须写在脸上，不能让用户以为
         "全部 100" 就是全库只有 100 个任务 -->
    <span class="tab-note muted">计数仅覆盖已加载的 {rows.length} 条</span>
  {/if}
  {#if ld.state.refreshing}
    <span class="tab-note muted" role="status">更新中…</span>
  {/if}
</nav>

{#if ld.state.phase === "failed"}
  <StateBlock kind="error" title="加载任务失败" text={ld.state.error}
    action="重试" onaction={() => void load()} />
{:else if ld.state.phase === "initial"}
  <div class="rows" aria-busy="true">
    {#each [1, 2, 3] as i (i)}<div class="row skeleton" aria-hidden="true"></div>{/each}
  </div>
{:else if rows.length === 0}
  <StateBlock title="还没有任务记录"
    text="入库、重解析或重新切分都会在这里留下记录。" />
{:else}
  {#if ld.state.error}
    <div class="banner-err" role="alert">
      <Icon name="alert" />
      <span>刷新失败，下面是上一次取到的结果：{ld.state.error}</span>
      <button class="btn btn-sm" type="button" onclick={() => void load()}>重试</button>
    </div>
  {/if}

  {#if shown.length === 0}
    <StateBlock title="没有符合条件的任务"
      text="换个筛选条件看看；也可以「再加载」把更早的任务取回来。"
      action="看全部" onaction={() => (filter = "all")} />
  {:else}
  <div class="table-wrap">
    <div class="table-scroll">
      <table class="docs">
        <thead>
          <tr>
            <th>状态</th><th>阶段</th><th>产出</th>
            <th>引擎</th><th class="num">耗时</th>
            <th>开始</th><th>结束</th>
            <th class="actions"><span class="sr-only">操作</span></th>
          </tr>
        </thead>
        <tbody>
          {#each shown as j (j.job_id)}
            <tr>
              <td><span class="badge {statusClass(j)}">{j.stage}</span></td>
              <td>
                {#if j.terminal}
                  <span class="muted">{j.total > 0 ? `${j.total} 块` : "—"}</span>
                {:else}
                  <span class="stage-cell">
                    {stageLabel(j.stage)}
                    {#if j.total > 0}<span class="muted"> · {j.total} 块</span>{/if}
                    {#if j.stage === "embed" && j.total > 0}
                      <span class="bar" role="progressbar"
                        aria-valuenow={Math.round((j.progress || 0) * 100)}
                        aria-valuemin="0" aria-valuemax="100">
                        <span class="bar-fill"
                          style="width:{Math.round((j.progress || 0) * 100)}%"></span>
                      </span>
                    {/if}
                  </span>
                {/if}
              </td>
              <td>
                {#if docHref(j)}
                  <a class="doc-link" href={docHref(j) ?? "#/"}
                    title={j.doc_id}>
                    {titles[j.doc_id] || j.doc_id.slice(0, 8)}
                  </a>
                {:else if j.doc_id}
                  <span class="muted" title={j.doc_id}>{j.doc_id.slice(0, 8)}</span>
                {:else}
                  <span class="muted">未产出</span>
                {/if}
              </td>
              <td class="muted">{j.parser_engine || "—"}</td>
              <td class="num">{elapsed(j)}</td>
              <td class="time">{fmtTimeShort(j.started_at)}</td>
              <td class="time">{fmtTimeShort(j.ended_at)}</td>
              <td class="actions">
                {#if !j.terminal}
                  <button class="btn btn-sm btn-danger" type="button"
                    onclick={() => cancelJob(j.job_id, () => { void load(); })}>
                    停止
                  </button>
                {:else if j.error}
                  <!-- 失败原因是这页最有价值的信息，旧实现用 max-width:22ch
                       把它截成省略号，只能靠 hover 看 title——触屏上根本读不到。
                       现在折叠起来，点开就是全文 -->
                  <details class="err-box">
                    <summary>{j.error.slice(0, 40)}{j.error.length > 40 ? "…" : ""}</summary>
                    <p class="err-full">{j.error}</p>
                  </details>
                {/if}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  </div>

    {#if pager.more}
      <ListMore loaded={pager.loaded} total={pager.total} size={pager.size}
        remaining={pager.remaining} unit="条" busy={ld.state.refreshing}
        onmore={() => void load(true)} />
    {/if}
  {/if}
{/if}

<style>
  .queue-stat { font-size: 12px; color: var(--text-3); font-variant-numeric: tabular-nums; }
  .auto { margin: 0; }

  .tabs { display: flex; gap: var(--u1); margin-bottom: var(--u4); border-bottom: 1px solid var(--line); }
  .tab {
    display: inline-flex; align-items: center; gap: var(--u2);
    padding: var(--u2) var(--u3); cursor: pointer;
    background: none; border: 0; border-bottom: 2px solid transparent;
    color: var(--text-2); font-size: 13px;
  }
  .tab:hover { color: var(--text); }
  .tab.active { color: var(--amber); border-bottom-color: var(--amber); }
  .tab-count {
    font-size: 11px; color: var(--text-3);
    padding: 0 5px; border-radius: 999px; background: var(--surface-3);
    font-variant-numeric: tabular-nums;
  }
  .tab.active .tab-count { background: var(--amber-soft); color: var(--amber); }

  .stage-cell { display: inline-flex; align-items: center; gap: var(--u2); }
  /* 向量化进度：只在真正有分母时画，否则一个不知道多宽的条毫无意义 */
  .bar {
    display: inline-block; width: 72px; height: 4px;
    background: var(--surface-3); border-radius: 999px; overflow: hidden;
  }
  .bar-fill { display: block; height: 100%; background: var(--amber); }

  .doc-link { color: var(--text); text-decoration: none; font-size: 12.5px; }
  .doc-link:hover { color: var(--amber); text-decoration: underline; }

  /* 失败原因：折叠的全文，取代旧的 max-width:22ch + title= 方案
     （title 在触屏上读不到，而这是这页最有价值的一列） */
  .err-box { max-width: 34ch; font-size: 11.5px; }
  .err-box summary {
    cursor: pointer; color: var(--red);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .err-full {
    margin-top: var(--u1); padding: var(--u2);
    background: var(--bg-sunken); border-radius: var(--r-sm);
    color: var(--text-2); font-family: var(--font-mono);
    font-size: 11.5px; line-height: 1.5;
    white-space: pre-wrap; word-break: break-word; max-width: 46ch;
  }
  .tab-note { margin-inline-start: auto; align-self: center; font-size: 11.5px; }
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
  @media (prefers-reduced-motion: reduce) { .skeleton { animation: none; } }
</style>