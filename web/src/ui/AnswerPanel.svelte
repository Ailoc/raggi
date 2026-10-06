<script lang="ts">
  /**
   * 流式问答面板。
   *
   * 三条设计约束（与后端事件序列对应）：
   * - **引用先出**：sources 事件先于正文，界面立刻显示「依据哪些分块」，
   *   用户不必对着空白等生成；
   * - **逐字追加**：delta 事件直接追加，不等整段——这就是流式的意义；
   * - **停止是真的停**：stop() 断开连接，后端随之放弃，而不是只在
   *   界面上把 loading 关掉。
   */
  import { onDestroy } from "svelte";
  import { streamPost } from "../lib/sse";
  import { pinBottom } from "../lib/actions";
  import type { StreamHandle } from "../lib/sse";
  import { app } from "../lib/stores.svelte";
  import { fmtMs } from "../lib/format";
  import Icon from "./Icon.svelte";
  import StateBlock from "./StateBlock.svelte";
  import Snippet from "./Snippet.svelte";
  import { toast } from "./toast.svelte";
  import type { AnswerStreamDone, Citation } from "../lib/types";

  let {
    kbId = "",
    defaultQuestion = "",
  }: { kbId?: string; defaultQuestion?: string } = $props();

  let question = $state("");
  let answer = $state("");
  let citations = $state<Citation[]>([]);
  let running = $state(false);
  let stopped = $state(false);
  let error = $state("");
  let done = $state<AnswerStreamDone | null>(null);
  let selected = $state<Citation | null>(null);
  let topK = $state(5);

  let handle: StreamHandle | null = null;

  // 只在用户还没自己输入时跟随外部关键词：检索页把当前 q 作为
  // 问答的预填，改关键词不该把他已经写好的问句冲掉。
  let touched = $state(false);
  $effect(() => {
    if (!touched && defaultQuestion) question = defaultQuestion;
  });

  // 贴底跟随交给 `use:pinBottom`（lib/actions.ts）。这里曾是一个只读 running
  // 的 effect：正文增长不触发它，于是"跟随输出"只在流式开始时滚了一次空内容。
  onDestroy(() => handle?.stop());

  /**
   * delta 缓冲 + 按帧刷新。
   *
   * 原来每个 token 都做一次 `answer += text`，而模板里是
   * `{#each splitCitations(answer)}`——每来一个字都要把**整段已累积的文本**
   * 重新解析一遍引用并重建片段，长度线性增长、次数也线性增长，整体是
   * O(n²)；同时 each 用下标做 key，前面的片段每次都被判定"变了"而重建。
   * 长答案滚起来会明显发涩。
   *
   * 攒到一帧再落地：视觉上仍是逐字流（60fps 足够），解析次数从
   * "每 token 一次"降到"每帧最多一次"。
   */
  let buf = "";
  let flushQueued = false;

  function flushNow(): void {
    flushQueued = false;
    if (!buf) return;
    answer += buf;
    buf = "";
  }
  function queueFlush(): void {
    if (flushQueued) return;
    flushQueued = true;
    requestAnimationFrame(flushNow);
  }

  async function ask(): Promise<void> {
    const q = question.trim();
    if (!q || running) return;
    touched = true;
    answer = ""; citations = []; done = null; error = ""; stopped = false;
    selected = null; buf = ""; flushQueued = false;
    running = true;

    handle = streamPost("/api/answer/stream",
      { q, top_k: topK, kb_id: kbId || null },
      {
        onSources: (cs) => {
          citations = cs as Citation[];
          selected = citations[0] ?? null;
        },
        onDelta: (text) => { buf += text; queueFlush(); },
        onDone: (info) => { flushNow(); done = info; running = false; },
        onError: (message, hint) => {
          flushNow();
          error = message + (hint ? `（${hint}）` : "");
          running = false;
        },
      });
  }

  function stop(): void {
    handle?.stop();
    flushNow();   // 已生成的部分要留在屏幕上，不能被半截缓冲吃掉
    stopped = true;
    running = false;
    toast("已停止生成");
  }

  function reset(): void {
    answer = ""; citations = []; done = null; error = ""; selected = null;
  }

  interface Seg { text: string; ref: number | null }

  /**
   * 把答案里的 `[n]` 拆成可点击片段。
   *
   * 答案中的引用编号是**唯一**的「答案 ↔ 依据」连接，做成可点才能从
   * 结论直接跳到原文；不这么做的话用户只能自己数第几个依据对不对得上。
   * 只认被引用表覆盖的编号，避免把 `[2024]` 这类正文数字误当引用。
   */
  function splitCitations(text: string): Seg[] {
    if (!text) return [];
    const out: Seg[] = [];
    const re = /\[(\d{1,2})\]/g;
    let last = 0;
    for (let m = re.exec(text); m; m = re.exec(text)) {
      const n = Number(m[1]);
      if (n < 1 || n > citations.length) continue;
      if (m.index > last) out.push({ text: text.slice(last, m.index), ref: null });
      out.push({ text: m[0], ref: n });
      last = m.index + m[0].length;
    }
    if (last < text.length) out.push({ text: text.slice(last), ref: null });
    return out;
  }
