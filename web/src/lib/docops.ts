/**
 * 文档级操作：重命名 / 移动 / 覆盖方案 / 重新解析 / 重新切分 / 删除。
 *
 * 文档列表页与文档详情页都要用这一套。抽到一处的原因：两边曾经
 * 各自实现，文案、是否走异步队列、失败提示很快就开始漂移。
 */
import { api } from "./api";
import { confirmDialog } from "../ui/dialog.svelte";
import { formDialog } from "../ui/form.svelte";
import { toast } from "../ui/toast.svelte";
import { invalidateKbs } from "./stores.svelte";
import type { DocPlan, IngestResult, Kb } from "./types";

const enc = encodeURIComponent;

/** 覆盖文档分块方案。返回是否保存成功（调用方据此刷新）。 */
export async function editDocPlanDialog(docId: string): Promise<boolean> {
  const p = await api<DocPlan>(`/api/documents/${enc(docId)}/plan`);
  const eff = p.effective;
  const src = eff.source === "doc" ? "本文档"
    : eff.source === "kb" ? "知识库默认" : "系统默认";
  return formDialog({
    title: "覆盖分块方案",
    submitLabel: "保存方案",
    intro: `当前生效：${eff.chunk_size} 字符，重叠 ${eff.overlap_ratio}%（来自${src}）。` +
      "文档级设置优先于知识库；保存后需点「重新切分」才会应用到已入库的分块。",
    fields: [
      { name: "inherit", label: "继承知识库默认", type: "switch", value: !p.own.custom },
      {
        name: "chunk_size", label: "分块大小（字符）", type: "number",
        min: 64, max: 8192,
        value: String(p.own.custom ? p.own.chunk_size : eff.chunk_size),
        disabledWhen: (v) => Boolean(v.inherit),
        note: "中文建议 300–600。过小丢失上下文，过大稀释向量。",
      },
      {
        name: "overlap_ratio", label: "重叠比例（%）", type: "number",
        min: 0, max: 50,
        value: String(p.own.custom ? p.own.overlap_ratio : eff.overlap_ratio),
        disabledWhen: (v) => Boolean(v.inherit),
      },
    ],
    onSubmit: async (v) => {
      const inherit = Boolean(v.inherit);
      await api(`/api/documents/${enc(docId)}/plan`, {
        method: "PUT",
        body: {
          chunk_size: inherit ? 0 : Number(v.chunk_size || 0),
          overlap_ratio: inherit ? 0 : Number(v.overlap_ratio || 0),
        },
      });
      toast(inherit ? "已恢复继承知识库默认" : "方案已保存，点「重新切分」应用");
    },
  });
}

/** 重命名文档。返回是否成功。 */
export async function renameDocDialog(docId: string, title: string): Promise<boolean> {
  return formDialog({
    title: "重命名文档",
    submitLabel: "保存",
    fields: [{ name: "title", label: "标题", value: title, required: true }],
    onSubmit: async (v) => {
      await api(`/api/documents/${enc(docId)}`, {
        method: "PUT", body: { title: String(v.title).trim() },
      });
      toast("已重命名");
    },
  });
}

/**
 * 把文档移动到另一个知识库（含「移出到未分组」）。
 *
 * 为什么需要：早前后端只接受 title，kb_id 被 Pydantic 静默忽略 ——
 * 请求返回 200 却什么都没发生，调用方无从察觉。文档入库后想换库
 * 只能删了重传，分块与手工编辑全部丢失。
 *
 * 后端会同步更新 documents.kb_id 与 chunks.kb_id 两张表，所以移动后
 * 按库检索仍能命中这篇文档。
 *
 * @param currentKbId 当前归属（用于在下拉里标出「当前」并排除自身）
 * @param onMoved 移动成功后回调，用于刷新面包屑与左侧树
 */
