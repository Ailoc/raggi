"""审计修复的回归测试。

每个用例对应一个**已复现**的缺陷，不是「代码风格」检查：

- resplit 丢 wait 参数 → 前端轮询空 job_id，请求同步阻塞到超时；
- 上传端点引用未导入的 ALLOWED_ENGINES → 选任意非 auto 引擎必然 500；
- lifespan 无 shutdown → SIGTERM 掐断入库，留下「0 分块但 ready」；
- 鉴权在事件循环内做同步 LanceDB IO；
- _next_ordinal 在写锁外 → 并发新增分块 ordinal 冲突；
- delete_document 不持锁 → 与入库竞态产生永久孤儿 chunk；
- save_config 非原子 + 明文密钥默认权限过宽；
- AnswerReq.kb_id 形同虚设、top_k 无上限；
- 降级路径把异常原文回传客户端。
"""
from __future__ import annotations

import threading
import time

import pytest


def _app(tmp_path, embedder=None, **settings_overrides):
    """构造一个带队列的 app（Ctx 会自行装配 IngestQueue）。"""
    from fastapi.testclient import TestClient

    from rag.api import Ctx, create_app
    from rag.core.config import Settings
    from rag.models.registry import ModelRegistry
    from rag.storage.tables import LanceStore

    store = LanceStore.for_data_dir(tmp_path, 4)
    settings = Settings()
    settings.data_dir = tmp_path
    settings.embed.dim = 4
    for k, v in settings_overrides.items():
        setattr(settings, k, v)
    registry = ModelRegistry(settings)
    if embedder is not None:
        registry.bundle.embedder = embedder
    ctx = Ctx(settings, store, registry)
    c = TestClient(create_app(ctx), raise_server_exceptions=False)
    c.ctx = ctx          # type: ignore[attr-defined]
    c.store = store      # type: ignore[attr-defined]
    return c


class _StubEmbedder:
    dim = 4
    model = "stub"

    def embed_one(self, text):
        return [1.0, 0.0, 0.0, 0.0]

    def embed(self, texts):
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


# ---- resplit 的 wait 契约（P0-2）------------------------------------


def test_resplit_accepts_wait_and_returns_job_id(tmp_path):
    """wait=false 必须立刻返回 job_id，而不是同步阻塞到重切完成。

    回归背景：前端 docops.ts 发 `resplit?wait=false` 并轮询 /api/jobs，
    早前后端没有 wait 形参，参数被静默丢弃 → 请求阻塞、响应里也没有
    job_id，前端只能空轮询到超时。
    """
    c = _app(tmp_path, _StubEmbedder())
    doc = c.post("/api/documents/text",
                 json={"text": "第一段内容。" * 40, "title": "t"}).json()
    doc_id = doc["doc_id"]

    r = c.post(f"/api/documents/{doc_id}/resplit?wait=false")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("job_id"), f"wait=false 未返回 job_id：{body}"
    assert body.get("status") == "queued"

    # 任务行必须真的能被查到，否则轮询同样会空转
    st = c.get(f"/api/jobs/{body['job_id']}")
    assert st.status_code == 200


def test_resplit_default_is_synchronous(tmp_path):
    """不传 wait 时保持同步语义（响应带结果而非 queued）。"""
    c = _app(tmp_path, _StubEmbedder())
    doc_id = c.post("/api/documents/text",
                    json={"text": "同步重切内容。" * 40}).json()["doc_id"]
    body = c.post(f"/api/documents/{doc_id}/resplit").json()
    assert body.get("status") != "queued", "默认应同步等待结果"
    assert body.get("chunk_count", 0) >= 0


# ---- 上传端点的引擎覆盖（P0-1）--------------------------------------


