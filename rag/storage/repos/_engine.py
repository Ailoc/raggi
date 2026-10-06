"""引擎分派的**唯一**入口。

为什么要有这个文件
------------------
`documents / jobs / kbs / apikeys` 的每个读写函数都要先回答一个问题：
「这次该读 SQLite 还是 LanceDB？」改造期间这个问题在四个模块里各写了一份
`def _meta(store): return getattr(store, "meta", None)`。形状一致但没有强制：
任何一处写成 `store.meta` 直接访问、或者把属性名打错一个字母，
都会**静默走回退路径** —— 而不是报错。

这不是理论风险，本仓库为此付过两次代价（见 docs/ARCH-AUDIT-2026-10-06.md §1.1、§5.1）：

- `has_keys()` 还在数 LanceDB 的空 `apikeys` 表，而密钥已写进 SQLite
  ⇒「库里一把密钥都没有」永远成立 ⇒ **整个 API 变成无鉴权**；
- 列表读 SQLite、健康检查读 LanceDB ⇒ 界面显示 8 篇文档、`/health` 说 7 篇、
  检索永远搜不到那 1 篇，而且三处都不报错。

两个症状的共同点是**读写落在两个引擎上**，而分派点分散时没人能保证它们一致。

本模块只做一件事：把「拿当前元数据引擎」变成一个可调用的、可被测试引用的
单一事实点。它**不**改变语义（仍然是「有 meta 用 meta，没有就走回退」），
但把「回退是合法的」这个判定收到了 `meta_may_be_absent()` 里 ——
回退只在 `storage.meta_engine=lancedb` 被显式配置、或 SQLite 里还没有任何
数据时才允许；否则装配层必须**拒绝启动**（见 `rag.storage.meta`）。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..meta import MetaStore
    from ..tables import LanceStore


def meta_of(store: "LanceStore") -> "MetaStore | None":
    """当前元数据引擎；None = LanceDB 回退路径。

    读的是 `LanceStore.meta`（一个声明式 property，不是随手挂上的实例属性），
    所以这里刻意用 `getattr` 兜一层：脚本与测试里存在鸭子类型的 store 对象
    （没有 meta 属性），它们本来就该走回退路径。
    """
    return getattr(store, "meta", None)


def engine_kind(store: "LanceStore") -> str:
    """给观测用：当前这次调用实际会打到哪个引擎。"""
    return "sqlite" if meta_of(store) is not None else "lancedb"
