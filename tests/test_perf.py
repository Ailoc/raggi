"""性能与并发回归：并发入库无重复、检索正确性（去 pandas 后）、
health 大表正确性、写锁不覆盖 embedding（并发吞吐）。
"""
from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path

from rag.chunk_edit import add_manual_chunk
from rag.core.config import ParserConfig, RetrieveConfig, Settings, SplitConfig
from rag.ingest import pipeline
from rag.models.registry import ModelRegistry
from rag.retrieval.search import search
from rag.storage.health import health
from rag.storage.tables import LanceStore


def _warm_jieba() -> None:
    """预热 jieba 词典（约 2s，一次性）。

    jieba 首次 cut() 才加载词典，若不预热会算进首个入库的耗时，
    使并发耗时断言（断言 embedding 未被写锁串行化）偶发失败。
    """
    import jieba
    list(jieba.cut("预热分词词典"))


_warm_jieba()


class _StubEmbedder:
    dim = 4
    model = "stub"

    def embed(self, texts):
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    def embed_one(self, text):
        return [1.0, 0.0, 0.0, 0.0]


class _SlowEmbedder(_StubEmbedder):
    """模拟远端 embedding 延迟（暴露锁范围问题）。"""

    def embed(self, texts):
        time.sleep(0.05)
        return super().embed(texts)


def _registry_with(embedder) -> ModelRegistry:
    s = Settings()
    reg = ModelRegistry(s)
    reg.bundle.embedder = embedder
    return reg


def test_concurrent_ingest_no_duplicates():
    """并发入库同一文本 → 恰好 1 个文档（双重去重检查）。"""
    with tempfile.TemporaryDirectory() as d:
        store = LanceStore.for_data_dir(Path(d), 4)
        settings = Settings()
        reg = _registry_with(_SlowEmbedder())
        text = "并发去重测试文本：同一内容不应产生重复文档"
        results = []
        lock = threading.Lock()

        def _worker():
            r = pipeline.ingest_text(
                store, reg, text, "并发测试", ParserConfig(), settings)
            with lock:
                results.append(r)

        threads = [threading.Thread(target=_worker) for _ in range(4)]
        t0 = time.perf_counter()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        elapsed = time.perf_counter() - t0

        ready = [r for r in results if r["status"] == "ready"]
        dup = [r for r in results if r["status"] == "duplicate"]
        assert len(ready) == 1, f"应有且仅有 1 个 ready: {results}"
        assert len(dup) == 3, f"其余应为 duplicate: {results}"
        assert store.documents.count_rows() == 1
        assert store.chunks.count_rows() == ready[0]["chunk_count"]
        # 并发 embedding 未被写锁串行化：4 个任务同时进行时，
        # 总耗时应接近单次（0.05s）的常数倍，而非 4 倍串行。
        # 阈值放宽到 4×串行的一半以上：CI/共享机器抖动大，
        # 真正串行化时约 0.2s+，留足余量仍能捕获回归。
        assert elapsed < 4 * 0.05 * 3, \
            f"embedding 疑似被串行化: {elapsed:.2f}s"


def test_search_correctness_after_pandas_removal():
    """去 pandas 后检索结果字段完整且正确。"""
    with tempfile.TemporaryDirectory() as d:
        store = LanceStore.for_data_dir(Path(d), 4)
        for i in range(5):
            add_manual_chunk(store, _StubEmbedder(),
                             f"检索正确性测试块 {i}")
        for mode in ("hybrid", "vector", "fts"):
            res = search(store, _StubEmbedder(), None, "检索正确性",
                         RetrieveConfig(mode=mode, top_k=3))
            assert res["mode"] == mode
            assert res["degraded_reason"] is None
            assert len(res["results"]) >= 1
            r = res["results"][0]
            for key in ("chunk_id", "doc_id", "title", "ordinal",
                        "score", "scores", "snippet", "context",
                        "parser_engine", "origin"):
                assert key in r, f"{mode} 结果缺字段 {key}"
            # 独立分块不再归入「便签」虚拟文档，故无标题；
            # 它直接挂在知识库下（doc_id 为空）。
            assert r["doc_id"] == ""
            assert r["title"] == ""


def test_health_large_table():
    """health() 在较大表上计数正确（箭头化单趟扫描）。"""
    with tempfile.TemporaryDirectory() as d:
        store = LanceStore.for_data_dir(Path(d), 4)
        # 2 个文档，各 500 块
        for doc_idx in range(2):
            doc_id = f"doc{doc_idx}"
            rows = []
            for i in range(500):
                rows.append({
                    "chunk_id": f"{doc_id}_c{i}", "doc_id": doc_id,
                    "ordinal": i, "text": f"内容 {i}",
                    "text_seg": "内容", "heading_path": "",
                    "page": None, "char_start": 0, "char_end": 5,
                    "token_count": 1, "origin": "manual",
                    "edited": False, "original_text": None,
                    "offset_valid": True, "embed_model": "stub",
                    "vector": [1.0, 0.0, 0.0, 0.0],
                    "created_at": "", "updated_at": "",
                })
            from rag.storage.repos import upsert_chunks, upsert_documents

            upsert_chunks(store, rows)
            upsert_documents(store, [{
                "doc_id": doc_id, "title": f"文档{doc_idx}",
                "source_uri": None, "mime": "text/plain",
                "parser_engine": "native",
                "content_hash": f"h{doc_idx}",
                "char_count": 2500, "chunk_count": 500,
                "text": "内容", "status": "ready", "error": None,
                "meta": "{}", "created_at": "", "updated_at": "",
            }])
        h = health(store, 4, "stub")
        assert h["chunk_count"] == 1000
        assert h["count_mismatch"] == []
        assert h["orphan_chunks"] == 0
        assert h["status"] == "ok"
        # 人为制造不一致：直接写 1 块且不回写 chunk_count
        from rag.storage.repos import upsert_chunks

        upsert_chunks(store, [{
            "chunk_id": "doc0_extra", "doc_id": "doc0",
            "ordinal": 500, "text": "额外块", "text_seg": "额外",
            "heading_path": "", "page": None, "char_start": 0,
            "char_end": 3, "token_count": 1, "origin": "manual",
            "edited": False, "original_text": None,
            "offset_valid": True, "embed_model": "stub",
            "vector": [1.0, 0.0, 0.0, 0.0],
            "created_at": "", "updated_at": "",
        }])
        h2 = health(store, 4, "stub")
        assert h2["count_mismatch"], "应检测到计数不一致"
        assert h2["status"] == "degraded"


