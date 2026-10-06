"""FastAPI 应用装配：认证 / CORS / ORJSON / 路由注册 / 后台启动维护。

性能：默认 ORJSONResponse；启动期索引维护放后台线程（冷启动 <3s）。
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, ORJSONResponse
from fastapi.security import APIKeyHeader, HTTPBearer
from fastapi.staticfiles import StaticFiles

from rag.api.answer import router as answer_router
from rag.api.apikeys import router as apikeys_router
from rag.api.auth import AuthError, authenticate
from rag.api.chunks import router as chunks_router
from rag.api.documents import IDEM as _IDEM
from rag.api.documents import router as documents_router
from rag.api.jobs import router as jobs_router
from rag.api.kbs import router as kbs_router
from rag.api.models import router as models_router
from rag.api.plan import router as plan_router
from rag.api.search import router as search_router
from rag.api.system import router as system_router
from rag.core.config import Settings
from rag.core import ratelimit, requestid
from rag.core.ratelimit import RateLimiter
from rag.core.errors import RaggiError, short
from rag.models.registry import ModelRegistry
from rag.models.embeddings import EmbedUnavailable
from rag.storage.tables import LanceStore

logger = logging.getLogger("raggi.api")

# OpenAPI 里安全方案的**声明**（真正的校验在 auth 中间件里）。
# 两种凭据等价（OR 关系）：Authorization: Bearer <密钥> 或 X-API-Key。
# 旧版静态 token 也走 Bearer 头，因此没有单独声明。
_bearer = HTTPBearer(
    auto_error=False, scheme_name="ApiKeyBearer",
    description="在「设置 → 密钥」签发的密钥（rg_…）或旧版静态 token，"
                "作为 Bearer 凭据放在 Authorization 头里。")
_api_key_header = APIKeyHeader(
    name="X-API-Key", auto_error=False, scheme_name="ApiKeyHeader",
    description="与 Authorization: Bearer 等价的备用头，"
                "适合只支持自定义请求头的客户端。")
SECURITY_DEPS = [Security(_bearer), Security(_api_key_header)]

TAGS_METADATA = [
    {"name": "documents", "description": "文档入库（上传/文本/批量/URL）、列表、详情、重命名、重解析、原文下载"},
    {"name": "chunks", "description": "分块查询与编辑：新增、改写（重向量化）、批量改写、停用/启用、删除"},
    {"name": "knowledge-bases", "description": "知识库的增删改查（删除级联清理文档、分块与留档原文）"},
    {"name": "plans", "description": "两级分块方案：知识库默认值、文档级覆盖、按方案重切分"},
    {"name": "search", "description": "混合检索（向量 + 全文 + 精排）与 Markdown 报告导出"},
    {"name": "answer", "description": "生成式问答（features.answer 开启后可用），答案带引用"},
    {"name": "models", "description": "Embedding / LLM / Rerank 配置热切换、连通性测试与向量化接口"},
    {"name": "keys", "description": "访问密钥的签发、列表与吊销（明文只在签发时出现一次）"},
    {"name": "jobs", "description": "入库队列任务的状态查询（wait=false 提交后轮询）"},
    {"name": "system", "description": "健康对账、容量统计、版本回滚、索引重建与计数修复"},
]

# 会改变数据状态的方法。判定「该不该作废健康快照」用（与 ratelimit.is_limited_method
# 用的是同一组语义，但不 import 它——那个函数还包含限流豁免的判断，语义并不等价）。
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

WEB_DIR = Path(__file__).resolve().parent.parent.parent / "web"
# 源码在 web/src/（Svelte 5 + Vite），产物在 web/dist/。
# 分成两棵树：构建只写 dist，清空产物时不可能误删源码。
WEB_SRC_DIR = WEB_DIR / "src"
WEB_DIST_DIR = WEB_DIR / "dist"
WEB_ENTRY = "dist/index.html"


class Ctx:  # 应用上下文（在 server 中装配）
    def __init__(self, settings: Settings, store: LanceStore,
                 registry: ModelRegistry, backend=None, meta=None):
        self.settings = settings
        self.store = store
        self.registry = registry
        # OLTP 元数据引擎（SQLite/WAL）。挂在 store 上是为了让仓储层能用
        # `getattr(store, "meta", None)` 分派，而不必改遍所有签名；
        # None = 走 `meta_engine=lancedb` 的回退路径。
        self.meta = meta
        if meta is not None:
            store.meta = meta
        # 存储后端：原文归档与容量统计都经它，业务层不关心 local / s3
        self.backend = backend if backend is not None else store.backend
        # 原文预览的签名 URL 签发器（iframe/img 无法带 Authorization 头）
        from rag.core.signing import UrlSigner

        self.signer = UrlSigner(settings.data_dir)
        # 入库队列：有界并发 + 状态可查（见 rag/queue.py）
        from rag.ingest.queue import IngestQueue

        self.queue = IngestQueue(store, workers=settings.ingest_workers,
                                 retention_days=settings.job_retention_days)


def _check_web_build() -> None:
    """启动期校验前端产物（web/dist 由 web/src 经 Vite 构建而来）。

    web/dist 不入库（见 .gitignore），因此「源码改了但没重新编译」是
    常见状态。这里在启动时把问题暴露成日志，而不是让人对着空白页
    猜是「忘记 npm run build」还是「接口挂了」。
    """
    entry = WEB_DIR / WEB_ENTRY
    if entry.exists():
        # 递归扫描：视图在 views/、组件在 ui/、样式在 styles/，
        # 只看顶层 *.ts 会漏掉绝大多数改动
        sources = [p for p in WEB_SRC_DIR.rglob("*")
                   if p.suffix in (".ts", ".svelte", ".css") and p.is_file()]
        stale = [p.name for p in sources
                 if p.stat().st_mtime > entry.stat().st_mtime]
        if stale:
            logger.warning(
                "前端产物已过期（%s 比 web/dist 旧于源码：%s），"
                "请执行 npm run build 以应用最新改动",
                WEB_ENTRY, ", ".join(sorted(stale)[:5]))
        return
    if WEB_SRC_DIR.exists():
        logger.error(
            "缺少前端产物 %s —— 源码在 web/src/，"
            "需先构建：npm install --include=dev && npm run build",
            WEB_ENTRY)
    else:
        logger.warning("未找到前端目录 %s，API 可用但无页面", WEB_DIR)


def _tune_thread_pools() -> int:
    """把默认线程池与 anyio 令牌桶调到与核数匹配的量级。

    为什么要显式设：`asyncio.to_thread` 用的是**默认 executor**，
    它的上界是 `min(32, cpu+4)` —— 本机 4 核就是 **8 条线程**，
    而每个 API 请求至少要占用它两次（鉴权一次 + handler 一次）。
    静态文件（Starlette 走 anyio）另有 40 个令牌的独立上限。

    诚实说明：实测把 executor 从 8 提到 128 **不改变**入库吞吐坍塌曲线
    （见诊断报告 §2.5），所以这条不是本次的性能来源，是「不知道什么时候
    会踩到的暗坎」——它便宜，且能让读路径在并发下不必排队等线程。
    """
    import os
    from concurrent.futures import ThreadPoolExecutor

    workers = max(16, (os.cpu_count() or 4) * 16)
    loop = None
    try:
        import asyncio

        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        loop.set_default_executor(
            ThreadPoolExecutor(max_workers=workers,
                               thread_name_prefix="raggi-blk"))
    try:
        import anyio.to_thread

        anyio.to_thread.current_default_thread_limiter().total_tokens = max(
            64, workers)
    except Exception:  # noqa: BLE001
        # 不在 anyio 上下文里（比如测试直接构造 app）——跳过，不影响功能
        pass
    return workers


def create_app(ctx: Ctx) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 线程池调参必须在**有运行中事件循环**的地方做（lifespan 里正好有），
        # 放在 create_app 里拿不到 loop，set_default_executor 会静默跳过。
        app.state.blocking_threads = _tune_thread_pools()
        # 启动期索引维护放后台线程，避免阻塞冷启动（目标 <3s）
        maint = threading.Thread(
            target=_startup_maintenance, args=(ctx,),
            daemon=True)
        maint.start()
        try:
            yield
        finally:
            # 优雅关闭：**必须有**。缺了这一段，SIGTERM 会直接掐断正在
            # 跑的入库任务 —— pipeline 的「删旧块」与「写新块」是两次
            # 独立提交（无事务），中途被杀会留下「0 分块但 status=ready」
            # 的文档；任务行也会停在 stage=embed 永不终结。
            _shutdown(ctx, maint)

    app = FastAPI(
        title="Raggi", version="0.2.0",
        description="极简单机 RAG：入库 / 检索 / 分块管理 / 系统维护。"
                    "错误响应统一为 {\"detail\": \"...\"}。",
        default_response_class=ORJSONResponse,
        lifespan=lifespan,
        openapi_tags=TAGS_METADATA,
    )
    app.state.ctx = ctx
    # 幂等键存储挂到 app.state：进程内共享是有意的（见 documents.IDEM
    # 的注释），挂出来是为了让测试与运维能触达它、重置它。
    app.state.idem = _IDEM
    app.state.limiter = RateLimiter(
        limit=ctx.settings.rate_limit_writes_per_min,
        burst=ctx.settings.rate_limit_burst)
    _check_web_build()

    @app.middleware("http")
    async def auth_middleware(request, call_next):
        # 请求 ID 贯穿全程：响应头、错误体、日志行都用同一个值，
        # 排障时不必在日志里靠时间戳猜哪一行对应客户端那条报错。
        rid = requestid.resolve(request)
        request.state.request_id = rid
        started = time.perf_counter()
        # 鉴权：API 密钥（可吊销/过期/分作用域）优先，兼容旧版静态 token。
        # 未配置 token 且无任何密钥时不拦截（单机自用零摩擦）。
        # 静态前端与 CORS 预检放行。
        key_row = None
        try:
            # 鉴权要查 LanceDB（has_keys/verify/touch），是同步 IO。
            # 放事件循环里会阻塞整个服务：每请求一次 count_rows，
            # 配了密钥后还额外一次写（touch）。必须挪到线程池。
            key_row = await asyncio.to_thread(authenticate, ctx, request)
        except AuthError as e:
            # 必须在中间件里显式构造响应：此时 ExceptionMiddleware 尚未
            # 生效，直接抛 HTTPException 会退化成裸 500，调用方看不出原因。
            resp = e.to_response()
            resp.headers[requestid.HEADER] = rid
            resp.headers["X-Response-Time-Ms"] = _elapsed_ms(started)
            return resp

        # 限流放在鉴权之后：未通过鉴权的请求不该消耗别人的配额，
        # 也不该向调用方泄露「这个 key 还有多少额度」。
        path = request.url.path
        if (app.state.limiter.enabled
                and ratelimit.is_limited_method(request)
                and path.startswith("/api")
                and not ratelimit.is_idempotent(path)):
            ok, retry = app.state.limiter.allow(
                ratelimit.caller_key(request, key_row))
            if not ok:
                resp = JSONResponse(
                    {"detail": f"写入过于频繁，请 {retry} 秒后重试",
                     "retry_after": retry, "request_id": rid},
                    status_code=429,
                    headers={requestid.HEADER: rid,
                             "Retry-After": str(retry)})
                return resp

        response = await call_next(request)
        # 快照类端点（/health、/stats）带 3s 缓存，如实告诉调用方
        # 这份数据是多少毫秒前的——否则刚入库完刷新界面看到的是旧数，
        # 而它读起来像实时状态。
        age = getattr(request.state, "snapshot_age_ms", None)
        if age is not None:
            response.headers["X-Snapshot-Age-Ms"] = str(age)
        # 写成功 → 作废健康/容量快照（见 api/system.invalidate_snapshots）。
        # 放在中间件里而不是每个端点里：漏一个端点就会让用户看到过期账本，
        # 而这种「改完了但界面还是旧的」正是最难自查的一类问题。
        if (request.method in _WRITE_METHODS
                and path.startswith("/api") and response.status_code < 300):
            from rag.api.system import invalidate_snapshots

            invalidate_snapshots()
        if not request.url.path.startswith("/api"):
            # Vite 的产物文件名自带内容哈希（assets/xxx-<hash>.js），
            # 所以 /assets/ 下的资源可以永久强缓存：改了前端会生成新文件名，
            # index.html 里的引用也随之变化 —— 不存在「改了但页面没变」。
            #
            # 改前对**所有**非 /api 路径一律 no-cache，于是 SPA 每次进入
            # 都要重新验证整套 JS/CSS：在并发下这些请求和 API 抢同一批
            # 线程池额度（StaticFiles 的读文件也走 to_thread）。
            if request.url.path.startswith("/assets/"):
                response.headers["Cache-Control"] = (
                    "public, max-age=31536000, immutable")
            else:
                # index.html 与 favicon 等仍不缓存：它们是「指向哪份产物」的入口
                response.headers["Cache-Control"] = "no-cache"
        response.headers[requestid.HEADER] = rid
        response.headers["X-Response-Time-Ms"] = _elapsed_ms(started)
        return response

    # CORS：add_middleware 后加者在外层，故 CORSMiddleware 先于
    # auth 处理请求（preflight 无需认证即可应答）
    if ctx.settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=ctx.settings.cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # 注册顺序即匹配顺序：plan_router 含 /documents/plans 这类字面量路径，
    # 必须先于 documents_router 的 /documents/{doc_id}，否则会被后者
    # 当成 doc_id="plans" 而返回 404。
    #
    # 版本策略：/api/v1 是**规范版本**（出现在 OpenAPI 里）；
    # /api 是等价的兼容别名（include_in_schema=False），现有前端与
    # 用户脚本不用改。破坏性变更发 /api/v2，v1 保持可用。
    #
    # SECURITY_DEPS 只是**声明**：让 OpenAPI 里出现安全方案、/docs
    # 有 Authorize 按钮、生成的客户端知道怎么带凭据。真正的校验仍在
    # auth 中间件里（支持密钥/旧 token，且能放行静态资源与预检）。
    for r in (plan_router, documents_router, kbs_router, search_router,
              chunks_router, models_router, system_router, answer_router,
              apikeys_router, jobs_router):
        app.include_router(r, prefix="/api/v1", dependencies=SECURITY_DEPS)
        app.include_router(r, prefix="/api", include_in_schema=False,
                           dependencies=SECURITY_DEPS)

    if WEB_DIST_DIR.exists():
        app.mount(
            "/", StaticFiles(directory=str(WEB_DIST_DIR), html=True),
            name="web")

    @app.exception_handler(EmbedUnavailable)
    async def embed_unavailable_handler(request, exc: EmbedUnavailable):
        # 模型服务挂了不该表现为裸 500：503 + 指明原因，
        # 前端提示条与模态错误都能给出可行动的信息。
        return JSONResponse({"detail": str(exc)}, status_code=503)

    @app.exception_handler(RaggiError)
    async def raggi_error_handler(request, exc: RaggiError):
        # 领域异常的兜底映射：NotFound→404 / Invalid→400 / Conflict→409 /
        # Unavailable→503。没有这一层时，漏接的领域异常会退化成裸 500，
        # 与「契约里声明了 404/503」互相矛盾。
        detail = short(exc)
        headers = getattr(exc, "headers", None) or None
        return JSONResponse({"detail": detail},
                            status_code=getattr(exc, "status_code", 500),
                            headers=headers)

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request, exc: Exception):
        """兜底：把未捕获异常变成带请求 ID 的 JSON 500。

        不做这一层的话，Starlette 直接回纯文本 "Internal Server Error"，
        客户端既拿不到原因、也拿不到能对日志的线索——排障只能靠猜。
        这里**不**回传异常原文：堆栈与路径是信息泄露（见 audit §B）。
        """
        rid = getattr(request.state, "request_id", requestid.new_request_id())
        logger.exception("未处理异常 rid=%s %s %s", rid, request.method,
                         request.url.path)
        return JSONResponse(
            {"detail": f"服务端内部错误（请求 ID: {rid}），请查看服务端日志",
             "request_id": rid},
            status_code=500,
            headers={requestid.HEADER: rid})

    return app


def _elapsed_ms(started: float) -> str:
    return f"{(time.perf_counter() - started) * 1000:.1f}"


def _startup_maintenance(ctx: Ctx) -> None:
    from rag.server import mark_maintenance_done, should_run_startup_maintenance

    # 「查标记 → 干活 → 写标记」必须在**跨进程写锁里**做。
    # 不持锁时的竞态是真的：4 个 worker 同时启动、同时看到「没做过」，
    # 于是同一份建索引/重建 FTS 干 4 遍，后 3 个撞 LanceDB 的乐观提交冲突
    # （`Retryable commit conflict ... preempted by concurrent CreateIndex`）。
    # 锁是可重入的，下面每个 ensure_* 自己再取一次锁不会死锁。
    with ctx.store.write_lock():
        if not should_run_startup_maintenance(ctx.settings.data_dir):
            # 多进程形态下别的 worker 刚做过同样的事
            logger.debug("最近已做过启动期索引维护，本进程跳过")
            return
        try:
            ctx.store.ensure_scalar_indexes()
            ctx.store.ensure_vector_index()
            ctx.store.ensure_fts_index()
        except Exception as e:  # noqa: BLE001
            # 上报而不只是 warning：这个异常意味着服务跑在坏索引上
            # （列迁移/向量维度/FTS 都不完整），检索结果不可信。
            # 不在这里 fail-fast 是因为启动维护刻意放后台线程（冷启动 <3s），
            # 但必须让运维在 /api/health 与日志里看得见。
            logger.error("启动期索引维护失败，服务可能运行在不完整索引上: %s",
                         e, exc_info=True)
            return
        mark_maintenance_done(ctx.settings.data_dir)


def _shutdown(ctx: Ctx, maint: threading.Thread) -> None:
    """优雅关闭：先停止接收新任务，再等在跑的入库收尾。

    等不到就如实记录：宁可留下明确的中断日志，也不要让调用方以为
    已经干净退出了 —— 被强杀留下的半截状态恰恰最难排查。
    """
    queue = getattr(ctx, "queue", None)
    if queue is not None:
        try:
            queue.shutdown(wait=True)
        except Exception as e:  # noqa: BLE001
            logger.error("关闭入库队列时出错: %s", e)
        else:
            logger.info("入库队列已关闭")
    maint.join(timeout=5.0)
    if maint.is_alive():
        logger.warning("启动维护线程未在 5s 内结束（索引可能未建完）")
