<script lang="ts">
  import { dismiss, toasts } from "./toast.svelte";
  import Icon from "./Icon.svelte";
</script>

<!-- aria-live 让读屏用户听到异步结果，而不只是看到 -->
<div class="toaster" role="status" aria-live="polite">
  {#each toasts as t (t.id)}
    <div class="toast toast-{t.kind}">
      <span class="toast-ico"><Icon name={t.kind === "err" ? "alert" : "check"} /></span>
      <span class="toast-text">{t.text}</span>
      <button class="toast-x" type="button" aria-label="关闭提示"
        onclick={() => dismiss(t.id)}><Icon name="close" /></button>
    </div>
  {/each}
</div>

<style>
  .toaster {
    position: fixed; z-index: 60;
    right: var(--u4); bottom: var(--u4);
    display: flex; flex-direction: column; gap: var(--u2);
    max-width: min(420px, calc(100vw - var(--u6)));
  }
  .toast {
    display: flex; align-items: flex-start; gap: var(--u2);
    padding: var(--u2) var(--u3);
    border: 1px solid var(--line);
    border-radius: var(--r-md);
    background: var(--surface-2);
    box-shadow: var(--shadow-pop);
    color: var(--text);
    /* 入场由 CSS 动画完成：toast 是新增节点，过渡没有起点 */
    animation: toast-in 180ms var(--ease) both;
  }
  .toast-err { border-color: var(--red); }
  .toast-ico { flex: none; color: var(--green); margin-top: 1px; }
  .toast-err .toast-ico { color: var(--red); }
  .toast-text { flex: 1 1 auto; font-size: 13px; line-height: 1.45; }
  .toast-x {
    flex: none; display: inline-flex; border: 0; background: none; cursor: pointer;
    color: var(--text-3); line-height: 1; padding: 2px;
  }
  .toast-x:hover { color: var(--text); }
  @keyframes toast-in {
    from { opacity: 0; transform: translateY(6px) scale(0.98); }
    to   { opacity: 1; transform: none; }
  }
  @media (prefers-reduced-motion: reduce) {
    .toast { animation-duration: 1ms; }
  }
</style>
