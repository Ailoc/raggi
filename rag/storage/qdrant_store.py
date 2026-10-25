"""Qdrant 分块存储后端（阶段 3b 核心）。

与 LanceDB 是**同一批语义的另一种实现**：结构化过滤（`ChunkFilter`）、
dense 向量检索、sparse 全文检索、RRF 融合。默认后端仍是 LanceDB；
这个模块先由测试与迁移工具验证结果口径，再接入 `LanceStore` 的分派。

三个刻意的对齐点：

1. **过滤**：`ChunkFilter` 直接映射成 Qdrant 的 `Filter`，不解析 SQL；
   mime/parser_engine 解析出的白名单为空时，落成恒假条件。
2. **全文**：用 jieba 分词 → sparse 向量（token 计数），索引开 IDF
   修饰器，逼近 BM25 的「稀有词权重更高」。与 tantivy 的分数不可比，
   但排序口径一致（越大越相关）。
3. **分数**：Qdrant 的 cosine 返回**相似度**（越大越相关），而应用层
   按 Lance 的「余弦距离」读 `_distance`。适配器统一折成
   `_distance = 1 - similarity`，RRF 融合落 `_relevance_score`，
   sparse 落 `_score` —— 与 `_score_of()` 的既有约定一一对应。
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any

from rag.parsing.segment import segment

from .filters import ChunkFilter

logger = logging.getLogger("raggi.storage.qdrant")

COLLECTION = "chunks"
DENSE = "dense"
SPARSE = "text"

# 结果行必须带的列（与 Lance 的 SEARCH_COLS 对齐）
PAYLOAD_COLS = ["chunk_id", "doc_id", "kb_id", "ordinal", "heading_path",
                "page", "text", "char_start", "char_end", "offset_valid",
                "edited", "enabled", "origin", "embed_model",
                "created_at", "updated_at"]
# 复合排序键：ordinal 只在单文档内唯一，必须再拼 chunk_id 才有全序，
# 否则分页在 ordinal 并列处会重复/漏项（与 Lance 侧的次级排序键同义）。
ORD_SORT = "ord_sort"


def _token_id(token: str) -> int:
    """token → uint32（稳定哈希；跨进程/重启一致）。"""
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=4).digest()
    return int.from_bytes(digest, "big")


def sparse_vector(text: str) -> tuple[list[int], list[float]]:
    """jieba 分词 → (indices, values)，值取词频。"""
    counts: dict[int, float] = {}
    for token in segment(text).split():
        if not token:
            continue
        idx = _token_id(token)
        counts[idx] = counts.get(idx, 0.0) + 1.0
    items = sorted(counts.items())
    return [i for i, _ in items], [v for _, v in items]


def to_qdrant_filter(where: ChunkFilter | None):
    """ChunkFilter → qdrant.Filter（恒假/空条件都有明确语义）。"""
    from qdrant_client import models as qm

    if where is None:
        return None
    must: list[Any] = []
    if where.enabled_only:
        must.append(qm.FieldCondition(
            key="enabled", match=qm.MatchValue(value=True)))
    if where.origin:
        must.append(qm.FieldCondition(
            key="origin", match=qm.MatchValue(value=where.origin)))
    if where.kb_id:
        must.append(qm.FieldCondition(
            key="kb_id", match=qm.MatchValue(value=where.kb_id)))
    elif where.kb_ids:
        must.append(qm.FieldCondition(
            key="kb_id", match=qm.MatchAny(any=list(where.kb_ids))))
    if where.doc_ids:
        must.append(qm.FieldCondition(
            key="doc_id", match=qm.MatchAny(any=list(where.doc_ids))))
    for ids in where.doc_id_sets:
        if not ids:
            # 恒假：绝不退化成"不加这个过滤"（那会返回全库）
            must.append(qm.FieldCondition(
                key="chunk_id", match=qm.MatchAny(any=["__no_match__"])))
        else:
            must.append(qm.FieldCondition(
                key="doc_id", match=qm.MatchAny(any=list(ids))))
    if where.only_standalone:
        must.append(qm.FieldCondition(
            key="doc_id", match=qm.MatchValue(value="")))
    if (where.text_contains or "").strip():
        # 依赖 payload text 上的全文索引；中文分词粒度由 Qdrant 决定，
        # 与 tantivy 的 jieba 分词不完全一致（列在这里作为已知差异）。
        must.append(qm.FieldCondition(
            key="text",
            match=qm.MatchText(text=where.text_contains.strip())))
    return qm.Filter(must=must) if must else None


@dataclass
class QdrantChunkStore:
    """分块在 Qdrant 里的读写（检索是主路径）。"""

    url: str
    collection: str = COLLECTION
    dim: int = 4
    timeout: int = 10

    def __post_init__(self) -> None:
        try:
            from qdrant_client import QdrantClient
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "未安装 qdrant-client（pip install 'raggi[vector-qdrant]'）") from e
        self.client = QdrantClient(url=self.url, timeout=self.timeout)

    # ---- collection ----------------------------------------------------

    def ensure_collection(self, *, recreate: bool = False) -> None:
        from qdrant_client import models as qm

        exists = self.client.collection_exists(self.collection)
        if exists and recreate:
            self.client.delete_collection(self.collection)
            exists = False
        if not exists:
            self.client.create_collection(
                self.collection,
                vectors_config={
                    DENSE: qm.VectorParams(
                        size=self.dim, distance=qm.Distance.COSINE),
                },
                sparse_vectors_config={
                    SPARSE: qm.SparseVectorParams(
                        index=qm.SparseIndexParams(on_disk=False),
                        modifier=qm.Modifier.IDF),
                },
            )
        try:
            self.client.create_payload_index(
                self.collection, field_name=ORD_SORT,
                field_schema=qm.PayloadSchemaType.INTEGER)
        except Exception:  # noqa: BLE001  已存在即可
            pass
        for field in ("doc_id", "kb_id", "origin", "enabled"):
            try:
                self.client.create_payload_index(
                    self.collection, field_name=field,
                    field_schema=qm.PayloadSchemaType.KEYWORD
                    if field != "enabled" else qm.PayloadSchemaType.BOOL)
            except Exception:  # noqa: BLE001  已存在即可
                pass
        try:
            self.client.create_payload_index(
                self.collection, field_name="text",
                field_schema=qm.TextIndexParams(
                    type=qm.TextIndexType.TEXT,
                    tokenizer=qm.TokenizerType.WORD,
                    lowercase=True))
        except Exception:  # noqa: BLE001
            pass

    def drop(self) -> None:
        if self.client.collection_exists(self.collection):
            self.client.delete_collection(self.collection)

    def count(self, where: ChunkFilter | None = None) -> int:
        return int(self.client.count(
            self.collection, count_filter=to_qdrant_filter(where),
            exact=True).count)

    def vector_dim(self) -> int | None:
        """collection 里 dense 向量的实际维度（对账用）。"""
        info = self.client.get_collection(self.collection)
        vectors = info.config.params.vectors
        if isinstance(vectors, dict):
            params = vectors.get(DENSE)
            return int(params.size) if params is not None else None
        return int(vectors.size) if vectors is not None else None

    def page(self, *, where: ChunkFilter | None = None,
             limit: int = 100, offset: int = 0) -> tuple[list[dict], int]:
        """分块列表页：稳定排序 + 计数。

        Qdrant 的 scroll 游标是"上一页最后一个点"，不是数字偏移；为了
        保持 API 的 `offset` 语义，这里取 `offset+limit` 再切片。分块列表
        的页深很小（limit 上限 500），代价可接受；真要深翻页应改用游标。
        """
        from qdrant_client import models as qm

        n = max(1, int(limit))
        skip = max(0, int(offset))
        flt = to_qdrant_filter(where)
        rows: list[dict] = []
        cursor = None
        while len(rows) < skip + n:
            pts, cursor = self.client.scroll(
                self.collection, scroll_filter=flt,
                limit=skip + n - len(rows), offset=cursor,
                order_by=qm.OrderBy(key=ORD_SORT,
                                    direction=qm.Direction.ASC),
                with_payload=True, with_vectors=False)
            rows.extend(dict(p.payload or {}) for p in pts)
            if cursor is None:
                break
        return rows[skip:skip + n], self.count(where)

    def facts(self) -> tuple[dict[str, int], int, set[str]]:
        """健康检查需要的三项（与 Lance 侧 `_chunk_facts` 同义）。

        Qdrant 里没有"待重建 FTS"这回事：sparse 向量随点位一起更新，
        所以 fts_stale 恒为 0。
        """
        counts: dict[str, int] = {}
        models: set[str] = set()
        cursor = None
        while True:
            pts, cursor = self.client.scroll(
                self.collection, limit=1000, offset=cursor,
                with_payload=["doc_id", "embed_model"], with_vectors=False)
            for p in pts:
                payload = dict(p.payload or {})
                doc = str(payload.get("doc_id") or "")
                counts[doc] = counts.get(doc, 0) + 1
                model = payload.get("embed_model")
                if model:
                    models.add(str(model))
            if cursor is None:
                break
        return counts, 0, models

    # ---- 写入 ----------------------------------------------------------

    def upsert_chunks(self, rows: list[dict]) -> None:
        from qdrant_client import models as qm

        points = []
        for r in rows:
            vec = r.get("vector")
            if vec is None:
                raise ValueError("upsert_chunks 需要 vector 列")
            payload = {k: r.get(k) for k in PAYLOAD_COLS if k in r}
            payload["chunk_id"] = str(r["chunk_id"])
            payload[ORD_SORT] = _ord_sort(r.get("ordinal"), r["chunk_id"])
            indices, values = sparse_vector(str(r.get("text") or ""))
            points.append(qm.PointStruct(
                id=str(r["chunk_id"]),
                vector={
                    DENSE: [float(x) for x in vec],
                    SPARSE: qm.SparseVector(indices=indices, values=values),
                },
                payload=payload,
            ))
        if points:
            self.client.upsert(self.collection, points=points, wait=True)

    def delete_chunks(self, where: ChunkFilter | None) -> None:
        from qdrant_client import models as qm

        flt = to_qdrant_filter(where)
        if flt is None:
            raise ValueError("删除必须带过滤条件（全量删除请走 drop()）")
        self.client.delete(
            self.collection,
            points_selector=qm.FilterSelector(filter=flt), wait=True)

    def set_payload(self, chunk_id: str, values: dict) -> None:
        self.client.set_payload(
            self.collection, payload=values, points=[str(chunk_id)], wait=True)

    def update_text(self, chunk_id: str, *, text: str, vector: list[float],
                    **payload) -> None:
        """改写正文：dense 向量与 sparse 全文一起更新（编辑分块路径）。"""
        from qdrant_client import models as qm

        indices, values = sparse_vector(text)
        self.client.update_vectors(
            self.collection, points=[qm.PointVectors(
                id=str(chunk_id),
                vector={
                    DENSE: [float(x) for x in vector],
                    SPARSE: qm.SparseVector(indices=indices, values=values),
                })], wait=True)
        self.set_payload(chunk_id, {"text": text, **payload})

    def get_chunk(self, chunk_id: str) -> dict | None:
        pts = self.client.retrieve(
            self.collection, ids=[str(chunk_id)], with_payload=True,
            with_vectors=False)
        return dict(pts[0].payload or {}) if pts else None

    # ---- 检索 ----------------------------------------------------------

    def search(self, *, mode: str = "vector", vector: list[float] | None = None,
               text: str = "", where: ChunkFilter | None = None,
               limit: int = 50, candidate_k: int | None = None) -> list[dict]:
        """vector / fts / hybrid（dense + sparse，RRF 融合）。"""
        from qdrant_client import models as qm

        n = max(1, int(candidate_k or limit))
        flt = to_qdrant_filter(where)
        if mode == "hybrid":
            if vector is None:
                raise ValueError("hybrid 检索需要 vector")
            indices, values = sparse_vector(text)
            prefetch = [
                qm.Prefetch(query=[float(x) for x in vector], using=DENSE,
                            limit=n, filter=flt),
                qm.Prefetch(
                    query=qm.SparseVector(indices=indices, values=values),
                    using=SPARSE, limit=n, filter=flt),
            ]
            res = self.client.query_points(
                self.collection, prefetch=prefetch,
                query=qm.FusionQuery(fusion=qm.Fusion.RRF),
                limit=n, with_payload=True)
            rows = [_row(p, relevance=p.score) for p in res.points]
        elif mode == "fts":
            indices, values = sparse_vector(text)
            res = self.client.query_points(
                self.collection,
                query=qm.SparseVector(indices=indices, values=values),
                using=SPARSE, query_filter=flt, limit=n, with_payload=True)
            rows = [_row(p, score=p.score) for p in res.points]
        else:
            if vector is None:
                raise ValueError("向量检索需要 vector")
            res = self.client.query_points(
                self.collection, query=[float(x) for x in vector],
                using=DENSE, query_filter=flt, limit=n, with_payload=True)
            # cosine 相似度 → 应用层约定的余弦距离
            rows = [_row(p, distance=1.0 - float(p.score)) for p in res.points]
        # 分数列在检索后按上限截断（应用层还会精排/阈值过滤）
        return rows[:max(1, int(limit))]


def _ord_sort(ordinal, chunk_id) -> int:
    """(ordinal, chunk_id) 的**全序**整数键：高位 ordinal，低位 chunk_id 哈希。

    Qdrant 的 order_by 要求 range 索引，不能按字符串排序；把两级排序键
    压进一个 int64 既满足索引要求，也保住「并列处不重复不丢项」。
    """
    try:
        n = max(0, int(ordinal or 0))
    except (TypeError, ValueError):
        n = 0
    h = int.from_bytes(
        hashlib.blake2b(str(chunk_id).encode("utf-8"), digest_size=4).digest(),
        "big")
    return (n << 32) | (h & 0xFFFFFFFF)


def _row(point, *, distance: float | None = None,
         score: float | None = None,
         relevance: float | None = None) -> dict:
    row = dict(point.payload or {})
    row["chunk_id"] = str(row.get("chunk_id") or point.id)
    if distance is not None:
        row["_distance"] = float(distance)
    if score is not None:
        row["_score"] = float(score)
    if relevance is not None:
        row["_relevance_score"] = float(relevance)
    return row
