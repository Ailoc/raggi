import { useSyncExternalStore } from "react";
import { api } from "./api";
import type { Job, JobList, Kb } from "./types";

/**
 * 全局状态（kbs / busy / theme / crumbs）+ 订阅。
 *
 * 用"模块级对象 + useSyncExternalStore"而不是 Context + useState：
 * 这些状态都有**非组件的写者**——API 层的 401 处理要切主题、
 * 轮询要在组件树之外跑、crumbs 由任意视图在 effect 里写。放进 Context
 * 就得把它们一路透传下去；模块级 store 天然谁都能读写。
 * （这也是 Svelte 版 `.svelte.ts` 模块级 `$state` 的等价物，迁移后
 * 心智模型不变。）
 */

interface State {
  kbs: Kb[];
  kbsLoaded: boolean;
  /** 顶栏忙碌计数：并发请求数，>0 时显示指示 */
  busy: number;
  theme: "dark" | "light";
  crumbs: Array<{ label: string; href?: string }>;
}

let state: State = {
  kbs: [],
  kbsLoaded: false,
  busy: 0,
  theme: (document.documentElement.dataset.theme as "dark" | "light") ?? "dark",
  crumbs: [],
};

const listeners = new Set<() => void>();

function emit(): void {
  for (const l of listeners) l();
}

function set(patch: Partial<State>): void {
  state = { ...state, ...patch };
  emit();
}

export function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function getState(): State {
  return state;
}

/** 组件读状态的唯一入口（快照引用在 set 时整体替换，浅比较即可） */
export function useStore(): State {
  return useSyncExternalStore(subscribe, getState);
}

// ---- 主题 ----------------------------------------------------------------

export function setTheme(theme: "dark" | "light"): void {
  set({ theme });
  document.documentElement.dataset.theme = theme;
  document.documentElement.style.colorScheme = theme;
  try {
    localStorage.setItem("raggi.theme", theme);
  } catch { /* 隐私模式：只影响下次打开时的默认值 */ }
}

// ---- 忙碌计数 ------------------------------------------------------------

export function track<T>(p: Promise<T>): Promise<T> {
  set({ busy: state.busy + 1 });
  return p.finally(() => set({ busy: Math.max(0, state.busy - 1) }));
}

// ---- 知识库列表 ----------------------------------------------------------

/**
 * 列表加载：**并发去重 + 世代号**。
 *
 * 两个坑（Svelte 版踩过、这里原样保留防护）：
 * 1. 同一刻两个调用（壳层与总览页各拉一次）会打两个相同请求，后回来的
 *    覆盖前一个——用 in-flight 的 promise 去重；
 * 2. 慢请求先发后到会把新数据盖回旧数据——用世代号丢弃过期响应。
 */
let kbsInflight: Promise<Kb[]> | null = null;
let kbsGeneration = 0;

export function loadKbs(): Promise<Kb[]> {
  if (kbsInflight) return kbsInflight;
  const gen = ++kbsGeneration;
  kbsInflight = api<Kb[]>("/api/kbs")
    .then((list) => {
      if (gen === kbsGeneration) set({ kbs: list, kbsLoaded: true });
      return list;
    })
    .finally(() => {
      // 清空 in-flight 必须在 finally：失败后下一次调用要能重试
      if (gen === kbsGeneration) kbsInflight = null;
    });
  return kbsInflight;
}

/** 强制下次 loadKbs() 重新拉取（移动文档 / 增删知识库后调用） */
export function invalidateKbs(): void {
  kbsInflight = null;
}

export function kbName(kbId: string): string {
  return state.kbs.find((k) => k.kb_id === kbId)?.name ?? "知识库";
}

// ---- 面包屑 --------------------------------------------------------------

/**
 * 面包屑由视图写入、壳层渲染。
 * React 的重渲染是异步的：连续两次 setCrumbs 不该叠成一个数组——
 * 这里是整体替换而不是合并（与 Svelte 版一致）。
 */
export function setCrumbs(items: Array<{ label: string; href?: string }>): void {
  set({ crumbs: items });
}

// ---- 队列摘要 ------------------------------------------------------------

export interface QueueSummary {
  jobs: Job[];
  active: number;
  loaded: boolean;
}

let queue: QueueSummary = { jobs: [], active: 0, loaded: false };
const queueListeners = new Set<() => void>();
const queueSnapshot = (): QueueSummary => queue;

function setQueue(next: QueueSummary): void {
  queue = next;
  for (const l of queueListeners) l();
}

export function useQueue(): QueueSummary {
  return useSyncExternalStore(
    (l) => {
      queueListeners.add(l);
      return () => queueListeners.delete(l);
    },
    queueSnapshot,
  );
}

const ACTIVE_MS = 4000;
const IDLE_MS = 25000;
let queueTimer: ReturnType<typeof setTimeout> | undefined;
let queueStarted = false;
/**
 * 轮询世代号。
 *
 * React StrictMode 会挂载→卸载→再挂载：第一次 `queueTick()` 的 promise
 * 在 `stopQueueWatch()` 之后才 resolve，若它的 `.finally(queueSchedule)`
 * 不看世代，就会在新会话里再排一条链——两条轮询同时打接口，且再也
 * 收不掉。每次 start/stop 都推进世代，旧链的收尾因此自然失效。
 */
let queueGen = 0;

async function queueTick(gen: number): Promise<void> {
  try {
    // 服务端先过滤再分页：不能先取最近 20 条再由前端筛，
    // 否则更早的活跃任务会被一堆更新的终态任务挤出窗口，顶栏显示 0。
    const r = await api<JobList>("/api/jobs?status=active&limit=50");
    if (gen !== queueGen) return;
    setQueue({ jobs: r.items, active: r.items.length, loaded: true });
  } catch {
    // 后端重启期间取不到队列：保留上一次结果（摘要不是权威列表，
    // 不值得因为一次失败把整个壳层打成错误态）
  }
}

function queueSchedule(gen: number): void {
  if (!queueStarted || gen !== queueGen) return;
  clearTimeout(queueTimer);
  const wait = queue.active > 0 ? ACTIVE_MS : IDLE_MS;
  queueTimer = setTimeout(() => {
    if (!queueStarted || gen !== queueGen) return;
    if (document.visibilityState !== "hidden") {
      void queueTick(gen).finally(() => queueSchedule(gen));
    } else {
      queueSchedule(gen);
    }
  }, wait);
}

export function startQueueWatch(): void {
  if (queueStarted) return;
  queueStarted = true;
  const gen = ++queueGen;
  void queueTick(gen).finally(() => queueSchedule(gen));
}

export function stopQueueWatch(): void {
  queueStarted = false;
  queueGen += 1;
  clearTimeout(queueTimer);
}

/**
 * 立刻刷新一次队列（提交入库/重解析之后调用）。
 *
 * 空闲期轮询是 25s 一次，而任务提交恰恰发生在"之前什么也没跑"的时刻——
 * 不 poke 的话"点了上传、顶栏没动静"要持续 25 秒，足够让人重复提交第二遍。
 */
export function pokeQueue(): void {
  if (!queueStarted) return;
  const gen = queueGen;
  void queueTick(gen).finally(() => queueSchedule(gen));
}
