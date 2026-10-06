"""测试会话级隔离。

**为什么必须隔离 `data/config.toml`**：`Settings()` 会在实例化时读取
`{data_dir}/config.toml`。测试若不隔离，就会读到开发者本机的生产配置——
一旦本地填了真实的模型凭据或打开了某个 feature 开关，测试行为随之改变。

实测踩到过：在 `data/config.toml` 里开启 `features.answer` 后，
`test_answer_disabled_by_default` 立即失败——它断言的是「默认关闭」，
而"默认"被本机配置覆盖了。

这里把 `RAG_DATA_DIR` 指向一个临时目录，使配置源退化为纯代码默认值。
需要真实目录的测试照旧自行构造 `Settings` 并覆盖字段。
"""
from __future__ import annotations

import os
import tempfile

# 必须在任何 rag.* 模块被导入之前设置：config.toml 的路径在
# Settings 实例化（模块级 settings = Settings()）时就已解析。
_TMP = tempfile.mkdtemp(prefix="raggi-tests-")
os.environ["RAG_DATA_DIR"] = _TMP
