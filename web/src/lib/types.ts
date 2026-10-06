// API 响应/请求类型：与 rag/api/*.py 的实际返回结构一一对应。
// 只声明前端真正消费的字段，其余（向量、text_seg 等）刻意不建模。

// ---- 知识库 ----

export interface Kb {
  kb_id: string;
  name: string;
  description: string;
  doc_count: number;
  chunk_count: number;
  /** 分块大小（字符）；0 = 继承全局 */
  chunk_size: number;
  /** 重叠比例（%） */
  overlap_ratio: number;
  /** chunk_size > 0 即为自定义方案，否则继承全局 */
  plan_custom: boolean;
  created_at: string;
  updated_at: string;
}

// ---- 分块方案（知识库 / 文档共用）----

export interface SplitPayload {
  chunk_size: number;
  overlap_ratio: number;
  overlap_chars: number;
  /** global | kb | doc —— 生效值的来源层级 */
  source: string;
}

/** 入库时写入的方案快照（meta.chunking.snapshot）。 */
export interface PlanSnapshot {
  chunk_size: number;
  overlap_ratio: number;
  source: string;
}

export interface PlanOwn {
  chunk_size: number;
  overlap_ratio: number;
  custom: boolean;
}

export interface KbPlan {
  scope: "kb";
  kb_id: string;
  own: PlanOwn;
  effective: SplitPayload;
  global: SplitPayload;
}

export interface DocPlan {
  scope: "doc";
  doc_id: string;
  kb_id: string;
  own: PlanOwn;
  effective: SplitPayload;
  /** 实际入库时用的参数；改设置后不变，用于解释历史分块 */
  snapshot: PlanSnapshot | null;
}

// ---- 文档 ----

export interface DocChunkRef {
  chunk_id: string;
  ordinal: number;
  heading_path: string | null;
  page: number | null;
  text: string;
}

export interface Document {
  doc_id: string;
  title: string;
  source_uri: string | null;
  mime: string;
  parser_engine: string;
  content_hash: string;
  char_count: number;
  chunk_count: number;
  status: string;
  error: string | null;
  meta: string;
  /** 知识库归属（真实列） */
  kb_id: string;
  /** 留档原文文件名；粘贴文本 / URL 入库的文档为 null */
  stored_file: string | null;
  /** 是否有可预览的留档原文 */
  has_file?: boolean;
  /** 原文预览地址（启用鉴权时为短期签名 URL） */
  file_url?: string | null;
  /** GET /api/documents/{id} 附带的解析后全文 */
  text?: string;
  /** GET /api/documents/{id} 附带的分块索引（不含向量） */
  chunks?: DocChunkRef[];
  created_at: string;
  updated_at: string;
}

export interface DocumentList {
  total: number;
  items: Document[];
}

// ---- 分块 ----

export interface Chunk {
  chunk_id: string;
  doc_id: string;
  kb_id: string;
  ordinal: number;
  origin: "parsed" | "manual";
  edited: boolean;
  /** false = 仅退出检索，数据保留 */
  enabled: boolean;
  token_count: number;
  heading_path: string | null;
  text: string;
  /** 所在页码（PDF 按页解析时才有），用于原文跳页 */
  page?: number | null;
  char_start?: number;
  char_end?: number;
  /** false 表示编辑后偏移已失效，不应据此定位原文 */
  offset_valid?: boolean;
  original_text?: string | null;
  created_at?: string;
  /**
   * 版本标记：编辑时回传它做乐观并发校验。若期间别人改过，
   * 服务端返回 409 而不是静默覆盖（见 PATCH /chunks/{id}）。
   */
  updated_at?: string;
}

export interface ChunkList {
  items: Chunk[];
  total: number;
  limit: number;
  offset: number;
}

// ---- 检索 ----

export interface SearchScores {
  /** 余弦距离（越小越近）。仅向量通道有值 */
  vector_distance: number;
  /** BM25 分（越大越相关） */
  fts: number;
  /** RRF 融合分（越大越相关） */
  rrf: number;
}

export interface SearchHit {
  chunk_id: string;
  /** 空串 = 独立分块（不属于任何文档） */
  doc_id: string;
  kb_id: string;
  /** doc_id 为空时后端返回空串 */
  title: string;
  heading_path: string;
  page: number | null;
  ordinal: number;
  char_start: number;
  char_end: number;
  offset_valid: boolean;
  /** 相关性，**越大越相关**（已归一，方向与原字段无关） */
  score: number;
  /** score 的量纲：rrf / bm25 / cosine_similarity */
  score_kind: string;
  scores: SearchScores;
  /** 检索片段：纯文本 + 高亮区间。**不是 HTML**，用 Snippet 组件渲染。 */
  snippet: Snippet;
  parser_engine: string;
  edited: boolean;
  enabled: boolean;
  origin: string;
  /** ordinal → 文本；键是字符串化的序号。include_context=false 时为空 */
  context: Record<string, string>;
}

export interface SearchResponse {
  /** 查询词回显 */
  query: string;
  took_ms: number;
  mode: string;
  /** score 的量纲说明 */
  score_kind: string;
  /** 非空表示所选通道不可用、已自动降级 */
  degraded_reason: string | null;
  /** 过滤/去重后、翻页前的可用总数 */
  total_candidates: number;
  offset: number;
  top_k: number;
  /** 是否还有下一页 */
  has_more: boolean;
  results: SearchHit[];
}

