"""安全边界回归：SSRF 重定向、安全响应头、API key 过期损坏时 fail-closed。"""
from __future__ import annotations

from pathlib import Path

import pytest

from rag.parsing import loaders

ROOT = Path(__file__).resolve().parent.parent


def _redirect_handler():
    return loaders._SafeRedirectHandler()


def test_url_redirect_to_private_address_is_rejected():
    """公开 URL 302 到内网地址必须被拒绝，而不是跟随。

    回归防护：urllib 默认会自动跟随重定向且只检查 scheme；只校验入口
    URL 时，`public.example` → `http://169.254.169.254/` 可直接绕过 SSRF 防护。
    """
    handler = _redirect_handler()
    req = loaders.urllib.request.Request("https://example.com/doc")
    with pytest.raises(ValueError, match="内网|保留"):
        handler.redirect_request(
            req, None, 302, "Found", {}, "http://169.254.169.254/latest/meta-data")


def test_url_rejects_userinfo_and_invalid_port():
    for url in (
        "http://user@example.com/",
        "http://example.com:not-a-port/",
        "http://example.com:99999/",
    ):
        with pytest.raises(ValueError):
            loaders._check_url(url)


def test_url_rejects_overlong_input():
    with pytest.raises(ValueError, match="过长"):
        loaders._check_url("https://example.com/" + "a" * loaders.URL_MAX_CHARS)


def test_csp_and_security_headers_on_api(tmp_path):
    """所有 API 响应（包括错误路径）都要带安全头。"""
    from fastapi.testclient import TestClient

    from rag.api import Ctx, create_app
    from rag.core.config import Settings
    from rag.models.registry import ModelRegistry
    from rag.storage.tables import LanceStore

    settings = Settings()
    settings.data_dir = tmp_path
    settings.embed.dim = 4
    store = LanceStore.for_data_dir(tmp_path, 4)
    client = TestClient(create_app(Ctx(settings, store,
                                       ModelRegistry(settings))))
    for path in ("/api/health", "/api/does-not-exist"):
        r = client.get(path)
        assert r.headers["content-security-policy"].startswith("default-src 'self'")
        assert "script-src 'self'" in r.headers["content-security-policy"]
        assert "unsafe-inline" not in r.headers["content-security-policy"].split(
            "script-src")[1].split(";")[0]
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["x-frame-options"] == "SAMEORIGIN"
        assert r.headers["referrer-policy"] == "no-referrer"
        assert r.headers["cache-control"] == "no-store", \
            "API 响应不允许被浏览器/代理缓存"
        assert "style-src 'unsafe-inline'" not in r.headers["content-security-policy"]


def test_oversized_request_rejected_before_body_read(tmp_path):
    """声明超大 Content-Length 的请求应在读取正文前被 413 拒绝。"""
    from fastapi.testclient import TestClient

    from rag.api import Ctx, create_app
    from rag.core.config import Settings
    from rag.models.registry import ModelRegistry
    from rag.storage.tables import LanceStore

    settings = Settings()
    settings.data_dir = tmp_path
    settings.embed.dim = 4
    settings.max_upload_mb = 1
    store = LanceStore.for_data_dir(tmp_path, 4)
    client = TestClient(create_app(Ctx(settings, store,
                                       ModelRegistry(settings))))
    r = client.post("/api/documents/text", json={"text": "x"},
                    headers={"content-length": str(64 * 1024 * 1024)})
    assert r.status_code == 413
    assert "请求体超过上限" in r.json()["detail"]


def test_index_has_no_inline_script():
    """CSP 的 script-src 'self' 要求页面没有内联脚本。"""
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert "./theme-boot.js" in html
    scripts = [s for s in html.split("<script")[1:]]
    for tag in scripts:
        head = tag.split(">", 1)[0]
        assert "src=" in head, f"发现内联脚本: <script{head[:80]}>"


