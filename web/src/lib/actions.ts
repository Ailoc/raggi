/**
 * DOM 副作用的唯一落脚点（Svelte 5 actions + 一小撮纯函数）。
 *
 * 阶段 7 的目标不是"消灭命令式代码"——浏览器 API 总得有人调，`scrollIntoView`
 * 就是没有等价声明式写法的。要消灭的是**同一个行为在多个视图里各写一遍**：
 * 一旦分叉，修一处就漏一处，而漏掉的那处看起来完全正常。
 *
 * 已经有两组实证：
 * - 焦点陷阱曾有两份。一份把 Escape/Tab 挂在**面板元素**上——焦点一旦离开面板
 *   （比如被聚焦的按钮在确认过程中被移除），键盘就再也关不掉这个框；另一份挂在
 *   **window** 上并写明了理由。两份的 selector 也已经漂了：一份排除了 disabled
 *   输入，另一份没有。
 * - `revealHit`（"目标出现就滚进视区"）在 `Search.svelte` 与 `DocView.svelte`
 *   里逐字相同，连注释都是一个字不改地抄的。
 *
 * 留在视图里的命令式调用只应该是**这一份元素独有、且无法声明化**的那些
 * （剪贴板降级要操作选区、主题要写到 `<html>`、下载要造 `<a>`），
 * 由 `tests/test_frontend_wiring.py::test_dom_side_effects_live_in_actions` 划界。
 */
import { tick } from "svelte";
import { reduced } from "./motion";

/**
 * 可聚焦元素的选择器。disabled 一律排除：`focus()` 一个 disabled 元素会
 * **静默失败**，焦点留在 body 上，于是 Escape 与 Tab 循环整体失效——
 * 表现是"对话框打不开也关不掉"，而代码看起来什么都没做错。
 */
export const FOCUSABLE = [
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "a[href]",
  "iframe",
  'summary',
  '[tabindex]:not([tabindex="-1"])',
].join(", ");

/** 面板内当前可聚焦的元素，按文档顺序。 */
export function focusables(node: HTMLElement): HTMLElement[] {
  return [...node.querySelectorAll<HTMLElement>(FOCUSABLE)];
}

export interface TrapOptions {
  /** Escape 的处理。不传则该对话框不响应 Escape（破坏性确认框故意这样也说得通）。 */
  onEscape?: () => void;
}

/**
 * 焦点陷阱 + Escape，**挂在 window 上**。
 *
 * 挂元素上的陷阱有个前提：焦点一直在元素里面。这个前提在对话框里不成立——
 * 提交中按钮会被禁用、内容会整块替换，焦点随时可能落回 body。届时对话框
 * 既关不掉也走不出去，而它正盖着整屏。
 *
 * 焦点已经在面板外时不是"什么都不做"，而是按方向把它拉回循环内。
 */
export function trap(node: HTMLElement, options: TrapOptions = {}) {
  let opts = options;
  function onkey(e: KeyboardEvent): void {
    if (e.key === "Escape") {
      if (opts.onEscape) {
        e.preventDefault();
        opts.onEscape();
      }
      return;
    }
    if (e.key !== "Tab") return;
    const items = focusables(node);
    if (items.length === 0) { e.preventDefault(); return; }
    const first = items[0] as HTMLElement;
    const last = items[items.length - 1] as HTMLElement;
    const active = document.activeElement;
    if (!node.contains(active)) {
      e.preventDefault();
      (e.shiftKey ? last : first).focus();
      return;
    }
    if (!e.shiftKey && active === last) { e.preventDefault(); first.focus(); }
    else if (e.shiftKey && active === first) { e.preventDefault(); last.focus(); }
  }
  window.addEventListener("keydown", onkey);
  return {
    // onEscape 常捕获会变的局部状态（如 busy），换实现时必须一起换掉
    update(next: TrapOptions): void { opts = next ?? {}; },
    destroy(): void { window.removeEventListener("keydown", onkey); },
  };
}

/**
 * 把焦点交给面板里第一个能接住它的元素，都没有就交给面板本身
 * （面板带 `tabindex="-1"`，正是为这一刻准备的）。
 *
 * 用 `await tick()` 而不是 `setTimeout(…, 0)`：后者赌的是"这一帧该渲染完了"，
 * 内容多或机器慢就焦点在旧 DOM 上；前者等的是渲染真的完成。
 */
export async function focusFirst(node: HTMLElement): Promise<void> {
  await tick();
  (focusables(node)[0] ?? node).focus();
}

export interface RevealArg {
  /** 换一个目标就换一个值；值不变不滚。 */
  key: string;
  block?: ScrollLogicalPosition;
  behavior?: ScrollBehavior;
}

/**
 * 目标出现或换目标时滚进视区。
 *
 * "key 变了才滚"是这条行为的全部难点：不判等的话，任何一次无关重渲染都会把
 * 用户正在读的位置顶走；判等则要求调用方给出**能代表目标身份**的值。
 *
 * 前置条件：**调用时目标元素已经有布局盒**。阶段 7 实测踩过反例——分组折叠是用
 * `hidden` 实现的，卡片元素一直挂载着，"解除 hidden"和"改 key"在同一次 flush 里，
 * scrollIntoView 落在一个还没有布局的元素上，表现为"点了索引只闪一下、页面不动"。
 * 修法不是在这里等一帧：`requestAnimationFrame` 在隐藏文档里根本不触发，那会把
 * 滚动静默丢掉。是调用方 `await tick()` 等渲染完成，再改 key（见 ApiDoc 的 jump）。
 */
