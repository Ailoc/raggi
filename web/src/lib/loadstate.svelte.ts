/**
 * 列表加载状态：把「没有数据」和「数据在后台更新」分开。
 *
 * 旧写法是一个 `loading` 布尔，于是每次刷新都等于"整页重进加载态"：
 * 骨架屏换掉表格、工具栏连同输入框被卸载重建。实测到的后果是
 * **有任务在跑时每 2.5 秒丢一次过滤框焦点**，以及"点了刷新一下白"。
 *
 * 三个状态各自回答一个独立的问题：
 *
 *   phase       有没有可显示的数据？没有才给骨架屏
 *   refreshing  正在后台更新？有数据时的任何重载都是它
 *   error       最近一次失败的原因；有数据时不清空数据，只加一条横幅
 *
 * 关键点：**有数据就不进 initial**。所以轮询、手动刷新、改过滤条件
 * 都自然走 refreshing，不需要调用方各自记得传"静默"标志——少一个
 * 能被忘记的参数，就少一处会漂移的地方。
 */

export type Phase = "initial" | "ready" | "failed";

export interface LoadState {
  phase: Phase;
  refreshing: boolean;
  error: string;
}

export interface Loader {
  state: LoadState;
  /**
   * 执行一次取数。
   *
   * @param fetch   真正的请求
   * @param apply   成功且**不是过期响应**时写入视图状态
   * @returns 是否成功（失败时 error 已写好，调用方可据此决定是否提示）
   */
  run<T>(fetch: () => Promise<T>, apply: (value: T) => void): Promise<boolean>;
  /** 结构性变更（换知识库、退出过滤）：承认"现在没有可显示的数据" */
  reset(): void;
  /** 当前是否有可显示的数据 */
  hasData(): boolean;
}

export function loader(): Loader {
  const state = $state<LoadState>({ phase: "initial", refreshing: false, error: "" });
  let hasData = false;
  /** 请求序号：并发时只认最后一次的结果，否则慢的那次会覆盖快的 */
  let seq = 0;

  async function run<T>(fetch: () => Promise<T>, apply: (value: T) => void): Promise<boolean> {
    const mine = ++seq;
    state.refreshing = hasData;
    if (!hasData) state.phase = "initial";
    // 有数据时不清 error 也不清数据：刷新失败应当"数据还在 + 一条横幅"，
    // 而不是把用户已经看到的内容抹掉
    try {
      const value = await fetch();
      if (mine !== seq) return false;   // 已有更新的请求在飞，这次结果作废
      apply(value);
      hasData = true;
      state.phase = "ready";
      state.error = "";
      return true;
    } catch (e) {
      if (mine !== seq) return false;
      state.error = e instanceof Error ? e.message : String(e);
      if (!hasData) state.phase = "failed";
      return false;
    } finally {
      if (mine === seq) state.refreshing = false;
    }
  }

  return {
    state,
    run,
    reset() { hasData = false; state.phase = "initial"; state.error = ""; },
    hasData() { return hasData; },
  };
}

/**
 * 分页累加器。
 *
 * 为什么不用 `limit += 200` 的老写法：服务端把 limit 夹在 500
 * （`documents.py:283`、`chunks.py:49`），于是 200→400→600 的第三次
 * 点击会被静默压回 500——"再加载"按钮还在，但点了永远不多一条。
 * offset 追加既避开这个上限，也让每次请求的量恒定。
 */
export class Pager {
  readonly size: number;
  /**
   * 必须是 `$state`。
   *
   * 之前它们是普通字段：界面读 `pager.total` / `pager.more` 时没有任何响应式
   * 依赖，只有**恰好**别的 `$state` 变了、模板顺带重渲染时才跟着更新。
   * 实测到的后果就是"匹配 5 块"和 3 行数据并排出现——过滤已经生效、
   * 服务端的 total 也已经是 3，标签还停在旧值上。
   * `{#if pager.more}` 同样中招：它会在该隐藏时继续显示。
   */
  offset = $state(0);
  total = $state(0);

  constructor(size = 200) { this.size = size; }

  get loaded(): number { return this.offset; }
  get more(): boolean { return this.offset < this.total; }
  get remaining(): number { return Math.max(0, this.total - this.offset); }

  restart(): void { this.offset = 0; }
  /** 本次请求要用的 limit/offset 参数 */
  params(): string { return `limit=${this.size}&offset=${this.offset}`; }
  /** 成功后记录 total，并把游标推进到已取条数 */
  advance(count: number, total: number): void {
    this.total = total;
    this.offset += count;
  }
}