export async function moveDocDialog(
  docId: string,
  currentKbId: string,
  onMoved?: (newKbId: string) => void,
): Promise<boolean> {
  let kbs: Kb[] = [];
  try {
    kbs = await api<Kb[]>("/api/kbs");
  } catch (e) {
    toast(e instanceof Error ? e.message : String(e));
    return false;
  }
  // 只有「别的」库才是可选项：移到当前所在库是无意义的操作，
  // 放进下拉只会让人以为它能干什么。
  const targets = kbs.filter((k) => k.kb_id !== currentKbId);
  if (!targets.length) {
    toast("没有其它知识库可移动。先在「知识库」里新建一个。");
    return false;
  }
  return formDialog({
    title: "移动到知识库",
    submitLabel: "移动",
    intro: "移动只改归属，不重新解析也不重新切分；分块与手工编辑都会保留。",
    fields: [{
      name: "kb_id",
      label: "目标知识库",
      type: "select",
      value: targets[0].kb_id,
      options: [
        // 移出到未分组是真实需求（先批量导入再分类），因此始终提供
        { value: "", label: "未分组（移出知识库）" },
        ...targets.map((k) => ({ value: k.kb_id, label: k.name })),
      ],
    }],
    onSubmit: async (v) => {
      const target = String(v.kb_id ?? "");
      const r = await api<{ kb_id: string }>(`/api/documents/${enc(docId)}`, {
        method: "PUT", body: { kb_id: target },
      });
      // 归属变了，列表里的 doc_count/chunk_count 全部失效
      invalidateKbs();
      // 以服务端回传的归属为准：万一与请求不一致（如并发移动），
      // 界面要反映真实结果而不是我们以为的结果。
      toast(target ? `已移动到「${kbs.find((k) => k.kb_id === r.kb_id)?.name ?? "目标库"}」`
                   : "已移出到未分组");
      onMoved?.(r.kb_id ?? "");
    },
  });
}

/**
 * 重新解析（可换引擎）。走异步队列，提交后立刻返回。
 *
 * @param onQueued 拿到 job_id 时回调。调用方**必须**用它去盯任务：
 *   `wait=false` 意味着接口回来时流水线才刚启动，不订阅的话页面会一直
 *   停在旧状态，用户只能离开再回来——而文案当时写的却是"自动更新"。
 */
export async function reparseDoc(
  docId: string,
  onQueued?: (r: IngestResult) => void,
): Promise<boolean> {
  return formDialog({
    title: "重新解析",
    submitLabel: "开始解析",
    intro: "用留档原文重新解析，保留文档 ID、知识库归属与入库时间；"
      + "已有的手工分块会被保留。仅对「上传文件」入库的文档可用。",
    fields: [{
      name: "engine", label: "解析引擎", type: "select", value: "auto",
      options: [
        { value: "auto", label: "沿用当前配置" },
        { value: "pymupdf4llm", label: "pymupdf4llm（PDF 默认）" },
        { value: "native", label: "native（纯文本）" },
        { value: "docling", label: "docling（重排版 / 扫描件）" },
        { value: "unstructured", label: "unstructured" },
      ],
      note: "扫描件选 docling；普通 PDF 用 pymupdf4llm 即可。",
    }],
    onSubmit: async (v) => {
      const r = await api<IngestResult>(
        `/api/documents/${enc(docId)}/reparse?wait=false`, {
          method: "POST", body: { engine: String(v.engine || "auto") },
        });
      toast("已提交重新解析，处理完成后本页会自动刷新");
      onQueued?.(r);
    },
  });
}

/**
 * 重新切分：按当前生效方案重切并重新向量化（保留手工块）。
 * @param onQueued 见 reparseDoc 的同名参数。
 */
export async function resplitDoc(
  docId: string,
  onQueued?: (r: IngestResult) => void,
): Promise<boolean> {
  const ok = await confirmDialog({
    title: "重新切分",
    body: "将按当前分块方案重新切分该文档并重新向量化。手工新增的分块会保留，此操作不可撤销。",
    confirmLabel: "重新切分",
  });
  if (!ok) return false;
  try {
    const r = await api<IngestResult>(
      `/api/documents/${enc(docId)}/resplit?wait=false`, { method: "POST" });
    toast("已提交重新切分，处理完成后本页会自动刷新");
    onQueued?.(r);
    return true;
  } catch (e) {
    toast(e instanceof Error ? e.message : String(e), "err");
    return false;
  }
}

/** 删除文档（含全部分块与留档原文）。返回是否已删除。 */
export async function deleteDocDialog(
  docId: string, title: string, chunkCount: number,
): Promise<boolean> {
  const ok = await confirmDialog({
    title: `删除文档「${title}」`,
    body: `将删除它的 ${chunkCount} 个分块与留档原文，此操作不可撤销。`,
    confirmLabel: "删除",
    destructive: true,
  });
  if (!ok) return false;
  try {
    await api(`/api/documents/${enc(docId)}`, { method: "DELETE" });
    // 知识库列表带 doc_count/chunk_count，删完文档不失效就会一直显示旧计数
    invalidateKbs();
    toast("已删除");
    return true;
  } catch (e) {
    toast(e instanceof Error ? e.message : String(e), "err");
    return false;
  }
}
