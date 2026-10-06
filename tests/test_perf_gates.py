"""并发改造的结构性门槛。

断言的是**「每请求发几次数据库查询」**，不是绝对延迟 —— 后者受机器负载影响
（这台开发机常驻 load≈2.3），放进默认套件只会得到假红。
延迟门槛在 `@pytest.mark.perf` 下，手动跑。

为什么钉「查询次数」就够：本轮诊断里最大的两个可修开销
（检索的 N+1 上下文回捞、观测端点的全表扫描）都不是「慢查询」，
而是「查询次数 × 每查询固定开销」。固定开销要靠换调用路径才能降，
而**次数写在代码里** —— 它一旦回退，性能就回退，这条能稳定断言。
"""
from __future__ import annotations

import time
from unittest import mock

import numpy as np
import pytest

from rag.core.config import RetrieveConfig
from rag.retrieval.search import search
from rag.storage.health import health
from rag.storage.repos import contexts_for, get_document, list_documents
from rag.storage.tables import LanceStore

DIM = 16
DOCS = 60
CHUNKS_PER = 8


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    """小而真实的数据集：够走真实查询路径，又不至于拖慢 CI。"""
    from rag.parsing.segment import segment

    d = tmp_path_factory.mktemp("perf-gates")
    s = LanceStore.for_data_dir(d, DIM)
    rng = np.random.default_rng(11)
    docs, chunks = [], []
    now = "2026-10-05T00:00:00+00:00"
    for i in range(DOCS):
        did = f"doc-{i:04d}"
        docs.append({"doc_id": did, "title": f"文档 {i}", "source_uri": None,
                     "mime": "application/pdf", "parser_engine": "native",
                     "content_hash": f"hash-{i}", "char_count": 1000,
                     "chunk_count": CHUNKS_PER, "text": "正文" * 100,
                     "status": "ready", "error": None,
                     "kb_id": f"kb-{i % 3}", "stored_file": None,
                     "meta": "{}",
                     "created_at": f"2026-10-0{1 + i % 5}T00:00:00+00:00",
                     "updated_at": now})
        for j in range(CHUNKS_PER):
            v = rng.standard_normal(DIM, dtype=np.float32)
            v /= np.linalg.norm(v)
            txt = f"第 {i} 篇第 {j} 段 测试样例 alpha{i}"
            chunks.append({"chunk_id": f"c-{i}-{j}", "doc_id": did,
                           "ordinal": j, "kb_id": f"kb-{i % 3}",
                           "text": txt,
                           "text_seg": " ".join(segment(txt).split()),
                           "heading_path": "", "page": j,
                           "char_start": j * 50, "char_end": j * 50 + 40,
                           "token_count": 20, "origin": "parsed",
                           "edited": False, "enabled": True,
                           "original_text": None, "offset_valid": True,
                           "fts_stale": False, "embed_model": "stub",
                           "vector": v.tolist(),
                           "created_at": now, "updated_at": now})
    s.documents.add(docs)
    s.chunks.add(chunks)
    s.ensure_scalar_indexes()
    s.ensure_fts_index()
    return s


