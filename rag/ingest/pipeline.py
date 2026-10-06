"""入库流水线：路由→加载→切分→批量并发向量化→幂等写入→后台建索引→对账。

性能关键（重构）：
- 写锁仅覆盖「去重检查 + 写库」短临界区；embedding（慢操作）在锁外并发执行
- 并发 embedding：按 embed.concurrency 分片，ThreadPoolExecutor 并行
- 索引维护后台化：schedule_maintenance() 去抖合并，入库响应不等待
- 偏移计算：span 内游标查找（重复内容不错位）+ 边界 clamp
"""
from __future__ import annotations

import datetime
import hashlib
import json
import logging
import mimetypes
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from rag.core.config import ParserConfig, Settings, SplitConfig
from rag.ingest.queue import JobCancelled
from rag.ingest.splitter import split_text
from rag.models.http import ScaledSemaphore
from rag.models.registry import ModelRegistry
from rag.parsing.loaders import load_text, load_url
from rag.parsing.normalize import normalize
from rag.parsing.router import ALLOWED_ENGINES, route
from rag.parsing.segment import segment_batch
from rag.storage.plan import resolve_plan, stamp_snapshot
from rag.storage.repos import (
    escape_sql,
    fetch_rows,
    find_by_hash,
    get_document,
    jobs,
    scalar_row,
    set_doc_fields,
    upsert_chunks,
    upsert_documents,
)
from rag.storage.tables import LanceStore

logger = logging.getLogger("raggi.ingest")

# 文档正文上限：解析器/网页可能产出超大文本，全量切分会拖垮向量化
MAX_DOC_CHARS = 20 * 1024 * 1024


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _job_row(job_id, doc_id, stage, progress, total, engine, error=None, ended=None):
    return {
        "job_id": job_id,
        "doc_id": doc_id,
        # 直接调 pipeline（不经队列）时也要带上 kb_id：列是 jobs 表
        # schema 的一部分，缺它会被 LanceDB 判为 "different schema" 拒收。
        # 直调场景确实没有队列上下文，留空即可。
        "kb_id": "",
        "stage": stage,
        "progress": progress,
        "total": total,
        "parser_engine": engine,
        "error": error,
        "started_at": _now(),
        "ended_at": ended,
    }


def _finish_job(store: LanceStore, job_id: str, stage: str, engine: str,
                error=None) -> None:
    """把任务行更新为终态（done / failed / cancelled），失败不影响主流程。"""
    jobs.set_job(store, job_id, stage=stage, progress=1.0,
                 parser_engine=engine, error=error, ended_at=_now())


def _set_stage(store: LanceStore, job_id: str, stage: str,
               engine: str | None = None, total: int | None = None) -> None:
    """更新任务的进行中阶段（观测数据，失败不影响主流程）。"""
    values: dict = {"stage": stage}
    if engine is not None:
        values["parser_engine"] = engine
    if total is not None:
        values["total"] = total
    # 观测数据：写不进去不该影响入库本身
    jobs.set_job(store, job_id, **values)


def _check_cancelled(queue, job_id: str) -> None:
    """阶段边界检查取消请求。

    只能这样实现：解析（PyMuPDF）与向量化（HTTP 请求）都不响应中断，
    已发出的调用必须跑完。检查点选在**阶段之间**——此时没有任何
    半成品需要回滚，退出是干净的。
    """
    if queue is not None and queue.is_cancelled(job_id):
        raise JobCancelled(f"任务 {job_id} 已取消")


def _set_job_doc(store: LanceStore, job_id: str, doc_id: str) -> None:
    """把产出的 doc_id 回写到任务行。

    异步调用方只拿得到 job_id；若不回写 doc_id，任务结束后无从知道
    这批内容对应哪篇文档——只能去文档列表里靠标题猜。
    """
    if not doc_id:
        return
    jobs.set_job(store, job_id, doc_id=doc_id)


