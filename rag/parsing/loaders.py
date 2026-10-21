"""解析器接入：Docling / PyMuPDF4LLM / Unstructured / native。

重型解析器为懒加载；缺失时在入库流水线中自动回退。

URL 入库（load_url）有两道安全闸：
- 协议白名单：只允许 http/https，挡掉 file:// 任意本地文件读取（S1）。
- 内网地址拦截：拒绝 loopback / private / link-local（含云元数据 169.254.x），
  否则可探测内网服务或窃取云凭据（S2）。
"""
from __future__ import annotations

import html.parser
import http.client
import ipaddress
import logging
import re
import socket
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from langchain_core.documents import Document

logger = logging.getLogger("raggi.parsing")

# URL 抓取上限（避免一个巨大页面把内存/索引撑爆）
URL_MAX_BYTES = 8 * 1024 * 1024
# URL 长度上限：超长 URL 既可能是攻击载荷，也常是调用方拼错
URL_MAX_CHARS = 4096
# 重定向跳数上限：urllib 默认 10；再短一点，避免重定向链拖住入库任务
URL_MAX_REDIRECTS = 5
ALLOWED_URL_SCHEMES = {"http", "https"}


def _load_docling(path: Path, ocr: bool) -> list[Document]:
    from langchain_docling import DoclingLoader

    loader = DoclingLoader(file_path=str(path))
    return loader.load()


def _load_pymupdf(path: Path) -> list[Document]:
    """PDF → Markdown（按页返回），直接调用 pymupdf4llm。

    不再走 `langchain_community.document_loaders.PyMuPDF4LLMLoader`：
    langchain-community 0.4 起已移除该符号（该包正在 sunset），
    继续引用会让**所有 PDF 解析全部失败**并回退到同样失败的 native，
    最终上传直接 500。pymupdf4llm 本身是核心依赖，直接用更稳。

    一次 page_chunks=True 调用即可拿到逐页文本；列表按页序排列，
    下标 +1 就是页码，由此建立页码映射（检索结果才能显示「第 N 页」）。
    """
    import pymupdf4llm

    try:
        chunks = pymupdf4llm.to_markdown(
            str(path), page_chunks=True, show_progress=False) or []
    except Exception as e:  # noqa: BLE001
        logger.warning("pymupdf4llm 解析失败: %s", e)
        raise

    out: list[Document] = []
    for i, ch in enumerate(chunks):
        text = (ch.get("text") if isinstance(ch, dict) else str(ch)) or ""
        if not text.strip():
            continue          # 空白页不产出 Document，避免空分块
        out.append(Document(page_content=text,
                            metadata={"source": str(path), "page": i + 1}))
    # 整本无文本（典型是扫描件）→ 返回空，由路由层报出可读原因
    return out


def _load_unstructured(path: Path) -> list[Document]:
    from langchain_unstructured import UnstructuredLoader

    return UnstructuredLoader(file_path=str(path), mode="elements", strategy="hi_res").load()


def _load_native(path: Path) -> list[Document]:
    ext = path.suffix.lower()
    if ext in (".md", ".markdown", ".txt"):
        return [
            Document(
                page_content=path.read_text(encoding="utf-8", errors="ignore"),
                metadata={"source": str(path)},
            )
        ]
    # 无重型依赖时退化为 PyMuPDF4LLM（核心依赖）
    return _load_pymupdf(path)


ENGINES = {
    "docling": _load_docling,
    "pymupdf4llm": _load_pymupdf,
    "unstructured": _load_unstructured,
    "native": _load_native,
}


def load_file(path: Path, engine: str, ocr: bool = False) -> tuple[str, list[Document]]:
    loader = ENGINES.get(engine, _load_native)
    try:
        docs = loader(path, ocr) if engine == "docling" else loader(path)
        return engine, docs
    except Exception as e:  # noqa: BLE001
        logger.warning("%s 解析失败: %s", engine, e)
        raise


def load_text(text: str, title: str) -> tuple[str, list[Document]]:
    return "native", [Document(page_content=text, metadata={"source": title})]