@pytest.mark.parametrize("engine", ["pymupdf4llm", "native", "docling"])
def test_upload_with_explicit_engine_does_not_500(tmp_path, engine):
    """带 engine 表单字段上传不能 500。

    回归背景：api_upload 第 186 行用了 ALLOWED_ENGINES 却从未导入它，
    NameError 被兜底 handler 吞成 500。前端下拉框选任意非 auto 引擎
    就会踩到，而当时**没有任何**上传测试带 engine 字段，所以 414 个
    用例全绿也照样漏掉。
    """
    c = _app(tmp_path, _StubEmbedder())
    r = c.post("/api/documents",
               files={"file": ("a.txt", "纯文本内容".encode(), "text/plain")},
               data={"engine": engine})
    assert r.status_code == 200, f"engine={engine} 上传失败：{r.text}"


def test_upload_rejects_unknown_engine_without_500(tmp_path):
    """未知引擎应被忽略（走默认配置），而不是崩在成员检查上。"""
    c = _app(tmp_path, _StubEmbedder())
    r = c.post("/api/documents",
               files={"file": ("a.txt", b"hello", "text/plain")},
               data={"engine": "totally-bogus"})
    assert r.status_code == 200, r.text


def test_upload_suffix_is_sanitized(tmp_path):
    """用户可控的文件名不得影响临时文件命名。

    后缀决定 router.route() 选哪个解析引擎，必须走白名单而不是
    「最后一个点之后」。
    """
    from rag.api.documents import _safe_suffix

    assert _safe_suffix("a.pdf") == ".pdf"
    assert _safe_suffix("a.PDF") == ".pdf"
    # 双扩展名取最后一个（与早前行为一致），但非法字符一律丢弃
    assert _safe_suffix("x.pdf.exe") == ".exe"
    assert _safe_suffix("noext") == ""
    assert _safe_suffix("../../etc/passwd") == ""
    assert _safe_suffix("a." + "z" * 40) == ""
    assert _safe_suffix("a.p f") == ""
    assert _safe_suffix(None) == ""


def test_upload_leaves_no_part_files_behind(tmp_path):
    """成功上传后 tmp_dir 不应残留 .part 中间文件。"""
    c = _app(tmp_path, _StubEmbedder())
    r = c.post("/api/documents",
               files={"file": ("a.txt", b"hello world", "text/plain")})
    assert r.status_code == 200, r.text
    leftovers = list((tmp_path / "tmp").glob("*.part"))
    assert not leftovers, f"残留半截文件：{leftovers}"


# ---- 优雅关闭（C3）-------------------------------------------------


def test_lifespan_shuts_down_queue(tmp_path):
    """lifespan 退出时必须关掉队列。

    回归背景：早前 lifespan 只有 `yield` 没有 finally，SIGTERM 会直接
    掐断正在跑的入库——pipeline 的「删旧块」与「写新块」是两次独立
    提交，留下「0 分块但 status=ready」的文档和永不终结的任务行。
    """
    c = _app(tmp_path, _StubEmbedder())
    queue = c.ctx.queue
    with c:
        assert queue is not None
    # ThreadPoolExecutor.shutdown 已执行过：再提交会直接抛
    with pytest.raises(RuntimeError):
        queue.submit(lambda **kwargs: None)


def test_lifespan_shutdown_survives_missing_queue(tmp_path):
    """没有队列时也不能在关闭阶段抛异常（否则掩盖真实退出原因）。"""
    c = _app(tmp_path, _StubEmbedder())
    c.ctx.queue = None
    with c:
        pass


# ---- 鉴权不再阻塞事件循环（C1）--------------------------------------


def test_auth_is_threaded_off_event_loop(tmp_path):
    """authenticate 必须走 to_thread，否则每请求一次同步 LanceDB IO。

    回归背景：中间件里 `authenticate(ctx, request)` 是裸同步调用，
    内部含 count_rows/verify/touch，配了密钥后还每次写一次 apikeys。
    """
    import inspect

    from rag.api import create_app

    src = inspect.getsource(create_app)
    assert "await asyncio.to_thread(authenticate, ctx, request)" in src, \
        "鉴权未移出事件循环"
    assert "\n            key_row = authenticate(ctx, request)" not in src, \
        "仍存在同步直调鉴权"


