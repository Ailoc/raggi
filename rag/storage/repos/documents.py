"""文档仓储：documents 表的全部读写。

`kb_id` 与 `stored_file` 是**真实列**（曾塞在 meta JSON 里，导致 20 处
防御式解析、且无法下推 SQL）。meta 现在只承载 chunking 这类嵌套结构。

引擎分派：`store.meta` 存在时 documents 走 SQLite(WAL)，否则走 LanceDB
（`storage.meta_engine=lancedb` 的回退路径）。documents 是 OLTP 画像最重的
一张表——点查、按库分页、改标题、移动知识库，实测在 LanceDB 上
点查 5.68ms / 分页 6.76ms，在 SQLite 上是 0.008ms / 0.100ms。

**正文 `text` 只在详情读**：它曾是 LanceDB 行里最大的一列（上限 20MB/篇），
列表查询把它一起 materialize 就是灾难。两边都保持「列表不取 text」。
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from rag.core.errors import Invalid

from ..sql import (
    count_rows,
    doc_defaults,
    escape_like,
    escape_sql,
    fill_missing,
    now_iso,
    only_cols,
    scalar_row,
    scalar_rows,
    table_columns,
)
from ._engine import meta_of as _meta

if TYPE_CHECKING:
    from ..tables import LanceStore

logger = logging.getLogger("raggi.repos.documents")

# 列表/详情返回的列。刻意不含向量与 text：详情单独取，避免列表响应过大。
LIST_COLS = ["doc_id", "title", "mime", "parser_engine", "chunk_count",
             "status", "kb_id", "stored_file", "created_at", "meta"]
DETAIL_COLS = ["doc_id", "title", "source_uri", "mime", "parser_engine",
               "content_hash", "char_count", "chunk_count", "text", "status",
               "error", "kb_id", "stored_file", "meta", "created_at",
               "updated_at"]
# 入库去重只需要这几列就能判定命中了哪篇文档；**绝不含 text**（见 find_by_hash）
DEDUP_COLS = ["doc_id", "title", "chunk_count", "status", "kb_id"]

def _fill_doc_defaults(store: "LanceStore", rows: list[dict]) -> list[dict]:
    return fill_missing(rows, table_columns(store.documents), doc_defaults())

def _sql_where(**eq) -> tuple[str, tuple]:
    """等值条件拼成参数化 WHERE（None 的键跳过）。"""
    parts, vals = [], []
    for k, v in eq.items():
        if v is not None:
            parts.append(f"{k}=?")
            vals.append(v)
    return (" WHERE " + " AND ".join(parts) if parts else ""), tuple(vals)

def upsert_documents(store: "LanceStore", rows: list[dict]) -> None:
    """按 doc_id 幂等写入/更新文档。"""
    if not rows:
        return
    meta = _meta(store)
    if meta is not None:
        meta.upsert("documents", [
            {k: r.get(k) for k in DETAIL_COLS} for r in rows], "doc_id")
        return
    (store.documents.merge_insert("doc_id")
     .when_matched_update_all()
     .when_not_matched_insert_all()
     .execute(_fill_doc_defaults(store, rows)))

def get_document(store: "LanceStore", doc_id: str) -> dict | None:
    rows = docs_query(store, DETAIL_COLS, eq={"doc_id": doc_id}, limit=1)
    return scalar_row(rows[0]) if rows else None

def get_documents(store: "LanceStore", doc_ids, cols=None) -> dict[str, dict]:
    """批量取多篇文档的指定列，返回 doc_id → row。

    收在这里而不是各调用点自己拼 IN()：检索层要标题、对账要计数，
    以前是「每篇文档一次查询」，一页 8 条就是 8 次 ~5ms 的往返（实测 N+1）。
    """
    ids = [str(d) for d in dict.fromkeys(doc_ids) if d]
    if not ids:
        return {}
    rows = docs_query(store, cols or ["doc_id", "title"], ids=ids)
    return {str(r.get("doc_id")): scalar_row(r) for r in rows}

def find_by_hash(store: "LanceStore", content_hash: str) -> list[dict]:
    """按内容哈希查重（content_hash 有标量索引）。

    刻意**不取 text 列**：入库的去重检查在写锁临界区内，取回整篇正文
    （上限 MAX_DOC_CHARS = 20MB）等于把锁的持有时间乘以文档大小。
    """
    return docs_query(store, DEDUP_COLS, eq={"content_hash": content_hash})

def list_documents(
    store: "LanceStore",
    *,
    q: str = "",
    status: str = "",
    parser_engine: str = "",
    mime: str = "",
    kb_id: str = "",
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """文档列表：过滤、排序、分页**全部下推**，total 由 count 出。

    改前是「取回全表 LIST_COLS → Python 过滤 → 切片」：实测 2000 行 8.63ms，
    下推后 2.50ms（成本从 O(表行数) 变成 O(页大小)；元数据搬进 SQLite 之后
    这条是 0.100ms）。更重要的是**分页语义第一次是确定的**——显式
    `created_at DESC, doc_id ASC`；不写排序时 offset 依赖 fragment 布局，
    写入或 compaction 之后会漂移、重复或漏项。

    标题子串下推成 `ILIKE`（本机实测对中文有效）。模式串必须过 `escape_like`：
    查询词含 `%` / `_` 时不能当通配符用，否则「子串包含」会静默变成「通配匹配」。
    """
    eq = {k: v for k, v in (("status", status), ("parser_engine", parser_engine),
                            ("mime", mime), ("kb_id", kb_id)) if v}
    contains = ("title", q) if (q or "").strip() else None
    order = [("created_at", False), ("doc_id", True)]
    total = docs_count(store, eq=eq, contains=contains)
    rows = docs_query(store, LIST_COLS, eq=eq, contains=contains,
                      order_by=order, limit=max(1, int(limit)),
                      offset=max(0, int(offset)))
    out = []
    for r in rows:
        item = scalar_row({k: v for k, v in r.items() if k != "meta"})
        item["kb_id"] = str(r.get("kb_id") or "")
        item["has_file"] = bool(r.get("stored_file"))
        out.append(item)
    return out, total

def stored_file(store: "LanceStore", doc_id: str) -> str | None:
    """读 documents.stored_file（上传原文的归档名）。"""

    try:
        rows = scalar_rows(store.documents, cols=["stored_file"],
                           where=f"doc_id = '{escape_sql(doc_id)}'", limit=1)
        if rows:
            return rows[0].get("stored_file") or None
    except Exception as e:  # noqa: BLE001
        logger.debug("读取 stored_file 失败: %s", e)
    return None

def delete_document(store: "LanceStore", doc_id: str, backend=None) -> None:
    """删除文档、其全部分块，以及归档原文。

    归档走 backend 抽象——local 是删文件，s3 是删对象。

    **必须持写锁**：与并发的入库流水线存在真实竞态——pipeline 重切/
    重解析时是「删旧块 → 写新块」两次独立提交，本函数若在两步之间
    执行，新块会写进已被删掉的 documents 行里，成为永久孤儿 chunk，
    health 的 orphan_chunks 恒大于 0 且永不恢复。
    """
    name = stored_file(store, doc_id)
    if name and backend is not None:
        try:
            backend.delete_blob(name)
        except Exception as e:  # noqa: BLE001
            logger.debug("归档清理失败 %s: %s", name, e)
    with store.write_lock():
        store.chunks.delete(where=f"doc_id = '{escape_sql(doc_id)}'")
        _delete_doc_rows(store, [doc_id])

def delete_documents(store: "LanceStore", doc_ids: list[str],
                     backend=None) -> dict:
    """批量删除文档（级联分块与留档原文）。

    与逐条 delete_document 的差别有两点，都是批量场景必需的：
    - **一次加锁**：逐条调用会在每篇文档上各持一次写锁，与并发的
      入库/编辑互相争抢，删 50 篇时窗口期明显变长；
    - **归档先批量取出**：留档文件名在删除后就查不到了，必须在删之前
      一次性读完。

    不存在的 ID 会被跳过（幂等），并在结果里如实报告跳过数量——
    调用方据此提示「其中 N 篇已不存在」，而不是假装全删成功。
    """
    ids = [d for d in dict.fromkeys(str(i) for i in doc_ids) if d]
    if not ids:
        return {"deleted": 0, "skipped": 0}
    # 一次取回，不在循环里逐条查（删 50 篇 = 以前 50 次 ~5ms 的往返）
    found = get_documents(store, ids, cols=["doc_id", "stored_file"])
    names = [str(found[i].get("stored_file") or "")
             for i in found if found[i].get("stored_file")]
    known = list(found)
    if not known:
        return {"deleted": 0, "skipped": len(ids)}
    with store.write_lock():
        for name in names:
            if backend is None:
                continue
            try:
                backend.delete_blob(name)
            except Exception as e:  # noqa: BLE001
                logger.debug("归档清理失败 %s: %s", name, e)
        for doc_id in known:
            store.chunks.delete(where=f"doc_id = '{escape_sql(doc_id)}'")
        _delete_doc_rows(store, known)
    return {"deleted": len(known), "skipped": len(ids) - len(known)}

def _delete_doc_rows(store: "LanceStore", doc_ids: list[str]) -> None:
    """删 documents 行（引擎分派）。"""
    ids = [str(d) for d in doc_ids if d]
    if not ids:
        return
    meta = _meta(store)
    if meta is not None:
        meta.execute(
            f"DELETE FROM documents WHERE doc_id IN "
            f"({', '.join('?' for _ in ids)})", tuple(ids))
        return
    for doc_id in ids:
        store.documents.delete(where=f"doc_id = '{escape_sql(doc_id)}'")

def _update_doc_row(store: "LanceStore", doc_id: str, values: dict) -> None:
    """更新 documents 一行的若干列（引擎分派）。

    形参刻意**不叫** `meta`：`update_metadata` 有个叫 `meta` 的业务参数
    （chunking 方案 JSON），在它里面用 `meta` 指代元数据引擎会静默串位。
    """
    if not values:
        return
    mdb = _meta(store)
    if mdb is not None:
        sets = ", ".join(f"{k}=?" for k in values)
        mdb.execute(f"UPDATE documents SET {sets} WHERE doc_id=?",
                    (*values.values(), doc_id))
        return
    store.documents.update(where=f"doc_id = '{escape_sql(doc_id)}'",
                           values=values)

def set_doc_fields(store: "LanceStore", doc_id: str, **values) -> None:
    """更新 documents 一行的若干字段（公开入口，供 plan / chunk_edit / pipeline 用）。

    存在理由：不指定列清单的 LanceDB `update` 会重写整行，而这行里带着
    整篇正文（上限 20MB）。收成一个入口之后，「只更新要改的列」这件事
    只需要对一个地方负责。
    """
    _update_doc_row(store, doc_id, values)

# ---- 唯一的 documents 读入口 -------------------------------------------
#
# 为什么要有这一层：把 OLTP 读写分派到两个引擎之后，「谁读哪张表」这件事
# 一旦散落到各调用点，就会长出两类很难看的 bug（本轮审计真的踩到了）：
#   ① 观测/统计路径还在读 LanceDB 旧表 ⇒ 新入库一篇，列表说 8 篇、
#      /api/health 说 7 篇并直接报 degraded；
#   ② 每个调用点自己写 `if meta: … else: …` ⇒ 两边各一份 SQL 方言
#      （SQLite 要参数化 `?`，LanceDB 只能拼字符串），漏改一处就分叉。
# 所以现在只有这一个函数知道「两个方言怎么写」，调用方只描述**意图**
# （等值过滤 / 子串过滤 / 排序 / 分页），不再接触 SQL 字符串。
def _doc_allow(store: "LanceStore") -> set[str]:
    """documents 的合法投影列，给 `only_cols` 白名单用。

    用**代码声明的列清单**（三份投影清单的并集）而不是实时 schema：
    升级过程中表结构可能落后于代码（`_ensure_cols` 之后才补齐），
    拿 schema 当白名单会把正常读打挂；而不在这些清单里的列名，
    按定义就是本代码不支持的读法。
    `store` 参数保留是为了调用点对齐，将来若要并入真实 schema 不用改签名。
    """
    return set(DETAIL_COLS) | set(LIST_COLS) | set(DEDUP_COLS)

def docs_query(store: "LanceStore", cols: list[str], *,
               eq: dict | None = None, one_of: dict | None = None,
               contains: tuple[str, str] | None = None,
               ids: list[str] | None = None,
               order_by: list[tuple[str, bool]] | None = None,
               limit: int | None = None,
               offset: int | None = None) -> list[dict]:
    """按意图查 documents 行；返回 list[dict]，列顺序与 `cols` 一致。

    `eq` 等值、`one_of` 值集合（IN）、`contains` 是 (列名, 子串)，
    `ids` 是主键集合。四者可组合（AND）。**值永远不会被拼进 SQL 文本**：
    SQLite 侧走占位符，LanceDB 侧只能拼接、因此统一过 `escape_sql`。

    `cols` 是唯一无法参数化的部分（列名不能做成占位符），所以它要过
    `only_cols` 白名单 —— 否则投影本身就是一条注入通道。
    """
    mdb = _meta(store)
    cols = only_cols(cols, _doc_allow(store), table="documents")
    if mdb is not None:
        conds: list[str] = []
        params: list = []
        for col, val in (eq or {}).items():
            conds.append(f"{col}=?")
            params.append(val)
        for col, vals in (one_of or {}).items():
            vals = list(vals)
            if not vals:
                return []
            conds.append(f"{col} IN ({', '.join('?' for _ in vals)})")
            params.extend(vals)
        if contains:
            col, needle = contains
            needle = str(needle).strip()
            if needle:
                # ESCAPE '\' 是显式声明：LIKE 里的 % / _ 必须是字面量，
                # 否则「子串包含」会静默变成「通配匹配」。
                conds.append(f"{col} LIKE ? ESCAPE '\\'")
                params.append(f"%{escape_like(needle)}%")
        if ids is not None:
            if not ids:
                return []
            conds.append(f"doc_id IN ({', '.join('?' for _ in ids)})")
            params.extend(ids)
        sql = f"SELECT {', '.join(cols)} FROM documents"
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        if order_by:
            sql += " ORDER BY " + ", ".join(
                f"{col} {'ASC' if asc else 'DESC'}" for col, asc in order_by)
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        if offset:
            sql += " OFFSET ?"
            params.append(int(offset))
        return mdb.query(sql, tuple(params))

    where = _lance_eq(eq, one_of, contains, ids)
    return scalar_rows(store.documents, cols=list(cols), where=where,
                       order_by=order_by, limit=limit, offset=offset)

def docs_count(store: "LanceStore", *, eq: dict | None = None,
               one_of: dict | None = None,
               contains: tuple[str, str] | None = None) -> int:
    """documents 计数，条件语义与 `docs_query` 完全一致。"""
    mdb = _meta(store)
    if mdb is not None:
        conds: list[str] = []
        params: list = []
        for col, val in (eq or {}).items():
            conds.append(f"{col}=?")
            params.append(val)
        for col, vals in (one_of or {}).items():
            vals = list(vals)
            if not vals:
                return 0
            conds.append(f"{col} IN ({', '.join('?' for _ in vals)})")
            params.extend(vals)
        if contains:
            col, needle = contains
            needle = str(needle).strip()
            if needle:
                conds.append(f"{col} LIKE ? ESCAPE '\\'")
                params.append(f"%{escape_like(needle)}%")
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        return int(mdb.value(f"SELECT count(*) FROM documents{where}",
                              tuple(params), 0) or 0)
    return count_rows(store.documents, _lance_eq(eq, one_of, contains, None))

def _lance_eq(eq, one_of, contains, ids) -> str | None:
    """把同一份意图翻译成 LanceDB 的 where 字符串（只在这里拼 SQL）。"""
    conds: list[str] = []
    for col, val in (eq or {}).items():
        conds.append(f"{col} = '{escape_sql(val)}'")
    for col, vals in (one_of or {}).items():
        vals = list(vals)
        if not vals:
            return "1=0"
        conds.append(f"{col} IN ({', '.join(chr(39) + escape_sql(v) + chr(39) for v in vals)})")
    if contains:
        col, needle = contains
        needle = str(needle).strip()
        if needle:
            conds.append(f"{col} ILIKE '%{escape_like(needle)}%'")
    if ids:
        conds.append(f"doc_id IN ({', '.join(chr(39) + escape_sql(i) + chr(39) for i in ids)})")
    return " AND ".join(conds) if conds else None

def update_metadata(store: "LanceStore", doc_id: str, *,
                    title: str | None = None,
                    meta: str | None = None,
                    kb_id: str | None = None) -> dict | None:
    """更新文档的可编辑元信息（标题、所属知识库）。

    标题曾完全不可改——粘贴文本的标题取首行前 60 字，写错就永久错了。

    **移动知识库必须同时改两张表**：documents.kb_id 决定它出现在哪个库的
    列表里，chunks.kb_id 决定按库检索时能否命中。只改前者会出现
    「库里有这篇文档，但检索它却搜不到」的分裂状态——而这种不一致
    不会有任何报错，只表现为结果莫名其妙地少。

    kb_id 传 "" 表示移出到「未分组」。None 表示不动（区分「不改」
    与「改成空」这两种语义不同的请求）。
    """
    row = get_document(store, doc_id)
    if row is None:
        return None
    values: dict = {"updated_at": now_iso()}
    if title is not None:
        title = title.strip()
        if not title:
            raise Invalid("标题不能为空")
        values["title"] = title
    if meta is not None:
        values["meta"] = meta

    if kb_id is not None:
        # 延迟导入：kbs.py 依赖本模块取文档，直接 import 会成环
        from .kbs import get_kb

        target = str(kb_id).strip()
        # 目标库必须存在：允许 kb_id 指向已删除的库会留下悬空引用，
        # 文档从此不出现在任何列表里，且没有任何症状提示它去哪了。
        if target and get_kb(store, target) is None:
            raise Invalid(f"知识库不存在: {target}")
        if target == str(row.get("kb_id") or ""):
            # 原地不动也要走一遍（幂等），但不必改分块
            values["kb_id"] = target
            _update_doc_row(store, doc_id, values)
            return get_document(store, doc_id)
        values["kb_id"] = target
        # 与入库/删除同一条并发纪律：读—改—写必须在写锁内
        with store.write_lock():
            _update_doc_row(store, doc_id, values)
            store.chunks.update(where=f"doc_id = '{escape_sql(doc_id)}'",
                                values={"kb_id": target})
        return get_document(store, doc_id)

    _update_doc_row(store, doc_id, values)
    return get_document(store, doc_id)

