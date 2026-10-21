"""E2E 用最小服务：临时数据目录 + 桩 embedding + 种子知识库。

Playwright 通过 `webServer` 启动它。特点：
- **不依赖外部 embedding/LLM 服务**：注册表里的 embedder 换成确定性桩，
  维度与配置一致（4），入库/检索都能离线跑；
- **每次进程一个新临时目录**：测试不会写进用户真实数据，也不会互相污染；
- 种子库在启动时用 pipeline 直接写入，测试只需读路径，避免每个用例
  都依赖"先通过 UI 入库成功"这一前置。
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import os
import tempfile


class StubEmbedder:
    """确定性向量：同文本永远同向量，不访问网络。"""

    dim = 4
    model = "stub"

    @staticmethod
    def _vec(text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return [digest[i] / 255.0 for i in range(4)]

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def embed_one(self, text: str) -> list[float]:
        return self._vec(text)


def _seed(ctx) -> None:
    from rag.ingest import pipeline
    from rag.storage.repos import kbs as kb_repo

    rows = kb_repo.kbs_query(ctx.store, ["kb_id", "name"])
    if any(r.get("name") == "示例知识库" for r in rows):
        return
    kb = kb_repo.create_kb(ctx.store, "示例知识库", description="产品资料与运维经验示例")
    doc_a = """# 示例文档 A

## 安装与接线
苹果 香蕉 葡萄 常见水果列表。端到端测试种子内容，含关键词苹果。
变频器应安装在通风良好的位置，柜内上下留出散热空间。
输入端子 R/S/T 接三相电源，输出端子 U/V/W 接电机，接地端子必须可靠接地。

## 参数设置
额定电压 380V，允许波动 ±10%。输入电流 45A，输出频率 0–50Hz。
加速时间与减速时间按负载惯量设置，重载启动时可适当延长。
过载保护系数建议按电机额定电流的 1.1–1.2 倍设置。

## 故障排查
故障码 OC 表示过流：检查电机电缆是否短路、负载是否过重。
故障码 OH 表示过热：检查风扇、环境温度与散热风道。
故障码 LU 表示欠压：检查进线电压与接触器触点。

## 日常维护
每季度检查一次散热风扇与滤网，每年紧固一次主回路端子。
长期存放后重新上电前，应先测量绝缘电阻并确认母线电容状态。
"""
    doc_b = """# 示例文档 B

## 植物档案
龙脑香 dipterocarp 稀有树种。端到端测试种子内容，含关键词 dipterocarp。
龙脑香科植物多分布于热带雨林，树干高大，木材与树脂具有经济价值。

## 观察记录
叶片革质，花期集中在雨季前，果实带翅，可借助风力传播。
野外调查时应记录海拔、坡向与伴生树种，避免只记录单一植株。
"""
    for title, text in (("示例文档 A", doc_a), ("示例文档 B", doc_b)):
        pipeline.ingest_text(
            ctx.store, ctx.registry, text, title,
            ctx.settings.parser, ctx.settings, kb_id=kb["kb_id"])

    ops = kb_repo.create_kb(ctx.store, "运维笔记", description="故障排查与现场经验")
    pipeline.ingest_text(
        ctx.store, ctx.registry,
        "现场故障排查记录：变频器过流报警先检查电机电缆绝缘与负载，\n"
        "PLC 通信异常先确认 Modbus TCP 端口 502、站号与网段配置。\n"
        "更换备件前记录原设备序列号与固件版本，便于追溯。",
        "故障排查记录", ctx.settings.parser, ctx.settings, kb_id=ops["kb_id"])

    product = kb_repo.create_kb(ctx.store, "产品手册", description="设备参数与安装规范")
    pipeline.ingest_text(
        ctx.store, ctx.registry,
        "安装规范：柜内设备间距不小于 50mm，进线端子使用 4mm² 铜线并压接冷压端子。\n"
        "接线完成后逐一核对相序、接地与屏蔽层处理，再上电测试。",
        "安装规范", ctx.settings.parser, ctx.settings, kb_id=product["kb_id"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8177)
    args = ap.parse_args()

    tmp = tempfile.mkdtemp(prefix="raggi-e2e-")
    os.environ["RAG_DATA_DIR"] = tmp
    # 必须在 import rag.* 之前写入环境：Settings 单例在导入时构造
    os.environ["RAG_EMBED__PROVIDER"] = "custom"
    os.environ["RAG_EMBED__MODEL"] = "stub"
    os.environ["RAG_EMBED__DIM"] = "4"
    os.environ["RAG_HOST"] = "127.0.0.1"
    os.environ["RAG_PORT"] = str(args.port)

    import uvicorn

    from rag.server import build_app

    logging.basicConfig(level=logging.WARNING)
    app = build_app()
    ctx = app.state.ctx
    ctx.registry.bundle.embedder = StubEmbedder()
    _seed(ctx)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
