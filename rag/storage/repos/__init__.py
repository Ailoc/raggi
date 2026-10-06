"""数据仓储：按实体收敛 documents / chunks / kbs / keys 的读写。

调用方统一 `from rag.storage.repos import xxx`，不必关心某个函数落在
哪个文件里——重写期间这样能把「模块划分」与「调用点」解耦。

只导出**确有调用方**的函数：零调用的 helper 挂在这里会被误认为公共 API
（于是有人照着它写新代码，而它本身可能早已与实际行为脱节）。
"""
from __future__ import annotations

from ..sql import (
    count_rows,
    escape_like,
    escape_sql,
    fetch_rows,
    now_iso,
    quote_in,
    scalar,
    scalar_row,
    scalar_rows,
)
from .chunks import (
    chunk_filters,
    chunk_prefilter,
    chunks_page,
    contexts_for,
    delete_chunk,
    delete_kb_chunks,
    doc_chunk_index,
    doc_ids_by_attr,
    get_chunk,
    search_chunks,
    texts_by_id,
    update_chunk_text,
    upsert_chunks,
)
from .documents import (
    delete_document,
    delete_documents,
    docs_count,
    docs_query,
    find_by_hash,
    get_document,
    get_documents,
    list_documents,
    set_doc_fields,
    stored_file,
    update_metadata,
    upsert_documents,
)
from .jobs import add_job, get_job, is_terminal, list_jobs, prune_jobs, set_job
from .kbs import (
    create_kb,
    delete_kb,
    doc_ids_in_kb,
    get_kb,
    kbs_query,
    list_kbs,
    set_kb_fields,
)

# 兼容旧名：delete_doc 现在叫 delete_document（语义更明确）
delete_doc = delete_document

__all__ = [
    # chunks
    "chunk_filters", "chunk_prefilter", "chunks_page", "search_chunks", "contexts_for", "delete_chunk",
    "delete_kb_chunks", "doc_chunk_index", "doc_ids_by_attr", "get_chunk", "texts_by_id",
    "update_chunk_text", "upsert_chunks",
    # documents
    "delete_document", "delete_documents", "delete_doc", "docs_count",
    "docs_query", "find_by_hash", "get_document", "get_documents",
    "list_documents", "set_doc_fields", "stored_file",
    "update_metadata", "upsert_documents",
    # jobs
    "add_job", "get_job", "is_terminal", "list_jobs", "prune_jobs", "set_job",
    # kbs
    "create_kb", "delete_kb", "doc_ids_in_kb", "get_kb", "kbs_query",
    "list_kbs", "set_kb_fields",
    # sql 工具
    "count_rows", "escape_like", "escape_sql", "fetch_rows", "now_iso",
    "quote_in", "scalar", "scalar_row", "scalar_rows",
]