export function reveal(node: HTMLElement, arg: RevealArg): { update(next: RevealArg): void } {
  let last = "";
  let conf = arg;
  function run(key: string): void {
    if (key && key !== last) {
      // 平滑滚动是由动画帧驱动的：在隐藏文档里它根本不推进（实测 scrollTop 停在 0），
      // 而 prefers-reduced-motion 的用户本来就不该看到它。两种情况下退回即时定位，
      // 让"跳过去了"这件事至少是真的。
      const want = conf.behavior ?? "auto";
      const behavior = want === "smooth" && reduced() ? "auto" : want;
      node.scrollIntoView({ block: conf.block ?? "center", behavior });
    }
    last = key;
  }
  run(arg.key);
  return {
    update(next: RevealArg): void {
      conf = next ?? conf;
      run(conf.key);
    },
  };
}

export interface PinBottomArg {
  /** 是否处于"应当贴底"的状态（流式进行中）。 */
  follow: boolean;
  /** 被跟踪的内容。它的变化就是"新字到了"的信号。 */
  text: string;
}

/**
 * 内容增长时把容器贴住底部（流式输出）。
 *
 * 为什么要 `text` 参与比较而不是"每次 update 都滚"：无关重渲染也把用户弹回
 * 底部，等于在他往回翻找引用出处时抢走滚动位置。
 *
 * 回归防护：这里的旧实现是一个 `$effect(() => { if (running && el) el.scrollTop =
 * el.scrollHeight })`——它同步读了 `running` 和元素，却**没读正文**，于是正文
 * 增长不会让它重跑：贴底只在流式开始那一刻发生一次，而那会儿还没有文字。
 * 效果就是"逐字输出的面板要用户手动追到底"，和它声称的相反。与阶段 6 的
 * ApiDoc debounce 是同一类错误（依赖没在同步阶段读出来）。
 */
export function pinBottom(node: HTMLElement, arg: PinBottomArg): { update(next: PinBottomArg): void } {
  let conf = arg;
  function run(next: PinBottomArg): void {
    const c = next ?? conf;
    const grew = c.text !== conf.text;
    conf = c;
    if (c.follow && grew) node.scrollTop = node.scrollHeight;
  }
  run(arg);
  return { update: run };
}

/**
 * 选中整段文本。`navigator.clipboard` 需要安全上下文与权限，内网 http 或被拒时
 * 会抛错——此时给一个"点了没反应"的复制按钮比不给按钮更糟，所以退化成
 * 帮用户选好，让他按 Ctrl+C。选区 API 没有声明式写法，留在这里而不是视图里。
 */
export function selectText(el: HTMLElement): void {
  const range = document.createRange();
  range.selectNodeContents(el);
  const sel = window.getSelection();
  sel?.removeAllRanges();
  sel?.addRange(range);
}

export interface SyncScrollArg {
  /** 期望的滚动位置（状态）。 */
  value: number;
  /** 用户滚动时写回状态。必须是回调：模块不认识组件的 `$state`。 */
  sink: (v: number) => void;
}

/**
 * `scrollTop` 双向同步。
 *
 * Svelte 5 的普通元素没有 `bind:scrollTop`，所以这层只能写成 action：
 * 用户滚动时写回状态，键盘换块时（状态变了）再把滚动位置推给 DOM。
 * **两边的相等性检查是这条行为的全部难度**：不设防的话，设值触发的 scroll 事件
 * 会立刻写回状态，状态又触发 update，形成"自己喂自己"的更新循环。
 */
export function syncScroll(node: HTMLElement, arg: SyncScrollArg): {
  update: (next: SyncScrollArg) => void; destroy: () => void;
} {
  let conf = arg;
  const onScroll = (): void => { conf.sink(node.scrollTop); };
  node.addEventListener("scroll", onScroll, { passive: true });
  if (node.scrollTop !== conf.value) node.scrollTop = conf.value;
  return {
    update(next: SyncScrollArg): void {
      conf = next ?? conf;
      if (node.scrollTop !== conf.value) node.scrollTop = conf.value;
    },
    destroy(): void { node.removeEventListener("scroll", onScroll); },
  };
}

/**
 * 判断"这一刻键盘该不该被全局快捷键接管"。
 *
 * 旧实现只排除了 INPUT/TEXTAREA，于是焦点在 `<select>` 上、按 Ctrl+/、
 * 或对话框开着时照样拦截——用户按了键却没填进该填的地方。
 * 集中一份，是为了让"排除条件"这个清单只有一处可修。
 */
export function isTypingTarget(el: EventTarget | null): boolean {
  if (!(el instanceof HTMLElement)) return false;
  const tag = el.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return true;
  return el.isContentEditable;
}
