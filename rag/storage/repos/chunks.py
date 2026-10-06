"""分块仓储：chunks 表的全部读写。"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pyarrow as pa
import pyarrow.compute as pc

from ..sql import (
    chunk_defaults,
    escape_like,
    escape_sql,
    fetch_rows,
    fill_missing,
    now_iso,
    quote_in,
    scalar_rows,
    table_columns,
)

if TYPE_CHECKING:
    from ..tables import LanceStore

logger = logging.getLogger("raggi.repos.chunks")

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


def chunk_counts_by_doc(store: "LanceStore") -> dict[str, int]:
    """全库分块数按 doc 聚合：一次窄列扫描 + 向量化 value_counts。

    住在 chunks 仓储而不是 `storage/health.py` 里，是因为它有**两个**消费者
    （健康检查与「修复计数」），而第二处原先自己写了一份
    「`fetch_rows(search().select(["doc_id"]))` 然后在 Python 里数」——
    那既把全部分块行 materialize 进内存，又和 health 的口径可能分叉。

    替代两类旧写法：
    - 「取回全部 chunk 行再数」——那会把 text（乃至 vector）带进 Python；
    - 「每个 doc 一次 `count_rows(filter=…)`」——20 篇文档就是 20 次 ~1.2ms 的扫描。
    """
    values = [r["doc_id"] for r in scalar_rows(store.chunks, cols=["doc_id"])]
    vc = pc.value_counts(pa.chunked_array([values]))
    return {str(v): int(c) for v, c in zip(
        vc.field("values").to_pylist(), vc.field("counts").to_pylist())}


def chunk_filters(*, doc_id: str = "", kb_id: str = "",
                  only_standalone: bool = False, q: str = "") -> str | None:
    """把分块列表的过滤条件翻成 where 子句（**SQL 文本只在这层出现**）。

    `doc_id` 与 `kb_id` 是**互斥优先**而不是 AND：调用方给了 doc_id 就只按
    文档取，这是前端文档详情页与知识库列表页共用一个端点的前提。
    """
    conds: list[str] = []
    if doc_id:
        conds.append(f"doc_id = '{escape_sql(doc_id)}'")
    elif kb_id:
        # kb_id 在入库时即写入每个分块，故库下全部块（含独立分块）都能命中
        conds.append(f"kb_id = '{escape_sql(kb_id)}'")
    if only_standalone:
        # doc_id='' 是「独立分块」的存储形态，须下推到 SQL 而非事后过滤
        conds.append("doc_id = ''")
    needle = (q or "").strip()
    if needle:
        # 子串过滤下推成 ILIKE（本机实测对中文有效），并且必须过 escape_like：
        # 查询词含 % 或 _ 时不能当通配符，否则「包含 foo」会变成「foo 开头」。
        conds.append(f"text ILIKE '%{escape_like(needle)}%'")
    return " AND ".join(conds) if conds else None


def chunks_page(store: "LanceStore", *, doc_id: str = "", kb_id: str = "",
                only_standalone: bool = False, q: str = "",
                limit: int = 100, offset: int = 0
                ) -> tuple[list[dict], int]:
    """分块列表页：过滤、排序、分页、计数**全部下推**。

    为什么收在仓储层：改前这个端点自己拼 where 再调 `scalar_rows` /
    `count_rows`——「只有 storage 层碰 SQL 文本」这条纪律就被绕过了，
    而绕过的代价不是立刻出错，是**下一处过滤条件各写一份**（转义漏一处
    就是查询结果为空或通配符误命中，且都不报错）。

    次级排序键必须是 chunk_id：`ordinal` **只在单文档内唯一**，按库或
    不带条件列时它会大量重复，只按 ordinal 排序的分页在并列处会重复/漏项。
    """
    from ..sql import count_rows, scalar_rows

    n = max(1, int(limit))
    where = chunk_filters(doc_id=doc_id, kb_id=kb_id,
                          only_standalone=only_standalone, q=q)
    rows = scalar_rows(store.chunks, cols=LIST_COLS, where=where,
                       order_by=[("ordinal", True), ("chunk_id", True)],
                       limit=n, offset=int(offset) or None)
    return rows, count_rows(store.chunks, where)


# 检索命中行真正需要返回的列。必须显式投影：不写时 LanceDB 返回**全部列**，
# 其中包括 `vector`（1024 维 float）与 `text_seg`（jieba 分词串）——
# 实测 20k 块 / candidate_k=50 时单行 dict 约 22.8 KB，向量占绝对大头。
# 分数列**不在**这个列表里，却会出现在返回的行 dict 中：Lance 的
# `scoring autoprojection` 在显式投影缺 `_distance`/`_score` 时把它们补回来。
# 这是一条**依赖**而不是巧合 —— `retrieval/search.py:_score_of` 读的正是它们，
# 而 scanner 每次查询都在为此打一条 Rust Deprecation 警告：
# 「将来不再自动补，请调 `disable_scoring_autoprojection` 采纳新行为」。
# 所以两点必须知道：
#   1. **不要**为了消除警告去调那个开关 —— 分数列会消失，`_score_of` 的
#      兜底 `return 0.0, "none"` 让它静默退化（排序、score_threshold、
#      score_kind 一起说谎），这正是本文件反复在防的「结果悄悄不对」。
#   2. 升级 lance/lancedb 时如果这个行为变了，会由
#      `tests/test_search_api.py::test_score_kind_matches_mode`
#      （vector 必须拿到 `cosine_similarity`）先变红，而不是等到线上静默。
SEARCH_COLS = ["chunk_id", "doc_id", "kb_id", "ordinal", "heading_path", "page",
               "text", "char_start", "char_end", "offset_valid",
               "edited", "enabled", "origin"]


def _in_clause(column: str, ids) -> str:
    """值集合 → SQL IN (…)。**空集合绝不能写成 `IN ()`**：那是语法错误，
    而且若被上层 except 吞掉就会退化成「不加这个过滤」，调用方以为过滤过了。
    """
    return f"{column} IN (" + ", ".join(
        f"'{escape_sql(i)}'" for i in ids) + ")"


def chunk_prefilter(store: "LanceStore", filters: dict) -> str | None:
    """把检索过滤条件翻成 chunks 的 where 子句（SQL 文本只在这层出现）。

    这里守着的两件事都**不会报错**，所以必须集中在一个地方：

    - `enabled = true`：停用的分块不参与检索（数据保留、可恢复）。
    - mime / parser_engine 这两列在 **documents** 上，要先解析成 doc_id 集合
      再套到 chunks 上；解析失败或解析出空集合时**必须产生恒假条件**，
      绝不能跳过该过滤——改前 `if ids is not None` 就是这个意思：
      传 `mime=application/pdf` 却返回全库结果，调用方看不出来。

    `store` 是显式参数。改前它被塞进 `filters["_store"]` 一路带下来，
    那是一种隐式通道：filters 同时是「用户可传的过滤条件」和
    「内部携带的对象」，任何人把 filters 原样回显或转发都会带出内部状态。
    """
    conds: list[str] = []
    if filters.get("include_disabled") is not True:
        conds.append("enabled = true")
    if filters.get("origin"):
        conds.append(f"origin = '{escape_sql(filters['origin'])}'")
    if filters.get("kb_id"):
        conds.append(f"kb_id = '{escape_sql(filters['kb_id'])}'")
    if filters.get("doc_ids"):
        conds.append(_in_clause("doc_id", filters["doc_ids"]))
    # DESIGN §7：mime / parser_engine 过滤。
    for key in ("mime", "parser_engine"):
        val = filters.get(key)
        if not val:
            continue
        vals = val if isinstance(val, (list, tuple)) else [val]
        ids = doc_ids_by_attr(store, key, vals)
        # 解析失败（ids 为 None）与解析出空集合，都必须产生**恒假条件**。
        # 改前这里是 `if ids is not None`，于是查询异常退化成「不加该过滤」——
        # 传 mime=application/pdf 却返回全库结果，调用方会以为已经过滤过了。
        # 宁严勿松：返回 0 条是可见的空结果，返回全库是不可见的错误结果。
        conds.append(_in_clause("doc_id", ids) if ids
                     else "doc_id IN ('__no_match__')")
    return " AND ".join(conds) if conds else None


def doc_ids_by_attr(store: "LanceStore", column: str, vals) -> list[str] | None:
    """按 documents 的列（mime / parser_engine）取 doc_id 集合；None = 读不了。"""
    if store is None:
        return None
    # 走仓储层的统一读入口：这些列现在住在元数据引擎里，
    # 自己拼 where 会读错引擎（元数据搬到 SQLite 后过滤条件恒空）。
    from .documents import docs_query

    try:
        rows = docs_query(store, ["doc_id"], one_of={column: list(vals)})
    except Exception:  # noqa: BLE001
        logger.warning("按 %s 解析 doc_id 失败", column, exc_info=True)
        return None
    return [str(r["doc_id"]) for r in rows]


def search_chunks(store: "LanceStore", *, mode: str = "vector",
                  vector=None, text: str = "", where: str | None = None,
                  limit: int = 50, nprobes: int = 20, refine_factor: int = 10,
                  rrf_k: int | None = None, native_reranker=None) -> list[dict]:
    """一次检索查询（vector / fts / hybrid），返回命中行（含分数列）。

    hybrid 走 LanceDB **原生一次查询**（`query_type="hybrid"`），
    不是「向量 + FTS 两条各自查完再合并」——审计时确认过这点，
    所以「把三条通道并发化」这个看起来显然的优化**并不适用**。

    失败时抛给调用方而不是就地降级：降级成 vector 是**检索策略**
    （要写进响应的 `degraded` 字段告诉调用方结果口径变了），
    不属于存储层该做的决定。
    """
    from lancedb.rerankers import RRFReranker

    tbl = store.chunks
    if mode == "hybrid":
        qb = tbl.search(query_type="hybrid").vector(vector).text(text)
        if where:
            qb = qb.where(where, prefilter=True)
        qb = qb.limit(limit).nprobes(nprobes).refine_factor(refine_factor)
        if native_reranker is not None:
            qb = qb.rerank(native_reranker)
        else:
            # 应用层精排在召回之后做；融合仍然用 RRF，保证两路召回被正确归一
            qb = qb.rerank(RRFReranker(K=rrf_k or limit))
        return _exec(qb)
    if mode == "fts":
        qb = tbl.search(text, query_type="fts")
        if where:
            qb = qb.where(where, prefilter=True)
        return _exec(qb.limit(limit))
    qb = tbl.search(vector, vector_column_name="vector")
    if where:
        qb = qb.where(where, prefilter=True)
    return _exec(qb.limit(limit).nprobes(nprobes).refine_factor(refine_factor))


def _exec(qb) -> list[dict]:
    return fetch_rows(qb.select(SEARCH_COLS))


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
