<script lang="ts">
  /**
   * 列表底部的「再加载」块：分页是 offset 式的，一次只取回一页，
   * 所以"还有多少没取到"必须显式告诉用户，否则空列表看起来像"没有数据"。
   *
   * 抽出来的理由不是"少几行标签"，而是这四块已经漂成三个样子：
   * - 已加载数：三处用 `pager.loaded`，Jobs 用 `rows.length`（今天相等，
   *   哪天追加逻辑改了就会不一致地显示）；
   * - `aria-busy` 只有 KbDocs 那份写了，另外三个按钮在请求期间对读屏用户
   *   毫无反馈——同一个动作在四个地方有四种可访问性；
   * - "再加载多少个"的 `Math.min(size, remaining)` 公式抄了四遍。
   * 现在公式、口径与可访问性只有一份。
   *
   * 顺带留住一条旧教训：分页必须用 **offset 追加**，不是把 `limit` 越加越大。
   * 服务端把 limit 夹在 500（documents.py:283、chunks.py:49），于是
   * 200→400→600 的第三次点击会被静默压回 500——按钮还在、也在发请求，
   * 但一条都不会多。所以 `onmore` 的语义是"再取一页接上"，不是"换个大 limit 重取"。
   */
  let {
    loaded, total, size, remaining, unit, busy = false, label = "已加载",
    onmore,
  }: {
    loaded: number; total: number; size: number; remaining: number;
    /** 量词：篇 / 块 / 条 */
    unit: string;
    busy?: boolean;
    /** 前缀文案，默认「已加载」。过滤态下调用方会写「已加载命中」，因为那个 total 是命中数 */
    label?: string;
    onmore: () => void;
  } = $props();
</script>

<div class="listmore">
  <span class="muted">{label} {loaded} / {total} {unit}</span>
  <button class="btn btn-sm" type="button"
    aria-busy={busy || undefined}
    disabled={busy} onclick={onmore}>
    再加载 {Math.min(size, remaining)} {unit}
  </button>
</div>
