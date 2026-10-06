"""检索参数敏感性：`candidate_k` / `nprobes` / `refine_factor` 各自花多少、改变多少结果。

为什么需要这个工具
------------------
`retrieve.refine_factor` × `retrieve.candidate_k` 决定「对多少条候选做精确重排」。
审计时量到这一段约占 `/api/search` p50 的 80%，而 `refine_factor=1` 时同样
查询几乎减半——**但那是召回质量的旋钮，不能由性能单方面调**。
（真实 embedding 下的召回数据证实了这句：见 `docs/ARCH-AUDIT-2026-10-06.md` §4.7
 —— `refine_factor` 降到 1–2 时，跨主题查询的 recall@10 从 0.91–0.96
 塌到 0.61–0.79，而只省下 5–9ms。）

基准组取的是**代码里的当前默认值**（`RetrieveConfig()`），不是抄下来的一份数字 ——
抄的那份在默认值改动后会变成假话，而报表看起来完全正常。

所以「要不要调默认值」需要两个数，缺一不可：
1. 成本：这组参数的 p50 / p95 延迟；
2. 变化幅度：这组参数返回的 top-k 与基准配置的**重合度**。
只看 1 会把默认值调到更快但更差；只看 2 不知道该付多少钱。

必须知道的局限（读结果前先看这段）
----------------------------------
- 数据集是**随机单位向量**（见 `dataset.py`）。维度、分布形状、索引形态与
  真实 embedding 一致，所以 **ANN/FTS 的路径与成本是真实的**；
  但随机高维向量的距离趋于一致，**排序稳定性只是弱代理**，
  不能当作「换成真实模型后重合度也这样」。这一点由 `overlap` 那列的
  解释边界承担，不由我替它背书。
- 假 embedding 服务不参与：这里量的是**引擎内部**的检索成本，
  与 embedding 是不是真的无关（查询向量在进程内直接构造）。
- 单机单进程、无并发。并发下的绝对数字会更差，但**参数之间的相对差**
  是本工具想给出的东西。
"""
from __future__ import annotations

import statistics
import time
from pathlib import Path

DIM_DEFAULT = 1024


def _pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    return s[min(len(s) - 1, int(len(s) * p))]


def _timed(fn, n: int, warm: int = 3, divide: int = 1) -> tuple[float, float]:
    """p50 / p95，单位是**单次查询**毫秒。

    `divide` 是每次 `fn()` 里包含的查询条数：把一批查询放在一个计时样本里
    能摊薄 Python 与建连接的噪声，但如果不除回去，报表上的「p50」会比真实
    单次延迟大 N 倍 —— 这种单位的坑必须由工具自己堵掉。
    """
    for _ in range(warm):
        fn()
    ts = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        ts.append((time.perf_counter() - t0) * 1000 / max(1, divide))
    return _pct(ts, 0.5), _pct(ts, 0.95)


def _query_vectors(store, dim: int, n: int, seed: int):
    """造 n 个查询向量 + 对应 FTS 词。

    FTS 词必须能在 `text_seg` 里命中，否则 hybrid/fts 通道测的是「零结果」
    那条捷径，与真实检索的成本完全不是一回事。
    """
    import numpy as np

    rng = np.random.default_rng(seed)
    out = []
    for i in range(n):
        v = rng.standard_normal(dim, dtype=np.float32)
        v /= np.linalg.norm(v)
        # 词必须真能在 text_seg 里命中：数据集每篇文档写的是
        # 「…关键词 测试样例 alpha{i}」，所以取 alpha{某篇文档号}
        out.append((v.tolist(), f"alpha{i * 137}"))
    return out


