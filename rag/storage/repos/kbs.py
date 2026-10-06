"""知识库 CRUD。

文档归属是 documents 表上的**真实列** `kb_id`（曾塞在 meta JSON 里，
导致无法下推 SQL、只能全表扫描后逐行解析）。计数用 SQL 过滤后聚合。
"""
from __future__ import annotations

import logging
import uuid

from ..sql import escape_sql, fetch_rows, now_iso, only_cols, scalar, scalar_rows
from ..tables import LanceStore
from ._engine import meta_of as _meta
from .chunks import delete_kb_chunks
from .documents import delete_document, docs_query

logger = logging.getLogger("raggi.repos.kbs")

# 只取知识库行需要的列：`meta` 不在里面（kbs 表没有 meta，但曾经列表查询
# 是「整行取回」，加列时形状会静默变化）。显式列名 = 契约。
KB_COLS = ["kb_id", "name", "description", "chunk_size", "overlap_ratio",
           "created_at", "updated_at"]

def _kb_allow(store: LanceStore) -> set[str]:
    """kbs 的合法投影列。用代码声明的列清单，而不是实时探测 schema：
    表结构在升级过程中可能落后于代码，拿 schema 当白名单会把正常读打挂。"""
    return set(KB_COLS)

def _row(kb: dict, doc_count: int = 0,
         chunk_count: int = 0) -> dict:
    size = int(kb.get("chunk_size") or 0)
    return {
        "kb_id": kb["kb_id"],
        "name": kb["name"],
        "description": kb.get("description", ""),
        "doc_count": doc_count,
        "chunk_count": chunk_count,
        # 分块方案：custom=False 表示继承全局
        "chunk_size": size,
        "overlap_ratio": float(kb.get("overlap_ratio") or 0.0),
        "created_at": kb.get("created_at", ""),
        "updated_at": kb.get("updated_at", ""),
    }

def _kb_counts(store: LanceStore) -> dict[str, list[int]]:
    """kb_id → [文档数, 分块数]。

    分块数**用 documents.chunk_count 求和**而不是去 chunks 表数：
    后者是一次全 chunk 扫描（10 万块时上界 ~220ms）。chunk_count 的漂移
    由 /api/health 报出、POST /api/reconcile 修复（这条口径与改前一致）。
    """
    per: dict[str, list[int]] = {}
    rows = docs_query(store, ["kb_id", "chunk_count"])
    for d in rows:
        slot = per.setdefault(str(d.get("kb_id") or ""), [0, 0])
        slot[0] += 1
        slot[1] += int(d.get("chunk_count") or 0)
    return per

def list_kbs(store: LanceStore) -> list[dict]:
    """知识库列表（带文档数 / 分块数，按更新时间倒序）。

    排序下推 + 显式窄列：改前是 `search()` 取全表（整行，含所有列）再
    Python 排序；现在只投影需要的 7 列，排序交给 Lance。
    """
    mdb = _meta(store)
    if mdb is not None:
        kbs = mdb.query(
            f"SELECT {', '.join(KB_COLS)} FROM kbs"
            " ORDER BY updated_at DESC, kb_id")
        per_doc = _kb_counts(store)
        return [_row(kb, *per_doc.get(str(kb["kb_id"]), [0, 0]))
                for kb in kbs]
    kbs = scalar_rows(store.kbs, cols=KB_COLS,
                      order_by=[("updated_at", False), ("kb_id", True)])
    per_doc = _kb_counts(store)
    return [_row(kb, *per_doc.get(str(kb["kb_id"]), [0, 0])) for kb in kbs]

def get_kb(store: LanceStore, kb_id: str) -> dict | None:
    mdb = _meta(store)
    if mdb is not None:
        kb_row = mdb.query_one(
            f"SELECT {', '.join(KB_COLS)} FROM kbs WHERE kb_id=?", (kb_id,))
        if kb_row is None:
            return None
        kb = {k: scalar(v) for k, v in kb_row.items()}
        docs = mdb.query(
            "SELECT chunk_count FROM documents WHERE kb_id=?", (kb_id,))
        return _row(kb, len(docs),
                    sum(int(d.get("chunk_count") or 0) for d in docs))
    rows = scalar_rows(store.kbs, cols=KB_COLS,
                       where=f"kb_id = '{escape_sql(kb_id)}'", limit=1)
    if not rows:
        return None
    kb = {k: scalar(v) for k, v in rows[0].items()}
    docs = docs_query(store, ["chunk_count"], eq={"kb_id": kb_id})
    return _row(kb, len(docs),
                sum(int(d.get("chunk_count") or 0) for d in docs))

def create_kb(store: LanceStore, name: str,
              description: str = "",
              chunk_size: int = 0,
              overlap_ratio: float = 0.0,
              default_size: int = 512,
              default_ratio: float = 0.0) -> dict:
    from ..plan import clamp_ratio, clamp_size

    kb_id = str(uuid.uuid4())
    now = now_iso()
    size = clamp_size(chunk_size)
    ratio = clamp_ratio(overlap_ratio)
    if size <= 0:
        # 两级模型：知识库始终持有具体值。未指定时用系统默认**物化**，
        # 不留「chunk_size=0 表示继承」这种需要运行时解释的状态。
        size = clamp_size(default_size) or 512
        if ratio <= 0:
            ratio = clamp_ratio(default_ratio)
    row = {"kb_id": kb_id, "name": name, "description": description,
           "chunk_size": size, "overlap_ratio": ratio,
           "created_at": now, "updated_at": now}
    mdb = _meta(store)
    if mdb is not None:
        mdb.upsert("kbs", [row], "kb_id")
    else:
        store.kbs.add([row])
    return _row({"kb_id": kb_id, "name": name,
                 "description": description,
                 "chunk_size": size, "overlap_ratio": ratio,
                 "created_at": now, "updated_at": now})

