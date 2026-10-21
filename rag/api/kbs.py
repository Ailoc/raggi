"""知识库端点：列表 / 创建 / 更新 / 删除（级联）/ 详情。"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request

from rag.api._common import get_ctx
from rag.api.responses import KbDeleteOut, KbOut, error_responses
from rag.api.schemas import KBCreateReq, KBUpdateReq
from rag.storage import plan as chunking
from rag.storage.repos import kbs

router = APIRouter(
    tags=["knowledge-bases"],
    responses=error_responses(not_found=True),
)


def _with_custom(row: dict, ctx) -> dict:
    """补上 plan_custom：该库方案是否**偏离系统默认**。

    两级模型下每个知识库都持有具体数值，所以「有没有设置过」不再有信息量；
    对用户有意义的是「跟默认一样吗」。这个判断需要 settings，
    因此放在 API 层而不是仓储层。

    策略也要参与判定：只比 size/ratio 的话，"同样的数字 + 不同的策略"
    会被标成"用默认"，而实际行为（结构感知 vs 纯字符）不同——
    前端 KbSettings 的 isCustomPlan 用同一把尺子，两处必须一致。
    """
    g = chunking.global_plan(ctx.settings)
    row["plan_custom"] = chunking.is_custom_plan(row, g)
    return row


@router.get("/kbs")
async def api_kb_list(request: Request) -> list[KbOut]:
    ctx = get_ctx(request)
    rows = await asyncio.to_thread(kbs.list_kbs, ctx.store)
    return [_with_custom(r, ctx) for r in rows]


@router.post("/kbs")
async def api_kb_create(request: Request, body: KBCreateReq) -> KbOut:
    ctx = get_ctx(request)
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "名称不能为空")
    g = chunking.global_plan(ctx.settings)
    row = await asyncio.to_thread(
        kbs.create_kb, ctx.store, name, body.description,
        body.chunk_size or 0, body.overlap_ratio or 0.0,
        body.strategy or "",
        g["chunk_size"], g["overlap_ratio"], g["strategy"])
    return _with_custom(row, ctx)


@router.get("/kbs/{kb_id}")
async def api_kb_get(request: Request, kb_id: str) -> KbOut:
    ctx = get_ctx(request)
    row = await asyncio.to_thread(kbs.get_kb, ctx.store, kb_id)
    if row is None:
        raise HTTPException(404, "knowledge base not found")
    return _with_custom(row, ctx)


@router.put("/kbs/{kb_id}")
async def api_kb_update(request: Request, kb_id: str,
                        body: KBUpdateReq) -> KbOut:
    ctx = get_ctx(request)
    chunk_size = body.chunk_size
    overlap = body.overlap_ratio
    strategy = body.strategy
    if chunk_size == 0:
        # 两级模型下不存在运行时“继承”：0 的语义是“把当前系统默认物化
        # 到本库”。不物化会留下 chunk_size=0 的旧形态，列表显示与实际
        # 生效值不一致（plan_custom 也会误判）。
        g = chunking.global_plan(ctx.settings)
        chunk_size = int(g["chunk_size"])
        if overlap is None:
            overlap = float(g["overlap_ratio"])
        if not strategy:
            strategy = str(g["strategy"])
    row = await asyncio.to_thread(
        kbs.update_kb, ctx.store, kb_id,
        name=body.name, description=body.description,
        chunk_size=chunk_size,
        overlap_ratio=overlap,
        strategy=strategy)
    if row is None:
        raise HTTPException(404, "knowledge base not found")
    return _with_custom(row, ctx)


@router.delete("/kbs/{kb_id}")
async def api_kb_delete(request: Request, kb_id: str) -> KbDeleteOut:
    ctx = get_ctx(request)
    ok = await asyncio.to_thread(
        kbs.delete_kb, ctx.store, kb_id,
        ctx.backend)
    if not ok:
        raise HTTPException(404, "knowledge base not found")
    return {"ok": True, "kb_id": kb_id}
