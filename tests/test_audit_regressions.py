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


def test_auth_is_threaded_off_event_loop(tmp_path, monkeypatch):
    """authenticate 必须走 to_thread，否则每请求一次同步 LanceDB IO。

    回归背景：中间件里 `authenticate(ctx, request)` 是裸同步调用，
    内部含 count_rows/verify/touch，配了密钥后还每次写一次 apikeys。
    在事件循环里跑同步 IO 会把**整个服务**串行化（每请求 ~7ms 的存储
    往返直接成为吞吐上限），而表现只是「慢」，不会报错。

    为什么改成行为断言：这一条原来是 `inspect.getsource(create_app)`
    里找字符串 `"await asyncio.to_thread(authenticate, ctx, request)"`。
    那种守卫有两个致命问题：① 装配代码搬个位置就红（本轮把中间件体
    拆成 `_http_guards` 时它就红了，而行为一点没变）；② 反过来它也会
    假绿 —— 只要那行字符串还在，哪怕外面被 `# type: ignore` 包成同步调用
    它也照过。**字符串不是行为**。
    现在用「在工作线程里 `asyncio.get_running_loop()` 必然抛 RuntimeError」
    这个事实来判定 authenticate 到底跑在哪个线程上：确定性、无计时、
    且无论中间件体叫什么名字、放在哪个函数里都成立。
    """
    import asyncio

    import rag.api as api_mod

    ran_on_loop: list[bool] = []

    def fake_authenticate(ctx, request):
        try:
            asyncio.get_running_loop()
            ran_on_loop.append(True)      # 拿到了循环 ⇒ 还在事件循环线程上
        except RuntimeError:
            ran_on_loop.append(False)     # 没有循环 ⇒ 已经在线程池里
        return None

    monkeypatch.setattr(api_mod, "authenticate", fake_authenticate)
    c = _app(tmp_path)
    assert c.get("/api/documents").status_code == 200
    assert ran_on_loop, "authenticate 根本没被调用，这条守卫成了空断言"
    assert not any(ran_on_loop), \
        f"鉴权仍跑在事件循环上（{sum(ran_on_loop)}/{len(ran_on_loop)} 次）"


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


def test_concurrent_manual_chunks_get_distinct_ordinals(tmp_path):
    """并发新增分块不得产生重复 ordinal（真并发验证，不只是看代码）。

    这里原来还有一条 `test_next_ordinal_is_computed_under_lock`：
    用 `inspect.getsource` 比 `_next_ordinal` 与 `with store.write_lock():`
    两个子串的字符位置。它守的行为本条已经直接测到了，而且文本版是假绿工厂 ——
    再加一个**锁外**的 `_next_ordinal` 调用点它照样绿（位置关系没变）。
    """
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
    ords = sorted(int(r["ordinal"]) for r in rows)
    assert len(ords) == len(set(ords)), f"ordinal 冲突：{ords}"
    # 只断言「不重复」还不够：锁外读 max 的那类竞态会让序号跳号，
    # 重复会被上一条抓到，**空洞**只有这一条抓得到（分页与上下文窗口都依赖连续序）
    assert ords == list(range(len(ords))), f"ordinal 不连续（有空洞）：{ords}"


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


def test_degraded_reason_hides_exception_text(tmp_path, monkeypatch):
    """降级说明不得回传异常原文（可能含路径 / 表名 / SQL 片段）。

    原来是 `inspect.getsource(search)` 里断言 `"已降级 vector: {e}" not in src`
    —— 一个「查 absence」的守卫：写成 `f"...{str(e)}"`、`f"{type(e)}: {e}"`
    或干脆把异常塞进另一个字段，字串都不在，泄漏照样回来。
    行为断言直接把哨兵值放进异常里，看它会不会出现在响应中。
    """
    from rag.core.config import RetrieveConfig
    from rag.retrieval import search as search_mod
    from rag.storage.tables import LanceStore

    store = LanceStore.for_data_dir(tmp_path, 4)
    doc_id = _seed_doc(store)
    sentinel = "/var/lib/raggi/secret.chunks: SELECT * FROM chunks WHERE 1=1"

    def fake_search_chunks(store_, *, mode, **kw):
        if mode != "vector":
            # 真实场景里这就是 Lance 抛的那类话：带表名、列名、SQL 片段
            raise RuntimeError(f"hybrid 检索失败：{sentinel}")
        return [{"chunk_id": "c1", "doc_id": doc_id, "ordinal": 0,
                 "text": "hello", "_distance": 0.2}]

    monkeypatch.setattr(search_mod, "search_chunks", fake_search_chunks)
    res = search_mod.search(store, _StubEmbedder(), None, "逆变器",
                            RetrieveConfig(mode="hybrid"),
                            include_context=False)
    assert res["results"], "降级后仍应返回结果"
    assert res["degraded_reason"], "降级了却没告诉调用方"
    assert sentinel not in res["degraded_reason"], "降级信息回传了异常原文"
    assert "chunks" not in res["degraded_reason"], (
        f"降级信息带出了内部表名：{res['degraded_reason']}")
    assert "详见服务端日志" in res["degraded_reason"]


