// 对外 HTTP API 的说明文档（前端展示用）。
//
// 这份清单是**手写的**而非从 /openapi.json 生成的，原因有三：
// 1. FastAPI 生成的 schema 只有字段名，没有「这个接口该怎么用」；
// 2. 手写版本能写清副作用（会落盘、会删数据）、幂等性与踩坑点；
// 3. tests/test_apidoc.py 会把这里的每条 method+path 与后端真实路由
//    实际注册表比对——新增或删除后端接口而忘记同步本文档，测试即失败。
// 因此：**改动后端路由时必须同步本文件**。

/** HTTP 方法 */
export type Method = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";

export interface Param {
  name: string;
  in: "path" | "query" | "body" | "form";
  type: string;
  required?: boolean;
  desc: string;
  /** 用于生成 curl 示例的占位值（如 path 里的 {kb_id}） */
  example?: string;
}

/** 响应字段说明。示例里出现的键都应在此列出。 */
export interface Field {
  name: string;
  type: string;
  desc: string;
}

export interface Endpoint {
  method: Method;
  path: string;
  /** 一句话说明这个接口做什么 */
  summary: string;
  /** 副作用与注意事项：会写库 / 会删数据 / 会落盘 / 有幂等性等 */
  notes?: string[];
  params?: Param[];
  /** 请求体示例（JSON 会被格式化后渲染） */
  example?: unknown;
  /** 响应字段；非 JSON 响应（如 Markdown）用 raw 代替 */
  fields?: Field[];
  /** 非 JSON 响应的原始样例 */
  raw?: string;
  /** 响应 Content-Type，非 JSON 时必填 */
  contentType?: string;
}

export interface Group {
  id: string;
  title: string;
  desc: string;
  /** 该组的关键提示（幂等性、鉴权、副作用等），置于组首 */
  notes?: string[];
  endpoints: Endpoint[];
}

/** 通用请求头。所有 /api/v1/*（/api 为兼容别名）在配置 token 或签发密钥后都需要凭据。 */
export const AUTH_NOTE =
  "调用方需要提供凭据，放在请求头（两者等效）：\n" +
  "  Authorization: Bearer rg_xxxxxxxx_yyyy…\n" +
  "  X-API-Key: rg_xxxxxxxx_yyyy…\n\n" +
  "密钥在「设置 → 访问密钥」页签发，形如 rg_<公开段>_<随机段>，" +
  "明文只显示一次，服务端仅保存 sha256 哈希——丢失后无法找回，" +
  "只能吊销重签。\n\n" +
  "权限分两档：read 可检索与查看；write 额外可入库、编辑、删除与" +
  "修改配置。管理密钥的接口（/api/v1/keys）要求 write——否则一把只读" +
  "密钥就能签发新密钥把自己提权成写入，权限体系形同虚设。\n\n" +
  "兼容旧版：部署时设置的环境变量 RAG_TOKEN 仍可用" +
  "（Authorization: Bearer <token>），等价 write 全权限。" +
  "两者都未配置且系统内没有任何密钥时不鉴权——此时请尽快签发密钥。";

