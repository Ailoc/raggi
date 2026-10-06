<script lang="ts">
  import { onMount } from "svelte";
  import { fade } from "svelte/transition";
  import { dur } from "./lib/motion";
  import Toaster from "./ui/Toaster.svelte";
  import ConfirmDialog from "./ui/ConfirmDialog.svelte";
  import FormDialog from "./ui/FormDialog.svelte";
  import Dock from "./ui/Dock.svelte";
  import WorkspaceTabs from "./ui/WorkspaceTabs.svelte";
  import Icon from "./ui/Icon.svelte";
  import { app, loadKbs } from "./lib/stores.svelte";
  import { route, startRouter } from "./lib/router.svelte";
  import { crumbs } from "./lib/crumbs.svelte";
  import { SECTIONS, sectionOf } from "./lib/nav";
  import {
    DOCK_W_MAX, DOCK_W_MIN, NARROW_QUERY,
    dock, initDock, toggleDock, setDockWidth, nudgeDockWidth, dockStyle,
  } from "./lib/dock.svelte";
  import { queue, startQueueWatch, stopQueueWatch } from "./lib/queue.svelte";
  import { pending } from "./ui/dialog.svelte";
  import { formPending } from "./ui/form.svelte";

  import KbList from "./views/KbList.svelte";
  import KbDocs from "./views/KbDocs.svelte";
  import KbChunks from "./views/KbChunks.svelte";
  import KbSettings from "./views/KbSettings.svelte";
  import Jobs from "./views/Jobs.svelte";
  import DocView from "./views/DocView.svelte";
  import Search from "./views/Search.svelte";
  import Settings from "./views/Settings.svelte";
  import ApiDoc from "./views/ApiDoc.svelte";

  let theme = $state<"dark" | "light">("dark");
  /**
   * 窄屏标记。
   *
   * 不能写成 $derived(matchMedia(...).matches)：派生只在这几个依赖变化时
   * 重算，而 matchMedia 不是响应式依赖——拖动窗口时它纹丝不动，
   * 浮层 dock 与 scrim 就会在错误的时机出现或赖着不走。
   * 所以老实订阅 media query 的 change 事件。
   */
  let narrow = $state(false);

  const section = $derived(sectionOf(route));

  let cleanupMq = () => {};

  onMount(() => {
    startRouter();
    initDock();
    theme = (document.documentElement.dataset.theme as "dark" | "light") ?? "dark";
    // 与 KbList 共用同一个带去重的加载器：以前两处各拉一次 /api/kbs，
    // 落在 #/ 上就是同一刻两个相同请求
    void refreshKbs();
    startQueueWatch();
    window.addEventListener("keydown", onKey);

    const mq = matchMedia(NARROW_QUERY);
    const apply = (): void => { narrow = mq.matches; };
    apply();
    mq.addEventListener("change", apply);
    cleanupMq = () => mq.removeEventListener("change", apply);

    return () => {
      window.removeEventListener("keydown", onKey);
      stopQueueWatch();
      cleanupMq();
    };
  });

  async function refreshKbs(): Promise<void> {
    try {
      await loadKbs();
    } catch {
      // 知识库列表拉不到不该阻塞整个界面：各视图会各自报错并可重试
    }
  }

  function toggleTheme(): void {
    theme = theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem("raggi.theme", theme); } catch { /* 忽略 */ }
  }

  /** 导航后收起窄屏浮层：否则 dock 会盖在刚打开的页面上。 */
  function afterNavigate(): void {
    if (narrow) dock.collapsed = true;
  }

  /**
   * 壳层快捷键。
   *
   * 这里把"什么时候不接管按键"写全，是因为检索页的 `/` 只排除了
   * INPUT/TEXTAREA——焦点在 <select> 上、Ctrl+/、或对话框开着时它照样拦截，
   * 于是"按了键却没填进该填的地方"。壳层若犯同样的错，影响面是全站。
   *
   * 这份清单比 `lib/actions.ts` 的 `isTypingTarget` **更宽**：还排除了 BUTTON 与 A。
   * 两者确实不等价，所以没有强行合并——Enter/Space 是激活焦点控件，
   * 焦点在链接上时数字键也不该把用户踢到别的 section；而检索页的 `/` 在
   * 按钮上按就应该生效（那正是"我要搜东西"的意思）。
   */
  function onKey(e: KeyboardEvent): void {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const t = e.target as HTMLElement | null;
    if (t && (/^(INPUT|TEXTAREA|SELECT|BUTTON|A)$/.test(t.tagName) || t.isContentEditable)) return;
    // 确认框/表单框开着时，键盘归它们
    if (pending.current || formPending.current) return;

    if (e.key === "Escape") {
      if (narrow && !dock.collapsed) { dock.collapsed = true; e.preventDefault(); }
      return;
    }
    const n = Number.parseInt(e.key, 10);
    if (Number.isInteger(n) && n >= 1 && n <= SECTIONS.length) {
      location.hash = SECTIONS[n - 1]!.href;
      afterNavigate();
      e.preventDefault();
    }
  }

  /* ---- 拖拽调宽 ----
   * dock 左贴边，所以指针的 clientX 就是目标宽度；clamp 在 dock.svelte.ts 里。
   *
   * 拖动期间要给整屏加 `cursor: col-resize; user-select: none`，否则快速拖动时
   * 指针掠过文字会变成"选中一段文本"而不是"改宽度"。这个状态以前靠
   * `document.body.classList.add()`——现在它是 `$state`，由壳层根元素持有，
   * 与「折叠态用 class:is-collapsed」同一种写法，少一处绕过框架改 DOM。
   */
  let resizing = $state(false);
  function beginDrag(e: PointerEvent): void {
    const el = e.currentTarget as HTMLElement;
    if (dock.collapsed) dock.collapsed = false;
    try { el.setPointerCapture(e.pointerId); } catch { /* 老浏览器忽略 */ }
    resizing = true;
    setDockWidth(e.clientX);
  }
  function onDrag(e: PointerEvent): void {
    if (e.buttons === 0) return;
    setDockWidth(e.clientX);
  }
  function endDrag(e: PointerEvent): void {
    const el = e.currentTarget as HTMLElement;
    try { el.releasePointerCapture(e.pointerId); } catch { /* 忽略 */ }
    resizing = false;
  }
  function onHandleKey(e: KeyboardEvent): void {
    const step = e.shiftKey ? 32 : 8;
    if (e.key === "ArrowLeft") { nudgeDockWidth(-step); e.preventDefault(); }
    else if (e.key === "ArrowRight") { nudgeDockWidth(step); e.preventDefault(); }
    else if (e.key === "Home") { nudgeDockWidth(-dock.width); e.preventDefault(); }
  }