def test_touch_is_throttled(tmp_path):
    """同一密钥一分钟内只写一次 last_used_at。

    回归背景：每请求一次写会给 apikeys 表制造一个新 Lance 版本，
    而 optimize(cleanup_older_than) 只清理 chunks 表，版本目录无界增长。
    """
    from rag.storage.repos import keys as apikeys

    apikeys._touch_cache.clear()
    store = None
    calls: list[str] = []

    class _FakeStore:
        class apikeys:  # noqa: N801
            @staticmethod
            def update(where, values):
                calls.append(values["last_used_at"])

    store = _FakeStore()
    apikeys.touch(store, "k1")
    apikeys.touch(store, "k1")
    apikeys.touch(store, "k1")
    assert len(calls) == 1, f"touch 未节流，写了 {len(calls)} 次"
    # 不同 key 各自独立计时
    apikeys.touch(store, "k2")
    assert len(calls) == 2
    apikeys._touch_cache.clear()


# ---- 锁范围（M1 / M2）----------------------------------------------


def test_next_ordinal_is_computed_under_lock(tmp_path):
    """_next_ordinal 的调用点必须在 write_lock 内。

    回归背景：早前在锁外先取 ordinal 再进锁，两个并发的「新增分块」
    会读到相同 max(ordinal) → ordinal 冲突，而分页排序依赖它唯一。
    """
    import inspect

    from rag import chunk_edit

    src = inspect.getsource(chunk_edit.add_manual_chunk)
    lock_at = src.index("with store.write_lock():")
    ord_at = src.index("_next_ordinal(store, doc_id)")
    assert ord_at > lock_at, "_next_ordinal 又跑回锁外了"


def test_concurrent_manual_chunks_get_distinct_ordinals(tmp_path):
    """并发新增分块不得产生重复 ordinal（真并发验证，不只是看代码）。"""
    from rag import chunk_edit
    from rag.storage.tables import LanceStore

    store = LanceStore.for_data_dir(tmp_path, 4)
    doc_id = _seed_doc(store)

    errors: list[Exception] = []

    def _add(i: int):
        try:
            chunk_edit.add_manual_chunk(
                store, _StubEmbedder(), f"分块内容 {i}", doc_id=doc_id)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=_add, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"并发新增分块报错：{errors}"

    from rag.storage.repos import escape_sql, fetch_rows
    rows = fetch_rows(store.chunks.search().select(["ordinal"]).where(
        f"doc_id = '{escape_sql(doc_id)}'"))
    ords = [int(r["ordinal"]) for r in rows]
    assert len(ords) == len(set(ords)), f"ordinal 冲突：{sorted(ords)}"


def _seed_doc(store) -> str:
    from rag.storage.repos import upsert_documents

    upsert_documents(store, [{
        "doc_id": "d1", "title": "t", "source_uri": None,
        "mime": "text/plain", "parser_engine": "native",
        "content_hash": "h1", "char_count": 10, "chunk_count": 0,
        "text": "hello", "status": "ready", "error": None,
        "meta": "{}", "kb_id": "", "created_at": "", "updated_at": "",
    }])
    return "d1"


# ---- 配置原子写与权限（L3 / L4）-------------------------------------


def test_save_config_is_atomic_and_private(tmp_path):
    """config.toml 必须 0600 且不残留临时文件。

    回归背景：早前直接 write_text —— 写到一半崩溃会留下截断的
    config.toml，下次启动 Settings 校验直接失败；且文件含明文 token 与
    S3 secret_key，却沿用默认 0644。
    """
    import os
    import stat

    from rag.core.config import Settings, save_config

    cfg = tmp_path / "config.toml"
    s = Settings()
    s.token = "secret-token"
    s.data_dir = tmp_path
    save_config(s, cfg)

    mode = stat.S_IMODE(os.stat(cfg).st_mode)
    assert mode == 0o600, f"config.toml 权限过宽：{oct(mode)}"
    assert not list(tmp_path.glob(".*.tmp")), "残留临时文件"
    # 内容仍需完整可用
    assert "secret-token" in cfg.read_text(encoding="utf-8")


