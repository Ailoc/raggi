/**
 * 入库任务取消。
 *
 * 与服务端的能力边界对应（rag/ingest/queue.py 的 IngestQueue.cancel）：
 * 排队中的任务是真取消，运行中的只能协作式取消——解析与向量化都不响应
 * 中断。返回的 outcome 必须**如实转述**给用户，不能一律显示「已取消」：
 * 那会让人以为已经停下，实际还在写库。
 */
import { api } from "./api";
import { toast } from "../ui/toast.svelte";

export type CancelOutcome = "cancelled" | "running" | "finished" | "unknown";

export interface CancelResult {
  outcome: CancelOutcome;
  message: string;
}

/**
 * 取消一个任务。
 *
 * @param onOutcome 服务端处置结果回调（如用于把文档行从「处理中」改回状态）
 * @returns 服务端回复；请求失败时返回 null（错误已 toast）
 */
export async function cancelJob(
  jobId: string,
  onOutcome?: (r: CancelResult) => void,
): Promise<CancelResult | null> {
  if (!jobId) return null;
  try {
    const r = await api<CancelResult & { ok: boolean }>(
      `/api/jobs/${encodeURIComponent(jobId)}`, { method: "DELETE" });
    const out: CancelResult = { outcome: r.outcome, message: r.message };
    // message 是服务端按 outcome 组织的说明，转述它而不是自己编一套文案
    toast(r.message || "已请求取消", r.outcome === "running" ? "ok" : "ok");
    onOutcome?.(out);
    return out;
  } catch (e) {
    toast("取消失败：" + (e instanceof Error ? e.message : String(e)), "err");
    return null;
  }
}
