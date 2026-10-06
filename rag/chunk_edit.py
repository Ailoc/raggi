"""分块编辑与重向量化：改原文 → 重算向量 → 更新；含 FTS 一致性治理与手动新增。

修复：ordinal 取 max(ordinal)+1（删除块后 count_rows 会变小导致冲突）；
新增：批量编辑（一次读取、一次 embed、一次 merge_insert 落地）。
所有写操作走 store.write_lock()，与入库并发安全。
"""
from __future__ import annotations

import datetime
import logging
import uuid

from rag.core.errors import EditConflict
from rag.models.embeddings import Embedder
from rag.parsing.segment import segment, segment_batch
from rag.storage.repos import (
    delete_chunk,
    escape_sql,
    fetch_rows,
    get_document,
    set_doc_fields,
    update_chunk_text,
    upsert_chunks,
)
from rag.storage.sql import scalar
from rag.storage.tables import LanceStore

logger = logging.getLogger("raggi.chunk_edit")

# 无 doc_id 的手动分块归入的虚拟"便签"文档（DESIGN §8.2）
# 零引用：add_manual_chunk 现在用 doc_id="" 表示独立分块，不再塞进
# 虚拟文档（它会污染文档列表，且无法表达「属于哪个知识库」）。
# 旧数据仍可读。确认无外部依赖后可删。
MANUAL_DOC_ID = "manual-notes"


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def get_chunk(store: LanceStore, chunk_id: str) -> dict | None:
    rows = fetch_rows(store.chunks.search().where(
        f"chunk_id = '{escape_sql(chunk_id)}'"))
    if not rows:
        return None
    return {k: scalar(v) for k, v in rows[0].items()}


def touch_doc_count(store: LanceStore, doc_id: str) -> None:
    """按实际 chunk 数回写 documents.chunk_count（两阶段写入的对账补偿）。"""
    if not doc_id:
        return
    try:
        n = int(store.chunks.count_rows(
            filter=f"doc_id = '{escape_sql(doc_id)}'"))
        set_doc_fields(store, doc_id, chunk_count=n,
                       updated_at=_now())
    except Exception as e:  # noqa: BLE001
        # 不抛是对的（分块已经写成功了，不该因为计数失败回滚用户的内容），
        # 但 `pass` 让它**彻底无痕**：documents.chunk_count 从此与实际不符，
        # 列表页与知识库卡片的块数一直虚高，且 /api/health 会永久 degraded，
        # 而日志里找不到任何一条线索。
        logger.warning("回写 chunk_count 失败 doc=%s（计数将与实际不符，"
                       "health 会报 count_mismatch）: %s", doc_id, e)


def _next_ordinal(store: LanceStore, doc_id: str) -> int:
    """该文档当前最大 ordinal + 1（删除块后仍单调，避免冲突）。"""
    try:
        from lancedb.query import ColumnOrdering

        rows = fetch_rows(store.chunks.search().where(
            f"doc_id = '{escape_sql(doc_id)}'").select(
            ["ordinal"]).order_by(
            [ColumnOrdering(column_name="ordinal",
                              ascending=False)]).limit(1))
        if rows:
            return int(rows[0]["ordinal"]) + 1
    except Exception as e:  # noqa: BLE001
        # 回落 0 意味着可能与既有分块撞号（ordinal 是分页与上下文窗口排序的依据）。
        # 这里刻意不抛：读不出 max 时「加一块」比「加不了」对用户更友好，
        # 但必须留痕，否则撞号之后没人知道它是从哪儿来的。
        logger.warning("取 max(ordinal) 失败，回落为 0（可能与既有分块撞号）"
                       " doc=%s: %s", doc_id, e)
    return 0


