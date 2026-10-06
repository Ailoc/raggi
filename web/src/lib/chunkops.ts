/**
 * 分块操作：编辑 / 新增 / 删除 / 启停（含批量）。
 *
 * 抽成模块的原因：文档页与「独立分块」页的分块表格交互完全相同，
 * 差异只有归属（doc_id 或 kb_id）。放在一个地方，两边的确认文案、
 * 失败提示与「重新向量化」语义才不会各自漂移。
 */
import { api } from "./api";
import { confirmDialog } from "../ui/dialog.svelte";
import { formDialog } from "../ui/form.svelte";
import { toast } from "../ui/toast.svelte";
import type { Chunk } from "./types";

/** 编辑分块内容（重新向量化）。返回是否有改动，供调用方决定是否重载。 */
export async function editChunkDialog(chunk: Chunk): Promise<boolean> {
  return formDialog({
    title: "编辑分块",
    intro: chunk.origin === "manual"
      ? "手工分块，编辑后直接重新向量化。"
      : "解析生成的分块，保存后会标记为「已编辑」，并重新向量化；重新切分时手工新增的分块仍会保留。",
    submitLabel: "保存并重新向量化",
    fields: [{
      name: "text", label: "内容", type: "textarea", rows: 12,
      value: chunk.text ?? "", required: true,
    }],
    onSubmit: async (v) => {
      await api(`/api/chunks/${encodeURIComponent(chunk.chunk_id)}`, {
        method: "PATCH",
        body: {
          text: String(v.text),
          // parsed 分块需后端保护：force=true 才能改写
          force: true,
          // 乐观并发：带上你打开这份内容时的版本。若期间别人改过，
          // 服务端返回 409 而不是静默覆盖——那时对话里会显示原因，
          // 用户关闭后重新打开即可看到最新内容。
          ...(chunk.updated_at ? { updated_at: chunk.updated_at } : {}),
        },
      });
      toast("已保存并重新向量化");
    },
  });
}

/** 新增分块：传 docId 归该文档，否则挂在该知识库下（独立分块）。 */
export async function addChunkDialog(opts: {
  kbId: string;
  docId?: string;
}): Promise<boolean> {
  const standalone = !opts.docId;
  return formDialog({
    title: standalone ? "新增独立分块" : "新增分块",
    intro: standalone
      ? "独立分块直接挂在知识库下，与文档分块一同参与检索，适合术语表、口径说明。"
      : "新增的分块挂在本文档下，与解析生成的分块一同参与检索。",
    submitLabel: "新增",
    fields: [{
      name: "text", label: "内容", type: "textarea", rows: 8, required: true,
    }],
    onSubmit: async (v) => {
      await api("/api/chunks", {
        method: "POST",
        body: {
          text: String(v.text),
          doc_id: opts.docId ?? null,
          kb_id: opts.kbId,
        },
      });
      toast(standalone ? "已新增独立分块" : "已新增分块");
    },
  });
}

/** 删除分块（永久）。返回是否删除成功。 */
export async function deleteChunk(chunk: Chunk): Promise<boolean> {
  const ok = await confirmDialog({
    title: `删除分块 #${chunk.ordinal}`,
    body: "将永久删除该分块。若只是暂时不想被检索到，用「停用」更合适。",
    confirmLabel: "删除",
    destructive: true,
  });
  if (!ok) return false;
  try {
    await api(`/api/chunks/${encodeURIComponent(chunk.chunk_id)}`, { method: "DELETE" });
    toast("已删除");
    return true;
  } catch (e) {
    toast(e instanceof Error ? e.message : String(e), "err");
    return false;
  }
}

/** 停用 / 启用单个分块。返回是否成功。 */
export async function setChunkEnabled(chunk: Chunk, enabled: boolean): Promise<boolean> {
  try {
    await api(`/api/chunks/${encodeURIComponent(chunk.chunk_id)}/enabled`, {
      method: "PATCH", body: { enabled },
    });
    toast(enabled ? "已启用" : "已停用，不再参与检索");
    return true;
  } catch (e) {
    toast((enabled ? "启用" : "停用") + "失败：" +
      (e instanceof Error ? e.message : String(e)), "err");
    return false;
  }
}

/**
 * 批量启停。
 *
 * 走 PATCH /chunks/batch-enabled（一次写入），而不是循环调单条端点：
 * 循环在 200 条时是 200 个请求且无原子性，中途失败会让「哪些块被停用了」
 * 变得无法回答。返回是否成功（调用方据此刷新列表）。
 */
export async function batchSetEnabled(chunks: Chunk[], enabled: boolean): Promise<boolean> {
  if (chunks.length === 0) return false;
  const label = enabled ? "启用" : "停用";
  try {
    const r = await api<{ updated: number; enabled: boolean }>(
      "/api/chunks/batch-enabled", {
        method: "PATCH",
        body: { chunk_ids: chunks.map((c) => c.chunk_id), enabled },
      });
    toast(`已${label} ${r.updated} 块`);
    return true;
  } catch (e) {
    toast(`批量${label}失败：` + (e instanceof Error ? e.message : String(e)), "err");
    return false;
  }
}
