/**
 * 跨视图共享状态。
 *
 * 旧实现把知识库列表放在一个非响应式的普通对象里，每个视图都要记得
 * 在变更后手动重渲染；漏一处就出现"改了没生效"。这里用 $state，
 * 订阅关系由编译器建立。
 */
import { api } from "./api";
import type { Kb } from "./types";

export const app = $state({
  /** 知识库列表：检索范围下拉、新建提示、面包屑都读它 */
  kbs: [] as Kb[],
  /** 当前生效的向量维度：保存模型配置时识别维度变更并二次确认 */
  embedDim: undefined as number | undefined,
  /** 顶栏检索框与全局 loading 态 */
  busy: 0,
});

export function setKbs(list: Kb[]): void {
  app.kbs = list;
  const kb = list[0];
  if (kb && app.embedDim === undefined) {
    const d = (kb as unknown as { embed_dim?: number }).embed_dim;
    if (typeof d === "number") app.embedDim = d;
  }
}

/**
 * 拉取知识库列表，**并发去重**。
 *
 * 之前 App 挂载时拉一次、KbList 挂载时又拉一次——落在 `#/` 上就是同一刻
 * 两个相同请求，后回来的覆盖前一个，白跑一趟。现在谁都能调 `loadKbs()`，
 * 同一时刻只会有一次在飞，后来者共享同一个 promise。
 */
let inflight: Promise<Kb[]> | null = null;
/** 世代号：每次失效 +1。在飞的旧请求回来时会发现自己已过期，直接丢弃。 */
let generation = 0;

/** 强制下次 loadKbs() 重新拉取（移动文档 / 增删知识库后调用）。 */
export function invalidateKbs(): void {
  inflight = null;
  generation += 1;
}

export function loadKbs(): Promise<Kb[]> {
  if (inflight) return inflight;
  const gen = generation;
  inflight = api<Kb[]>("/api/kbs")
    .then((list) => {
      // 失效前发出的请求可能带着旧数据回来。若不丢弃，它会在失效后
      // 的新请求之后执行 setKbs，把已经刷新的计数又覆盖回旧值 ——
      // 界面于是停在「移动已生效但计数没变」的状态，且不会自愈。
      if (gen !== generation) return list;
      setKbs(list);
      return list;
    })
    .finally(() => {
      if (gen === generation) inflight = null;
    });
  return inflight;
}

export function kbName(kbId: string): string {
  return app.kbs.find((k) => k.kb_id === kbId)?.name ?? kbId;
}

/** 包裹异步操作，让顶栏进度条对任何请求都成立。 */
export async function track<T>(fn: () => Promise<T>): Promise<T> {
  app.busy++;
  try {
    return await fn();
  } finally {
    app.busy--;
  }
}
