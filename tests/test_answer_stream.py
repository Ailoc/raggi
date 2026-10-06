"""问答流式（SSE）端点。

对应「流式问答」补齐：非流式 /answer 只能整段返回，用户要盯着空白等
到整段生成完毕。这里锁住事件序列与关键设计——**引用必须先于正文发出**，
因为用户判断要不要继续读取决于依据是否靠谱，而依据在生成前就已确定。
"""
from __future__ import annotations

import json
import re

import pytest


class _FakeChunk:
    """模拟 LangChain 的流式片段。"""

    def __init__(self, content):
        self.content = content


class _FakeChat:
    """按固定片段吐出内容，并记录收到的 prompt。"""

    def __init__(self, pieces):
        self.pieces = pieces
        self.prompts: list[str] = []

    async def ainvoke(self, prompt):
        self.prompts.append(prompt)
        return _FakeChunk("".join(self.pieces))

    async def astream(self, prompt):
        self.prompts.append(prompt)
        for p in self.pieces:
            yield _FakeChunk(p)


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    """把 SSE 文本解析成 [(event, data)]。"""
    out = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        ev = re.search(r"^event: (.+)$", block, re.M)
        data = re.search(r"^data: (.+)$", block, re.M)
        assert ev, f"事件缺少 event 字段: {block!r}"
        assert data, f"事件缺少 data 字段: {block!r}"
        out.append((ev.group(1).strip(), json.loads(data.group(1))))
    return out


@pytest.fixture()
def env(tmp_path):
    """装配应用，并注入可控的 chat。

    chat 走 bundle.chat_factory 惰性构造（见 ModelRegistry.chat），
    所以替换的是 factory 本身——直接赋值 bundle.chat 不会生效，
    那样测的就不是线上走的路径了。
    """
    from rag.api import Ctx, create_app
    from rag.core.config import Settings
    from rag.models.registry import ModelRegistry
    from rag.storage.tables import LanceStore

    class _E:
        dim = 4
        model = "stub"

        def embed_one(self, text):  # noqa: ARG002
            return [1.0, 0.0, 0.0, 0.0]

        def embed(self, texts):
            return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    store = LanceStore.for_data_dir(tmp_path, 4)
    s = Settings()
    s.data_dir = tmp_path
    s.embed.dim = 4
    s.features.answer = True
    registry = ModelRegistry(s)
    registry.bundle.embedder = _E()

    def use_chat(chat):
        registry.bundle.chat_factory = lambda: chat

    use_chat(_FakeChat(["占位"]))

    from fastapi.testclient import TestClient

    ctx = Ctx(s, store, registry)
    app = create_app(ctx)
    client = TestClient(app, raise_server_exceptions=False)
    client.store = store  # type: ignore[attr-defined]
    client.use_chat = use_chat  # type: ignore[attr-defined]
    return client, registry


def _seed(client) -> None:
    for i in range(3):
        client.post("/api/v1/documents/text", json={
            "text": f"变频器额定电压 {380 * (i + 1)}V，允许波动。 " * 20,
            "title": f"手册{i}",
        })
    client.store.ensure_fts_index(force=True)


def test_stream_emits_sources_then_deltas_then_done(env):
    client, _ = env
    _seed(client)
    client.use_chat(_FakeChat(["变频器", "额定电压为", "380V"]))

    r = client.post("/api/v1/answer/stream",
                    json={"q": "额定电压", "top_k": 3})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")

    events = _parse_sse(r.text)
    names = [e for e, _ in events]
    assert names[0] == "sources", "引用必须在正文之前发出"
    assert names[-1] == "done"
    assert names.count("delta") == 3

    deltas = "".join(d["text"] for e, d in events if e == "delta")
    assert deltas == "变频器额定电压为380V"

    sources = next(d for e, d in events if e == "sources")["citations"]
    assert sources, "sources 事件应带非空引用表"
    assert sources[0]["index"] == 1
    assert {"chunk_id", "doc_id", "title", "score"} <= set(sources[0])

    done = next(d for e, d in events if e == "done")
    assert done["mode"]
    assert "took_ms" in done


