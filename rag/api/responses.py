"""响应模型：对外契约的唯一事实来源。

为什么单独成文件：请求模型（schemas.py）早已类型化，但响应此前全部是
裸 dict——OpenAPI 里每个 200 响应的 schema 都是 `{}`，客户端无法生成
类型，`/docs` 的 Response 区也空白；响应悄悄改了字段没有任何机制能发现。

行模型（Document / Chunk / Kb / ApiKey / Job）设置 `extra="allow"`：
LanceDB 的行结构随表列演进，未知列**透传**而不是被静默丢弃——契约里
已知字段是文档化的，新增列不会让 API 悄悄少返回数据（这正是本项目
踩过的"防御式解析漏一处就静默拿空值"的同类风险，只是方向相反）。

共享的 `responses=` 字典定义在这里，供各路由在 APIRouter 上声明
统一的错误契约（401/403/404/503 的结构与含义）。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# ---- 通用 ---------------------------------------------------------------

class ErrorOut(BaseModel):
    """所有非 2xx 响应的统一结构。"""
    detail: str


class OkOut(BaseModel):
    ok: bool = True


# 错误契约：挂到 APIRouter(responses=...) 上，按资源组合。
AUTH_RESPONSES: dict[int | str, dict] = {
    401: {"model": ErrorOut, "description": "未认证：未配置任何凭据或凭据无效"},
    403: {"model": ErrorOut, "description": "权限不足：密钥作用域不够或需要 force"},
}
NOT_FOUND_RESPONSE: dict[int | str, dict] = {
    404: {"model": ErrorOut, "description": "对象不存在"},
}
UNAVAILABLE_RESPONSE: dict[int | str, dict] = {
    503: {"model": ErrorOut, "description": "依赖不可用（模型服务未就绪）或入库队列已满"},
}


def error_responses(*, not_found: bool = False, unavailable: bool = False) -> dict:
    """按需组合错误契约，避免给每个端点手写一遍。"""
    out: dict[int | str, dict] = dict(AUTH_RESPONSES)
    if not_found:
        out.update(NOT_FOUND_RESPONSE)
    if unavailable:
        out.update(UNAVAILABLE_RESPONSE)
    return out


# ---- 入库 -----------------------------------------------------------------

class IngestOut(BaseModel):
    """入库 / 重切分结果。

    wait=false 时 status="queued" 且只有 job_id/doc_id；
    wait=true 时带 engine/chunk_count。重切分额外带 kept_manual。
    """
    doc_id: str = ""
    job_id: str = ""
    status: str
    engine: str = ""
    chunk_count: int = 0
    error: str | None = None
    kept_manual: int | None = None
    # true 表示这是同 Idempotency-Key 的重试回放，请求并未真正执行
    idempotent_replay: bool | None = None


class BatchItemOut(BaseModel):
    """批量入库的单条结果（失败项带 error，不影响其余条目）。"""
    title: str = ""
    status: str = ""
    error: str | None = None
    doc_id: str = ""
    job_id: str = ""
    engine: str = ""
    chunk_count: int = 0


class BatchIngestOut(BaseModel):
    total: int
    results: list[BatchItemOut]


# ---- 文档 -----------------------------------------------------------------

class DocChunkRefOut(BaseModel):
    """文档详情附带的分块索引（不含向量）。"""
    chunk_id: str
    ordinal: int = 0
    heading_path: str | None = None
    page: int | None = None
    text: str = ""


class DocumentOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    doc_id: str
    title: str
    source_uri: str | None = None
    mime: str = ""
    parser_engine: str = ""
    content_hash: str = ""
    char_count: int = 0
    chunk_count: int = 0
    status: str = ""
    error: str | None = None
    kb_id: str = ""
    stored_file: str | None = None
    meta: str = "{}"
    created_at: str = ""
    updated_at: str = ""
    # GET /api/documents/{id} 的附加字段
    text: str | None = None
    chunks: list[DocChunkRefOut] | None = None
    has_file: bool | None = None
    file_url: str | None = None


class DocumentListOut(BaseModel):
    total: int
    items: list[DocumentOut]


class DocumentUpdateOut(BaseModel):
    doc_id: str
    title: str
    # 实际生效的归属（空串 = 未分组）。回传它是为了让调用方能确认
    # 移动真的发生了，而不是只能假设 200 就等于办妥。
    kb_id: str = ""


class DocDeleteBatchOut(BaseModel):
    """批量删除结果；skipped 是请求里已不存在的 ID 数。"""
    deleted: int
    skipped: int


# ---- 分块 -----------------------------------------------------------------

class ChunkOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    chunk_id: str
    doc_id: str = ""
    kb_id: str = ""
    ordinal: int = 0
    origin: str = "parsed"
    edited: bool = False
    enabled: bool = True
    token_count: int = 0
    heading_path: str | None = None
    text: str = ""
    page: int | None = None
    char_start: int = 0
    char_end: int = 0
    offset_valid: bool = True
    original_text: str | None = None
    embed_model: str = ""
    created_at: str = ""
    updated_at: str = ""


class ChunkListOut(BaseModel):
    items: list[ChunkOut]
    total: int
    limit: int
    offset: int


class ChunkCreatedOut(BaseModel):
    chunk_id: str


class ChunkEditedOut(BaseModel):
    chunk_id: str
    edited: bool
    embed_model: str
    fts_stale: int = 0


class ChunkBatchEditedOut(BaseModel):
    edited: int
    embed_model: str | None = None
    fts_stale: int | None = None


class ChunkEnabledOut(BaseModel):
    chunk_id: str
    enabled: bool


class ChunkBatchEnabledOut(BaseModel):
    """批量启停结果：updated 是实际改动的条数。"""
    updated: int
    enabled: bool


# ---- 知识库 ---------------------------------------------------------------

class KbOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    kb_id: str
    name: str
    description: str = ""
    doc_count: int = 0
    chunk_count: int = 0
    chunk_size: int = 0
    overlap_ratio: float = 0.0
    plan_custom: bool = False
    created_at: str = ""
    updated_at: str = ""


class KbDeleteOut(BaseModel):
    ok: bool = True
    kb_id: str


# ---- 分块方案 -------------------------------------------------------------

class SplitOut(BaseModel):
    """生效方案的展开形态（含派生出的 overlap_chars）。"""
    chunk_size: int
    overlap_ratio: float
    overlap_chars: int
    source: str


class PlanOwnOut(BaseModel):
    """本层自己持有的值（custom=false 表示与上一层一致）。"""
    chunk_size: int
    overlap_ratio: float
    custom: bool


class SnapshotOut(BaseModel):
    """入库时的方案快照（用于解释历史分块、判断"需重切"）。"""
    chunk_size: int
    overlap_ratio: float
    source: str


class KbPlanOut(BaseModel):
    scope: str = "kb"
    kb_id: str
    own: PlanOwnOut
    effective: SplitOut
    # global 是 Python 关键字，字段名加下划线、对外仍序列化为 "global"
    global_: SplitOut = Field(alias="global")


class DocPlanOut(BaseModel):
    scope: str = "doc"
    doc_id: str
    kb_id: str
    own: PlanOwnOut
    effective: SplitOut
    snapshot: SnapshotOut | None = None


class DocPlansOut(BaseModel):
    items: dict[str, DocPlanOut]


# ---- 检索 -----------------------------------------------------------------

class SearchScoresOut(BaseModel):
    """原始通道分数：vector_distance 越小越近，fts/rrf 越大越相关。"""
    vector_distance: float = 0.0
    fts: float = 0.0
    rrf: float = 0.0


class SnippetOut(BaseModel):
    """检索片段：纯文本 + 高亮区间。

    **不输出 HTML**：前端用 Svelte 原生插值渲染 `{text}`，并按 marks
    自己套 `<mark>`，因此不需要 `{@html}`，XSS 面从根上消除。
    """
    text: str = ""
    # [start, end) 区间列表，相对 text，已排序去重
    marks: list[list[int]] = Field(default_factory=list)


class SearchHitOut(BaseModel):
    chunk_id: str
    doc_id: str = ""
    kb_id: str = ""
    title: str = ""
    heading_path: str = ""
    page: int | None = None
    ordinal: int = 0
    char_start: int = 0
    char_end: int = 0
    offset_valid: bool = True
    score: float
    score_kind: str
    scores: SearchScoresOut
    # snippet 是**结构化**的：text 为纯文本（前端自动转义），marks 为
    # 命中区间（相对 text）。早前这里是拼好的 <mark> HTML，迫使前端用
    # {@html} 注入，安全性全靠后端每次都记得转义——入库文本来自 URL
    # 抓取，漏一次就是存储型 XSS。改成结构化后安全由语言保证。
    snippet: SnippetOut = Field(default_factory=SnippetOut)
    parser_engine: str = ""
    edited: bool = False
    enabled: bool = True
    origin: str = "parsed"
    # 键在 Python 侧是整数 ordinal（JSON 序列化后变为字符串键，
    # 与前端 types.ts 的 Record<string, string> 一致）
    context: dict[int, str] = Field(default_factory=dict)


class SearchOut(BaseModel):
    query: str
    took_ms: float
    mode: str
    score_kind: str
    degraded_reason: str | None = None
    total_candidates: int
    offset: int
    top_k: int
    has_more: bool
    results: list[SearchHitOut]


class CitationOut(BaseModel):
    index: int
    chunk_id: str
    doc_id: str = ""
    title: str = ""
    heading_path: str = ""
    page: int | None = None
    ordinal: int = 0
    score: float = 0.0
    snippet: SnippetOut = Field(default_factory=SnippetOut)


class AnswerOut(BaseModel):
    answer: str
    citations: list[CitationOut]
    took_ms: float
    mode: str
    degraded_reason: str | None = None


# ---- 模型配置 -------------------------------------------------------------

class EmbedCfgOut(BaseModel):
    provider: str = ""
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    dim: int = 0
    batch: int = 0
    concurrency: int = 0
    normalize: bool = True
    device: str = "cpu"
    timeout: int = 60


class LlmCfgOut(BaseModel):
    provider: str = ""
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    temperature: float = 0.0
    max_tokens: int = 0
    timeout: int = 0


class RerankCfgOut(BaseModel):
    enabled: bool = False
    provider: str = "none"
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    top_n: int = 8
    timeout: int = 30
    device: str = "cpu"


class RetrieveCfgOut(BaseModel):
    mode: str = "hybrid"
    candidate_k: int = 0
    top_k: int = 0
    k_rrf: int = 0
    nprobes: int = 0
    refine_factor: int = 0
    window: int = 0
    use_jieba: bool = True
    query_rewrite: bool = False


class ModelsOut(BaseModel):
    """配置回显。api_key 一律脱敏为 ***。"""
    embed: EmbedCfgOut
    llm: LlmCfgOut
    rerank: RerankCfgOut
    retrieve: RetrieveCfgOut


class ModelsUpdateOut(BaseModel):
    ok: bool = True
    applied: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    persisted: bool = False


class ModelsTestOut(BaseModel):
    """连通性探测：形状随 provider 而变，按请求的 kind 出现对应键。"""
    model_config = ConfigDict(extra="allow")

    embedding: dict[str, Any] | None = None
    rerank: dict[str, Any] | None = None
    llm: dict[str, Any] | None = None


class EmbedVectorsOut(BaseModel):
    model: str
    dim: int
    count: int
    vectors: list[list[float]]


# ---- 访问密钥 -------------------------------------------------------------

class ApiKeyOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    key_id: str
    name: str
    prefix: str = ""
    scope: str = "read"
    note: str = ""
    created_at: str = ""
    last_used_at: str | None = None
    expires_at: str | None = None
    revoked_at: str | None = None
    state: str = "active"
    expired: bool = False
    active: bool = True


class ApiKeyListOut(BaseModel):
    items: list[ApiKeyOut]
    total: int
    legacy_token_enabled: bool = False


class ApiKeyCreatedOut(ApiKeyOut):
    key: str
    warning: str = ""


class KeyRevokedOut(BaseModel):
    ok: bool = True
    key_id: str
    state: str = "revoked"


# ---- 任务 -----------------------------------------------------------------

class QueueStatsOut(BaseModel):
    workers: int = 0
    pending: int = 0
    max_pending: int = 0


class JobOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    job_id: str
    doc_id: str = ""
    kb_id: str = ""
    stage: str
    progress: int = 0
    total: int = 0
    parser_engine: str = ""
    error: str | None = None
    started_at: str = ""
    ended_at: str | None = None
    terminal: bool = False


class JobListOut(BaseModel):
    items: list[JobOut]
    total: int
    queue: QueueStatsOut


class JobCancelOut(BaseModel):
    """取消结果。

    `outcome` 如实区分处置方式，客户端据此给不同文案：
    - cancelled：排队中被撤销，永不执行；
    - running：已在运行，只置了取消标记，会在**下一个阶段边界**退出
      （解析与向量化不响应中断，已发出的请求必须跑完）；
    - finished：已是终态，无需处理；
    - unknown：任务不存在。
    """
    ok: bool = True
    job_id: str
    outcome: str
    stage: str = ""
    message: str = ""


# ---- 系统 -----------------------------------------------------------------

class IndexStateOut(BaseModel):
    indexed: bool = False
    unindexed_rows: int = 0


class CountMismatchOut(BaseModel):
    doc_id: str
    stated: int
    actual: int


class HealthOut(BaseModel):
    status: str
    now: str
    doc_count: int
    chunk_count: int
    count_mismatch: list[CountMismatchOut]
    orphan_chunks: int
    standalone_chunks: int
    fts_stale_count: int
    # 没查成的检查名字（不是布尔的「没问题」）。空列表 = 所有项都真的查过。
    # fts_stale_count 为 -1、index_state.unindexed_rows 为 -1 时表示「未知」
    # 而不是「0」——见 storage/health.py 的说明。
    checks_failed: list[str] = Field(default_factory=list)
    embedding_dim: int
    stored_embedding_dim: int | None = None
    dim_mismatch: bool = False
    embedding_model: str = ""
    embedding_model_mismatch: bool = False
    lancedb_versions: int = 0
    index_state: dict[str, IndexStateOut] = Field(default_factory=dict)


class StatsOut(BaseModel):
    """容量/索引统计；后端差异字段（磁盘 vs 对象数）通过 extra 透传。"""
    model_config = ConfigDict(extra="allow")

    doc_count: int = 0
    chunk_count: int = 0
    job_count: int = 0
    backend: str = ""


class VersionsOut(BaseModel):
    table: str
    versions: list[dict[str, Any]]


class RollbackOut(BaseModel):
    ok: bool = True
    table: str
    rolled_back_to: int
    note: str = ""


class ReconcileOut(BaseModel):
    ok: bool = True
    fixed: int = 0