def test_url_loader_pins_validated_ip_and_preserves_host(monkeypatch):
    """连接必须发往校验过的 IP，而不是再次解析域名。

    用一个本地 HTTP 服务 + 假域名验证：DNS 解析被固定到 127.0.0.1，
    但请求的 Host 头仍是原域名（服务端能读到）。这证明 IP pinning
    没有把 Host 一起换掉，也证明连接走的是 pinned 类而不是 urllib 默认。
    """
    import http.server
    import threading

    seen_host = []
    body = b"<html><head><title>Pinned</title></head><body>hello</body></html>"

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            seen_host.append(self.headers.get("Host"))
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # noqa: D401, ANN002
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setattr(loaders, "_check_url", lambda url: None)
        monkeypatch.setattr(
            loaders, "_resolve_public_ips",
            lambda host, port=None: ["127.0.0.1"])
        monkeypatch.setattr(loaders, "_check_connected_peer", lambda r: None)
        engine, docs = loaders.load_url(f"http://pinned.example:{port}/")
        assert engine == "native"
        assert docs[0].metadata["title"] == "Pinned"
        assert "hello" in docs[0].page_content
        assert seen_host and seen_host[0] == f"pinned.example:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _client_with_upload_limit(tmp_path, mb=1):
    from fastapi.testclient import TestClient

    from rag.api import Ctx, create_app
    from rag.core.config import Settings
    from rag.models.registry import ModelRegistry
    from rag.storage.tables import LanceStore

    settings = Settings()
    settings.data_dir = tmp_path
    settings.embed.dim = 4
    settings.max_upload_mb = mb
    store = LanceStore.for_data_dir(tmp_path, 4)
    registry = ModelRegistry(settings)

    class _Stub:
        dim = 4
        model = "stub"

        def embed(self, texts):
            return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

        def embed_one(self, text):  # noqa: ARG002
            return [1.0, 0.0, 0.0, 0.0]

    registry.bundle.embedder = _Stub()
    return TestClient(create_app(Ctx(settings, store, registry)))


def test_chunked_body_over_limit_is_rejected(tmp_path):
    """没有 Content-Length 的 chunked 请求也不能绕过正文上限。"""
    client = _client_with_upload_limit(tmp_path, mb=1)
    block = b"x" * (12 * 1024 * 1024)

    def body():
        yield block
        yield block          # 24MB > 20MB 正文上限 + 2MB 余量

    r = client.post("/api/documents/text", content=body(),
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 413, f"{r.status_code}: {r.text[:200]}"
    assert "请求体超过上限" in r.json()["detail"]


def test_chunked_body_within_limit_still_works(tmp_path):
    """加硬上限后，正常的分块传输不能被误杀。"""
    import json

    client = _client_with_upload_limit(tmp_path, mb=1)
    payload = json.dumps({"text": "分块传输正文", "title": "chunked"}).encode()
    first, second = payload[:7], payload[7:]

    def body():
        yield first
        yield second

    r = client.post("/api/documents/text", content=body(),
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 200, f"{r.status_code}: {r.text[:200]}"
    assert r.json()["status"] in ("ready", "queued")


def test_index_html_uses_nonce_csp(tmp_path):
    """首页 CSP 用 per-request nonce 管 style 元素（不再整条放行 unsafe-inline）。"""
    dist = ROOT / "web" / "dist" / "index.html"
    if not dist.exists():
        pytest.skip("尚未构建（npm run build）")
    from fastapi.testclient import TestClient

    from rag.api import Ctx, create_app
    from rag.core.config import Settings
    from rag.models.registry import ModelRegistry
    from rag.storage.tables import LanceStore

    settings = Settings()
    settings.data_dir = tmp_path
    settings.embed.dim = 4
    store = LanceStore.for_data_dir(tmp_path, 4)
    client = TestClient(create_app(Ctx(settings, store,
                                       ModelRegistry(settings))))
    r = client.get("/")
    assert r.status_code == 200
    csp = r.headers["content-security-policy"]
    assert "style-src-elem 'self' 'nonce-" in csp
    assert "style-src 'unsafe-inline'" not in csp
    assert 'name="csp-nonce"' in r.text
    # 每次请求 nonce 必须不同，否则 nonce 就退化成静态常量
    r2 = client.get("/")
    assert r2.headers["content-security-policy"] != csp
