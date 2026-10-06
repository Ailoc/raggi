"""生成式问答端点（受 features.answer 开关控制，默认关闭）。

同时提供两种消费方式：
- `POST /answer`        一次性返回完整答案（脚本 / 非流式客户端）
- `POST /answer/stream` SSE 逐段推送（界面：先出引用，再出文字）
"""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from rag.retrieval.answer import answer, answer_stream
from rag.api._common import cfg_with_overrides, get_ctx, raise_operation_error
from rag.api.responses import AnswerOut, error_responses
from rag.api.schemas import AnswerReq

router = APIRouter(
    tags=["answer"],
    responses=error_responses(unavailable=True),
)


def _require_answer_enabled(ctx) -> None:
    if not ctx.settings.features.answer:
        raise HTTPException(
            503, "生成式问答未启用：在配置中设置 features.answer=true")


def _filters(body: AnswerReq) -> dict:
    """检索过滤条件：filters 与顶层 kb_id 合并。

    kb_id 必须在这里并进 filters，否则该字段形同虚设——
    请求里写了 kb_id 却不生效，调用方会以为答的是那个库。
    """
    filters = dict(body.filters or {})
    if body.kb_id:
        filters["kb_id"] = body.kb_id
    return filters


@router.post("/answer")
async def api_answer(request: Request, body: AnswerReq) -> AnswerOut:
    ctx = get_ctx(request)
    _require_answer_enabled(ctx)
    cfg = cfg_with_overrides(ctx.settings, body.mode, body.window)
    try:
        return await answer(
            ctx.store, ctx.registry.embedder, ctx.registry.reranker,
            ctx.registry.chat(), body.q, cfg, _filters(body),
            body.top_k)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise_operation_error(e, "问答失败")


def _sse(event: str, data: dict) -> str:
    """一个 SSE 事件。

    手工序列化而非用 sse-starlette：事件只有 4 种、字段固定，多引一个依赖
    不划算。data 里的换行必须拆成多行 data:（SSE 规范），JSON 序列化后
    天然不含裸换行，但仍走 json.dumps 保证转义正确。
    """
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


@router.post("/answer/stream",
             responses={
                 200: {
                     "description": "SSE 事件流：sources → delta* → done",
                     "content": {"text/event-stream": {}},
                 },
             })
async def api_answer_stream(request: Request,
                            body: AnswerReq) -> StreamingResponse:
    """问答流式输出（SSE）。

    事件顺序固定：sources（引用表，先于正文）→ delta（增量文本，多次）
    → done（耗时与降级说明）。出错时推 error 事件而不是断连——
    断连会让前端只剩一个卡住的 loading，用户无从判断发生了什么。
    """
    ctx = get_ctx(request)
    _require_answer_enabled(ctx)
    cfg = cfg_with_overrides(ctx.settings, body.mode, body.window)

    async def gen():
        try:
            async for item in answer_stream(
                    ctx.store, ctx.registry.embedder,
                    ctx.registry.reranker, ctx.registry.chat(),
                    body.q, cfg, _filters(body), body.top_k):
                yield _sse(item["event"], item["data"])
        except asyncio.CancelledError:
            # 客户端断开：不必再写事件，也不该记成失败
            raise
        except Exception as e:  # noqa: BLE001
            yield _sse("error", {
                "message": str(e)[:400],
                "hint": "问答失败；若为模型服务问题，可用 /api/models/test 验证连通性",
            })

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            # 反向代理下必须关缓冲，否则事件会被攒着一起发，流式就没了
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
