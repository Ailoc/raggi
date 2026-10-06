<script lang="ts">
  import { formPending, settleForm } from "./form.svelte";
  import type { FormField } from "./form.svelte";
  import { focusFirst, trap } from "../lib/actions";

  let panel = $state<HTMLElement | null>(null);
  let vals = $state<Record<string, string | boolean>>({});
  let error = $state("");
  let busy = $state(false);

  const cur = $derived(formPending.current);

  function initial(fields: FormField[]): Record<string, string | boolean> {
    const v: Record<string, string | boolean> = {};
    for (const f of fields) v[f.name] = f.value ?? (f.type === "switch" ? false : "");
    return v;
  }

  /**
   * 每次打开重置输入与错误，并把焦点放进对话的第一个可用控件。
   *
   * 只挑「可用」的控件：首个字段是开关、后面的数字框被禁用时，
   * `focus()` 一个 disabled 元素会**静默失败**——焦点留在 body，
   * 于是这个框看起来"按了没反应"。选择器与原因都收在 `lib/actions.ts` 一份。
   *
   * 这里**同步**读 `panel`：首次挂载那一帧它还是 null，而读一次就建立依赖，
   * `bind:this` 到位后 effect 自动再跑一次，焦点才真的送得出去。
   * 旧写法是 `setTimeout(…, 0)`——赌渲染完成，不是等渲染完成。
   */
  $effect(() => {
    const c = cur;
    const node = panel;
    if (!c || !node) return;
    vals = initial(c.fields);
    error = "";
    busy = false;
    void focusFirst(node);
  });

  const canSubmit = $derived(
    !busy && (cur?.fields ?? []).every(
      (f) => !f.required || String(vals[f.name] ?? "").trim() !== ""),
  );

  async function submit(): Promise<void> {
    const c = cur;
    if (!c || !canSubmit) return;
    busy = true;
    error = "";
    try {
      // 数字字段在这里转成 number：输入框的 value 永远是字符串
      const out: Record<string, string | boolean> = { ...vals };
      for (const f of c.fields) {
        if (f.type === "number") {
          out[f.name] = String(Number(out[f.name] || 0));
        }
      }
      await c.onSubmit(out);
      settleForm(true);
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      busy = false;
    }
  }

  /**
   * Escape 关闭。忙的时候不许关：提交正在飞，关掉之后结果无处可去，
   * 用户以为保存了、其实没有。Tab 陷阱本身在 `lib/actions.ts` 的 `trap` 里。
   */
  function onEscape(): void {
    if (!busy) settleForm(false);
  }
</script>

{#if cur}
  <div class="scrim" role="presentation">
    <div class="panel" bind:this={panel} role="dialog" aria-modal="true"
      aria-labelledby="form-title" tabindex="-1" use:trap={{ onEscape }}>
      <h2 class="dlg-title" id="form-title">{cur.title}</h2>
      {#if cur.intro}<p class="dlg-body">{cur.intro}</p>{/if}

      <div class="fields">
        {#each cur.fields as f (f.name)}
          {@const disabled = f.disabledWhen?.(vals) ?? false}
          {#if f.type === "switch"}
            <label class="switch" for={`f-${cur.id}-${f.name}`}>
              <input id={`f-${cur.id}-${f.name}`} type="checkbox"
                checked={Boolean(vals[f.name])} disabled={disabled}
                onchange={(e) => (vals[f.name] = e.currentTarget.checked)} />
              <span class="switch-track"></span>
              <span class="switch-label">{f.label}</span>
            </label>
          {:else}
            <label class="field">
              <span class="field-label">{f.label}</span>
              {#if f.type === "textarea"}
                <textarea rows={f.rows ?? 6} placeholder={f.placeholder}
                  disabled={disabled}
                  value={String(vals[f.name] ?? "")}
                  oninput={(e) => (vals[f.name] = e.currentTarget.value)}></textarea>
              {:else if f.type === "select"}
                <select disabled={disabled}
                  value={String(vals[f.name] ?? "")}
                  onchange={(e) => (vals[f.name] = e.currentTarget.value)}>
                  {#each f.options ?? [] as opt (opt.value)}
                    <option value={opt.value}>{opt.label}</option>
                  {/each}
                </select>
              {:else}
                <input type={f.type === "number" ? "number" : "text"}
                  min={f.min} max={f.max} placeholder={f.placeholder}
                  disabled={disabled} autocomplete="off"
                  value={String(vals[f.name] ?? "")}
                  oninput={(e) => (vals[f.name] = e.currentTarget.value)} />
              {/if}
              {#if f.note}<span class="field-note">{f.note}</span>{/if}
            </label>
          {/if}
        {/each}
      </div>

      {#if error}<p class="form-error" role="alert">{error}</p>{/if}

      <div class="dlg-actions">
        <button class="btn" type="button" disabled={busy}
          onclick={() => settleForm(false)}>取消</button>
        <button class="btn btn-primary" type="button" disabled={!canSubmit}
          onclick={submit}>
          {busy ? "保存中…" : (cur.submitLabel ?? "保存")}
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
    width: min(560px, 100%);
    max-height: 86vh;
    overflow: auto;
    padding: var(--u4);
    border: 1px solid var(--line-strong);
    border-radius: var(--r-lg);
    background: var(--surface);
    box-shadow: var(--shadow-pop);
    animation: pop 160ms var(--ease) both;
  }
  /* 焦点容器例外（等效替代：整个对话框就是视觉焦点所在），
     只作程序化聚焦落点，用户不会用 Tab 停在它上面 */
  .panel:focus { outline: none; }
  .dlg-title { margin: 0 0 var(--u2); font-size: 15px; font-weight: 600; }
  .dlg-body { margin: 0 0 var(--u4); font-size: 13px; line-height: 1.6; color: var(--text-2); }
  .fields { display: flex; flex-direction: column; gap: var(--u3); }
  .form-error {
    margin: var(--u3) 0 0; font-size: 12.5px;
    color: var(--red); line-height: 1.5;
  }
  .dlg-actions { display: flex; justify-content: flex-end; gap: var(--u2); margin-top: var(--u4); }
  @keyframes fade { from { opacity: 0; } to { opacity: 1; } }
  @keyframes pop {
    from { opacity: 0; transform: translateY(4px) scale(0.98); }
    to   { opacity: 1; transform: none; }
  }
  @media (prefers-reduced-motion: reduce) {
    .scrim, .panel { animation-duration: 1ms; }
  }
</style>
