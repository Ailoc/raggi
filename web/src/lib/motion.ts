/**
 * 动效参数。
 *
 * 为什么需要一个模块而不是散落的字面量：动效的一致性来自**同一套
 * 时间轴**。当每个组件各写各的 150/200/300ms，界面会显得杂乱——
 * 用户说不出哪里不对，只是觉得"不精致"。
 *
 * 时长取自系统的物理直觉：
 * - 进入比退出慢，因为进入需要被理解，退出只需要被确认
 * - 越大的元素越慢（质量大），越小的越干脆
 */

/** 用户是否要求减少动效。系统设置优先于任何设计意图。 */
export function reduced(): boolean {
  if (typeof matchMedia !== "function") return false;
  return matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/** 时长：用户要求减少时归零，而不是变快——变快仍会触发前庭反应。 */
export function dur(ms: number): number {
  return reduced() ? 0 : ms;
}

/** 进入：稍慢，元素需要被看清。 */
export const ENTER = 220;
/** 退出：约为进入的 70%，离开应该比到来更干脆。 */
export const EXIT = Math.round(ENTER * 0.7);
/** 微交互：按钮、悬停、展开。 */
export const MICRO = 140;

export interface FlyParams {
  y?: number;
  x?: number;
  duration?: number;
  delay?: number;
}

/** 列表项进入：小幅上移 + 淡入。列表项之间用递增延迟形成级联。 */
export function enterItem(index: number, step = 18): FlyParams {
  return {
    y: 6,
    duration: dur(ENTER),
    // 级联延迟封顶，否则长列表末尾的项要等很久才出现
    delay: dur(Math.min(index, 12) * step),
  };
}
