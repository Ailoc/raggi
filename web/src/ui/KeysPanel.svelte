<script lang="ts">
  import { api, ApiError, getApiKey, getToken, setApiKey, setToken } from "../lib/api";
  import { fmtTime } from "../lib/format";
  import { toast } from "../ui/toast.svelte";
  import { confirmDialog } from "../ui/dialog.svelte";
  import { formDialog } from "../ui/form.svelte";
  import StateBlock from "../ui/StateBlock.svelte";
  import Icon from "../ui/Icon.svelte";
  import type { ApiKey, ApiKeyCreated, ApiKeyList } from "../lib/types";

  let list = $state<ApiKeyList | null>(null);
  let error = $state("");
  let localKey = $state(getApiKey());
  let localToken = $state(getToken());

  $effect(() => { void load(); });

  async function load(): Promise<void> {
    error = "";
    try {
      list = await api<ApiKeyList>("/api/keys");
    } catch (e) {
      error = e instanceof ApiError && (e.status === 401 || e.status === 403)
        ? "需要 write 权限才能管理密钥"
        : e instanceof Error ? e.message : String(e);
    }
  }

  const activeCount = $derived((list?.items ?? []).filter((k) => k.state === "active").length);

  const stateLabel: Record<string, string> = {
    active: "有效", revoked: "已吊销", expired: "已过期",
  };
  const stateClass: Record<string, string> = {
    active: "badge-ok", revoked: "badge-err", expired: "badge-warn",
  };

  async function createKey(): Promise<void> {
    let created: ApiKeyCreated | null = null;
    const ok = await formDialog({
      title: "签发 API 密钥",
      submitLabel: "签发",
      intro: "密钥明文只在签发后显示一次，服务端仅保存哈希，丢失后无法找回。",
      fields: [
        { name: "name", label: "名称", required: true, placeholder: "例如：CI 流水线 / 内部集成", note: "用于区分调用方，日后按名字吊销。" },
        {
          name: "scope", label: "权限", type: "select", value: "read",
          options: [
            { value: "read", label: "read（只读：检索与查看）" },
            { value: "write", label: "write（读写：可入库、编辑、删除）" },
          ],
          note: "只读够用就别给 write：只读密钥无法入库、编辑、删除，也不能再签发密钥。",
        },
        { name: "expires_in_days", label: "有效期（天）", type: "number", min: 1, placeholder: "留空 = 永不过期", note: "建议给外部集成设定期限，减少永久凭据的暴露面。" },
        { name: "note", label: "备注", placeholder: "用途说明（可选）" },
      ],
      onSubmit: async (v) => {
        const daysRaw = String(v.expires_in_days ?? "").trim();
        const days = daysRaw ? Number(daysRaw) : null;
        if (daysRaw && (!Number.isFinite(days) || (days as number) <= 0)) {
          throw new Error("有效期必须是正整数天");
        }
        created = await api<ApiKeyCreated>("/api/keys", {
          method: "POST",
          body: {
            name: String(v.name).trim(),
            scope: String(v.scope) === "write" ? "write" : "read",
            expires_in_days: days,
            note: String(v.note ?? "").trim(),
          },
        });
        // 顺序要紧：签发后鉴权立即生效。若本机此前没有任何凭据，
        // 随后的列表刷新就会 401，用户反而看不到刚签发的密钥。
        // 首次签发且本机无凭据时自动收下；已有凭据则不覆盖。
        if (!getApiKey() && !getToken() && created?.key) setApiKey(created.key);
      },
    });
    if (!ok || !created) return;
    const c: ApiKeyCreated = created;
    localKey = getApiKey();
    await load();
    await formDialog({
      title: "密钥已签发",
      submitLabel: "我已保存",
      intro: c.warning || "密钥明文只显示这一次，请立即保存。",
      fields: [{
        name: "key", label: "完整密钥", type: "textarea", rows: 2,
        value: c.key, required: true,
        note: "关闭后无法再次查看。若丢失，请在列表中吊销并重新签发。",
      }],
      onSubmit: async () => { /* 仅展示 */ },
    });
  }

  async function revoke(k: ApiKey): Promise<void> {
    const isLocal = !!getApiKey() && k.prefix && getApiKey().startsWith(k.prefix);
    const ok = await confirmDialog({
      title: `吊销「${k.name}」`,
      body: "使用该密钥的客户端会立即失效并收到 401。"
        + (isLocal ? "当前浏览器正在使用这把密钥，吊销后需要重新配置凭据。" : ""),
      confirmLabel: "吊销",
      destructive: true,
    });
    if (!ok) return;
    try {
      await api(`/api/keys/${encodeURIComponent(k.key_id)}`, { method: "DELETE" });
      toast("已吊销");
      await load();
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), "err");
    }
  }

  function saveLocal(): void {
    setApiKey(localKey.trim());
    setToken(localToken.trim());
    toast("本机凭据已保存");
    void load();
  }

  function clearLocal(): void {
    setApiKey("");
    setToken("");
    localKey = ""; localToken = "";
    toast("本机凭据已清除");
    void load();
  }

  // 时间格式化统一走 lib/format.ts（原先这里、以及另外 5 个视图各写一遍）
