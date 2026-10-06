"""存储层共用的 SQL / 行处理工具。

集中四处容易写错的地方：
1. **转义**：where 子句里的字符串字面量必须过 `escape_sql`（单引号翻倍），
   LIKE 模式串必须过 `escape_like`（`%` / `_` 是通配符），否则要么拼出非法 SQL、
   要么把「子串包含」变成「通配匹配」这种静默的语义漂移。
2. **取行**：优先 `to_list()`，避免 pandas DataFrame 的构造开销。
3. **标量读走 lance 原生 scanner**：`scalar_rows()`。收益拆成两块，别混着讲
   （`rag-bench scan-paths` 在本机同一列集下重测过，第二块其实很小）：
   - **排序 + 分页下推**才是大的那块：2000 行 10 列「全表取回再切片」8.63ms，
     `order_by + limit 50` 下推 2.50ms，而且成本从 O(表行数) 变成 O(页大小)；
   - 同列集下「原生 scanner vs lancedb 查询构建器」只是 ~1.2–1.5×（常常打平），
   - 两块都小于换引擎的收益：同样的列表查询在 SQLite 上是 **0.100ms**
     （`storage/meta.py`），因为剩下的那点开销是 Lance 的**每查询固定成本**，
     只有 7 行的表也要 ~6.8ms（诊断报告 §2.2）。
   向量 / FTS / hybrid 检索**不走这里**，仍用 lancedb——那是它值钱的地方。
4. **补齐缺列**：LanceModel 的带默认值字段在 Arrow schema 里仍是 non-nullable，
   merge_insert 遇到缺列直接报错（partial-schema）。统一补齐，调用点就不必记得写全
   所有列——**新增列也不会打挂老调用点**。
"""
from __future__ import annotations

import datetime
import logging

logger = logging.getLogger("raggi.sql")

# 默认值缓存：构造 LanceModel 较慢，按维度/表缓存一次
_CHUNK_DEFAULTS: dict[int, dict] = {}
_DOC_DEFAULTS: dict | None = None


def escape_sql(value) -> str:
    """转义 LanceDB SQL where 子句中的字符串字面量（单引号翻倍，防注入）。"""
    return str(value).replace("'", "''")


def escape_like(value) -> str:
    """转义 LIKE / ILIKE 模式串里的通配符。

    DataFusion 的 LIKE 用反斜杠转义 `%` 与 `_`。不转义的话查询词 `100%` 会变成
    通配模式（「以 100 结尾的任意内容」），与今天 Python 侧 `needle in title`
    的「子串包含」语义完全不同——下推时必须保住原语义。
    """
    s = str(value)
    for ch in ("\\", "%", "_"):
        s = s.replace(ch, "\\" + ch)
    return s


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def fetch_rows(query_builder) -> list[dict]:
    """执行查询并返回 Python 行列表。"""
    try:
        return query_builder.to_list()
    except Exception:  # noqa: BLE001
        return query_builder.to_arrow().to_pylist()


def quote_in(values) -> str:
    """把 Python 值列表拼成 SQL IN (…) 的字面量串（每个值都过 escape_sql）。

    只在本模块内被 `scalar_rows` 用。它**不是**给上层用的公共 API：
    「自己拼 IN ()」正是仓储层要收掉的写法（`docs_query(..., one_of=…)`
    才是）；早期留过一个 `_quote_in` 别名，注释声称"测试里也用到"，
    实际全仓零调用点 —— 一个骗人的注释比没有注释更坏，因为它会让后来人
    以为动它会红测试，从而永远不敢清理。
    """
    return ", ".join(f"'{escape_sql(v)}'" for v in values)


def _ordering(order_by):
    """把 [("created_at", False), …] 转成 lance 的 ColumnOrdering 列表。

    注意 `lance.dataset.ColumnOrdering` 与 `lancedb.query.ColumnOrdering`
    是**两个不同的类**：把后者传给前者会直接 pydantic 校验失败。
    """
    if not order_by:
        return None
    from lance.dataset import ColumnOrdering

    out = []
    for item in order_by:
        if isinstance(item, str):
            out.append(ColumnOrdering(item))
        else:
            col = item[0]
            asc = item[1] if len(item) > 1 else True
            out.append(ColumnOrdering(col, ascending=bool(asc)))
    return out


