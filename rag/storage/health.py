"""健全性对账：计数一致性、孤儿检测、fts_stale、维度一致性、版本数。

性能：全部走「窄列投影 + pyarrow 向量化聚合」。

窄列投影在这里**必须**用 `to_lance().to_table(columns=…)`（即 `sql.scalar_rows`）
而不是 `search().select(…).to_arrow()`：投影是否真的下推在本仓库是被质疑过的
（`api/documents.py` 里留过一句吐槽——「不保证 select 生效，若把正文带回来就是
性能问题」），而原生 scanner 的列裁剪是确定的。`documents.text` 存的是整篇正文
（上限 20MB/篇），一次对账顺带把它取回来就是事故。
"""
from __future__ import annotations

import datetime
import logging

import pyarrow.compute as pc

from .repos.chunks import chunk_counts_by_doc
from .repos.documents import docs_count, docs_query
from .tables import LanceStore

logger = logging.getLogger("raggi.health")


def doc_ids_present(store: LanceStore) -> set[str]:
    """documents 表里现存的 doc_id 集合。

    删除文档前取一次、删除后再取一次做差集，比「每篇文档一次 count_rows」
    便宜一个数量级（`repos.documents.delete_documents` 用它回写受影响库的计数）。
    """
    return {str(r["doc_id"]) for r in docs_query(store, ["doc_id"])
            if r.get("doc_id") is not None}


def _unique_models(store: LanceStore) -> set[str]:
    """chunks 表里出现过的 embed_model 取值（一致性校验用）。"""
    col = store.chunks.search().select(["embed_model"]).to_arrow().column(
        "embed_model")
    return {str(m) for m in pc.unique(col).to_pylist() if m}


def health(store: LanceStore, embed_dim: int, embed_model: str) -> dict:
    chunks = store.chunks

    # doc_count / stated 计数一律走仓储层的 documents 读入口。
    # 这里以前直接读 Lance 那张旧表：元数据真源搬到 SQLite 之后，
    # 新入库一篇就会「列表 8 篇、health 7 篇」，并因此永久 degraded。
    doc_count = docs_count(store)
    chunk_count = chunks.count_rows()
    actual_by_doc = chunk_counts_by_doc(store)
    stated_rows = docs_query(store, ["doc_id", "chunk_count"])

    mismatch = []
    for r in stated_rows:
        actual = actual_by_doc.get(str(r["doc_id"]), 0)
        if actual != int(r["chunk_count"] or 0):
            mismatch.append({"doc_id": str(r["doc_id"]),
                             "stated": int(r["chunk_count"] or 0),
                             "actual": actual})

    # 孤儿 chunk：doc_id 在 documents 中不存在。
    # doc_id 为空串的是「独立分块」——直接挂在知识库下、不属于任何文档，
    # 是合法数据，不能算孤儿（否则健康检查永远 degraded）。
    doc_ids = {str(r["doc_id"]) for r in stated_rows}
    orphan = sum(1 for d in actual_by_doc
                 if d not in doc_ids and d not in ("", None))

    # 下面三项检查在异常时**不许**回报「健康」。
    # 改前的形状是这一整个审计的主线：`except Exception: fts_stale = 0`
    # 与 `except Exception: embed_model_mismatch = False` —— 检查根本没跑成，
    # 却交出「0 条待重建 / 模型一致」这两个**肯定回答**，而
    # `embed_model_mismatch` 还直接参与 `status` 的计算 ⇒ 坏消息被翻译成绿灯，
    # 且一行日志都不留（默认日志级别下无人看见）。
    # 现在：失败记进 `checks_failed` 对外可见，值用「未知」而不是「没事」，
    # 并留 warning 级日志。字段类型保持不变（前端与契约都按 int/bool 用），
    # 未知用各自的哨兵表达：计数用 -1、布尔用「不算进 status 判定」。
    checks_failed: list[str] = []

    # fts stale：编辑后尚未重建 FTS 的行数（由 ensure_fts_index 清除）
    try:
        fts_stale = int(chunks.count_rows(filter="fts_stale = true"))
    except Exception as e:  # noqa: BLE001
        # -1 = 不知道（0 会被 UI 当成「没有待重建」显示成绿色）
        fts_stale = -1
        checks_failed.append("fts_stale")
        logger.warning("fts_stale 计数失败，健康检查该项未知: %s", e)

    # 嵌入模型一致性：表中出现与当前配置不同的 embed_model 即视为不一致。
    # 用 unique() 而不是整列 to_pylist 后逐行比（10 万行时那是一趟全量 Python 循环）。
    embed_model_checked = True
    try:
        embed_model_mismatch = any(m != embed_model for m in _unique_models(store))
    except Exception as e:  # noqa: BLE001
        embed_model_mismatch = False
        embed_model_checked = False
        checks_failed.append("embedding_model")
        logger.warning("embed_model 一致性检查失败，健康检查该项未知: %s", e)

    try:
        versions = len(chunks.list_versions())
    except Exception as e:  # noqa: BLE001
        versions = -1
        checks_failed.append("lancedb_versions")
        logger.warning("list_versions 失败: %s", e)

    # 索引覆盖状态（向量/FTS 是否存在及未索引行数）
    index_state = {}
    for col_name in ("vector", "text_seg"):
        try:
            exists, unindexed = store._index_state(col_name)
            index_state[col_name] = {"indexed": exists,
                                     "unindexed_rows": unindexed}
        except Exception as e:  # noqa: BLE001
            # indexed=False 至少不是好消息；unindexed_rows=-1 标出「没查到」
            index_state[col_name] = {"indexed": False, "unindexed_rows": -1}
            checks_failed.append(f"index_state:{col_name}")
            logger.warning("索引状态查询失败 (%s): %s", col_name, e)

    # 嵌入维度一致性：表中实际向量维度 vs 配置
    stored_dim = store.vector_dim()
    dim_mismatch = stored_dim is not None and stored_dim != embed_dim

    return {
        # `status` 的语义如实改为「**能确认**没有不一致」。
        # embed_model 这项检查没跑成时不再默认「一致」——它原本直接参与这个判定，
        # 静默吞掉就等于把坏消息翻译成绿灯。
        # 至于「哪几项压根没查成」，看 `checks_failed`：那是「不知道」，
        # 与「查到了、没问题」是两种不同的状态，别混在一起。
        "status": "ok" if not mismatch and orphan == 0
        and embed_model_checked and not embed_model_mismatch
        and not dim_mismatch
        else "degraded",
        "checks_failed": checks_failed,
        "now": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "doc_count": doc_count,
        "chunk_count": chunk_count,
        "count_mismatch": mismatch,
        "orphan_chunks": orphan,
        "standalone_chunks": int(actual_by_doc.get("", 0)),
        "fts_stale_count": fts_stale,
        "embedding_dim": embed_dim,
        "stored_embedding_dim": stored_dim,
        "dim_mismatch": dim_mismatch,
        "embedding_model": embed_model,
        "embedding_model_mismatch": embed_model_mismatch,
        "lancedb_versions": versions,
        "index_state": index_state,
    }