class QueryCounter:
    """数「执行了多少次表查询」，并区分走的是哪条路径。

    三个入口都要数，漏一个就会得到假的「1 次」：
      - `LanceTable._execute_query` ← 所有 lancedb 查询构建器的执行汇聚点
      - `LanceDataset.to_table`     ← `storage/sql.scalar_rows()`（原生投影）
      - `LanceTable.count_rows`     ← total 与对账

    刻意**不**patch `LanceQueryBuilder.to_arrow`：`LanceEmptyQueryBuilder`
    等子类自带 `to_arrow` 覆盖，patch 基类拦不住 —— 试过，会得到假的 n=0，
    门槛形同虚设。构造 builder 不计数：它不碰磁盘，不构成成本。

    `rows` 只统计原生路径 materialize 的行数。`_execute_query` 返回的是
    异步可迭代对象，在这里 drain 它会改变消费语义（检索路径会拿到已耗尽的
    迭代器），所以那条路径只数次数、不数行 —— 「不许取全表」由
    `builder == 0` 与 `rows <= limit` 两条一起守。
    """

    def __enter__(self):
        import lancedb.table as lt
        from lance.dataset import LanceDataset

        LanceTable = lt.LanceTable
        self.n = 0
        self.builder = 0
        self.native = 0
        self.rows = []
        outer = self

        def counting(orig, kind):
            def wrapper(self, *a, **kw):
                r = orig(self, *a, **kw)
                outer.n += 1
                if kind == "exec":
                    outer.builder += 1
                elif kind == "to_table":
                    outer.native += 1
                    try:
                        outer.rows.append(int(r.num_rows))
                    except Exception:  # noqa: BLE001
                        pass
                return r
            return wrapper

        self._patches = [
            mock.patch.object(LanceDataset, "to_table",
                              counting(LanceDataset.to_table, "to_table")),
            mock.patch.object(LanceTable, "_execute_query",
                              counting(LanceTable._execute_query, "exec")),
            mock.patch.object(LanceTable, "count_rows",
                              counting(LanceTable.count_rows, "count_rows")),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()
        return False


def test_list_documents_is_count_plus_page(store):
    """列表 = 1 次 count + 1 次分页投影。

    回退成「取全表再 Python 切片」会放大这个数：那条路径在 2000 行时
    实测 8.63ms，分页下推后 2.50ms（SQLite 路径下 0.100ms）。
    """
    with QueryCounter() as qc:
        list_documents(store, limit=20)
    assert qc.n <= 2, f"文档列表发了 {qc.n} 次查询（上界 2）"
    assert qc.builder == 0, (
        f"列表有 {qc.builder} 次走 lancedb 查询构建器：标量读必须走 "
        "storage/sql.scalar_rows()（实测同一张 2000 行的表，构建器全表取回 "
        "6.76ms，原生投影分页 2.50ms）")


def test_list_documents_pushes_down_filters(store):
    items, total = list_documents(store, kb_id="kb-1", limit=5)
    assert len(items) == 5
    assert total == 20, f"total 应是过滤后的总数 20，实得 {total}"
    assert all(i["kb_id"] == "kb-1" for i in items)


def test_list_documents_pagination_has_no_overlap_or_gaps(store):
    """分页不重不漏 —— 「显式把排序下推」换来的性质。

    不写排序时 offset 依赖 fragment 布局，写入或 compaction 之后会漂移，
    界面上表现为同一条出现两次、或某条永远翻不到。
    """
    seen, per_page = [], 20
    for off in range(0, DOCS, per_page):
        items, _total = list_documents(store, limit=per_page, offset=off)
        seen.extend(i["doc_id"] for i in items)
    assert len(seen) == DOCS, f"取回 {len(seen)} 条，应为 {DOCS}"
    assert len(set(seen)) == DOCS, "分页之间有重复条目"


def test_list_documents_does_not_materialize_the_whole_table(store):
    """分页读只 materialize 一页 —— 「取全表再切片」的回归哨。

    只断言查询次数拦不住旧写法：它也只有 1 次查询，但那次把全部 60 行
    （生产上就是全部文档数）搬进了 Python。
    """
    with QueryCounter() as qc:
        list_documents(store, limit=10)
    assert sum(qc.rows) <= 10, (
        f"limit=10 却 materialize 了 {sum(qc.rows)} 行（全表 {DOCS} 行）")


def test_list_documents_escapes_like_wildcards_in_query(store):
    """标题子串过滤里的 `%` 不能被当通配符。

    下推成 ILIKE 之后必须显式守住这条语义：改前是 Python 的
    `needle in title`（纯子串）。不转义的话查 `100%` 会命中所有「以 100
    开头」的标题 —— 静默的语义漂移，不会报错。
    """
    _items, total = list_documents(store, q="文档 7%", limit=50)
    assert total == 0, f"`%` 被当成通配符了，命中 {total} 条"
    _items, total = list_documents(store, q="文档 7", limit=50)
    assert total > 0, "普通子串过滤应当仍能命中"


def test_contexts_fetch_is_a_single_query(store):
    """上下文回捞：**一次**查询取回所有命中文档的窗口。

    改前是「每篇命中文档一次」，实测占掉一次 hybrid 检索的三分之一
    （~33ms）。这是根因清单里 R4 的回归哨。
    """
    ranges = {f"doc-{i:04d}": (1, 3) for i in (1, 5, 9, 13)}
    with QueryCounter() as qc:
        got = contexts_for(store, ranges)
    assert qc.n == 1, f"上下文回捞发了 {qc.n} 次查询，应为 1"
    assert set(got) == set(ranges)
    assert all(len(v) >= 2 for v in got.values())


def test_search_query_count_does_not_grow_with_hits(store):
    """检索的查询次数不随命中文档数增长（N+1 的另一半哨兵）。"""
    from rag.models.embeddings import Embedder

    class _Stub(Embedder):
        def __init__(self):
            super().__init__(None, DIM, "stub", False)

        def embed(self, texts):
            return [[1.0] + [0.0] * (DIM - 1) for _ in texts]

    cfg = RetrieveConfig(mode="hybrid", candidate_k=10, top_k=5, window=1)
    counts = []
    for q in ("alpha1", "alpha30"):
        with QueryCounter() as qc:
            search(store, _Stub(), None, q, cfg, {}, 5)
        counts.append(qc.n)
    assert counts[0] == counts[1], (
        f"查询次数随查询词变化（{counts}）：某处在按命中数放大查询")


def test_health_is_a_bounded_number_of_scans(store):
    """对账的查询次数是常数，与文档数无关。"""
    with QueryCounter() as qc:
        h = health(store, DIM, "stub")
    assert h["doc_count"] == DOCS
    assert h["chunk_count"] == DOCS * CHUNKS_PER
    assert qc.n <= 8, f"对账发了 {qc.n} 次查询（上界 8）"


def test_write_lock_excludes_threads_and_is_reentrant(tmp_path):
    """写锁的**两层**保证都要在：跨进程用 flock，跨线程还得自己锁。

    `fcntl.flock` 的互斥单位是「打开文件描述」：同一进程里多个线程共用一个
    fd 时，第二个线程对同一 fd 再 flock 会直接成功。只把 RLock 换成 flock
    就等于把线程互斥丢掉 —— 这条真丢过一次，表现为
    `test_concurrent_ingest_no_duplicates` 与
    `test_concurrent_manual_chunks_get_distinct_ordinals` 同时变红。
    """
    import threading

    from rag.storage.filelock import FileLock

    lock = FileLock(tmp_path / "w.lock")
    inside = {"n": 0}
    worst = {"n": 0}
    guard = threading.Lock()

    def worker():
        for _ in range(80):
            with lock:
                with guard:
                    inside["n"] += 1
                    worst["n"] = max(worst["n"], inside["n"])
                time.sleep(0.0004)
                with guard:
                    inside["n"] -= 1

    ths = [threading.Thread(target=worker) for _ in range(4)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    assert worst["n"] == 1, f"临界区内同时有 {worst['n']} 个线程：线程互斥丢了"

    with lock:
        with lock:                       # 同线程重入：delete_documents 依赖它
            assert lock.locked_by_us()
    assert not lock.locked_by_us()


def test_lock_form_follows_storage_backend(tmp_path):
    """本地后端 → 跨进程文件锁；对象存储 → 进程内锁 + 如实告警。

    多机共享一个 bucket 时每台机器各有一把 `lance-write.lock` 等于没有锁，
    所以这里不假装安全，而是退回单进程互斥并打 warning，
    让运维在日志里看得见「这个形态不要开多进程」。
    """
    from rag.storage.filelock import FileLock, ReentrantMutex

    s3 = mock.MagicMock()
    s3.kind = "s3"
    assert isinstance(LanceStore._build_write_lock(
        mock.MagicMock(), s3, True), ReentrantMutex)

    local = mock.MagicMock()
    local.kind = "local"
    local.data_dir = tmp_path
    assert isinstance(LanceStore._build_write_lock(
        mock.MagicMock(), local, True), FileLock)


def test_local_store_reports_cross_process_lock(tmp_path):
    """真实装配路径上也必须是跨进程锁，否则多进程读不安全。"""
    from rag.storage.backend import LocalBackend

    s = LanceStore(LocalBackend(tmp_path), DIM)
    assert s.lock_kind == "cross-process", (
        f"本地后端却拿到 {s.lock_kind} 写锁：多进程形态不安全")


@pytest.mark.perf
def test_latency_budgets(store):
    """延迟门槛：只在 `pytest -m perf` 下跑。

    绝对延迟在这台机器上抖动可达 2×，所以默认套件只断言查询次数；
    这些门槛要手动跑，并且只在「同一台机器、同一份数据集」前提下比较。
    """
    def p50(fn, n=15):
        for _ in range(4):
            fn()
        ts = []
        for _ in range(n):
            t0 = time.perf_counter()
            fn()
            ts.append((time.perf_counter() - t0) * 1000)
        ts.sort()
        return ts[len(ts) // 2]

    assert p50(lambda: list_documents(store, limit=50)) < 25
    assert p50(lambda: get_document(store, "doc-0005")) < 25
    assert p50(lambda: health(store, DIM, "stub"), n=8) < 150