def scalar_rows(table, *, cols: list[str], where: str | None = None,
                ids=None, id_column: str = "doc_id",
                limit: int | None = None, offset: int | None = None,
                order_by=None) -> list[dict]:
    """标量读（无向量、无全文）：走 lance 原生 scanner，支持排序 + 分页下推。

    `order_by` 形如 `[("created_at", False)]`（第二项是 ascending，缺省 True）。

    两个必须知道的坑（lance 12.0 实测）：
    1. **排序列必须出现在投影里**，否则 planning 直接失败：
       `TakeExec requires the input plan to have a column named '_rowaddr' or '_rowid'`。
       这里自动把排序列补进投影，取回后再裁掉调用方没要的列。
       （`with_row_address` / `scan_in_order` 都救不了这个错，已试过。）
    2. lancedb 的 `search().order_by(…).limit(…).offset(…)` 走另一条执行路径，
       同样会撞上面那个错——本仓库 `api/chunks.py` 之所以今天没炸，是因为它把排序列
       `ordinal` 一并 select 了。所以「排序 + 分页」一律走本函数。

    `ids` 用于「按 id 集合取一页」，与 `where` 是 AND 关系。
    """
    ordering = _ordering(order_by)
    projection = list(cols)
    extra: list[str] = []
    if ordering is not None:
        for item in order_by:
            col = item if isinstance(item, str) else item[0]
            if col not in projection and col not in extra:
                extra.append(col)
        projection = projection + extra

    clauses = [w for w in (where,) if w]
    if ids is not None:
        clauses.append(f"{id_column} IN ({quote_in(ids)})")

    kwargs: dict = {"columns": projection}
    if clauses:
        kwargs["filter"] = " AND ".join(clauses)
    if ordering is not None:
        kwargs["order_by"] = ordering
    if limit is not None:
        kwargs["limit"] = int(limit)
    if offset:
        kwargs["offset"] = int(offset)

    rows = table.to_lance().to_table(**kwargs).to_pylist()
    if extra:
        rows = [{k: v for k, v in r.items() if k not in extra} for r in rows]
    return rows


def count_rows(table, where: str | None = None) -> int:
    """按过滤条件计数，不 materialize 任何行。实测 0.28ms（无过滤）/ 1.26ms（带过滤）。"""
    try:
        return int(table.count_rows(filter=where) if where
                   else table.count_rows())
    except Exception:  # noqa: BLE001
        # 老版本 lancedb 的 count_rows 不接受 filter：退回窄列扫描计数
        return len(scalar_rows(table, cols=[table.schema.names[0]],
                               where=where))


def scalar(v):
    """numpy / pyarrow 标量转 Python 原生类型。"""
    try:
        return v.item() if hasattr(v, "item") else v
    except Exception:  # noqa: BLE001
        return v


def scalar_row(d: dict) -> dict:
    """整行做标量转换。"""
    return {k: scalar(v) for k, v in d.items()}


def _scalar_defaults(model_cls) -> dict:
    """LanceModel 各列的标量默认值（非标量默认置 None）。"""
    out: dict = {}
    for name, f in model_cls.model_fields.items():
        d = f.default
        if d is not None and not isinstance(d, (str, int, float, bool)):
            d = None
        out[name] = d
    return out


def chunk_defaults(dim: int) -> dict:
    cached = _CHUNK_DEFAULTS.get(dim)
    if cached is None:
        from .schema import chunk_schema

        cached = _scalar_defaults(chunk_schema(dim))
        _CHUNK_DEFAULTS[dim] = cached
    return cached


def doc_defaults() -> dict:
    global _DOC_DEFAULTS
    if _DOC_DEFAULTS is None:
        from .schema import Document

        _DOC_DEFAULTS = _scalar_defaults(Document)
    return _DOC_DEFAULTS


def fill_missing(rows: list[dict], cols: set[str], defaults: dict,
                 blob_col: str | None = None) -> list[dict]:
    """按表 schema 补齐行中缺失的列。"""
    if not cols:
        return rows
    filled = []
    for r in rows:
        missing = cols - set(r.keys())
        if not missing:
            filled.append(r)
            continue
        row = dict(r)
        for col in missing:
            if blob_col and col == blob_col:
                row[col] = []          # 向量列：空列表交给上层报明确错误
            elif col in defaults:
                row[col] = defaults[col]
        filled.append(row)
    return filled


def table_columns(table) -> set[str]:
    """取表的列名；失败返回空集（调用方据此跳过补齐）。

    「返回空集」是**故意的降级**：`only_cols` 拿到空集会把任何投影都判成
    未知列并报错，那是响的；而补齐路径（fill_missing）拿到空集只是少补几列。
    但失败本身必须留痕 —— 否则下一次「列怎么又没补上」无从可查。
    """
    try:
        return set(table.schema.names)
    except Exception as e:  # noqa: BLE001
        logger.warning("读取表列名失败，按「无列」处理: %s", e)
        return set()


def only_cols(cols, allowed: set[str], *, table: str) -> list[str]:
    """把调用方给的投影收窄到表的真实列；出现未知列名直接报错。

    **为什么必须拦而不是「无所谓」**：`cols` 是唯一会被**拼进 SQL 文本**
    的部分（`SELECT {', '.join(cols)}`）。值我们已经统一走占位符或
    `escape_sql`，但列名在 SQL 里天生不能参数化，所以投影只能来自白名单。
    漏了这一道，一个叫 `"title, text FROM documents --"` 的"列名"
    就能改写整条查询的语义。

    `allowed` 为空集时**不校验**（表元数据取不到，例如 Lance 表尚未建）：
    宁可放行一次正常读，也不要因为探测失败就把所有读打挂 ——
    报错发生在真正取到列清单的时候。
    """
    out = [str(c) for c in cols]
    if allowed:
        bad = [c for c in out if c not in allowed]
        if bad:
            from rag.core.errors import Invalid

            raise Invalid(f"{table} 没有这些列：{bad}")
    return out
