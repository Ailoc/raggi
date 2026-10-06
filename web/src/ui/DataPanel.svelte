<script lang="ts">
  import { api } from "../lib/api";
  import { fmtBytes, fmtTime } from "../lib/format";
  import { loader } from "../lib/loadstate.svelte";
  import { confirmDialog } from "./dialog.svelte";
  import { toast } from "./toast.svelte";
  import { track } from "../lib/stores.svelte";
  import StateBlock from "./StateBlock.svelte";
  import Icon from "./Icon.svelte";

  /**
   * 数据与版本：容量统计 + LanceDB 表版本 + 回滚。
   *
   * 这三个端点（/stats、/versions、/rollback）此前**完全没有界面**，
   * 而回滚恰恰是单机库最重要的一层自救手段：误删、错误的批量停用、
   * 切分方案改坏之后，除了重装数据目录别无他法。
   *
   * 界面上刻意保守：回滚按表生效、不跨表，所以必须把后端那句
   * "仅回滚指定表；请用 /api/health 对账"如实显示出来，而不是只报"成功"。
   */
  let { onGotoHealth }: { onGotoHealth?: () => void } = $props();

  const TABLES = ["chunks", "documents", "kbs", "apikeys", "jobs"] as const;

  interface Stats {
    doc_count: number; chunk_count: number; job_count: number;
    backend: string; versions: number;
    data_dir_bytes?: number; lancedb_dir_bytes?: number; disk_free_bytes?: number;
    indexes?: Record<string, { columns: string[]; type: string }[]>;
  }
  interface VersionRow {
    version: number; timestamp?: string;
    metadata?: Record<string, string | number>;
  }

  const statsLd = loader();
  const verLd = loader();
  let stats = $state<Stats | null>(null);
  let versions = $state<VersionRow[]>([]);
  let table = $state<string>("chunks");
  let rolling = $state(false);

  $effect(() => { void loadStats(); });
  $effect(() => { void loadVersions(); });

  function loadStats(): Promise<boolean> {
    return statsLd.run(() => api<Stats>("/api/stats"), (s) => { stats = s; });
  }

  function loadVersions(): Promise<boolean> {
    return verLd.run(
      () => api<{ table: string; versions: VersionRow[] }>(
        `/api/versions?table=${encodeURIComponent(table)}`),
      (r) => { versions = r.versions; },
    );
  }

  /** metadata 里的数字是字符串（后端原样透传 LanceDB），必须显式转 */
  function meta(v: VersionRow, key: string): string {
    const raw = v.metadata?.[key];
    if (raw === undefined || raw === null || raw === "") return "—";
    const n = Number(raw);
    return Number.isFinite(n) ? String(n) : String(raw);
  }

  /**
   * 取 metadata 里的数值，缺失/非数返回 null。
   *
   * 不要写成 `Number(meta(v, k)) || null`：空表的大小就是 0，而 0 是假值，
   * 于是"0 B"被当成"没有这个字段"显示成 `—`。缺字段和值为 0 是两件
   * 不同的事——后者恰恰告诉用户这张表当前不占空间。
   */
  function numMeta(v: VersionRow, key: string): number | null {
    const n = Number(v.metadata?.[key]);
    return Number.isFinite(n) ? n : null;
  }

  async function rollback(v: VersionRow): Promise<void> {
    const rows = meta(v, "total_rows");
    const ok = await confirmDialog({
      title: `把 ${table} 回滚到版本 ${v.version}？`,
      // 正文按纯文本渲染（ConfirmDialog 用 {cur.body}），不要写 Markdown：
      // `**不可撤销**` 在这里会带着星号原样显示出来。
      body: `该表将回到版本 ${v.version}（${fmtTime(v.timestamp)}，约 ${rows} 行）。`
        + `当前版本 ${latestVersion} 之后的改动会从这张表消失，且不可撤销。`
        + "版本按表管理：其他表不会一起回滚，回滚后请到「健康」页对账。",
      confirmLabel: "回滚",
      destructive: true,
      requireTyping: table,
    });
    if (!ok) return;
    rolling = true;
    try {
      const r = await track(() => api<{ ok: boolean; note: string; rolled_back_to: number }>(
        "/api/rollback", { method: "POST", body: { version: v.version, table } }));
      toast(`已回滚 ${table} 到版本 ${r.rolled_back_to}`);
      // 后端 note 原文照抄，不改写也不省略——它说的是"只回滚了这一张表"。
      // 这里用 err 不是因为它失败了，而是 toast 只有两档，而这条需要警示图标
      // 和 6 秒停留时间：用户如果不对账，接下来看到的计数就是错的。
      if (r.note) toast(r.note, "err");
      await Promise.all([loadStats(), loadVersions()]);
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), "err");
    } finally {
      rolling = false;
    }
  }

  const latestVersion = $derived(
    versions.reduce((m, v) => Math.max(m, v.version), 0),
  );
</script>

