"""API 密钥与鉴权流程的回归测试。

覆盖三条容易出错的路径：
- 密钥生命周期：签发 → 校验 → 吊销 → 过期；
- 权限作用域：read 不能写、不能管理密钥（防自我提权）；
- 引导流程：零密钥时可自助签发第一把，之后立即强制鉴权
  （避免「创建第一把密钥后反而把自己锁在门外」）。
"""
from __future__ import annotations

import pytest

from rag.storage.repos import keys as apikeys


class _StubEmbedder:
    dim = 4
    model = "stub"

    def embed_one(self, text):  # noqa: ARG002
        return [1.0, 0.0, 0.0, 0.0]

    def embed(self, texts):
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


@pytest.fixture()
def env(tmp_path):
    """带桩 embedder 的测试环境。"""
    from fastapi.testclient import TestClient

    from rag.api import Ctx, create_app
    from rag.core.config import Settings
    from rag.models.registry import ModelRegistry
    from rag.storage.tables import LanceStore

    store = LanceStore.for_data_dir(tmp_path, 4)
    settings = Settings()
    settings.data_dir = tmp_path
    settings.embed.dim = 4
    registry = ModelRegistry(settings)
    registry.bundle.embedder = _StubEmbedder()
    client = TestClient(create_app(Ctx(settings, store, registry)),
                        raise_server_exceptions=False)
    client.store = store          # type: ignore[attr-defined]
    client.settings = settings    # type: ignore[attr-defined]
    return client


@pytest.fixture()
def cors_env(tmp_path):
    """带 CORS 的环境。

    必须在建 app **之前** 设好 cors_origins：CORSMiddleware 是应用装配期
    根据它决定加不加的，事后改 settings 不会生效。
    """
    from fastapi.testclient import TestClient

    from rag.api import Ctx, create_app
    from rag.core.config import Settings
    from rag.models.registry import ModelRegistry
    from rag.storage.tables import LanceStore

    store = LanceStore.for_data_dir(tmp_path, 4)
    settings = Settings()
    settings.data_dir = tmp_path
    settings.embed.dim = 4
    settings.cors_origins = ["http://app.example"]
    settings.token = "legacy"
    registry = ModelRegistry(settings)
    registry.bundle.embedder = _StubEmbedder()
    client = TestClient(create_app(Ctx(settings, store, registry)),
                        raise_server_exceptions=False)
    client.store = store       # type: ignore[attr-defined]
    client.settings = settings  # type: ignore[attr-defined]
    return client


