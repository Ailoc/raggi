"""端到端：知识库 → 文档 → 分块 的完整业务链路。

用真实 FastAPI 应用 + 真实 LanceDB 存储，仅把 embedder 换成确定性向量，
从而在无 embedding 服务（Ollama）的环境下验证完整链路。
"""
from __future__ import annotations

import re
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rag.api import Ctx, create_app
from rag.core.config import Settings
from rag.models.registry import ModelRegistry
from rag.storage.tables import LanceStore

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
SRC_DIR = WEB_DIR / "src"

LONG = "。".join(f"这是第{i}句关于产品功能的详细说明内容" for i in range(120))


class StubEmbedder:
    """确定性伪向量：按字符统计生成。"""
    model = "stub-e2e"
    dim = 8

    def _vec(self, text):
        v = [0.0] * self.dim
        for i, ch in enumerate(str(text)):
            v[(ord(ch) + i) % self.dim] += 1.0
        n = sum(x * x for x in v) ** 0.5 or 1.0
        return [x / n for x in v]

    def embed(self, texts):
        return [self._vec(t) for t in texts]

    def embed_one(self, text):
        return self._vec(text)


@pytest.fixture()
def client():
    tmp = tempfile.mkdtemp()
    # 独立 Settings 实例，避免污染全局单例
    s = Settings(data_dir=Path(tmp))
    s.split.chunk_size = 512
    s.split.chunk_overlap = 64
    # 维度/模型须与 embedder 一致，否则 health 报 mismatch
    s.embed.dim = StubEmbedder.dim
    s.embed.model = StubEmbedder.model

    store = LanceStore.for_data_dir(s.data_dir, StubEmbedder.dim)
    registry = ModelRegistry(s)
    registry.bundle.embedder = StubEmbedder()
    return TestClient(create_app(Ctx(s, store, registry)))


def test_kb_plan_inheritance_and_chunks(client):
    # 建库并设默认分块方案
    r = client.post("/api/kbs", json={"name": "产品手册", "description": "产品文档",
                                      "chunk_size": 256, "overlap_ratio": 20})
    assert r.status_code == 200
    kid = r.json()["kb_id"]
    assert r.json()["plan_custom"] and r.json()["chunk_size"] == 256

    plan = client.get(f"/api/kbs/{kid}/plan").json()
    assert plan["effective"]["source"] == "kb"
    assert plan["effective"]["chunk_size"] == 256
    # 重叠按百分比换算
    assert plan["effective"]["overlap_chars"] == round(256 * 0.20)
    assert "overlap_chars" in plan["global"]

    # 入库：应按库方案切分
    doc = client.post("/api/documents/text",
                      json={"text": LONG, "title": "功能说明", "kb_id": kid}).json()
    did = doc["doc_id"]
    assert doc["status"] == "ready" and doc["chunk_count"] > 1

    items = client.get(f"/api/chunks?doc_id={did}&limit=500").json()["items"]
    assert max(len(c["text"]) for c in items) <= 300

    # 继承 + 快照
    dp = client.get(f"/api/documents/{did}/plan").json()
    assert dp["effective"]["source"] == "kb"
    assert dp["snapshot"]["chunk_size"] == 256

    # 改库默认值 → 文档快照不被污染
    client.put(f"/api/kbs/{kid}/plan", json={"chunk_size": 1024, "overlap_ratio": 30})
    dp2 = client.get(f"/api/documents/{did}/plan").json()
    assert dp2["snapshot"]["chunk_size"] == 256
    assert dp2["effective"]["chunk_size"] == 1024

    # 文档级覆盖
    client.put(f"/api/documents/{did}/plan", json={"chunk_size": 128, "overlap_ratio": 10})
    dp3 = client.get(f"/api/documents/{did}/plan").json()
    assert dp3["effective"]["source"] == "doc"
    assert dp3["effective"]["chunk_size"] == 128

    # 重新切分
    rs = client.post(f"/api/documents/{did}/resplit")
    assert rs.status_code == 200, rs.text
    items2 = client.get(f"/api/chunks?doc_id={did}&limit=500").json()["items"]
    assert max(len(c["text"]) for c in items2) < 200
    assert client.get(f"/api/documents/{did}/plan").json()["snapshot"]["chunk_size"] == 128

    # 清除覆盖 → 回落知识库
    client.put(f"/api/documents/{did}/plan", json={"chunk_size": 0, "overlap_ratio": 0})
    assert client.get(f"/api/documents/{did}/plan").json()["effective"]["source"] == "kb"


