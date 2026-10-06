/**
 * 队列摘要：让"有没有东西在跑"成为壳层的常驻信息。
 *
 * 现状是：想知道任务进度必须切到 #/jobs；而顶栏的忙碌圈只说"有请求在飞"，
 * 不说飞的是什么。入库是这产品最长的等待（解析→切分→向量化→写库），
 * 等待不可见就等于让用户怀疑界面卡死。
 *
 * 节奏：有活跃任务时 4s 一次，空了就退到 25s——没有东西在跑还每 4 秒
 * 打一次接口，是拿单机 SQLite/LanceDB 的读放大换心理安慰。
 * 标签页不可见时暂停（回来立刻补一次）。
 *
 * 注意这里**只读不改**：取消任务仍走 lib/jobcancel，列表页仍是权威。
 */
import { api } from "./api";
import type { Job, JobList } from "./types";

const ACTIVE_MS = 4000;
const IDLE_MS = 25000;
/** 摘要只关心前若干条：dock 里塞不下整个队列，也没必要 */
const WATCH_LIMIT = 20;

export interface QueueSummary {
  jobs: Job[];
  active: number;
  /** 后端排队/运行计数，拿不到时为 null（不假装是 0） */
  depth: number | null;
  loaded: boolean;
  error: string;
}

export const queue = $state<QueueSummary>({
  jobs: [], active: 0, depth: null, loaded: false, error: "",
});

let timer: ReturnType<typeof setTimeout> | undefined;
let started = false;

async function tick(): Promise<void> {
  try {
    // 与全站一致的 /api 前缀（Jobs.svelte 里那份 /api/v1 是第二套事实，
    // 阶段 2 统一）
    const r = await api<JobList>(`/api/jobs?limit=${WATCH_LIMIT}`);
    const active = r.items.filter((j) => !j.terminal);
    queue.jobs = active;
    queue.active = active.length;
    queue.depth = r.queue ? r.queue.pending : active.length;
    queue.loaded = true;
    queue.error = "";
  } catch (e) {
    // 后端重启期间取不到队列：保留上一次结果，只把错误记下来。
    // 摘要不是权威列表，不值得因为一次失败把整个壳层打成错误态。
    queue.error = e instanceof Error ? e.message : String(e);
  }
}

function schedule(): void {
  clearTimeout(timer);
  const wait = queue.active > 0 ? ACTIVE_MS : IDLE_MS;
  timer = setTimeout(() => {
    if (document.visibilityState !== "hidden") void tick().finally(schedule);
    else schedule();
  }, wait);
}

export function startQueueWatch(): void {
  if (started) return;
  started = true;
  void tick().then(schedule);
  document.addEventListener("visibilitychange", onVisible);
}

function onVisible(): void {
  // 切回标签页立刻补一次：等待中的用户第一动作就是回来看进度
  if (document.visibilityState === "visible") void tick();
}

export function stopQueueWatch(): void {
  started = false;
  clearTimeout(timer);
  document.removeEventListener("visibilitychange", onVisible);
}