def _mark_doc_failed(store: LanceStore, doc_id: str, error: str) -> None:
    """入库失败时把已有文档标记为 failed（分块保留，便于重试与排查）。

    修复：此前失败只更新 job，文档仍显示 ready，用户看到的“正常文档”
    实际没有可检索内容，health 却报计数不一致，信号互相矛盾。
    """
    try:
        set_doc_fields(store, doc_id, status="failed",
                       error=str(error)[:500], updated_at=_now())
    except Exception as e:  # noqa: BLE001
        logger.debug("标记文档失败态出错: %s", e)


# 进程级 embedding 并发闸：并发**任务**数 × 每个任务内的分片并发
# 会相乘（默认 2×4=8），无上限时远端 embedding 服务会开始限流，
# 而 _is_retryable 认 429 → 每个分片各自重试 3 次退避，
# 于是变成级联重试风暴：越限流越多重试，越多重试越限流。
# 信号量让总量封顶，重试也挤在同一闸内，不会放大。
_EMBED_SEMAPHORE_DEFAULT = 8


def _embed_gate(cfg) -> "ScaledSemaphore":
    """进程级 embedding 并发闸（按配置定尺寸）。

    闸门大小必须是**可配**的：它要匹配远端 provider 的配额，
    而硬编码 8 在换 provider / 换部署形态时就成了一个猜出来的数。
    诚实说明边界：这是**每进程**一把闸；多进程 API/worker 形态下
    总并发 = 进程数 × embed.concurrency，配置请按单进程理解。
    """
    n = int(getattr(cfg, "concurrency", _EMBED_SEMAPHORE_DEFAULT)
            or _EMBED_SEMAPHORE_DEFAULT)
    return ScaledSemaphore(max(1, min(n * 2, 64)))


def _embed_concurrently(embedder, texts: list[str], cfg) -> list[list[float]]:
    """按 cfg.concurrency 分片并发向量化（远端 embedding 服务真实加速）。

    分片并发受进程级闸门约束：外层入库队列的 worker 数与内层分片
    并发相乘后可能远超 embedding 服务的连接/限流阈值。
    """
    if not texts:
        return []
    gate = _embed_gate(cfg)
    batch = max(1, int(getattr(cfg, "batch", 64) or 64))
    batches = [texts[i:i + batch] for i in range(0, len(texts), batch)]
    workers = max(1, min(int(getattr(cfg, "concurrency", 1) or 1),
                         len(batches)))
    if workers <= 1 or len(batches) == 1:
        out: list[list[float]] = []
        for b in batches:
            with gate:
                out.extend(embedder.embed(b))
        return out

    def _guarded(b):
        with gate:
            return embedder.embed(b)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(_guarded, batches))
    return [v for rs in results for v in rs]