</script>

<div class="stack">
  <section class="panel">
    <div class="panel-head">
      <h2 class="panel-title"><Icon name="key" /> API 密钥</h2>
      <p class="panel-desc">
        对外调用本服务时使用的凭据，形如 <code class="inline">rg_xxxxxxxx…</code>。
        请求时放在 <code class="inline">Authorization: Bearer &lt;key&gt;</code>
        或 <code class="inline">X-API-Key</code> 头里。
      </p>
    </div>
    <div class="panel-body">
      {#if error}
        <StateBlock kind="error" title={error} text="若本机没有 write 凭据，可在下方「本机凭据」里填入后重试。"
          action="重试" onaction={load} />
      {:else if list}
        <dl class="kv-grid">
          <div class="kv"><dt>密钥总数</dt><dd>{list.total}</dd></div>
          <div class="kv"><dt>有效</dt><dd>{activeCount}</dd></div>
          <div class="kv"><dt>静态 token</dt><dd>{list.legacy_token_enabled ? "已启用" : "未启用"}</dd></div>
        </dl>
      {/if}
    </div>
    <div class="panel-foot">
      <button class="btn btn-primary" type="button" onclick={createKey}>
        <Icon name="plus" /> 签发新密钥
      </button>
    </div>
  </section>

  {#if list && list.items.length > 0}
    <section class="panel">
      <div class="panel-body">
        <div class="table-scroll">
          <table class="docs">
            <thead>
              <tr>
                <th>名称</th><th>密钥</th><th>权限</th><th>状态</th>
                <th>最近使用</th><th>过期</th>
                <th class="actions"><span class="sr-only">操作</span></th>
              </tr>
            </thead>
            <tbody>
              {#each list.items as k (k.key_id)}
                <tr>
                  <td><span class="keyname">{k.name}</span></td>
                  <td><span class="mono">{k.prefix ? `${k.prefix}…` : "—"}</span></td>
                  <td><span class="badge" class:badge-warn={k.scope === "write"}>{k.scope}</span></td>
                  <td><span class="badge {stateClass[k.state] ?? ""}">{stateLabel[k.state] ?? k.state}</span></td>
                  <td class="time">{fmtTime(k.last_used_at)}</td>
                  <td class="time">{k.expires_at ? fmtTime(k.expires_at) : "永不过期"}</td>
                  <td class="actions">
                    {#if k.state === "active"}
                      <button class="btn btn-sm" type="button" onclick={() => revoke(k)}>吊销</button>
                    {/if}
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  {:else if list}
    <StateBlock title="还没有 API 密钥"
      text="当前未启用鉴权，任何人都能直接调用本服务的接口。若要把 Raggi 暴露给其他人或公网，请先签发一把密钥。"
      action="签发第一把密钥" onaction={createKey} />
  {/if}

  <section class="panel">
    <div class="panel-head">
      <h2 class="panel-title">本机凭据</h2>
      <p class="panel-desc">
        浏览器调用本服务时携带的凭据，保存在本机 localStorage。
        API 密钥优先于静态 token；两者都为空时按「单机自用」直连。
      </p>
    </div>
    <div class="panel-body">
      <div class="form-grid">
        <div class="field">
          <label class="field-label" for="localKey">API 密钥</label>
          <input id="localKey" type="password" autocomplete="off" bind:value={localKey}
            placeholder="rg_…（留空则不使用）" />
        </div>
        <div class="field">
          <label class="field-label" for="localToken">静态 token</label>
          <input id="localToken" type="password" autocomplete="off" bind:value={localToken}
            placeholder="部署时 RAG_TOKEN 的值（可选）" />
        </div>
      </div>
    </div>
    <div class="panel-foot">
      <button class="btn" type="button" onclick={clearLocal}>清除</button>
      <button class="btn btn-primary" type="button" onclick={saveLocal}>保存并重试</button>
    </div>
  </section>
</div>
