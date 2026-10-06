/**
 * 轻提示。
 *
 * 放在模块级 $state 里而不是 DOM 里：旧实现靠往 body 追加节点并 setTimeout
 * 移除，切视图时残留的提示会叠在新页面上。
 */
export interface Toast {
  id: number;
  text: string;
  kind: "ok" | "err";
}

let seq = 0;
export const toasts = $state<Toast[]>([]);

export function toast(text: string, kind: "ok" | "err" = "ok"): void {
  const id = ++seq;
  toasts.push({ id, text, kind });
  // 错误留久一点：用户需要时间读完并决定下一步
  setTimeout(() => dismiss(id), kind === "err" ? 6000 : 3000);
}

export function dismiss(id: number): void {
  const i = toasts.findIndex((t) => t.id === id);
  if (i >= 0) toasts.splice(i, 1);
}
