"""原文下载的签名 URL。

**为什么需要它**：浏览器预览原文要用 `<iframe>` / `<img>` / `<embed>`，
这些标签无法携带 `Authorization` 头——鉴权一旦开启，直接放
`/api/documents/{id}/file` 到 src 里必然 401。

解决方案是标准的「签名 URL」：把过期时间与 HMAC 签名放进查询串，
服务端校验签名而非请求头。这样：

- 链接短期有效（默认 10 分钟），过期即失效；
- 签名与 doc_id 绑定，不能拿 A 的签名去读 B；
- 密钥是进程本地的，不泄露到前端；
- 未启用鉴权时退化为无签名直链（保持单机自用的零摩擦）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import time
from pathlib import Path

logger = logging.getLogger("raggi.files")

# 签名有效期（秒）。
#
# 取 1 小时而非更短：这个链接是嵌进 <iframe> 里给人**阅读**用的，
# 读一份 PDF 动辄十几分钟。若只有 10 分钟，用户读到一半点右侧分块，
# iframe 会带着过期签名重新加载，直接变成 403 空白页——而用户完全
# 无从理解发生了什么。
#
# 安全上仍可接受：链接与单个 doc_id 绑定、需要已通过鉴权的调用方
# 先取到文档详情才能拿到，且不泄露长期凭据。真要更严可调小，
# 代价是长文档阅读中途失效。
DEFAULT_TTL = 3600

SECRET_FILE = ".url_secret"


def _load_or_create_secret(data_dir: Path) -> bytes:
    """读取（或首次生成）URL 签名密钥。

    落盘而非仅存内存：多 worker 部署时各进程要用同一把密钥，
    否则 A worker 签的 URL 在 B worker 上校验失败。
    """
    path = Path(data_dir) / SECRET_FILE
    try:
        if path.exists():
            raw = path.read_bytes().strip()
            if raw:
                return raw
    except OSError as e:  # noqa: BLE001
        logger.warning("读取 URL 密钥失败，本次使用临时密钥: %s", e)
    secret = base64.urlsafe_b64encode(secrets.token_bytes(32))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(secret)
        path.chmod(0o600)
    except OSError as e:  # noqa: BLE001
        logger.warning("URL 密钥落盘失败（重启后旧链接将失效）: %s", e)
    return secret


class UrlSigner:
    """HMAC 签名器：为 doc_id 生成带过期时间的短链。"""

    def __init__(self, data_dir: Path):
        self._secret = _load_or_create_secret(Path(data_dir))

    def _digest(self, doc_id: str, exp: int) -> str:
        msg = f"{doc_id}:{exp}".encode("utf-8")
        return hmac.new(self._secret, msg, hashlib.sha256).hexdigest()

    def sign(self, doc_id: str, ttl: int = DEFAULT_TTL) -> tuple[int, str]:
        """返回 (过期时间戳, 签名)。"""
        exp = int(time.time()) + max(1, int(ttl))
        return exp, self._digest(doc_id, exp)

    def verify(self, doc_id: str, exp: int, sig: str) -> bool:
        """常数时间校验签名与有效期。"""
        if not sig:
            return False
        try:
            exp_i = int(exp)
        except (TypeError, ValueError):
            return False
        if exp_i < int(time.time()):
            return False
        return hmac.compare_digest(sig, self._digest(doc_id, exp_i))

    def file_url(self, doc_id: str, *, auth_enabled: bool,
                 ttl: int = DEFAULT_TTL) -> str:
        """拼出可供 <iframe> 直接使用的 URL。

        指向规范版本 /api/v1（/api 是兼容别名，新链接不带旧前缀）。
        """
        base = f"/api/v1/documents/{doc_id}/file"
        if not auth_enabled:
            return base
        exp, sig = self.sign(doc_id, ttl)
        return f"{base}?exp={exp}&sig={sig}"


def safe_join(base: Path, name: str) -> Path | None:
    """把留档文件名安全地拼到 files_dir 下；越界返回 None。

    stored_file 由服务端写成 `{doc_id}{ext}`，正常不会越界；但它毕竟
    是从数据库读出的字符串，一旦写脏（历史数据、手工改库）就可能变成
    `../../etc/passwd`。这里拒绝路径分隔符与点开头，并校验 resolve 后
    仍在 base 之内（同时挡住符号链接跳出）。
    """
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    base = Path(base).resolve()
    candidate = (base / name).resolve()
    if candidate.parent == base:
        return candidate
    return None