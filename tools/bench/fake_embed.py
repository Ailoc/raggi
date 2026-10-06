"""假 embedding / rerank 服务：OpenAI 兼容协议，瞬时返回确定性随机向量。

为什么要有它：压测**不能**依赖真实模型服务。
- 付费额度与配额会让基准不可复现（同一脚本两次跑出不同结果）；
- 网络抖动会把「后端排队与存储成本」淹掉——那正是要量的东西；
- 关掉它就没法测「入库吞吐」这条写路径。

所以这里给一个确定性、零延迟抖动的替身：同一句话永远给同一个单位向量，
于是检索结果稳定、索引仍然有意义。

真实凭据下的端到端延迟是**另一个**实验（`--real-embed`），两者不要混在一张表里比。
"""
from __future__ import annotations

import hashlib
import sys

import numpy as np
from fastapi import FastAPI, Request
from fastapi.responses import ORJSONResponse

app = FastAPI(default_response_class=ORJSONResponse)

DIM = int(sys.argv[sys.argv.index("--dim") + 1]) if "--dim" in sys.argv else 1024


def _vec(seed: str, dim: int = 0) -> list[float]:
    """按输入文本确定性生成一个单位向量。

    用 md5 前 8 位作随机种子：同一句话 → 同一个向量 → FTS/向量两路召回
    在多次运行间可比。
    """
    d = dim or DIM
    h = int(hashlib.md5(seed.encode("utf-8")).hexdigest()[:8], 16)
    v = np.random.default_rng(h).standard_normal(d, dtype=np.float32)
    n = float(np.linalg.norm(v)) or 1.0
    return [float(x / n) for x in v]


@app.post("/v1/embeddings")
async def embeddings(req: Request):
    body = await req.json()
    inputs = body.get("input") or []
    if isinstance(inputs, str):
        inputs = [inputs]
    data = [{"object": "embedding", "index": i, "embedding": _vec(t)}
            for i, t in enumerate(inputs)]
    return {"data": data, "model": body.get("model", "fake-bge-m3"),
            "usage": {"prompt_tokens": 0, "total_tokens": 0}}


@app.post("/v1/rerank")
async def rerank(req: Request):
    """把「文档里出现查询词的次数」当分数：够用来验证排序管线是否被调用。"""
    body = await req.json()
    q = str(body.get("query") or "")
    docs = body.get("documents") or []
    results = [{"index": i, "relevance_score": float(docs[i].count(q))}
               for i in range(len(docs))]
    results.sort(key=lambda r: -r["relevance_score"])
    return {"results": results}


@app.get("/healthz")
async def healthz():
    return {"ok": True, "dim": DIM}


if __name__ == "__main__":
    import uvicorn

    port = int(sys.argv[sys.argv.index("--port") + 1]) \
        if "--port" in sys.argv else 8390
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
