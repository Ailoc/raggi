"""Embedding 接入：ollama / openai(兼容) / huggingface / custom。

统一批处理与归一化；失败退避重试（DESIGN §6.3）；
embed_one 结果缓存（同文本不重复调用模型）。

缓存**必须带锁**：检索与入库都在线程池里并发跑，而这里原来是一份裸 dict
——并发写会丢更新、`len() >= MAX` 的上界会被突破、淘汰用的还是
`pop(next(iter(cache)))`（插入序最旧，不是 LRU）。见 core/cache.py 的说明。
"""
from __future__ import annotations

import math
import time

from rag.core.cache import TTLCache
from rag.core.config import EmbedConfig

MAX_RETRIES = 3
BACKOFF_SECONDS = (0.5, 1.0, 2.0)
# embed_one 缓存上限与存活时间（秒）
ONE_CACHE_MAX = 512
ONE_CACHE_TTL_SECONDS = 600.0


def _l2(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v] if n else v


def _is_retryable(exc: Exception) -> bool:
    """网络/超时/限流/服务端错误可重试；认证类错误直接抛出。"""
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    msg = str(exc).lower()
    return any(k in msg for k in (
        "429", "500", "502", "503", "504", "timeout", "connection",
        "temporar", "rate limit", "too many requests"))


def build_lc_embeddings(cfg: EmbedConfig):
    """按 provider 构造 LangChain Embeddings 实例（统一 OpenAI 兼容协议）。"""
    if cfg.provider == "ollama":
        from langchain_ollama import OllamaEmbeddings

        return OllamaEmbeddings(
            model=cfg.model, base_url=cfg.base_url,
            # Ollama 客户端把 timeout 透传给 httpx；不传时用库默认，
            # 用户改 embed.timeout 会静默无效。
            client_kwargs={"timeout": cfg.timeout},
        )
    if cfg.provider in ("openai", "custom"):
        from langchain_openai import OpenAIEmbeddings

        return OpenAIEmbeddings(
            model=cfg.model,
            base_url=cfg.base_url,
            api_key=cfg.api_key or "EMPTY",
            check_embedding_ctx_length=False,
            dimensions=cfg.dim if cfg.provider == "openai" else None,
            request_timeout=cfg.timeout,
            # 重试只保留 Embedder 自己那一层：LangChain 默认还会再重试
            # 2 次，叠加后一次坏请求最多打 9 次远端（3×3），退避时间
            # 也会翻倍。重试策略必须只有一个来源。
            max_retries=0,
        )
    if cfg.provider == "huggingface":
        from langchain_huggingface import HuggingFaceEmbeddings

        return HuggingFaceEmbeddings(
            model_name=cfg.model,
            model_kwargs={"device": cfg.device},
            encode_kwargs={"normalize_embeddings": cfg.normalize},
        )
    raise ValueError(f"unknown embed provider: {cfg.provider}")


class EmbedUnavailable(RuntimeError):
    """embedding 服务不可用（已按退避策略重试仍失败）。

    API 层把它映射成 503 + 可读信息；裸抛底层异常会变成
    「Internal Server Error」，使用者完全不知道该去检查模型服务。
    """


class Embedder:
    def __init__(self, lc_embeddings, dim: int, model: str, normalize: bool,
                 cache=None):
        self.lc = lc_embeddings
        self.dim = dim
        self.model = model
        self.normalize = normalize
        # 向量随模型变化，所以键里带模型名：换了 embedding 模型后
        # 旧缓存必须失效，否则会拿到旧模型的向量。
        #
        # cache 由装配层注入：默认是进程内 TTLCache；配了 redis.url 时
        # 是 LayeredCache（L1 + 共享 L2），多副本之间共享查询向量。
        self._one_cache = cache or TTLCache(
            max_items=ONE_CACHE_MAX, ttl_seconds=ONE_CACHE_TTL_SECONDS)

    def embed(self, texts: list[str]) -> list[list[float]]:
        """同步批量向量化：失败退避重试（仅可重试错误）。"""
        last: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                vecs = self.lc.embed_documents(texts)
                return [_l2(v) for v in vecs] if self.normalize else vecs
            except Exception as e:  # noqa: BLE001
                last = e
                if attempt < MAX_RETRIES - 1 and _is_retryable(e):
                    time.sleep(BACKOFF_SECONDS[attempt])
                    continue
                break
        raise EmbedUnavailable(
            f"embedding 服务不可用（已重试 {MAX_RETRIES} 次）：{last}"
        ) from last

    def embed_one(self, text: str) -> list[float]:
        """单条向量化（带线程安全的 LRU 结果缓存 + in-flight 合并）。

        TTL 而非永久：热点查询重复命中是检索并发的主要成本来源，
        但永久缓存会让长跑进程的内存随查询数无界增长。

        ``get_or_load`` 让同一个 key 的并发请求只真正调用一次 provider：
        16 个并发的相同热点查询从 16 次远端往返降到 1 次，等待者拿到
        同一份结果。没有引入队列式合批的等待窗口——不同 key 互不阻塞。
        """
        key = f"{self.model}\x00{text}"
        vec, _age, _loaded = self._one_cache.get_or_load(
            key, lambda: self.embed([text])[0])
        return vec


def build_embedder(cfg: EmbedConfig, cache=None) -> Embedder:
    return Embedder(build_lc_embeddings(cfg), cfg.dim, cfg.model, cfg.normalize,
                    cache=cache)
