"""LanceDB 表 schema（LanceModel 声明式）。

向量维度在创建 chunk 表时由 embedding 配置决定，因此 Chunk 模型用工厂动态生成。
"""
from __future__ import annotations

from typing import Optional

from lancedb.pydantic import LanceModel, Vector


class Document(LanceModel):
    """文档。

    kb_id 与 stored_file 曾经塞在 meta JSON 里，导致：
    - 20 处调用点各自 `json.loads(meta) if isinstance(meta, str) else meta`
      防御式解析，任何一处漏判就静默拿到 ""；
    - 无法下推到 SQL，按知识库过滤只能全表扫描后在 Python 里筛
      （list_kbs / delete_kb / doc_ids_in_kb / list_documents 都是这么写的）。

    提升为真实列后，过滤走标量索引，取值不再有解析分支。
    meta 仅保留嵌套的 chunking 方案（含 snapshot），那是展示用的可选结构。
    """
    doc_id: str
    title: str
    source_uri: Optional[str] = None
    mime: str = "text/plain"
    parser_engine: str = "native"
    content_hash: str = ""
    char_count: int = 0
    chunk_count: int = 0
    text: str = ""                    # 原文纯文本，供查看器整篇展示
    status: str = "indexing"          # indexing | ready | failed
    error: Optional[str] = None
    kb_id: str = ""                   # 知识库归属（空串 = 未分组）
    stored_file: Optional[str] = None  # 留档原文文件名（files_dir 下）
    meta: str = "{}"                  # 仅剩 chunking 等嵌套结构
    created_at: str = ""
    updated_at: str = ""


class Job(LanceModel):
    job_id: str
    doc_id: str = ""
    # 任务归属的知识库：入库时即确定，早于 doc_id 生成。
    # 没有它，任务页无法把一个任务链回它所属的库/文档——用户只能
    # 拿一串 UUID 猜那是哪篇内容。
    kb_id: str = ""
    stage: str = "pending"            # pending|load|split|embed|write|index|done|failed
    progress: float = 0.0
    total: int = 0
    parser_engine: str = ""
    error: Optional[str] = None
    started_at: str = ""
    ended_at: Optional[str] = None


class KnowledgeBase(LanceModel):
    """知识库（文档的逻辑分组；文档的 kb_id 存于 meta JSON）。

    分块方案为知识库级默认值：chunk_size == 0 表示继承全局
    settings.split（不用 0/null 混用，避免 LanceDB 空值语义歧义）。
    overlap_ratio 为百分比（0-50），换算见 store.chunking。
    """
    kb_id: str
    name: str
    description: str = ""
    chunk_size: int = 0
    overlap_ratio: float = 0.0
    created_at: str = ""
    updated_at: str = ""


class ApiKey(LanceModel):
    """对外 API 密钥。

    只保存 sha256(secret)，明文仅在创建时返回一次——库被拷贝或备份
    泄露时不会直接变成可用凭据。prefix 是明文密钥的公开段（rg_xxxxxxxx），
    仅供人工辨认，不参与校验。
    """
    key_id: str
    name: str
    prefix: str = ""
    key_hash: str = ""              # sha256(明文密钥)，十六进制
    scope: str = "read"             # read | write
    note: str = ""
    created_at: str = ""
    last_used_at: Optional[str] = None
    expires_at: Optional[str] = None
    revoked_at: Optional[str] = None


def chunk_schema(dim: int):
    """动态生成固定向量维度的 Chunk schema。"""

    class Chunk(LanceModel):
        chunk_id: str
        doc_id: str
        ordinal: int = 0
        kb_id: str = ""               # 知识库归属（doc_id 为空时=独立分块的唯一归属）
        text: str = ""
        text_seg: str = ""            # jieba 预分词列（中文 FTS 索引目标）
        heading_path: str = ""        # "3.2 > 配置" 面包屑
        page: Optional[int] = None
        char_start: int = 0
        char_end: int = 0
        token_count: int = 0
        origin: str = "parsed"        # parsed | manual
        edited: bool = False
        enabled: bool = True          # 停用的分块不参与检索（保留数据，可恢复）
        original_text: Optional[str] = None
        offset_valid: bool = True     # 编辑导致偏移失效时置 False
        fts_stale: Optional[bool] = None  # 编辑后尚未重建 FTS 的标记（ensure_fts_index 清除）
        embed_model: str = ""
        # lancedb 的 Vector 是**运行时参数化**的（`Vector(dim)` 返回一个
        # 固定长度 list 的类型构造器），这对 pydantic 是对的，但类型检查器
        # 把「调用结果当注解」判成非法语法 —— 这是 lancedb 没有 stub 造成的，
        # 不是这里写错了。用 ignore 而不是改写：改成任何静态形式都会破坏建表。
        vector: Vector(dim)  # type: ignore[valid-type]
        created_at: str = ""
        updated_at: str = ""

    return Chunk