def run(data_dir: Path, *, dim: int = DIM_DEFAULT, queries: int = 12,
        top_k: int = 10, repeats: int = 9) -> dict:
    from rag.storage.backend import LocalBackend
    from rag.storage.repos import search_chunks
    from rag.storage.tables import LanceStore

    data_dir = Path(data_dir)
    store = LanceStore(LocalBackend(data_dir), dim)
    qv = _query_vectors(store, dim, queries, seed=11)

    def measure(mode: str, *, ck: int, np_: int, rf: int) -> tuple:
        """返回 (p50, p95, 该模式下的 top-k 集合)。"""
        got: list[set] = []

        def one():
            rows = []
            for vec, term in qv:
                rows.append(search_chunks(
                    store, mode=mode, vector=vec,
                    text=term if mode != "vector" else "",
                    limit=ck, nprobes=np_, refine_factor=rf))
            return rows

        p50, p95 = _timed(one, repeats, divide=len(qv))
        for vec, term in qv:
            rows = search_chunks(store, mode=mode, vector=vec,
                                 text=term if mode != "vector" else "",
                                 limit=ck, nprobes=np_, refine_factor=rf)
            got.append({r["chunk_id"] for r in rows[:top_k]})
        return p50, p95, got

    report: dict = {"dim": dim, "queries": queries, "top_k": top_k,
                    "rows": store.chunks.count_rows(), "modes": {}}

    # 基准 = **代码里的真实默认值**，不是抄一份数字。
    # 原先这里硬写 `candidate_k=50` 并注释「现在的默认值」——默认值一改，
    # 这句话就变成假话，而报表看起来完全正常（§2.5 的注释漂移在工具里的版本）。
    from rag.core.config import RetrieveConfig

    dflt = RetrieveConfig()
    base = dict(candidate_k=dflt.candidate_k, nprobes=dflt.nprobes,
                refine_factor=dflt.refine_factor)
    report["baseline_default"] = base
    grids = {
        # 只动 refine_factor：它直接乘进「精确重排多少条」
        "refine_factor": [1, 2, 5, 10, 20],
        # 只动 nprobes：它决定扫多少个 IVF 簇
        "nprobes": [4, 10, 20, 40, 80],
        # 只动 candidate_k：它同时是返回上限与精确重排的基数
        "candidate_k": [10, 20, 50, 100],
    }
    for mode in ("vector", "hybrid", "fts"):
        per_mode: dict[str, list[dict]] = {}
        for pname, values in grids.items():
            # fts 通道不吃向量专属参数（与 retrieval/search.py 的判断一致）
            if mode == "fts" and pname in ("nprobes", "refine_factor"):
                continue
            rows = []
            ref_p50, ref_p95, ref_sets = measure(
                mode, ck=base["candidate_k"], np_=base["nprobes"],
                rf=base["refine_factor"])
            for v in values:
                kw = dict(candidate_k=v if pname == "candidate_k"
                          else base["candidate_k"],
                          nprobes=v if pname == "nprobes" else base["nprobes"],
                          refine_factor=v if pname == "refine_factor"
                          else base["refine_factor"])
                p50, p95, sets = measure(mode, ck=kw["candidate_k"],
                                         np_=kw["nprobes"],
                                         rf=kw["refine_factor"])
                # 与基准的重合度：|交| / |基准|。1.0 = 结果完全没变
                ov = [len(a & b) / max(1, len(b)) for a, b in zip(sets, ref_sets)]
                rows.append({pname: v, "p50_per_query_ms": round(p50, 2),
                             "p95_per_query_ms": round(p95, 2),
                             "vs_base": round(p50 / ref_p50, 2) if ref_p50 else None,
                             "top_k_overlap": round(
                                 statistics.fmean(ov), 3) if ov else None})
            per_mode[pname] = {
                "baseline_p50_per_query_ms": round(ref_p50, 2),
                "baseline_p95_per_query_ms": round(ref_p95, 2),
                "sweep": rows}
        report["modes"][mode] = per_mode
    return report


def print_report(rep: dict) -> None:
    print(f"\n数据集：{rep['rows']} 分块 / dim={rep['dim']} / "
          f"{rep['queries']} 个查询 / top_k={rep['top_k']}"
          f"（随机向量：成本可信，排序重合度只是弱代理）")
    print("单位：下面所有 p50/p95 都是**单次查询**毫秒"
          "（每个计时样本含全部查询，已除回）")
    for mode, per in rep["modes"].items():
        print(f"\n=== mode={mode}")
        for pname, blk in per.items():
            print(f"  扫 {pname}"
                  f"（基准单次 p50 {blk['baseline_p50_per_query_ms']}ms）")
            for r in blk["sweep"]:
                ov = r["top_k_overlap"]
                print(f"    {pname}={r[pname]:<4} 单次p50 "
                      f"{r['p50_per_query_ms']:>7.2f}ms"
                      f"  p95 {r['p95_per_query_ms']:>7.2f}ms"
                      f"  成本 {str(r['vs_base']) + '×':>6}"
                      f"  top-k 重合 {ov if ov is not None else '-'}")
