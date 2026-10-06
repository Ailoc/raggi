<script lang="ts">
  import { pending, settle } from "./dialog.svelte";
  import { focusFirst, trap } from "../lib/actions";

  let typed = $state("");
  let panel = $state<HTMLElement | null>(null);
  let typeInput = $state<HTMLInputElement | null>(null);
  let confirmBtn = $state<HTMLButtonElement | null>(null);
  let cancelBtn = $state<HTMLButtonElement | null>(null);

  const cur = $derived(pending.current);
  const needType = $derived(cur?.requireTyping ?? "");
  const canConfirm = $derived(!needType || typed.trim() === needType);

  /**
   * 每次打开重置输入，并把焦点交给"用户下一步要动的地方"。
   *
   * 不能一律 focus 确认按钮：`requireTyping` 没填对时它是 disabled，而
   * `focus()` 一个 disabled 元素**静默失败**，焦点留在 body——表现为
   * "打开的框既打不了字也走不动键"。破坏性动作把焦点放在「取消」一侧。
   * 用 `await tick()` 而非 `setTimeout(…, 0)`：后者赌渲染，前者等渲染。
   */
  $effect(() => {
    const c = cur;
    // 同步读这几个 ref：首次挂载那一帧它们还是 null，读一次就建立依赖，
    // `bind:this` 到位后 effect 自己再跑一次，焦点才真的送得出去。
    const node = panel;
    if (!c || !node) return;
    typed = "";
    const target = c.requireTyping ? typeInput : c.destructive ? cancelBtn : confirmBtn;
    void focusFirst(target ?? node);
  });
</script>

{#if cur}
  <!-- 背景点击不该静默关闭破坏性对话：让用户明确做出选择 -->
  <div class="scrim" role="presentation">
    <div class="panel" bind:this={panel} role="alertdialog" aria-modal="true"
      aria-labelledby="dlg-title" aria-describedby="dlg-body"
      tabindex="-1" use:trap={{ onEscape: () => settle(false) }}>
      <h2 class="dlg-title" id="dlg-title">{cur.title}</h2>
      <p class="dlg-body" id="dlg-body">{cur.body}</p>

      {#if needType}
        <label class="field">
          <span class="field-label">
            输入 <code>{needType}</code> 以确认
          </span>
          <input bind:this={typeInput} bind:value={typed} autocomplete="off" spellcheck="false" />
        </label>
      {/if}

      <div class="dlg-actions">
        <button class="btn" type="button" bind:this={cancelBtn}
          onclick={() => settle(false)}>
          {cur.cancelLabel ?? "取消"}
        </button>
        <button class="btn" class:btn-danger={cur.destructive}
          class:btn-primary={!cur.destructive}
          type="button" bind:this={confirmBtn} disabled={!canConfirm}
          onclick={() => settle(true)}>
          {cur.confirmLabel ?? "确认"}
        </button>
      </div>
    </div>
  </div>
{/if}

<style>
  .scrim {
    position: fixed; inset: 0; z-index: 70;
    display: grid; place-items: center;
    padding: var(--u4);
    background: oklch(0 0 0 / 0.55);
    animation: fade 120ms var(--ease) both;
  }
  .panel {
    width: min(460px, 100%);
    padding: var(--u4);
    border: 1px solid var(--line-strong);
    border-radius: var(--r-lg);
    background: var(--surface);
    box-shadow: var(--shadow-pop);
    /* 从触发处附近进入：缩放起点略小，落定不反弹 */
    animation: pop 160ms var(--ease) both;
  }
  /* 焦点容器例外（等效替代：整个对话框就是视觉焦点所在），
     只作程序化聚焦落点，用户不会用 Tab 停在它上面 */
  .panel:focus { outline: none; }
  .dlg-title { margin: 0 0 var(--u2); font-size: 15px; font-weight: 600; }
  .dlg-body { margin: 0 0 var(--u4); font-size: 13px; line-height: 1.6; color: var(--text-2); }
  .dlg-actions { display: flex; justify-content: flex-end; gap: var(--u2); margin-top: var(--u4); }
  .btn-danger { border-color: var(--red); color: var(--red); }
  .btn-danger:hover:not(:disabled) { background: var(--red); color: var(--bg); }
  code {
    font-family: var(--font-mono); font-size: 12px;
    padding: 0 4px; border-radius: 3px; background: var(--surface-3);
  }
  @keyframes fade { from { opacity: 0; } to { opacity: 1; } }
  @keyframes pop {
    from { opacity: 0; transform: translateY(4px) scale(0.98); }
    to   { opacity: 1; transform: none; }
  }
  @media (prefers-reduced-motion: reduce) {
    .scrim, .panel { animation-duration: 1ms; }
  }
</style>