def test_stream_and_nonstream_share_citations(env):
    """两条路径的引用必须一致。

    回归防护：它们共用 prepare()，若哪天分叉，用户会在两个界面看到
    不同的 [n] 对应关系——极难察觉。
    """
    client, _ = env
    _seed(client)

    stream = _parse_sse(client.post(
        "/api/v1/answer/stream", json={"q": "额定电压", "top_k": 3}).text)
    s_cites = next(d for e, d in stream if e == "sources")["citations"]

    client.use_chat(_FakeChat(["答案"]))
    plain = client.post("/api/v1/answer", json={"q": "额定电压", "top_k": 3}).json()

    assert [(c["index"], c["chunk_id"]) for c in s_cites] == \
           [(c["index"], c["chunk_id"]) for c in plain["citations"]]


def test_stream_handles_empty_retrieval(env):
    """没检索到内容时也要有明确事件，而不是空流。"""
    client, _ = env
    _seed(client)
    client.use_chat(_FakeChat(["不该被调用"]))

    # mode=fts（纯关键词）：桩 embedder 让所有向量相同，
    # hybrid 的向量通道会把它们全算命中，测不出「无结果」这条路
    events = _parse_sse(client.post(
        "/api/v1/answer/stream",
        json={"q": "库里完全没有这个词zzz", "top_k": 5,
              "mode": "fts"}).text)
    names = [e for e, _ in events]
    assert names[0] == "delta", "空结果应先给出说明"
    assert "未检索到" in next(d for e, d in events if e == "delta")["text"]
    assert names[-1] == "done"


def test_stream_reports_error_as_event(env):
    """出错时推 error 事件，而不是让连接凭空断掉。

    断连在前端只剩一个永久的 loading，用户完全无从判断发生了什么。
    """

    class _Boom:
        async def astream(self, prompt):  # noqa: ARG002
            raise RuntimeError("模型服务连接失败")
            yield  # 让函数成为异步生成器

    client, _ = env
    _seed(client)
    client.use_chat(_Boom())

    events = _parse_sse(client.post(
        "/api/v1/answer/stream", json={"q": "额定电压", "top_k": 2}).text)
    names = [e for e, _ in events]
    assert "error" in names, f"未推 error 事件: {names}"
    err = next(d for e, d in events if e == "error")
    assert "模型服务连接失败" in err["message"]
    assert err["hint"]


def test_stream_handles_empty_model_output(env):
    """provider 一次都没吐出内容时要如实说明，而不是静默结束。"""

    class _Silent:
        async def astream(self, prompt):  # noqa: ARG002
            if False:      # 空异步流：一次都不吐
                yield

    client, _ = env
    _seed(client)
    client.use_chat(_Silent())

    events = _parse_sse(client.post(
        "/api/v1/answer/stream", json={"q": "额定电压", "top_k": 2}).text)
    text = "".join(d["text"] for e, d in events if e == "delta")
    assert "未返回内容" in text


def test_stream_normalizes_list_content(env):
    """部分 OpenAI 兼容端点的片段 content 是 [{type,text}] 列表。"""

    class _ListContent:
        async def astream(self, prompt):  # noqa: ARG002
            yield _FakeChunk([{"type": "text", "text": "第一段"}])
            yield _FakeChunk([{"type": "text", "text": "第二段"}])

    client, _ = env
    _seed(client)
    client.use_chat(_ListContent())

    events = _parse_sse(client.post(
        "/api/v1/answer/stream", json={"q": "额定电压", "top_k": 2}).text)
    text = "".join(d["text"] for e, d in events if e == "delta")
    assert text == "第一段第二段"


def test_answer_requires_feature_flag(tmp_path):
    """开关关闭时两个端点都是 503（流式端点也不是静默空流）。"""
    from fastapi.testclient import TestClient

    from rag.api import Ctx, create_app
    from rag.core.config import Settings
    from rag.models.registry import ModelRegistry
    from rag.storage.tables import LanceStore

    d = Settings()
    d.data_dir = tmp_path
    d.embed.dim = 4
    d.features.answer = False
    app = create_app(Ctx(d, LanceStore.for_data_dir(tmp_path, 4),
                       ModelRegistry(d)))
    c = TestClient(app, raise_server_exceptions=False)
    assert c.post("/api/v1/answer", json={"q": "x"}).status_code == 503
    r = c.post("/api/v1/answer/stream", json={"q": "x"})
    assert r.status_code == 503
