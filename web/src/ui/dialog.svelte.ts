/**
 * 确认对话（Promise 化）。
 *
 * 为什么不用原生 confirm()：
 * 1. 无法量化影响——「确定删除？」和「将删除 342 个分块」是两种沟通质量
 * 2. 阻塞渲染线程，轮询进行时弹窗会让整个界面卡住
 * 3. 无法要求"输入名称以确认"这类强确认，误触即毁数据
 * 4. 样式、焦点、读屏行为都不可控
 */

export interface ConfirmOptions {
  title: string;
  /** 量化影响：说清会发生什么，而不是问"确定吗" */
  body: string;
  confirmLabel?: string;
  cancelLabel?: string;
  /** 破坏性操作：按钮用告警色，且默认焦点落在「取消」 */
  destructive?: boolean;
  /** 输入这段文字才能确认（用于不可逆操作） */
  requireTyping?: string;
}

export interface DialogState extends ConfirmOptions {
  id: number;
  resolve: (ok: boolean) => void;
}

let seq = 0;
export const pending = $state<{ current: DialogState | null }>({ current: null });

export function confirmDialog(opts: ConfirmOptions): Promise<boolean> {
  // 同时只允许一个确认框；后来的请求先取消前一个，避免堆叠
  pending.current?.resolve(false);
  return new Promise<boolean>((resolve) => {
    pending.current = { id: ++seq, resolve, ...opts };
  });
}

export function settle(ok: boolean): void {
  const cur = pending.current;
  if (!cur) return;
  pending.current = null;
  cur.resolve(ok);
}
