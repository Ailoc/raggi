"""分块端点：列表 / 详情 / 新增 / 单条编辑 / 批量编辑 / 批量启停 / 删除。"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request

from rag.api._common import get_ctx
from rag.api.responses import (
    ChunkBatchEditedOut,
    ChunkBatchEnabledOut,
    ChunkCreatedOut,
    ChunkEditedOut,
    ChunkEnabledOut,
    ChunkListOut,
    ChunkOut,
    OkOut,
    error_responses,
)
from rag.api.schemas import (
    ChunkAddReq,
    ChunkBatchEditReq,
    ChunkBatchEnabledReq,
    ChunkEditReq,
    ChunkEnabledReq,
)
from rag.chunk_edit import (
    add_manual_chunk,
    batch_edit_chunks,
    edit_chunk,
    get_chunk,
    remove_chunk,
    set_chunk_enabled,
    set_chunks_enabled,
)
from rag.core.errors import EditConflict
from rag.storage.repos import chunks_page

router = APIRouter(
    tags=["chunks"],
    responses=error_responses(not_found=True, unavailable=True),
)

@router.get("/chunks")
async def api_chunks_list(request: Request, doc_id: str = "",
                          kb_id: str = "", only_standalone: bool = False,
                          q: str = "",
                          limit: int = 100, offset: int = 0) -> ChunkListOut:
    """分块列表。按文档取该文档分块；按知识库取该库全部（含独立分块）。

    两者都不传时返回全部（供调试），不再隐式只返回手工分块。

    only_standalone=true 用于「该知识库下的独立分块」页：不带该参数时
    先取库内前 N 条再在 Python 层过滤，文档分块一多独立分块就会被截断
    掉而整个不可见（C2）。

    q 是**分页前**的内容子串过滤，`total` 因此是过滤后的块数——口径与
    `GET /documents` 的标题过滤一致。放在分页后做就会出现"匹配 3 块，
    但下面还挂着加载更多"这种自相矛盾的界面。
    """
    ctx = get_ctx(request)
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    # 过滤/排序/分页/计数的实现都在仓储层的 chunks_page（SQL 文本只许出现在
    # storage 层）。这里只负责把 HTTP 参数收进范围并交给它。
    items, total = await asyncio.to_thread(
        chunks_page, ctx.store, doc_id=doc_id, kb_id=kb_id,
        only_standalone=only_standalone, q=q, limit=limit, offset=offset)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/chunks/{chunk_id}")
async def api_chunk_get(request: Request, chunk_id: str) -> ChunkOut:
    ctx = get_ctx(request)
    row = await asyncio.to_thread(get_chunk, ctx.store, chunk_id)
    if row is None:
        raise HTTPException(404, "chunk not found")
    return row


@router.post("/chunks")
async def api_chunk_add(request: Request,
                        body: ChunkAddReq) -> ChunkCreatedOut:
    """新增分块。给了 doc_id 归该文档，否则为该知识库下的独立分块。"""
    ctx = get_ctx(request)
    try:
        chunk_id = await asyncio.to_thread(
            add_manual_chunk, ctx.store, ctx.registry.embedder,
            body.text, body.doc_id, body.kb_id or "")
    except KeyError as e:
        # KeyError 的 str() 是 repr（会带引号），对用户是噪声
        raise HTTPException(404, str(e).strip("'\""))
    return {"chunk_id": chunk_id}


@router.patch("/chunks/{chunk_id}/enabled")
async def api_chunk_enabled(request: Request, chunk_id: str,
                            body: ChunkEnabledReq) -> ChunkEnabledOut:
    """停用/启用分块（停用仅退出检索，数据保留）。"""
    ctx = get_ctx(request)
    try:
        return await asyncio.to_thread(
            set_chunk_enabled, ctx.store, chunk_id, body.enabled)
    except KeyError:
        raise HTTPException(404, "chunk not found")


# 必须注册在 PATCH /chunks/{chunk_id} 之前：字面量路径若排在
# 参数化路由之后，会被当成 chunk_id="batch-enabled" 而返回 404。
@router.patch("/chunks/batch-enabled")
async def api_chunks_batch_enabled(request: Request,
                                  body: ChunkBatchEnabledReq) -> ChunkBatchEnabledOut:
    """批量停用/启用分块（一次写入，非逐条循环）。

    与 PATCH /chunks（批量改内容、需重新向量化）是两件事：
    这里只翻 enabled 标志，不触发 embedding。
    """
    ctx = get_ctx(request)
    try:
        return await asyncio.to_thread(
            set_chunks_enabled, ctx.store, body.chunk_ids, body.enabled)
    except KeyError as e:
        # KeyError 的 str() 是 repr（会带引号），对用户是噪声
        raise HTTPException(404, str(e).strip("'\""))


@router.patch("/chunks/{chunk_id}")
async def api_chunk_edit(request: Request, chunk_id: str,
                         body: ChunkEditReq) -> ChunkEditedOut:
    ctx = get_ctx(request)
    try:
        return await asyncio.to_thread(
            edit_chunk, ctx.store, ctx.registry.embedder,
            chunk_id, body.text, body.force, body.updated_at)
    except KeyError:
        raise HTTPException(404, "chunk not found")
    except EditConflict as e:
        # 并发编辑：如实 409，让调用方重新读取后再提交，
        # 而不是把别人的修改静默覆盖掉
        raise HTTPException(409, str(e))
    except PermissionError as e:
        raise HTTPException(403, str(e))


@router.patch("/chunks")
async def api_chunk_batch_edit(request: Request,
                               body: ChunkBatchEditReq) -> ChunkBatchEditedOut:
    """批量编辑：一次读取、一次 embed、一次落地（DESIGN §8.2）。"""
    ctx = get_ctx(request)
    edits = [e.model_dump() for e in body.edits]
    try:
        return await asyncio.to_thread(
            batch_edit_chunks, ctx.store, ctx.registry.embedder, edits)
    except KeyError as e:
        # KeyError 的 str() 是 repr（会带引号），对用户是噪声
        raise HTTPException(404, str(e).strip("'\""))
    except PermissionError as e:
        raise HTTPException(403, str(e))


@router.delete("/chunks/{chunk_id}")
async def api_chunk_del(request: Request, chunk_id: str) -> OkOut:
    ctx = get_ctx(request)
    await asyncio.to_thread(remove_chunk, ctx.store, chunk_id)
    return {"ok": True}
