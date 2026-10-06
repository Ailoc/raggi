<script lang="ts">
  import { api } from "../lib/api";
  import { track } from "../lib/stores.svelte";
  import { fmtTime, fmtMs } from "../lib/format";
  import { loader } from "../lib/loadstate.svelte";
  import { go } from "../lib/router.svelte";
  import { setCrumbs } from "../lib/crumbs.svelte";
  import StateBlock from "../ui/StateBlock.svelte";
  import KeysPanel from "../ui/KeysPanel.svelte";
  import DataPanel from "../ui/DataPanel.svelte";
  import Icon from "../ui/Icon.svelte";
  import { toast } from "../ui/toast.svelte";
  import { confirmDialog } from "../ui/dialog.svelte";
  import type { ModelsConfig, Health } from "../lib/types";

  /** 命名参数由路由模式表给出（#/settings/{tab}），不再位置索引 parts */
  let { tab = "models" }: { tab?: string } = $props();

  let models = $state<ModelsConfig | null>(null);
  let health = $state<Health | null>(null);
  const ld = loader();
  let testing = $state<Record<string, boolean>>({});
  /**
   * 测试结果按 kind 存结构化对象，而不是拼成一坨 JSON 字符串。
   *
   * 旧实现是 `testResult = JSON.stringify(r, null, 2)` 塞进一个 <pre>：
   * 用户要自己从 `{"ok": true, "latency_ms": 812.3, "sorted_correctly":
   * false}` 里读出"所以重排到底对不对"。ok / 延迟 / 维度 / 排序是否正确
   * 这些字段本来就有明确结论，界面应该直接说结论。
   */
  let testResults = $state<Record<string, Record<string, unknown>>>({});
  /** 打开页面时生效的维度：保存前判断是否发生危险变更 */
  let origDim = $state(0);
  /** 已保存配置的快照，用来判断"有没有未保存的改动" */
  let savedSnapshot = $state("");
  let acting = $state("");
  const revealed = $state({ embed: false, llm: false, rerank: false });

  const dirty = $derived(
    !!models && JSON.stringify(models) !== savedSnapshot);

  $effect(() => {
    setCrumbs([{ label: "设置" }]);
    void load();
  });

  function load(): Promise<boolean> {
    return ld.run(async () => {
      if (tab === "health") return { kind: "health" as const, h: await api<Health>("/api/health") };
      if (tab === "keys") return { kind: "keys" as const };
      if (tab === "data") return { kind: "data" as const };
      return { kind: "models" as const, m: await api<ModelsConfig>("/api/models") };
    }, (r) => {
      if (r.kind === "health" && r.h) health = r.h;
      if (r.kind === "models" && r.m) {
        models = r.m;
        origDim = r.m.embed.dim;
        savedSnapshot = JSON.stringify(r.m);
      }
    });
  }

  async function save(): Promise<void> {
    if (!models) return;
    // 维度变更是不可逆的：表内向量宽度不会跟着变，改完所有写入都会失败，
    // 重建索引也修不回来。必须先量化后果让用户确认。
    if (models.embed.dim !== origDim) {
      const ok = await confirmDialog({
        title: "变更向量维度？",
        body: `向量维度 ${origDim} → ${models.embed.dim}。现有数据的向量宽度与`
          + "新配置不一致，之后所有入库与分块编辑都会失败；重建索引无法修复，"
          + "只能改回原维度或换新的数据目录重新入库。",
        confirmLabel: "仍然变更",
        destructive: true,
      });
      if (!ok) {
        models.embed.dim = origDim;
        return;
      }
    }
    try {
      const r = await track(() => api<{ warnings?: string[]; persisted?: boolean }>(
        "/api/models", { method: "PUT", body: models }));
      toast(r.persisted === false ? "已热切换（未落盘）" : "已保存并热切换");
      for (const w of r.warnings ?? []) toast(w, "err");
      await load();
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), "err");
    }
  }

  /**
   * 只测一类模型。禁用状态按 kind 记：旧实现用一个 `testing` 字符串，
   * 结果点一张卡片的测试会把三张卡片的按钮同时锁死，
   * 而另外两张明明可以并行测。
   */
  async function testKind(kind: "embedding" | "rerank" | "llm"): Promise<void> {
    testing = { ...testing, [kind]: true };
    try {
      const r = await api<Record<string, Record<string, unknown>>>(
        "/api/models/test", { method: "POST", body: { kind } });
      const value = r[kind];
      testResults = { ...testResults, [kind]: value ?? { ok: false, error: "后端未返回该类别的结果" } };
    } catch (e) {
      testResults = { ...testResults, [kind]: { ok: false, error: e instanceof Error ? e.message : String(e) } };
    } finally {
      testing = { ...testing, [kind]: false };
    }
  }

  /** 系统维护动作：重建索引 / 修复计数。 */
  async function sysAction(path: string, label: string): Promise<void> {
    acting = path;
    try {
      const r = await api<{ ok: boolean; fixed?: number }>(path, { method: "POST" });
      toast(path.endsWith("/reconcile") ? `已修复 ${r.fixed ?? 0} 处计数` : `${label}完成`);
      await load();
    } catch (e) {
      toast(`${label}失败：` + (e instanceof Error ? e.message : String(e)), "err");
    } finally {
      acting = "";
    }
  }

  const tabs = [
    { key: "models", label: "模型" },
    { key: "keys", label: "密钥" },
    { key: "health", label: "健康" },
    { key: "data", label: "数据与版本" },
  ];
