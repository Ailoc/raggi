/**
 * 文档级批量操作：批量删除。
 *
 * 走 DELETE /documents/batch（一次加锁），而不是循环调单条端点：
 * 循环在 50 篇时是 50 个请求且中途失败就说不清删了哪些。返回是否成功。
 */
import { api } from "./api";
import { toast } from "../ui/toast.svelte";

export interface DocDeleteBatchResult {
  deleted: number;
  /** 请求里已不存在的篇数（并发删除或过期链接） */
  skipped: number;
}

export async function deleteDocsBatch(
  docs: { doc_id: string; title: string }[],
): Promise<DocDeleteBatchResult | null> {
  if (docs.length === 0) return null;
  try {
    const r = await api<DocDeleteBatchResult>("/api/documents/batch", {
      method: "DELETE",
      body: { doc_ids: docs.map((d) => d.doc_id) },
    });
    if (r.skipped > 0) {
      toast(`已删除 ${r.deleted} 篇，${r.skipped} 篇已不存在`, "err");
    } else {
      toast(`已删除 ${r.deleted} 篇`);
    }
    return r;
  } catch (e) {
    toast("批量删除失败：" + (e instanceof Error ? e.message : String(e)), "err");
    return null;
  }
}
