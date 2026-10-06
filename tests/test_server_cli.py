"""从**真正的入口**跑一遍 `rag serve`：钉住 CLI 覆盖能生效。

为什么单独有这一组：C 档拆 api↔server 循环依赖时，我把 `rag/server.py` 里的
`from rag.api import …` 从函数体提到了模块级 —— 看起来只是让依赖图更诚实，
实际后果是 `rag serve --host/--port/--data` **全部静默失效**：
`rag.api` 连带导入 `rag.core.config`，而它在模块级就构造
`settings = Settings()` 单例；等 `main()` 解析完参数写进 `RAG_*` 时，
单例早已按 config.toml 冻结。实测绑上去的就是 `0.0.0.0:8000`，
而**561 条测试全绿** —— 因为所有测试都是直接调 `create_app(Ctx(...))`，
没有任何一条走 `rag.server.main()`。

⇒ 这个文件存在的理由就是那一课：**入口层的行为不在被测范围内时，
「套件全绿」和「命令能用」是两件事。**
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_line(proc, needle: str, timeout: float = 60.0) -> str:
    """读子进程输出直到出现 needle，返回已读到的全部文本；超时则抛。"""
    deadline = time.time() + timeout
    buf = ""
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                break
            time.sleep(0.05)
            continue
        buf += line
        if needle in buf:
            return buf
    proc.kill()
    raise AssertionError(f"{timeout}s 内没等到 {needle!r}，已读到：\n{buf[-2000:]}")


def test_cli_host_and_port_actually_reach_the_bind_address(tmp_path):
    """`--host/--port` 必须真的决定绑定地址，且这个地址上要有服务。

    用 `--data tmp_path`：既避免去碰开发者机器上的 `data/`，
    也让 config.toml 落到空目录 —— 于是绑定只能来自 CLI，
    万一覆盖再次失效，就会暴露成「预期的 127.0.0.1 上没有服务」。
    """
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "rag.server", "serve",
         "--host", "127.0.0.1", "--port", str(port),
         "--data", str(tmp_path), "--processes", "1"],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
        env={**os.environ, "PYTHONUNBUFFERED": "1"})
    try:
        got = _wait_line(proc, f"http://127.0.0.1:{port}")
        assert "0.0.0.0" not in got.split("data=")[0], (
            f"启动横幅里出现了 0.0.0.0，说明 --host 被忽略：{got}")

        # 光看横幅不算：要在**那个地址**上真拿到一次 200
        deadline = time.time() + 30
        last = None
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/api/health", timeout=5) as r:
                    body = json.loads(r.read())
                    assert r.status == 200
                    assert "status" in body
                    break
            except Exception as e:  # noqa: BLE001 - 重试中的启动竞态
                last = e
                time.sleep(0.5)
        else:
            raise AssertionError(f"127.0.0.1:{port} 上没有可用服务：{last}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()


def test_importing_server_does_not_freeze_settings_singleton():
    """钉住**机制**：`import rag.server` 不许提前构造 `settings` 单例。

    上一条测现象，这一条测原因 —— 只有原因那条会在有人「顺手把导入提到
    模块级」时立刻红，而不需要等到有人真去跑一次 `rag serve`。
    """
    code = (
        "import rag.server, os;"
        "os.environ['RAG_HOST'] = '127.0.0.9';"
        "from rag.core.config import settings;"
        "print(settings.host)"
    )
    p = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                       capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stderr[-1500:]
    assert p.stdout.strip().endswith("127.0.0.9"), (
        f"import rag.server 之后 RAG_HOST 已经来不及生效了（settings 单例被提前"
        f"构造）：读到 {p.stdout.strip()!r}。"
        "这会让 `rag serve --host/--port/--data` 静默失效。")
