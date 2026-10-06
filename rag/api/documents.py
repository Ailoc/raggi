"""文档与入库端点：上传（留档原文）/ 文本 / 批量 / URL / 列表 / 详情 / 删除 / 重解析。"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path
from typing import Optional

from fastapi import (APIRouter, File, Form, HTTPException, Request,
                     Response, UploadFile)
from fastapi.responses import FileResponse

from rag.api._common import (_bind, enqueue as _enqueue, get_ctx,
                              raise_operation_error, scalar_dict)
from rag.api.responses import (BatchIngestOut, DocDeleteBatchOut,
                               DocumentListOut, DocumentOut,
                               DocumentUpdateOut, ErrorOut, IngestOut, OkOut,
                               error_responses)
from rag.api.schemas import (BatchIngestReq, DocDeleteBatchReq, DocUpdateReq,
                             ReparseReq, TextIngestReq, UrlIngestReq)
from rag.core.errors import Invalid
from rag.core.idempotency import IdempotencyStore, normalize_key
from rag.ingest import pipeline
from rag.ingest.queue import JobCancelled
from rag.parsing.router import ALLOWED_ENGINES
from rag.storage.repos import keys as apikeys
from rag.storage.repos import (
    delete_document,
    delete_documents,
    doc_chunk_index,
    get_document,
    list_documents,
    update_metadata,
)

logger = logging.getLogger("raggi.api.documents")

# 幂等键存储：模块级实例，create_app 会把它挂到 app.state.idem。
# 进程内共享是**有意的**——幂等键的作用域本来就只应是「这一次进程」，
# 多 worker 部署下各持一份只会让重复请求偶尔漏拦，不会造成错误结果。
IDEM: "IdempotencyStore" = IdempotencyStore()


async def _idem(request, path: str, run) -> dict:
    """带 Idempotency-Key 的请求：同键重试直接返回首次结果，不重复入库。

    没有键时就是普通执行——**幂等是可选增强**，不给键的老客户端
    行为不变（内容哈希去重仍会挡住同内容重复入库）。
    """
    key = normalize_key(request)
    if not key:
        return await run()
    cached = IDEM.get(key, path)
    if cached is not None:
        # 回放首次结果，并明确标记：调用方据此知道这是重试而非新提交
        return {**cached, "idempotent_replay": True}
    result = await run()
    # 只记成功的结果：失败重试应该真能再试一次
    if result.get("status") != "failed":
        IDEM.put(key, path, result)
    return result


router = APIRouter(
    tags=["documents"],
    responses=error_responses(not_found=True, unavailable=True),
)


def _doc_chunks(store, doc_id: str) -> list[dict]:
    """文档详情的分块索引（窄列，一次取回，按 ordinal 升序）。

    走 `doc_chunk_index`：排序下推给 Lance，不在 Python 里 sort；
    列清单收在仓储层，与检索上下文用的是同一份定义。
    """
    return doc_chunk_index(store, doc_id)


def _safe_suffix(filename: str | None) -> str:
    """上传文件的后缀白名单。

    后缀会决定 router.route() 选哪个解析引擎，因此必须是**我们自己**的
    决定，而不是「用户输入的最后一个点之后」——早前直接取 Path.suffix，
    `x.pdf.exe` 会得到 .exe，空/超长/带路径的名字还会生成畸形临时文件名。
    """
    raw = Path(filename or "").suffix.lower()
    if not raw or len(raw) > 16:
        return ""
    if not re.fullmatch(r"\.[a-z0-9]{1,15}", raw):
        return ""
    return raw


def _ingest_upload(ctx, tmp: Path, title, parser_cfg, filename: str,
                   kb_id: str, job_id: str | None = None) -> dict:
    """任务体：入库上传的临时文件，并负责它的生命周期。

    临时文件的所有权归任务体：
    - 成功时 pipeline 已把它 move 到 files_dir（留档），无需再删；
    - 失败时由这里删除，避免磁盘泄漏。

    这样做是必须的——若让 HTTP 处理器用 finally 清理，异步模式下
    请求早已返回，会删掉 worker 正在读的文件。
    """
    try:
        # 全用关键字：位置参数一旦与签名顺序不一致就会串位
        # （此前 filename 落到 backend、kb_id 落到 source_uri）
        return pipeline.ingest_file(
            ctx.store, ctx.registry, tmp, title, parser_cfg, ctx.settings,
            backend=ctx.backend, source_uri=filename, kb_id=kb_id,
            job_id=job_id, queue=ctx.queue)
    except JobCancelled:
        # 取消也要清理临时文件：任务体终止后没人再管它
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


@router.post("/documents", responses={
    413: {"model": ErrorOut, "description": "文件超过 max_upload_mb"},
})
async def api_upload(request: Request,
                     file: Optional[UploadFile] = File(None),
                     engine: Optional[str] = Form(None),
                     title: Optional[str] = Form(None),
                     kb_id: Optional[str] = Form(None),
                     wait: bool = True) -> IngestOut:
    ctx = get_ctx(request)
    if file is None:
        raise HTTPException(400, "需要上传文件")
    # 幂等检查必须在**读文件与落盘之前**：重试请求不该把大文件再读一遍
    # 内存、再写一次临时文件。命中缓存就直接回放首次结果。
    key = normalize_key(request)
    if key:
        cached = IDEM.get(key, "/documents")
        if cached is not None:
            return {**cached, "idempotent_replay": True}

    # 流式落盘 + 边写边计数：file.read(n) 会先把 n+1 字节**全部**读进
    # 内存再比较，10 个并发 50MB 上传就是 500MB 常驻（还要 ×2 写盘）。
    # 分块写让峰值内存与单块大小同阶，且超过上限立刻中断。
    max_bytes = ctx.settings.max_upload_mb * 1024 * 1024
    chunk_size = 1024 * 1024
    total = 0
    # 请求级引擎覆盖（前端下拉框）
    parser_cfg = ctx.settings.parser
    if engine and engine != "auto" and engine in ALLOWED_ENGINES:
        parser_cfg = parser_cfg.model_copy(update={"default": engine})
    suffix = _safe_suffix(file.filename)
    tmp = ctx.settings.tmp_dir / f"{os.urandom(8).hex()}{suffix}"
    # 先写 .part 再 rename：写一半被 kill 只会留下可识别的残片，
    # 不会在 tmp_dir 里留下一个「看起来完整」的半截文件骗过后续处理。
    part = tmp.with_suffix(tmp.suffix + ".part")
    try:
        with part.open("wb") as fh:
            while True:
                block = await file.read(chunk_size)
                if not block:
                    break
                total += len(block)
                if total > max_bytes:
                    raise HTTPException(
                        413, f"文件超过上限 {ctx.settings.max_upload_mb}MB")
                fh.write(block)
        part.replace(tmp)
    except HTTPException:
        part.unlink(missing_ok=True)
        raise
    except Exception:
        part.unlink(missing_ok=True)
        raise_operation_error(RuntimeError("写临时文件失败"), "文件入库失败")
    try:
        from functools import partial

        result = await asyncio.to_thread(
            _enqueue, ctx, partial(_ingest_upload, ctx),
            tmp, title, parser_cfg, file.filename, kb_id or "",
            wait=wait)
        if key and result.get("status") != "failed":
            IDEM.put(key, "/documents", result)
        return result
    except HTTPException:
        # 入队失败（如队列已满）：任务体从未接管该文件，由请求方清理
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    except Exception as e:  # noqa: BLE001
        raise_operation_error(e, "文件入库失败")


@router.post("/documents/text")
async def api_text(request: Request, body: TextIngestReq,
                   wait: bool = True) -> IngestOut:
    ctx = get_ctx(request)

    async def _run():
        return await asyncio.to_thread(
            _enqueue, ctx, _bind(ctx, pipeline.ingest_text),
            body.text, body.title, ctx.settings.parser,
            ctx.settings, wait=wait, kb_id=body.kb_id or "")

    try:
        return await _idem(request, "/documents/text", _run)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise_operation_error(e, "入库失败")


@router.post("/documents/batch")
async def api_batch(request: Request, body: BatchIngestReq,
                    wait: bool = True) -> BatchIngestOut:
    """批量文本入库（外部接口可编程调用）。

    逐条提交到队列：单条失败不影响其余，失败项在 results 里带 error。
    异步模式只返回各条的 job_id，由调用方自行轮询。
    """
    ctx = get_ctx(request)

    def _run():
        out = []
        for item in body.items:
            try:
                out.append(_enqueue(
                    ctx, _bind(ctx, pipeline.ingest_text),
                    item.text, item.title,
                    ctx.settings.parser, ctx.settings,
                    wait=wait, kb_id=item.kb_id or ""))
            except Exception as e:  # noqa: BLE001
                out.append({"title": item.title,
                            "status": "failed", "error": str(e)})
        return out

    results = await asyncio.to_thread(_run)
    return {"total": len(results), "results": results}


@router.post("/documents/url")
async def api_url(request: Request, body: UrlIngestReq,
                  wait: bool = True) -> IngestOut:
    ctx = get_ctx(request)

    async def _run():
        return await asyncio.to_thread(
            _enqueue, ctx, _bind(ctx, pipeline.ingest_url),
            body.url, ctx.settings.parser, ctx.settings,
            wait=wait, kb_id=body.kb_id or "")

    try:
        return await _idem(request, "/documents/url", _run)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise_operation_error(e, "URL 入库失败")


@router.get("/documents")
async def api_list(request: Request, q: str = "", status: str = "",
                   parser_engine: str = "", mime: str = "",
                   kb_id: str = "",
                   limit: int = 50, offset: int = 0) -> DocumentListOut:
    ctx = get_ctx(request)
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    rows, total = await asyncio.to_thread(
        list_documents, ctx.store, q=q, status=status,
        parser_engine=parser_engine, mime=mime, kb_id=kb_id,
        limit=limit, offset=offset)
    return {"total": total, "items": rows}


@router.get("/documents/{doc_id}")
async def api_doc(request: Request, doc_id: str) -> DocumentOut:
    ctx = get_ctx(request)
    row = await asyncio.to_thread(get_document, ctx.store, doc_id)
    if row is None:
        raise HTTPException(404, "document not found")
    row = scalar_dict(row)
    # 分块索引（不含向量）
    row["chunks"] = await asyncio.to_thread(
        _doc_chunks, ctx.store, doc_id)
    # 原文预览地址：iframe/img 无法带 Authorization 头，
    # 故启用鉴权时改用短期签名 URL
    row["has_file"] = bool(row.get("stored_file"))
    row["file_url"] = _file_url(ctx, doc_id) if row["has_file"] else None
    return row


def _file_url(ctx, doc_id: str) -> str:
    """留档原文的可用 URL（按是否启用鉴权决定要不要签名）。"""
    auth_on = bool((ctx.settings.token or "").strip()) or apikeys.has_keys(
        ctx.store)
    return ctx.signer.file_url(doc_id, auth_enabled=auth_on)


@router.get("/documents/{doc_id}/file",
            response_class=FileResponse,
            responses={
                200: {
                    "description": "原文字节流（Content-Type 随实际文件类型）",
                    "content": {"application/octet-stream": {}},
                },
                410: {"model": ErrorOut, "description": "留档原文已丢失"},
            })
async def api_doc_file(request: Request, doc_id: str,
                       exp: int = 0, sig: str = "",
                       download: bool = False) -> Response:
    """提供留档原文的原始字节，供浏览器内联预览。

    响应类型为二进制流（Content-Type 随实际文件），
    `download=true` 时加 Content-Disposition: attachment。

    鉴权由全局中间件统一处理（见 auth.authenticate）：
    - 常规请求头（Authorization / X-API-Key）；或
    - 查询串签名（exp + sig）——`<iframe>`/`<img>` 无法携带请求头，
      故签名版链接是浏览器内嵌预览的唯一可行方式。

    这里只负责安全地取出文件并决定内联还是下载。
    """
    ctx = get_ctx(request)
    row = await asyncio.to_thread(get_document, ctx.store, doc_id)
    if row is None:
        raise HTTPException(404, "document not found")
    stored = row.get("stored_file")
    if not stored:
        raise HTTPException(
            404, "该文档没有留档原文（粘贴文本 / URL 入库的文档不保存原文件）")

    mime = str(row.get("mime") or "application/octet-stream")
    disposition = "attachment" if download else "inline"
    name = _quote(str(stored))
    headers = {
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{name}",
        # 签名链接短期有效，允许浏览器缓存几分钟以减少重复解码
        "Cache-Control": "private, max-age=300" if sig else "no-cache",
    }

    def _load():
        """local 走文件路径（支持 Range，大 PDF 分段加载）；
        s3 走对象读取。两条路都不把「存储介质」泄漏给路由层。"""
        backend = ctx.backend
        local = getattr(backend, "local_path", None)
        if callable(local):
            path = backend.local_path(str(stored))
            if path is not None:
                if not path.exists():
                    raise HTTPException(410, "留档原文已丢失（可能被手工删除）")
                return path, None
        if not backend.blob_exists(str(stored)):
            raise HTTPException(410, "留档原文已丢失（可能被手工删除）")
        return None, backend.read_blob(str(stored))

    try:
        path, blob = await asyncio.to_thread(_load)
    except Invalid as e:
        raise HTTPException(400, str(e))

    if path is not None:
        return FileResponse(path, media_type=mime,
                            headers={**headers, "Accept-Ranges": "bytes"})
    return Response(content=blob, media_type=mime, headers=headers)


def _quote(name: str) -> str:
    from urllib.parse import quote
    return quote(name)


@router.put("/documents/{doc_id}")
async def api_update_doc(request: Request, doc_id: str,
                         body: DocUpdateReq) -> DocumentUpdateOut:
    """更新文档标题与所属知识库。"""
    ctx = get_ctx(request)
    try:
        row = await asyncio.to_thread(
            update_metadata, ctx.store, doc_id,
            title=body.title, kb_id=body.kb_id)
    except Invalid as e:
        raise HTTPException(400, str(e))
    if row is None:
        raise HTTPException(404, "document not found")
    # 回传实际生效的归属：调用方据此确认「真的移动了」，
    # 而不是只能假设 200 就等于事情办妥了。
    return {"doc_id": doc_id, "title": row.get("title"),
            "kb_id": str(row.get("kb_id") or "")}


# 必须注册在 DELETE /documents/{doc_id} 之前：字面量路径若排在
# 参数化路由之后，会被当成 doc_id="batch" 而返回 404。
@router.delete("/documents/batch")
async def api_del_docs_batch(request: Request,
                             body: DocDeleteBatchReq) -> DocDeleteBatchOut:
    """批量删除文档（级联分块与留档原文）。

    与 DELETE /documents/{id} 的差别是**一次加锁 + 归档先取出**：
    逐条删 50 篇时写锁窗口被切成 50 段，且归档文件名在删掉后就查不到。
    请求里已不存在的 ID 会被跳过并在 skipped 里如实回报。
    """
    ctx = get_ctx(request)
    return await asyncio.to_thread(
        delete_documents, ctx.store, body.doc_ids, ctx.backend)


@router.delete("/documents/{doc_id}")
async def api_del_doc(request: Request, doc_id: str) -> OkOut:
    ctx = get_ctx(request)
    # 同时清理留档原文（DESIGN §14）
    await asyncio.to_thread(
        delete_document, ctx.store, doc_id, ctx.backend)
    return {"ok": True}


@router.post("/documents/{doc_id}/reparse")
async def api_reparse(request: Request, doc_id: str,
                      body: Optional[ReparseReq] = None,
                      wait: bool = True) -> IngestOut:
    """用留档原文重新解析（保留 doc_id，支持 engine 覆盖）。"""
    ctx = get_ctx(request)
    engine = body.engine if body else None
    try:
        return await asyncio.to_thread(
            _enqueue, ctx, _bind(ctx, pipeline.reparse_doc),
            doc_id, ctx.settings.parser, ctx.settings, ctx.backend,
            # engine 可能是 None（请求未指定），而任务行的 parser_engine
            # 是非空列——这里归一成空串，交给 pipeline 走"沿用当前配置"
            wait=wait, doc_id=doc_id, engine=engine or "")
    except KeyError:
        raise HTTPException(404, "document not found")
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001
        raise_operation_error(e, "重解析失败")