// ---- 问答 ----

/**
 * 检索片段：**纯文本 + 高亮区间**，不是 HTML。
 *
 * 早前这里是后端拼好的 `<mark>` 字符串，前端必须 `{@html}` 注入，
 * 安全性依赖后端每次都记得转义（入库文本来自 URL 抓取，漏一次就是
 * 存储型 XSS）。现在文本由 Svelte 原生插值自动转义。
 */
export interface Snippet {
  text: string;
  /** [start, end) 区间，相对 text，已排序去重 */
  marks: number[][];
}

export interface Citation {
  index: number;
  chunk_id: string;
  doc_id: string;
  title: string;
  heading_path: string;
  page: number | null;
  ordinal: number;
  score: number;
  snippet: Snippet;
}

export interface AnswerReq {
  q: string;
  top_k?: number | null;
  mode?: string | null;
  kb_id?: string | null;
  window?: number | null;
  filters?: Record<string, unknown>;
}

/** SSE 的 done 事件载荷（rag/retrieval/answer.py 的事件序列）。 */
export interface AnswerStreamDone {
  took_ms: number;
  mode: string;
  degraded_reason: string | null;
}

// ---- 任务（入库队列）--------------------------------------------------

export interface QueueStats {
  workers: number;
  pending: number;
  max_pending: number;
}

export interface Job {
  job_id: string;
  doc_id: string;
  /** 入队时就写入的归属，早于 doc_id 生成 */
  kb_id: string;
  stage: string;
  progress: number;
  total: number;
  parser_engine: string;
  error: string | null;
  started_at: string;
  ended_at: string | null;
  terminal: boolean;
}

export interface JobList {
  items: Job[];
  total: number;
  queue: QueueStats;
}

export interface CancelOutcome {
  ok: boolean;
  job_id: string;
  /** cancelled=真取消 / running=在停止中 / finished=已结束 / unknown=不存在 */
  outcome: string;
  stage: string;
  message: string;
}

// ---- 入库 / 重切分 ----

export interface IngestResult {
  doc_id: string;
  job_id: string;
  status: "ready" | "duplicate" | "failed";
  engine: string;
  chunk_count: number;
  error?: string;
}

export interface ResplitResult {
  doc_id: string;
  chunk_count: number;
  /** 重切分后保留的手工分块数 */
  kept_manual: number;
}

// ---- 模型配置 ----

export interface EmbedConfig {
  provider: string;
  model: string;
  base_url: string;
  api_key: string;
  dim: number;
}

export interface LlmConfig {
  provider: string;
  model: string;
  base_url: string;
  api_key: string;
}

export interface RerankConfig {
  enabled: boolean;
  provider: string;
  model: string;
  base_url: string;
  api_key: string;
}

export interface ModelsConfig {
  embed: EmbedConfig;
  llm: LlmConfig;
  rerank: RerankConfig;
}

// ---- 访问密钥 ----

export type KeyScope = "read" | "write";
/** active | revoked | expired */
export type KeyState = "active" | "revoked" | "expired";

export interface ApiKey {
  key_id: string;
  name: string;
  /** 密钥公开段（rg_xxxxxxxx），用于人工辨认，非完整密钥 */
  prefix: string;
  scope: KeyScope;
  note: string;
  created_at: string;
  last_used_at: string | null;
  expires_at: string | null;
  revoked_at: string | null;
  /** 派生状态 */
  state: KeyState;
  expired: boolean;
  active: boolean;
}

export interface ApiKeyList {
  items: ApiKey[];
  total: number;
  /** 是否启用了旧版静态 token */
  legacy_token_enabled: boolean;
}

/** 创建密钥的响应：明文只在这一次出现 */
export interface ApiKeyCreated extends ApiKey {
  key: string;
  warning: string;
}

// ---- 健康 / 统计 ----

export interface CountMismatch {
  doc_id: string;
  stated: number;
  actual: number;
}

export interface IndexState {
  indexed: boolean;
  unindexed_rows: number;
}

export interface Health {
  status: "ok" | "degraded";
  now: string;
  doc_count: number;
  chunk_count: number;
  count_mismatch: CountMismatch[];
  orphan_chunks: number;
  /** doc_id 为空、独立挂在知识库下的分块数 */
  standalone_chunks: number;
  fts_stale_count: number;
  /**
   * 压根没查成的检查名（如 "fts_stale" / "embedding_model"）。
   * 空数组 = 每一项都真的查过；非空时 `status: degraded` 的含义是
   * 「无法确认」而不是「查到问题了」——两者必须在界面上分开显示。
   * 配套：`fts_stale_count` 为 -1 表示「未知」，不是「0 条待重建」。
   */
  checks_failed?: string[];
  embedding_dim: number;
  stored_embedding_dim: number | null;
  dim_mismatch: boolean;
  embedding_model: string;
  embedding_model_mismatch: boolean;
  lancedb_versions: number;
  index_state: Record<string, IndexState>;
  /** 磁盘与索引细节，随版本演进 */
  [k: string]: unknown;
}