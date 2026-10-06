"""模型配置与测试端点：GET/PUT（落盘 config.toml）/ test / embed。"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request

from rag.api._common import (get_ctx, mask, merge_sub, raise_operation_error,
                             safe)
from rag.api.responses import (EmbedVectorsOut, ModelsOut, ModelsTestOut,
                               ModelsUpdateOut, error_responses)
from rag.api.schemas import (EmbedReq, ModelsTestReq,
                             ModelsUpdateReq)
from rag.core.config import save_config
from rag.models.testkit import test_embedding, test_llm, test_rerank

router = APIRouter(
    tags=["models"],
    responses=error_responses(unavailable=True),
)


@router.get("/models")
async def api_models(request: Request) -> ModelsOut:
    ctx = get_ctx(request)
    ec = ctx.registry.bundle.embed_cfg
    lc = ctx.registry.bundle.llm_cfg
    rc = ctx.registry.bundle.rerank_cfg
    return {
        "embed": mask(ec.model_dump()),
        "llm": mask(lc.model_dump()),
        "rerank": mask(rc.model_dump()),
        # 检索默认值：前端据此把「留空 = 用默认」变成可见的具体数字，
        # 否则用户想调大 top_k 时无从知道现在是多少。
        "retrieve": ctx.settings.retrieve.model_dump(),
    }


@router.put("/models")
async def api_models_put(request: Request, body: ModelsUpdateReq,
                         persist: bool = True) -> ModelsUpdateOut:
    """热更新模型配置；persist=true（默认）时写回 data/config.toml。

    默认落盘：设置页承诺「保存」，若只改内存，重启即丢——用户会以为
    配置没生效。要临时只热切换请显式传 persist=false。
    """
    ctx = get_ctx(request)
    applied = []
    for key in ("embed", "llm", "rerank"):
        patch = getattr(body, key)
        if patch and isinstance(patch, dict):
            merge_sub(ctx.settings, key, patch)
            applied.append(key)
    result = ctx.registry.reload()
    persisted = False
    if persist:
        save_config(ctx.settings)
        persisted = True
    return {"ok": True, "applied": applied,
            "warnings": result["warnings"], "persisted": persisted}


@router.post("/models/test")
async def api_models_test(request: Request, body: ModelsTestReq,
                          persist: bool = False) -> ModelsTestOut:
    ctx = get_ctx(request)
    out = {}
    if body.kind in ("all", "embedding"):
        out["embedding"] = await safe(test_embedding(ctx.registry.embedder))
    if body.kind in ("all", "rerank"):
        out["rerank"] = await safe(test_rerank(
            ctx.registry.reranker, "测试查询",
            ["相关文档内容 about testing",
             "无关内容 random abc", "另一段无关文本 xyz"]))
    if body.kind in ("all", "llm"):
        out["llm"] = await safe(test_llm(ctx.registry.chat()))
    if persist:
        save_config(ctx.settings)
        out["persisted"] = True
    return out


@router.post("/embed")
async def api_embed(request: Request, body: EmbedReq) -> EmbedVectorsOut:
    """文本向量化（外部工具直接复用检索的嵌入能力）。"""
    ctx = get_ctx(request)
    texts = [t for t in body.texts if t][:1000]
    if not texts:
        raise HTTPException(400, "texts 不能为空")
    try:
        vecs = await asyncio.to_thread(
            ctx.registry.embedder.embed, texts)
    except Exception as e:  # noqa: BLE001
        raise_operation_error(e, "向量化失败")
    return {
        "model": ctx.registry.embedder.model,
        "dim": ctx.registry.embedder.dim,
        "count": len(vecs),
        "vectors": vecs,
    }
