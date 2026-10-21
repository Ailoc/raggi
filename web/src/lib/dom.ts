/**
 * 浏览器副作用的最小集合。
 *
 * 焦点陷阱、滚动、贴底等行为在 React + MUI 里已由组件库提供（Dialog 自己
 * 处理焦点与 Escape）；这里只保留浏览器没有声明式等价物的操作。
 */

/**
 * 选中元素的全部文本。
 *
 * 剪贴板 API 在非安全上下文（内网 http）或被拒绝时会抛错——此时"复制"
 * 按钮必须给出下一步，而不是按了毫无反应。选中后用户可按 Ctrl+C。
 */
export function selectText(el: HTMLElement): void {
  const sel = window.getSelection();
  if (!sel) return;
  const range = document.createRange();
  range.selectNodeContents(el);
  sel.removeAllRanges();
  sel.addRange(range);
}