def test_save_config_overwrites_existing_loose_permissions(tmp_path):
    """历史遗留的 0644 文件也应收紧（open 的 mode 只在创建时生效）。"""
    import os
    import stat

    from rag.core.config import Settings, save_config

    cfg = tmp_path / "config.toml"
    cfg.write_text("port = 1\n", encoding="utf-8")
    os.chmod(cfg, 0o644)

    save_config(Settings(), cfg)
    assert stat.S_IMODE(os.stat(cfg).st_mode) == 0o600


# ---- 问答契约（P1-3 / C2）------------------------------------------


def test_answer_kb_id_reaches_filters():
    """顶层 kb_id 必须并进检索条件，否则「按库问答」形同虚设。"""
    from rag.api.answer import _filters
    from rag.api.schemas import AnswerReq

    assert _filters(AnswerReq(q="q", kb_id="kb1"))["kb_id"] == "kb1"
    assert _filters(AnswerReq(q="q", filters={"origin": "manual"})) == \
        {"origin": "manual"}
    # 显式 filters 不得被顶层字段覆盖掉
    merged = _filters(AnswerReq(q="q", kb_id="kb1",
                                filters={"kb_id": "kb2"}))
    assert merged["kb_id"] == "kb1"


def test_answer_top_k_is_bounded():
    """AnswerReq.top_k 必须有上限（否则可拉爆内存）。"""
    import pytest as _pytest
    from pydantic import ValidationError

    from rag.api.schemas import AnswerReq

    assert AnswerReq(q="q", top_k=50).top_k == 50
    with _pytest.raises(ValidationError):
        AnswerReq(q="q", top_k=100000)
    with _pytest.raises(ValidationError):
        AnswerReq(q="q", top_k=0)


# ---- 降级信息脱敏（M5）----------------------------------------------


def test_degraded_reason_hides_exception_text():
    """降级说明不得回传异常原文（可能含路径 / 表名 / SQL）。"""
    import inspect

    from rag.retrieval.search import search

    src = inspect.getsource(search)
    assert "已降级 vector: {e}" not in src, "降级路径仍在回传异常原文"


def test_score_kind_reports_mixed_instead_of_first_row():
    """混有不同分来源时必须报 mixed，不能拿首行冒充全体。"""
    import inspect

    from rag.retrieval.search import search

    src = inspect.getsource(search)
    assert 'score_kind = scored[0][2]' not in src, \
        "score_kind 又退回取首行"


# ---- 过滤失败不得静默放行（P1-5）-------------------------------------


def test_attr_filter_failure_yields_no_match_not_everything(tmp_path):
    """mime/parser_engine 查询失败时返回 0 条，而不是全库结果。

    回归背景：早前 `if ids is not None:` 让解析异常直接跳过该过滤条件，
    传 mime=application/pdf 却返回全库，调用方会以为已过滤。
    """
    import inspect

    from rag.retrieval.search import _build_where

    # 只看代码本身（不含 docstring/注释），否则会被解释性注释误伤
    src = inspect.getsource(_build_where)
    code = "\n".join(
        ln for ln in src.splitlines()
        if not ln.strip().startswith("#"))
    assert "if ids is not None" not in code, \
        "属性过滤失败仍会静默放行"

    where = _build_where({"mime": "application/pdf", "_store": None})
    assert "__no_match__" in where, \
        "store 缺失时应产生恒假条件而非忽略过滤"


