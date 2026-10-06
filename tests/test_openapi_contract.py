"""OpenAPI 契约守卫：响应模型 / 安全方案 / 分组 / 错误契约。

对应「API 契约专业化」：此前 47 个端点的 200 响应 schema 全是 `{}`
（客户端无法生成类型、/docs 的 Response 区空白），安全方案未声明
（没有 Authorize 入口），47 个端点平铺无分组，错误只声明了 422。

这些断言守护的是**对外契约**本身，而不是某个端点的行为——
行为有各自的测试，契约一旦退化（例如有人把 response_model 删掉，
或忘了给新端点加 tags），这里会先失败。
"""
from __future__ import annotations

import pathlib
import tempfile

import pytest


@pytest.fixture(scope="module")
def spec() -> dict:
    """离线构建应用并生成 OpenAPI（不需要真实数据）。"""
    import os

    os.environ.setdefault("RAG_DATA_DIR", tempfile.mkdtemp())
    os.environ["RAG_EMBED__DIM"] = "4"

    from rag.api import Ctx, create_app
    from rag.core.config import Settings
    from rag.models.registry import ModelRegistry
    from rag.storage.tables import LanceStore

    d = pathlib.Path(tempfile.mkdtemp())
    s = Settings()
    s.data_dir = d
    s.embed.dim = 4
    app = create_app(Ctx(s, LanceStore.for_data_dir(d, 4), ModelRegistry(s)))
    return app.openapi()


def _ops(spec: dict):
    """遍历 /api 下的全部操作。"""
    for path, ops in spec["paths"].items():
        if not path.startswith("/api"):
            continue
        for method, op in ops.items():
            if method in ("get", "post", "put", "patch", "delete"):
                yield method, path, op


# 只返回字节流 / 文本 / 事件流 的端点：没有 JSON 响应体可言
_NON_JSON = {
    ("get", "/api/v1/documents/{doc_id}/file"),
    ("post", "/api/v1/search/report"),
    ("post", "/api/v1/answer/stream"),
}


def test_every_api_operation_has_response_schema(spec):
    """除字节流与文本报告外，所有 200 响应必须声明 JSON schema。"""
    missing = []
    for m, p, op in _ops(spec):
        if (m, p) in _NON_JSON:
            continue
        schema = (op["responses"]["200"].get("content", {})
                  .get("application/json", {}).get("schema"))
        if not schema:
            missing.append(f"{m.upper()} {p}")
    assert not missing, f"这些端点缺少响应模型（OpenAPI 里会是 {{}}）: {missing}"


def test_non_json_endpoints_declare_media_type(spec):
    """字节流、文本报告与事件流要声明自己的媒体类型，而不是默认 application/json。"""
    file_op = spec["paths"]["/api/v1/documents/{doc_id}/file"]["get"]
    assert "application/octet-stream" in file_op["responses"]["200"]["content"]
    report_op = spec["paths"]["/api/v1/search/report"]["post"]
    assert "text/markdown" in report_op["responses"]["200"]["content"]
    sse_op = spec["paths"]["/api/v1/answer/stream"]["post"]
    assert "text/event-stream" in sse_op["responses"]["200"]["content"]


def test_security_scheme_declared(spec):
    """鉴权要在契约里可发现：两种等价凭据 + 每个操作都声明 security。

    回归防护：没有 securitySchemes，/docs 就没有 Authorize 按钮，
    生成的客户端也不知道把密钥放到哪里。
    """
    schemes = spec["components"].get("securitySchemes", {})
    assert "ApiKeyBearer" in schemes, "缺少 Bearer 安全方案"
    assert "ApiKeyHeader" in schemes, "缺少 X-API-Key 安全方案"
    for m, p, op in _ops(spec):
        assert op.get("security"), f"{m.upper()} {p} 未声明安全方案"


def test_operations_are_tagged(spec):
    """端点按资源分组，且只使用声明过的 tag。"""
    declared = {t["name"] for t in spec.get("tags", [])}
    expected = {"documents", "chunks", "knowledge-bases", "plans", "search",
                "answer", "models", "keys", "jobs", "system"}
    assert expected <= declared, f"缺少 tag 元数据: {expected - declared}"
    for m, p, op in _ops(spec):
        tags = op.get("tags")
        assert tags, f"{m.upper()} {p} 未分组"
        assert set(tags) <= declared, f"{m.upper()} {p} 使用了未声明的 tag"