def update_kb(store: LanceStore, kb_id: str, *,
              name: str | None = None,
              description: str | None = None,
              chunk_size: int | None = None,
              overlap_ratio: float | None = None) -> dict | None:
    from ..plan import clamp_ratio, clamp_size

    row = get_kb(store, kb_id)
    if row is None:
        return None
    values = {"updated_at": now_iso()}
    if name is not None:
        values["name"] = name
    if description is not None:
        values["description"] = description
    if chunk_size is not None:
        values["chunk_size"] = clamp_size(chunk_size)
    if overlap_ratio is not None:
        values["overlap_ratio"] = clamp_ratio(overlap_ratio)
    mdb = _meta(store)
    if mdb is not None:
        sets = ", ".join(f"{k}=?" for k in values)
        mdb.execute(f"UPDATE kbs SET {sets} WHERE kb_id=?",
                    (*values.values(), kb_id))
    else:
        store.kbs.update(where=f"kb_id = '{escape_sql(kb_id)}'",
                         values=values)
    updated = get_kb(store, kb_id)
    return updated

def kbs_query(store: LanceStore, cols: list[str], *,
              eq: dict | None = None,
              order_by: list[tuple[str, bool]] | None = None,
              limit: int | None = None) -> list[dict]:
    """唯一的 kbs 读入口（同 docs_query 的理由：两个 SQL 方言只在一处知道）。

    `cols` 过白名单：列名做不成 SQL 占位符，投影是唯一能改写查询本体的位置。
    """
    mdb = _meta(store)
    cols = only_cols(cols, _kb_allow(store), table="kbs")
    if mdb is not None:
        conds, params = [], []
        for col, val in (eq or {}).items():
            conds.append(f"{col}=?")
            params.append(val)
        sql = f"SELECT {', '.join(cols)} FROM kbs"
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        if order_by:
            sql += " ORDER BY " + ", ".join(
                f"{col} {'ASC' if asc else 'DESC'}" for col, asc in order_by)
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        return mdb.query(sql, tuple(params))
    where = " AND ".join(f"{col} = '{escape_sql(val)}'"
                         for col, val in (eq or {}).items()) or None
    return scalar_rows(store.kbs, cols=list(cols), where=where,
                       order_by=order_by, limit=limit)

def set_kb_fields(store: LanceStore, kb_id: str, **values) -> None:
    """更新 kbs 一行的若干字段（引擎分派）。"""
    if not values:
        return
    mdb = _meta(store)
    if mdb is not None:
        sets = ", ".join(f"{col}=?" for col in values)
        mdb.execute(f"UPDATE kbs SET {sets} WHERE kb_id=?",
                    (*values.values(), kb_id))
        return
    store.kbs.update(where=f"kb_id = '{escape_sql(kb_id)}'", values=values)

def delete_kb(store: LanceStore, kb_id: str, backend=None) -> bool:
    """删除知识库并级联删除其文档、分块与留档原文。"""
    row = get_kb(store, kb_id)
    if row is None:
        return False
    # 走 doc_ids_in_kb（它会分派到当前真源）。以前这里自己拼一次 Lance 查询：
    # 元数据搬到 SQLite 之后就查不到行，库删了但文档行留在原地成孤儿，
    # 而孤儿文档仍会被 /api/documents 列出来。
    for did in doc_ids_in_kb(store, kb_id):
        delete_document(store, did, backend=backend)
    # 收尾：清掉该库的独立分块（doc_id=""）。
    # 它们不属于任何文档，上面的 delete_document 覆盖不到；漏删会留下
    # 挂在已删除 kb_id 下的数据，且仍会被 kb_id 检索下推命中。
    delete_kb_chunks(store, kb_id)
    mdb = _meta(store)
    if mdb is not None:
        mdb.execute("DELETE FROM kbs WHERE kb_id=?", (kb_id,))
    else:
        store.kbs.delete(where=f"kb_id = '{escape_sql(kb_id)}'")
    return True

def doc_ids_in_kb(store: LanceStore, kb_id: str) -> list[str]:
    """该知识库下的文档 id 列表（删除知识库时级联清理用）。"""
    rows = docs_query(store, ["doc_id"], eq={"kb_id": kb_id})
    return [str(r.get("doc_id")) for r in rows]

def reconcile_doc_counts(store: LanceStore) -> int:
    """按实际分块数回写全部 documents.chunk_count，返回修复条数。

    删除分块等路径可能漏掉计数回写，导致 /api/health 长期 degraded。
    """
    actual: dict[str, int] = {}
    for r in fetch_rows(store.chunks.search().select(["doc_id"])):
        did = str(r.get("doc_id") or "")
        actual[did] = actual.get(did, 0) + 1
    fixed = 0
    for r in docs_query(store, ["doc_id", "chunk_count"]):
        want = actual.get(str(r.get("doc_id") or ""), 0)
        if int(r.get("chunk_count") or 0) != want:
            store.documents.update(
                where=f"doc_id = '{escape_sql(r['doc_id'])}'",
                values={"chunk_count": want, "updated_at": now_iso()})
            fixed += 1
    return fixed

