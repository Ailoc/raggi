export type ParamKind = "str" | "num" | "bool";
export type ParamGroup = "base" | "filter" | "advanced";

export interface ParamSpec {
  key: string;
  id: string;
  kind: ParamKind;
  label: string;
  group: ParamGroup;
}

/**
 * 检索参数的唯一规格表。
 *
 * URL 序列化、表单回填、控件渲染、以及测试里的「声明类型 vs 控件类型」
 * 交叉校验，都读这一份定义。
 *
 * 旧实现里这张表只管 URL 同步，控件是另写一遍的 HTML——两边一旦
 * 不一致（例如 kind 声明为 bool 但实际渲染成 <select>），TypeScript
 * 查不出（都是 HTMLElement），只会表现为"这个参数永远提交默认值"。
 * 那个 bug 真实发生过（group_by_doc）。
 */
export const PARAMS: ParamSpec[] = [
  { key: "kb_id",            id: "pKb",        kind: "str",  label: "知识库",     group: "base" },
  { key: "mode",             id: "pMode",      kind: "str",  label: "模式",       group: "base" },
  { key: "top_k",            id: "pTopK",      kind: "num",  label: "返回条数",   group: "base" },
  { key: "group_by_doc",     id: "pGroup",     kind: "str",  label: "按文档合并", group: "base" },
  { key: "origin",           id: "pOrigin",    kind: "str",  label: "来源",       group: "filter" },
  { key: "parser_engine",    id: "pEngine",    kind: "str",  label: "解析引擎",   group: "filter" },
  { key: "mime",             id: "pMime",      kind: "str",  label: "MIME",       group: "filter" },
  { key: "include_disabled", id: "pDisabled",  kind: "bool", label: "含已停用",   group: "filter" },
  { key: "score_threshold",  id: "pThreshold", kind: "num",  label: "分数阈值",   group: "advanced" },
  { key: "window",           id: "pWindow",    kind: "num",  label: "上下文窗口", group: "advanced" },
  { key: "snippet_chars",    id: "pSnippet",   kind: "num",  label: "片段长度",   group: "advanced" },
  { key: "highlight",        id: "pHighlight", kind: "bool", label: "高亮",       group: "advanced" },
  { key: "candidate_k",      id: "pCandidate", kind: "num",  label: "候选数",     group: "advanced" },
  { key: "nprobes",          id: "pNprobes",   kind: "num",  label: "nprobes",    group: "advanced" },
  { key: "refine_factor",    id: "pRefine",    kind: "num",  label: "refine",     group: "advanced" },
  { key: "k_rrf",            id: "pKrrf",      kind: "num",  label: "RRF K",      group: "advanced" },
  { key: "rerank",           id: "pRerank",    kind: "str",  label: "重排",       group: "advanced" },
];

/** 默认值不入 URL，保持分享链接精简。 */
export const DEFAULTS: Record<string, string> = {
  kb_id: "", mode: "hybrid", top_k: "8", group_by_doc: "false",
  origin: "", parser_engine: "", mime: "", include_disabled: "false",
  score_threshold: "", window: "1", snippet_chars: "", highlight: "true",
  candidate_k: "", nprobes: "", refine_factor: "", k_rrf: "", rerank: "",
};

/** 从查询串构造请求体：只带上真正被设置过的参数。 */
export function toBody(q: string, sp: URLSearchParams): Record<string, unknown> {
  const body: Record<string, unknown> = { q, mode: sp.get("mode") ?? DEFAULTS.mode };
  const topK = Number(sp.get("top_k") ?? DEFAULTS.top_k);
  body.top_k = Number.isFinite(topK) && topK > 0 ? topK : 8;
  for (const p of PARAMS) {
    if (p.key === "mode" || p.key === "top_k") continue;
    const raw = sp.get(p.key);
    if (raw === null || raw === "") continue;
    body[p.key] = p.kind === "num" ? Number(raw)
      : p.kind === "bool" ? raw === "true"
      : raw;
  }
  return body;
}

/**
 * 把"应当生效的参数"整体序列化成检索地址。
 *
 * 取代旧的 `withParam(sp, key, value)`——那是"改一个参数就立刻改写 URL"
 * 的写法，而改写 URL 并不等于重新检索，于是出现过"参数面板是新值、
 * 结果集是旧值"的脱节。现在参数改动先进暂存区，只有提交时经这一个
 * 函数生成地址，URL 因此始终描述"产生了当前这批结果的参数"。
 *
 * 等于默认值的项不入 URL，保持分享链接精简。
 */
export function searchUrl(
  text: string,
  values: Record<string, string | undefined>,
): string {
  const sp = new URLSearchParams();
  const q = (values.q ?? text).trim();
  if (q) sp.set("q", q);
  for (const p of PARAMS) {
    const v = values[p.key] ?? "";
    if (v === "" || v === (DEFAULTS[p.key] ?? "")) continue;
    sp.set(p.key, v);
  }
  const s = sp.toString();
  return "/search" + (s ? "?" + s : "");
}