def test_error_contract_declared(spec):
    """错误结构统一为 ErrorOut，且关键状态码出现在对应操作上。

    400/404/503 是这条 API 的主要失败面；401/403 对所有鉴权后的
    操作都成立。契约里缺了它们，调用方只能靠猜。
    """
    assert "ErrorOut" in spec["components"]["schemas"]
    assert spec["components"]["schemas"]["ErrorOut"]["properties"] == {
        "detail": {"type": "string", "title": "Detail"}}

    def codes(method: str, path: str) -> set[str]:
        return set(spec["paths"][path][method]["responses"].keys())

    list_op = codes("get", "/api/v1/kbs")
    assert {"401", "403"} <= list_op, f"列表端点缺少鉴权错误契约: {list_op}"
    item_op = codes("get", "/api/v1/kbs/{kb_id}")
    assert "404" in item_op, f"资源端点缺少 404 契约: {item_op}"
    search_op = codes("post", "/api/v1/search")
    assert {"503", "422"} <= search_op, f"检索缺少 503/422 契约: {search_op}"


def test_plan_global_field_alias(spec):
    """KbPlanOut 的 global 字段对外必须仍叫 "global"。

    回归防护：`global` 是 Python 关键字，模型里只能写成 global_；
    若忘记 alias，JSON 字段会漂移成 global_，前端读取 global 直接拿空。
    """
    props = spec["components"]["schemas"]["KbPlanOut"]["properties"]
    assert "global" in props and "global_" not in props


# ---- 运行时的错误行为与契约一致 -----------------------------------------

@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from rag.api import Ctx, create_app
    from rag.core.config import Settings
    from rag.models.registry import ModelRegistry
    from rag.storage.tables import LanceStore

    d = pathlib.Path(tempfile.mkdtemp())
    s = Settings()
    s.data_dir = d
    s.embed.dim = 4
    return TestClient(create_app(
        Ctx(s, LanceStore.for_data_dir(d, 4), ModelRegistry(s))),
        raise_server_exceptions=False)


def test_error_body_matches_contract(client):
    """错误响应体就是 {"detail": str}——契约不是愿望，是实际形状。"""
    r = client.get("/api/v1/documents/does-not-exist")
    assert r.status_code == 404
    body = r.json()
    assert set(body) == {"detail"} and isinstance(body["detail"], str)

    r = client.post("/api/v1/kbs", json={"name": "   "})
    assert r.status_code == 400
    assert set(r.json()) == {"detail"}


def test_validation_error_shape(client):
    """请求体校验失败（422）也返回 detail，调用方能统一处理。"""
    r = client.post("/api/v1/search", json={})
    assert r.status_code == 422
    assert "detail" in r.json()


# ---- 版本策略 -----------------------------------------------------------

def test_spec_declares_canonical_version_only(spec):
    """OpenAPI 只暴露规范版本 /api/v1；/api 是兼容别名，不进契约。

    回归防护：把别名也写进契约，客户端会生成两套重复方法，
    文档页则把每个接口列两遍。
    """
    for p in spec["paths"]:
        assert p.startswith("/api/v1/"), f"契约里出现非规范路径: {p}"


def test_legacy_prefix_is_an_equivalent_alias(client):
    """旧前缀 /api 与规范版本行为一致——这是不破坏现有调用方的关键。

    回归防护：版本化最常见的翻车方式是「加了 /api/v1 却忘了旧路径」，
    用户脚本与前端全部 404；或两条路由挂到不同实现上而行为漂移。
    """
    for path in ("/api/kbs", "/api/health", "/api/documents"):
        v1 = client.get(path.replace("/api", "/api/v1", 1))
        old = client.get(path)
        assert v1.status_code == old.status_code == 200, (
            f"GET {path}: v1={v1.status_code} alias={old.status_code}")
        a, b = v1.json(), old.json()
        # health 带检测时间戳，天然每次不同——它不属于「行为一致性」
        if isinstance(a, dict):
            a.pop("now", None)
            b.pop("now", None)
        assert a == b, f"GET {path} 两条路径行为不一致"


def test_alias_requires_auth_when_enabled():
    """鉴权在别名路径上同样生效——安全不能只覆盖规范路径。"""
    import tempfile as _tf
    from pathlib import Path as _P

    from fastapi.testclient import TestClient as _TC

    from rag.api import Ctx
    from rag.api import create_app as _ca
    from rag.core.config import Settings as _S
    from rag.models.registry import ModelRegistry as _MR
    from rag.storage.tables import LanceStore as _LS

    d = _P(_tf.mkdtemp())
    s = _S()
    s.data_dir = d
    s.token = "secret"
    c = _TC(_ca(Ctx(s, _LS.for_data_dir(d, 4), _MR(s))),
             raise_server_exceptions=False)
    assert c.get("/api/kbs").status_code == 401
    assert c.get("/api/v1/kbs").status_code == 401
    assert c.get("/api/v1/kbs",
                 headers={"Authorization": "Bearer secret"}).status_code == 200
