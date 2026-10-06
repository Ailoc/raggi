/**
 * dock（常驻上下文栏）的状态。
 *
 * 与第一代侧栏的关键差别，就是这一个文件里的取舍：
 *
 * - **折叠态不持久化**。旧侧栏把"折叠了"写进 localStorage，刷新后仍折叠，
 *   而折叠时切换入口被一起隐藏 → 用户重开浏览器还是进不去，只能改存储自救。
 *   现在每次加载都从"展开"开始（窄屏除外，那是按视口现算的，不是记忆）。
 * - **只有宽度持久化**。宽度记下来是纯收益：拖到手感合适的宽度后刷新还在。
 *   它不可能造成无法恢复的状态，前提是它被 clamp 住——见 clampWidth()。
 * - **复位入口常驻**（resetLayout）。任何布局状态都不该是单行道。
 */

const WIDTH_KEY = "raggi.dock.width";

/**
 * 窄屏判据。JS 侧只有这一处。
 *
 * styles/shell.css 的 `@media (max-width: 900px)` 必须与它一致——CSS 读不到
 * JS 常量，所以这条耦合靠注释钉住：改一处就要改另一处。两处各写一遍且
 * 值不同时，会出现"浮层已展开但网格还留着 dock 列"这类只在边界宽度出现的
 * 布局裂缝。
 */
export const NARROW_QUERY = "(max-width: 900px)";

/** 与 NARROW_QUERY 同步：媒体查询是否命中。 */
export function isNarrow(): boolean {
  return typeof matchMedia === "function" && matchMedia(NARROW_QUERY).matches;
}

export const DOCK_W_MIN = 240;
export const DOCK_W_MAX = 320;
export const DOCK_W_DEFAULT = 264;
/** 折叠态下的图标条宽度：仍然是"能点的导航"，不是隐藏。 */
export const DOCK_COLLAPSED_W = 56;

/** 视口占比上限：小屏上 dock 不能把内容列挤没。 */
const VIEWPORT_CAP = 0.38;

export const dock = $state({
  collapsed: false,
  width: DOCK_W_DEFAULT,
  /** 已展开的库（对象树按库→文档两层，展开态只在内存里） */
  expandedKb: "" as string,
});

function clampWidth(w: number): number {
  // 上界同时受"令牌上限"与"当前视口比例"约束：
  // 不 clamp 的话，把手可以被拖到视口外，宽度写进 localStorage 后
  // 刷新仍是那个值——于是 dock 看起来"消失了"，这正是必须避免的那类状态。
  const cap = Math.min(DOCK_W_MAX, Math.floor(window.innerWidth * VIEWPORT_CAP));
  // 极窄视口下 cap 可能小于 MIN：此时以能容纳图标条为准，避免 NaN/负值
  const max = Math.max(cap, DOCK_COLLAPSED_W + 24);
  return Math.max(DOCK_W_MIN, Math.min(max, Math.round(w)));
}

function loadWidth(): number {
  try {
    const raw = localStorage.getItem(WIDTH_KEY);
    if (!raw) return DOCK_W_DEFAULT;
    const n = Number.parseInt(raw, 10);
    return Number.isFinite(n) ? clampWidth(n) : DOCK_W_DEFAULT;
  } catch {
    // 隐私模式读不到就退回默认值——布局不该因为存储不可用而坏掉
    return DOCK_W_DEFAULT;
  }
}

/** 首帧前调用一次：宽度从存储恢复，折叠态按视口现算（不读存储）。 */
export function initDock(): void {
  dock.width = loadWidth();
  // 窄屏默认折叠：这是"按当前视口决定初始视图"，不是"记住上次折叠了"
  dock.collapsed = isNarrow();
}

export function setDockWidth(w: number): void {
  dock.width = clampWidth(w);
  try { localStorage.setItem(WIDTH_KEY, String(dock.width)); } catch { /* 忽略 */ }
}

export function toggleDock(): void {
  dock.collapsed = !dock.collapsed;
}

/** 键盘调宽：把手聚焦后 ←/→（含 Shift 大步长）可微调。 */
export function nudgeDockWidth(delta: number): void {
  if (dock.collapsed) dock.collapsed = false;
  setDockWidth(dock.width + delta);
}

/** 恢复默认布局：展开 + 默认宽度 + 清掉记住的宽度。 */
export function resetLayout(): void {
  dock.collapsed = false;
  dock.width = DOCK_W_DEFAULT;
  try { localStorage.removeItem(WIDTH_KEY); } catch { /* 忽略 */ }
}

/** 给外壳用的 CSS 自定义属性。折叠时宽度不生效（由 .is-collapsed 接管）。 */
export function dockStyle(): string {
  return `--dock-w: ${clampWidth(dock.width)}px`;
}
