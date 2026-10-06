<script lang="ts">
  import { api } from "../lib/api";
  import { fmtTime } from "../lib/format";
  import { untrack } from "svelte";
  import { track, loadKbs } from "../lib/stores.svelte";
  import { loader } from "../lib/loadstate.svelte";
  import { go } from "../lib/router.svelte";
  import { setCrumbs } from "../lib/crumbs.svelte";
  import { toast } from "../ui/toast.svelte";
  import { removeKbDialog } from "../lib/kbops";
  import Icon from "../ui/Icon.svelte";
  import StateBlock from "../ui/StateBlock.svelte";
  import type { Kb, KbPlan } from "../lib/types";

  let { kbId }: { kbId: string } = $props();

  let kb = $state<Kb | null>(null);
  let plan = $state<KbPlan | null>(null);
  const ld = loader();

  // 基本信息
  let name = $state("");
  let desc = $state("");

  // 分块方案
  let inherit = $state(true);
  let size = $state(512);
  let ratio = $state(12.5);

  /**
   * 两个分区各自记快照、各自判脏。
   *
   * 原来两组字段共用一次 `load()` 刷新：保存基本信息成功后会整页重载，
   * 于是**分块方案面板里还没提交的编辑被静默抹掉**——用户先改方案、
   * 再顺手改个描述并保存，方案就回到服务器上的旧值了。
   */
  let savedBase = $state("");
  let savedPlan = $state("");
  /**
   * 快照只由这两个函数算，加载和保存都走同一条路。
   *
   * 之前 savedPlan 在 load() 里按 [inherit, size, ratio] 现拼、在
   * snapshotPlan() 里按 [!own.custom, own.chunk_size, own.overlap_ratio]
   * 现拼。两处算法不同就意味着"脏"的判定取决于上一次是哪条路写进去的：
   * 轻则保存按钮该亮不亮，重则改回原值也显示未保存，用户点了保存
   * 却什么也没发生。字段值是唯一真相，快照必须是它的纯函数。
   */
  const baseSnap = (n: string, d: string): string => JSON.stringify([n.trim(), d.trim()]);
  const planSnap = (i: boolean, s: number, r: number): string =>
    JSON.stringify([i, Number(s || 0), Number(r || 0)]);
  const baseDirty = $derived(!!kb && baseSnap(name, desc) !== savedBase);
  const planDirty = $derived(!!plan && planSnap(inherit, size, ratio) !== savedPlan);

  /**
   * 「这个库算不算自定义方案」= 它与系统默认是否不同。
   *
   * 不能用 `plan.own.custom`：两级模型下每个知识库都持有具体数值
   * （rag/storage/repos/kbs.py:90-97 明确写了"不留 chunk_size=0 表示继承"），
   * 所以那个字段说的是"这行有没有值"，永远为真。后端自己也放弃了它——
   * `plan_custom` 现在按"与默认值相比"来算，容差 0.05（rag/api/kbs.py:18-31），
   * 前端必须用同一个判据，否则两处对同一件事有两种答案。
   *
   * 用错信号的后果是浏览器里看到的：用户点「用系统默认值」并保存成功，
   * 徽章仍写着「本知识库自定义」，而同一行"当前生效"就是那组默认值。
   */
  function isCustomPlan(
    own: { chunk_size: number; overlap_ratio: number },
    g: { chunk_size: number; overlap_ratio: number }): boolean {
    return Number(own.chunk_size || 0) !== Number(g.chunk_size || 0)
      || Math.abs(Number(own.overlap_ratio || 0) - Number(g.overlap_ratio || 0)) > 0.05;
  }

  const customPlan = $derived(!!plan && isCustomPlan(plan.own, plan.global));

  $effect(() => {
    const id = kbId;
    if (!id) return;
    untrack(() => { ld.reset(); void load(); });
  });

  async function load(): Promise<boolean> {
    return ld.run(async () => {
      const [k, p] = await Promise.all([
        api<Kb>(`/api/kbs/${encodeURIComponent(kbId)}`),
        api<KbPlan>(`/api/kbs/${encodeURIComponent(kbId)}/plan`),
      ]);
      return { k, p };
    }, ({ k, p }) => {
      kb = k;
      plan = p;
      name = k.name;
      desc = k.description ?? "";
      const g = p.global;
      inherit = !isCustomPlan(p.own, p.global);
      // own 在两级模型下总是具体值；为 0 只可能是尚未物化的旧数据，退回默认
      size = p.own.chunk_size || g.chunk_size;
      ratio = p.own.overlap_ratio || g.overlap_ratio;
      savedBase = baseSnap(k.name, k.description ?? "");
      savedPlan = planSnap(inherit, size, ratio);
      setCrumbs([
        { label: "知识库", href: "#/" },
        { label: k.name, href: `#/kb/${encodeURIComponent(kbId)}/docs` },
        { label: "设置" },
      ]);
    });
  }

  async function saveBase(): Promise<void> {
    if (!kb) return;
    const n = name.trim();
    if (!n) { toast("名称不能为空", "err"); return; }
    try {
      await track(() => api(`/api/kbs/${encodeURIComponent(kbId)}`, {
        method: "PUT", body: { name: n, description: desc.trim() },
      }));
      toast("基本信息已保存");
      // 就地更新，不整页重载：重载会连带把方案面板里未提交的编辑冲掉
      kb = { ...kb, name: n, description: desc.trim() };
      savedBase = baseSnap(n, desc);
      void loadKbs();   // 顶栏/dock 显示的库名要跟着变
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), "err");
    }
  }

  async function savePlan(): Promise<void> {
    if (!plan) return;
    const s = Number(size || 0);
    const r = Number(ratio || 0);
    // 后端把 chunk_size=0 解释为"恢复继承"（rag/api/schemas.py:122）。
    // 于是"取消勾选继承 + 大小留空"这一组合绝不能发出去：用户想设自定义，
    // 服务端却把知识库改回继承，界面还回一句"分块方案已保存"——
    // 一次静默的错写，而且要到下一个文档入库才会被发现。
    // 上下界取自 rag/storage/plan.py:35-36（64–8192 / 0–50），不另立一套。
    if (!inherit && !(s >= 64 && s <= 8192)) {
      toast("分块大小需在 64–8192 之间。留空等于「恢复继承」，不是自定义", "err");
      return;
    }
    if (!inherit && !(r >= 0 && r <= 50)) {
      toast("重叠比例需在 0–50% 之间（超过 50% 收益递减且块数翻倍）", "err");
      return;
    }
    try {
      await track(() => api(`/api/kbs/${encodeURIComponent(kbId)}/plan`, {
        method: "PUT",
        body: { chunk_size: inherit ? 0 : s, overlap_ratio: inherit ? 0 : r },
      }));
      toast(inherit ? "已恢复系统默认方案" : "分块方案已保存");
      // 输入框要跟着落回"生效值"：恢复继承后如果留着空，界面上就同时存在
      // 「当前生效 512 字符」和两个空字段——用户下次取消勾选时以为默认是空。
      if (inherit) {
        size = plan.global.chunk_size;
        ratio = plan.global.overlap_ratio;
      }
      // 只重取本面板的方案接口，不整页重载（重载会抹掉基本信息里的未提交编辑）。
      // 为什么不是在前端自己拼 effective：`当前生效` 还带着 overlap_chars 与
      // source 两个后端算出来的字段，在这里重算等于把切分口径实现第二遍，
      // 迟早和后端不一致。实测踩过：就地只补 own 时，徽章已经是"本知识库
      // 自定义"、输入框已经是 600，而"当前生效"还写着 512。
      try {
        plan = await api<KbPlan>(`/api/kbs/${encodeURIComponent(kbId)}/plan`);
      } catch {
        plan = {
          ...plan,
          own: {
            ...plan.own,
            custom: !inherit,
            chunk_size: inherit ? plan.global.chunk_size : s,
            overlap_ratio: inherit ? plan.global.overlap_ratio : r,
          },
        };
      }
      savedPlan = planSnap(inherit, size, ratio);
      // plan_custom 的口径与后端一致：与默认值相比，而不是"有没有设置过"
      if (kb && plan) kb = { ...kb, plan_custom: isCustomPlan(plan.own, plan.global) };
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), "err");
    }
  }

  async function removeKb(): Promise<void> {
    if (!kb) return;
    // 走 kbops 的统一入口：删除库此前有 3 个入口、2 套确认路径，
    // 现在既只剩这一处，确认文案也只该有一份实现
    if (await removeKbDialog(kb)) go("/");
  }
