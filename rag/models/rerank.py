"""Rerank 接入：HTTP 精排（Jina / Cohere / vLLM / Xinference 通用）+ 本地交叉编码器。

**为什么要自己实现 HTTP 客户端**：`provider="api"` 分支早前直接
`return None`——等于没实现。拿到服务方给的 base_url + api_key 也无处可用。

**接口约定**（业界事实标准，多家一致）：

    POST {base_url}/rerank
    { "model": "...", "query": "...", "documents": ["...", ...], "top_n": N }
    → { "results": [ {"index": 0, "relevance_score": 0.93}, ... ] }

部分服务挂在 `/v1/rerank` 下（OpenAI 风格前缀），因此**自动探测**：
先试 `/rerank`，404 再试 `/v1/rerank`，并把命中的前缀缓存下来。
响应字段也做兼容（`results` / `data`、`relevance_score` / `score`）。

**失败策略**：精排不可用时**降级为不精排并标注原因**，不让整次检索失败——
召回结果本身仍然可用，只是没有精排。
"""
from __future__ import annotations

import json
import logging

import httpx

from rag.core.errors import Unavailable
from rag.models.http import CircuitBreaker, post_json

logger = logging.getLogger("raggi.rerank")

# 候选路径前缀：按常见程度排序，命中后缓存
_CANDIDATE_PATHS = ("", "/v1")


class RerankUnavailable(Unavailable):
    """精排服务不可用。检索层会捕获它并降级。"""


class HttpReranker:
    """HTTP 精排客户端。

    暴露 `rerank(query, df) -> df`，与 LanceDB 的 reranker 接口一致
    （`retrieval/search.py` 的 `_crossencoder_rerank` 就是这个形状）。
    """

    def __init__(self, *, base_url: str, api_key: str = "", model: str = "",
                 top_n: int = 8, timeout: int = 30):
        base = (base_url or "").strip().rstrip("/")
        if not base:
            raise RerankUnavailable("rerank 未配置 base_url")
        self.base_url = base
        self.api_key = (api_key or "").strip()
        self.model = (model or "").strip()
        self.top_n = max(1, int(top_n or 8))
        self.timeout = max(1, int(timeout or 30))
        # 探测结果缓存：避免每次检索都多打一次 404
        self._prefix: str | None = None
        # 精排是**可选增强**：远端挂了就停止打它，而不是每个请求再撞一次超时
        self._breaker = CircuitBreaker(threshold=3, cooldown_seconds=30.0)

    # ---- 对外接口 ----------------------------------------------------

    def rerank(self, query: str, df):
        """给候选文档打分并排序。

        入参/出参都是 pandas DataFrame（LanceDB 的约定）：
        入参需含 `text` 与 `id`，出参在原列基础上加 `_relevance_score`。
        """
        texts = [str(t) for t in df["text"].tolist()]
        if not texts:
            return df
        scores = self.score(query, texts)
        out = df.copy()
        out["_relevance_score"] = scores
        # 按分数降序：精排的意义就在于改变顺序
        return out.sort_values("_relevance_score", ascending=False) \
            .reset_index(drop=True)

    # ---- 打分 --------------------------------------------------------

    def score(self, query: str, documents: list[str]) -> list[float]:
        """返回与 documents 等长的相关性分数；服务返回不全时用 0 补齐。

        连接**复用进程级共享的 httpx 池**：原来是 `urllib.urlopen`，
        每个请求新建一条 TCP 连接（HTTPS 再付一次 TLS 握手）、没有
        keep-alive。并发下这部分成本是每请求的。
        """
        if not self._breaker.allow():
            raise RerankUnavailable("精排服务连续失败，已短路降级"
                                    "（稍后自动恢复尝试）")
        payload = {"query": query, "documents": documents,
                   "top_n": min(self.top_n, len(documents))}
        if self.model:
            payload["model"] = self.model

        prefixes = [self._prefix] if self._prefix is not None \
            else list(_CANDIDATE_PATHS)
        last_err: Exception | None = None
        for prefix in prefixes:
            url = f"{self.base_url}{prefix}/rerank"
            try:
                data = self._post(url, payload)
            except httpx.HTTPStatusError as e:
                last_err = e
                # 404 说明此路径不存在，换下一个；其余错误直接报出
                if e.response.status_code == 404 and self._prefix is None:
                    continue
                self._breaker.record_failure()
                raise RerankUnavailable(
                    f"rerank 服务返回 {e.response.status_code}: "
                    f"{_short(e)}") from e
            except Exception as e:  # noqa: BLE001
                self._breaker.record_failure()
                raise RerankUnavailable(
                    f"rerank 请求失败: {_short(e)}") from e
            self._prefix = prefix          # 记住可用前缀
            self._breaker.record_success()
            return _parse_scores(data, len(documents))

        raise RerankUnavailable(
            "rerank 服务未找到可用端点（已尝试 "
            + ", ".join(self.base_url + p + "/rerank"
                        for p in _CANDIDATE_PATHS)
            + f"）：{_short(last_err) if last_err else '未知错误'}")

    def _post(self, url: str, body: dict) -> dict:
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return post_json(url, body, headers=headers,
                         timeout=float(self.timeout))


def _short(exc: BaseException, limit: int = 200) -> str:
    msg = str(exc).strip() or exc.__class__.__name__
    return msg.splitlines()[0][:limit]


def _parse_scores(data: dict, n_docs: int) -> list[float]:
    """解析响应，兼容 results / data 与 relevance_score / score 两种命名。

    服务可能只返回 top_n 条（而非全部），因此按下标回填，
    未返回的位置给 0 分——调用方拿到的列表长度与入参一致。
    """
    items = data.get("results")
    if items is None:
        items = data.get("data")
    if not isinstance(items, list):
        raise RerankUnavailable(
            "rerank 响应格式无法识别（缺少 results/data）: "
            + json.dumps(data)[:200])

    scores = [0.0] * n_docs
    for it in items:
        if not isinstance(it, dict):
            continue
        idx = it.get("index")
        raw = it.get("relevance_score", it.get("score"))
        if idx is None or raw is None:
            continue
        try:
            i = int(idx)
            if 0 <= i < n_docs:
                scores[i] = float(raw)
        except (TypeError, ValueError):
            continue
    return scores


def build_reranker(cfg):
    """按配置构造精排器；未启用或配置不全时返回 None（用内置 RRF 融合）。

    注意：**不在这里抛异常**——rerank 是可选增强，配置缺失不该让服务起不来。
    真正的调用失败在检索时降级并标注。
    """
    if not getattr(cfg, "enabled", False):
        return None
    provider = (getattr(cfg, "provider", "none") or "none").lower()

    if provider == "none":
        return None

    if provider == "api":
        try:
            return HttpReranker(
                base_url=cfg.base_url, api_key=cfg.api_key,
                model=cfg.model, top_n=cfg.top_n, timeout=cfg.timeout)
        except Unavailable as e:
            logger.warning("rerank 未启用：%s", e)
            return None

    if provider == "cross-encoder":
        # 本地交叉编码器：需 sentence-transformers（可选依赖）
        try:
            from lancedb.rerankers import CrossEncoderReranker

            return CrossEncoderReranker(model_name=cfg.model,
                                        device=cfg.device)
        except Exception as e:  # noqa: BLE001
            logger.warning("本地 rerank 不可用（需 sentence-transformers）: %s", e)
            return None

    logger.warning("未知的 rerank provider: %r，已忽略", provider)
    return None
