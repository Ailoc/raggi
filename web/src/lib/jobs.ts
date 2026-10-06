/**
 * 入库任务轮询：把「上传后干等」变成「立刻返回 + 进度可见」。
 *
 * 入库走队列后，接口用 `?wait=false` 立即返回 job_id；前端轮询
 * /api/jobs/{job_id} 直到终态。这样上传大文件时界面不会假死，
 * 用户能看到当前处于哪个阶段（解析 / 切分 / 向量化 / 写库）。
 */
import { api } from "./api";

export interface Job {
  job_id: string;
  doc_id: string;
  stage: string;
  progress: number;
  total: number;
  parser_engine: string;
  error: string | null;
  started_at: string;
  ended_at: string | null;
  /** true 表示已到终态，可停止轮询 */
  terminal: boolean;
}

/** 阶段 → 人话。队列里的阶段名是给机器看的，这里翻译给用户。 */
const STAGE_LABEL: Record<string, string> = {
  queued: "排队中",
  fetch: "抓取网页",
  parse: "解析文档",
  load: "加载内容",
  split: "切分文本",
  embed: "向量化",
  write: "写入索引",
  done: "完成",
  failed: "失败",
};

export function stageLabel(stage: string): string {
  return STAGE_LABEL[stage] ?? stage;
}

/** 处理中的文档状态：这些状态下文档列表需要轮询等它变终态。 */
export const PENDING_STATUS = new Set(["indexing", "pending", "running", "queued", "parse", "split", "embed"]);

const POLL_MS = 700;
/** 轮询上限：约 10 分钟。超时后不再等待，但任务仍在后台继续。 */
const MAX_POLLS = Math.ceil(600_000 / POLL_MS);

/**
 * 等待任务结束。
 *
 * @param onTick 每次拿到状态时回调，用于更新进度文案
 * @param signal 调用方的生命周期信号：视图被销毁时必须停下来。
 *   没有它，用户离开页面后这个循环还会继续轮询到上限（约 10 分钟），
 *   并且回调会往已经卸载的组件状态里写值。
 * @returns 终态的任务对象；轮询超时或被中止返回 null（任务仍在后台跑）
 */
export async function waitForJob(
  jobId: string,
  onTick?: (j: Job) => void,
  signal?: AbortSignal,
): Promise<Job | null> {
  for (let i = 0; i < MAX_POLLS; i++) {
    if (signal?.aborted) return null;
    await new Promise((r) => setTimeout(r, POLL_MS));
    if (signal?.aborted) return null;
    let job: Job;
    try {
      job = await api<Job>(`/api/jobs/${encodeURIComponent(jobId)}`);
    } catch {
      // 单次查询失败不中断轮询（服务短暂繁忙 / 任务记录被清理）
      continue;
    }
    onTick?.(job);
    if (job.terminal) return job;
  }
  return null;
}

/** 从任务结果里取错误信息（失败时才有）。 */
export function jobError(job: Job | null): string {
  if (!job) return "任务仍在后台执行，请稍后查看文档列表";
  return job.error || "未知原因";
}