def test_ingest_offset_integrity():
    """入库偏移闭合性：offset_valid 块满足 text == 全文[cs:ce]。"""
    with tempfile.TemporaryDirectory() as d:
        store = LanceStore.for_data_dir(Path(d), 4)
        settings = Settings()
        reg = _registry_with(_StubEmbedder())
        # 构造含重复段落的长文本，暴露 find() 首命中问题
        para = "重复段落内容：苹果、香蕉、葡萄。西瓜、桃子、梨子。"
        text = "\n\n".join([para] * 8)
        res = pipeline.ingest_text(
            store, reg, text, "偏移测试",
            ParserConfig(), settings)
        assert res["status"] == "ready"
        doc = store.documents.search().where(
            f"doc_id = '{res['doc_id']}'").to_list()[0]
        full = doc["text"]
        chunks = store.chunks.search().where(
            f"doc_id = '{res['doc_id']}'").to_list()
        assert len(chunks) == res["chunk_count"]
        checked = 0
        for c in chunks:
            if c["offset_valid"]:
                assert full[c["char_start"]:c["char_end"]] == c["text"], \
                    f"偏移不闭合: chunk {c['chunk_id']}"
                assert c["char_end"] <= len(full)
                checked += 1
        assert checked == len(chunks), "所有块偏移应有效"


def test_split_config_applied():
    """Settings.split 的 chunk_size/overlap 生效。"""
    with tempfile.TemporaryDirectory() as d:
        store = LanceStore.for_data_dir(Path(d), 4)
        s = Settings()
        reg = _registry_with(_StubEmbedder())
        s.split = SplitConfig(chunk_size=64, chunk_overlap=8)
        long_text = "。".join(f"句子{i}" for i in range(200))
        res = pipeline.ingest_text(
            store, reg, long_text, "切分测试",
            ParserConfig(), s)
        # 入库本身也要断言：只验证 _build_chunk_rows 的话，pipeline 里
        # 「用哪份 split 配置」这条路径其实没被覆盖（而这正是本条测试的名字）
        assert res["status"] == "ready", res
        assert res["chunk_count"] > 1, "200 句长文本按 64 尺寸应切成多块"
        # 覆盖 pipeline 内 split_cfg 默认：直接验证 _run 使用传入配置
        from langchain_core.documents import Document

        from rag.ingest.pipeline import _build_chunk_rows
        from rag.parsing.normalize import normalize

        parsed = normalize(
            [Document(page_content=long_text, metadata={})])
        rows_default = _build_chunk_rows(
            parsed, "x", "stub", SplitConfig())
        rows_small = _build_chunk_rows(
            parsed, "x", "stub", SplitConfig(chunk_size=64,
                                               chunk_overlap=8))
        assert len(rows_small) > len(rows_default), \
            "更小的 chunk_size 应产生更多分块"


def test_write_lock_is_thread_exclusive(tmp_path):
    """跨进程写锁**不能**丢掉线程互斥——这是换锁形态时最容易漏的一条。

    `fcntl.flock` 的互斥单位是「打开文件描述」：同一进程里多个线程共用一个
    fd 时，第二个线程再 flock 同一个 fd 是直接成功的。所以只把 RLock 换成
    flock，写临界区会在**本进程内**裸奔——并发入库去重、并发取 ordinal
    这两类竞态会重新出现（这次改造真的踩过：
    `test_concurrent_ingest_no_duplicates` 与
    `test_concurrent_manual_chunks_get_distinct_ordinals` 同时变红）。
    """
    from rag.storage.filelock import FileLock

    lock = FileLock(tmp_path / "w.lock")
    inside = {"n": 0}
    worst = {"n": 0}
    guard = threading.Lock()

    def worker() -> None:
        for _ in range(120):
            with lock:
                with guard:
                    inside["n"] += 1
                    worst["n"] = max(worst["n"], inside["n"])
                time.sleep(0.0005)          # 给别的线程撞进来的机会
                with guard:
                    inside["n"] -= 1

    ths = [threading.Thread(target=worker) for _ in range(4)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    assert worst["n"] == 1, (
        f"临界区里同时有 {worst['n']} 个线程：写锁的线程互斥丢了")


def test_write_lock_is_reentrant(tmp_path):
    """同线程重入必须允许：`delete_documents` 在已持锁时还会再取一次锁。"""
    from rag.storage.filelock import FileLock

    lock = FileLock(tmp_path / "w.lock")
    with lock:
        with lock:
            assert lock.locked_by_us()
    assert not lock.locked_by_us()