</script>

<header class="page-head">
  <div class="page-head-main">
    <h1 class="page-title">设置</h1>
    <p class="page-sub">模型热切换、访问密钥与运行状况。</p>
  </div>
</header>

<nav class="tabs" aria-label="设置分区">
  {#each tabs as t (t.key)}
    <a class="tab" class:active={tab === t.key} href="#/settings/{t.key}"
      aria-current={tab === t.key ? "page" : undefined}>{t.label}</a>
  {/each}
</nav>

{#if ld.state.phase === "ready" && tab === "models" && !models}
  <div class="rows" aria-busy="true">
    {#each [1, 2, 3] as i (i)}<div class="row skeleton" aria-hidden="true"></div>{/each}
  </div>
{/if}

<!-- 连通性结果：把后端字段翻译成结论，而不是把 JSON 甩给用户 -->
{#snippet result(kind: "embedding" | "rerank" | "llm")}
  {@const r = testResults[kind]}
  {#if r}
    <dl class="tst" class:tst-bad={r.ok !== true}>
      <div class="tst-head">
        <dt>
          {#if r.ok === true}<Icon name="check" />{:else}<Icon name="alert" />{/if}
          {r.ok === true ? "连通" : "不通"}
        </dt>
        {#if r.latency_ms !== undefined}<dd>延迟 {fmtMs(Number(r.latency_ms))}</dd>{/if}
      </div>
      {#if r.ok !== true}
        <dd class="tst-err">{String(r.error ?? "未知错误")}</dd>
      {:else if r.skipped === true}
        <dd class="muted">{String(r.reason ?? "已跳过")}</dd>
      {:else}
        {#if r.model !== undefined}<dd>模型 {String(r.model)}</dd>{/if}
        {#if r.dim !== undefined}<dd>维度 {String(r.dim)}</dd>{/if}
        {#if r.throughput_texts_per_s !== undefined}
          <dd>吞吐 {String(r.throughput_texts_per_s)} 条/秒</dd>
        {/if}
        {#if r.sorted_correctly !== undefined}
          <dd>
            排序{r.sorted_correctly === true ? "正确" : "有误"}
            {#if r.scores !== undefined}
              <span class="muted">（分数 {String(r.scores)}）</span>
            {/if}
          </dd>
        {/if}
        {#if r.sample !== undefined}
          <dd class="tst-sample">{String(r.sample)}</dd>
        {/if}
      {/if}
    </dl>
  {/if}
{/snippet}

{#if ld.state.phase === "failed"}
  <StateBlock kind="error" title="加载失败" text={ld.state.error}
    action="重试" onaction={() => void load()} />
{:else if tab === "keys"}
  <KeysPanel />
{:else if tab === "data"}
  <DataPanel onGotoHealth={() => go("/settings/health")} />
{:else if tab === "models" && models}
  <form class="stack" onsubmit={(e) => { e.preventDefault(); void save(); }}>
    <div class="model-grid">
      <section class="panel">
        <div class="panel-head">
          <h2 class="panel-title">Embedding</h2>
          <p class="panel-desc">决定向量维度与语义召回质量。</p>
        </div>
        <div class="panel-body">
          <div class="field">
            <label class="field-label" for="embed-provider">提供方</label>
            <select id="embed-provider" bind:value={models.embed.provider}>
              <option value="custom">custom（OpenAI 兼容）</option>
              <option value="openai">openai</option>
              <option value="ollama">ollama</option>
              <option value="huggingface">huggingface</option>
            </select>
          </div>
          <div class="field">
            <label class="field-label" for="embed-model">模型</label>
            <input id="embed-model" bind:value={models.embed.model} autocomplete="off" />
          </div>
          <div class="field">
            <label class="field-label" for="embed-base">base_url</label>
            <input id="embed-base" bind:value={models.embed.base_url} autocomplete="off" />
          </div>
          <div class="field">
            <label class="field-label" for="embed-key">API Key</label>
            <span class="input-with-action">
              <input id="embed-key" type={revealed.embed ? "text" : "password"}
                autocomplete="off" bind:value={models.embed.api_key} />
              <button class="icon-btn" type="button" aria-label="显示或隐藏密钥"
                onclick={() => (revealed.embed = !revealed.embed)}>
                <Icon name={revealed.embed ? "eye-off" : "eye"} />
              </button>
            </span>
            <span class="field-note">显示 *** 表示已保存；保持原样即不修改。</span>
          </div>
          <div class="field">
            <label class="field-label" for="embed-dim">向量维度</label>
            <input id="embed-dim" type="number" bind:value={models.embed.dim} />
            <span class="field-note">危险：决定索引宽度。改动会让现有数据写入失败，且重建索引无法修复。</span>
          </div>
        </div>
        <div class="panel-foot">
          <button class="btn btn-sm" type="button" disabled={!!testing.embedding}
            onclick={() => void testKind("embedding")}>
            <Icon name="pulse" /> {testing.embedding ? "测试中…" : "测试连通性"}
          </button>
          {@render result("embedding")}
        </div>
      </section>

      <section class="panel">
        <div class="panel-head">
          <h2 class="panel-title">LLM</h2>
          <p class="panel-desc">用于问答生成，可热切换。</p>
        </div>
        <div class="panel-body">
          <div class="field">
            <label class="field-label" for="llm-provider">提供方</label>
            <select id="llm-provider" bind:value={models.llm.provider}>
              <option value="custom">custom（OpenAI 兼容）</option>
              <option value="openai">openai</option>
              <option value="ollama">ollama</option>
            </select>
          </div>
          <div class="field">
            <label class="field-label" for="llm-model">模型</label>
            <input id="llm-model" bind:value={models.llm.model} autocomplete="off" />
          </div>
          <div class="field">
            <label class="field-label" for="llm-base">base_url</label>
            <input id="llm-base" bind:value={models.llm.base_url} autocomplete="off" />
          </div>
          <div class="field">
            <label class="field-label" for="llm-key">API Key</label>
            <span class="input-with-action">
              <input id="llm-key" type={revealed.llm ? "text" : "password"}
                autocomplete="off" bind:value={models.llm.api_key} />
              <button class="icon-btn" type="button" aria-label="显示或隐藏密钥"
                onclick={() => (revealed.llm = !revealed.llm)}>
                <Icon name={revealed.llm ? "eye-off" : "eye"} />
              </button>
            </span>
            <span class="field-note">显示 *** 表示已保存；保持原样即不修改。</span>
          </div>
        </div>
        <div class="panel-foot">
          <button class="btn btn-sm" type="button" disabled={!!testing.llm}
            onclick={() => void testKind("llm")}>
            <Icon name="pulse" /> {testing.llm ? "测试中…" : "测试连通性"}
          </button>
          {@render result("llm")}
        </div>
      </section>

      <section class="panel">
        <div class="panel-head">
          <h2 class="panel-title">Rerank</h2>
          <p class="panel-desc">对候选分块重排序，未启用时按融合分数排序。</p>
        </div>
        <div class="panel-body">
          <div class="field">
            <label class="field-label" for="rerank-enabled">启用</label>
            <select id="rerank-enabled" bind:value={models.rerank.enabled}>
              <option value={true}>开</option><option value={false}>关</option>
            </select>
          </div>
          <div class="field">
            <label class="field-label" for="rerank-provider">提供方</label>
            <select id="rerank-provider" bind:value={models.rerank.provider}>
              <option value="none">none（不精排）</option>
              <option value="api">api（HTTP）</option>
              <option value="cross-encoder">cross-encoder（本地）</option>
            </select>
          </div>
          <div class="field">
            <label class="field-label" for="rerank-model">模型</label>
            <input id="rerank-model" bind:value={models.rerank.model} autocomplete="off" />
          </div>
          <div class="field">
            <label class="field-label" for="rerank-base">base_url</label>
            <input id="rerank-base" bind:value={models.rerank.base_url} autocomplete="off" />
          </div>
          <div class="field">
            <label class="field-label" for="rerank-key">API Key</label>
            <span class="input-with-action">
              <input id="rerank-key" type={revealed.rerank ? "text" : "password"}
                autocomplete="off" bind:value={models.rerank.api_key} />
              <button class="icon-btn" type="button" aria-label="显示或隐藏密钥"
                onclick={() => (revealed.rerank = !revealed.rerank)}>
                <Icon name={revealed.rerank ? "eye-off" : "eye"} />
              </button>
            </span>
          </div>
        </div>
        <div class="panel-foot">
          <button class="btn btn-sm" type="button" disabled={!!testing.rerank}
            onclick={() => void testKind("rerank")}>
            <Icon name="pulse" /> {testing.rerank ? "测试中…" : "测试连通性"}
          </button>
          {@render result("rerank")}
        </div>
      </section>
    </div>

    <section class="panel">
      <div class="panel-head">
        <h2 class="panel-title">保存</h2>
        <p class="panel-desc">
          保存后立即热切换，并写入 data/config.toml（重启后仍然生效）。
          <strong>向量维度变更会导致写入失效</strong>，需谨慎操作。
        </p>
      </div>
      <div class="panel-foot">
        <!-- 脏值必须可见：保存是全局的，而表单分散在三张卡片里，
             没有提示的话用户不知道哪些改动还没落地 -->
        {#if dirty}
          <span class="unsaved" role="status">
            <Icon name="alert" /> 有未保存的改动
          </span>
          <button class="btn btn-sm" type="button" onclick={() => void load()}>放弃改动</button>
        {/if}
        <button class="btn btn-primary" type="submit" disabled={!dirty}>保存并热切换</button>
      </div>
    </section>
  </form>
{:else if tab === "health" && health}
  <div class="stack">
    <section class="panel">
      <div class="panel-head">
        <h2 class="panel-title">
          运行状况
          <span class="badge" class:badge-ok={health.status === "ok"}
            class:badge-err={health.status !== "ok"}>
            <Icon name={health.status === "ok" ? "check" : "alert"} />
            {health.status === "ok" ? "正常" : "需关注"}
          </span>
        </h2>
        <p class="panel-desc">
          {health.status === "ok" ? "全部检查通过。" : "存在需要关注的问题。"}
          最近检测 {fmtTime(health.now)}
        </p>
      </div>
      <div class="panel-body">
        <dl class="kv-grid">
          <div class="kv"><dt>文档 / 分块</dt><dd>{health.doc_count} / {health.chunk_count}</dd></div>
          <div class="kv"><dt>独立分块</dt><dd>{health.standalone_chunks}</dd></div>
          <div class="kv"><dt>孤立分块</dt>
            <dd>
              {#if health.orphan_chunks}
                <span class="badge badge-err"><Icon name="alert" />{health.orphan_chunks}</span>
              {:else}<span class="badge badge-ok"><Icon name="check" />无</span>{/if}
            </dd>
          </div>
          <div class="kv"><dt>计数不一致</dt>
            <dd>
              {#if health.count_mismatch.length}
                <span class="badge badge-warn">{health.count_mismatch.length} 个文档</span>
              {:else}<span class="badge badge-ok"><Icon name="check" />无</span>{/if}
            </dd>
          </div>
          <div class="kv"><dt>FTS 待重建</dt>
            <dd>
              {#if health.checks_failed?.includes("fts_stale") || health.fts_stale_count < 0}
                <span class="badge badge-warn">未知（检查没跑成）</span>
              {:else if health.fts_stale_count}
                <span class="badge badge-warn">{health.fts_stale_count}</span>
              {:else}<span class="badge badge-ok"><Icon name="check" />无</span>{/if}
            </dd>
          </div>
          <div class="kv"><dt>嵌入维度</dt><dd>{health.embedding_dim}（表内 {health.stored_embedding_dim ?? "—"}）</dd></div>
          <div class="kv"><dt>嵌入模型</dt>
            <dd>
              {health.embedding_model}
              {#if health.checks_failed?.includes("embedding_model")}
                <span class="badge badge-warn">未查到（不能说一致）</span>
              {:else if health.embedding_model_mismatch}
                <span class="badge badge-err"><Icon name="alert" />与表内不一致</span>
              {:else}<span class="badge badge-ok"><Icon name="check" />一致</span>{/if}
            </dd>
          </div>
          <div class="kv"><dt>LanceDB 版本数</dt><dd>{health.lancedb_versions}</dd></div>
        </dl>

        <p class="group-sep">索引状态</p>
        <dl class="kv-grid">
          {#each Object.entries(health.index_state) as [name, s] (name)}
            <div class="kv"><dt>{name}</dt>
              <dd>
                {#if s.indexed}
                  <span class="badge badge-ok"><Icon name="check" />已建</span>
                {:else}
                  <span class="badge badge-warn"><Icon name="alert" />未建（待索引 {s.unindexed_rows}）</span>
                {/if}
              </dd>
            </div>
          {/each}
        </dl>
      </div>
      <div class="panel-foot">
        <button class="btn" type="button" disabled={acting !== ""}
          onclick={() => sysAction("/api/reindex", "重建索引")}>
          <Icon name="layers" /> {acting === "/api/reindex" ? "重建中…" : "重建索引"}
        </button>
        <button class="btn" type="button" disabled={acting !== ""}
          onclick={() => sysAction("/api/reconcile", "修复计数")}>
          <Icon name="refresh" /> {acting === "/api/reconcile" ? "修复中…" : "修复计数"}
        </button>
        <button class="btn btn-primary" type="button" onclick={load}>
          <Icon name="refresh" /> 重新检测
        </button>
      </div>
    </section>

    <details class="raw">
      <summary>原始响应（排障用）</summary>
      <pre class="out">{JSON.stringify(health, null, 2)}</pre>
    </details>
  </div>
{/if}

<style>
  .tabs { display: flex; gap: var(--u1); margin-bottom: var(--u4); border-bottom: 1px solid var(--line); }
  .tab {
    padding: var(--u2) var(--u3); color: var(--text-2); text-decoration: none;
    font-size: 13px; border-bottom: 2px solid transparent;
  }
  .tab:hover { color: var(--text); }
  .tab.active { color: var(--accent); border-bottom-color: var(--accent); }
  .out {
    padding: var(--u4); border: 1px solid var(--line); border-radius: var(--r-md);
    background: var(--bg-sunken); color: var(--text-2);
    font-family: var(--font-mono); font-size: 12px; line-height: 1.55;
    overflow: auto; max-height: 460px;
  }
  .raw { margin-top: var(--u2); }
  .raw summary { cursor: pointer; font-size: 12.5px; color: var(--text-3); }
  .raw summary:hover { color: var(--text-2); }
  .group-sep { margin-top: var(--u4); font-size: 12px; color: var(--text-3); }

  /* 连通性结果：结论先行，字段其次 */
  .tst {
    flex: 1; min-width: 0;
    display: flex; flex-direction: column; gap: 2px;
    margin-inline-start: var(--u3);
    font-size: 12px; color: var(--text-2);
  }
  .tst-head { display: flex; align-items: center; gap: var(--u3); }
  .tst-head dt {
    display: inline-flex; align-items: center; gap: var(--u1);
    font-weight: 600; color: var(--ok);
  }
  .tst-bad .tst-head dt { color: var(--danger); }
  .tst dd { margin: 0; font-variant-numeric: tabular-nums; }
  .tst-err { color: var(--danger); word-break: break-word; }
  .tst-sample {
    font-family: var(--font-mono); font-size: 11.5px; color: var(--text-3);
    white-space: pre-wrap; word-break: break-word; max-height: 5.5em; overflow: auto;
  }
  .unsaved {
    display: inline-flex; align-items: center; gap: var(--u1);
    margin-inline-end: auto; font-size: 12.5px; color: var(--accent);
  }
  .rows { display: flex; flex-direction: column; gap: var(--u2); }
  .row { height: 22px; }
  .skeleton { animation: pulse 1.4s ease-in-out infinite; }
  @keyframes pulse { 50% { opacity: 0.55; } }
  @media (prefers-reduced-motion: reduce) { .skeleton { animation: none; } }
</style>