def test_score_kind_reports_mixed_instead_of_first_row(tmp_path, monkeypatch):
    """混有不同分来源时必须报 mixed，不能拿首行冒充全体。

    原来是 `assert 'score_kind = scored[0][2]' not in src`：
    写成 `score_kind = scored[0].kind` 或 `next(iter(kinds))` 就绕过字串检查，
    而 bug 一模一样回来。行为断言：造两种分量纲的行，看顶层敢不敢声明单一量纲。
    """
    from rag.core.config import RetrieveConfig
    from rag.retrieval import search as search_mod
    from rag.storage.tables import LanceStore

    store = LanceStore.for_data_dir(tmp_path, 4)
    doc_id = _seed_doc(store)
    rows = [
        {"chunk_id": "c1", "doc_id": doc_id, "ordinal": 0, "text": "hello",
         "_relevance_score": 0.9},          # hybrid/RRF 量纲
        {"chunk_id": "c2", "doc_id": doc_id, "ordinal": 1, "text": "world",
         "_distance": 0.2},                 # 余弦距离量纲
    ]
    monkeypatch.setattr(search_mod, "search_chunks",
                        lambda *a, **kw: list(rows))
    res = search_mod.search(store, _StubEmbedder(), None, "逆变器",
                            RetrieveConfig(mode="hybrid"),
                            include_context=False)
    per_row = [h["score_kind"] for h in res["results"]]
    assert set(per_row) == {"rrf", "cosine_similarity"}, (
        f"单项 score_kind 没如实标注：{per_row}")
    assert res["score_kind"] == "mixed", (
        f"整列量纲不一致却声明了单一量纲：{res['score_kind']}")


# ---- 过滤失败不得静默放行（P1-5）-------------------------------------


def test_attr_filter_failure_yields_no_match_not_everything():
    """mime/parser_engine 解析不出文档时返回 0 条，而不是全库结果。

    回归背景：早前 `if ids is not None:` 让解析异常直接跳过该过滤条件，
    传 mime=application/pdf 却返回全库，调用方会以为已过滤。

    这条原来是 `inspect.getsource(_build_where)` 里找
    `"if ids is not None"` 字符串——又一处文本型守卫。它守的其实是
    「解析失败必须退化成恒假条件」这个**行为**，而行为可以直接测出来：
    传一个读不了的 store（None）与一个空匹配的属性值，两种情况都必须
    得到恒假条件，且不得抛异常、不得返回 None（None = 不加过滤）。
    """
    from rag.storage.repos import chunk_prefilter

    for filters in ({"mime": "application/pdf"},
                    {"parser_engine": "docling"}):
        where = chunk_prefilter(None, filters)
        assert where is not None, "返回 None 等于不加过滤，会返回全库"
        assert "__no_match__" in where, f"{filters} 没产生恒假条件: {where}"


# ---- 批量编辑的 force 语义（P1-1）-----------------------------------


def test_batch_edit_force_is_a_real_three_state_default(tmp_path):
    """批量级 force 是「默认值」而不是「总开关」：单项 force 必须优先。

    原来是 `inspect.signature(batch_edit_chunks).parameters["force"].default is None`
    —— 结构断言，不是行为断言：默认值对而 `_force_of` 写反了（`return bool(force)`
    忽略单项）它照样绿，而那才是历史上真正犯过的 bug。
    四种组合各自测：批量 True/False × 单项给/不给。
    断言的是 `PermissionError` —— 它是本仓**故意**当作 403 信号用的内建异常
    （`api/chunks.py:141` 与 `:157` 各映射一次，`core/errors.py` 里没有对应类），
    不是实现随手抛错了类型。
    """
    from rag import chunk_edit
    from rag.storage.repos import upsert_chunks
    from rag.storage.tables import LanceStore

    store = LanceStore.for_data_dir(tmp_path, 4)
    doc_id = _seed_doc(store)
    vec = [1.0] + [0.0] * (store.dim - 1)
    upsert_chunks(store, [{
        "chunk_id": "cp1", "doc_id": doc_id, "ordinal": 0, "text": "解析出来的正文",
        "text_seg": "解析 出来", "heading_path": "", "page": None,
        "char_start": 0, "char_end": 8, "token_count": 4,
        "origin": "parsed", "edited": False, "original_text": None,
        "offset_valid": True, "embed_model": "stub", "vector": vec,
        "created_at": "", "updated_at": "",
    }])
    edits = [{"chunk_id": "cp1", "text": "改过的正文"}]

    def _run(**kw):
        return chunk_edit.batch_edit_chunks(store, _StubEmbedder(), edits, **kw)

    # 单项没给 force ⇒ 回落到批量级
    _run(force=True)                       # 必须放行（早前 force=False 时必 403）
    with pytest.raises(PermissionError):
        _run(force=False)                  # 批量级关掉 ⇒ 解析块不许改
    # 单项给了 ⇒ 覆盖批量级，两个方向都要覆盖
    with_item_true = [{"chunk_id": "cp1", "text": "x", "force": True}]
    chunk_edit.batch_edit_chunks(store, _StubEmbedder(), with_item_true,
                                 force=False)
    with_item_false = [{"chunk_id": "cp1", "text": "y", "force": False}]
    with pytest.raises(PermissionError):
        chunk_edit.batch_edit_chunks(store, _StubEmbedder(), with_item_false,
                                     force=True)


