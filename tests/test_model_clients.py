"""模型客户端的配置透传：改 timeout 必须真的生效。"""
from __future__ import annotations

from rag.core.config import EmbedConfig, LLMConfig
from rag.models.chat import build_chat
from rag.models.embeddings import build_lc_embeddings


def test_openai_embedding_timeout_and_retry_policy():
    cfg = EmbedConfig(provider="openai", model="m", dim=4,
                      api_key="k", timeout=7)
    client = build_lc_embeddings(cfg)
    assert client.request_timeout == 7
    # 重试只保留 Embedder 一层：库默认还会重试 2 次，叠加后是 3×3
    assert client.max_retries == 0


def test_ollama_embedding_timeout():
    cfg = EmbedConfig(provider="ollama", model="m", dim=4, timeout=5)
    client = build_lc_embeddings(cfg)
    assert client.client_kwargs["timeout"] == 5


def test_openai_chat_timeout():
    cfg = LLMConfig(provider="openai", model="m", api_key="k", timeout=9)
    client = build_chat(cfg)
    assert client.request_timeout == 9


def test_ollama_chat_timeout():
    cfg = LLMConfig(provider="ollama", model="m", timeout=11)
    client = build_chat(cfg)
    assert client.client_kwargs["timeout"] == 11