def edit_chunk(store: LanceStore, embedder: Embedder, chunk_id: str,
               text: str, force: bool = False,
               expect_updated_at: str | None = None) -> dict:
    row = get_chunk(store, chunk_id)
    if row is None:
        raise KeyError("chunk not found")
    if row.get("origin") == "parsed" and not force:
        raise PermissionError(
            "parsed 分块需 force=true 才能编辑（会保留 original_text 并标记 edited）")

    vector = embedder.embed_one(text)
    # 仅当改写导致长度变化时失效偏移（DESIGN §8.3）
    offset_valid = len(text) == len(row.get("text", ""))
    # 保留最初原文用于对比/回滚（DESIGN §8.1）：仅当尚未保留时写入
    original_text = row.get("original_text")
    if original_text is None:
        original_text = row.get("text")
    with store.write_lock():
        # 向量化之后、写库之前再校验一次：并发的另一份编辑可能就发生
        # 在这中间（读取→embedding 是毫秒级窗口，正是冲突高发期）。
        ok = update_chunk_text(
            store, chunk_id,
            text=text, text_seg=segment(text), vector=vector,
            embed_model=embedder.model,
            original_text=original_text, offset_valid=offset_valid,
            expect_updated_at=expect_updated_at,
        )
        if not ok:
            # 向量已白算，但**不能写**：写了就是覆盖别人的修改。
            raise EditConflict(
                f"该分块已被他人修改（你读到的是 {row.get('updated_at', '?')}），"
                "请重新打开确认最新内容后再提交")
        touch_doc_count(store, row.get("doc_id", ""))
    # FTS 一致性治理：小库同步重建；大库累计 stale（由 health 展示）
    n = store.chunks.count_rows()
    if n < 50000:
        store.ensure_fts_index(force=True)
    stale = int(store.chunks.count_rows(filter="fts_stale = true"))
    return {"chunk_id": chunk_id, "edited": True,
            "embed_model": embedder.model, "fts_stale": stale}


def batch_edit_chunks(store: LanceStore, embedder: Embedder,
                      edits: list[dict], force: bool | None = None) -> dict:
    """批量编辑：一次读取、一次 embed、一次 merge_insert 落地（DESIGN §8.2）。

    force 是**批量级默认值**：单项的 force 优先，为 None 时才回落到它。
    早前签名是 force=False，调用方只传 edits（形参形同虚设），而每项
    自带的 force 默认又是 False —— 于是批量编辑解析块必然 403，整条功能
    在界面上不可用。三态（None=未指定）才能让「整批放行」表达得出来。
    """
    if not edits:
        return {"edited": 0}

    def _force_of(item: dict) -> bool:
        f = item.get("force")
        return bool(force) if f is None else bool(f)

    by_id: dict[str, dict] = {}
    for e in edits:
        chunk_id = e.get("chunk_id")
        row = get_chunk(store, chunk_id)
        if row is None:
            raise KeyError(f"chunk not found: {chunk_id}")
        if row.get("origin") == "parsed" and not _force_of(e):
            raise PermissionError(
                f"parsed 分块需 force=true: {chunk_id}")
        by_id[chunk_id] = row

    texts = [e["text"] for e in edits]
    text_segs = segment_batch(texts)
    vectors = embedder.embed(texts)

    rows = []
    for e, seg, vec in zip(edits, text_segs, vectors):
        row = by_id[e["chunk_id"]]
        text = e["text"]
        offset_valid = len(text) == len(row.get("text", ""))
        original_text = row.get("original_text")
        if original_text is None:
            original_text = row.get("text")
        rows.append({
            **row,
            "text": text,
            "text_seg": seg,
            "vector": vec,
            "embed_model": embedder.model,
            "edited": True,
            "original_text": original_text,
            "offset_valid": offset_valid,
            "fts_stale": True,
            "updated_at": _now(),
        })

    with store.write_lock():
        upsert_chunks(store, rows)
        for doc_id in {r["doc_id"] for r in rows}:
            touch_doc_count(store, doc_id)

    n = store.chunks.count_rows()
    if n < 50000:
        store.ensure_fts_index(force=True)
    stale = int(store.chunks.count_rows(filter="fts_stale = true"))
    return {"edited": len(rows), "embed_model": embedder.model,
            "fts_stale": stale}


