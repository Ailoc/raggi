"""配置契约：Settings 里每个字段都必须有实际读取点。

回归防护：`RetrieveConfig.query_rewrite` 与 `FeaturesConfig.query_logs`
曾在配置与 /api/models 响应中存在，但全仓没有任何读取点——用户改了配置
却发现行为不变，而这类“配置在骗人”不会有任何报错。删除它们之后，这条
守卫负责拦住下一个未接线就写进 Settings 的字段。
"""
from __future__ import annotations

import re
from pathlib import Path

from rag.core.config import (
    EmbedConfig,
    FeaturesConfig,
    LLMConfig,
    ParserConfig,
    RerankConfig,
    RetrieveConfig,
    Settings,
    SplitConfig,
    StorageConfig,
)

ROOT = Path(__file__).resolve().parent.parent

# 只靠 pydantic-settings / 框架消费、没有业务读取点的字段（部署面）。
# 不在业务代码里搜它们是**有意**的：加一条要写清为什么。
INFRA_FIELDS = {
    # 由 pydantic-settings 自己消费（env_prefix / 嵌套分隔符）
    "model_config",
}


def _corpus() -> str:
    """除 config.py / responses.py 外的全部后端源码。

    这两个文件只负责“声明”，声明的字段出现在自己文件里不算被消费。
    """
    return "\n".join(
        p.read_text(encoding="utf-8")
        for p in (ROOT / "rag").rglob("*.py")
        if p.name not in ("config.py", "responses.py")
    )


def test_every_settings_field_has_a_reader():
    corpus = _corpus()
    models = [
        (Settings, "settings"),
        (StorageConfig, "storage"),
        (EmbedConfig, "embed"),
        (RerankConfig, "rerank"),
        (LLMConfig, "llm"),
        (RetrieveConfig, "retrieve"),
        (ParserConfig, "parser"),
        (FeaturesConfig, "features"),
        (SplitConfig, "split"),
    ]
    missing = []
    for model, label in models:
        for name in model.model_fields:
            if name in INFRA_FIELDS:
                continue
            if re.search(rf"\b{re.escape(name)}\b", corpus):
                continue
            missing.append(f"{label}.{name}")
    assert not missing, (
        "这些 Settings 字段没有任何业务读取点（配置写了但运行时没人读）: "
        f"{missing}"
    )
