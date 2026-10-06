/**
 * 表单对话（Promise 化）。
 *
 * 与 confirmDialog 分开的原因：确认框回答「要不要做」，表单向用户索取
 * 内容（改标题、写分块、调方案）。两者都是模态，但字段、校验与错误
 * 处理完全不同，混在一个原语里会让调用方到处写 if。
 *
 * 提交回调在对话内执行：失败时错误就地显示、对话保持打开——否则一段
 * 刚敲好的长文本会因为一次网络抖动被关掉，用户得重新写。
 */

export interface SelectOption {
  value: string;
  label: string;
}

export interface FormField {
  name: string;
  label: string;
  /** 缺省 text；number 在提交时转数字，switch 为布尔开关 */
  type?: "text" | "textarea" | "number" | "switch" | "select";
  value?: string | boolean;
  /** 仅 type=select */
  options?: SelectOption[];
  rows?: number;
  min?: number;
  max?: number;
  placeholder?: string;
  note?: string;
  required?: boolean;
  /** 依赖其它字段的禁用规则（如「继承」勾选后数值不可填） */
  disabledWhen?: (values: Record<string, string | boolean>) => boolean;
}

export interface FormOptions {
  title: string;
  intro?: string;
  submitLabel?: string;
  fields: FormField[];
  /** 抛出的错误显示在表单内且不关闭对话 */
  onSubmit: (values: Record<string, string | boolean>) => Promise<void>;
}

export interface FormState extends FormOptions {
  id: number;
  resolve: (ok: boolean) => void;
}

let seq = 0;
export const formPending = $state<{ current: FormState | null }>({ current: null });

export function formDialog(opts: FormOptions): Promise<boolean> {
  // 同时只允许一个表单；后来的请求先取消前一个，避免堆叠
  formPending.current?.resolve(false);
  return new Promise<boolean>((resolve) => {
    formPending.current = { id: ++seq, resolve, ...opts };
  });
}

export function settleForm(ok: boolean): void {
  const cur = formPending.current;
  if (!cur) return;
  formPending.current = null;
  cur.resolve(ok);
}
