"""解析器接入：Docling / PyMuPDF4LLM / Unstructured / native。

重型解析器为懒加载；缺失时在入库流水线中自动回退。

URL 入库（load_url）有两道安全闸：
- 协议白名单：只允许 http/https，挡掉 file:// 任意本地文件读取（S1）。
- 内网地址拦截：拒绝 loopback / private / link-local（含云元数据 169.254.x），
  否则可探测内网服务或窃取云凭据（S2）。
"""
from __future__ import annotations

import html.parser
import ipaddress
import logging
import re
import socket
from pathlib import Path
from urllib.parse import urlparse

from langchain_core.documents import Document

logger = logging.getLogger("raggi.parsing")

# URL 抓取上限（避免一个巨大页面把内存/索引撑爆）
URL_MAX_BYTES = 8 * 1024 * 1024
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
    """URL 入库前的安全校验：协议白名单 + 内网地址拦截。"""
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ALLOWED_URL_SCHEMES:
        raise ValueError(
            f"URL 协议 {scheme or '(空)'} 不被允许，仅支持 "
            f"{'/'.join(sorted(ALLOWED_URL_SCHEMES))}")
    host = parsed.hostname
    if not host:
        raise ValueError("URL 缺少主机名")

    # 解析 DNS 后逐个校验 IP，堵住「域名指向内网」与 DNS rebinding 的
    # 常见形态；解析失败时不静默放行（宁可拒绝一次合法抓取）。
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise ValueError(f"无法解析主机 {host}: {e}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
            raise ValueError(
                f"禁止抓取内网/保留地址：{host} → {ip}。"
                "如确需抓取内网服务，请直接上传文件或改用本机解析器")


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
    开一个「读服务器文件」或「扫内网」的口子（S1/S2）。
    """
    import urllib.request

    _check_url(url)
    req = urllib.request.Request(url, headers={  # noqa: S310
        "User-Agent": "Raggi/0.2 (RAG document ingester)",
        "Accept": "text/html,text/plain;q=0.9,*/*;q=0.5",
    })
    with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310
        content_type = (r.headers.get("Content-Type") or "").lower()
        data = r.read(URL_MAX_BYTES + 1)
    if len(data) > URL_MAX_BYTES:
        raise ValueError(
            f"页面超过上限 {URL_MAX_BYTES // (1024 * 1024)}MB，已中止抓取")
    raw = data.decode("utf-8", errors="ignore")

    title = url
    if "html" in content_type or raw.lstrip()[:200].lower().startswith(
            ("<!doctype html", "<html")):
        raw, page_title = extract_html_text(raw)
        title = page_title or url

    return "native", [Document(page_content=raw,
                               metadata={"source": url, "title": title})]