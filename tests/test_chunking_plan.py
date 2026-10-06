"""分块方案三级继承（全局 → 知识库 → 文档）与分块启停/归属。"""
from __future__ import annotations

import tempfile
from pathlib import Path

from rag.chunk_edit import add_manual_chunk, set_chunk_enabled
from rag.core.config import ParserConfig, RetrieveConfig, Settings, SplitConfig
from rag.ingest import pipeline
from rag.models.registry import ModelRegistry
from rag.retrieval.search import search
from rag.storage import plan as chunking
from rag.storage.repos import kbs
from rag.storage.tables import LanceStore


class _StubEmbedder:
    dim = 4
    model = "stub"

    def embed(self, texts):
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    def embed_one(self, text):
        return [1.0, 0.0, 0.0, 0.0]


def _store():
    d = tempfile.mkdtemp()
    return LanceStore.for_data_dir(Path(d), 4)


def _settings():
    s = Settings()
    s.split = SplitConfig(chunk_size=512, chunk_overlap=64)
    return s


def _registry(s):
    reg = ModelRegistry(s)
    reg.bundle.embedder = _StubEmbedder()
    return reg


def test_system_default_used_when_no_kb():
    """不属于任何知识库的内容落到系统默认。

    两级模型（知识库 → 文档）下不再有「全局」这个用户可见层级；
    `settings.split` 只是**系统默认值**，因此 source 标为 "default"
    而非 "global"——后者会让人以为还有一级可配置的继承。
    """
    store, s = _store(), _settings()
    plan = chunking.resolve_plan(store, s, kb_id="", doc_id="")
    assert plan["source"] == "default"
    assert plan["chunk_size"] == 512


def test_kb_plan_overrides_global():
    store, s = _store(), _settings()
    kb = kbs.create_kb(store, "库", chunk_size=1024, overlap_ratio=20)
    plan = chunking.resolve_plan(store, s, kb_id=kb["kb_id"])
    assert plan["source"] == "kb"
    assert plan["chunk_size"] == 1024
    # 百分比换算：1024 * 20% = 205
    assert plan["overlap_chars"] == round(1024 * 0.20)


def test_doc_plan_overrides_kb():
    store, s = _store(), _settings()
    kb = kbs.create_kb(store, "库", chunk_size=1024, overlap_ratio=20)
    doc = pipeline.ingest_text(store, _registry(s), "一段中文正文内容用于测试。",
                               "d", ParserConfig(), s, kb["kb_id"])
    chunking.set_doc_plan(store, doc["doc_id"],
                          chunk_size=256, overlap_ratio=10)
    plan = chunking.resolve_plan(store, s,
                                 kb_id=kb["kb_id"], doc_id=doc["doc_id"])
    assert plan["source"] == "doc"
    assert plan["chunk_size"] == 256


def test_clearing_doc_plan_falls_back_to_kb():
    store, s = _store(), _settings()
    kb = kbs.create_kb(store, "库", chunk_size=1024)
    doc = pipeline.ingest_text(store, _registry(s), "另一段正文内容测试。",
                               "d", ParserConfig(), s, kb["kb_id"])
    chunking.set_doc_plan(store, doc["doc_id"],
                          chunk_size=256, overlap_ratio=10)
    chunking.set_doc_plan(store, doc["doc_id"],
                          chunk_size=0, overlap_ratio=0)
    plan = chunking.resolve_plan(store, s,
                                 kb_id=kb["kb_id"], doc_id=doc["doc_id"])
    assert plan["source"] == "kb"
    assert plan["chunk_size"] == 1024


def test_snapshot_recorded_and_survives_kb_change():
    """入库时快照生效方案；之后改知识库默认值不影响快照。"""
    store, s = _store(), _settings()
    kb = kbs.create_kb(store, "库", chunk_size=1024, overlap_ratio=20)
    doc = pipeline.ingest_text(store, _registry(s), "快照测试正文内容。",
                               "d", ParserConfig(), s, kb["kb_id"])
    snap = chunking.doc_plan(store, doc["doc_id"])["snapshot"]
    assert snap and snap["chunk_size"] == 1024

    kbs.update_kb(store, kb["kb_id"], chunk_size=2048)
    snap2 = chunking.doc_plan(store, doc["doc_id"])["snapshot"]
    assert snap2["chunk_size"] == 1024, "快照不应被后续改动污染"


def test_chunks_carry_kb_id():
    store, s = _store(), _settings()
    kb = kbs.create_kb(store, "库")
    doc = pipeline.ingest_text(store, _registry(s), "归属测试正文内容。",
                               "d", ParserConfig(), s, kb["kb_id"])
    rows = store.chunks.search().where(
        f"doc_id = '{doc['doc_id']}'").to_list()
    assert rows and all(r["kb_id"] == kb["kb_id"] for r in rows)


