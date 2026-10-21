import { api } from "./api";
import { confirmDialog, formDialog } from "./dialog";
import { CHUNK_STRATEGIES } from "./types";
import type { Chunk, DocPlan, Document, IngestResult, Kb } from "./types";
import { invalidateKbs, loadKbs, pokeQueue } from "./store";
import { toast } from "../ui/toast";

/**
 * 操作层：知识库 / 分块 / 文档三组"改数据"的动作。
 *
 * 与 Svelte 版同样的分工——视图只管"点了什么"，对话框、请求、错误转述、
 * 之后的失效刷新都在这里。集中在一处的收益是可数的：删除的确认文案说清
 * 影响面、提交失败**就地报错不关框**、任务提交后 poke 队列摘要，
 * 这三件事在 30+ 个调用点上各写一遍必然漂。
 */

// ---- 知识库 --------------------------------------------------------------

export async function removeKbDialog(
  kbId: string,
  name: string,
  docCount: number,
  chunkCount: number,
): Promise<boolean> {
  const ok = await confirmDialog({
    title: "删除知识库",
    body: `将删除「${name}」及其 ${docCount} 篇文档、${chunkCount} 个分块，`
      + `并从检索中移除。\n此操作不可撤销。`,
    confirmLabel: "删除知识库",
    destructive: true,
    // 删除知识库是本站影响最大的操作：输入库名才能确认
    requireTyping: name,
  });
  if (!ok) return false;
  try {
    await api(`/api/kbs/${encodeURIComponent(kbId)}`, { method: "DELETE" });
    toast(`已删除「${name}」`);
    invalidateKbs();
    await loadKbs();
    return true;
  } catch (e) {
    toast(e instanceof Error ? e.message : String(e), "err");
    return false;
  }
}

// ---- 分块 ----------------------------------------------------------------

/**
 * 编辑分块内容。
 *
 * `force: true` 是必需的：服务端对"解析产生的分块"要求显式 force 才允许改写
 * （防止脚本误伤），用户从界面上点"编辑"就是要改它。
 * `updated_at` 走乐观并发：拿的是"我读到的那一版"，若期间别人改过，
 * 服务端返回 409 而不是静默覆盖。
 */
export async function editChunkDialog(chunk: Chunk): Promise<boolean> {
  const v = await formDialog({
    title: "编辑分块",
    note: chunk.origin === "parsed"
      ? "这是解析产生的分块，保存后会标记为「已编辑」并重新向量化。"
      : undefined,
    fields: [
      { key: "text", label: "内容", type: "textarea", value: chunk.text, required: true },
    ],
    submitLabel: "保存",
    onSubmit: async (values) => {
      await api(`/api/chunks/${encodeURIComponent(chunk.chunk_id)}`, {
        method: "PATCH",
        body: {
          text: String(values.text ?? ""),
          force: true,
          ...(chunk.updated_at ? { updated_at: chunk.updated_at } : {}),
        },
      });
    },
  });
  if (v) toast("分块已更新，正在重新向量化");
  return !!v;
}

export async function addChunkDialog(
  opts: { kbId: string; docId?: string },
): Promise<boolean> {
  const v = await formDialog({
    title: opts.docId ? "新增分块" : "新增独立分块",
    note: opts.docId
      ? "新分块会追加到该文档末尾。"
      : "独立分块不属于任何文档，直接挂在知识库下（适合术语表、口径说明）。",
    fields: [
      { key: "text", label: "内容", type: "textarea", required: true },
    ],
    submitLabel: "新增",
    onSubmit: async (values) => {
      await api("/api/chunks", {
        method: "POST",
        body: {
          text: String(values.text ?? ""),
          ...(opts.docId ? { doc_id: opts.docId } : { kb_id: opts.kbId }),
        },
      });
    },
  });
  if (v) toast("分块已新增并入库");
  return !!v;
}

export async function deleteChunk(chunk: Chunk): Promise<boolean> {
  const ok = await confirmDialog({
    title: "删除分块",
    body: "将永久删除该分块。若只是暂时不想被检索到，用「停用」更合适。",
    confirmLabel: "删除",
    destructive: true,
  });
  if (!ok) return false;
  try {
    await api(`/api/chunks/${encodeURIComponent(chunk.chunk_id)}`, { method: "DELETE" });
    toast("分块已删除");
    return true;
  } catch (e) {
    toast(e instanceof Error ? e.message : String(e), "err");
    return false;
  }
}

