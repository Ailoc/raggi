"""分块存储的后端分派：LanceDB（默认）与 Qdrant。

仓储层只调用这里的方法，不再直接摸 Lance 表。两个实现的**语义**必须
一致，差异写在各自的方法里：

- 过滤统一用 `ChunkFilter`（结构化）；
- 排序：Lance 支持任意列；Qdrant 只支持 `ord_sort` 全序键（ordinal +
  chunk_id 哈希），调用方的排序需求正好只有这一种；
- 全文：Lance 是 tantivy + FTS 重建；Qdrant 是 sparse 向量随点更新，
  因此没有 `fts_stale` 状态；
- 乐观并发：Lance 的 update 能带 `updated_at` 条件一次完成；Qdrant
  只能「读-比对-写」，窗口更宽，这里如实记录这一差异。
"""
from __future__ import annotations

import logging

from .filters import ChunkFilter, where_sql

logger = logging.getLogger("raggi.storage.backends")


class ChunkBackend:
    engine = "?"

    def __init__(self, store):
        self.store = store

    # 读
    def rows(self, *, cols: list[str], where: "ChunkFilter | str | None" = None,
             order_by: list[tuple[str, bool]] | None = None,
             limit: int | None = None, offset: int | None = None,
             ids=None, id_column: str = "chunk_id") -> list[dict]:
        raise NotImplementedError

    def count(self, where: "ChunkFilter | str | None" = None) -> int:
        raise NotImplementedError

    def max_ordinal(self, doc_id: str) -> int:
        raise NotImplementedError

    # 写
    def upsert(self, rows: list[dict]) -> None:
        raise NotImplementedError

    def delete(self, where: ChunkFilter) -> None:
        raise NotImplementedError

    def update(self, where: ChunkFilter, values: dict) -> int:
        raise NotImplementedError

    def update_text(self, *, chunk_id: str, values: dict,
                    expect_updated_at: str | None = None) -> bool:
        raise NotImplementedError

    # 检索与观测
    def search(self, **kwargs) -> list[dict]:
        raise NotImplementedError

    def facts(self) -> dict:
        raise NotImplementedError

    def vector_dim(self) -> int | None:
        raise NotImplementedError

    def index_state(self) -> dict:
        raise NotImplementedError

    def ensure_indexes(self) -> None:
        raise NotImplementedError


class LanceChunks(ChunkBackend):
    engine = "lance"

    def rows(self, *, cols, where=None, order_by=None, limit=None,
             offset=None, ids=None, id_column="chunk_id"):
        from .sql import scalar_rows

        return scalar_rows(
            self.store.chunks, cols=cols, where=where_sql(where),
            order_by=order_by, limit=limit, offset=offset,
            ids=ids, id_column=id_column)

    def count(self, where=None) -> int:
        from .sql import count_rows

        return int(count_rows(self.store.chunks, where_sql(where)))

    def max_ordinal(self, doc_id: str) -> int:
        from .sql import scalar_rows

        rows = scalar_rows(
            self.store.chunks, cols=["ordinal"],
            where=where_sql(ChunkFilter(doc_ids=(doc_id,))),
            order_by=[("ordinal", False)], limit=1)
        return int(rows[0]["ordinal"]) if rows else -1

    def upsert(self, rows: list[dict]) -> None:
        from .repos.chunks import _lance_upsert

        _lance_upsert(self.store, rows)

    def delete(self, where: ChunkFilter) -> None:
        from .repos.chunks import _lance_delete

        _lance_delete(self.store, where)

    def update(self, where: ChunkFilter, values: dict) -> int:
        from .repos.chunks import _lance_update

        return _lance_update(self.store, where, values)

    def update_text(self, *, chunk_id, values, expect_updated_at=None) -> bool:
        from .repos.chunks import _lance_update_text

        return _lance_update_text(
            self.store, chunk_id, values, expect_updated_at=expect_updated_at)

    def search(self, **kwargs) -> list[dict]:
        from .repos.chunks import _lance_search

        return _lance_search(self.store, **kwargs)

    def facts(self) -> dict:
        # 2 万块的实测：三趟扫描合并成一次 Arrow 窄列扫描（40ms → 8–17ms）
        import pyarrow as pa
        import pyarrow.compute as pc

        tbl = self.store.chunks.to_lance().to_table(
            columns=["doc_id", "fts_stale", "embed_model"])
        vc = pc.value_counts(tbl.column("doc_id"))
        counts = {str(v): int(c) for v, c in zip(
            vc.field("values").to_pylist(), vc.field("counts").to_pylist())}
        stale = int(pc.sum(pc.cast(
            tbl.column("fts_stale"), pa.int64())).as_py() or 0)
        models = {str(m) for m in pc.unique(
            tbl.column("embed_model")).to_pylist() if m}
        return {"counts": counts, "fts_stale": stale, "models": models}

    def vector_dim(self) -> int | None:
        return self.store.vector_dim()

    def index_state(self) -> dict:
        out = {}
        for col in ("vector", "text_seg"):
            exists, unindexed = self.store._index_state(col)
            out[col] = {"indexed": exists, "unindexed_rows": unindexed}
        return out

    def ensure_indexes(self) -> None:
        self.store.ensure_scalar_indexes()
        self.store.ensure_vector_index()
        self.store.ensure_fts_index()


