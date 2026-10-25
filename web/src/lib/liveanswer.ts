/**
 * 流式正文的独立订阅源。
 *
 * 正文如果放在页面级 state 里，每来一个 delta 就要替换整轮对话数组：
 * 878 行的 Search 会整棵 reconcile 一遍，而且顺带触发"贴底滚动"里的
 * 布局读取。这里把正在生成的这一行拆出来，只有它订阅文本、只有它重渲染；
 * 其它轮次与外层页面的引用都不变。
 *
 * 通知仍按帧合并：模型一秒钟能推几十个 delta，按 delta 重渲染没有意义。
 */
export class LiveAnswerStore {
  private text = "";
  private readonly listeners = new Set<() => void>();
  private frame: number | null = null;
  private onGrow: (() => void) | null = null;

  /** 文本变长后的回调（用于"贴底时跟随滚动"），与渲染同一帧触发。 */
  setOnGrow(fn: (() => void) | null): void {
    this.onGrow = fn;
  }

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => { this.listeners.delete(listener); };
  };

  getSnapshot = (): string => this.text;

  reset(): void {
    this.text = "";
    this.cancel();
    this.emit();
  }

  push(chunk: string): void {
    if (!chunk) return;
    // 文本同步累积：onDone 可能早于下一帧到达，读到的必须是完整正文
    this.text += chunk;
    if (this.frame != null) return;
    this.frame = requestAnimationFrame(() => {
      this.frame = null;
      this.emit();
      this.onGrow?.();
    });
  }

  /** 流结束（完成/报错）：丢掉挂起的帧，别再触达已经卸载的订阅者。 */
  finish(): void {
    this.cancel();
  }

  private cancel(): void {
    if (this.frame != null) {
      cancelAnimationFrame(this.frame);
      this.frame = null;
    }
  }

  private emit(): void {
    for (const listener of [...this.listeners]) listener();
  }
}