# ---- 批量编辑的 force 语义（P1-1）-----------------------------------


def test_batch_edit_force_default_applies():
    """批量级 force 必须真的生效（早前形参形同虚设 → 解析块必 403）。"""
    from rag.chunk_edit import batch_edit_chunks

    import inspect
    sig = inspect.signature(batch_edit_chunks)
    assert sig.parameters["force"].default is None, \
        "force 默认应是 None（三态），否则单项缺失时回落不到批量级默认值"


# ---- 启动期维护失败要留痕（M4）---------------------------------------


def test_startup_maintenance_logs_error_not_silent():
    """索引维护失败必须 error 级留痕（服务可能跑在坏索引上）。"""
    import inspect

    from rag.api import _startup_maintenance

    src = inspect.getsource(_startup_maintenance)
    assert "logger.error" in src, "启动维护失败仍只是 warning"
    assert "exc_info=True" in src, "未记录堆栈"


# ---- 队列竞态（H2）---------------------------------------------------


def test_submit_does_not_register_already_done_future(tmp_path):
    """已完成的 Future 不得再被登记进 _futures。

    回归背景：submit 在 _pool.submit 之后才登记，worker 可能已经跑完并
    在 finally 里 pop 过；再登记会让 cancel() 对一个**终态**任务写
    stage=cancelling，且永不修复（finally 已执行完）。
    """
    from rag.ingest.queue import IngestQueue
    from rag.storage.tables import LanceStore

    store = LanceStore.for_data_dir(tmp_path, 4)
    q = IngestQueue(store, workers=4, max_pending=16)

    def _noop(**kwargs):
        return None

    try:
        for _ in range(20):
            _, fut = q.submit(_noop)
            fut.result()          # 确保任务已终止
        time.sleep(0.05)
        stale = [j for j, f in q._futures.items() if f.done()]
        assert not stale, f"已终止的 Future 仍留在 _futures：{stale}"
    finally:
        q.shutdown(wait=True)


def test_cancel_after_completion_reports_finished(tmp_path):
    """任务已完成后取消，不得把它改写成非终态（cancelling）。

    回归背景：submit 的登记窗口竞态会让已终止的 Future 留在 _futures，
    cancel() 拿到它后 fut.cancel() 返回 False，于是对一个**已完成**的
    任务写 stage=cancelling —— 而 _wrapped 的 finally 已执行完，
    这个非终态永不修复，/api/jobs 会一直停在 cancelling。

    注意 cancel() 的 finished 判定依据是 jobs 表的 stage，裸任务体
    （lambda）不会自己写终态，因此这里断言「不被写成 cancelling」。
    """
    from rag.ingest.queue import IngestQueue, get_job
    from rag.storage.tables import LanceStore

    store = LanceStore.for_data_dir(tmp_path, 4)
    q = IngestQueue(store, workers=2, max_pending=8)

    def _noop(**kwargs):
        return None

    try:
        job_id, fut = q.submit(_noop)
        fut.result()
        time.sleep(0.05)
        q.cancel(job_id)
        row = get_job(store, job_id) or {}
        assert row.get("stage") != "cancelling", \
            f"已终止任务被写成 cancelling：{row}"
    finally:
        q.shutdown(wait=True)


# ---- 端到端冒烟 ------------------------------------------------------


def test_upload_text_search_resplit_cycle(tmp_path):
    """入库 → 检索 → 重切 全链路能跑通。"""
    c = _app(tmp_path, _StubEmbedder())
    doc_id = c.post("/api/documents/text",
                    json={"text": "端到端测试内容。", "title": "e2e"}
                    ).json()["doc_id"]
    assert c.get("/api/documents").status_code == 200
    r = c.post("/api/search", json={"q": "端到端"})
    assert r.status_code == 200, r.text
    assert c.post(f"/api/documents/{doc_id}/resplit").status_code == 200
    assert c.get("/api/health").status_code == 200