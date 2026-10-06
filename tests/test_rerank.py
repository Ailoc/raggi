"""HTTP 精排的回归测试。

覆盖上一版「`provider="api"` 直接 return None（等于没实现）」的缺口：
- 真实发出 POST {base}/rerank 并解析结果；
- `/v1/rerank` 前缀自动探测；
- 响应字段命名兼容（results/data、relevance_score/score）；
- 只返回 top_n 条时按下标回填，结果长度与入参一致；
- 失败降级：精排不可用时检索仍然可用并标注原因。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest


class _RerankStub(BaseHTTPRequestHandler):
    """最小 rerank 服务：按关键词命中数给分。"""

    calls: list[dict] = []
    # 可注入行为
    path_prefix = "/rerank"
    field_names = ("results", "relevance_score")
    top_n_only = False
    fail_code = 0

    def log_message(self, *a):  # 静音
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        type(self).calls.append({
            "path": self.path,
            "auth": self.headers.get("Authorization"),
            "body": body,
        })
        if type(self).fail_code:
            self.send_response(type(self).fail_code)
            self.end_headers()
            self.wfile.write(b'{"error":"boom"}')
            return
        if not self.path.endswith(type(self).path_prefix):
            self.send_response(404)
            self.end_headers()
            return

        q = str(body.get("query") or "")
        docs = body.get("documents") or []
        ranked = sorted(
            ((i, float(str(d).count(q)) + 0.1) for i, d in enumerate(docs)),
            key=lambda x: -x[1])
        if type(self).top_n_only:
            ranked = ranked[:max(1, len(ranked) // 2)]
        bucket, score_field = type(self).field_names
        items = [{"index": i, score_field: s} for i, s in ranked]
        out = json.dumps({bucket: items}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


@pytest.fixture()
def stub():
    srv = HTTPServer(("127.0.0.1", 0), _RerankStub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _RerankStub.calls = []
    _RerankStub.path_prefix = "/rerank"
    _RerankStub.field_names = ("results", "relevance_score")
    _RerankStub.top_n_only = False
    _RerankStub.fail_code = 0
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def _reranker(base, **kw):
    from rag.models.rerank import HttpReranker

    return HttpReranker(base_url=base, api_key="secret-key",
                        model="bge-reranker", top_n=8, timeout=5, **kw)


# ---- 基本契约 ----------------------------------------------------------


def test_sends_expected_request(stub):
    r = _reranker(stub)
    r.score("逆变器", ["逆变器参数", "无关内容", "逆变器手册"])
    call = _RerankStub.calls[-1]
    assert call["path"] == "/rerank"
    assert call["auth"] == "Bearer secret-key", "未携带 api_key"
    assert call["body"]["query"] == "逆变器"
    assert call["body"]["documents"] == ["逆变器参数", "无关内容", "逆变器手册"]
    assert call["body"]["model"] == "bge-reranker"


def test_scores_align_with_documents(stub):
    """返回的分数必须与入参文档一一对应（按下标回填）。"""
    r = _reranker(stub)
    scores = r.score("逆变器", ["逆变器A", "无", "逆变器B"])
    assert len(scores) == 3
    assert scores[0] > scores[1], "命中多的应得高分"
    assert scores[2] > scores[1]


def test_rerank_returns_dataframe_sorted(stub):
    """精排要真的改变顺序——这是它的全部意义。"""
    import pandas as pd

    r = _reranker(stub)
    df = pd.DataFrame({"text": ["无关内容", "逆变器手册", "逆变器参数"],
                       "id": [0, 1, 2]})
    out = r.rerank("逆变器", df)
    assert "_relevance_score" in out
    assert out["id"].tolist()[0] in (1, 2), f"排序未生效: {out['id'].tolist()}"
    assert out["_relevance_score"].tolist() == sorted(
        out["_relevance_score"].tolist(), reverse=True)


def test_top_n_partial_results_backfilled(stub):
    """服务只返回 top_n 条时，未返回的按下标补 0，长度保持一致。"""
    _RerankStub.top_n_only = True
    r = _reranker(stub)
    scores = r.score("逆变器", ["逆变器A", "无", "无", "无"])
    assert len(scores) == 4
    assert scores[0] > 0
    assert sum(1 for s in scores if s == 0) >= 1


# ---- 端点与字段兼容 ----------------------------------------------------


def test_probes_v1_prefix(stub):
    """/rerank 不存在时自动改试 /v1/rerank。"""
    _RerankStub.path_prefix = "/v1/rerank"
    r = _reranker(stub)
    scores = r.score("逆变器", ["逆变器A", "无"])
    assert scores[0] > scores[1]
    assert [c["path"] for c in _RerankStub.calls] == [
        "/rerank", "/v1/rerank"], "未按预期探测前缀"


def test_prefix_cached_after_probe(stub):
    """探测成功后要缓存，避免每次检索都多打一次 404。"""
    _RerankStub.path_prefix = "/v1/rerank"
    r = _reranker(stub)
    r.score("q", ["a"])
    n_before = len(_RerankStub.calls)
    r.score("q", ["a"])
    assert len(_RerankStub.calls) - n_before == 1, "前缀未缓存"


def test_accepts_data_and_score_naming(stub):
    """响应字段命名兼容：data/score 与 results/relevance_score 等价。"""
    _RerankStub.field_names = ("data", "score")
    r = _reranker(stub)
    scores = r.score("逆变器", ["逆变器A", "无"])
    assert scores[0] > scores[1]


def test_unrecognized_payload_raises(stub):
    """响应格式无法识别时要报明确错误，而不是静默返回全 0。"""
    from rag.models.rerank import RerankUnavailable

    class Bad(_RerankStub):
        def do_POST(self):
            out = b'{"unexpected": 1}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

    srv = HTTPServer(("127.0.0.1", 0), Bad)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with pytest.raises(RerankUnavailable, match="无法识别"):
            _reranker(f"http://127.0.0.1:{srv.server_port}").score("q", ["a"])
    finally:
        srv.shutdown()


def test_missing_base_url_is_reported():
    from rag.models.rerank import HttpReranker, RerankUnavailable

    with pytest.raises(RerankUnavailable, match="base_url"):
        HttpReranker(base_url="")


# ---- 构造与降级 --------------------------------------------------------


def test_build_reranker_disabled_returns_none():
    from rag.core.config import RerankConfig
    from rag.models.rerank import build_reranker

    assert build_reranker(RerankConfig(enabled=False)) is None


def test_build_reranker_api_provider(stub):
    from rag.core.config import RerankConfig
    from rag.models.rerank import HttpReranker, build_reranker

    r = build_reranker(RerankConfig(
        enabled=True, provider="api", base_url=stub,
        api_key="k", model="m"))
    assert isinstance(r, HttpReranker)


def test_build_reranker_api_without_url_degrades():
    """配置缺失不该让服务起不来——rerank 是可选增强。"""
    from rag.core.config import RerankConfig
    from rag.models.rerank import build_reranker

    assert build_reranker(RerankConfig(
        enabled=True, provider="api", base_url="")) is None


def test_rerank_config_has_credentials_fields():
    """回归防护：RerankConfig 曾完全没有 base_url / api_key 字段，
    拿到服务凭据也无处可填。"""
    from rag.core.config import RerankConfig

    fields = RerankConfig.model_fields
    for name in ("base_url", "api_key", "model", "timeout", "top_n"):
        assert name in fields, f"RerankConfig 缺少 {name}"
