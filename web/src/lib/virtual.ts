/**
 * 定高行窗口化。
 *
 * 为什么需要：一篇 3000 块的手册会渲染 3000 个 `<tr>`。DOM 节点数与
 * 内存占用随之线性增长，输入过滤时每一次按键都要 diff 全表——
 * 界面会明显发涩。只渲染视口内的行，节点数就与数据量脱钩。
 *
 * 为什么用**定高**：变高行需要逐行测量并缓存，测量本身触发同步布局，
 * 在长列表上反而更慢；而分块列表本来就适合单行省略的扫描式阅读。
 */
export interface Window {
  /** 渲染区间 [start, end) */
  start: number;
  end: number;
  /** 上下占位高度，撑出真实滚动条 */
  padTop: number;
  padBottom: number;
}

export const ROW_H = 34;
const OVERSCAN = 8;

export function windowOf(
  scrollTop: number,
  viewportH: number,
  total: number,
  rowH = ROW_H,
): Window {
  const first = Math.floor(scrollTop / rowH);
  const visible = Math.ceil(viewportH / rowH) + 1;
  // 上下各多渲染几行：滚动时不会先看到空白再看到内容
  const start = Math.max(0, first - OVERSCAN);
  const end = Math.min(total, first + visible + OVERSCAN);
  return {
    start,
    end,
    padTop: start * rowH,
    padBottom: Math.max(0, (total - end) * rowH),
  };
}