# ---- 启动期维护失败要留痕（M4）---------------------------------------


def test_startup_maintenance_logs_error_with_stack(tmp_path, caplog):
    """索引维护失败必须 error 级 + 带堆栈留痕（服务可能跑在坏索引上）。

    原来是 `inspect.getsource(_startup_maintenance)` 里查 `"logger.error"`
    与 `"exc_info=True"` 两个字串是否在 —— 只要这两个词出现在函数任何位置就过，
    包括出现在注释里，而真实失败路径可以悄悄降回 warning。
    行为断言：真的让维护抛一次，看日志里到底留下了什么。
    """
    import logging

    from rag.api import Ctx, _startup_maintenance
    from rag.core.config import Settings
    from rag.models.registry import ModelRegistry
    from rag.storage.tables import LanceStore

    store = LanceStore.for_data_dir(tmp_path, 4)
    settings = Settings()
    settings.data_dir = tmp_path
    settings.embed.dim = 4
    ctx = Ctx(settings, store, ModelRegistry(settings))

    def boom():
        raise RuntimeError("向量索引建不起来：dim 不匹配")

    store.ensure_vector_index = boom
    with caplog.at_level(logging.DEBUG):
        _startup_maintenance(ctx)

    errs = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errs, "启动期索引维护失败没有 error 级留痕（运维看不到）"
    assert any(r.exc_info and r.exc_info[1] is not None for r in errs), \
        "error 日志没带堆栈，下一次事故无从诊断"
    assert "不完整索引" in errs[0].getMessage()


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

# ---- 检查失败不许翻译成绿灯（§3 C3 里最严重的一处）---------------------


class _ChunksThatFail:
    """只有「带 filter 的 count_rows」会坏 —— 模拟老版本 lancedb 的形态。"""

    def __init__(self):
        self.calls: list[str] = []

    def count_rows(self, filter=None):   # noqa: A002 - 与 lancedb 签名一致
        self.calls.append("filtered" if filter else "all")
        if filter:
            raise RuntimeError("ArrowInvalid: filter 不被这个版本支持")
        return 3

    def list_versions(self):
        return [1, 2]


class _StoreThatFails:
    def __init__(self):
        self.chunks = _ChunksThatFail()

    def vector_dim(self):
        return 4

    def _index_state(self, col):
        return True, 0


def test_failed_health_checks_are_reported_as_unknown_not_ok(monkeypatch):
    """`except Exception: fts_stale = 0` 是这一整个审计的主线形状。

    检查根本没跑成，却交出「0 条待重建」「模型一致」两个**肯定回答**，
    而 `embed_model_mismatch` 还直接参与 `status` ⇒ 坏消息变绿灯，
    且默认日志级别下一行日志都没有。

    这条断言三件事：未知要能被外部看到（checks_failed）、
    哨兵值不许伪装成 0、以及**无法确认时不许说 ok**。
    """
    from rag.storage import health as health_mod

    # 夹具本身必须**自洽**：stated 3 == actual 3，
    # 否则 degraded 是从 count_mismatch 来的，就证明不了「失败检查」这条路径。
    # （第一版就是这么错的，断言因此变成了假绿。）
    monkeypatch.setattr(health_mod, "docs_count", lambda s: 1)
    monkeypatch.setattr(health_mod, "docs_query",
                        lambda s, cols: [{"doc_id": "d1", "chunk_count": 3}])
    monkeypatch.setattr(health_mod, "chunk_counts_by_doc",
                        lambda s: {"d1": 3})

    def _boom(_store):
        raise RuntimeError("embed_model 列读不出来")

    monkeypatch.setattr(health_mod, "_unique_models", _boom)

    store = _StoreThatFails()
    res = health_mod.health(store, 4, "stub")

    assert res["count_mismatch"] == [], "夹具不自洽，这条测试证明不了任何东西"
    assert res["orphan_chunks"] == 0
    assert "fts_stale" in res["checks_failed"], "fts 检查失败却没对外说明"
    assert "embedding_model" in res["checks_failed"]
    # -1 而不是 0：0 会被界面渲染成「没有待重建」的绿色徽章
    assert res["fts_stale_count"] == -1, (
        f"未知被伪装成健康值：{res['fts_stale_count']}")
    assert res["status"] == "degraded", (
        "一致性检查根本没跑成，却宣称健康 —— 这正是被修掉的谎")


