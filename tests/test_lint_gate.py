"""把 CI 的静态门槛变成本地门槛。

为什么需要这一条：B5 重构（f751957）把两条 `I001`  import 排序问题推上了
main，CI 当场变红，而**本地 549 条测试全绿** —— pytest 里没有任何一条跑过
ruff，于是「CI 是门槛」这件事只在推上去之后才成立。用 AST/正则做源码手术后
尤其容易留这种尾巴（那两条正是删函数和插 import 的残迹）。

这条测试不做任何代码检查，只把 CI 里那条命令原样跑一遍：门槛重复一次，
且必须与 CI 用的是同一条，否则两边会各自漂移。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _ruff_available() -> bool:
    out = subprocess.run([sys.executable, "-m", "ruff", "--version"],
                         capture_output=True, text=True)
    return out.returncode == 0


@pytest.mark.skipif(not _ruff_available(),
                    reason="ruff 未安装（CI 与 dev extra 会装）")
def test_ruff_gate_matches_ci():
    """与 .github/workflows/ci.yml 的 `ruff check rag tools tests` 一致。"""
    p = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "rag", "tools", "tests"],
        cwd=ROOT, capture_output=True, text=True)
    assert p.returncode == 0, (p.stdout + p.stderr)[-3000:]
