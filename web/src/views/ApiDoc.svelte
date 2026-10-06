<script lang="ts">
  import { AUTH_NOTE, GROUPS, curlFor } from "../data/apidoc";
  import type { Endpoint, Method } from "../data/apidoc";
  import { tick } from "svelte";
  import { reveal, selectText } from "../lib/actions";
  import { go } from "../lib/router.svelte";
  import StateBlock from "../ui/StateBlock.svelte";
  import Icon from "../ui/Icon.svelte";

  let { query }: { query: URLSearchParams } = $props();

  /**
   * 过滤词同步进 URL（`#/apidoc?q=…`）。
   *
   * 之前只存在内存里：调好一次过滤、刷新就没了，也没法把"所有 /chunks
   * 相关端点"这种视图分享给别人。这一页本来就是给人查的。
   */
  // URL 是过滤词的真相来源，这里只需要它的初值；之后由下面两个 $effect
  // 分别负责「本地 → URL」和「URL → 本地」。写成 derived 反而会让每次
  // 敲字都重建 filter，输入框根本留不住光标。
  // svelte-ignore state_referenced_locally
  const initialQ = query.get("q") ?? "";
  let filter = $state(initialQ);
  /** 最后一次由本页写进 URL 的值：用来区分「URL 是我改的」和「URL 是后退改的」 */
  let pushed = initialQ;
  let copied = $state("");
  let live = $state("");
  let flashId = $state("");

  // 外部改 URL（浏览器后退/手敲地址）时跟随。不做这一步，后退之后
  // 输入框显示的还是刚才的值，等于又造了一次"界面与 URL 不一致"。
  $effect(() => {
    const fromUrl = query.get("q") ?? "";
    if (fromUrl !== pushed) { pushed = fromUrl; filter = fromUrl; }
  });

  $effect(() => {
    // 依赖必须在**同步阶段**读出来。写成 setTimeout 回调里的 `filter.trim()`
    // 时，effect 建立时根本没有读到 filter → 它不会随输入重跑 → URL 永远是
    // #/apidoc，看着像"同步功能没生效"。（实测踩过，浏览器里 hash 不动。）
    const want = filter.trim();
    // 本地态 → URL，debounce 一下：每敲一个字就改写 hash 会让后退键
    // 塞进几十条历史，按一次退一格，体验上像坏了。
    const t = setTimeout(() => {
      if (want === pushed) return;
      pushed = want;
      go(want ? "/apidoc?q=" + encodeURIComponent(want) : "/apidoc");
    }, 300);
    return () => clearTimeout(t);
  });

  /** 文档里的路径可直接粘贴到当前部署上，所以 base 取当前 origin。 */
  const base = $derived(typeof location !== "undefined" ? location.origin : "");
  const total = $derived(GROUPS.reduce((n, g) => n + g.endpoints.length, 0));

  /** 展开的分组。首组默认展开——全部折叠时首屏只有几行标题，
      用户看不到任何接口内容，容易以为页面坏了。 */
  const OPEN_KEY = "raggi.apidoc.open";
  let open = $state<Set<string>>(loadOpen());

  function loadOpen(): Set<string> {
    try {
      const raw = localStorage.getItem(OPEN_KEY);
      if (raw) return new Set(JSON.parse(raw) as string[]);
    } catch { /* 隐私模式下不可用，退回默认 */ }
    return new Set([GROUPS[0]?.id].filter(Boolean) as string[]);
  }

  function persistOpen(next: Set<string>): void {
    open = next;
    try { localStorage.setItem(OPEN_KEY, JSON.stringify([...next])); } catch { /* 忽略 */ }
  }

  function toggle(id: string): void {
    const next = new Set(open);
    if (next.has(id)) next.delete(id); else next.add(id);
    persistOpen(next);
  }

  const shown = $derived(
    filter.trim()
      ? GROUPS.map((g) => ({
          ...g,
          endpoints: g.endpoints.filter((e) =>
            (e.path + e.summary).toLowerCase().includes(filter.trim().toLowerCase())),
        })).filter((g) => g.endpoints.length > 0)
      : GROUPS,
  );

  /**
   * 接口索引跟着过滤结果走。
   *
   * 旧实现索引渲染的是 `GROUPS`（全量），而下面的卡片列表用的是 `shown`
   * （已过滤）——搜一个词，卡片只剩 3 个、索引仍列 51 个，
   * 点索引还会跳到一个已经被过滤掉的端点上。两处必须同源。
   */
  const indexed = $derived(shown);

  /**
   * 过滤命中的分组自动展开。
   *
   * 只有首组默认展开，而搜 "chunks" 命中的往往是别的组：不自动展开的话，
   * 卡片区一张都不渲染，页面上只剩「匹配 8 / 51」和 8 条索引链接——
   * 看着像过滤把内容清光了。（实测就是这么暴露出来的。）
   * 写回的是同一个 `open`，所以自动展开之后用户仍可手动收起某一组。
   */
  $effect(() => {
    const want = filter.trim().toLowerCase();
    if (!want) return;
    const ids = shown.filter((g) => g.endpoints.length > 0).map((g) => g.id);
    // 全部已在 open 里就直接返回：没有这一行，写 open 会再触发本 effect，
    // 形成"写入 → 重跑 → 又写入"的循环
    if (ids.every((id) => open.has(id))) return;
    persistOpen(new Set([...open, ...ids]));
  });

  const shownCount = $derived(
    shown.reduce((n, g) => n + g.endpoints.length, 0),
  );

  const METHOD_TITLE: Record<Method, string> = {
    GET: "读取数据", POST: "创建或触发动作", PUT: "整体更新",
    PATCH: "局部更新", DELETE: "删除数据",
  };

  function esc(s: string): string {
    return s.replace(/[&<>"]/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c] as string));
  }

  /** 极小的行内标记：**粗体**、`代码` 与换行。数据是仓内静态清单，
   *  仍先整体转义再注入标签——以后加入运行时数据也不会变成注入面。 */
  function renderInline(s: string): string {
    return esc(s)
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/`([^`]+)`/g, '<code class="inline">$1</code>')
      .replace(/\n/g, "<br>");
  }

  function announce(msg: string): void { live = msg; }

  /**
   * 复制。navigator.clipboard 需要安全上下文与权限，内网 http 或权限
   * 被拒时会抛错——此时给一个失败后毫无反应的按钮比不给按钮更糟，
   * 因此降级为「选中文本」并明确提示下一步（选区操作在 lib/actions.ts）。
   */
  async function copy(text: string, key: string, el: HTMLElement): Promise<void> {
    try {
      await navigator.clipboard.writeText(text);
      announce(`已复制 ${key}`);
    } catch {
      selectText(el);
      announce("复制失败，已选中，按 Ctrl+C 复制");
    }
    copied = key;
    setTimeout(() => { if (copied === key) copied = ""; }, 2000);
  }

  const epKey = (e: Endpoint): string => `${e.method} ${e.path}`;
  /** DOM id 只用 [A-Za-z0-9_-]：路径里有 `{}` 和 `/`，直接拼会生成非法 id */
  const epDomId = (e: Endpoint): string =>
    `ep-${e.method}-${e.path.replace(/[^a-zA-Z0-9]+/g, "-")}`;

  /**
   * 索引跳转：展开目标所在的分组，等这一帧渲染完，再把它标记成高亮的那一个。
   *
   * 滚动交给卡片上的 `use:reveal`（key 一变就滚），所以这里不需要元素表。
   * 但 `await tick()` 是**必须**的：折叠是用 `hidden` 做的，卡片元素一直挂着，
   * "解除 hidden"和"改 key"若在同一次 flush 里，reveal 就落在一个还没有布局盒的
   * 元素上——实测过，表现是"点了索引只闪一下，页面没动"。
   * `tick()` 等的是渲染真的完成；旧实现是 `setTimeout(…, 50)`，那是赌。
   */
  async function jump(gid: string, e: Endpoint): Promise<void> {
    if (!open.has(gid)) toggle(gid);
    await tick();
    const id = epDomId(e);
    flashId = id;
    setTimeout(() => { if (flashId === id) flashId = ""; }, 1400);
  }

  function paramsOf(ep: Endpoint) { return ep.params ?? []; }
  const inLabel: Record<string, string> = {
    path: "路径", query: "查询", body: "请求体", form: "表单",
  };
</script>

<!-- 复制结果对读屏用户不可见，必须经 live region 播报 -->
<div id="apiDocLive" class="sr-only" role="status" aria-live="polite">{live}</div>

<header class="page-head">
  <div class="page-head-main">
    <h1 class="page-title">API 文档</h1>
    <p class="page-sub">{total} 个端点 · 与 openapi.json 双向比对，漂移会失败</p>
  </div>
</header>

<div class="docbar">
  <input class="filter" bind:value={filter} type="search"
    placeholder="按路径或说明过滤端点…" aria-label="过滤 API 端点" />
  {#if filter.trim()}
    <!-- 命中数必须显示：不显示的话过滤到只剩 3 条时，用户不知道是
         "库里只有这些"还是"被我的关键词筛掉了" -->
    <span class="muted">匹配 {shownCount} / {total} 个端点</span>
    <button class="btn btn-sm" type="button" onclick={() => (filter = "")}>清空</button>
  {/if}
</div>

{#if shown.length === 0}
  <StateBlock title="没有匹配的端点" text="换个关键词，或清空过滤条件。"
    action="清空过滤" onaction={() => (filter = "")} />
{:else}
  <div class="stack">
    <section class="panel">
      <div class="panel-head">
        <h2 class="panel-title"><Icon name="info" />概览</h2>
        <p class="panel-desc">
          Raggi 对外暴露 {total} 个 HTTP 接口，全部位于 <code class="inline">/api</code> 前缀下。
          检索与入库可以直接用 curl 调用，无需经过前端。
        </p>
      </div>
      <div class="panel-body">
        <dl class="kv-grid">
          <div class="kv"><dt>Base URL</dt><dd><code class="inline">{base}</code></dd></div>
          <div class="kv"><dt>请求体格式</dt><dd><code class="inline">application/json</code>（文件上传为 multipart）</dd></div>
          <div class="kv"><dt>响应格式</dt><dd><code class="inline">application/json</code>（报告导出为 text/markdown）</dd></div>
          <div class="kv"><dt>机器可读契约</dt><dd><code class="inline">GET /openapi.json</code>（OpenAPI 3.1）</dd></div>
        </dl>
        <div class="apidoc-auth">
          <p class="apidoc-auth-title"><Icon name="key" />鉴权</p>
          <p>{@html renderInline(AUTH_NOTE)}</p>
        </div>
        <div class="apidoc-errors">
          <p class="apidoc-auth-title"><Icon name="alert" />错误约定</p>
          <p>
            错误响应统一为 <code class="inline">{"{"}"detail": "..."{"}"}</code>，
            但状态码区分了原因，排查时请以状态码为准：
          </p>
          <table class="apidoc-table">
            <thead><tr><th>状态码</th><th>含义</th><th>典型场景</th></tr></thead>
            <tbody>
              <tr><td class="mono">400</td><td>请求参数有问题</td><td>缺必填字段、URL 协议不允许、指向内网地址</td></tr>
              <tr><td class="mono">401</td><td>未认证</td><td>配置了密钥或 token 但未携带 Authorization 头</td></tr>
              <tr><td class="mono">403</td><td>权限不足</td><td>编辑解析产生的分块但未带 force=true；密钥 scope 不够</td></tr>
              <tr><td class="mono">404</td><td>对象不存在</td><td>文档 / 分块 / 知识库 / 任务 ID 无效</td></tr>
              <tr><td class="mono">413</td><td>上传超限</td><td>文件超过 max_upload_mb</td></tr>
              <tr><td class="mono">500</td><td>服务端错误</td><td>向量维度失配等，detail 保留原始原因</td></tr>
              <tr><td class="mono">503</td><td>依赖不可用</td><td>embedding / LLM 服务未就绪、入库队列已满，或功能开关未启用</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </section>

    <section class="panel">
      <div class="panel-head">
        <h2 class="panel-title"><Icon name="grid" />接口索引</h2>
        <p class="panel-desc">点击可直接跳到对应接口。基地址 <code class="inline">{base}</code>。</p>
      </div>
      <div class="panel-body apidoc-jump">
        {#each indexed as g (g.id)}
          <div class="apidoc-jump-group">
            <p class="apidoc-jump-title">{g.title}</p>
            <ul>
              {#each g.endpoints as e (epKey(e))}
                <li>
                  <span class="verb verb-{e.method.toLowerCase()}">{e.method}</span>
                  <button class="jump-link" type="button" onclick={() => jump(g.id, e)}>{e.path}</button>
                </li>
              {/each}
            </ul>
          </div>
        {/each}
      </div>
    </section>

    {#each shown as g (g.id)}
      <section class="panel apidoc-group">
        <div class="apidoc-group-head">
          <button class="apidoc-group-toggle" type="button" aria-expanded={open.has(g.id)}
            onclick={() => toggle(g.id)}>
            <span class="caret" class:open={open.has(g.id)}><Icon name="chevron" /></span>
            <span class="apidoc-group-title">{g.title}</span>
            <span class="badge">{g.endpoints.length} 个接口</span>
          </button>
        </div>
        {#if open.has(g.id)}
          <div class="panel-body apidoc-group-body">
            <p class="panel-desc">{@html renderInline(g.desc)}</p>
            {#if g.notes?.length}
              <ul class="apidoc-notes">
                {#each g.notes as n (n)}<li>{@html renderInline(n)}</li>{/each}
              </ul>
            {/if}
            <div class="apidoc-eps">
              {#each g.endpoints as e (epKey(e))}
                {@const curl = curlFor(e, base)}
                <article class="apidoc-ep" id={epDomId(e)}
                  use:reveal={{ key: flashId === epDomId(e) ? flashId : "",
                                block: "start", behavior: "smooth" }}
                  class:is-flash={flashId === epDomId(e)}>
                  <header class="apidoc-ep-head">
                    <span class="verb verb-{e.method.toLowerCase()}">{e.method}</span>
                    <code class="apidoc-path">{e.path}</code>
                  </header>
                  <p class="apidoc-summary">{e.summary}</p>
                  <p class="apidoc-title-note">{METHOD_TITLE[e.method]}</p>

                  {#if e.notes?.length}
                    <ul class="apidoc-notes">
                      {#each e.notes as n (n)}<li>{@html renderInline(n)}</li>{/each}
                    </ul>
                  {/if}

                  {#if paramsOf(e).length > 0}
                    <table class="apidoc-table">
                      <caption class="sr-only">{e.method} {e.path} 的参数</caption>
                      <thead><tr><th>参数</th><th>位置</th><th>类型</th><th>说明</th></tr></thead>
                      <tbody>
                        {#each paramsOf(e) as p (p.name)}
                          <tr>
                            <td class="mono"><code>{p.name}</code>{#if p.required}<span class="req" title="必填">*</span>{/if}</td>
                            <td class="muted">{inLabel[p.in] ?? p.in}</td>
                            <td class="mono muted">{p.type}</td>
                            <td>{p.desc}</td>
                          </tr>
                        {/each}
                      </tbody>
                    </table>
                  {/if}

                  {#if e.example !== undefined}
                    <div class="apidoc-code">
                      <div class="apidoc-code-head">
                        <span>请求示例</span>
                        <button class="icon-btn" class:is-copied={copied === `${epKey(e)} 示例`}
                          type="button" aria-label="复制请求示例"
                          onclick={(ev) => copy(JSON.stringify(e.example, null, 2),
                            `${epKey(e)} 示例`, ev.currentTarget.parentElement!.nextElementSibling as HTMLElement)}>
                          <Icon name={copied === `${epKey(e)} 示例` ? "check" : "copy"} />
                        </button>
                      </div>
                      <pre><code>{JSON.stringify(e.example, null, 2)}</code></pre>
                    </div>
                  {/if}

                  <div class="apidoc-code">
                    <div class="apidoc-code-head">
                      <span>curl</span>
                      <button class="icon-btn" class:is-copied={copied === epKey(e)}
                        type="button" aria-label="复制 curl 命令"
                        onclick={(ev) => copy(curl, epKey(e),
                          ev.currentTarget.parentElement!.nextElementSibling as HTMLElement)}>
                        <Icon name={copied === epKey(e) ? "check" : "copy"} />
                      </button>
                    </div>
                    <pre><code>{curl}</code></pre>
                  </div>

                  {#if e.contentType}
                    <p class="apidoc-ct">响应类型：<code class="inline">{e.contentType}</code></p>
                  {/if}

                  {#if e.fields?.length}
                    <table class="apidoc-table">
                      <caption class="sr-only">响应字段</caption>
                      <thead><tr><th>字段</th><th>类型</th><th>说明</th></tr></thead>
                      <tbody>
                        {#each e.fields as f (f.name)}
                          <tr>
                            <td class="mono"><code>{f.name}</code></td>
                            <td class="mono muted">{f.type}</td>
                            <td>{f.desc}</td>
                          </tr>
                        {/each}
                      </tbody>
                    </table>
                  {/if}

                  {#if e.raw}
                    <div class="apidoc-code">
                      <div class="apidoc-code-head">
                        <span>响应示例</span>
                        <button class="icon-btn" class:is-copied={copied === `${epKey(e)} 响应`}
                          type="button" aria-label="复制响应示例"
                          onclick={(ev) => copy(e.raw ?? "", `${epKey(e)} 响应`,
                            ev.currentTarget.parentElement!.nextElementSibling as HTMLElement)}>
                          <Icon name={copied === `${epKey(e)} 响应` ? "check" : "copy"} />
                        </button>
                      </div>
                      <pre><code>{e.raw}</code></pre>
                    </div>
                  {/if}
                </article>
              {/each}
            </div>
          </div>
        {/if}
      </section>
    {/each}
  </div>
{/if}

<style>
  .filter { max-width: 320px; }
  /* 过滤条独立成行：以前它挤在页头的 actions 槽里，与"页头放动作按钮"
     的约定混在一起，而且没有地方放命中数与清空按钮 */
  .docbar {
    display: flex; align-items: center; gap: var(--u3);
    margin-bottom: var(--u4); font-size: 12.5px;
  }
  .docbar .muted { flex: none; }
  .docbar .btn-sm { margin-inline-start: auto; }
  /* 索引里的路径做成按钮（滚动定位），视觉上保持链接感 */
  .jump-link {
    background: none; border: 0; padding: 0; cursor: pointer;
    font-family: var(--font-mono); font-size: 12px; color: var(--text-2);
    text-align: start;
  }
  .jump-link:hover { color: var(--accent); text-decoration: underline; }
  .caret { display: inline-flex; transition: transform 160ms var(--ease); color: var(--text-3); }
  .caret.open { transform: rotate(90deg); }
  @media (prefers-reduced-motion: reduce) { .caret { transition: none; } }
</style>
