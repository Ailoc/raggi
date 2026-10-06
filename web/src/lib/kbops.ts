/**
 * 知识库级操作：新建 / 删除。
 *
 * 列表页、文档页、设置页都要用；三处各自实现过的文案与确认方式
 * （量化影响、要求输入名称）必须保持一致，否则破坏性操作的
 * 摩擦强度会随入口不同而变化。
 */
import { api } from "./api";
import { confirmDialog } from "../ui/dialog.svelte";
import { formDialog } from "../ui/form.svelte";
import { toast } from "../ui/toast.svelte";
import { invalidateKbs } from "./stores.svelte";

/** 新建知识库：只问名称。描述与分块方案属于「建库后再决定」的进阶参数。 */
export async function createKbDialog(): Promise<boolean> {
  return formDialog({
    title: "新建知识库",
    submitLabel: "创建",
    intro: "先起个名字。描述、分块方案这些进阶设置，建库后在「知识库设置」里随时可以改。",
    fields: [{ name: "name", label: "名称", required: true, placeholder: "例如：产品手册" }],
    onSubmit: async (v) => {
      const name = String(v.name).trim();
      await api("/api/kbs", { method: "POST", body: { name } });
      // 新库的计数是后端算的，本地这份列表必须作废
      invalidateKbs();
      toast(`已创建「${name}」`);
    },
  });
}

export interface KbRef {
  kb_id: string;
  name: string;
  doc_count?: number;
  chunk_count?: number;
}

/** 删除知识库（不可逆）。返回是否已删除。 */
export async function removeKbDialog(kb: KbRef): Promise<boolean> {
  const ok = await confirmDialog({
    title: `删除知识库「${kb.name}」`,
    body: `将删除 ${kb.doc_count ?? 0} 个文档、${kb.chunk_count ?? 0} 个分块，`
      + "以及这些文档的留档原文。此操作不可撤销。",
    confirmLabel: "删除",
    destructive: true,
    requireTyping: kb.name,
  });
  if (!ok) return false;
  try {
    await api(`/api/kbs/${encodeURIComponent(kb.kb_id)}`, { method: "DELETE" });
    invalidateKbs();
    toast("已删除");
    return true;
  } catch (e) {
    toast(e instanceof Error ? e.message : String(e), "err");
    return false;
  }
}