</script>

{#if ld.state.phase === "failed"}
  <StateBlock kind="error" title="加载知识库设置失败" text={ld.state.error}
    action="重试" onaction={() => void load()} />
{:else if ld.state.phase === "initial"}
  <!-- 这一支以前不存在：整页被 `{#if error}{:else if kb && plan}` 包住，
       拉取期间两个条件都不成立 → **屏幕全白**，用户不知道是在加载还是坏了 -->
  <div class="rows" aria-busy="true">
    {#each [1, 2, 3] as i (i)}<div class="row skeleton" aria-hidden="true"></div>{/each}
  </div>
{:else if kb && plan}
  <!-- 返回入口由工作区标签条承担，这里不再重复摆 backlink -->

  <header class="page-head">
    <div class="page-head-main">
      <h1 class="page-title">{kb.name}</h1>
      <p class="page-sub">
        {kb.doc_count} 个文档 · {kb.chunk_count} 个分块 · 更新于 {fmtTime(kb.updated_at)}
      </p>
    </div>
  </header>

  <div class="stack">
    <section class="panel">
      <div class="panel-head">
        <h2 class="panel-title">基本信息</h2>
        <p class="panel-desc">名称与描述。描述只给人看，不参与检索。</p>
      </div>
      <div class="panel-body">
        <div class="form-grid">
          <div class="field">
            <label class="field-label" for="kbSetName">名称</label>
            <input id="kbSetName" type="text" bind:value={name} autocomplete="off" />
          </div>
          <div class="field">
            <label class="field-label" for="kbSetDesc">描述</label>
            <textarea id="kbSetDesc" rows="3" placeholder="这个库收录什么内容"
              bind:value={desc}></textarea>
            <span class="field-note">留空时列表卡片会提示可补充。</span>
          </div>
        </div>
      </div>
      <div class="panel-foot">
        {#if baseDirty}
          <span class="unsaved" role="status"><Icon name="alert" /> 有未保存的改动</span>
        {/if}
        <button class="btn btn-primary" type="button"
          disabled={!baseDirty} onclick={saveBase}>保存基本信息</button>
      </div>
    </section>

    <section class="panel">
      <div class="panel-head">
        <h2 class="panel-title">分块方案</h2>
        <p class="panel-desc">决定文档入库时怎么切。单个文档还可再覆盖一次。</p>
      </div>
      <div class="panel-body">
        <p class="effective">
          当前生效 <strong>{plan.effective.chunk_size} 字符 · 重叠 {plan.effective.overlap_ratio}%</strong>
          <span class="planbadge" class:custom={customPlan}
            class:badge-inherit={!customPlan}>
            {customPlan ? "本知识库自定义" : "与系统默认一致"}
          </span>
        </p>
        <label class="switch" for="kbSetInherit">
          <input type="checkbox" id="kbSetInherit" bind:checked={inherit} />
          <span class="switch-track"></span>
          <span class="switch-label">
            用系统默认值（{plan.global.chunk_size} 字符 / {plan.global.overlap_ratio}%）
          </span>
        </label>
        <!-- 这不是真正的"继承"：两级模型下知识库总是持有具体数值
             （rag/storage/plan.py:140-142），勾选并保存只是**把当前的系统默认
             抄给这个库**。之后改系统默认不会传到这里。
             不写这一句，"用系统默认值"就是在承诺一个后端做不到的联动。 -->
        <p class="field-note">
          保存时把系统默认抄到本库；日后修改系统默认不会自动影响已建好的库。
        </p>
        <div class="form-grid">
          <div class="field">
            <label class="field-label" for="kbSetSize">分块大小（字符）</label>
            <input id="kbSetSize" type="number" min="64" max="8192"
              bind:value={size} disabled={inherit} />
            <span class="field-note">中文建议 300–600。过小丢失上下文，过大稀释向量。</span>
          </div>
          <div class="field">
            <label class="field-label" for="kbSetOverlap">重叠比例（%）</label>
            <input id="kbSetOverlap" type="number" min="0" max="50"
              bind:value={ratio} disabled={inherit} />
            <span class="field-note">业界常用 10–20%。按比例而非绝对值。</span>
          </div>
        </div>
        <p class="field-note">保存只影响之后入库的文档；已有文档要在文档行上点「重新切分」才会按新方案重切。</p>
      </div>
      <div class="panel-foot">
        {#if planDirty}
          <span class="unsaved" role="status"><Icon name="alert" /> 方案有未保存的改动</span>
        {/if}
        <button class="btn btn-primary" type="button"
          disabled={!planDirty} onclick={savePlan}>保存方案</button>
      </div>
    </section>

    <section class="panel">
      <div class="panel-head">
        <h2 class="panel-title">内容概览</h2>
        <p class="panel-desc">只读统计，用于核对入库结果。</p>
      </div>
      <div class="panel-body">
        <dl class="kv-grid">
          <div class="kv"><dt>文档</dt><dd>{kb.doc_count}</dd></div>
          <div class="kv"><dt>分块</dt><dd>{kb.chunk_count}</dd></div>
          <div class="kv"><dt>创建时间</dt><dd>{fmtTime(kb.created_at)}</dd></div>
          <div class="kv"><dt>更新时间</dt><dd>{fmtTime(kb.updated_at)}</dd></div>
          <div class="kv"><dt>知识库 ID</dt><dd class="mono">{kb.kb_id}</dd></div>
        </dl>
        <div class="form-actions">
          <a class="btn" href={`#/kb/${encodeURIComponent(kbId)}/docs`}>
            <Icon name="files" /> 查看文档
          </a>
          <a class="btn" href={`#/kb/${encodeURIComponent(kbId)}/chunks?scope=standalone`}>
            <Icon name="grid" /> 独立分块
          </a>
        </div>
      </div>
    </section>

    <section class="panel panel-danger">
      <div class="panel-head">
        <h2 class="panel-title">危险操作</h2>
        <p class="panel-desc">删除知识库会连同它的全部文档、分块与留档原文一起删除，不可撤销。</p>
      </div>
      <div class="panel-body">
        <button class="btn btn-danger" type="button" onclick={removeKb}>
          <Icon name="trash" /> 删除本知识库
        </button>
      </div>
    </section>
  </div>
{/if}

<style>
  .effective { display: flex; align-items: center; gap: var(--u2); font-size: 13px; color: var(--text-2); }
  .effective strong { color: var(--text); font-weight: 600; }
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
