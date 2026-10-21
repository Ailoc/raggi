"""LLM 接入：ollama / openai(兼容) / custom。均暴露 OpenAI 兼容端点。"""
from __future__ import annotations

from rag.core.config import LLMConfig


def build_chat(cfg: LLMConfig):
    if cfg.provider == "ollama":
        from langchain_ollama import ChatOllama

        return ChatOllama(
            model=cfg.model, base_url=cfg.base_url,
            temperature=cfg.temperature,
            client_kwargs={"timeout": cfg.timeout},
        )
    if cfg.provider in ("openai", "custom"):
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=cfg.model,
            base_url=cfg.base_url,
            api_key=cfg.api_key or "EMPTY",
            temperature=cfg.temperature,
            max_tokens=cfg.max_tokens,
            # 不传时 LangChain 的默认超时与 config.toml 里的 llm.timeout
            # 完全无关——用户以为设了 120s，实际仍是库默认。
            request_timeout=cfg.timeout,
        )
    raise ValueError(f"unknown llm provider: {cfg.provider}")