export async function setChunkEnabled(chunk: Chunk, enabled: boolean): Promise<boolean> {
  try {
    await api(`/api/chunks/${encodeURIComponent(chunk.chunk_id)}/enabled`, {
      method: "PATCH",
      body: { enabled },
    });
    return true;
  } catch (e) {
    toast(e instanceof Error ? e.message : String(e), "err");
    return false;
  }
}

/**
 * 批量启停：走 `/chunks/batch-enabled` 一次写入。
 * 循环调单条端点会产生 200 次请求、无原子性，中途失败后"哪些块被停用了"
 * 变得无法回答（这是后端专门开这个端点时写下的原因）。
 */
export async function batchSetEnabled(chunks: Chunk[], enabled: boolean): Promise<boolean> {
  if (chunks.length === 0) return false;
  try {
    await api("/api/chunks/batch-enabled", {
      method: "PATCH",
      body: { chunk_ids: chunks.map((c) => c.chunk_id), enabled },
    });
    toast(`已${enabled ? "启用" : "停用"} ${chunks.length} 个分块`);
    return true;
  } catch (e) {
    toast(e instanceof Error ? e.message : String(e), "err");
    return false;
  }
}

// ---- 文档 ----------------------------------------------------------------

export async function renameDocDialog(docId: string, title: string): Promise<boolean> {
  const v = await formDialog({
    title: "重命名文档",
    fields: [{ key: "title", label: "标题", value: title, required: true }],
    submitLabel: "保存",
    onSubmit: async (values) => {
      await api(`/api/documents/${encodeURIComponent(docId)}`, {
        method: "PUT",
        body: { title: String(values.title ?? "").trim() },
      });
    },
  });
  if (v) toast("已重命名");
  return !!v;
}

/** 移动到其它知识库；返回新的 kbId（"移出到未分组"返回空串），取消返回 null */
export async function moveDocDialog(
  docId: string,
  currentKbId: string,
): Promise<string | null> {
  const kbs = await api<Kb[]>("/api/kbs");
  const options = [
    { value: "", label: "（移出到未分组）" },
    ...kbs.filter((k) => k.kb_id !== currentKbId)
      .map((k) => ({ value: k.kb_id, label: k.name })),
  ];
  const v = await formDialog({
    title: "移动到其他知识库",
    fields: [{
      key: "kb_id", label: "目标知识库", type: "select",
      value: "", options,
      note: "移到别的库后，本文档的方案覆盖保持不变。",
    }],
    submitLabel: "移动",
    onSubmit: async (values) => {
      await api(`/api/documents/${encodeURIComponent(docId)}`, {
        method: "PUT",
        body: { kb_id: String(values.kb_id ?? "") },
      });
    },
  });
  if (!v) return null;
  invalidateKbs();
  void loadKbs();
  const target = String(v.kb_id ?? "");
  toast(target ? "已移动" : "已移出到未分组");
  return target;
}

export async function editDocPlanDialog(docId: string): Promise<boolean> {
  const p = await api<DocPlan>(`/api/documents/${encodeURIComponent(docId)}/plan`);
  const e = p.effective;
  const v = await formDialog({
    title: "文档分块方案",
    note: `当前生效：${e.chunk_size} 字符 / 重叠 ${e.overlap_ratio}% · ${e.strategy}`
      + `（${p.own.custom ? "本文档自定义" : "继承知识库"}）`,
    fields: [
      { key: "inherit", label: "继承知识库默认", type: "switch", value: !p.own.custom },
      {
        key: "strategy", label: "分块策略", type: "select",
        value: e.strategy || "auto",
        options: CHUNK_STRATEGIES.map((s) => ({ value: s.value, label: s.label })),
      },
      {
        key: "chunk_size", label: "分块大小（字符）", type: "number",
        value: e.chunk_size, min: 64, max: 8192,
        note: "64–8192。技术手册 512、FAQ 256 是常见起点。",
      },
      {
        key: "overlap_ratio", label: "重叠比例（%）", type: "number",
        value: e.overlap_ratio, min: 0, max: 50,
      },
    ],
    submitLabel: "保存方案",
    onSubmit: async (values) => {
      const inherit = Boolean(values.inherit);
      // 0 / 空 = 恢复继承上一级（后端契约，见 PlanUpdateReq 的注释）
      await api(`/api/documents/${encodeURIComponent(docId)}/plan`, {
        method: "PUT",
        body: {
          chunk_size: inherit ? 0 : Number(values.chunk_size ?? 0),
          overlap_ratio: inherit ? 0 : Number(values.overlap_ratio ?? 0),
          strategy: inherit ? "" : String(values.strategy ?? "auto"),
        },
      });
    },
  });
  if (v) toast("方案已保存，点「重新切分」后生效");
  return !!v;
}

