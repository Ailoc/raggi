"""启动期维护的「最近做过」标记 —— 从 `rag/server.py` 搬来的叶子。

为什么要搬（审计 §1.3 的 A 环）：`api/__init__.py` 的 `_startup_maintenance`
要在函数体里 `from rag.server import …`，而 `server.build_app()` 又要
`from rag.api import Ctx, create_app` —— 两条边都是**运行时调用**而不是类型依赖，
谁先导入都还行，但这条环让「api 与 server 谁在上」这件事没有答案，
而且它只需要一个布尔判断就被彻底拆掉：标记文件的操作不属于 server（CLI 装配层），
它是一个独立的小约定。

搬到 core 之后：`api → core.maintenance ← server`，两向都是单向，
`api/__init__.py` 里那句延迟导入也就能回到模块级了。
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

# 多进程下每个 worker 都会跑一次建索引/FTS 重建，那是同一份活干 N 遍
# （还互相抢写锁）。用标记文件把它收敛成
# 「每台机器每 MAINTAIN_MARKER_TTL 秒最多一次」。
MAINTAIN_MARKER = ".last_index_maintenance"
MAINTAIN_MARKER_TTL_SECONDS = 300.0

log = logging.getLogger("raggi.maintenance")


def should_run_startup_maintenance(data_dir: Path,
                                   now: float | None = None) -> bool:
    """这台机器最近做过启动期索引维护吗。

    标记只在「维护成功启动」时写入；崩溃的进程留下过期标记的代价是
    下一次启动多做一遍，而不是少做——这个方向是安全的。
    """
    marker = data_dir / MAINTAIN_MARKER
    try:
        mtime = marker.stat().st_mtime
    except OSError:
        return True
    return (now or time.time()) - mtime > MAINTAIN_MARKER_TTL_SECONDS


def mark_maintenance_done(data_dir: Path) -> None:
    try:
        (data_dir / MAINTAIN_MARKER).touch()
    except OSError as e:  # pragma: no cover
        log.debug("写维护标记失败（不影响功能）: %s", e)