class QdrantChunks(ChunkBackend):
    engine = "qdrant"

    def __init__(self, store, qdrant):
        super().__init__(store)
        self.q = qdrant

    # ---- 读 ------------------------------------------------------------

    def rows(self, *, cols, where=None, order_by=None, limit=None,
             offset=None, ids=None, id_column="chunk_id"):
        flt = self._filter(where)
        if ids is not None:
            wanted = [str(i) for i in ids]
            if not wanted:
                return []
            rows = self._retrieve(wanted)
        else:
            want = int(limit or 100) + int(offset or 0)
            rows = self._scroll(flt, want)
            if order_by:
                rows = _sort_rows(rows, order_by)
            rows = rows[int(offset or 0):int(offset or 0) + int(limit or len(rows))]
        return [{c: r.get(c) for c in cols} for r in rows]

    def count(self, where=None) -> int:
        return self.q.count(self._filter(where))

    def max_ordinal(self, doc_id: str) -> int:
        rows = self._scroll(
            self._filter(ChunkFilter(doc_ids=(doc_id,))), 1, desc=True)
        return int(rows[0]["ordinal"]) if rows else -1

    # ---- 写 ------------------------------------------------------------

    def upsert(self, rows: list[dict]) -> None:
        self.q.upsert_chunks(rows)

    def delete(self, where: ChunkFilter) -> None:
        if where is None or where.is_empty():
            raise ValueError("Qdrant 删除必须带过滤条件（全删请 drop collection）")
        self.q.delete_chunks(where)

    def update(self, where: ChunkFilter, values: dict) -> int:
        if where is None or where.is_empty():
            raise ValueError("Qdrant 更新必须带过滤条件")
        ids = [r["chunk_id"] for r in self._scroll(self._filter(where), 100000)]
        if not ids:
            return 0
        # Qdrant 的 set_payload 不支持 per-point 不同值；这里值都相同
        for cid in ids:
            self.q.set_payload(cid, values)
        return len(ids)

    def update_text(self, *, chunk_id, values, expect_updated_at=None) -> bool:
        current = self.q.get_chunk(chunk_id)
        if current is None:
            return False
        if expect_updated_at and str(current.get("updated_at") or "") != expect_updated_at:
            return False
        values = dict(values)
        vector = values.pop("vector")
        text = str(values.pop("text"))
        payload = {k: v for k, v in values.items() if k != "text_seg"}
        self.q.update_text(chunk_id, text=text, vector=vector, **payload)
        return True

    # ---- 检索与观测 ------------------------------------------------------

    def search(self, **kwargs) -> list[dict]:
        mode = kwargs.get("mode", "vector")
        return self.q.search(
            mode=mode, vector=kwargs.get("vector"),
            text=kwargs.get("text") or "",
            where=kwargs.get("where"),
            limit=int(kwargs.get("limit") or 50),
            candidate_k=kwargs.get("candidate_k"))

    def facts(self) -> dict:
        counts, stale, models = self.q.facts()
        return {"counts": counts, "fts_stale": stale, "models": models}

    def vector_dim(self) -> int | None:
        return self.q.vector_dim()

    def index_state(self) -> dict:
        # Qdrant 的向量与 sparse 索引随写入维护，没有"未索引行"
        return {"vector": {"indexed": True, "unindexed_rows": 0},
                "text_seg": {"indexed": True, "unindexed_rows": 0}}

    def ensure_indexes(self) -> None:
        self.q.ensure_collection()

    # ---- 内部 -----------------------------------------------------------

    def _filter(self, where):
        if where is None:
            return None
        if isinstance(where, ChunkFilter):
            return where
        raise TypeError(
            "Qdrant 后端需要结构化 ChunkFilter；字符串 SQL 只属于 Lance")

    def _retrieve(self, ids: list[str]) -> list[dict]:
        pts = self.q.client.retrieve(
            self.q.collection, ids=ids, with_payload=True, with_vectors=False)
        return [dict(p.payload or {}) for p in pts]

    def _scroll(self, where: ChunkFilter | None, limit: int,
                desc: bool = False) -> list[dict]:
        from qdrant_client import models as qm

        pts, _cursor = self.q.client.scroll(
            self.q.collection, scroll_filter=_qdrant_filter(where),
            limit=max(1, min(int(limit), 100000)),
            order_by=qm.OrderBy(
                key="ord_sort",
                direction=qm.Direction.DESC if desc else qm.Direction.ASC),
            with_payload=True, with_vectors=False)
        return [dict(p.payload or {}) for p in pts]


def _qdrant_filter(where: ChunkFilter | None):
    from .qdrant_store import to_qdrant_filter

    return to_qdrant_filter(where)


def _sort_rows(rows: list[dict], order_by: list[tuple[str, bool]]) -> list[dict]:
    for col, asc in reversed(order_by):
        rows.sort(key=lambda r: (r.get(col) is None, r.get(col)),
                  reverse=not asc)
    return rows


def chunk_backend(store) -> ChunkBackend:
    """按 store 的向量引擎返回后端（默认 Lance，行为不变）。"""
    engine = str(getattr(store, "vector_engine", "lance") or "lance")
    if engine == "qdrant":
        qdrant = getattr(store, "qdrant", None)
        if qdrant is None:
            raise RuntimeError(
                "vector_engine=qdrant 但 store.qdrant 未装配（见 server.build_ctx）")
        return QdrantChunks(store, qdrant)
    return LanceChunks(store)
