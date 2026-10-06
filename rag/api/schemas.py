"""请求/响应模型：全部端点类型化（替代原 body: dict）。"""
from __future__ import annotations

from typing import Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field


class SearchFilters(BaseModel):
    """检索过滤条件。全部可选，缺省不过滤。

    kb_id / doc_ids / origin 作用在 chunks 表（下推到 SQL）；
    mime / parser_engine 在 documents 表，会先解析成 doc_id 集合再下推。
    """
    kb_id: Optional[str] = None
    doc_ids: Optional[list[str]] = None
    origin: Optional[Literal["parsed", "manual"]] = None
    mime: Optional[Union[str, list[str]]] = None
    parser_engine: Optional[Union[str, list[str]]] = None
    include_disabled: bool = False


class SearchReq(BaseModel):
    """检索请求。

    参数按职责分三层，便于调用方理解「调哪个旋钮影响什么」：

    - **召回层**（candidate_k / nprobes / refine_factor / k_rrf / rerank）
      决定「找出多少候选」，影响召回率与延迟；
    - **返回层**（top_k / offset / score_threshold / group_by_doc）
      决定「返回哪些、返回多少」，影响可读性与翻页；
    - **展示层**（window / include_context / snippet_chars / highlight）
      只影响响应体积与呈现，不影响召回结果。
    """
    q: str = Field(min_length=1, max_length=2000)

    # ---- 检索模式 ----
    mode: Optional[Literal["hybrid", "vector", "fts"]] = None

    # ---- 返回层 ----
    top_k: Optional[int] = Field(default=None, ge=1, le=200,
                                description="返回条数，缺省取配置 retrieve.top_k")
    offset: int = Field(default=0, ge=0, le=10000,
                        description="跳过前 N 条（配合 top_k 翻页）")
    score_threshold: Optional[float] = Field(
        default=None, ge=-100.0, le=100.0,
        description="相关性下限，低于它的命中被丢弃；单位见响应的 "
                    "score_kind（统一为越大越相关）")
    group_by_doc: bool = Field(
        default=False, description="同一文档只保留最高分的一条（跨文档去重）")

    # ---- 召回层 ----
    candidate_k: Optional[int] = Field(default=None, ge=1, le=1000,
                                       description="候选池大小，越大召回越全越慢")
    nprobes: Optional[int] = Field(default=None, ge=1, le=1024,
                                   description="向量索引探测的分区数")
    refine_factor: Optional[int] = Field(default=None, ge=1, le=100,
                                         description="量化索引的重排倍数")
    k_rrf: Optional[int] = Field(default=None, ge=1, le=1000,
                                 description="混合检索 RRF 融合常数")
    rerank: Optional[bool] = Field(
        default=None, description="强制开启/关闭精排；缺省按服务端配置")

    # ---- 展示层 ----
    window: Optional[int] = Field(default=None, ge=0, le=10,
                                  description="上下文窗口（前后各取 N 个分块）")
    include_context: bool = Field(default=True,
                                  description="是否返回上下文分块文本")
    snippet_chars: Optional[int] = Field(
        default=None, ge=50, le=2000,
        description="摘要长度（字符）。片段是结构化的 {text, marks}，"
                    "text 就是纯文本长度")
    highlight: bool = Field(default=True,
                            description="是否产出高亮区间（marks）")

    # ---- 过滤 ----
    # 用 SearchFilters 而非裸 dict：过滤契约因此进入 OpenAPI，
    # 调用方能在文档里看到全部可用的过滤维度。早前这里是 dict，
    # SearchFilters 只是个从未被引用的死模型。
    filters: Optional[SearchFilters] = None
    kb_id: Optional[str] = Field(
        default=None, description="限定知识库；等价于 filters.kb_id，"
                                  "保留是为了兼容旧调用方")


class TextIngestReq(BaseModel):
    text: str
    title: str = "untitled"
    kb_id: Optional[str] = None


class BatchIngestReq(BaseModel):
    items: list[TextIngestReq] = Field(default_factory=list)


class UrlIngestReq(BaseModel):
    url: str
    kb_id: Optional[str] = None