def add_manual_chunk(store: LanceStore, embedder: Embedder, text: str,
                     doc_id: str | None = None,
                     kb_id: str = "") -> str:
    """新增分块。给了 doc_id 归该文档；否则为知识库下的独立分块（doc_id=""）。

    独立分块不再塞进 MANUAL_DOC_ID 虚拟文档：虚拟文档会污染文档列表，
    且无法表达「这个块属于哪个知识库」。旧 MANUAL_DOC_ID 数据仍可读。

    给了 doc_id 时必须校验文档存在——否则分块挂在一个 documents 里
    没有的行上，成为孤儿，health 的 orphan_chunks 永远大于 0。
    """
    chunk_id = str(uuid.uuid4())
    vector = embedder.embed_one(text)
    doc_id = doc_id or ""
    if doc_id and get_document(store, doc_id) is None:
        raise KeyError(f"document not found: {doc_id}")
    row = {
        "chunk_id": chunk_id,
        "doc_id": doc_id,
        "ordinal": 0,
        "kb_id": kb_id,
        "text": text,
        "text_seg": segment(text),
        "heading_path": "",
        "page": None,
        "char_start": 0,
        "char_end": len(text),
        "token_count": max(1, len(text) // 4),
        "origin": "manual",
        "edited": False,
        "enabled": True,
        "original_text": None,
        "offset_valid": True,
        "fts_stale": False,
        "embed_model": embedder.model,
        "vector": vector,
        "created_at": _now(),
        "updated_at": _now(),
    }
    with store.write_lock():
        # ordinal 必须在锁内取：读 max(ordinal)+1 与写入若不原子，
        # 两个并发的「新增分块」会拿到同一个值 → ordinal 冲突，
        # 而分页排序依赖它唯一，翻页会出现重复或漏项。
        row["ordinal"] = _next_ordinal(store, doc_id)
        upsert_chunks(store, [row])
        touch_doc_count(store, doc_id)
    n = store.chunks.count_rows()
    if n < 50000:
        store.ensure_fts_index(force=True)
    return chunk_id


def set_chunk_enabled(store: LanceStore, chunk_id: str,
                      enabled: bool) -> dict:
    """停用/启用分块。停用只是不参与检索，数据保留可恢复。

    比删除更适合「暂时排除某段内容」：Dify / RAGFlow 都以 disable 为主张。
    """
    row = get_chunk(store, chunk_id)
    if row is None:
        raise KeyError("chunk not found")
    # 与其余写路径一致地持锁，避免与并发入库/编辑争抢同一块
    with store.write_lock():
        store.chunks.update(
            where=f"chunk_id = '{escape_sql(chunk_id)}'",
            values={"enabled": bool(enabled), "updated_at": _now()})
    return {"chunk_id": chunk_id, "enabled": bool(enabled)}


def set_chunks_enabled(store: LanceStore, chunk_ids: list[str],
                       enabled: bool) -> dict:
    """批量停用/启用。

    与单条路径的关键差别是**原子性**：逐条调用意味着用户点了「停用 200 块」
    却只成功 87 条，界面很难说清现在是什么状态。这里一次写入完成，
    全部成功或全部不动。

    语义与单条一致：停用只是不参与检索，数据保留可恢复。
    """
    ids = [c for c in dict.fromkeys(str(i) for i in chunk_ids) if c]
    if not ids:
        return {"updated": 0, "enabled": bool(enabled)}
    known = [c for c in ids if get_chunk(store, c) is not None]
    if len(known) != len(ids):
        missing = [c for c in ids if c not in known]
        raise KeyError(f"chunk not found: {', '.join(missing[:5])}"
                       + (f" 等 {len(missing)} 个" if len(missing) > 5 else ""))
    ts = _now()
    with store.write_lock():
        for cid in known:
            store.chunks.update(
                where=f"chunk_id = '{escape_sql(cid)}'",
                values={"enabled": bool(enabled), "updated_at": ts})
    return {"updated": len(known), "enabled": bool(enabled)}


def remove_chunk(store: LanceStore, chunk_id: str) -> None:
    # 先取 doc_id：删除后无法再从表里查到，回写计数必须提前拿到归属。
    row = get_chunk(store, chunk_id)
    with store.write_lock():
        delete_chunk(store, chunk_id)
        # 回写 chunk_count——漏掉会让 health 的 count_mismatch 永久
        # degraded，且列表/知识库卡片块数虚高。
        touch_doc_count(store, str(row.get("doc_id", "")) if row else "")
