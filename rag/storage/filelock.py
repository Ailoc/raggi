"""跨进程写锁：把 LanceDB 的写入与索引维护串行化到**整个机器**，而不只是一个进程。

为什么需要它
------------
`LanceStore` 原来用 `threading.RLock`，于是「多进程共享一个数据目录」是不安全的
（DESIGN §16 明确写了「跨进程并发写 LanceDB 的正确性未经验证，故不支持
`--workers > 1`」）。这条限制真正的代价在**读**上：单进程意味着一个 GIL，
实测同一份读操作 1/2/4/8 线程只到 1.28×，而多进程能到 2.68×
（docs/PERF-CONCURRENCY-2026-10-05.md §2.4）。要拿到那 2.5×，
必须先把「写」做成跨进程安全的。

为什么 flock 够用
-----------------
`fcntl.flock` 在同一台机器上的多个进程之间由内核保证互斥，且**进程死掉自动释放**
（fd 关闭即解锁），不会留下需要人工清理的锁文件状态。这就把「单进程」这个
架构前提换成了「单写入临界区」，而不需要引入外部服务。

必须知道的边界
--------------
1. **只在 `storage.backend=local` 且进程同机时成立**。对象存储（s3）或多机部署下
   每台机器各有一把本地锁，等于没锁——那时需要的是共享存储上的锁
   （LanceDB 自身的乐观并发提交 / 一个外部协调者）。`FileLock` 因此要求显式传入
   锁文件路径，装配层只在 local 后端下启用它（见 `storage/tables.py`）。
2. **锁是进程间互斥、线程间也互斥**。为了保持原来 `RLock` 的可重入语义
   （`delete_documents` 会在已持锁时再取一次），这里套了两层：进程内一把
   `RLock` 负责线程互斥，最外层再 `flock` 负责进程互斥。
   正因为同一时刻只有一个线程进得来，**可重入深度可以是全局一个计数器**。
   按线程各记一份是错的：共用同一个 fd 时，线程 A 释放自己那一份 depth
   就会 `LOCK_UN`，而线程 B 还以为自己持着锁。
3. **持锁期间不要做慢 IO**（embedding、解析）。这条纪律原本就写在
   pipeline 的注释里，并由 `tests/test_perf.py` 的耗时断言守着；
   换成跨进程锁之后这条纪律**更重要**了——它现在会让别的 API 进程一起等。
"""
from __future__ import annotations

import fcntl
import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger("raggi.lock")

class FileLock:
    """可重入（同线程）+ 跨进程互斥的写锁。

    用法与原 `threading.RLock` 一致：`with store.write_lock(): ...`

    **为什么必须两层**：`flock` 的互斥单位是「打开文件描述（fd）」。
    同一进程内多个线程共用一个 fd 时，第二个线程对**同一个 fd** 再 flock
    一次是直接成功的——于是只有进程间互斥、**线程间裸奔**。
    原来用 `threading.RLock` 是有线程互斥的，只换成 flock 会把这个保证丢掉
    （实测会让「并发入库去重」与「并发手工分块拿同一个 ordinal」两类
    竞态重新出现）。所以这里：先拿进程内 RLock（线程互斥、可重入），
    再在最外层拿 flock（进程互斥）。
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._fd: int | None = None
        # 名字刻意不叫 _local：它不是 thread-local，是一把进程内的 RLock，
        # 作用正是「让多个线程排队进同一个 fd」。
        self._guard = threading.RLock()
        self._depth = 0

    def _ensure_fd(self) -> int:
        if self._fd is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
        return self._fd

    def acquire(self) -> None:
        self._guard.acquire()
        try:
            if self._depth == 0:
                fcntl.flock(self._ensure_fd(), fcntl.LOCK_EX)
            self._depth += 1
        except BaseException:
            self._guard.release()
            raise

    def release(self) -> None:
        if self._depth <= 0:
            raise RuntimeError("写锁被重复释放")
        self._depth -= 1
        try:
            if self._depth == 0 and self._fd is not None:
                # 只解锁，不关 fd：反复 open/close 会让锁语义与 fd 生命周期纠缠
                fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            self._guard.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False

    def locked_by_us(self) -> bool:
        """诊断用：本进程当前是否持有写锁。

        **故意不做阻塞**：这是个观测接口（测试、`/health` 都可能调），
        为了读一个整数而去等一把正在干活的写锁，等于让观测改变行为。
        无阻塞拿到就读，拿不到就报 False —— 语义是「此刻没在临界区」。
        """
        if not self._guard.acquire(blocking=False):
            return False
        try:
            return self._depth > 0
        finally:
            self._guard.release()


class ReentrantMutex:
    """`threading.RLock` 的同进程包装：保留旧行为，供测试与单进程部署使用。"""

    def __init__(self):
        self._lock = threading.RLock()

    def acquire(self) -> None:
        self._lock.acquire()

    def release(self) -> None:
        self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False

    def locked_by_us(self) -> bool:
        return self._lock._owner == threading.get_ident() if hasattr(
            self._lock, "_owner") else True