</script>

<header class="topbar">
  <!-- 折叠开关画在 <Dock> **之前**、且属于顶栏而不是 dock：
       第一代侧栏把开关放在被折叠的容器里，折叠后没人能再打开它。 -->
  <button class="icon-btn dock-toggle" type="button"
    aria-expanded={!dock.collapsed} aria-controls="dock"
    aria-label={dock.collapsed ? "展开导航栏" : "折叠导航栏"}
    title={dock.collapsed ? "展开导航栏" : "折叠导航栏"}
    onclick={toggleDock}>
    <span class="dock-toggle-ico" class:flip={!dock.collapsed}><Icon name="chevron" /></span>
  </button>

  <a class="brand" href="#/">
    <svg class="brand-mark" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      stroke-width="1.8" stroke-linecap="round" aria-hidden="true">
      <path d="M4 5h16M4 12h10M4 19h7"/>
    </svg>
    <span class="brand-name">Raggi</span>
  </a>

  <div class="topbar-spacer"></div>

  <div class="topbar-actions">
    {#if app.busy > 0}
      <!-- 可见的忙碌指示：没有它，慢请求期间界面看起来就是卡住了 -->
      <div class="busy" role="progressbar" aria-label="正在加载"></div>
    {/if}
    <a class="queue-chip" class:is-active={queue.active > 0} href="#/jobs"
      aria-label={queue.active > 0 ? `队列中有 ${queue.active} 个任务在跑，点击查看` : "队列空闲，点击查看任务"}>
      <span class="queue-dot" aria-hidden="true"></span>
      {#if queue.active > 0}
        队列 {queue.active}
      {:else}
        任务
      {/if}
    </a>
    <button class="icon-btn" type="button" onclick={toggleTheme}
      aria-label={theme === "dark" ? "切换到浅色主题" : "切换到深色主题"}
      title={theme === "dark" ? "浅色" : "深色"}>
      <Icon name={theme === "dark" ? "sun" : "moon"} />
    </button>
  </div>
</header>

<div class="shell" class:is-collapsed={dock.collapsed} class:is-resizing={resizing} style={dockStyle()}>
  {#if !dock.collapsed}
    <Dock {route} {section} onnavigate={afterNavigate} />
    <!-- 把手按 ARIA 的 splitter 模式实现：role="separator" + tabindex +
         aria-valuenow，左右键调宽。Svelte 的 a11y 检查不认识这个模式
         （它按"非交互元素不该有 tabindex/监听器"报警），但 W3C 的
         Splitter 范例就是这么写的，所以显式豁免并留下说明，
         而不是把语义换成勉强及格的 role="slider"。 -->
    <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
    <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
    <div class="dock-handle" role="separator" tabindex="0"
      aria-orientation="vertical" aria-label="导航栏宽度"
      aria-valuemin={DOCK_W_MIN} aria-valuemax={DOCK_W_MAX}
      aria-valuenow={dock.width}
      onpointerdown={beginDrag} onpointermove={onDrag}
      onpointerup={endDrag} onpointercancel={endDrag}
      onkeydown={onHandleKey}></div>
  {:else}
    <!-- 折叠时仍渲染 dock（图标条），所以 section 切换器不会消失；
         id="dock" 必须存在，aria-controls 才指得到东西 -->
    <nav class="dock dock-rail" id="dock" aria-label="视图">
      {#each SECTIONS as s (s.key)}
        <a class="section-item" href={s.href}
          class:is-current={section.key === s.key}
          aria-current={section.key === s.key ? "page" : undefined}
          title={s.label}
          onclick={afterNavigate}>
          <Icon name={s.icon} />
          <span class="sr-only">{s.label}</span>
        </a>
      {/each}
    </nav>
    <div class="dock-handle"></div>
  {/if}

  <div class="workspace">
    <!-- 面包屑栏：内容区内部的位置指示。层级仍由 App 统一渲染——
         各页自拼时顺序与文案很快就开始漂移。 -->
    <div class="crumbbar" hidden={crumbs.items.length <= 1}>
      <nav class="crumbs" aria-label="面包屑">
        {#each crumbs.items as c, i (i)}
          {#if i > 0}<span class="crumb-sep" aria-hidden="true">/</span>{/if}
          {#if i === crumbs.items.length - 1}
            <span class="crumb-cur" aria-current="page">{c.label}</span>
          {:else}
            <a class="crumb" href={c.href ?? "#/"}>{c.label}</a>
          {/if}
        {/each}
      </nav>
    </div>

    <!-- 路由切换的过渡按 key 触发：key 变化时旧视图退出、新视图进入。
         不加过渡的话，切页是瞬间替换，用户会短暂失去"我在哪"的定位感。 -->
    <!-- is-full：文档详情页用 SplitView 双栏，外层不滚、两栏各自滚动，
         高度由这条 flex 链交下来（见 app.css §5）。其他页面照常整页滚动。 -->
    <main id="main" class="main" class:is-full={route.name === "doc"}>
      <div class="content-wrap">
        <!-- 过渡挂在 .route 上而不是 main 上：main 现在是常驻的滚动容器，
             只有 .route 随路由重建，切页才有"旧视图退出、新视图进入"的定位感。

             key 里带上 tab 与 kbId：工作区三个标签共用 route.name="kb"，
             只 key name 的话切标签不会重建视图（数据停留在上一个标签），
             也没有过渡。换库同理。 -->
        {#key `${route.name}:${route.tab}:${route.params.kbId ?? ""}:${route.params.docId ?? ""}`}
          <div class="route" in:fade={{ duration: dur(160) }}>
            {#if route.name === "kb" || route.name === "doc"}
              {@const kbId = route.params.kbId ?? ""}
              <WorkspaceTabs {kbId} tab={route.tab} />
              {#if route.name === "doc"}
                <DocView {kbId} docId={route.params.docId ?? ""}
                  chunk={route.query.get("chunk") ?? ""} />
              {:else if route.tab === "chunks"}
                <KbChunks {kbId} scope={route.query.get("scope") ?? ""} />
              {:else if route.tab === "settings"}
                <KbSettings {kbId} />
              {:else}
                <KbDocs {kbId} />
              {/if}
            {:else if route.name === "search"}
              <Search query={route.query} />
            {:else if route.name === "jobs"}
              <Jobs />
            {:else if route.name === "settings"}
              <Settings tab={route.params.tab ?? "models"} />
            {:else if route.name === "apidoc"}
              <ApiDoc query={route.query} />
            {:else}
              <KbList />
            {/if}
          </div>
        {/key}
      </div>
    </main>
  </div>
</div>

{#if narrow && !dock.collapsed}
  <button class="dock-scrim" type="button" aria-label="关闭导航栏"
    onclick={() => (dock.collapsed = true)}></button>
{/if}

<Toaster />
<ConfirmDialog />
<FormDialog />

<style>
  .busy {
    width: 14px; height: 14px; flex: none;
    border: 2px solid var(--line-strong); border-top-color: var(--accent);
    border-radius: 50%; animation: spin 700ms linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  .dock-toggle-ico { display: inline-flex; transition: transform 140ms var(--ease); }
  .dock-toggle-ico.flip { transform: rotate(180deg); }

  /* 折叠态的图标条：仍然是可点的导航，不是隐藏 */
  .dock-rail {
    align-items: stretch; padding: var(--u2) 0; gap: var(--u1);
  }
  .dock-rail .section-item { justify-content: center; padding: var(--u2); gap: 0; }

  @media (prefers-reduced-motion: reduce) {
    .busy { animation-duration: 2s; }
    .dock-toggle-ico { transition: none; }
  }
</style>