def test_healthy_store_reports_no_failed_checks(monkeypatch):
    """反向对照：全都查成了时 `checks_failed` 必须是空列表、状态是 ok。

    没有这条，上一条可以靠「永远把 checks_failed 填上」混过去。
    """
    from rag.storage import health as health_mod

    monkeypatch.setattr(health_mod, "docs_count", lambda s: 1)
    monkeypatch.setattr(health_mod, "docs_query",
                        lambda s, cols: [{"doc_id": "d1", "chunk_count": 3}])
    monkeypatch.setattr(health_mod, "chunk_counts_by_doc",
                        lambda s: {"d1": 3})
    monkeypatch.setattr(health_mod, "_unique_models", lambda s: {"stub"})

    class _OkChunks(_ChunksThatFail):
        def count_rows(self, filter=None):   # noqa: A002
            return 3

    store = _StoreThatFails()
    store.chunks = _OkChunks()
    res = health_mod.health(store, 4, "stub")
    assert res["checks_failed"] == []
    assert res["status"] == "ok", res
    assert res["fts_stale_count"] == 3


# ---- 鉴权配置读不出来 ≠ 「没配密钥」（fail-open 漏洞）-------------------


def test_has_keys_raises_instead_of_answering_no_keys(tmp_path, monkeypatch):
    """`except Exception: return False` 在鉴权路径上等于「全体放行」。

    `has_keys()` 的两个调用点都把 False 当放行条件，而结果还会被缓存 10 秒
    ⇒ 一次瞬时读失败 = 10 秒的无鉴权 API。修成抛 `ApiKeysUnavailable`，
    并验证异常**不会**被缓存（第二次调用还是要抛，而不是记住上次的假设）。
    """
    from rag.storage.repos import keys as apikeys
    from rag.storage.tables import LanceStore

    store = LanceStore.for_data_dir(tmp_path, 4)
    apikeys._has_keys_cache.clear()

    class _BoomMeta:
        def count(self, table):
            raise RuntimeError("元数据库打不开")

    # 走 SQLite 分支：把 _meta() 换成一个 count 会炸的对象。
    # （不去动 store.apikeys —— 它是只读 property， setattr 会 AttributeError。）
    monkeypatch.setattr(apikeys, "_meta", lambda _s: _BoomMeta())

    for i in (1, 2):
        with pytest.raises(apikeys.ApiKeysUnavailable):
            apikeys.has_keys(store)
    # 第二次仍然真去查（说明失败值没被缓存成「有密钥」或「无密钥」）
    apikeys._has_keys_cache.clear()


def test_unreadable_auth_config_is_503_not_anonymous_pass_through(
        tmp_path, monkeypatch):
    """读不出鉴权配置时，请求必须被拒（503），不能被当成「无鉴权」放行。"""
    from rag.storage.repos import keys as apikeys

    c = _app(tmp_path)
    assert c.get("/api/stats").status_code == 200   # 默认形态确实无需凭据

    def boom(_store):
        raise apikeys.ApiKeysUnavailable("模拟：apikeys 表读不出来")

    # 必须用 monkeypatch：它按函数作用域还原。
    # 写成 `auth_mod.apikeys.has_keys = boom` + `finally: del ...` 会把这个方法
    # **整个删掉**（del 没有「恢复原值」的语义），于是后续每一条走鉴权的测试全红 ——
    # 我第一版就是这么写的，一次带走了 16 条测试。
    monkeypatch.setattr(apikeys, "has_keys", boom)
    r = c.get("/api/stats")
    assert r.status_code == 503, (
        f"鉴权配置读失败却返回 {r.status_code}："
        "把「不知道有没有密钥」当成了「没有密钥」⇒ 放行")
    assert "暂时不可读" in r.json()["detail"]
