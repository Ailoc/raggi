<script lang="ts">
  import Icon from "./Icon.svelte";
  import SectionSwitcher from "./SectionSwitcher.svelte";
  import ObjectTree from "./ObjectTree.svelte";
  import StateBlock from "./StateBlock.svelte";
  import { resetLayout } from "../lib/dock.svelte";
  import { queue } from "../lib/queue.svelte";
  import { stageLabel } from "../lib/jobs";
  import { lastSearch, href, type Route } from "../lib/router.svelte";
  import type { Section } from "../lib/nav";
  import { GROUPS } from "../data/apidoc";

  /**
   * 常驻上下文栏。
   *
   * 三条硬规矩（都由 tests/test_frontend_wiring.py 的 dock 守卫锁住）：
   * 1. **折叠开关不在这里**——它在 App.svelte 的顶栏里，画在 <Dock> 之前。
   *    第一代侧栏就是因为把开关放在被折叠的容器里，折叠后没人能再打开它。
   * 2. 折叠态不落盘：刷新一律回到展开（窄屏按视口现算，不是"记忆"）。
   * 3. 这里的内容全部可被内容区的完整页面取代——dock 是捷径，不是唯一入口。
   *    所以每个 section 的 dock 内容都留了"去看完整页"的出口。
   */
  let { route, section, onnavigate }:
    { route: Route; section: Section; onnavigate?: () => void } = $props();

  const settingsLinks = [
    { key: "models", label: "模型设置", href: "#/settings/models", icon: "sliders" as const },
    { key: "keys", label: "访问密钥", href: "#/settings/keys", icon: "key" as const },
    { key: "health", label: "健康与索引", href: "#/settings/health", icon: "pulse" as const },
    // 与设置页的 tabs 一一对应。少这一条，「数据与版本」就只能进设置页
    // 再点标签才能到——dock 声称是"每个 section 的第二栏"，缺一个子页就是缺一个。
    { key: "data", label: "数据与版本", href: "#/settings/data", icon: "layers" as const },
  ];

  /**
   * 上次检索（sessionStorage，本会话内）。
   *
   * 这里必须"看一眼 route"：lastSearch() 读的是 sessionStorage，
   * Svelte 追踪不到它，不建立依赖的话这条捷径会一直停在首次渲染的值上，
   * 用户新检索完回到 dock 看到的还是旧的那条。
   */
  const recent = $derived.by(() => {
    void route.name;
    return lastSearch();
  });
</script>

<aside class="dock" id="dock" aria-label="上下文导航">
  <div class="dock-head">
    <SectionSwitcher current={section.key} {onnavigate} />
  </div>

  <div class="dock-scroll">
    {#if section.key === "kbs"}
      <p class="dock-title">知识库</p>
      <ObjectTree {route} />
    {:else if section.key === "search"}
      <p class="dock-title">检索</p>
      {#if recent}
        <a class="tree-row" href={recent} onclick={onnavigate}>
          <Icon name="refresh" />
          <span class="tree-name">回到上次检索</span>
        </a>
      {/if}
      <p class="tree-hint">
        检索历史只存在这台浏览器，不会上传——后端没有历史接口，
        所以这里显示的是本地记录，别把它当成服务端状态。
      </p>
    {:else if section.key === "jobs"}
      <p class="dock-title">队列</p>
      <!-- 摘要取不到时必须说出口：早前只把错误写进 queue.error 而
           从不渲染，队列面板会一直显示「没有正在跑的任务」，
           后端挂了看起来却像"确实没任务"，用户无从分辨。 -->
      {#if queue.error}
        <StateBlock
          kind="error"
          title="队列状态读取失败"
          text={queue.error}
        />
      {/if}
      {#if queue.jobs.length === 0}
        <p class="tree-hint">{queue.loaded ? "没有正在跑的任务。" : "正在读队列…"}</p>
      {:else}
        <div class="tree">
          {#each queue.jobs as j (j.job_id)}
            {#if j.doc_id && j.kb_id}
              <a class="tree-row" href={href("kb", j.kb_id, "doc", j.doc_id)}
                title={j.job_id}>
                <Icon name="doc" />
                <span class="tree-name">{stageLabel(j.stage)}</span>
                <span class="tree-count">{Math.round((j.progress || 0) * 100)}%</span>
              </a>
            {:else}
              <div class="tree-row" title={j.job_id}>
                <Icon name="doc" />
                <span class="tree-name">{stageLabel(j.stage)}</span>
              </div>
            {/if}
          {/each}
        </div>
      {/if}
      <a class="tree-row" href="#/jobs" onclick={onnavigate}>
        <Icon name="grid" />
        <span class="tree-name">查看全部任务</span>
      </a>
    {:else if section.key === "settings"}
      <p class="dock-title">设置</p>
      <div class="tree">
        {#each settingsLinks as s (s.href)}
          <a class="tree-row" href={s.href} class:is-current={route.parts[1] === s.key}
            onclick={onnavigate}>
            <Icon name={s.icon} />
            <span class="tree-name">{s.label}</span>
          </a>
        {/each}
      </div>
    {:else}
      <p class="dock-title">接口分组</p>
      <div class="tree">
        {#each GROUPS as g (g.id)}
          <!-- 只读概览：端点级索引要能跳过去，得等 ApiDoc 的过滤/定位进 URL（阶段 6） -->
          <div class="tree-row" title={g.desc}>
            <Icon name="layers" />
            <span class="tree-name">{g.title}</span>
            <span class="tree-count">{g.endpoints.length}</span>
          </div>
        {/each}
      </div>
    {/if}
  </div>

  <div class="dock-foot">
    <button class="tree-row" type="button" onclick={resetLayout}
      title="恢复默认布局：展开 dock 并回到默认宽度">
      <Icon name="refresh" />
      <span class="tree-name">恢复默认布局</span>
    </button>
  </div>
</aside>

<style>
  .dock-head { flex: none; padding: var(--u2); }
  .tree-name { color: inherit; }
</style>
