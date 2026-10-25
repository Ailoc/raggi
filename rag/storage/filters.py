"""与引擎无关的分块过滤条件。

改前过滤是「在仓储层直接拼出来的 SQL 字符串」：只有 Lance/SQLite 能消费。
换向量引擎时只有两条路——解析 SQL（脆、且要重写一遍解析器），或者在
适配器里再写一份过滤语义（两份语义迟早不一致，而且是不报错的那种）。

这里收成一个数据对象：检索侧与列表侧各自构造 `ChunkFilter`，
`to_sql()` 只服务当前引擎；未来的 Qdrant 适配器直接读字段构造它自己的
Filter，不再碰 SQL。
"""
from __future__ import annotations

from dataclasses import dataclass

from .sql import escape_like, escape_sql

# 恒假条件：过滤条件解析不出任何候选时必须产生它，而不是「不加过滤」。
# 返回全库是不可见的错误结果，返回 0 条是可见的空结果——宁严勿松。
NEVER = "doc_id IN ('__no_match__')"


def in_clause(column: str, ids) -> str:
    """值集合 → SQL IN (…)。**空集合直接报错**：`IN ()` 是语法错误，而且
    若被上层 except 吞掉就会退化成「不加这个过滤」——调用方必须显式用
    `NEVER` 表达恒假。
    """
    values = list(ids)
    if not values:
        raise ValueError(
            f"{column} 的过滤集合为空；恒假条件请用 filters.NEVER")
    return f"{column} IN (" + ", ".join(
        f"'{escape_sql(i)}'" for i in values) + ")"


@dataclass(frozen=True)
class ChunkFilter:
    """分块过滤条件（结构化；渲染 SQL 只是它的一种用法）。"""

    # 检索侧
    enabled_only: bool = False
    origin: str = ""
    kb_id: str = ""
    kb_ids: tuple[str, ...] = ()
    doc_ids: tuple[str, ...] = ()
    chunk_ids: tuple[str, ...] = ()
    # mime / parser_engine 在 documents 上，先解析成 doc_id 白名单再套到分块；
    # 其中一个集合为空 ⇒ 整体恒假（不能跳过该过滤）
    doc_id_sets: tuple[tuple[str, ...], ...] = ()
    # 列表侧
    only_standalone: bool = False
    text_contains: str = ""
    # 上下文窗口：(doc_id, lo, hi) 的并集（跨文档一次取）
    ranges: tuple[tuple[str, int, int], ...] = ()

    def is_never(self) -> bool:
        return any(len(ids) == 0 for ids in self.doc_id_sets)

    def is_empty(self) -> bool:
        """没有任何条件：等价于「全部分块」。"""
        return self.to_sql() is None

    def to_sql(self) -> str | None:
        """渲染成当前引擎（Lance/SQLite）的 where 子句。"""
        conds: list[str] = []
        if self.enabled_only:
            conds.append("enabled = true")
        if self.origin:
            conds.append(f"origin = '{escape_sql(self.origin)}'")
        if self.kb_id:
            conds.append(f"kb_id = '{escape_sql(self.kb_id)}'")
        elif self.kb_ids:
            conds.append(in_clause("kb_id", self.kb_ids))
        if self.doc_ids:
            conds.append(in_clause("doc_id", self.doc_ids))
        if self.chunk_ids:
            conds.append(in_clause("chunk_id", self.chunk_ids))
        for ids in self.doc_id_sets:
            conds.append(in_clause("doc_id", ids) if ids else NEVER)
        if self.only_standalone:
            # doc_id='' 是「独立分块」的存储形态，必须下推而不是事后过滤
            conds.append("doc_id = ''")
        if self.ranges:
            per_doc = " OR ".join(
                f"(doc_id = '{escape_sql(d)}' AND ordinal BETWEEN {int(lo)} AND {int(hi)})"
                for d, lo, hi in self.ranges)
            conds.append(f"({per_doc})")
        needle = (self.text_contains or "").strip()
        if needle:
            # 子串过滤下推成 ILIKE（对中文有效）；% / _ 必须当字面量
            conds.append(f"text ILIKE '%{escape_like(needle)}%'")
        return " AND ".join(conds) if conds else None


def where_sql(where: "ChunkFilter | str | None") -> str | None:
    """兼容入口：结构化对象渲染成 SQL；已经是对外的字符串原样返回。"""
    if isinstance(where, ChunkFilter):
        return where.to_sql()
    return where