def _build_chunk_rows(parsed, doc_id: str, embed_model: str,
                      split_cfg: SplitConfig,
                      kb_id: str = "") -> list[dict]:
    """切分 + 偏移计算。

    相对偏移与 heading_path 由 splitter 附加（RCT 原样保留内容，
    偏移闭合）；此处换算为相对全文纯文本的绝对偏移并 clamp 到
    span 边界。
    """
    chunk_rows: list[dict] = []
    ordinal = 0
    for src in parsed.spans:
        page = src.page
        sub = split_text(src.content, split_cfg.chunk_size,
                         split_cfg.chunk_overlap)
        for cd in sub:
            rel = int(cd.metadata.get("char_start", -1))
            offset_valid = rel >= 0
            if rel < 0:
                rel = 0
            start = src.start + rel
            end = min(start + len(cd.page_content), src.end)
            chunk_rows.append({
                "chunk_id": str(uuid.uuid4()),
                "doc_id": doc_id,
                "ordinal": ordinal,
                "kb_id": kb_id,
                "text": cd.page_content,
                "text_seg": "",  # 批量分词在外部填充
                "heading_path": cd.metadata.get("heading_path", ""),
                "page": page,
                "char_start": start,
                "char_end": end,
                "token_count": max(1, len(cd.page_content) // 4),
                "origin": "parsed",
                "edited": False,
                "original_text": None,
                "offset_valid": offset_valid,
                "embed_model": embed_model,
                "vector": [],
                "created_at": _now(),
                "updated_at": _now(),
            })
            ordinal += 1
    return chunk_rows


def _run(store: LanceStore, registry: ModelRegistry, engine: str, documents,
         title: str, source_uri: str, job_id: str, parser_cfg: ParserConfig,
         mime: str = "text/plain", split_cfg: SplitConfig | None = None,
         retain_path: Path | None = None,
         backend=None,
         doc_id: str | None = None,
         kb_id: str = "",
         plan: dict | None = None,
         queue=None) -> dict:
    """入库主流程。doc_id 显式给出时为「重解析」：先清旧分块并复用 doc_id。

    kb_id 存于 documents.meta.kb_id（知识库归属；空串 = 未分组）。
    plan 为解析后的分块方案（文档覆盖 > 知识库 > 全局）；给出时
    以其覆盖 split_cfg，并快照进 meta.chunking.snapshot。
    """
    embedder = registry.embedder
    embed_model = embedder.model
    split_cfg = split_cfg or SplitConfig()
    if plan:
        split_cfg = SplitConfig(
            chunk_size=plan["chunk_size"],
            chunk_overlap=plan["overlap_chars"])
    # 归一化（用于 char 偏移/页码映射）
    parsed = normalize(documents)
    full_text = parsed.text[:MAX_DOC_CHARS]
    content_hash = hashlib.sha256(full_text.encode("utf-8")).hexdigest()
    explicit_doc = doc_id is not None

    # 阶段边界 ①：解析完成。此时还没有任何写操作，退出最干净。
    _check_cancelled(queue, job_id)

    # 阶段 1：去重检查（短临界区，不覆盖慢操作）
    # 注意：重解析/重切分**不在此处**删除旧分块——旧数据必须保留到
    # 新分块向量化成功之后（见阶段 3）。否则 embedding 服务一次抖动
    # 就会清空文档内容，而 documents.status 仍显示 ready。
    with store.write_lock():
        if not explicit_doc:
            existing = find_by_hash(store, content_hash)
            if existing:
                did = existing[0]["doc_id"]
                # 重复入库也要回写 doc_id：调用方据此知道「命中了哪一篇」
                _set_job_doc(store, job_id, did)
                _finish_job(store, job_id, "done", engine)
                return {"doc_id": did, "job_id": job_id,
                        "status": "duplicate", "engine": engine,
                        "chunk_count": int(existing[0]["chunk_count"] or 0)}
        if doc_id is None:
            doc_id = str(uuid.uuid4())
    # doc_id 至此确定，立刻回写任务行：异步调用方靠它找到产出的文档
    _set_job_doc(store, job_id, doc_id)
    _set_stage(store, job_id, "split")
    chunk_rows = _build_chunk_rows(parsed, doc_id, embed_model, split_cfg,
                                   kb_id)
    texts = [r["text"] for r in chunk_rows]
    for r, seg in zip(chunk_rows, segment_batch(texts)):
        r["text_seg"] = seg
    _set_stage(store, job_id, "embed", total=len(chunk_rows))
    vectors = _embed_concurrently(embedder, texts,
                                  registry.bundle.embed_cfg)

    # 阶段边界 ②：向量化完成、写库之前。这里是最值得检查的位置——
    # 向量化通常是最慢的一段，让用户在等它时还能取消最有意义；
    # 且此刻旧分块还没被删（显式 doc_id 时），退出不会留下空洞。
    _check_cancelled(queue, job_id)

    for r, v in zip(chunk_rows, vectors):
        r["vector"] = v
    total = len(chunk_rows)

    # 阶段 3：写库（短临界区，双重去重检查防并发重复）
    _set_stage(store, job_id, "write", total=total)
    with store.write_lock():
        if not explicit_doc:
            existing = find_by_hash(store, content_hash)
            if existing:
                did = existing[0]["doc_id"]
                # 并发下另一请求抢先写入同一内容：同样回写命中的 doc_id
                _set_job_doc(store, job_id, did)
                _finish_job(store, job_id, "done", engine)
                return {"doc_id": did, "job_id": job_id,
                        "status": "duplicate", "engine": engine,
                        "chunk_count": int(existing[0]["chunk_count"]
                                            or 0)}

        # 重解析保留旧文档的归属与留档；新入库由 kb_id 与原文留档构成。
        # kb_id / stored_file 是真实列，meta 只承载嵌套的 chunking 结构。
        old_row = get_document(store, doc_id) if explicit_doc else None
        if not kb_id and old_row:
            kb_id = str(old_row.get("kb_id") or "")
        stored_file = old_row.get("stored_file") if old_row else None
        if retain_path is not None and backend is not None:
            # 原文留档：交给存储后端（local 是同盘 move，s3 是上传对象）
            stored = f"{doc_id}{Path(retain_path).suffix}"
            try:
                _archive(backend, Path(retain_path), stored)
                stored_file = stored
            except Exception as e:  # noqa: BLE001
                logger.warning("原文留档失败: %s", e)
        meta = json.dumps(
            _meta_of(old_row.get("meta")) if old_row else {})
        created_at = _now()
        if old_row:
            # 重解析/重切分不覆盖 created_at（入库时间保持首入时间）
            created_at = old_row.get("created_at") or created_at

        # 重解析/重切分：新向量已就绪，此时才清旧块。删除与写入在同一
        # 临界区内完成；向量化失败根本走不到这里，旧分块原样保留。
        if explicit_doc:
            store.chunks.delete(
                where=f"doc_id = '{escape_sql(doc_id)}'")
        upsert_chunks(store, chunk_rows)
        upsert_documents(store, [{
            "doc_id": doc_id,
            "title": title,
            "source_uri": source_uri,
            "mime": mime,
            "parser_engine": engine,
            "content_hash": content_hash,
            "char_count": len(full_text),
            "chunk_count": total,
            "text": full_text,
            "status": "ready",
            "error": None,
            "kb_id": kb_id or "",
            "stored_file": stored_file,
            "meta": meta,
            "created_at": created_at,
            "updated_at": _now(),
        }])
        _finish_job(store, job_id, "done", engine)

    # 阶段 3.5：快照生效方案（改设置后仍能解释历史分块的实际参数；
    # 重切分同样刷新，否则改方案后重切会留下过期快照）
    if plan:
        stamp_snapshot(store, doc_id, plan)

    # 阶段 4：后台索引维护（去抖合并，入库响应不等待 FTS 重建）
    store.schedule_maintenance()
    return {"doc_id": doc_id, "job_id": job_id, "status": "ready",
            "engine": engine, "chunk_count": total}


def ingest_file(store: LanceStore, registry: ModelRegistry, path, title: str | None,
                parser_cfg: ParserConfig, settings: Settings, backend=None,
                source_uri: str | None = None,
                kb_id: str = "", job_id: str | None = None,
                queue=None) -> dict:
    """上传文件入库。

    job_id 由队列提供时任务行已存在，这里只推进阶段；直接调用则自行建行。
    """
    path = path if hasattr(path, "suffix") else Path(path)
    job_id = _ensure_job(store, job_id, job_id_doc="")
    try:
        _set_stage(store, job_id, "parse")
        engine, documents = route(path, parser_cfg)
        title = title or path.name
        mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        _set_stage(store, job_id, "load", engine)
        return _run(store, registry, engine, documents, title,
                    source_uri or str(path), job_id, parser_cfg, mime,
                    split_cfg=settings.split,
                    retain_path=path, backend=backend,
                    kb_id=kb_id,
                    plan=resolve_plan(store, settings, kb_id=kb_id),
                    queue=queue)
    except JobCancelled:
        # 取消不是失败：任务行已由 _finish_job 写成 cancelled，
        # 文档状态保持原样（重解析时旧分块还在，不该标成 failed）
        _finish_job(store, job_id, "cancelled", "", error="已取消")
        raise
    except Exception as e:  # noqa: BLE001
        _finish_job(store, job_id, "failed", "", error=str(e))
        raise


def _ensure_job(store: LanceStore, job_id: str | None,
                job_id_doc: str = "") -> str:
    """任务行不存在时补建（队列提交时已建，直接调用的路径需要）。

    入队时队列已写入 stage=queued 的行；这里只在「自己生成 job_id」
    的情况下建行，避免把队列写好的状态覆盖回 queued。
    """
    if job_id:
        return job_id
    new_id = str(uuid.uuid4())
    jobs.add_job(store, _job_row(new_id, job_id_doc, "queued", 0.0, 0, ""))
    return new_id


def _archive(backend, src: Path, name: str) -> None:
    """把上传的临时文件收进归档。

    local 后端支持同盘 rename（零拷贝）；s3 后端走对象上传。
    统一在这里分派，业务层不必知道存储介质。
    """
    move_in = getattr(backend, "move_in", None)
    if callable(move_in):
        move_in(src, name)
        return
    upload = getattr(backend, "put_blob_from_path", None)
    if callable(upload):
        upload(src, name)
        src.unlink(missing_ok=True)
        return
    backend.put_blob(name, src.read_bytes())
    src.unlink(missing_ok=True)


def _meta_of(raw) -> dict:
    """解析 documents.meta（仅剩 chunking 等嵌套结构）。容错返回 {}。"""
    try:
        if not raw:
            return {}
        return json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (TypeError, ValueError):
        return {}


def _doc_kb_id(row: dict) -> str:
    """读取文档的知识库归属（真实列，不再解析 meta）。"""
    return str(row.get("kb_id") or "")


def resplit_doc(store: LanceStore, registry: ModelRegistry, doc_id: str,
                parser_cfg: ParserConfig, settings: Settings,
                job_id: str | None = None, queue=None) -> dict:
    """用当前生效方案重新切分文档（保留 doc_id 与手工块）。

    与 reparse_doc 的区别：不重新解析文件，直接用已存的 documents.text
    重新切分，因此粘贴文本 / URL 入库的文档（无留档原文）也能重切。
    手工新增的块（origin='manual'）在重切后保留。
    """
    row = get_document(store, doc_id)
    if row is None:
        raise KeyError("document not found")
    text = row.get("text") or ""
    if not text.strip():
        raise ValueError("该文档无正文，无法重切分（早期版本未保存全文）")

    kb_id = _doc_kb_id(row)
    plan = resolve_plan(store, settings, kb_id=kb_id, doc_id=doc_id)

    # 保留手工块（重解析会整表重建分块，手工块不应被抹掉）
    manual = fetch_rows(store.chunks.search().where(
        f"doc_id = '{escape_sql(doc_id)}' AND origin = 'manual'"))
    keep = [scalar_row(r) for r in manual]

    engine = row.get("parser_engine") or "native"
    job_id = _ensure_job(store, job_id, job_id_doc=doc_id)
    try:
        result = _run(store, registry, engine, load_text(text, row.get("title") or "doc")[1],
                      row.get("title") or "doc",
                      row.get("source_uri") or "", job_id, parser_cfg,
                      mime=row.get("mime") or "text/plain",
                      split_cfg=SplitConfig(
                          chunk_size=plan["chunk_size"],
                          chunk_overlap=plan["overlap_chars"]),
                      doc_id=doc_id, kb_id=kb_id, plan=plan,
                      queue=queue)
    except JobCancelled:
        # 取消不是失败：任务行已由 _finish_job 写成 cancelled，
        # 文档状态保持原样（重解析时旧分块还在，不该标成 failed）
        _finish_job(store, job_id, "cancelled", "", error="已取消")
        raise
    except Exception as e:  # noqa: BLE001
        _finish_job(store, job_id, "failed", engine, error=str(e))
        _mark_doc_failed(store, doc_id, str(e))
        raise

    # _run 已按新方案重建全部分块 → 把手工块写回
    if keep:
        with store.write_lock():
            upsert_chunks(store, keep)
        from rag.chunk_edit import touch_doc_count
        touch_doc_count(store, doc_id)
    result["kept_manual"] = len(keep)
    return result


def reparse_doc(store: LanceStore, registry: ModelRegistry, doc_id: str,
                parser_cfg: ParserConfig, settings: Settings, backend=None,
                engine: str | None = None,
                job_id: str | None = None, queue=None) -> dict:
    """用留档原文重新解析入库（保留 doc_id，更新分块与元数据）。"""
    row = get_document(store, doc_id)
    if row is None:
        raise KeyError("document not found")
    stored = row.get("stored_file")
    if not stored:
        raise ValueError(
            "该文档无留档原文，无法重解析（早期版本入库未留存，请重新上传）")
    if backend is None:
        raise ValueError("重解析需要存储后端（未装配）")
    path = backend.materialize(stored)
    if engine and engine != "auto" and engine in ALLOWED_ENGINES:
        parser_cfg = parser_cfg.model_copy(update={"default": engine})
    engine_name, documents = route(path, parser_cfg)
    job_id = _ensure_job(store, job_id, job_id_doc=doc_id)
    try:
        return _run(store, registry, engine_name, documents,
                    row.get("title") or path.name,
                    row.get("source_uri") or str(path), job_id,
                    parser_cfg, row.get("mime") or "text/plain",
                    split_cfg=settings.split, doc_id=doc_id,
                    kb_id=_doc_kb_id(row),
                    plan=resolve_plan(store, settings,
                                      kb_id=_doc_kb_id(row),
                                      doc_id=doc_id),
                    queue=queue)
    except JobCancelled:
        # 取消不是失败：任务行已由 _finish_job 写成 cancelled，
        # 文档状态保持原样（重解析时旧分块还在，不该标成 failed）
        _finish_job(store, job_id, "cancelled", "", error="已取消")
        raise
    except Exception as e:  # noqa: BLE001
        _finish_job(store, job_id, "failed", engine_name, error=str(e))
        _mark_doc_failed(store, doc_id, str(e))
        raise


def ingest_text(store: LanceStore, registry: ModelRegistry, text: str, title: str,
                parser_cfg: ParserConfig, settings: Settings,
                kb_id: str = "", job_id: str | None = None,
                queue=None) -> dict:
    job_id = _ensure_job(store, job_id)
    try:
        engine, documents = load_text(text, title)
        _set_stage(store, job_id, "load", engine)
        # 修复：原先硬编码 SplitConfig()，无视全局/知识库方案
        return _run(store, registry, engine, documents, title,
                    "text:" + title, job_id, parser_cfg,
                    split_cfg=settings.split, kb_id=kb_id,
                    plan=resolve_plan(store, settings, kb_id=kb_id),
                    queue=queue)
    except JobCancelled:
        # 取消不是失败：任务行已由 _finish_job 写成 cancelled，
        # 文档状态保持原样（重解析时旧分块还在，不该标成 failed）
        _finish_job(store, job_id, "cancelled", "", error="已取消")
        raise
    except Exception as e:  # noqa: BLE001
        _finish_job(store, job_id, "failed", "", error=str(e))
        raise


def ingest_url(store: LanceStore, registry: ModelRegistry, url: str,
               parser_cfg: ParserConfig, settings: Settings,
               kb_id: str = "", job_id: str | None = None,
               queue=None) -> dict:
    job_id = _ensure_job(store, job_id)
    try:
        # 抓取与正文抽取都可能较慢（网络 IO），放在任务里而不是请求线程
        _set_stage(store, job_id, "fetch")
        engine, documents = load_url(url)
        # 网页标题来自 <title>（抽取失败时 load_url 回退为 URL）
        title = (documents[0].metadata.get("title") if documents else "") or url
        _set_stage(store, job_id, "load", engine)
        # 同上：原硬编码 SplitConfig() 忽略配置
        return _run(store, registry, engine, documents, title, url, job_id,
                    parser_cfg, split_cfg=settings.split, kb_id=kb_id,
                    plan=resolve_plan(store, settings, kb_id=kb_id),
                    queue=queue)
    except JobCancelled:
        # 取消不是失败：任务行已由 _finish_job 写成 cancelled，
        # 文档状态保持原样（重解析时旧分块还在，不该标成 failed）
        _finish_job(store, job_id, "cancelled", "", error="已取消")
        raise
    except Exception as e:  # noqa: BLE001
        _finish_job(store, job_id, "failed", "", error=str(e))
        raise
