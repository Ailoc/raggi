"""把文档移动到其它知识库。

**为什么要有这个能力**：早前 `PUT /api/documents/{id}` 只接受 `title`，
前端传的 `kb_id` 被 Pydantic **静默忽略** —— 请求返回 200 却什么都没
发生，调用方完全无从察觉。文档入库后想换库只能删了重传，分块与手工
编辑全部丢失。

这里锁住的每一条都是那个"成功但没生效"背后的真实语义。
"""
from __future__ import annotations

import pytest


class _StubEmbedder:
    dim = 4
    model = "stub"

    def embed_one(self, text):
        return [1.0, 0.0, 0.0, 0.0]

    def embed(self, texts):
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


def _client(tmp_path):
    from fastapi.testclient import TestClient

    from rag.api import Ctx, create_app
    from rag.core.config import Settings
    from rag.models.registry import ModelRegistry
    from rag.storage.tables import LanceStore

    store = LanceStore.for_data_dir(tmp_path, 4)
    settings = Settings()
    settings.data_dir = tmp_path
    settings.embed.dim = 4
    settings.token = ""
    registry = ModelRegistry(settings)
    registry.bundle.embedder = _StubEmbedder()
    ctx = Ctx(settings, store, registry)
    c = TestClient(create_app(ctx), raise_server_exceptions=False)
    c.ctx = ctx      # type: ignore[attr-defined]
    c.store = store  # type: ignore[attr-defined]
    return c


def _mk_kb(c, name):
    return c.post("/api/kbs", json={"name": name}).json()["kb_id"]


def _mk_doc(c, kb_id="", title="t"):
    return c.post("/api/documents/text",
                  json={"title": title, "kb_id": kb_id,
                        "text": "移动测试正文。" * 20}).json()["doc_id"]


def _chunk_kb_ids(c, doc_id):
    """该文档全部分块的 kb_id（应与 documents.kb_id 一致）。"""
    from rag.storage.repos import escape_sql, fetch_rows

    rows = fetch_rows(c.ctx.store.chunks.search().select(["kb_id"]).where(
        f"doc_id = '{escape_sql(doc_id)}'"))
    return {str(r.get("kb_id") or "") for r in rows}


# ---- 基本移动 --------------------------------------------------------


def test_move_document_between_kbs(tmp_path):
    c = _client(tmp_path)
    a, b = _mk_kb(c, "库A"), _mk_kb(c, "库B")
    doc_id = _mk_doc(c, a)

    r = c.put(f"/api/documents/{doc_id}", json={"kb_id": b})
    assert r.status_code == 200, r.text
    # 回传实际生效的归属：调用方据此确认，而不是只能假设 200 = 办妥
    assert r.json()["kb_id"] == b

    detail = c.get(f"/api/documents/{doc_id}").json()
    assert detail["kb_id"] == b, "响应没反映真实归属"


def test_move_updates_chunks_table_too(tmp_path):
    """**两张表必须同步**。

    documents.kb_id 决定它出现在哪个库的列表里，chunks.kb_id 决定按库
    检索能否命中。只改前者会得到「库里有这篇文档，但按库检索搜不到」
    的分裂状态——而且没有任何报错，只表现为结果莫名其妙地少。
    """
    c = _client(tmp_path)
    a, b = _mk_kb(c, "库A"), _mk_kb(c, "库B")
    doc_id = _mk_doc(c, a)
    assert _chunk_kb_ids(c, doc_id) == {a}, "前置条件：分块初始归属"

    c.put(f"/api/documents/{doc_id}", json={"kb_id": b})
    assert _chunk_kb_ids(c, doc_id) == {b}, \
        f"分块归属没跟着改：{_chunk_kb_ids(c, doc_id)}"


