"""双向导入探针：把「导入次序」变成可重跑的检查，而不是一句注释。

为什么需要这一条：`pyproject.toml` 里写着「开 ruff I 之前实测过风险 ——
用 `pkgutil.walk_packages` 把 rag.* 正序与逆序各导一遍 ⇒ 0 失败」，
但**这个探针从来没进过测试套件**（全仓 grep 不到 `walk_packages`）。
一句无法重跑的验证等价于没有验证 —— 它就是 §2.5 说的注释漂移。

循环依赖的危险恰恰是**次序相关**的：A→B→A 只在某个模块先被导入时才炸
（换个方向时，半初始化的模块恰好已经带上了对方要的名字）。
所以「导入了全部模块」的单向测试会给出假绿，这里两种方向都跑。

必须在**子进程**里跑：清 `sys.modules` 会让后续测试拿到第二份
`RaggiError` 之类的类对象，`pytest.raises` 的身份判断随之失效 ——
那就是一条测试污染整条套件。子进程里怎么折腾都不影响别人。
"""
from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_PROBE = '''
import importlib, os, sys
import rag

# 从文件系统枚举，不用 pkgutil.walk_packages：
# rag/ingest、rag/models、rag/parsing 都没有 __init__.py（隐式命名空间包），
# walk_packages 只会返回 40 个而磁盘上是 53 个 —— 探针会「全绿」，
# 却从没导入过 pipeline/queue/splitter 这三个真正最容易成环的模块。
# （这个缺口是本轮第一版探针自己报出来的：它断言模块数 > 40 就红了。）
names = set()
for root, dirs, files in os.walk(os.path.dirname(rag.__file__)):
    dirs[:] = [d for d in dirs if d != "__pycache__"]
    for f in files:
        if f.endswith(".py") and f != "__init__.py":
            rel = os.path.relpath(os.path.join(root, f),
                                  os.path.dirname(os.path.dirname(rag.__file__)))
            names.add(rel[:-3].replace(os.sep, "."))
names = sorted(names)
assert len(names) >= 50, f"模块数异常（枚举失败？）：{len(names)}"

def fresh(name):
    for key in [k for k in sys.modules
                if k == "rag" or k.startswith("rag.")]:
        del sys.modules[key]
    importlib.import_module(name)

bad = []
for order, seq in (("forward", names), ("reverse", list(reversed(names)))):
    for n in seq:
        try:
            fresh(n)
        except Exception as e:
            bad.append(f"[{order}] {n}: {type(e).__name__}: {e}")
print("MODULES", len(names))
if bad:
    print("\\n".join(bad))
    sys.exit(1)
'''


def test_package_imports_in_both_orders():
    p = subprocess.run([sys.executable, "-c", _PROBE], cwd=ROOT,
                       capture_output=True, text=True)
    assert p.returncode == 0, (p.stdout + p.stderr)[-4000:]
    # 顺带确认探针真的在数模块，而不是空跑一遍就退出 0
    line = [l for l in p.stdout.splitlines() if l.startswith("MODULES ")]
    assert line, f"探针没报告模块数：{p.stdout!r}"
    assert int(line[0].split()[1]) >= 50


def test_declared_console_scripts_are_importable():
    """`[project.scripts]` 里声明的每个入口，模块路径必须真的能导入。

    这条是被 `rag-bench` 逼出来的：`packages.find` 只收 `rag*`，
    而入口指向 `tools.bench.__main__` ⇒ 非 editable 安装后
    `ModuleNotFoundError: No module named 'tools'`（已实测）。
    editable 恰好看不出来，因为源码目录本身在 sys.path 上。

    这里只查「入口的模块能不能导入」；`include` 里少了什么要靠真装一次，
    那个动作写在 §7 的复现清单里（CI 已经有一次 build + 装 wheel）。
    """
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10：项目本来就为此依赖了 tomli
        import tomli as tomllib  # type: ignore[no-redef]

    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = data["project"]["scripts"]
    assert scripts, "没读到 [project.scripts]，这条测试变成空跑了"
    bad = []
    for name, target in scripts.items():
        mod = target.split(":")[0]
        try:
            importlib.import_module(mod)
        except Exception as e:  # noqa: BLE001
            bad.append(f"{name} -> {target}: {type(e).__name__}: {e}")
    assert not bad, "声明的入口导不进来：\n" + "\n".join(bad)
    # 声明了入口却没把那个包打进 wheel，就是当初那个 bug 的形状
    found = data.get("tool", {}).get("setuptools", {}).get(
        "packages", {}).get("find", {}).get("include", [])
    assert any(t.startswith("tools") for t in found), (
        f"packages.find 的 include={found} 不含 tools*，"
        "而 rag-bench 入口在 tools.bench 下 —— 装出来的脚本会 ModuleNotFoundError")