def _check_url(url: str) -> None:
    """URL 入库前的安全校验：协议白名单 + 内网地址拦截。

    这是**每一个**出网地址（初始 URL 与每次重定向）都要过的闸。
    只校验初始地址是不够的：公开 URL 完全可以 302 到
    ``http://169.254.169.254/`` 或内网管理端口，而 urllib 默认会自动
    跟随重定向——那就等于把 SSRF 防护整体绕过了。
    """
    if len(url) > URL_MAX_CHARS:
        raise ValueError(f"URL 过长（上限 {URL_MAX_CHARS} 字符）")
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ALLOWED_URL_SCHEMES:
        raise ValueError(
            f"URL 协议 {scheme or '(空)'} 不被允许，仅支持 "
            f"{'/'.join(sorted(ALLOWED_URL_SCHEMES))}")
    if parsed.username or parsed.password:
        # userinfo 会让日志/错误信息与实际连接目标产生歧义，且常见的
        # `http://public@127.0.0.1` 正是绕过朴素 host 判断的手法之一。
        raise ValueError("URL 不允许携带用户名或密码")
    host = parsed.hostname
    if not host:
        raise ValueError("URL 缺少主机名")
    try:
        parsed.port  # noqa: B018 —— 非法端口在这里抛 ValueError
    except ValueError as e:
        raise ValueError(f"URL 端口不合法：{e}") from e

    # 解析 DNS 后逐个校验 IP，堵住「域名指向内网」的常见形态；
    # 解析失败时不静默放行（宁可拒绝一次合法抓取）。
    # 连接层还会用同一份解析结果（见 _Pinned*），不再二次解析。
    _resolve_public_ips(host)