def test_moved_document_is_searchable_in_new_kb(tmp_path):
    """端到端：移动后按新库检索能命中（这是两表同步的真正目的）。"""
    c = _client(tmp_path)
    a, b = _mk_kb(c, "库A"), _mk_kb(c, "库B")
    doc_id = _mk_doc(c, a, title="制动电阻")

    # 移动前：按 B 库检索搜不到
    r0 = c.post("/api/search", json={"q": "移动测试正文",
                                     "filters": {"kb_id": b}})
    assert r0.json()["results"] == [], "前置条件：B 库里不该有它"

    c.put(f"/api/documents/{doc_id}", json={"kb_id": b})
    r1 = c.post("/api/search", json={"q": "移动测试正文",
                                     "filters": {"kb_id": b}})
    assert len(r1.json()["results"]) == 1, \
        "移动后按新库检索搜不到（chunks.kb_id 没同步？）"


def test_move_to_ungrouped(tmp_path):
    """移出到未分组（kb_id=""）是真实需求：先批量导入再分类。"""
    c = _client(tmp_path)
    a = _mk_kb(c, "库A")
    doc_id = _mk_doc(c, a)

    r = c.put(f"/api/documents/{doc_id}", json={"kb_id": ""})
    assert r.status_code == 200, r.text
    assert r.json()["kb_id"] == ""
    assert c.get(f"/api/documents/{doc_id}").json()["kb_id"] == ""
    assert _chunk_kb_ids(c, doc_id) == {""}


# ---- 计数不漂 --------------------------------------------------------


def test_kb_counts_after_move(tmp_path):
    """移动后两个库的文档数/分块数都要正确重算。

    知识库计数是按 documents.kb_id + documents.chunk_count 聚合的，
    归属变了但计数没跟着变的话，列表页会长期显示错数。
    """
    c = _client(tmp_path)
    a, b = _mk_kb(c, "库A"), _mk_kb(c, "库B")
    doc_id = _mk_doc(c, a)

    def counts(kb_id):
        kb = c.get(f"/api/kbs/{kb_id}").json()
        return kb["doc_count"], kb["chunk_count"]

    assert counts(a)[0] == 1, "前置条件：A 库应有 1 篇"
    assert counts(b)[0] == 0

    c.put(f"/api/documents/{doc_id}", json={"kb_id": b})
    assert counts(a)[0] == 0, "A 库文档数没减少"
    assert counts(b)[0] == 1, "B 库文档数没增加"
    # 分块数也要跟着走
    assert counts(a)[1] == 0, f"A 库分块数没清零：{counts(a)}"
    assert counts(b)[1] >= 1, f"B 库分块数没增加：{counts(b)}"


def test_doc_list_is_scoped_by_kb_after_move(tmp_path):
    """文档列表按库过滤：移动后不应再出现在原库的列表里。"""
    c = _client(tmp_path)
    a, b = _mk_kb(c, "库A"), _mk_kb(c, "库B")
    doc_id = _mk_doc(c, a)

    c.put(f"/api/documents/{doc_id}", json={"kb_id": b})
    in_a = c.get(f"/api/documents?kb_id={a}").json()
    items = in_a if isinstance(in_a, list) else in_a.get("items", [])
    assert all(d["doc_id"] != doc_id for d in items), "仍出现在原库列表里"

    in_b = c.get(f"/api/documents?kb_id={b}").json()
    items_b = in_b if isinstance(in_b, list) else in_b.get("items", [])
    assert any(d["doc_id"] == doc_id for d in items_b), "目标库里看不到"


# ---- 边界与校验 ------------------------------------------------------


def test_move_to_nonexistent_kb_is_rejected(tmp_path):
    """指向不存在的库必须报错，不能留下悬空引用。

    悬空引用的症状是文档从此不出现在任何列表里，且没有任何提示。
    """
    c = _client(tmp_path)
    a = _mk_kb(c, "库A")
    doc_id = _mk_doc(c, a)

    r = c.put(f"/api/documents/{doc_id}", json={"kb_id": "does-not-exist"})
    assert r.status_code == 400, f"应拒绝悬空引用，实际 {r.status_code}"
    # 归属必须保持原样
    assert c.get(f"/api/documents/{doc_id}").json()["kb_id"] == a


