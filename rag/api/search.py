"""检索端点：POST/GET 检索 + Markdown 报告导出。"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from rag.api._common import get_ctx, raise_operation_error
from rag.api.responses import SearchOut, error_responses
from rag.api.schemas import SearchReq
from rag.retrieval.search import search

router = APIRouter(
    tags=["search"],
    responses=error_responses(unavailable=True),
)


class MarkdownResponse(PlainTextResponse):
    """报告导出的响应类型：契约里声明 text/markdown 而不是默认的
    text/plain，调用方据此选择解析方式。"""
    media_type = "text/markdown"


def _build_req_ctx(ctx, body: SearchReq):
    """把请求参数折算成 (cfg, filters, top_k, reranker, opts)。

    集中一处，避免 POST /api/search 与 /report 两处漂移：
    召回层参数覆盖到 cfg，返回层/展示层走 opts，rerank 开关解析成实际对象。
    """
    overrides: dict = {}
    if body.mode:
        overrides["mode"] = body.mode
    if body.window is not None:
        overrides["window"] = body.window
    # 召回层：只在显式给出时覆盖，缺省沿用服务端配置
    if body.candidate_k is not None:
        overrides["candidate_k"] = body.candidate_k
    if body.nprobes is not None:
        overrides["nprobes"] = body.nprobes
    if body.refine_factor is not None:
        overrides["refine_factor"] = body.refine_factor
    if body.k_rrf is not None:
        overrides["k_rrf"] = body.k_rrf
    cfg = ctx.settings.retrieve.model_copy(update=overrides) if overrides \
        else ctx.settings.retrieve

    # exclude_none 是必需的：filters 现在是 SearchFilters 模型，
    # 未设置的维度是 None，若原样带下去 _build_where 会去查
    # "mime = NULL"（永远不匹配）而不是「不过滤」。
    filters = (body.filters.model_dump(exclude_none=True)
               if body.filters else {})
    # 顶层 kb_id 优先级高于 filters.kb_id（兼容旧调用方）
    if body.kb_id:
        filters["kb_id"] = body.kb_id

    # rerank=false 时显式关闭精排；缺省沿用服务端配置的 reranker
    reranker = None if body.rerank is False else ctx.registry.reranker

    opts = {
        "offset": body.offset,
        "score_threshold": body.score_threshold,
        "group_by_doc": body.group_by_doc,
        "include_context": body.include_context,
        "snippet_chars": body.snippet_chars,
        "highlight": body.highlight,
    }
    return cfg, filters, body.top_k, reranker, opts


async def _run(ctx, body: SearchReq) -> dict:
    cfg, filters, top_k, reranker, opts = _build_req_ctx(ctx, body)
    try:
        return await asyncio.to_thread(
            search, ctx.store, ctx.registry.embedder, reranker,
            body.q, cfg, filters, top_k, **opts)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise_operation_error(e, "检索失败")


@router.post("/search")
async def api_search(request: Request, body: SearchReq) -> SearchOut:
    return await _run(get_ctx(request), body)


@router.get("/search")
async def api_search_get(
        request: Request, q: str, mode: str = "",
        top_k: int = 0, window: int = -1,
        offset: int = 0, score_threshold: float | None = None,
        group_by_doc: bool = False, include_context: bool = True,
        snippet_chars: int | None = None, highlight: bool = True,
        doc_ids: str = "", origin: str = "",
        mime: str = "", parser_engine: str = "",
        include_disabled: bool = False,
        kb_id: str = "") -> SearchOut:
    """GET 便捷检索（外部脚本 / curl 直接调用）。

    与 POST 版等价，只是把参数平铺到 query string：
    doc_ids 逗号分隔，其余同名同义。
    """
    filters: dict = {}
    if doc_ids:
        filters["doc_ids"] = [d for d in doc_ids.split(",") if d]
    if origin:
        filters["origin"] = origin
    if mime:
        filters["mime"] = mime
    if parser_engine:
        filters["parser_engine"] = parser_engine
    if include_disabled:
        filters["include_disabled"] = True
    body = SearchReq(
        q=q,
        mode=mode or None,
        top_k=top_k if top_k > 0 else None,
        window=window if window >= 0 else None,
        offset=offset,
        score_threshold=score_threshold,
        group_by_doc=group_by_doc,
        include_context=include_context,
        snippet_chars=snippet_chars,
        highlight=highlight,
        filters=filters,
        kb_id=kb_id or None,
    )
    return await _run(get_ctx(request), body)


@router.post("/search/report", response_class=MarkdownResponse)
async def api_report(request: Request, body: SearchReq) -> MarkdownResponse:
    """导出 Markdown 检索报告（查询词/模式/耗时/命中明细）。"""
    ctx = get_ctx(request)
    # 报告是给人看的：关掉 HTML 高亮，避免 <mark> 混进 Markdown
    body = body.model_copy(update={"highlight": False})
    res = await _run(ctx, body)
    md = render_report(body.q, res)
    return MarkdownResponse(md)


def render_report(q: str, res: dict) -> str:
    lines = [
        "# 检索报告",
        "",
        f"- 查询：{q}",
        f"- 时间：{datetime.now(timezone.utc).isoformat()}",
        f"- 模式：{res['mode']}",
        f"- 耗时：{res['took_ms']}ms",
        f"- 候选：{res.get('total_candidates', 0)}"
        f"，命中：{len(res['results'])}",
        f"- 分数口径：{res.get('score_kind', 'none')}（越大越相关）",
    ]
    if res.get("offset"):
        lines.append(f"- 偏移：{res['offset']}")
    if res.get("degraded_reason"):
        lines.append(f"- 降级：{res['degraded_reason']}")
    lines += ["", "## 命中结果", ""]
    for i, r in enumerate(res["results"], 1):
        scores = r.get("scores", {})
        lines += [
            f"### {i}. {r['title']}",
            "",
            f"- chunk_id：`{r['chunk_id']}`",
            f"- 位置：{r['heading_path'] or '（无层级）'} | "
            f"page={r['page']} | ordinal={r['ordinal']}",
            f"- 分数：{r['score']:.4f}（{r.get('score_kind', '')}）"
            f" | vector_distance={scores.get('vector_distance', 0):.4f}"
            f" | fts={scores.get('fts', 0):.4f}",
            f"- 引擎：{r['parser_engine']} | edited={r['edited']}"
            f" | origin={r['origin']}",
            "",
            # snippet 是结构化的 {text, marks}；Markdown 引用块里放纯文本
            # 即可（`**命中**` 由 marks 表达）。早前这里是拼好的 <mark> HTML，
            # Markdown 渲染器根本不会解析它，只会在报告里露出一堆标签。
            f"> {r['snippet']['text'] if isinstance(r.get('snippet'), dict) else r.get('snippet', '')}",
            "",
        ]
    return "\n".join(lines)
