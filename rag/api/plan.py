"""分块方案端点：知识库 / 文档的方案读写与重切分。

继承：文档覆盖 > 知识库 > 全局 settings.split。
方案变更不影响已入库内容——需显式调用 /resplit 才会按新方案重切。
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request

from rag.api._common import _bind, get_ctx, raise_operation_error
from rag.api._common import enqueue as _enqueue
from rag.api.responses import DocPlanOut, DocPlansOut, IngestOut, KbPlanOut, error_responses
from rag.api.schemas import PlanUpdateReq
from rag.ingest import pipeline
from rag.storage import plan as chunking
from rag.storage.repos import get_document, list_documents
from rag.storage.repos import kbs as kbs_store

router = APIRouter(
    tags=["plans"],
    responses=error_responses(not_found=True, unavailable=True),
)


def _split_payload(plan: dict) -> dict:
    return {"chunk_size": plan["chunk_size"],
            "overlap_ratio": plan["overlap_ratio"],
            "overlap_chars": plan["overlap_chars"],
            "source": plan["source"]}


@router.get("/kbs/{kb_id}/plan")
async def api_kb_plan(request: Request, kb_id: str) -> KbPlanOut:
    ctx = get_ctx(request)
    if not await asyncio.to_thread(kbs_store.get_kb, ctx.store, kb_id):
        raise HTTPException(404, "knowledge base not found")
    return await asyncio.to_thread(_kb_plan, ctx.store, ctx.settings, kb_id)


@router.put("/kbs/{kb_id}/plan")
async def api_kb_plan_put(request: Request, kb_id: str,
                          body: PlanUpdateReq) -> KbPlanOut:
    ctx = get_ctx(request)
    if not await asyncio.to_thread(kbs_store.get_kb, ctx.store, kb_id):
        raise HTTPException(404, "knowledge base not found")

    def _w():
        chunking.set_kb_plan(ctx.store, kb_id,
                             chunk_size=body.chunk_size or 0,
                             overlap_ratio=body.overlap_ratio or 0.0,
                             settings=ctx.settings)
        return _kb_plan(ctx.store, ctx.settings, kb_id)

    return await asyncio.to_thread(_w)


@router.get("/documents/{doc_id}/plan")
async def api_doc_plan(request: Request, doc_id: str) -> DocPlanOut:
    ctx = get_ctx(request)
    return await asyncio.to_thread(_doc_plan, ctx.store, ctx.settings,
                                   doc_id)


@router.put("/documents/{doc_id}/plan")
async def api_doc_plan_put(request: Request, doc_id: str,
                           body: PlanUpdateReq) -> DocPlanOut:
    ctx = get_ctx(request)

    def _w():
        try:
            chunking.set_doc_plan(ctx.store, doc_id,
                                  chunk_size=body.chunk_size,
                                  overlap_ratio=body.overlap_ratio or 0.0)
        except KeyError:
            raise HTTPException(404, "document not found")
        return _doc_plan(ctx.store, ctx.settings, doc_id)

    return await asyncio.to_thread(_w)


@router.post("/documents/{doc_id}/resplit")
async def api_doc_resplit(request: Request, doc_id: str,
                          wait: bool = True) -> IngestOut:
    """按当前生效方案重切该文档（保留手工块）。

    wait=false 时立即返回 {job_id, status: "queued"}，客户端轮询
    /api/jobs/{job_id}。**必须走队列**：重切是一次全量删除+重建，
    大文档同步执行会把 HTTP 连接挂住直到超时。
    """
    ctx = get_ctx(request)
    try:
        return await asyncio.to_thread(
            _enqueue, ctx, _bind(ctx, pipeline.resplit_doc),
            doc_id, ctx.settings.parser, ctx.settings, queue=ctx.queue,
            wait=wait, doc_id=doc_id)
    except KeyError:
        raise HTTPException(404, "document not found")
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001
        # 旧实现只捕 KeyError/ValueError，其余异常裸抛成 500 且把完整
        # traceback 回传客户端——本页其它写端点都有兜底，这里不能例外。
        raise_operation_error(e, "重切分失败")


def _kb_plan(store, settings, kb_id: str) -> dict:
    own = chunking.kb_plan(store, kb_id, settings) or {
        "chunk_size": 0, "overlap_ratio": 0.0, "custom": False}
    effective = chunking.resolve_plan(store, settings, kb_id=kb_id)
    return {
        "scope": "kb",
        "kb_id": kb_id,
        "own": {"chunk_size": own["chunk_size"],
                "overlap_ratio": own["overlap_ratio"],
                "custom": own["custom"]},
        "effective": _split_payload(effective),
        "global": _split_payload(chunking.global_plan(settings)),
    }


def _doc_plan(store, settings, doc_id: str) -> dict:
    row = get_document(store, doc_id)
    if row is None:
        raise HTTPException(404, "document not found")
    kb_id = str(row.get("kb_id") or "")   # 真实列，无需解析 meta
    own = chunking.doc_plan(store, doc_id)
    effective = chunking.resolve_plan(store, settings,
                                      kb_id=kb_id, doc_id=doc_id)
    return {
        "scope": "doc",
        "doc_id": doc_id,
        "kb_id": kb_id,
        "own": {"chunk_size": own["chunk_size"],
                "overlap_ratio": own["overlap_ratio"],
                "custom": own["custom"]},
        "effective": _split_payload(effective),
        # 实际入库时用的参数（改设置后不会自动变，用于解释历史分块）
        "snapshot": own.get("snapshot"),
    }


@router.get("/documents/plans")
async def api_doc_plans(request: Request, kb_id: str = "",
                        limit: int = 200,
                        offset: int = 0) -> DocPlansOut:
    """批量取文档的方案摘要（一次请求解决列表页的 N+1）。

    前端文档列表需要给每行标注「方案已改但还没重切」；逐文档调
    /api/documents/{id}/plan 会让 200 篇文档变成 200 个请求。
    """
    ctx = get_ctx(request)
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    items, _ = await asyncio.to_thread(
        list_documents, ctx.store, kb_id=kb_id, limit=limit, offset=offset)
    doc_ids = [str(d.get("doc_id")) for d in items]
    if not doc_ids:
        return {"items": {}}

    def _all():
        out: dict[str, dict] = {}
        for did in doc_ids:
            try:
                out[did] = _doc_plan(ctx.store, ctx.settings, did)
            except HTTPException:
                continue
        return out

    return {"items": await asyncio.to_thread(_all)}