def test_move_nonexistent_document_is_404(tmp_path):
    c = _client(tmp_path)
    b = _mk_kb(c, "库B")
    r = c.put("/api/documents/no-such-doc", json={"kb_id": b})
    assert r.status_code == 404


def test_omitting_kb_id_leaves_it_unchanged(tmp_path):
    """不传 kb_id = 不动（与传空串「移出未分组」语义不同）。"""
    c = _client(tmp_path)
    a, b = _mk_kb(c, "库A"), _mk_kb(c, "库B")
    doc_id = _mk_doc(c, a)

    c.put(f"/api/documents/{doc_id}", json={"title": "改个名"})
    assert c.get(f"/api/documents/{doc_id}").json()["kb_id"] == a


def test_move_to_same_kb_is_idempotent(tmp_path):
    """移到当前所在库应当是幂等的空操作，不能报错也不能改坏数据。"""
    c = _client(tmp_path)
    a = _mk_kb(c, "库A")
    doc_id = _mk_doc(c, a)
    before = _chunk_kb_ids(c, doc_id)

    r = c.put(f"/api/documents/{doc_id}", json={"kb_id": a})
    assert r.status_code == 200, r.text
    assert r.json()["kb_id"] == a
    assert _chunk_kb_ids(c, doc_id) == before


def test_title_and_kb_can_be_changed_together(tmp_path):
    c = _client(tmp_path)
    a, b = _mk_kb(c, "库A"), _mk_kb(c, "库B")
    doc_id = _mk_doc(c, a, title="旧标题")

    r = c.put(f"/api/documents/{doc_id}",
              json={"title": "新标题", "kb_id": b})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["title"] == "新标题" and body["kb_id"] == b


def test_unknown_field_is_rejected(tmp_path):
    """拼错的字段名必须 422，而不是「成功但没生效」。

    这正是本次问题的根因：extra 默认 ignore 时，`kb_id` 之外任何
    笔误（比如 `kbid`、`kb-id`）都会静默消失，接口还返回 200。
    """
    c = _client(tmp_path)
    doc_id = _mk_doc(c)
    r = c.put(f"/api/documents/{doc_id}", json={"kbid": "typo"})
    assert r.status_code == 422, f"多余字段应被拒绝，实际 {r.status_code}"


def test_manual_chunks_survive_move(tmp_path):
    """移动不动分块内容：手工新增的块必须还在。"""
    c = _client(tmp_path)
    a, b = _mk_kb(c, "库A"), _mk_kb(c, "库B")
    doc_id = _mk_doc(c, a)

    r = c.post("/api/chunks", json={"doc_id": doc_id, "text": "手工块内容"})
    assert r.status_code in (200, 201), r.text
    chunk_id = r.json()["chunk_id"]

    c.put(f"/api/documents/{doc_id}", json={"kb_id": b})

    got = c.get(f"/api/chunks?doc_id={doc_id}").json()
    items = got if isinstance(got, list) else got.get("items", [])
    assert any(x["chunk_id"] == chunk_id for x in items), \
        "手工块在移动后消失了"


def test_standalone_chunks_are_not_moved(tmp_path):
    """独立分块（doc_id=""）不属于任何文档，不该被文档移动牵连。

    它们靠自己的 kb_id 归属；文档移动只改该文档自己的分块。
    """
    from rag.storage.repos import escape_sql, fetch_rows

    c = _client(tmp_path)
    a, b = _mk_kb(c, "库A"), _mk_kb(c, "库B")
    doc_id = _mk_doc(c, a)

    c.post("/api/chunks", json={"kb_id": a, "text": "独立分块内容"})

    c.put(f"/api/documents/{doc_id}", json={"kb_id": b})

    rows = fetch_rows(c.ctx.store.chunks.search().select(["kb_id", "doc_id"]).where(
        f"kb_id = '{escape_sql(a)}'"))
    assert rows, "A 库的独立分块不该被删或被改归属"
    assert all(str(r.get("doc_id") or "") == "" for r in rows), \
        "移动文档不应改动独立分块"