def _h(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def _mk(env, scope="write", name="k"):
    """直接在存储层签发一把密钥（绕过「引导期无鉴权」）。"""
    return apikeys.create_key(env.store, name, scope=scope)[1]


# ---- 密钥生成与存储 ----------------------------------------------------


def test_generated_secret_format():
    """密钥形如 rg_<8位公开段>_<32位随机段>，便于人工辨认。"""
    s = apikeys.generate_secret()
    assert s.startswith("rg_")
    parts = s.split("_")
    assert len(parts) == 3, f"格式应为 rg_public_secret: {s}"
    assert len(parts[1]) == apikeys.PUBLIC_LEN
    assert len(parts[2]) == apikeys.SECRET_LEN


def test_secrets_are_unique():
    assert len({apikeys.generate_secret() for _ in range(50)}) == 50


def test_plaintext_never_stored(env):
    """库里只存 sha256 哈希，不含明文——备份泄露不等于凭据泄露。"""
    row, secret = apikeys.create_key(env.store, "测试", scope="read")
    assert row["key_hash"] != secret
    assert secret not in row["key_hash"]
    stored = env.store.apikeys.search().to_list()
    blob = str(stored)
    assert secret not in blob, "明文密钥被写入了存储"
    assert row["prefix"] in blob, "公开段应保留以便辨认"


def test_list_never_exposes_hash(env):
    """列表接口不应有能力还原凭据。"""
    apikeys.create_key(env.store, "a", scope="read")
    items = apikeys.list_keys(env.store)
    assert items and all("key_hash" not in i for i in items)


def test_default_scope_is_read(env):
    """签发即最小权限。"""
    row, _ = apikeys.create_key(env.store, "a")
    assert row["scope"] == "read"


def test_invalid_scope_rejected(env):
    with pytest.raises(ValueError, match="未知权限"):
        apikeys.create_key(env.store, "a", scope="admin")


def test_empty_name_rejected(env):
    with pytest.raises(ValueError, match="名称"):
        apikeys.create_key(env.store, "  ")


def test_non_positive_expiry_rejected(env):
    with pytest.raises(ValueError, match="有效期"):
        apikeys.create_key(env.store, "a", expires_in_days=0)


# ---- 校验 --------------------------------------------------------------


def test_verify_accepts_correct_key(env):
    _, secret = apikeys.create_key(env.store, "a", scope="read")
    row = apikeys.verify(env.store, secret)
    assert row["scope"] == "read"


@pytest.mark.parametrize("bad", ["", "wrong", "rg_xxx", "abc_def"])
def test_verify_rejects_wrong_key(env, bad):
    apikeys.create_key(env.store, "a")
    with pytest.raises(apikeys.ApiKeyAuthError):
        apikeys.verify(env.store, bad)


def test_verify_rejects_revoked(env):
    _, secret = apikeys.create_key(env.store, "a")
    apikeys.revoke(env.store,
                   apikeys.list_keys(env.store)[0]["key_id"])
    with pytest.raises(apikeys.ApiKeyAuthError, match="吊销"):
        apikeys.verify(env.store, secret)


def test_verify_rejects_expired(env):
    row, secret = apikeys.create_key(env.store, "a")
    env.store.apikeys.update(
        where=f"key_id = '{row['key_id']}'",
        values={"expires_at": "2000-01-01T00:00:00+00:00"})
    with pytest.raises(apikeys.ApiKeyAuthError, match="过期"):
        apikeys.verify(env.store, secret)


def test_expiry_in_future_is_accepted(env):
    _, secret = apikeys.create_key(env.store, "a", expires_in_days=30)
    assert apikeys.verify(env.store, secret)


def test_revoke_is_idempotent(env):
    row, _ = apikeys.create_key(env.store, "a")
    assert apikeys.revoke(env.store, row["key_id"]) is True
    assert apikeys.revoke(env.store, row["key_id"]) is True


def test_revoke_unknown_returns_false(env):
    assert apikeys.revoke(env.store, "nope") is False


def test_key_state_derivation(env):
    row, _ = apikeys.create_key(env.store, "a")
    assert apikeys.key_state({"revoked_at": None, "expires_at": ""}) == "active"
    assert apikeys.key_state(
        {"revoked_at": "2020-01-01", "expires_at": ""}) == "revoked"
    assert apikeys.key_state(
        {"revoked_at": None,
         "expires_at": "2000-01-01T00:00:00+00:00"}) == "expired"


def test_scope_allows_matrix():
    assert apikeys.scope_allows("write", need_write=True)
    assert apikeys.scope_allows("write", need_write=False)
    assert apikeys.scope_allows("read", need_write=False)
    assert not apikeys.scope_allows("read", need_write=True)


# ---- HTTP 鉴权流程 -----------------------------------------------------


def test_no_auth_when_no_keys_and_no_token(env):
    """单机自用的零摩擦体验：什么都没配时不拦截。"""
    assert env.get("/api/health").status_code == 200


def test_bootstrap_allows_listing_and_first_key(env):
    """引导期：零密钥时可读空列表并签发第一把。"""
    assert env.get("/api/keys").json()["total"] == 0
    r = env.post("/api/keys", json={"name": "第一把", "scope": "read"})
    assert r.status_code == 200
    assert r.json()["key"].startswith("rg_")


def test_auth_required_once_key_exists(env):
    """有密钥后立刻强制鉴权。"""
    apikeys.create_key(env.store, "a", scope="read")
    r = env.get("/api/health")
    assert r.status_code == 401
    # 关键：401 必须带可读的 detail，而不是裸 500
    assert "detail" in r.json()
    assert "凭据" in r.json()["detail"]


def test_middleware_auth_error_has_json_body(env):
    """回归防护：中间件里 raise HTTPException 会退化成裸 500。

    Starlette 的 ExceptionMiddleware 在中间件栈内层，中间件抛出的
    HTTPException 不会被它接住，最终返回无正文的 Internal Server Error，
    调用方完全看不出鉴权失败的原因。
    """
    apikeys.create_key(env.store, "a", scope="read")
    r = env.get("/api/health")
    assert r.status_code == 401
    assert r.headers.get("content-type", "").startswith("application/json")
    assert "Internal Server Error" not in r.text


def test_x_api_key_header_equivalent(env):
    key = _mk(env, "read")
    assert env.get("/api/health", headers={"X-API-Key": key}).status_code == 200


def test_read_scope_cannot_write(env):
    key = _mk(env, "read")
    r = env.post("/api/documents/text",
                 json={"text": "x", "title": "t"}, headers=_h(key))
    assert r.status_code == 403
    assert "read" in r.json()["detail"]


def test_read_scope_cannot_manage_keys(env):
    """防自我提权：read 密钥不能签发或吊销密钥。"""
    key = _mk(env, "read")
    admin = _mk(env, "write")
    kid = [i for i in env.get("/api/keys", headers=_h(admin)).json()["items"]
           if i["scope"] == "read"][0]["key_id"]
    r = env.post("/api/keys", json={"name": "提权"}, headers=_h(key))
    assert r.status_code == 403
    assert env.delete(f"/api/keys/{kid}", headers=_h(key)).status_code == 403


def test_read_scope_can_list_keys(env):
    """read 密钥可查看密钥列表，但不能签发 / 吊销。

    回归防护：若列举也要 write，用户签发第一把密钥后浏览器会立刻
    失去查看密钥页的权限——而它手里只有刚创建的那把，构成自锁。
    列举拿不到明文，不构成提权。
    """
    key = _mk(env, "read")
    r = env.get("/api/keys", headers=_h(key))
    assert r.status_code == 200
    assert all("key_hash" not in i for i in r.json()["items"])


def test_write_scope_can_manage_and_write(env):
    key = _mk(env, "write")
    assert env.get("/api/keys", headers=_h(key)).status_code == 200
    assert env.post("/api/documents/text", json={"text": "hello", "title": "t"},
                    headers=_h(key)).status_code == 200


def test_last_used_at_recorded(env):
    key = _mk(env, "read")
    env.get("/api/health", headers=_h(key))
    items = env.get("/api/keys", headers=_h(_mk(env, "write"))).json()["items"]
    used = [i for i in items if i["last_used_at"]]
    assert used, "last_used_at 未被记录"


def test_revoke_takes_effect_immediately(env):
    key = _mk(env, "read")
    admin = _mk(env, "write")
    # 直接按 key_id 找，别依赖 last_used_at 之类的间接信号
    kid = [i for i in env.get("/api/keys", headers=_h(admin)).json()["items"]
           if i["scope"] == "read"][0]["key_id"]
    assert env.get("/api/health", headers=_h(key)).status_code == 200
    assert env.delete(f"/api/keys/{kid}", headers=_h(admin)).status_code == 200
    assert env.get("/api/health", headers=_h(key)).status_code == 401
    # 行保留以便审计，且状态标记为已吊销
    row = [i for i in env.get("/api/keys", headers=_h(admin)).json()["items"]
           if i["key_id"] == kid][0]
    assert row["state"] == "revoked"
    assert row["revoked_at"] is not None


def test_revoke_unknown_key_404(env):
    admin = _mk(env, "write")
    assert env.delete("/api/keys/nope", headers=_h(admin)).status_code == 404


def test_legacy_token_still_works(env):
    """兼容既有部署：RAG_TOKEN 仍是 write 全权限。"""
    env.settings.token = "legacy-secret"
    assert env.get("/api/health",
                   headers={"Authorization": "Bearer legacy-secret"}
                   ).status_code == 200
    assert env.get("/api/health").status_code == 401


def test_static_assets_not_protected(env):
    apikeys.create_key(env.store, "a", scope="read")
    assert env.get("/").status_code == 200


def test_cors_preflight_passes_auth(cors_env):
    """预检不带凭据，若被拦则浏览器端跨域调用全挂。"""
    r = cors_env.options("/api/search", headers={
        "Origin": "http://app.example",
        "Access-Control-Request-Method": "POST",
    })
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "http://app.example"


def test_cross_origin_request_still_authenticated(cors_env):
    """跨域 POST 无凭据应得 401（说明鉴权生效而非被 CORS 吞掉）。"""
    r = cors_env.post("/api/search", json={"q": "x"},
                      headers={"Origin": "http://app.example"})
    assert r.status_code == 401


def test_wrong_token_rejected(env):
    env.settings.token = "right"
    assert env.get("/api/health",
                   headers={"Authorization": "Bearer wrong"}
                   ).status_code == 401