"""单次存储操作的成本归因（进程内，不起服务）。

这个脚本的存在理由：本项目的性能问题**几乎全部**是「每请求固定开销」，
而固定开销只能进程内量清楚。它同时是「换 lancedb 版本值不值」
这个实验的度量尺——升级后重跑一遍，看每查询固定开销有没有掉下来。

关键对照是 `lancedb 查询构建器` vs `lance 原生 scanner`：
本机实测同一张 2000 行的表，前者全表取回 6.76ms，后者 2.26ms。
差值就是 `storage/sql.scalar_rows()` 存在的理由。
"""
from __future__ import annotations

import time
from pathlib import Path

DOC_COLS = ["doc_id", "title", "mime", "parser_engine", "chunk_count",
            "status", "kb_id", "stored_file", "created_at", "meta"]


def _bench(fn, n=20, warm=6) -> float:
    for _ in range(warm):
        fn()
    ts = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        ts.append((time.perf_counter() - t0) * 1000)
    ts.sort()
    return round(ts[len(ts) // 2], 3)


def run(data_dir: Path, *, dim: int = 1024, query: str = "测试 样例") -> dict:
    from rag.storage.backend import LocalBackend
    from rag.storage.repos import list_documents
    from rag.storage.sql import count_rows, escape_sql, scalar_rows
    from rag.storage.tables import LanceStore

    store = LanceStore(LocalBackend(Path(data_dir)), dim)
    docs, chunks = store.documents, store.chunks
    out = {"rows": {"documents": docs.count_rows(),
                    "chunks": chunks.count_rows(),
                    "jobs": store.jobs.count_rows()}}
    if not docs.count_rows():
        out["error"] = "documents 表为空：先跑 tools.bench gen"
        return out

    did = scalar_rows(docs, cols=["doc_id"], limit=1)[0]["doc_id"]
    vec = [0.0] * dim
    vec[0] = 1.0

    out["lance"] = {
        "count_rows": _bench(lambda: docs.count_rows()),
        "count_rows_filtered": _bench(
            lambda: count_rows(docs, "kb_id = 'kb-1'")),
        "lancedb_full_scan": _bench(
            lambda: docs.search().select(DOC_COLS).to_list()),
        "native_full_scan": _bench(lambda: scalar_rows(docs, cols=DOC_COLS)),
        "native_page_ordered": _bench(
            lambda: scalar_rows(docs, cols=DOC_COLS,
                                order_by=[("created_at", False),
                                          ("doc_id", True)],
                                limit=50, offset=100)),
        "point_lookup_doc_id": _bench(
            lambda: scalar_rows(docs, cols=DOC_COLS,
                                where=f"doc_id = '{escape_sql(did)}'",
                                limit=1)),
        "list_documents": _bench(lambda: list_documents(store, limit=50)),
    }
    if chunks.count_rows():
        out["retrieval"] = {
            "vector_topk": _bench(
                lambda: chunks.search(vec, vector_column_name="vector")
                .limit(50).select(["chunk_id"]).to_list(), n=10, warm=3),
            "fts_topk": _bench(
                lambda: chunks.search(query, query_type="fts")
                .limit(50).select(["chunk_id"]).to_list(), n=10, warm=3),
        }
    # 版本治理是可观测性的关键一项：list_versions() 实测 ≈0.17ms/版本
    out["versions"] = {
        name: _bench(lambda n=name: store.table_versions(n), n=6, warm=2)
        for name in ("chunks", "jobs", "documents")}
    out["version_counts"] = {name: len(store.table_versions(name))
                             for name in ("chunks", "jobs", "documents")}
    return out


def report(result: dict) -> str:
    lines = []
    if "error" in result:
        return "跳过：" + result["error"]
    lines.append(f"行数: {result['rows']}")
    lance = result.get("lance", {})
    lines.append("\n  操作                         p50 (ms)")
    for k, v in lance.items():
        lines.append(f"  {k:28s} {v:8.2f}")
    ratio = None
    if lance.get("lancedb_full_scan") and lance.get("native_full_scan"):
        ratio = round(lance["lancedb_full_scan"] / lance["native_full_scan"], 2)
        lines.append(f"\n  查询构建器 / 原生 scanner = {ratio}× "
                     "（这一项变大说明该换版本或该改调用路径）")
    for section in ("retrieval", "versions"):
        data = result.get(section) or {}
        if data:
            lines.append(f"\n  {section}:")
            for k, v in data.items():
                lines.append(f"    {k:26s} {v:8.2f} ms")
        counts = result.get("version_counts")
        if section == "versions" and counts:
            lines.append(f"    版本数: {counts}")
    return "\n".join(lines)