export const GROUPS: Group[] = [
  {
    id: "search",
    title: "检索",
    desc: "知识库的核心能力：按查询词召回分块，返回命中位置、分数与上下文窗口。" +
      "所有检索接口都会调用 embedding 服务，模型不可用时返回 503。",
    notes: [
      "**参数分三层**，调哪个旋钮影响什么是明确的：" +
        "召回层（candidate_k / nprobes / refine_factor / k_rrf / rerank）决定找出多少候选；" +
        "返回层（top_k / offset / score_threshold / group_by_doc）决定返回哪些；" +
        "展示层（window / include_context / snippet_chars / highlight）只影响响应体积与呈现。",
      "**分数方向统一**：score 一律「越大越相关」，score_kind 说明其量纲" +
        "（rrf / bm25 / cosine_similarity）。原始值仍保留在 scores 里——" +
        "其中 vector_distance 是余弦距离，方向相反（越小越近），不要混用。",
      "**翻页用 offset + top_k**：total_candidates 是过滤/去重后、翻页前的可用总数，" +
        "has_more 直接告诉还有没有下一页。",
      "mode 三选一：hybrid（向量+FTS 融合，默认）、vector（纯语义）、fts（纯关键词）。",
      "被「停用」的分块默认不参与检索；filters.include_disabled=true 才可见。",
      "**精排**：rerank.enabled=true 且 provider=api 时，召回结果会再经 " +
        "HTTP 精排（POST {base_url}/rerank，兼容 /v1/rerank 前缀）。" +
        "精排服务不可用时**自动降级**为按召回分排序，并在 degraded_reason 里说明原因" +
        "——不会让整次检索失败。此时 score_kind 会变成 \"rerank\"。",
    ],
    endpoints: [
      {
        method: "POST",
        path: "/api/v1/search",
        summary: "检索（推荐入口）。",
        notes: [
          "响应 degraded_reason 非空表示所选通道不可用、已自动降级，" +
            "结果仍可用但召回口径可能与预期不同（例如 FTS 索引尚未建立）。",
          "失败时按异常类型返回 503 / 400 / 500，detail 保留真实原因。",
        ],
        params: [
          { name: "q", in: "body", type: "string", required: true, desc: "查询词" },
          { name: "mode", in: "body", type: "string", desc: "hybrid / vector / fts" },
          { name: "top_k", in: "body", type: "int|null", desc: "返回条数（1-200），默认取服务端配置" },
          { name: "offset", in: "body", type: "int", desc: "跳过前 N 条，配合 top_k 翻页" },
          { name: "score_threshold", in: "body", type: "float|null", desc: "相关性下限，低于它的命中被丢弃" },
          { name: "group_by_doc", in: "body", type: "boolean", desc: "同一文档只保留最高分的一条" },
          { name: "candidate_k", in: "body", type: "int|null", desc: "候选池大小，越大召回越全越慢" },
          { name: "nprobes", in: "body", type: "int|null", desc: "向量索引探测的分区数" },
          { name: "refine_factor", in: "body", type: "int|null", desc: "量化索引的重排倍数" },
          { name: "k_rrf", in: "body", type: "int|null", desc: "混合检索 RRF 融合常数" },
          { name: "rerank", in: "body", type: "boolean|null", desc: "强制开启/关闭精排" },
          { name: "window", in: "body", type: "int|null", desc: "上下文窗口（前后各取 N 个分块）" },
          { name: "include_context", in: "body", type: "boolean", desc: "是否返回上下文文本" },
          { name: "snippet_chars", in: "body", type: "int|null", desc: "摘要长度（50-2000，纯文本部分）" },
          { name: "highlight", in: "body", type: "boolean", desc: "摘要中是否用 <mark> 标注命中词" },
          { name: "filters", in: "body", type: "object", desc: "kb_id / doc_ids / origin / mime / parser_engine / include_disabled" },
          { name: "kb_id", in: "body", type: "string|null", desc: "限定知识库（等价 filters.kb_id）" },
        ],
        example: {
          q: "逆变器 额定电压",
          mode: "hybrid",
          top_k: 5,
          offset: 0,
          group_by_doc: true,
          score_threshold: 0.02,
          window: 1,
          snippet_chars: 220,
          highlight: true,
          filters: { origin: "parsed", include_disabled: false },
        },
        fields: [
          { name: "query", type: "string", desc: "查询词回显" },
          { name: "took_ms", type: "number", desc: "检索耗时（毫秒）" },
          { name: "mode", type: "string", desc: "实际使用的检索通道" },
          { name: "score_kind", type: "string", desc: "score 的量纲：rrf / bm25 / cosine_similarity" },
          { name: "degraded_reason", type: "string|null", desc: "降级说明；正常为 null" },
          { name: "total_candidates", type: "number", desc: "过滤与去重后、翻页前的可用总数" },
          { name: "offset / top_k", type: "number", desc: "本次翻页参数回显" },
          { name: "has_more", type: "boolean", desc: "是否还有下一页" },
          { name: "results", type: "SearchHit[]", desc: "命中列表（含 score_kind / scores 原始分）" },
        ],
      },
      {
        method: "GET",
        path: "/api/v1/search",
        summary: "检索（便于 curl / 脚本调用的便捷形式）。",
        notes: ["参数全部走 query string，字段名与 POST 版一致；doc_ids 用逗号分隔。"],
        fields: [
          { name: "query", type: "string", desc: "查询词回显" },
          { name: "results", type: "SearchHit[]", desc: "命中列表，结构同 POST 版" },
        ],
      },
      {
        method: "POST",
        path: "/api/v1/search/report",
        summary: "导出 Markdown 检索报告。",
        notes: [
          "响应是 Markdown 文本而非 JSON，可直接存为 .md 文件。",
          "报告会自动关闭 highlight——`<mark>` 混进 Markdown 是噪声。",
          "请求体与 POST /api/v1/search 完全一致，便于「先调试再导出」。",
        ],
        contentType: "text/markdown",
        params: [
          { name: "q", in: "body", type: "string", required: true, desc: "查询词" },
          { name: "top_k", in: "body", type: "int|null", desc: "返回条数" },
          { name: "mode", in: "body", type: "string", desc: "hybrid / vector / fts" },
        ],
        example: { q: "逆变器", top_k: 5, group_by_doc: true },
        raw: "# 检索报告\n\n- 查询：逆变器\n- 时间：2026-10-02T12:23:58+00:00\n- 模式：hybrid\n- 耗时：40.5ms\n- 候选：50，命中：5\n- 分数口径：rrf（越大越相关）\n\n## 命中结果\n\n### 1. 逆变器技术手册\n…",
      },
      {
        method: "POST",
        path: "/api/v1/answer",
        summary: "基于检索结果生成问答（需开启 features.answer）。",
        notes: [
          "默认关闭（features.answer=false），未开启时返回 503。",
          "上下文取命中分块的**全文**（非 snippet），答案按 [n] 标注引用。",
          "会调用 LLM；未检索到内容时不调用模型，直接返回「（未检索到相关内容）」。",
        ],
        params: [
          { name: "q", in: "body", type: "string", required: true, desc: "问题" },
          { name: "top_k", in: "body", type: "int|null", desc: "召回条数" },
          { name: "kb_id", in: "body", type: "string|null", desc: "限定知识库" },
        ],
        example: { q: "逆变器的额定电压是多少？", top_k: 5 },
        fields: [
          { name: "answer", type: "string", desc: "生成的答案" },
          { name: "citations", type: "Citation[]", desc: "引用列表（对应答案中的 [n]）" },
          { name: "took_ms", type: "number", desc: "生成耗时（毫秒）" },
          { name: "degraded_reason", type: "string|null", desc: "检索降级说明" },
        ],
      },
      {
        method: "POST",
        path: "/api/v1/answer/stream",
        contentType: "text/event-stream",
        summary: "问答流式输出（SSE）。",
        notes: [
          "事件顺序固定：**sources → delta* → done**。" +
            "引用先于正文是关键设计——用户判断要不要继续读，取决于依据是否靠谱，" +
            "而依据在生成之前就已确定。",
          "`delta` 的 data 是 {text}，可能多次出现，需逐段追加。",
          "出错时推 `error` 事件（{message, hint}）而不是断连：" +
            "断连会让前端只剩一个永久的 loading，用户无从判断发生了什么。",
          "响应头带 X-Accel-Buffering: no——反向代理下不关缓冲，事件会被攒着一起发。",
          "参数与非流式端点完全一致。",
        ],
        params: [
          { name: "q", in: "body", type: "string", required: true, desc: "问题" },
          { name: "top_k", in: "body", type: "int|null", desc: "召回条数" },
          { name: "kb_id", in: "body", type: "string|null", desc: "限定知识库" },
        ],
        example: { q: "逆变器的额定电压是多少？", top_k: 5 },
        raw: "event: sources\ndata: {\"citations\": [{\"index\": 1, \"chunk_id\": \"c1\", \"title\": \"逆变器技术手册\", \"score\": 0.61}]}\n\nevent: delta\ndata: {\"text\": \"变频器额定电压为 \"}\n\nevent: delta\ndata: {\"text\": \"380V，允许波动 ±10%。[1]\"}\n\nevent: done\ndata: {\"took_ms\": 1180.4, \"mode\": \"hybrid\", \"degraded_reason\": null}\n",
      },
    ],
  },

  {
    id: "kbs",
    title: "知识库",
    desc: "知识库是文档的容器。每个分块在入库时就带上 kb_id，" +
      "因此按知识库检索既能覆盖文档分块，也能覆盖挂在库下的独立分块。",
    notes: [
      "文档归属存在 documents.meta.kb_id（JSON 字段）里，而非独立列——" +
        "这是零 schema 迁移的历史设计。",
      "删除知识库会级联删除其全部文档、分块与留档原文，不可撤销。",
    ],
    endpoints: [
      {
        method: "GET",
        path: "/api/v1/kbs",
        summary: "知识库列表（含文档数 / 分块数）。",
        notes: ["按更新时间倒序。doc_count 与 chunk_count 由 documents 表聚合得出。"],
        fields: [
          { name: "kb_id", type: "string", desc: "知识库 ID" },
          { name: "name", type: "string", desc: "名称" },
          { name: "description", type: "string", desc: "描述" },
          { name: "doc_count", type: "number", desc: "文档数" },
          { name: "chunk_count", type: "number", desc: "分块数" },
          { name: "chunk_size", type: "number", desc: "分块大小；0 表示继承全局" },
          { name: "overlap_ratio", type: "number", desc: "重叠百分比（0-50）" },
          { name: "plan_custom", type: "boolean", desc: "是否自定义了分块方案" },
          { name: "created_at / updated_at", type: "string", desc: "ISO 时间" },
        ],
      },
      {
        method: "POST",
        path: "/api/v1/kbs",
        summary: "创建知识库。",
        notes: ["只需名称；分块方案留空则继承全局设置。"],
        params: [
          { name: "name", in: "body", type: "string", required: true, desc: "名称，不能为空" },
          { name: "description", in: "body", type: "string", desc: "描述" },
          { name: "chunk_size", in: "body", type: "int|null", desc: "分块大小；null/0 = 继承全局（范围 64-8192）" },
          { name: "overlap_ratio", in: "body", type: "number|null", desc: "重叠百分比（0-50）" },
        ],
        example: { name: "设备手册", description: "变频器与伺服产品文档", chunk_size: 768, overlap_ratio: 12 },
        fields: [
          { name: "kb_id", type: "string", desc: "新知识库的 ID" },
          { name: "doc_count / chunk_count", type: "number", desc: "新库均为 0" },
        ],
      },
      {
        method: "GET",
        path: "/api/v1/kbs/{kb_id}",
        summary: "知识库详情。",
        params: [{ name: "kb_id", in: "path", type: "string", required: true, desc: "知识库 ID" }],
        fields: [
          { name: "kb_id", type: "string", desc: "知识库 ID" },
          { name: "doc_count / chunk_count", type: "number", desc: "统计值" },
          { name: "plan_custom", type: "boolean", desc: "是否自定义分块方案" },
        ],
      },
      {
        method: "PUT",
        path: "/api/v1/kbs/{kb_id}",
        summary: "更新知识库基本信息或分块方案。",
        notes: [
          "只传需要改的字段；chunk_size 传 0 可恢复继承全局。",
          "改方案只影响之后入库的文档，已有文档需调用 /resplit 才会重切。",
        ],
        params: [
          { name: "kb_id", in: "path", type: "string", required: true, desc: "知识库 ID" },
          { name: "name / description", in: "body", type: "string", desc: "基本信息" },
          { name: "chunk_size", in: "body", type: "int|null", desc: "null 不改；0 = 恢复继承" },
          { name: "overlap_ratio", in: "body", type: "number|null", desc: "重叠百分比" },
        ],
        example: { name: "设备手册", chunk_size: 1024, overlap_ratio: 15 },
        fields: [{ name: "kb_id", type: "string", desc: "知识库 ID" }],
      },
      {
        method: "DELETE",
        path: "/api/v1/kbs/{kb_id}",
        summary: "删除知识库（级联删除文档 / 分块 / 留档原文）。",
        notes: [
          "**不可撤销**。",
          "会同时清理挂在该库下的独立分块（doc_id 为空的分块）。",
        ],
        params: [{ name: "kb_id", in: "path", type: "string", required: true, desc: "知识库 ID" }],
        fields: [{ name: "ok", type: "boolean", desc: "固定为 true" }],
      },
      {
        method: "GET",
        path: "/api/v1/kbs/{kb_id}/plan",
        summary: "查询知识库的分块方案与继承链。",
        params: [{ name: "kb_id", in: "path", type: "string", required: true, desc: "知识库 ID" }],
        fields: [
          { name: "own", type: "object", desc: "本库自定义值（custom=false 表示继承）" },
          { name: "effective", type: "object", desc: "当前实际生效值，含 chunk_size / overlap_chars / source" },
          { name: "global", type: "object", desc: "全局默认值，供对比" },
        ],
      },
      {
        method: "PUT",
        path: "/api/v1/kbs/{kb_id}/plan",
        summary: "设置知识库的分块方案。",
        notes: ["chunk_size 传 0 或 null 恢复继承全局；不影响已入库文档。"],
        params: [
          { name: "kb_id", in: "path", type: "string", required: true, desc: "知识库 ID" },
          { name: "chunk_size", in: "body", type: "int|null", desc: "null/0 = 继承全局" },
          { name: "overlap_ratio", in: "body", type: "number|null", desc: "重叠百分比" },
        ],
        example: { chunk_size: 1024, overlap_ratio: 15 },
        fields: [
          { name: "own", type: "object", desc: "设置后的本库值" },
          { name: "effective", type: "object", desc: "生效值" },
        ],
      },
    ],
  },

  {
    id: "documents",
    title: "文档入库与管理",
    desc: "入库流水线：路由解析器 → 加载正文 → 切分 → 批量并发向量化 → 幂等写库 → 后台建索引。" +
      "文档正文（解析后的全文）会一并保存，因此文本粘贴与 URL 入库的文档也能「重新切分」。",
    notes: [
      "**幂等**：按内容 SHA-256 去重。重复入库同一内容返回 status=\"duplicate\" 与既有 doc_id，不会产生第二份。",
      "**失败不留脏数据**：重切分 / 重解析时先向量化、后删除旧分块；" +
        "向量化失败则旧分块原样保留，文档状态置 failed。",
      "上传默认上限 50MB（max_upload_mb 可调），超限返回 413。",
    ],
    endpoints: [
      {
        method: "POST",
        path: "/api/v1/documents",
        summary: "上传文件入库（multipart/form-data）。",
        notes: [
          "支持 PDF / Word / Markdown / 纯文本等，由解析引擎路由决定实际解析器。",
          "成功后原始文件会被留档到 files_dir/{doc_id}{ext}，「重新解析」依赖它。",
        ],
        params: [
          { name: "file", in: "form", type: "file", required: true, desc: "待入库文件" },
          { name: "title", in: "form", type: "string", desc: "标题；缺省用文件名" },
          { name: "kb_id", in: "form", type: "string", desc: "归属知识库" },
          { name: "engine", in: "form", type: "string", desc: "解析引擎：auto / docling / pymupdf4llm / unstructured / native" },
        ],
        fields: [
          { name: "doc_id", type: "string", desc: "文档 ID" },
          { name: "job_id", type: "string", desc: "任务 ID" },
          { name: "status", type: "string", desc: "ready / duplicate / failed" },
          { name: "engine", type: "string", desc: "实际使用的解析引擎" },
          { name: "chunk_count", type: "number", desc: "生成的分块数" },
        ],
      },
      {
        method: "POST",
        path: "/api/v1/documents/text",
        summary: "粘贴文本入库。",
        notes: ["适合短文本；无留档原文，因此不支持「重新解析」（可「重新切分」）。"],
        params: [
          { name: "text", in: "body", type: "string", required: true, desc: "正文内容" },
          { name: "title", in: "body", type: "string", desc: "标题" },
          { name: "kb_id", in: "body", type: "string|null", desc: "归属知识库" },
        ],
        example: { text: "逆变器额定电压 380V，允许波动 ±10%。", title: "参数摘录", kb_id: null },
        fields: [
          { name: "doc_id", type: "string", desc: "文档 ID" },
          { name: "status", type: "string", desc: "ready / duplicate" },
          { name: "chunk_count", type: "number", desc: "分块数" },
        ],
      },
      {
        method: "POST",
        path: "/api/v1/documents/batch",
        summary: "批量文本入库。",
        notes: ["逐条处理，单条失败不影响其余条目——失败项在 results 里带 error 字段。"],
        params: [
          { name: "items", in: "body", type: "TextIngestReq[]", required: true, desc: "待入库条目列表" },
        ],
        example: {
          items: [
            { text: "第一份文档内容", title: "文档一", kb_id: null },
            { text: "第二份文档内容", title: "文档二", kb_id: null },
          ],
        },
        fields: [
          { name: "total", type: "number", desc: "条目总数" },
          { name: "results", type: "IngestResult[]", desc: "逐条结果" },
        ],
      },
      {
        method: "POST",
        path: "/api/v1/documents/url",
        summary: "抓取网页并入库。",
        notes: [
          "仅允许 http/https；**拒绝指向内网或保留地址**（回环、私有网段、" +
            "链路本地与云元数据地址），以防 SSRF。",
          "自动抽取正文：剥离 script/style/nav 等非正文节点，标题取 <title>。",
          "页面超过 8MB 会中止抓取。",
        ],
        params: [
          { name: "url", in: "body", type: "string", required: true, desc: "网页地址" },
          { name: "kb_id", in: "body", type: "string|null", desc: "归属知识库" },
        ],
        example: { url: "https://example.com/manual", kb_id: null },
        fields: [
          { name: "doc_id", type: "string", desc: "文档 ID" },
          { name: "status", type: "string", desc: "ready / duplicate" },
          { name: "chunk_count", type: "number", desc: "分块数" },
        ],
      },
      {
        method: "GET",
        path: "/api/v1/documents",
        summary: "文档列表（支持多维过滤与分页）。",
        notes: [
          "limit 上限 500，默认 50。响应里的 total 是**过滤后**的总数，可用于分页。",
          "kb_id 过滤基于 documents.meta.kb_id。",
        ],
        params: [
          { name: "q", in: "query", type: "string", desc: "标题子串（不区分大小写）" },
          { name: "status", in: "query", type: "string", desc: "ready / failed" },
          { name: "parser_engine", in: "query", type: "string", desc: "按解析引擎过滤" },
          { name: "mime", in: "query", type: "string", desc: "按 MIME 过滤" },
          { name: "kb_id", in: "query", type: "string", desc: "按知识库过滤" },
          { name: "limit", in: "query", type: "int", desc: "默认 50，上限 500" },
          { name: "offset", in: "query", type: "int", desc: "偏移量，默认 0" },
        ],
        fields: [
          { name: "total", type: "number", desc: "过滤后的文档总数" },
          { name: "items", type: "Document[]", desc: "文档摘要列表" },
        ],
      },
      {
        method: "GET",
        path: "/api/v1/documents/{doc_id}",
        summary: "文档详情（含解析全文与分块索引）。",
        notes: ["响应包含 documents.text 全文——大文档体积可观，脚本批量拉取时注意。"],
        params: [{ name: "doc_id", in: "path", type: "string", required: true, desc: "文档 ID" }],
        fields: [
          { name: "title / mime / parser_engine", type: "string", desc: "基本属性" },
          { name: "content_hash", type: "string", desc: "正文 SHA-256，用于查重" },
          { name: "char_count / chunk_count", type: "number", desc: "字数与分块数" },
          { name: "status", type: "string", desc: "ready / failed" },
          { name: "text", type: "string", desc: "解析后的正文全文" },
          { name: "chunks", type: "DocChunkRef[]", desc: "分块索引（不含向量）" },
          { name: "created_at / updated_at", type: "string", desc: "ISO 时间" },
        ],
      },
      {
        method: "DELETE",
        path: "/api/v1/documents/{doc_id}",
        summary: "删除文档（级联删除其分块与留档原文）。",
        notes: ["**不可撤销**；同时清理 files_dir 下的留档文件。"],
        params: [{ name: "doc_id", in: "path", type: "string", required: true, desc: "文档 ID" }],
        fields: [{ name: "ok", type: "boolean", desc: "固定为 true" }],
      },
      {
        method: "DELETE",
        path: "/api/v1/documents/batch",
        summary: "批量删除文档（一次加锁，级联分块与留档原文）。",
        notes: [
          "与逐条调用 DELETE /documents/{id} 的差别是一次加锁 + 归档先取出：" +
            "删 50 篇时写锁窗口被切成 50 段，且归档文件名在删掉后就查不到。",
          "请求里已不存在的 ID 会被跳过，并在 skipped 里如实回报——" +
            "调用方据此提示「其中 N 篇已不存在」，而不是假装全删成功。",
        ],
        params: [{ name: "doc_ids", in: "body", type: "string[]", required: true, desc: "文档 ID 列表" }],
        example: { doc_ids: ["d1", "d2"] },
        fields: [
          { name: "deleted", type: "number", desc: "实际删除的篇数" },
          { name: "skipped", type: "number", desc: "请求里已不存在的篇数" },
        ],
      },
      {
        method: "POST",
        path: "/api/v1/documents/{doc_id}/reparse",
        summary: "用留档原文重新解析入库。",
        notes: [
          "需要 documents.meta.stored_file 存在（早期版本入库未留档的文档会返回 400）。",
          "保留 doc_id、kb_id 与 created_at（入库时间不会被改写）。",
          "失败时旧分块原样保留，文档状态置 failed。",
        ],
        params: [
          { name: "doc_id", in: "path", type: "string", required: true, desc: "文档 ID" },
          { name: "engine", in: "body", type: "string|null", desc: "解析引擎；auto 表示沿用当前配置" },
        ],
        example: { engine: "native" },
        fields: [
          { name: "doc_id", type: "string", desc: "文档 ID" },
          { name: "status", type: "string", desc: "ready" },
          { name: "chunk_count", type: "number", desc: "重解析后的分块数" },
        ],
      },
      {
        method: "POST",
        path: "/api/v1/documents/{doc_id}/resplit",
        summary: "按当前生效的分块方案重新切分（不重新解析原文）。",
        notes: [
          "手工新增的分块（origin=manual）会在重切后保留，响应里的 kept_manual 是保留数。",
          "粘贴文本 / URL 入库的文档（无留档原文）也能重切。",
          "向量化失败时旧分块不会被删除（先向量化、后删旧写新）。",
        ],
        params: [{ name: "doc_id", in: "path", type: "string", required: true, desc: "文档 ID" }],
        fields: [
          { name: "doc_id", type: "string", desc: "文档 ID" },
          { name: "chunk_count", type: "number", desc: "重切后的分块数" },
          { name: "kept_manual", type: "number", desc: "保留的手工分块数" },
        ],
      },
      {
        method: "GET",
        path: "/api/v1/documents/{doc_id}/plan",
        summary: "查询单个文档的分块方案与快照。",
        notes: [
          "snapshot 是该文档**实际入库时**用的参数；改设置后它不变，" +
            "用于解释「为什么这个块是 1024 而方案现在写的是 512」。",
        ],
        params: [{ name: "doc_id", in: "path", type: "string", required: true, desc: "文档 ID" }],
        fields: [
          { name: "own", type: "object", desc: "文档级自定义值" },
          { name: "effective", type: "object", desc: "当前生效值" },
          { name: "snapshot", type: "object|null", desc: "入库时实际使用的参数" },
        ],
      },
      {
        method: "PUT",
        path: "/api/v1/documents/{doc_id}/plan",
        summary: "设置文档级分块方案（优先级高于知识库与全局）。",
        notes: ["chunk_size 传 null 或 0 可清除覆盖、恢复继承。改完需 /resplit 才生效。"],
        params: [
          { name: "doc_id", in: "path", type: "string", required: true, desc: "文档 ID" },
          { name: "chunk_size", in: "body", type: "int|null", desc: "null/0 = 恢复继承" },
          { name: "overlap_ratio", in: "body", type: "number|null", desc: "重叠百分比" },
        ],
        example: { chunk_size: 512, overlap_ratio: 10 },
        fields: [
          { name: "own", type: "object", desc: "设置后的文档级值" },
          { name: "effective", type: "object", desc: "生效值" },
        ],
      },
      {
        method: "PUT",
        path: "/api/v1/documents/{doc_id}",
        summary: "更新文档的可编辑元信息（标题）。",
        notes: ["此前标题完全不可改——粘贴文本的标题取首行前 60 字，写错就永久错了。"],
        params: [
          { name: "doc_id", in: "path", type: "string", required: true, desc: "文档 ID" },
          { name: "title", in: "body", type: "string", desc: "新标题（不能为空）" },
        ],
        example: { title: "会议纪要 2026-01" },
        fields: [
          { name: "doc_id", type: "string", desc: "文档 ID" },
          { name: "title", type: "string", desc: "更新后的标题" },
        ],
      },
      {
        method: "GET",
        path: "/api/v1/documents/plans",
        summary: "批量取文档的方案摘要（列表页用，避免 N+1 请求）。",
        notes: [
          "items 是以 doc_id 为键的对象，缺失的文档不会出现在结果里。",
          "与 /api/v1/documents 使用同一套分页参数。",
        ],
        params: [
          { name: "kb_id", in: "query", type: "string", desc: "按知识库过滤" },
          { name: "limit", in: "query", type: "int", desc: "默认 200，上限 500" },
          { name: "offset", in: "query", type: "int", desc: "偏移量" },
        ],
        fields: [
          { name: "items", type: "Record<doc_id, DocPlan>", desc: "各文档的方案信息" },
        ],
      },
      {
        method: "GET",
        path: "/api/v1/documents/{doc_id}/file",
        summary: "提供留档原文的原始字节（浏览器内联预览用）。",
        notes: [
          "**两种鉴权二选一**：常规请求头，或查询串签名（`exp` + `sig`）。" +
            "后者是必需的——`<iframe>` / `<img>` / `<embed>` 无法携带 " +
            "`Authorization` 头，浏览器内嵌预览只能走签名链接。",
          "签名 URL 由文档详情接口返回的 `file_url` 给出，默认 10 分钟有效，" +
            "且与 doc_id 绑定（拿 A 的签名读不到 B）。",
          "只有**上传文件**入库的文档才有留档原文；粘贴文本 / URL 入库的" +
            "文档返回 404——它们本来就没有原始文件。",
          "响应带 `Accept-Ranges: bytes`，支持浏览器分段加载大 PDF。",
        ],
        params: [
          { name: "doc_id", in: "path", type: "string", required: true, desc: "文档 ID" },
          { name: "exp", in: "query", type: "int", desc: "签名过期时间（Unix 秒）" },
          { name: "sig", in: "query", type: "string", desc: "HMAC 签名" },
          { name: "download", in: "query", type: "boolean", desc: "true 则强制下载而非内联预览" },
        ],
        contentType: "按文档 mime 返回（application/pdf、image/*、text/* 等）",
        fields: [
          { name: "—", type: "binary", desc: "原始文件字节" },
        ],
      },
    ],
  },

  {
    id: "chunks",
    title: "分块",
    desc: "分块是检索的最小单位。每块都带 kb_id、doc_id、ordinal 与字符偏移，" +
      "检索命中的就是它。",
    notes: [
      "分块方案是三级继承：**文档覆盖 > 知识库 > 全局**。" +
        "入库时会把实际生效的方案快照写进文档 meta，用于解释历史分块。",
      "编辑分块会重新向量化并标记 fts_stale，直到 FTS 索引重建。",
      "解析产生的分块（origin=parsed）默认不可直接编辑，需显式 force=true；" +
        "编辑会保留 original_text 以便对比回滚。",
    ],
    endpoints: [
      {
        method: "GET",
        path: "/api/v1/chunks",
        summary: "分块列表（按文档 / 知识库查询）。",
        notes: [
          "doc_id 与 kb_id 互斥：同时给时以 doc_id 为准。",
          "only_standalone=true 用于「某库下的独立分块」——过滤在服务端完成，" +
            "不会因文档分块占满前 N 条而漏掉独立分块。",
          "q 是**分页前**的内容子串过滤（大小写不敏感），所以 total 是过滤后的块数；" +
            "口径与 GET /documents 的标题过滤一致。",
          "按 ordinal 稳定排序，配合 limit/offset 分页不会重复或漏项。",
        ],
        params: [
          { name: "doc_id", in: "query", type: "string", desc: "按文档过滤" },
          { name: "kb_id", in: "query", type: "string", desc: "按知识库过滤" },
          { name: "only_standalone", in: "query", type: "boolean", desc: "只要独立分块（doc_id 为空）" },
          { name: "q", in: "query", type: "string", desc: "分块内容子串，分页前过滤；total 为命中块数" },
          { name: "limit", in: "query", type: "int", desc: "默认 100，上限 500" },
          { name: "offset", in: "query", type: "int", desc: "偏移量" },
        ],
        fields: [
          { name: "items", type: "Chunk[]", desc: "分块列表（不含向量）" },
          { name: "total", type: "number", desc: "过滤后的总数" },
          { name: "limit / offset", type: "number", desc: "本次请求的分页参数回显" },
        ],
      },
      {
        method: "GET",
        path: "/api/v1/chunks/{chunk_id}",
        summary: "分块详情。",
        params: [{ name: "chunk_id", in: "path", type: "string", required: true, desc: "分块 ID" }],
        fields: [
          { name: "text / text_seg", type: "string", desc: "原文与分词后文本（jieba 预分词，FTS 索引用）" },
          { name: "char_start / char_end", type: "number", desc: "在文档全文中的偏移" },
          { name: "offset_valid", type: "boolean", desc: "偏移是否仍有效（改写后失效）" },
          { name: "original_text", type: "string|null", desc: "首次编辑前的原文" },
          { name: "vector", type: "number[]", desc: "向量" },
        ],
      },
      {
        method: "POST",
        path: "/api/v1/chunks",
        summary: "新增分块。",
        notes: [
          "给了 doc_id 则归入该文档；**doc_id 必须存在**，否则 404（否则会产生孤儿分块）。",
          "不传 doc_id 则为挂在知识库下的「独立分块」，适合术语表、口径说明。",
          "新增后会立即触发 FTS 重建（小库）。",
        ],
        params: [
          { name: "text", in: "body", type: "string", required: true, desc: "分块内容" },
          { name: "doc_id", in: "body", type: "string|null", desc: "归属文档；留空则为独立分块" },
          { name: "kb_id", in: "body", type: "string|null", desc: "归属知识库" },
        ],
        example: { text: "本项目统称「逆变器」为变频器。", doc_id: null, kb_id: null },
        fields: [{ name: "chunk_id", type: "string", desc: "新分块的 ID" }],
      },
      {
        method: "PATCH",
        path: "/api/v1/chunks/{chunk_id}",
        summary: "编辑分块原文（重新向量化）。",
        notes: [
          "origin=parsed 的分块必须传 force=true，否则 403。",
          "首次编辑会保存 original_text；长度变化时 offset_valid 置 false，" +
            "此时检索页会降级为整篇展示而不做偏移高亮。",
        ],
        params: [
          { name: "chunk_id", in: "path", type: "string", required: true, desc: "分块 ID" },
          { name: "text", in: "body", type: "string", required: true, desc: "新内容" },
          { name: "force", in: "body", type: "boolean", desc: "编辑解析产生的分块时必须为 true" },
        ],
        example: { text: "更新后的分块内容", force: true },
        fields: [
          { name: "chunk_id", type: "string", desc: "分块 ID" },
          { name: "edited", type: "boolean", desc: "固定为 true" },
          { name: "embed_model", type: "string", desc: "当前 embedding 模型" },
          { name: "fts_stale", type: "number", desc: "全库待重建 FTS 的行数" },
        ],
      },
      {
        method: "PATCH",
        path: "/api/v1/chunks",
        summary: "批量编辑分块（一次读取、一次向量化、一次落地）。",
        notes: ["任一 chunk_id 不存在则整体失败（404）；解析产生的分块需逐条带 force=true。"],
        params: [
          { name: "edits", in: "body", type: "ChunkEditItem[]", required: true, desc: "编辑列表" },
        ],
        example: {
          edits: [
            { chunk_id: "7de47047-22f2-4b70-86c0-7bf0691259d6", text: "新内容 A", force: true },
            { chunk_id: "8ef5f1b8-33a3-4e81-97d1-6c1702e8a4f7", text: "新内容 B", force: true },
          ],
        },
        fields: [
          { name: "edited", type: "number", desc: "成功编辑的条数" },
          { name: "fts_stale", type: "number", desc: "全库待重建 FTS 的行数" },
        ],
      },
      {
        method: "PATCH",
        path: "/api/v1/chunks/{chunk_id}/enabled",
        summary: "停用 / 启用分块。",
        notes: [
          "停用只是**退出检索**、数据保留可恢复——比删除更适合「暂时排除某段内容」。",
          "检索时默认排除 enabled=false 的分块。",
        ],
        params: [
          { name: "chunk_id", in: "path", type: "string", required: true, desc: "分块 ID" },
          { name: "enabled", in: "body", type: "boolean", required: true, desc: "true 启用，false 停用" },
        ],
        example: { enabled: false },
        fields: [
          { name: "chunk_id", type: "string", desc: "分块 ID" },
          { name: "enabled", type: "boolean", desc: "设置后的状态" },
        ],
      },
      {
        method: "PATCH",
        path: "/api/v1/chunks/batch-enabled",
        summary: "批量停用 / 启用分块（一次写入）。",
        notes: [
          "与 PATCH /chunks（批量改内容、需重新向量化）是两件事：" +
            "这里只翻 enabled 标志，不触发 embedding。",
          "前端原先在客户端循环调用单条端点：200 次请求、无原子性，" +
            "中途失败会让「哪些块被停用了」变得无法回答；本端点一次完成。",
          "任一 chunk_id 不存在即整体 404，不做部分成功。",
        ],
        params: [
          { name: "chunk_ids", in: "body", type: "string[]", required: true, desc: "分块 ID 列表" },
          { name: "enabled", in: "body", type: "boolean", required: true, desc: "true 启用，false 停用" },
        ],
        example: { chunk_ids: ["c1", "c2"], enabled: false },
        fields: [
          { name: "updated", type: "number", desc: "实际改动的条数" },
          { name: "enabled", type: "boolean", desc: "设置后的状态" },
        ],
      },
      {
        method: "DELETE",
        path: "/api/v1/chunks/{chunk_id}",
        summary: "删除分块。",
        notes: ["**不可撤销**。删除后会回写所属文档的 chunk_count。"],
        params: [{ name: "chunk_id", in: "path", type: "string", required: true, desc: "分块 ID" }],
        fields: [{ name: "ok", type: "boolean", desc: "固定为 true" }],
      },
    ],
  },

  {
    id: "models",
    title: "模型配置",
    desc: "Embedding / LLM / Rerank 三类模型的配置读取、热切换与连通性测试。",
    notes: [
      "**PUT 默认落盘**（persist=true），写入 data/config.toml，重启后仍然生效。" +
        "只想临时热切换请显式传 persist=false。",
      "api_key 回显为 \"***\"，原样回传会被忽略，不会误覆盖真实密钥。",
      "修改 embed.dim 是**危险操作**：现有 chunks 表的向量列宽度不会变，" +
        "改完所有入库与分块编辑都会失败，且 POST /api/v1/reindex 无法修复——" +
        "只有把维度改回去，或换新的数据目录重新入库。",
    ],
    endpoints: [
      {
        method: "GET",
        path: "/api/v1/models",
        summary: "读取当前生效的模型配置。",
        notes: ["api_key 以 \"***\" 掩码返回。"],
        fields: [
          { name: "embed", type: "EmbedConfig", desc: "provider / model / base_url / api_key / dim / batch / concurrency / normalize / device" },
          { name: "llm", type: "LLMConfig", desc: "provider / model / base_url / api_key / temperature / max_tokens" },
          { name: "rerank", type: "RerankConfig", desc: "enabled / provider / model / base_url / api_key / top_n / timeout" },
          { name: "retrieve", type: "RetrieveConfig", desc: "检索默认值（mode / top_k / candidate_k / window / nprobes / k_rrf / refine_factor 等），供前端显示「留空 = 用多少」" },
        ],
      },
      {
        method: "PUT",
        path: "/api/v1/models",
        summary: "更新模型配置（热切换，可选落盘）。",
        notes: [
          "未知字段会被忽略；只提交要改的部分即可。",
          "响应的 warnings 列出本次变更的风险（如维度变化），应如实展示给用户。",
        ],
        params: [
          { name: "embed / llm / rerank", in: "body", type: "object|null", desc: "要覆盖的配置片段" },
          { name: "persist", in: "query", type: "boolean", desc: "是否写入 config.toml，默认 true" },
        ],
        example: {
          embed: { provider: "ollama", model: "bge-m3", base_url: "http://127.0.0.1:11434", dim: 1024 },
        },
        fields: [
          { name: "ok", type: "boolean", desc: "固定为 true" },
          { name: "applied", type: "string[]", desc: "实际更新的配置段" },
          { name: "warnings", type: "string[]", desc: "变更风险提示；无风险时为空数组" },
          { name: "persisted", type: "boolean", desc: "是否已落盘" },
        ],
      },
      {
        method: "POST",
        path: "/api/v1/models/test",
        summary: "测试模型连通性。",
        notes: [
          "kind 可选 all / embedding / rerank / llm。",
          "**单类失败不影响其余**：每个 key 独立给出 {ok, error}，不会整体 500。",
        ],
        params: [
          { name: "kind", in: "body", type: "string", desc: "all / embedding / rerank / llm，默认 all" },
        ],
        example: { kind: "all" },
        fields: [
          { name: "embedding", type: "object", desc: "{ok, ...} embedding 连通性结果" },
          { name: "rerank", type: "object", desc: "rerank 连通性结果" },
          { name: "llm", type: "object", desc: "llm 连通性结果" },
        ],
      },
      {
        method: "POST",
        path: "/api/v1/embed",
        summary: "文本向量化（复用检索的嵌入能力，便于外部工具调用）。",
        notes: ["最多处理 1000 条文本；返回完整向量数组，注意响应体积。"],
        params: [
          { name: "texts", in: "body", type: "string[]", required: true, desc: "待向量化文本，最多 1000 条" },
        ],
        example: { texts: ["第一段文本", "第二段文本"] },
        fields: [
          { name: "model", type: "string", desc: "embedding 模型名" },
          { name: "dim", type: "number", desc: "向量维度" },
          { name: "count", type: "number", desc: "实际向量数量" },
          { name: "vectors", type: "number[][]", desc: "向量数组" },
        ],
      },
    ],
  },

  {
    id: "keys",
    title: "访问密钥",
    desc: "对外调用凭据的签发与吊销。密钥明文只在创建响应里返回一次，" +
      "服务端仅保存 sha256 哈希。",
    notes: [
      "**签发与吊销需要 write 权限**：否则一把 read 密钥就能签发新密钥" +
        "把自己提权成 write，权限体系等于作废。",
      "**列举（GET）不要求 write**：read 密钥也能查看密钥列表。" +
        "否则用户签发第一把密钥后，浏览器立刻失去查看密钥页的权限——" +
        "而它手里只有刚创建的那把。列举拿不到明文，不构成提权。",
      "**引导例外**：系统内一把密钥都没有时，本组接口允许无凭据访问，" +
        "以便签发第一把；一旦有密钥即恢复鉴权。",
      "列表响应不含任何哈希或明文——列举接口不该有能力还原凭据。",
      "吊销是幂等的：重复调用同一 key_id 都返回 200，行保留以便审计。",
    ],
    endpoints: [
      {
        method: "GET",
        path: "/api/v1/keys",
        summary: "列出全部 API 密钥（含已吊销 / 已过期）。",
        notes: ["按创建时间倒序。响应不含哈希与明文。"],
        fields: [
          { name: "items", type: "ApiKey[]", desc: "密钥列表（不含哈希）" },
          { name: "total", type: "number", desc: "密钥总数" },
          { name: "legacy_token_enabled", type: "boolean", desc: "服务端是否启用了旧版静态 token" },
        ],
      },
      {
        method: "POST",
        path: "/api/v1/keys",
        summary: "签发一把新密钥。",
        notes: [
          "**明文只在这次响应里出现一次**，请立即保存；丢失后只能吊销重签。",
          "scope 缺省为 read——签发即最小权限，需要写能力要显式指定。",
          "expires_in_days 为 null 时永不过期。",
        ],
        params: [
          { name: "name", in: "body", type: "string", required: true, desc: "名称，用于区分调用方" },
          { name: "scope", in: "body", type: "string", desc: "read（默认）/ write" },
          { name: "expires_in_days", in: "body", type: "int|null", desc: "有效期天数；null = 永不过期" },
          { name: "note", in: "body", type: "string|null", desc: "备注" },
        ],
        example: { name: "CI 流水线", scope: "read", expires_in_days: 90, note: "自动化检索" },
        fields: [
          { name: "key", type: "string", desc: "**明文密钥，仅此一次返回**" },
          { name: "key_id", type: "string", desc: "密钥 ID（吊销时用）" },
          { name: "prefix", type: "string", desc: "公开段，如 rg_ab12cd34" },
          { name: "scope", type: "string", desc: "权限档位" },
          { name: "state", type: "string", desc: "active" },
          { name: "warning", type: "string", desc: "保存提醒" },
        ],
      },
      {
        method: "DELETE",
        path: "/api/v1/keys/{key_id}",
        summary: "吊销密钥。",
        notes: ["吊销后使用该密钥的调用方立即收到 401。行保留以便审计，重复调用幂等。"],
        params: [{ name: "key_id", in: "path", type: "string", required: true, desc: "密钥 ID" }],
        fields: [
          { name: "ok", type: "boolean", desc: "固定为 true" },
          { name: "key_id", type: "string", desc: "被吊销的密钥 ID" },
          { name: "state", type: "string", desc: "revoked" },
        ],
      },
    ],
  },

  {
    id: "jobs",
    title: "入库任务",
    desc: "入库走队列执行：解析、切分、向量化都是重操作，排队后并发受控、" +
      "进度可查，也不必让 HTTP 请求一直挂着等大文件处理完。",
    notes: [
      "**两种用法**：入库接口带 `wait=true`（默认）阻塞等结果，行为与没有队列时一致；" +
        "带 `wait=false` 立即返回 `{job_id, status:\"queued\"}`，再用本组接口轮询进度。",
      "**阶段流转**：queued → parse/fetch → load → split → embed → write → done / failed。",
      "**队列有上限**（默认 32）：满了入库接口返回 503，快速失败好过无限堆积。",
      "并发 worker 数由 `ingest_workers` 配置（默认 2）——入库是重操作，" +
        "并发过高只会互相争抢 LanceDB 写锁与 embedding 服务。",
      "任务记录保留 7 天后自动清理（在提交时顺带执行，无需定时器）。",
    ],
    endpoints: [
      {
        method: "GET",
        path: "/api/v1/jobs",
        summary: "最近的任务列表（按开始时间倒序）。",
        notes: ["active_only=true 只返回未结束的任务，可用于「正在入库」指示。"],
        params: [
          { name: "limit", in: "query", type: "int", desc: "条数上限，默认 50" },
          { name: "active_only", in: "query", type: "boolean", desc: "只要未结束的任务" },
        ],
        fields: [
          { name: "items", type: "Job[]", desc: "任务列表" },
          { name: "total", type: "number", desc: "返回条数" },
          { name: "queue", type: "object", desc: "队列状态：workers / pending / max_pending" },
        ],
      },
      {
        method: "GET",
        path: "/api/v1/jobs/{job_id}",
        summary: "单个任务状态。",
        notes: [
          "`terminal=true` 表示已到终态（done / failed / cancelled），前端可停止轮询。",
          "cancelling 是过渡态：已请求取消，任务会在下一个阶段边界退出。",
        ],
        params: [
          { name: "job_id", in: "path", type: "string", required: true, desc: "任务 ID" },
        ],
        fields: [
          { name: "stage", type: "string", desc: "queued / parse / load / split / embed / write / cancelling / done / failed / cancelled" },
          { name: "progress", type: "number", desc: "0-1" },
          { name: "total", type: "number", desc: "分块总数（切分后才有）" },
          { name: "doc_id", type: "string", desc: "产出的文档 ID" },
          { name: "error", type: "string|null", desc: "失败或取消原因" },
          { name: "terminal", type: "boolean", desc: "是否已结束" },
        ],
      },
      {
        method: "DELETE",
        path: "/api/v1/jobs/{job_id}",
        summary: "取消一个入库任务。",
        notes: [
          "**能力边界**：排队中的任务是真取消（从队列撤销，永不执行）；" +
            "运行中的只能**协作式取消**——解析（PyMuPDF）与向量化（HTTP）" +
            "都不响应中断，已发出的调用必须跑完，任务在下一个阶段边界退出。",
          "outcome 如实区分处置方式，客户端据此给不同文案，不要一律显示" +
            "「已取消」——那会让用户以为已经停下、实际还在写库。",
          "取消不是失败：任务行记为 cancelled，文档状态保持原样" +
            "（重解析时旧分块仍在，不受任何影响）。",
          "对已结束的任务返回 outcome=finished（幂等，不报错）。",
        ],
        params: [
          { name: "job_id", in: "path", type: "string", required: true, desc: "任务 ID" },
        ],
        fields: [
          { name: "outcome", type: "string", desc: "cancelled（真取消）/ running（在停止中）/ finished / unknown" },
          { name: "stage", type: "string", desc: "取消后的当前阶段" },
          { name: "message", type: "string", desc: "给用户看的说明（已按 outcome 组织）" },
        ],
      },
    ],
  },

  {
    id: "system",
    title: "系统运维",
    desc: "健康检查、容量统计、索引重建、数据对账与版本回滚。",
    notes: [
      "健康检查的 status 为 degraded 有两类原因，看 `checks_failed` 区分：" +
        "一是 count_mismatch / orphan_chunks / dim_mismatch / " +
        "embedding_model_mismatch 真的查出了问题；二是某项检查**根本没跑成**，" +
        "此时「无法确认」也报 degraded —— 检查失败绝不翻译成绿灯" +
        "（改前 `except Exception: embed_model_mismatch = False` 就是这个形状）。",
      "「未知」用哨兵值表达：`fts_stale_count = -1`、`index_state.*.unindexed_rows = -1`" +
        "都表示没查到，不是 0。",
      "索引在后台维护（去抖合并），入库响应不等待索引重建，" +
        "刚入库的数据可能要等片刻才能被 FTS 通道检索到。",
    ],
    endpoints: [
      {
        method: "GET",
        path: "/api/v1/health",
        summary: "健康检查与一致性对账（只读）。",
        fields: [
          { name: "status", type: "string", desc: "ok / degraded" },
          { name: "doc_count / chunk_count", type: "number", desc: "实际行数" },
          { name: "count_mismatch", type: "object[]", desc: "chunk_count 与实际不符的文档 {doc_id, stated, actual}" },
          { name: "orphan_chunks", type: "number", desc: "doc_id 在 documents 中不存在的分块数" },
          { name: "standalone_chunks", type: "number", desc: "独立分块数（doc_id 为空）" },
          { name: "fts_stale_count", type: "number", desc: "待重建 FTS 的行数；-1 = 这项没查成" },
          { name: "checks_failed", type: "string[]", desc: "根本没跑成的检查名（空数组=所有项都查过）；非空时 degraded 的含义是「无法确认」" },
          { name: "embedding_dim / stored_embedding_dim", type: "number", desc: "配置的维度与表中实际维度" },
          { name: "dim_mismatch", type: "boolean", desc: "维度是否不一致（不一致会导致写入失败）" },
          { name: "embedding_model_mismatch", type: "boolean", desc: "表内 embed_model 是否与当前配置不一致" },
          { name: "lancedb_versions", type: "number", desc: "chunks 表版本数" },
          { name: "index_state", type: "object", desc: "vector / text_seg 的索引状态与未索引行数" },
        ],
      },
      {
        method: "GET",
        path: "/api/v1/stats",
        summary: "容量 / 索引 / 版本 / 磁盘统计。",
        fields: [
          { name: "doc_count / chunk_count / job_count", type: "number", desc: "各表行数" },
          { name: "data_dir_bytes / lancedb_dir_bytes", type: "number", desc: "目录占用字节" },
          { name: "disk_free_bytes", type: "number", desc: "剩余磁盘字节；获取失败为 -1" },
          { name: "indexes", type: "object", desc: "chunks / documents 的索引清单" },
          { name: "versions", type: "number", desc: "chunks 表版本数" },
        ],
      },
      {
        method: "POST",
        path: "/api/v1/reconcile",
        summary: "按实际分块数回写 documents.chunk_count。",
        notes: ["用于修复健康检查报出的 count_mismatch；返回修复条数。"],
        fields: [
          { name: "ok", type: "boolean", desc: "固定为 true" },
          { name: "fixed", type: "number", desc: "被修正的文档数" },
        ],
      },
      {
        method: "POST",
        path: "/api/v1/reindex",
        summary: "重建向量 / FTS / 标量索引并 optimize。",
        notes: [
          "耗时操作。",
          "**不能修复向量维度变更**——索引重建不改变表 schema。",
        ],
        fields: [{ name: "ok", type: "boolean", desc: "固定为 true" }],
      },
      {
        method: "GET",
        path: "/api/v1/versions",
        summary: "列出表的历史版本（供回滚使用）。",
        params: [
          { name: "table", in: "query", type: "string", desc: "表名，默认 chunks" },
        ],
        fields: [
          { name: "table", type: "string", desc: "表名" },
          { name: "versions", type: "object[]", desc: "版本列表" },
        ],
      },
      {
        method: "POST",
        path: "/api/v1/rollback",
        summary: "把指定表回滚到历史版本。",
        notes: [
          "**危险**：LanceDB 按表管理版本，回滚 chunks 后 documents 表不会跟着回滚，" +
            "必须随后调用 /api/v1/health 对账、必要时执行 /api/v1/reconcile。",
        ],
        params: [
          { name: "version", in: "body", type: "int", required: true, desc: "目标版本号" },
          { name: "table", in: "body", type: "string", desc: "表名，默认 chunks" },
        ],
        example: { version: 12, table: "chunks" },
        fields: [
          { name: "ok", type: "boolean", desc: "固定为 true" },
          { name: "rolled_back_to", type: "number", desc: "回滚到的版本号" },
          { name: "note", type: "string", desc: "提示：对账提醒" },
        ],
      },
    ],
  },
];

/** 全部端点（扁平），供统计与测试使用。 */
export function allEndpoints(): { group: Group; ep: Endpoint }[] {
  return GROUPS.flatMap((g) => g.endpoints.map((ep) => ({ group: g, ep })));
}

/** 生成 curl 示例：path 占位统一留作 <name>，请求体用 example 序列化。 */
export function curlFor(ep: Endpoint, base: string): string {
  const url = base + ep.path.replace(/\{(\w+)\}/g, "<$1>");
  const head = `curl -X ${ep.method} '${url}'`;

  // 文件上传走 multipart：给 JSON 示例会让读者照抄后必然失败
  const fileParam = ep.params?.find((p) => p.name === "file");
  if (fileParam) {
    const parts = [
      `curl -X ${ep.method} '${url}'`,
      `  -F 'file=@/path/to/doc.pdf'`,
      ...(ep.params ?? [])
        .filter((p) => p.in === "form" && p.name !== "file")
        .map((p) => `  -F '${p.name}=<${p.name}>'`),
    ];
    return parts.join(" \\\n");
  }

  if (ep.example === undefined) return head;
  return `${head} \\\n  -H 'Content-Type: application/json' \\\n  -d '${JSON.stringify(ep.example)}'`;
}