def test_standalone_chunks_belong_to_kb(client):
    kid = client.post("/api/kbs", json={"name": "库"}).json()["kb_id"]
    cid = client.post("/api/chunks",
                      json={"text": "独有关键词玄鸟标记", "kb_id": kid}).json()["chunk_id"]

    items = client.get(f"/api/chunks?kb_id={kid}&limit=500").json()["items"]
    standalone = [c for c in items if not c["doc_id"]]
    assert len(standalone) == 1
    assert standalone[0]["kb_id"] == kid

    # 不应产生「便签」虚拟文档
    docs = client.get(f"/api/documents?kb_id={kid}").json()["items"]
    assert not [d for d in docs if d["doc_id"] == "manual-notes"]

    # 库内检索能命中独立分块
    res = client.post("/api/search", json={"q": "玄鸟标记", "kb_id": kid,
                                           "mode": "fts", "top_k": 10}).json()
    assert any(x["chunk_id"] == cid for x in res["results"])
    assert all("kb_id" in x for x in res["results"])


def test_disabled_chunk_excluded_and_kb_isolated(client):
    kid = client.post("/api/kbs", json={"name": "库", "chunk_size": 256}).json()["kb_id"]
    doc = client.post("/api/documents/text",
                      json={"text": LONG, "title": "d", "kb_id": kid}).json()
    target = client.get(
        f"/api/chunks?doc_id={doc['doc_id']}&limit=10").json()["items"][0]

    q = {"q": "产品功能详细说明", "kb_id": kid, "mode": "fts", "top_k": 30}
    assert any(x["chunk_id"] == target["chunk_id"]
               for x in client.post("/api/search", json=q).json()["results"])

    client.patch(f"/api/chunks/{target['chunk_id']}/enabled", json={"enabled": False})
    assert not any(x["chunk_id"] == target["chunk_id"]
                   for x in client.post("/api/search", json=q).json()["results"])

    client.patch(f"/api/chunks/{target['chunk_id']}/enabled", json={"enabled": True})
    assert any(x["chunk_id"] == target["chunk_id"]
               for x in client.post("/api/search", json=q).json()["results"])

    # 另一个库检索不到
    kid2 = client.post("/api/kbs", json={"name": "另一个库"}).json()["kb_id"]
    res = client.post("/api/search", json={"q": "产品功能详细说明", "kb_id": kid2,
                                           "mode": "fts"}).json()
    assert res["results"] == []


def test_health_ok_with_standalone_chunks(client):
    """独立分块（doc_id 为空）是合法数据，不能算孤儿。"""
    kid = client.post("/api/kbs", json={"name": "库"}).json()["kb_id"]
    client.post("/api/documents/text", json={"text": LONG, "title": "d", "kb_id": kid})
    client.post("/api/chunks", json={"text": "独立分块", "kb_id": kid})

    h = client.get("/api/health").json()
    assert h["status"] == "ok", h
    assert h["dim_mismatch"] is False
    assert h["count_mismatch"] == []
    assert h["orphan_chunks"] == 0
    assert h["standalone_chunks"] == 1


def test_frontend_assets_served(client):
    """静态页面与构建产物始终可用。

    web/dist 是 Vite 产物且不入库，因此测试不应强制依赖 `npm run build`：
    未构建时跳过产物断言，构建后则确保 **index.html 引用的每个资源**
    都能真的取到——文件名带哈希，漏掉任何一个都会在浏览器里 404，
    而构建成功与静态检查都发现不了。
    """
    index = client.get("/")
    assert index.status_code == 200

    entry = WEB_DIR / "dist" / "index.html"
    if not entry.exists():
        pytest.skip("未构建前端产物（npm run build）")

    # 从真实产物里解析出被引用的资源，逐个取一次
    html = entry.read_text(encoding="utf-8")
    assets = re.findall(r'(?:src|href)="\.?/?(assets/[^"]+)"', html)
    assert assets, "产物 index.html 未引用任何 assets/ 资源"
    for a in assets:
        assert client.get("/" + a).status_code == 200, f"/{a}"


def test_legacy_frontend_files_removed(client):
    """旧的双栏骨架模块与 tsc 产物不得回流。"""
    for path in ("/js/documents.js", "/js/kb.js", "/js/main.js", "/app.css"):
        assert client.get(path).status_code == 404, path