def _assert_public_ip(host: str, raw_ip: str) -> None:
    try:
        ip = ipaddress.ip_address(raw_ip)
    except ValueError as e:
        raise ValueError(f"主机 {host} 解析出非法地址 {raw_ip!r}") from e
    if (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
        raise ValueError(
            f"禁止抓取内网/保留地址：{host} → {ip}。"
            "如确需抓取内网服务，请直接上传文件或改用本机解析器")


def _resolve_public_ips(host: str, port: int | None = None) -> list[str]:
    """解析主机并返回**全部**公网 IP（去重、保序）。

    这是 URL 校验与连接共用的唯一 DNS 解析点：连接时把这份结果钉到
    socket 上，避免“校验时是公网、connect 时变内网”的 DNS rebinding。
    任一条解析结果是私网就整体拒绝（宁可拒绝一次合法抓取）。
    """
    try:
        infos = socket.getaddrinfo(
            host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise ValueError(f"无法解析主机 {host}: {e}") from e
    out: list[str] = []
    for info in infos:
        raw = str(info[4][0]).split("%", 1)[0]
        _assert_public_ip(host, raw)
        if raw not in out:
            out.append(raw)
    if not out:
        raise ValueError(f"主机 {host} 没有可用地址")
    return out


def _hostname_of(host: str) -> str:
    """`example.com:8080` → `example.com`（连接类拿到的是 host:port）。"""
    parsed = urlparse("//" + host)
    return parsed.hostname or host


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """把 HTTP 连接固定到校验过的 IP，同时保留原 Host 头。"""

    def __init__(self, host: str, ips: list[str], **kwargs):
        super().__init__(host, **kwargs)
        self._pinned_ips = ips

    def connect(self) -> None:
        last: OSError | None = None
        for ip in self._pinned_ips:
            try:
                self.sock = socket.create_connection(
                    (ip, self.port), self.timeout, self.source_address)
                return
            except OSError as e:  # 尝试下一个已校验的地址
                last = e
        raise last or OSError("没有可连接的公网地址")


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS 版本：固定 IP 连接，但 SNI / 证书仍按原主机名校验。"""

    def __init__(self, host: str, ips: list[str], **kwargs):
        super().__init__(host, **kwargs)
        self._pinned_ips = ips

    def connect(self) -> None:
        last: OSError | None = None
        for ip in self._pinned_ips:
            sock = None
            try:
                sock = socket.create_connection(
                    (ip, self.port), self.timeout, self.source_address)
                # server_hostname 用原主机名：证书与 SNI 不能因 IP pinning 变成 IP
                self.sock = self._context.wrap_socket(
                    sock, server_hostname=_hostname_of(self.host))
                return
            except OSError as e:
                last = e
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
        raise last or OSError("没有可连接的公网地址")


class _PinnedHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        def factory(host, **kwargs):
            return _PinnedHTTPConnection(
                host, _resolve_public_ips(_hostname_of(host)), **kwargs)
        return self.do_open(factory, req)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        def factory(host, **kwargs):
            return _PinnedHTTPSConnection(
                host, _resolve_public_ips(_hostname_of(host)), **kwargs)
        return self.do_open(factory, req)


def _check_connected_peer(response) -> None:
    """校验**实际连上的**对端地址，而不只是解析结果。

    URL 校验与真正的 connect 之间存在 DNS rebinding 窗口：攻击者可以让
    域名第一次解析到公网、连接时解析到 127.0.0.1。urllib 没有把解析
    结果钉住，所以这里在连接建立后再读一次内核告诉我们的对端 IP。
    读不到（不同 Python/SSL 实现的内部结构差异）就跳过——这是纵深
    防御，不把兼容性赌在私有属性上。
    """
    try:
        peer = response.fp.raw._sock.getpeername()[0]
    except Exception:  # noqa: BLE001
        return
    _assert_public_ip("实际连接", str(peer).split("%", 1)[0])


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """每一次重定向都重新走 `_check_url`。

    urllib 默认的 `HTTPRedirectHandler` 只校验新地址是不是 HTTP(S)，
    不校验主机是否属于内网。攻击者用一个公开域名 302 到云元数据地址
    即可绕过 entry URL 的检查。
    """

    max_redirections = URL_MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        _check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# 显式清空代理：环境变量里的 HTTP(S)_PROXY 会让默认 ProxyHandler
# 抢在 pinned handler 前面——连接目标变成代理，IP 固定就失效了。
_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    _SafeRedirectHandler(),
    _PinnedHTTPHandler(),
    _PinnedHTTPSHandler(),
)


class _TextExtractor(html.parser.HTMLParser):
    """极简 HTML → 正文抽取（stdlib，无第三方依赖）。

    丢弃 script/style/nav/header/footer 等非正文节点，避免其中的密钥、
    内网地址随页面一起进入索引（B4）。
    """

    SKIP = {"script", "style", "noscript", "template", "svg", "canvas",
            "iframe", "head", "nav", "header", "footer", "aside", "form"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5",
             "h6", "section", "article", "blockquote", "pre", "table"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        elif tag in self.SKIP:
            self._skip_depth += 1
        elif tag in self.BLOCK:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag in self.SKIP and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag in self.BLOCK:
            self._parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data.strip()
            return
        if self._skip_depth == 0 and data.strip():
            self._parts.append(data)

    def text(self) -> str:
        joined = "".join(self._parts)
        joined = re.sub(r"[ \t\r\f\v]+", " ", joined)
        joined = re.sub(r"\n\s*\n\s*\n+", "\n\n", joined)
        return joined.strip()


def extract_html_text(raw: str) -> tuple[str, str]:
    """返回 (正文, 标题)；抽取不出正文时回退为原样文本。"""
    parser = _TextExtractor()
    try:
        parser.feed(raw)
        parser.close()
    except Exception as e:  # noqa: BLE001
        logger.warning("HTML 抽取失败，回退原样: %s", e)
        return raw.strip(), ""
    text = parser.text()
    return (text, parser.title) if text else (raw.strip(), parser.title)


def load_url(url: str) -> tuple[str, list[Document]]:
    """抓取 URL 并抽正文入库。

    只允许 http/https 且不得指向内网/保留地址——否则等于给未鉴权的 API
    开一个「读服务器文件」或「扫内网」的口子（S1/S2）。重定向链上的
    每一跳同样要过闸，否则公开 URL 可以 302 到内网；最终地址再核一次，
    防自定义 handler 被绕过或服务端返回相对跳转的边角情形。
    """
    _check_url(url)
    req = urllib.request.Request(url, headers={  # noqa: S310
        "User-Agent": "Raggi/0.2 (RAG document ingester)",
        "Accept": "text/html,text/plain;q=0.9,*/*;q=0.5",
    })
    with _OPENER.open(req, timeout=30) as r:  # noqa: S310
        final_url = r.geturl() or url
        _check_url(final_url)
        _check_connected_peer(r)
        content_type = (r.headers.get("Content-Type") or "").lower()
        charset = r.headers.get_content_charset() or "utf-8"
        data = r.read(URL_MAX_BYTES + 1)
    if len(data) > URL_MAX_BYTES:
        raise ValueError(
            f"页面超过上限 {URL_MAX_BYTES // (1024 * 1024)}MB，已中止抓取")
    try:
        raw = data.decode(charset, errors="ignore")
    except LookupError:
        # 服务端声明了不存在的 charset：退回 utf-8，而不是让整次入库失败
        raw = data.decode("utf-8", errors="ignore")

    title = url
    if "html" in content_type or raw.lstrip()[:200].lower().startswith(
            ("<!doctype html", "<html")):
        raw, page_title = extract_html_text(raw)
        title = page_title or url

    return "native", [Document(page_content=raw,
                               metadata={"source": final_url, "title": title})]