<div class="stack">
  <section class="panel">
    <div class="panel-head">
      <h2 class="panel-title"><Icon name="layers" /> 容量</h2>
      <p class="panel-desc">后端 {stats?.backend ?? "—"}；数据目录占用与索引规模。</p>
    </div>
    {#if statsLd.state.phase === "failed"}
      <div class="panel-body">
        <StateBlock kind="error" title="读取统计失败" text={statsLd.state.error}
          action="重试" onaction={() => void loadStats()} />
      </div>
    {:else if statsLd.state.phase === "initial"}
      <div class="panel-body">
        <div class="rows" aria-busy="true">
          {#each [1, 2] as i (i)}<div class="row skeleton" aria-hidden="true"></div>{/each}
        </div>
      </div>
    {:else if stats}
      <div class="panel-body">
        <dl class="kv-grid">
          <div class="kv"><dt>文档</dt><dd>{stats.doc_count}</dd></div>
          <div class="kv"><dt>分块</dt><dd>{stats.chunk_count}</dd></div>
          <div class="kv"><dt>任务记录</dt><dd>{stats.job_count}</dd></div>
          <div class="kv"><dt>数据目录</dt><dd>{fmtBytes(stats.data_dir_bytes)}</dd></div>
          <div class="kv"><dt>索引目录</dt><dd>{fmtBytes(stats.lancedb_dir_bytes)}</dd></div>
          <div class="kv"><dt>磁盘剩余</dt><dd>{fmtBytes(stats.disk_free_bytes)}</dd></div>
          <div class="kv"><dt>表版本总数</dt><dd>{stats.versions}</dd></div>
        </dl>

        {#if stats.indexes}
          <p class="group-sep">已建索引</p>
          <ul class="idx">
            {#each Object.entries(stats.indexes) as [t, list] (t)}
              <li>
                <strong>{t}</strong>
                {#if list.length === 0}<span class="muted">无索引</span>{:else}
                  {#each list as ix (ix.columns.join(",") + ix.type)}
                    <span class="badge">{ix.type} · {ix.columns.join(", ")}</span>
                  {/each}
                {/if}
              </li>
            {/each}
          </ul>
        {/if}
      </div>
      <div class="panel-foot">
        <button class="btn btn-sm" type="button" onclick={() => void loadStats()}>
          <Icon name="refresh" /> 刷新
        </button>
      </div>
    {/if}
  </section>

  <section class="panel panel-danger">
    <div class="panel-head">
      <h2 class="panel-title"><Icon name="alert" /> 版本与回滚</h2>
      <p class="panel-desc">
        LanceDB 每次写入都留下版本，可以退回任意一个。
        <strong>回滚按表生效，不会带上其他表</strong>——回滚 chunks 之后
        documents 的计数需要重新对账。
      </p>
    </div>
    <div class="panel-body">
      <label class="field">
        <span class="field-label">选择表</span>
        <select bind:value={table} aria-label="要查看版本的表">
          {#each TABLES as t (t)}<option value={t}>{t}</option>{/each}
        </select>
      </label>

      {#if verLd.state.phase === "failed"}
        <StateBlock kind="error" title="读取版本失败" text={verLd.state.error}
          action="重试" onaction={() => void loadVersions()} />
      {:else if verLd.state.phase === "initial"}
        <div class="rows" aria-busy="true">
          {#each [1, 2, 3] as i (i)}<div class="row skeleton" aria-hidden="true"></div>{/each}
        </div>
      {:else if versions.length === 0}
        <StateBlock title="这张表还没有可回滚的版本"
          text="新库或刚重建过的库可能只有一个版本；有写入之后这里会列出来。" />
      {:else}
        <div class="table-wrap">
          <div class="table-scroll">
            <table class="docs">
              <thead>
                <tr>
                  <th class="num">版本</th><th>时间</th>
                  <th class="num">行数</th><th class="num">文件</th>
                  <th class="num">大小</th>
                  <th class="actions"><span class="sr-only">操作</span></th>
                </tr>
              </thead>
              <tbody>
                {#each versions as v (v.version)}
                  <tr>
                    <td class="num mono">
                      {v.version}
                      {#if v.version === latestVersion}<span class="badge badge-ok">当前</span>{/if}
                    </td>
                    <td class="time">{fmtTime(v.timestamp)}</td>
                    <td class="num">{meta(v, "total_rows")}</td>
                    <td class="num">{meta(v, "total_data_files")}</td>
                    <td class="num">{fmtBytes(numMeta(v, "total_files_size"))}</td>
                    <td class="actions">
                      {#if v.version === latestVersion}
                        <span class="muted">—</span>
                      {:else}
                        <button class="btn btn-sm btn-danger" type="button"
                          disabled={rolling} onclick={() => void rollback(v)}>
                          回滚到此版本
                        </button>
                      {/if}
                    </td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
        </div>
      {/if}
    </div>
    <div class="panel-foot">
      {#if onGotoHealth}
        <button class="btn btn-sm" type="button" onclick={onGotoHealth}>
          <Icon name="pulse" /> 回滚后去对账
        </button>
      {/if}
      <button class="btn btn-sm" type="button" onclick={() => void loadVersions()}>
        <Icon name="refresh" /> 刷新版本
      </button>
    </div>
  </section>
</div>

<style>
  .rows { display: flex; flex-direction: column; gap: var(--u2); }
  .row { height: 22px; }
  .skeleton { animation: pulse 1.4s ease-in-out infinite; }
  @keyframes pulse { 50% { opacity: 0.55; } }
  .group-sep { margin-top: var(--u4); margin-bottom: var(--u2); font-size: 12px; color: var(--text-3); }
  .idx { display: flex; flex-direction: column; gap: var(--u1); font-size: 12.5px; }
  .idx li { display: flex; align-items: center; gap: var(--u2); flex-wrap: wrap; }
  .field { max-width: 260px; margin-bottom: var(--u4); }
  @media (prefers-reduced-motion: reduce) { .skeleton { animation: none; } }
</style>
