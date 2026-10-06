"""分块端点：列表 / 详情 / 新增 / 单条编辑 / 批量编辑 / 批量启停 / 删除。"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request

from rag.api._common import get_ctx, scalar_dict
from rag.api.responses import (ChunkBatchEditedOut, ChunkBatchEnabledOut,
                               ChunkCreatedOut, ChunkEditedOut,
                               ChunkEnabledOut, ChunkListOut, ChunkOut, OkOut,
                               error_responses)
from rag.api.schemas import (ChunkAddReq, ChunkBatchEditReq,
                             ChunkBatchEnabledReq, ChunkEditReq,
                             ChunkEnabledReq)
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
from rag.storage.repos import (count_rows, escape_like,
                              escape_sql, scalar_rows)

router = APIRouter(
    tags=["chunks"],
    responses=error_responses(not_found=True, unavailable=True),
)

# 列表要返回的列。抽出来是因为加了 q 过滤之后有两条取数路径，
# 列清单写两遍迟早不一致（一条少返回 offset_valid，前端的高亮就会静默不生效）。
CHUNK_LIST_COLS = [
    "chunk_id", "doc_id", "kb_id", "ordinal", "origin", "edited",
    "enabled", "token_count", "heading_path", "text", "created_at",
    "updated_at",
    # 原文对照需要：page 用于 PDF 跳页，char_* 用于文本高亮，
    # offset_valid 决定偏移是否可信
    "page", "char_start", "char_end", "offset_valid",
]


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
    conds: list[str] = []
    if doc_id:
        conds.append(f"doc_id = '{escape_sql(doc_id)}'")
    elif kb_id:
        # kb_id 在入库时即写入每个分块，故库下全部块（含独立分块）都能命中
        conds.append(f"kb_id = '{escape_sql(kb_id)}'")
    if only_standalone:
        # doc_id='' 是「独立分块」的存储形态，须下推到 SQL 而非事后过滤
        conds.append("doc_id = ''")
    needle = (q or "").strip()
    if needle:
        # 子串过滤下推成 ILIKE（本机实测对中文有效），并且必须过 escape_like：
        # 查询词含 % 或 _ 时不能当通配符，否则「包含 foo」会变成「foo 开头」。
        conds.append(f"text ILIKE '%{escape_like(needle)}%'")
    where = " AND ".join(conds) if conds else None

    def _fetch():
        # 排序 + 分页 + total 全部下推，走 lance 原生投影（scalar_rows）。
        # 改前是 `search().select(...).order_by(...).limit().offset()`：
        # 同一条页在本机慢 ~3×（6.8ms vs 2.3ms），且带 q 时是「取回全表
        # 再在 Python 里筛+切片」——一页数据把整库正文搬进内存。
        #
        # 次级排序键补了 chunk_id：ordinal **只在单文档内唯一**，
        # 按库或不带条件列时它会大量重复，只有 ordinal 排序的分页
        # 在并列处会重复/漏项（旧注释说「按它排分页才不会重复/漏项」，
        # 那个前提只在 doc_id 过滤下成立）。
        rows = scalar_rows(ctx.store.chunks, cols=CHUNK_LIST_COLS,
                           where=where,
                           order_by=[("ordinal", True), ("chunk_id", True)],
                           limit=limit, offset=offset or None)
        total = count_rows(ctx.store.chunks, where)
        return [scalar_dict(r) for r in rows], total

    items, total = await asyncio.to_thread(_fetch)
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