class DocUpdateReq(BaseModel):
    """文档可编辑元信息。

    此前标题完全不可改——粘贴文本的标题取首行前 60 字，写错就永久错了。

    `kb_id` 让文档能在知识库之间移动。空串表示移出到「未分组」，
    不传（null）表示不动——这两者语义不同，不能混。

    **extra="forbid"**：默认的「忽略多余字段」会让请求**返回成功却什么
    都没做**。早前端点想改归属时传的 kb_id 被静默丢弃，调用方无从察觉；
    现在拼错字段名会立刻 422。
    """
    model_config = ConfigDict(extra="forbid")

    title: Optional[str] = None
    kb_id: Optional[str] = Field(
        default=None,
        description="目标知识库；空串表示移出到未分组，不传表示不改动")


class DocDeleteBatchReq(BaseModel):
    """批量删除文档。

    为什么不是 `DELETE /api/documents/{id}` 循环：批量场景下逐条请求
    既慢又没有原子性（删到一半失败时用户无从判断删了哪些）。
    """
    doc_ids: list[str] = Field(min_length=1)


class ReparseReq(BaseModel):
    engine: Optional[str] = None


class KBCreateReq(BaseModel):
    name: str
    description: str = ""
    chunk_size: Optional[int] = None      # None/0 = 继承全局
    overlap_ratio: Optional[float] = None


class KBUpdateReq(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    chunk_size: Optional[int] = None      # None = 不改；0 = 恢复继承
    overlap_ratio: Optional[float] = None


class PlanUpdateReq(BaseModel):
    """分块方案写入（知识库 / 文档共用）。

    chunk_size 为 None 或 0 表示恢复继承上一级。
    """
    chunk_size: Optional[int] = None
    overlap_ratio: Optional[float] = None


class ChunkAddReq(BaseModel):
    text: str
    doc_id: Optional[str] = None
    kb_id: Optional[str] = None


class ChunkEditReq(BaseModel):
    text: str
    force: bool = False
    # 乐观并发：提交你读到的那一版 updated_at。若这期间别人改过，
    # 服务端返回 409 而不是静默覆盖。不传则沿用「最后写入者胜」，
    # 旧脚本与旧客户端因此不受影响。
    updated_at: Optional[str] = None


class ChunkEnabledReq(BaseModel):
    enabled: bool


class ChunkBatchEnabledReq(BaseModel):
    """批量启停分块（与批量改内容 PATCH /api/chunks 是两件事）。

    前端原先在客户端循环调用单条端点：200 次请求、无原子性，
    中途失败会让「哪些块被停用了」变得无法回答。
    """
    chunk_ids: list[str] = Field(min_length=1)
    enabled: bool


class ChunkEditItem(BaseModel):
    chunk_id: str
    text: str
    force: bool = False
    updated_at: Optional[str] = None


class ChunkBatchEditReq(BaseModel):
    edits: list[ChunkEditItem] = Field(default_factory=list)


class AnswerReq(BaseModel):
    q: str
    # 上限必须与 SearchReq.top_k 对齐：这里会一次性把命中块的**全文**
    # 全部拉进内存拼提示词，没有上限就能用构造请求打出数百 MB。
    top_k: Optional[int] = Field(default=None, ge=1, le=50)
    mode: Optional[str] = None
    filters: dict = {}
    window: Optional[int] = None
    kb_id: Optional[str] = None    # 按知识库问答（原先缺失）


class EmbedReq(BaseModel):
    texts: list[str] = Field(default_factory=list)


class ModelsTestReq(BaseModel):
    kind: str = "all"


class ModelsUpdateReq(BaseModel):
    embed: Optional[dict] = None
    llm: Optional[dict] = None
    rerank: Optional[dict] = None


class ApiKeyCreateReq(BaseModel):
    """签发 API 密钥。

    scope 缺省为最保守的 read——「签发即最小权限」，需要写能力时
    应由用户显式选择，而不是默默给全权限。
    """
    name: str
    scope: Literal["read", "write"] = "read"
    expires_in_days: Optional[int] = None   # null = 永不过期
    note: Optional[str] = None


class RollbackReq(BaseModel):
    version: int
    table: str = "chunks"
