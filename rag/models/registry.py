"""模型注册表：保存当前生效的 embedder / chat / reranker，支持热切换与测试。"""
from __future__ import annotations

from dataclasses import dataclass

from rag.core.config import EmbedConfig, LLMConfig, RerankConfig, Settings
from rag.models.chat import build_chat
from rag.models.embeddings import Embedder, build_embedder
from rag.models.rerank import build_reranker


@dataclass
class ModelBundle:
    embedder: Embedder
    chat_factory: callable
    reranker: object | None
    embed_cfg: EmbedConfig
    llm_cfg: LLMConfig
    rerank_cfg: RerankConfig
    # 惰性单例，放在最后（上面都是必填字段，带默认值的字段不能夹在中间，
    # 否则 dataclass 直接 TypeError）。
    #
    # `chat_factory()` 每次调用都会新建一个客户端，而 ChatOpenAI 内部自带
    # 一个 httpx 连接池 —— 每请求新建就等于**每请求重新握手**，
    # 问答并发时这部分全是白付的成本。建好的实例缓存在这里
    # （LangChain 的 chat 对象按调用无状态，可以安全复用）。
    chat: object | None = None


class ModelRegistry:
    def __init__(self, settings: Settings):
        self.settings = settings
        # 旧配置的标量快照：merge_sub 会就地改 settings.embed，若 bundle 只存
        # 同一对象引用，reload() 里 old vs new 恒相等，警告永远是死代码（B3）。
        self._prev = self._snapshot()
        self.bundle = self._build(settings)

    def _snapshot(self) -> dict:
        s = self.settings
        return {"dim": s.embed.dim, "model": s.embed.model}

    def _build(self, s: Settings) -> ModelBundle:
        return ModelBundle(
            embedder=build_embedder(s.embed),
            chat_factory=lambda: build_chat(s.llm),
            reranker=build_reranker(s.rerank),
            # 存副本而非引用，隔离外部对 settings 的就地修改
            embed_cfg=s.embed.model_copy(deep=True),
            llm_cfg=s.llm.model_copy(deep=True),
            rerank_cfg=s.rerank.model_copy(deep=True),
        )

    def reload(self) -> dict:
        """热切换模型配置，返回变更摘要（维度/模型变更需迁移数据的警告）。"""
        old = self._prev
        self.bundle = self._build(self.settings)
        now = {"dim": self.settings.embed.dim, "model": self.settings.embed.model}
        warnings: list[str] = []
        if old is not None:
            if old["dim"] != now["dim"]:
                warnings.append(
                    f"embed.dim {old['dim']} -> {now['dim']}：向量维度变化，"
                    "现有 chunks 表的 vector 列仍是旧维度，写入将全部失败。"
                    "POST /api/reindex 无法修复（它只重建索引，不改表 "
                    "schema）——请把维度改回，或换用新的数据目录重新入库。"
                )
            if old["model"] != now["model"]:
                warnings.append(
                    f"embed.model {old['model']} -> {now['model']}："
                    "已有 chunk 的 embed_model 与新模型不一致，检索质量会下降，"
                    "建议对全部文档执行「重新切分」以重算向量"
                )
        self._prev = now
        return {"ok": True, "warnings": warnings}

    @property
    def embedder(self) -> Embedder:
        return self.bundle.embedder

    @property
    def reranker(self):
        return self.bundle.reranker

    def chat(self):
        """取当前生效的 chat 客户端（首次使用时建，之后复用）。

        配置热切换时 `reload()` 会重建 bundle，缓存自然作废 ——
        不会出现「改了模型名还在用旧客户端」。
        """
        bundle = self.bundle
        if bundle.chat is None:
            bundle.chat = bundle.chat_factory()
        return bundle.chat
