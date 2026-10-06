"""模型连通性/性能测试：embedding / rerank / llm。

所有同步模型调用均包 asyncio.to_thread，避免阻塞事件循环。
"""
from __future__ import annotations

import asyncio
import time

from rag.models.embeddings import Embedder

PROBE = "这是一个连通性测试探针 sentence probe。"


async def test_embedding(embedder: Embedder) -> dict:
    t0 = time.perf_counter()
    vec = await asyncio.to_thread(embedder.embed_one, PROBE)
    dt = (time.perf_counter() - t0) * 1000
    # 吞吐：32 条批量探针测 texts/s
    batch = [PROBE] * 32
    t1 = time.perf_counter()
    await asyncio.to_thread(embedder.embed, batch)
    tput = len(batch) / max((time.perf_counter() - t1), 1e-6)
    return {
        "ok": True,
        "dim": len(vec),
        "latency_ms": round(dt, 1),
        "throughput_texts_per_s": round(tput, 1),
        "model": embedder.model,
        "first3": [round(x, 4) for x in vec[:3]],
    }


async def test_rerank(reranker, query: str, docs: list[str]) -> dict:
    if reranker is None:
        return {"ok": True, "skipped": True, "reason": "rerank 未启用"}
    import pandas as pd

    df = pd.DataFrame({"text": docs, "id": list(range(len(docs)))})
    t0 = time.perf_counter()
    out = await asyncio.to_thread(reranker.rerank, query, df)
    dt = (time.perf_counter() - t0) * 1000
    scores = out["_relevance_score"].tolist() if "_relevance_score" in out else []
    order = out["id"].tolist()
    return {
        "ok": True,
        "latency_ms": round(dt, 1),
        "scores": [round(s, 4) for s in scores],
        "order": order,
        "sorted_correctly": order[0] == 0 if order else False,
    }


async def test_llm(chat) -> dict:
    t0 = time.perf_counter()
    resp = await chat.ainvoke("用一句话回答：1+1 等于几？")
    dt = (time.perf_counter() - t0) * 1000
    content = getattr(resp, "content", str(resp))
    return {"ok": True, "latency_ms": round(dt, 1), "sample": content[:200]}
