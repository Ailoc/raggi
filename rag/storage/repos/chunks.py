"""分块仓储：chunks 表的全部读写。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..sql import (
    chunk_defaults,
    escape_sql,
    fill_missing,
    now_iso,
    quote_in,
    scalar_rows,
    table_columns,
)

if TYPE_CHECKING:
    from ..tables import LanceStore

# 列表列：原文对照需要 page/char_* 做定位，offset_valid 决定偏移是否可信
LIST_COLS = ["chunk_id", "doc_id", "kb_id", "ordinal", "origin", "edited",
             "enabled", "token_count", "heading_path", "text",
             "created_at", "updated_at",
             "page", "char_start", "char_end", "offset_valid"]

# 文档详情用的「分块索引」列：刻意不含 vector，updated_at 供前端回填编辑框
DOC_INDEX_COLS = ["chunk_id", "doc_id", "ordinal", "heading_path", "page",
                  "text", "updated_at"]
# 检索上下文窗口用的列：只要 ordinal + 正文
CONTEXT_COLS = ["doc_id", "ordinal", "text"]


def _fill_chunk_defaults(store: "LanceStore", rows: list[dict]) -> list[dict]:
    return fill_missing(rows, table_columns(store.chunks),
                        chunk_defaults(store.dim), blob_col="vector")


def upsert_chunks(store: "LanceStore", rows: list[dict]) -> None:
    """按 chunk_id 幂等写入/更新分块。"""
    if not rows:
        return
    (store.chunks.merge_insert("chunk_id")
     .when_matched_update_all()
     .when_not_matched_insert_all()
     .execute(_fill_chunk_defaults(store, rows)))


def update_chunk_text(
    store: "LanceStore",
    chunk_id: str,
    *,
    text: str,
    text_seg: str,
    vector: list[float],
    embed_model: str,
    original_text: str | None = None,
    offset_valid: bool = False,
    expect_updated_at: str | None = None,
) -> bool:
    """编辑分块原文并重新向量化（一次 update 落地）。

    original_text 仅在「首次编辑」时由调用方传入（非 None），
    后续编辑传 None 以保留最初原文。

    expect_updated_at 给定时做**乐观并发**校验：条件里带上调用方
    读到的 updated_at，行已被别人改过就更新 0 行——返回 False 让调用方
    报 409，而不是静默覆盖别人的修改。未提供该参数时恒为 True（沿用
    「最后写入者胜」的旧行为，便于脚本与旧客户端不受影响）。
    """
    values: dict = {
        "text": text,
        "text_seg": text_seg,
        "vector": vector,
        "embed_model": embed_model,
        "edited": True,
        "offset_valid": offset_valid,
        "fts_stale": True,          # 待重建 FTS（ensure_fts_index 负责清除）
        "updated_at": now_iso(),
    }
    if original_text is not None:
        values["original_text"] = original_text
    where = f"chunk_id = '{escape_sql(chunk_id)}'"
    if expect_updated_at:
        where += f" AND updated_at = '{escape_sql(expect_updated_at)}'"
        # LanceDB 返回 UpdateResult（rows_updated / version），不是整数
        return store.chunks.update(where=where,
                                   values=values).rows_updated > 0
    store.chunks.update(where=where, values=values)
    return True


def delete_chunk(store: "LanceStore", chunk_id: str) -> None:
    store.chunks.delete(where=f"chunk_id = '{escape_sql(chunk_id)}'")


def delete_kb_chunks(store: "LanceStore", kb_id: str) -> None:
    """清掉某知识库的全部分块。

    删除知识库时**必须**调用：文档分块会随 delete_document 被清掉，
    但 `doc_id=""` 的独立分块不属于任何文档，漏删会留下挂在已删除
    kb_id 下、且仍能被检索命中的数据。
    """
    store.chunks.delete(where=f"kb_id = '{escape_sql(kb_id)}'")


def get_chunk(store: "LanceStore", chunk_id: str) -> dict | None:
    rows = scalar_rows(store.chunks, cols=LIST_COLS,
                       where=f"chunk_id = '{escape_sql(chunk_id)}'", limit=1)
    if not rows:
        return None
    from ..sql import scalar_row

    return scalar_row(rows[0])


def doc_chunk_index(store: "LanceStore", doc_id: str) -> list[dict]:
    """某文档的分块索引（含正文，不含向量），按 ordinal 升序。

    排序交给 Lance 下推（`order_by`）而不是取回后 `rows.sort(...)`：
    投影 + 排序 + 限量都确定在 Rust 侧完成。
    """
    return scalar_rows(store.chunks, cols=DOC_INDEX_COLS,
                       where=f"doc_id = '{escape_sql(doc_id)}'",
                       order_by=[("ordinal", True)])


def contexts_for(store: "LanceStore", ranges: dict[str, tuple[int, int]]
                 ) -> dict[str, dict[int, str]]:
    """按文档批量取上下文窗口：doc_id → {ordinal: text}。

    `ranges` 是每篇文档需要的 [lo, hi] 闭区间（由调用方按命中行的 ordinal
    ±window 合并得到）。**一次**查回全部涉及的文档——以前这里是
    「每篇文档一次查询」，一页 8 条命中跨 8 篇文档就是 8 × ~5ms。

    排序列与过滤列都要出现在投影里（Lance 的原生 scanner 有这个要求），
    所以 doc_id/ordinal 一并 select。
    """
    items = [(d, lo, hi) for d, (lo, hi) in ranges.items() if d]
    if not items:
        return {}
    ids = quote_in([d for d, _, _ in items])
    per_doc = " OR ".join(
        f"(doc_id = '{escape_sql(d)}' AND ordinal BETWEEN {int(lo)} AND {int(hi)})"
        for d, lo, hi in items)
    rows = scalar_rows(store.chunks, cols=CONTEXT_COLS,
                       where=f"doc_id IN ({ids}) AND ({per_doc})")
    out: dict[str, dict[int, str]] = {}
    for r in rows:
        out.setdefault(str(r.get("doc_id") or ""), {})[int(r["ordinal"])] = \
            r.get("text") or ""
    return out


def texts_by_id(store: "LanceStore", chunk_ids) -> dict[str, str]:
    """按 chunk_id 批量取正文（问答拼装上下文用，一次查完）。"""
    ids = [str(c) for c in dict.fromkeys(chunk_ids) if c]
    if not ids:
        return {}
    rows = scalar_rows(store.chunks, cols=["chunk_id", "text"], ids=ids,
                       id_column="chunk_id")
    return {str(r["chunk_id"]): (r.get("text") or "") for r in rows}