/** 重新解析（换引擎重读原文）——提交后把 job_id 交给调用方去盯终态 */
export async function reparseDoc(
  docId: string,
  onQueued?: (r: IngestResult) => void,
): Promise<boolean> {
  const v = await formDialog({
    title: "重新解析",
    note: "会重新读取原文并覆盖现有分块（手工新增的分块保留在内）。",
    fields: [{
      key: "engine", label: "解析引擎", type: "select", value: "auto",
      options: [
        { value: "auto", label: "auto（按文件类型自动选择）" },
        { value: "pymupdf4llm", label: "pymupdf4llm（PDF 默认）" },
        { value: "native", label: "native（纯文本）" },
        { value: "docling", label: "docling（重排版 / 扫描件）" },
        { value: "unstructured", label: "unstructured" },
      ],
      note: "扫描件选 docling；普通 PDF 用 pymupdf4llm 即可。",
    }],
    submitLabel: "重新解析",
    onSubmit: async (values) => {
      const r = await api<IngestResult>(
        `/api/documents/${encodeURIComponent(docId)}/reparse?wait=false`,
        { method: "POST", body: { engine: String(values.engine ?? "auto") } },
      );
      toast("已提交重新解析");
      // 顶栏队列立刻刷新（空闲期轮询 25s，不 poke 会让人觉得没提交上）
      pokeQueue();
      onQueued?.(r);
    },
  });
  return !!v;
}

export async function resplitDoc(
  docId: string,
  onQueued?: (r: IngestResult) => void,
): Promise<boolean> {
  const ok = await confirmDialog({
    title: "重新切分",
    body: "将按当前分块方案重新切分该文档并重新向量化。"
      + "手工新增的分块会保留，此操作不可撤销。",
    confirmLabel: "重新切分",
  });
  if (!ok) return false;
  try {
    const r = await api<IngestResult>(
      `/api/documents/${encodeURIComponent(docId)}/resplit?wait=false`, { method: "POST" });
    toast("已提交重新切分");
    pokeQueue();
    onQueued?.(r);
    return true;
  } catch (e) {
    toast(e instanceof Error ? e.message : String(e), "err");
    return false;
  }
}

/**
 * 批量删除文档：走 `DELETE /documents/batch` 一次加锁。
 *
 * 循环调单条端点在 50 篇时是 50 个请求，且中途失败就回答不了
 * "哪些已经删了"；批量端点会如实回报 skipped。
 */
export async function deleteDocsBatch(
  docs: Array<{ doc_id: string; title: string }>,
): Promise<boolean> {
  if (docs.length === 0) return false;
  try {
    const r = await api<{ deleted: number; skipped: number }>(
      "/api/documents/batch",
      { method: "DELETE", body: { doc_ids: docs.map((d) => d.doc_id) } },
    );
    toast(r.skipped > 0
      ? `已删除 ${r.deleted} 篇，${r.skipped} 篇已不存在`
      : `已删除 ${r.deleted} 篇`, r.skipped > 0 ? "err" : "ok");
    return true;
  } catch (e) {
    toast("批量删除失败：" + (e instanceof Error ? e.message : String(e)), "err");
    return false;
  }
}

export async function deleteDocDialog(doc: Document): Promise<boolean> {
  const ok = await confirmDialog({
    title: "删除文档",
    body: `将删除「${doc.title}」及其 ${doc.chunk_count ?? 0} 个分块，并从检索中移除。`
      + "此操作不可撤销。",
    confirmLabel: "删除",
    destructive: true,
  });
  if (!ok) return false;
  try {
    await api(`/api/documents/${encodeURIComponent(doc.doc_id)}`, { method: "DELETE" });
    toast(`已删除「${doc.title}」`);
    invalidateKbs();
    void loadKbs();
    return true;
  } catch (e) {
    toast(e instanceof Error ? e.message : String(e), "err");
    return false;
  }
}
