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
export const MODE_OPTIONS = [
  { value: "hybrid", label: "混合", hint: "向量 + 全文，按 RRF 融合" },
  { value: "vector", label: "向量", hint: "只看语义相近的证据" },
  { value: "fts", label: "全文", hint: "只看字面命中的证据" },
] as const;

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

/**
 * 参数默认值。
 *
 * 检索词与已生效参数都不再写入 URL：进入检索页永远是空态输入台，
 * 刷新/分享不会复现上一次查询（产品决策，见 DESIGN §7.3）。
 */
export const DEFAULTS: Record<string, string> = {
  kb_id: "", mode: "hybrid", top_k: "8", group_by_doc: "false",
  origin: "", parser_engine: "", mime: "", include_disabled: "false",
  score_threshold: "", window: "1", snippet_chars: "", highlight: "true",
  candidate_k: "", nprobes: "", refine_factor: "", k_rrf: "", rerank: "",
};

/** 全套默认参数（本地状态初始值）。 */
export function defaultParams(): Record<string, string> {
  const out: Record<string, string> = {};
  for (const p of PARAMS) out[p.key] = DEFAULTS[p.key] ?? "";
  return out;
}

const FILTER_KEYS = new Set(["origin", "parser_engine", "mime", "include_disabled"]);

/**
 * 知识库多选值的序列化：`params` 仍是 `Record<string,string>`，所以
 * 多个 kb_id 用逗号连接。ID 由服务端生成（UUID），不含逗号；这里
 * 仍按空段过滤，避免旧数据里的尾逗号变成一次空 ID 查询。
 */
export function kbIdsFromValue(value: string): string[] {
  return value.split(",").map((s) => s.trim()).filter(Boolean);
}

export function kbValueFromIds(ids: string[]): string {
  return [...new Set(ids.filter(Boolean))].join(",");
}

/** 过滤条件必须放进 `filters`：SearchReq 顶层没有这些字段，放外面会被静默忽略。 */
export function filtersFromParams(values: Record<string, string>): Record<string, unknown> {
  const filters: Record<string, unknown> = {};
  const kbIds = kbIdsFromValue(values.kb_id ?? "");
  if (kbIds.length > 0) filters.kb_ids = kbIds;
  if (values.origin) filters.origin = values.origin;
  if (values.parser_engine) filters.parser_engine = values.parser_engine;
  if (values.mime) filters.mime = values.mime;
  if (values.include_disabled === "true") filters.include_disabled = true;
  return filters;
}

/** 参数表 → 请求体（对话与检索共用，避免两套参数解释）。 */
export function paramsToBody(
  q: string,
  values: Record<string, string>,
): Record<string, unknown> {
  const body: Record<string, unknown> = {
    q,
    mode: values.mode || DEFAULTS.mode,
  };
  const topK = Number(values.top_k || DEFAULTS.top_k);
  // 上限与问答接口对齐：返回条数直接决定证据卡数量（每张都是玻璃面 +
  // 若干图标），曾经可以填到 200，一次检索就把页面撑成上百块卡片。
  body.top_k = Number.isFinite(topK)
    ? Math.min(50, Math.max(1, Math.round(topK)))
    : 8;
  for (const p of PARAMS) {
    if (p.key === "mode" || p.key === "top_k" || p.key === "kb_id"
      || FILTER_KEYS.has(p.key)) continue;
    const raw = values[p.key] ?? "";
    if (raw === "") continue;
    body[p.key] = p.kind === "num" ? Number(raw)
      : p.kind === "bool" ? raw === "true"
      // 这两项在界面里是下拉框（字符串），API 契约却是 bool：
      // 直接透传 "false" 只能靠 pydantic 的宽松转换，离开 HTTP 就错。
      : p.key === "group_by_doc" || p.key === "rerank" ? raw === "true"
      : raw;
  }
  const filters = filtersFromParams(values);
  if (Object.keys(filters).length > 0) body.filters = filters;
  return body;
}