</script>

<section class="answer-panel" aria-label="问答">
  <form class="ask-bar" onsubmit={(e) => { e.preventDefault(); void ask(); }}>
    <input bind:value={question} type="text" placeholder="就库里的内容提问…"
      aria-label="问题" disabled={running} />
    <label class="topk" title="召回多少个分块作为依据">
      依据
      <input bind:value={topK} type="number" min="1" max="20" step="1"
        aria-label="召回条数" disabled={running} />
    </label>
    {#if running}
      <button class="btn btn-danger" type="button" onclick={stop}>
        <Icon name="close" /> 停止
      </button>
    {:else}
      <button class="btn btn-primary" type="submit" disabled={!question.trim()}>
        <Icon name="search" /> 提问
      </button>
    {/if}
  </form>

  {#if error}
    <StateBlock kind="error" title="问答失败" text={error} action="重试" onaction={ask} />
  {:else if !answer && !running && citations.length === 0}
    <p class="hint">
      依据库里的分块作答，答案中的 <code>[n]</code> 对应下方引用。
      {#if app.kbs.length === 0}先在「知识库」里入库一些内容。{/if}
    </p>
  {:else}
    <div class="answer-body">
      <div class="answer-main">
        {#if running && !answer}
          <p class="pending">正在检索依据…</p>
        {/if}
        <div class="answer-text" use:pinBottom={{ follow: running, text: answer }}>
          {#each splitCitations(answer) as seg, i (i)}
            {#if seg.ref !== null}
              <button class="cite-ref" type="button"
                title={citations[seg.ref - 1]?.title || `引用 ${seg.ref}`}
                onclick={() => (selected = citations[seg.ref! - 1] ?? null)}>
                [{seg.ref}]
              </button>
            {:else}
              {seg.text}
            {/if}
          {/each}
          {#if running}<span class="caret" aria-hidden="true"></span>{/if}
        </div>
        {#if stopped}
          <p class="hint">已停止（后端随之放弃本次生成）。</p>
        {:else if done}
          <p class="hint">
            {fmtMs(done.took_ms)} · 模式 {done.mode}
            {#if done.degraded_reason}· 已降级：{done.degraded_reason}{/if}
          </p>
        {/if}
        {#if answer && !running}
          <div class="ask-actions">
            <button class="btn btn-sm" type="button" onclick={reset}>清空</button>
          </div>
        {/if}
      </div>

      {#if citations.length > 0}
        <aside class="ask-cites" aria-label="引用">
          <p class="cites-title">
            依据 {#if running}<span class="badge">检索中</span>{/if}
          </p>
          <ul class="cite-list">
            {#each citations as c (c.chunk_id)}
              <li>
                <button class="cite" class:active={selected?.chunk_id === c.chunk_id}
                  type="button" onclick={() => (selected = c)}>
                  <span class="cite-idx">[{c.index}]</span>
                  <span class="cite-title">
                    {c.title || "独立分块"}
                    {#if c.page}<span class="muted"> · 第 {c.page} 页</span>{/if}
                  </span>
                  <span class="cite-score mono">{c.score.toFixed(3)}</span>
                </button>
                {#if selected?.chunk_id === c.chunk_id}
                  <!-- 结构化片段：纯文本由 Svelte 自动转义，高亮区间由
                       Snippet 组件渲染。早前这里是 {@html c.snippet}，
                       安全性全靠后端每次都记得转义——入库文本来自 URL
                       抓取，漏一次就是存储型 XSS。 -->
                  <p class="cite-snip">
                    <Snippet snippet={c.snippet} />
                  </p>
                  {#if !c.snippet?.text && c.heading_path}
                    <p class="cite-snip">{c.heading_path}</p>
                  {/if}
                  {#if c.doc_id}
                    <a class="cite-link"
                      href={`#/kb/${encodeURIComponent(kbId)}/doc/${encodeURIComponent(c.doc_id)}`}>
                      查看原文 <Icon name="arrow-left" />
                    </a>
                  {/if}
                {/if}
              </li>
            {/each}
          </ul>
        </aside>
      {/if}
    </div>
  {/if}
</section>

<style>
  .answer-panel {
    margin-top: var(--u6);
    padding: var(--u4);
    border: 1px solid var(--line);
    border-radius: var(--r-lg);
    background: var(--surface);
  }

  .ask-bar { display: flex; align-items: center; gap: var(--u2); }
  .ask-bar > input[type="text"] { flex: 1 1 auto; min-width: 0; }
  .topk {
    display: flex; align-items: center; gap: var(--u1);
    font-size: 12px; color: var(--text-3); flex: none;
  }
  .topk input { width: 64px; }

  .answer-body {
    display: grid; gap: var(--u4); margin-top: var(--u4);
    grid-template-columns: minmax(0, 1.5fr) minmax(0, 1fr);
    align-items: start;
  }
  @media (max-width: 900px) { .answer-body { grid-template-columns: 1fr; } }

  .answer-text {
    font-size: 14px; line-height: 1.75; color: var(--text);
    white-space: pre-wrap; word-break: break-word;
    max-height: 46vh; overflow-y: auto;
    padding: var(--u3);
    background: var(--bg-sunken); border-radius: var(--r-md);
  }
  .answer-text :global(code) {
    font-family: var(--font-mono); font-size: .92em;
    background: var(--surface-3); padding: 1px 4px; border-radius: 3px;
  }
  /* 答案里的 [n]：点它跳到对应依据 */
  .cite-ref {
    display: inline; padding: 0 2px; margin: 0 1px;
    font: inherit; font-size: .9em; font-family: var(--font-mono);
    color: var(--amber); cursor: pointer;
    background: var(--amber-soft); border: 0; border-radius: 3px;
  }
  .cite-ref:hover { background: var(--amber); color: var(--bg); }

  /* 生成中的光标：让「还在出字」一眼可见，而不是盯着不动 */
  .caret {
    display: inline-block; width: 7px; height: 1em;
    margin-left: 2px; vertical-align: text-bottom;
    background: var(--amber); animation: blink 1s steps(2) infinite;
  }
  @keyframes blink { 50% { opacity: 0; } }
  @media (prefers-reduced-motion: reduce) { .caret { animation: none; } }

  .pending { margin: 0 0 var(--u2); font-size: 12.5px; color: var(--text-3); }
  .hint { margin: var(--u2) 0 0; font-size: 12px; color: var(--text-3); }
  .hint code {
    font-family: var(--font-mono); font-size: .92em;
    background: var(--surface-3); padding: 0 4px; border-radius: 3px;
  }
  .ask-actions { margin-top: var(--u2); }

  .ask-cites {
    min-width: 0;
    padding: var(--u3);
    border: 1px solid var(--line); border-radius: var(--r-md);
    background: var(--bg-sunken);
    max-height: 46vh; overflow-y: auto;
  }
  .cites-title {
    display: flex; align-items: center; gap: var(--u2);
    margin: 0 0 var(--u2); font-size: 12px; font-weight: 600;
    color: var(--text-2);
  }
  .cite-list { display: flex; flex-direction: column; gap: var(--u1); }
  .cite {
    display: flex; align-items: baseline; gap: var(--u2); width: 100%;
    padding: var(--u1) var(--u2); text-align: start; cursor: pointer;
    background: none; border: 1px solid transparent; border-radius: var(--r-sm);
    color: var(--text-2); font-size: 12.5px;
  }
  .cite:hover { background: var(--surface-2); }
  .cite.active { border-color: var(--amber-line); background: var(--amber-soft); }
  .cite-idx { flex: none; font-family: var(--font-mono); color: var(--amber); }
  .cite-title {
    flex: 1 1 auto; min-width: 0; overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap;
  }
  .cite-score { flex: none; font-size: 11px; color: var(--text-3); }
  .cite-snip {
    margin: var(--u1) 0 0 var(--u2); padding: var(--u2);
    font-size: 12px; line-height: 1.65; color: var(--text-2);
    background: var(--surface); border-radius: var(--r-sm);
    border-inline-start: 2px solid var(--amber);
  }
  /* snippet 里的命中词高亮（后端已生成 <mark>） */
  .cite-snip :global(mark) {
    background: var(--amber-soft); color: var(--amber);
    border-radius: 2px; padding: 0 2px;
  }
  .cite-link {
    display: inline-flex; align-items: center; gap: var(--u1);
    margin: var(--u1) 0 0 var(--u2);
    font-size: 11.5px; color: var(--text-3); text-decoration: none;
  }
  .cite-link:hover { color: var(--amber); }
</style>