def test_standalone_chunk_has_empty_doc_id_and_kb_id():
    store = _store()
    kb = kbs.create_kb(store, "库")
    cid = add_manual_chunk(store, _StubEmbedder(), "独立分块内容", None,
                           kb["kb_id"])
    row = store.chunks.search().where(
        f"chunk_id = '{cid}'").to_list()[0]
    assert row["doc_id"] == ""
    assert row["kb_id"] == kb["kb_id"]
    assert row["origin"] == "manual"
    # 不应产生任何虚拟文档
    assert store.documents.count_rows() == 0


def test_disabled_chunk_excluded_from_search():
    store = _store()
    cid = add_manual_chunk(store, _StubEmbedder(), "可被检索到的独特词元")
    res = search(store, _StubEmbedder(), None, "独特词元",
                 RetrieveConfig(mode="fts", top_k=5))
    assert any(r["chunk_id"] == cid for r in res["results"])

    set_chunk_enabled(store, cid, False)
    res2 = search(store, _StubEmbedder(), None, "独特词元",
                  RetrieveConfig(mode="fts", top_k=5))
    assert not any(r["chunk_id"] == cid for r in res2["results"])

    # 恢复后重新可检索
    set_chunk_enabled(store, cid, True)
    res3 = search(store, _StubEmbedder(), None, "独特词元",
                  RetrieveConfig(mode="fts", top_k=5))
    assert any(r["chunk_id"] == cid for r in res3["results"])


def test_kb_filter_includes_standalone_chunks():
    """按知识库检索应同时覆盖文档分块与独立分块。"""
    store, s = _store(), _settings()
    kb = kbs.create_kb(store, "库")
    other = kbs.create_kb(store, "另一个库")
    pipeline.ingest_text(store, _registry(s), "文档里的独特词汇甲。",
                         "d", ParserConfig(), s, kb["kb_id"])
    cid = add_manual_chunk(store, _StubEmbedder(), "独立分块里的独特词汇乙",
                           None, kb["kb_id"])
    add_manual_chunk(store, _StubEmbedder(), "别的库里的独特词汇丙",
                     None, other["kb_id"])

    res = search(store, _StubEmbedder(), None, "独特词汇",
                 RetrieveConfig(mode="fts", top_k=20),
                 filters={"kb_id": kb["kb_id"]})
    ids = {r["chunk_id"] for r in res["results"]}
    assert cid in ids, "独立分块应能被知识库检索命中"
    assert all(r["kb_id"] == kb["kb_id"] for r in res["results"])


def test_overlap_never_exceeds_half_chunk():
    for size in (64, 100, 512, 1024):
        for ratio in (0, 10, 25, 50, 99):
            ov = chunking.overlap_chars(size, ratio)
            assert 0 <= ov < size, (size, ratio, ov)


def test_plan_values_are_clamped():
    assert chunking.clamp_size(0) == 0
    assert chunking.clamp_size(1) == chunking.MIN_CHUNK_SIZE
    assert chunking.clamp_size(10**9) == chunking.MAX_CHUNK_SIZE
    assert chunking.clamp_ratio(-5) == 0.0
    assert chunking.clamp_ratio(90) == chunking.MAX_OVERLAP_RATIO
    assert chunking.clamp_ratio("bad") == 0.0


def test_all_plan_shapes_expose_same_keys():
    """API 直接把 global/effective 当 payload 用，键必须齐全。

    回归防护：global_plan 曾缺 overlap_chars，导致 /api/kbs/{id}/plan 500。
    kb_plan 只需 own 三键（source 由 resolve_plan 判定）。
    """
    store, s = _store(), _settings()
    kb = kbs.create_kb(store, "库", chunk_size=256, overlap_ratio=20)

    for p in (chunking.global_plan(s), chunking.resolve_plan(store, s, kb_id=kb["kb_id"])):
        for key in ("chunk_size", "overlap_ratio", "source", "custom", "overlap_chars"):
            assert key in p, f"缺少 {key}: {p}"

    own = chunking.kb_plan(store, kb["kb_id"], s)
    for key in ("chunk_size", "overlap_ratio", "custom"):
        assert key in own, f"缺少 {key}: {own}"


def test_global_ratio_reflects_configured_overlap():
    """全局存的是绝对 overlap，展示时换算回百分比。"""
    s = Settings()
    s.split = SplitConfig(chunk_size=500, chunk_overlap=100)
    p = chunking.global_plan(s)
    assert p["chunk_size"] == 500
    assert p["overlap_ratio"] == 20.0
    assert p["overlap_chars"] == 100
