"""LLM 接入：ollama / openai(兼容) / custom。均暴露 OpenAI 兼容端点。"""
from __future__ import annotations

from rag.core.config import LLMConfig


def build_chat(cfg: LLMConfig):
    if cfg.provider == "ollama":
        from langchain_ollama import ChatOllama

        return ChatOllama(model=cfg.model, base_url=cfg.base_url, temperature=cfg.temperature)
    if cfg.provider in ("openai", "custom"):
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=cfg.model,
            base_url=cfg.base_url,
            api_key=cfg.api_key or "EMPTY",
            temperature=cfg.temperature,
            max_tokens=cfg.max_tokens,
        )
    raise ValueError(f"unknown llm provider: {cfg.provider}")
