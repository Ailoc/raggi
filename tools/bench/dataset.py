"""合成数据集：直接写 LanceDB，不经 API、不依赖真实 embedding。

为什么不经 API：基准要的是「可控的规模」，而不是「能不能入库」。
走 API 会把解析/向量化的波动混进数据集本身。

向量是随机单位向量 —— 语义上是噪声，但**维度、分布、索引形态**与真实
embedding 一致，因此 ANN/FTS/标量索引的路径与成本都是真实的。
"""
from __future__ import annotations

import time
import uuid
from pathlib import Path

import numpy as np


def build(data_dir: Path, *, docs: int = 2000, chunks_per: int = 10,
          dim: int = 1024, kbs: int = 5, seed: int = 7,
          with_indexes: bool = True) -> dict:
    from rag.parsing.segment import segment
    from rag.storage.backend import LocalBackend
    from rag.storage.tables import LanceStore

    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    store = LanceStore(LocalBackend(data_dir), dim)
    rng = np.random.default_rng(seed)
    now = "2026-10-05T00:00:00+00:00"

    doc_rows, chunk_rows = [], []
    kb_rows = [{"kb_id": f"kb-{i}", "name": f"知识库 {i}", "description": "",
                "chunk_size": 512, "overlap_ratio": 0.1,
                "created_at": now, "updated_at": now} for i in range(kbs)]
    for i in range(docs):
        did = f"doc-{i:05d}"
        kb = f"kb-{i % max(1, kbs)}"
        doc_rows.append({
            "doc_id": did, "title": f"文档 {i}", "source_uri": None,
            "mime": "application/pdf", "parser_engine": "pymupdf4llm",
            "content_hash": uuid.uuid4().hex, "char_count": 5000,
            "chunk_count": chunks_per, "text": "正文 " * 500,
            "status": "ready", "error": None, "kb_id": kb,
            "stored_file": None, "meta": "{}",
            "created_at": now, "updated_at": now})
        for j in range(chunks_per):
            v = rng.standard_normal(dim, dtype=np.float32)
            v /= np.linalg.norm(v)
            txt = (f"这是第 {i} 篇文档的第 {j} 段，包含可检索的关键词 "
                   f"测试样例 alpha{i}，用于并发性能测量。")
            chunk_rows.append({
                "chunk_id": str(uuid.uuid4()), "doc_id": did, "ordinal": j,
                "kb_id": kb, "text": txt, "text_seg": " ".join(
                    segment(txt).split()),
                "heading_path": "", "page": j, "char_start": j * 300,
                "char_end": j * 300 + 280, "token_count": 80,
                "origin": "parsed", "edited": False, "enabled": True,
                "original_text": None, "offset_valid": True,
                "fts_stale": False, "embed_model": "fake-bge-m3",
                "vector": v.tolist(), "created_at": now, "updated_at": now})

    t0 = time.perf_counter()
    if kb_rows:
        store.kbs.add(kb_rows)
    store.documents.add(doc_rows)
    store.chunks.add(chunk_rows)
    written = {"docs": len(doc_rows), "chunks": len(chunk_rows),
               "kbs": len(kb_rows), "write_seconds": round(
                   time.perf_counter() - t0, 1)}
    if with_indexes:
        t1 = time.perf_counter()
        store.ensure_scalar_indexes()
        store.ensure_vector_index()
        store.ensure_fts_index()
        written["index_seconds"] = round(time.perf_counter() - t1, 1)
    return written
