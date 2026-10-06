/**
 * 面包屑：由各视图在加载时写入，顶栏下方的面包屑栏统一渲染。
 *
 * 集中管理的原因：旧版每页各自拼面包屑，文案与层级顺序很快就漂移
 * （有的少一层、有的把当前页也做成链接）。这里只有一份状态，
 * `setCrumbs` 的调用点就是全部事实来源。
 */
export interface Crumb {
  label: string;
  /** 缺省为当前页（不可点） */
  href?: string;
}

export const crumbs = $state<{ items: Crumb[] }>({ items: [] });

export function setCrumbs(items: Crumb[]): void {
  crumbs.items = items;
}
