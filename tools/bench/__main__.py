"""`python3 -m tools.bench …` / `rag-bench …`

子命令：
  gen         造合成数据集（默认 2000 文档 / 20000 分块）
  fake-embed  起 OpenAI 兼容的假模型服务
  quick       读端点并发矩阵
  ingest      入库并发矩阵（会写数据，请在副本上跑）
  scan-paths    进程内存储开销归因
  param-sweep 检索参数（candidate_k/nprobes/refine_factor）敏感性与结果重合度
  report      跑 quick+ingest 并存成 JSON 快照，与上一次对比
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

READ_PATHS = [
    "/api/documents?limit=50",
    "/api/kbs",
    "/api/jobs?limit=50",
    "/api/health",
]

GATES = {
    # 门槛来自 docs/PERF-FINAL-DECISION-2026-10-05.md §6。
    # 数值是「这台机器、这份数据集」的验收线，换机器要重标。
    "/api/documents?limit=50": {"p50_max_ms": 6.0, "rps_min": 250.0},
    "/api/kbs": {"p50_max_ms": 6.0, "rps_min": 250.0},
    "/api/jobs?limit=50": {"p50_max_ms": 6.0, "rps_min": 250.0},
    "/api/health": {"p50_max_ms": 8.0, "rps_min": 200.0},
}


def _matrix(base: str, concs: list[int], duration: float,
            paths: list[str] | None = None):
    from tools.bench.client import closed_loop

    rows = []
    for path in (paths or READ_PATHS):
        for c in concs:
            s = closed_loop(base, path, concurrency=c, duration=duration)
            print(s.line())
            rows.append(s.as_dict())
            if s.n == 0:
                print(f"    ↑ 全错：{list(s.errors)[:3]}")
    return rows


def _check_gates(rows: list[dict]) -> int:
    bad = 0
    print("\n门槛（c=max 档比对；不达标即红）：")
    by_path: dict[str, list[dict]] = {}
    for r in rows:
        by_path.setdefault(r["path"], []).append(r)
    for path, samples in by_path.items():
        gate = GATES.get(path)
        if not gate:
            continue
        worst = max(samples, key=lambda s: s["concurrency"])
        ok = worst["p50_ms"] <= gate["p50_max_ms"] and worst["rps"] >= gate["rps_min"]
        bad += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {path[:40]:42s} "
              f"c={worst['concurrency']} p50={worst['p50_ms']:.1f}ms "
              f"(<= {gate['p50_max_ms']}) rps={worst['rps']:.1f} "
              f"(>= {gate['rps_min']})")
    return bad


def cmd_quick(a) -> int:
    rows = _matrix(a.base, a.concs, a.duration)
    return 1 if (a.strict and _check_gates(rows)) else 0


def cmd_ingest(a) -> int:
    from tools.bench.client import closed_loop

    print("入库矩阵（假 embedding 下；真实凭据请另跑 --real-embed）")
    prev = None
    bad = 0
    for c in a.concs:
        s = closed_loop(a.base, "/api/documents/text", concurrency=c,
                        duration=a.duration, method="POST",
                        json_body={"text": "并发入库压测 唯一标记",
                                   "title": "bench"},
                        unique_each=True, timeout=a.timeout)
        print(s.line())
        if s.errors:
            print(f"    错误：{list(s.errors.items())[:2]}")
        if prev is not None and s.rps < prev * 0.9:
            print(f"    FAIL 吞吐随并发下降 >10%（{prev:.2f} → {s.rps:.2f}）"
                  "——这是「越并发越慢」的坍塌特征")
            bad += 1
        prev = s.rps
    return 1 if bad else 0


def cmd_scan_paths(a) -> int:
    from tools.bench import scanpaths

    result = scanpaths.run(Path(a.data), dim=a.dim)
    print(scanpaths.report(result))
    out = Path(a.out) if a.out else None
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        print(f"\n已写入 {out}")
        if out.exists():
            prev = _load_prev(out)
            if prev:
                _diff_scan(prev, result)
    return 0


def _load_prev(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        return None


def _diff_scan(prev: dict, now: dict) -> None:
    print("\n与上一份快照对比（负数=变快）：")
    for section in ("lance", "retrieval"):
        p, n = prev.get(section, {}), now.get(section, {})
        for k in n:
            if k in p and p[k]:
                print(f"  {section}.{k:26s} {p[k]:8.2f} → {n[k]:8.2f} ms"
                      f"  {(n[k] - p[k]) / p[k] * 100:+6.1f}%")


def cmd_gen(a) -> int:
    from tools.bench import dataset

    print(f"生成 {a.docs} 文档 / {a.docs * a.chunks_per} 分块 → {a.data}")
    info = dataset.build(Path(a.data), docs=a.docs, chunks_per=a.chunks_per,
                        dim=a.dim, kbs=a.kbs, with_indexes=not a.no_indexes)
    print(json.dumps(info, indent=2, ensure_ascii=False))
    return 0


def cmd_fake_embed(a) -> int:
    # 直接跑它的 __main__：fake_embed 要从 argv 读 DIM/port，
    # 用 `python -m uvicorn …` 的话这些参数会归 uvicorn 而不是它自己。
    cmd = [sys.executable, "-m", "tools.bench.fake_embed",
           "--port", str(a.port), "--dim", str(a.dim)]
    print("假模型服务（OpenAI 兼容 /v1/embeddings + /v1/rerank）："
          f"http://127.0.0.1:{a.port}")
    print("用它把网络抖动与额度从基准里摘掉；真实模型延迟是另一个实验。")
    return subprocess.call(cmd)


def cmd_report(a) -> int:
    rows = _matrix(a.base, a.concs, a.duration)
    payload = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "base": a.base, "read": rows}
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    prev = None
    if out.exists():
        try:
            prev = json.loads(out.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            prev = None
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    print(f"\n快照写入 {out}")
    if prev:
        print("与上一次对比：")
        old = {r["path"]: r for r in prev.get("read", [])
               if r["concurrency"] == max(a.concs)}
        new = {r["path"]: r for r in rows if r["concurrency"] == max(a.concs)}
        for p, n in new.items():
            o = old.get(p)
            if o and o.get("rps"):
                print(f"  {p[:40]:42s} rps {o['rps']:7.1f} → {n['rps']:7.1f}"
                      f"  {(n['rps'] - o['rps']) / o['rps'] * 100:+6.1f}%")
    return 1 if _check_gates(rows) else 0

def cmd_param_sweep(a) -> int:
    from tools.bench import paramsweep

    rep = paramsweep.run(Path(a.data), dim=a.dim, queries=a.queries,
                         top_k=a.top_k, repeats=a.repeats)
    paramsweep.print_report(rep)
    if a.out:
        out = Path(a.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rep, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        print(f"\nJSON 已写入 {out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="rag-bench",
                                 description="Raggi 并发基准与开销归因")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_common(p, base_default="http://127.0.0.1:8000"):
        p.add_argument("--base", default=base_default)
        p.add_argument("--duration", type=float, default=5.0)
        p.add_argument("--concs", type=int, nargs="+", default=[1, 8, 32])

    p = sub.add_parser("quick"); add_common(p)
    p.add_argument("--strict", action="store_true", help="门槛不过则退出码 1")
    p.set_defaults(fn=cmd_quick)

    p = sub.add_parser("ingest"); add_common(p)
    p.add_argument("--timeout", type=float, default=300.0)
    p.add_argument("--real-embed", action="store_true",
                   help="声明这一轮打的是真实模型服务（不与假服务结果混排）")
    p.set_defaults(fn=cmd_ingest)

    p = sub.add_parser("scan-paths")
    p.add_argument("--data", default="data")
    p.add_argument("--dim", type=int, default=1024)
    p.add_argument("--out", default="")
    p.set_defaults(fn=cmd_scan_paths)

    p = sub.add_parser("param-sweep")
    p.add_argument("--data", default="data")
    p.add_argument("--dim", type=int, default=1024)
    p.add_argument("--queries", type=int, default=12)
    p.add_argument("--top-k", dest="top_k", type=int, default=10)
    p.add_argument("--repeats", type=int, default=9)
    p.add_argument("--out", default="")
    p.set_defaults(fn=cmd_param_sweep)

    p = sub.add_parser("gen")
    p.add_argument("--data", required=True)
    p.add_argument("--docs", type=int, default=2000)
    p.add_argument("--chunks-per", type=int, default=10)
    p.add_argument("--dim", type=int, default=1024)
    p.add_argument("--kbs", type=int, default=5)
    p.add_argument("--no-indexes", action="store_true")
    p.set_defaults(fn=cmd_gen)

    p = sub.add_parser("fake-embed")
    p.add_argument("--port", type=int, default=8390)
    p.add_argument("--dim", type=int, default=1024)
    p.set_defaults(fn=cmd_fake_embed)

    p = sub.add_parser("report"); add_common(p)
    p.add_argument("--out", default="benchmarks/latest.json")
    p.set_defaults(fn=cmd_report)
    return ap


def main(argv=None) -> int:
    ap = build_parser()
    a = ap.parse_args(argv)
    if getattr(a, "real_embed", False):
        print("注意：--real-embed 的结果不能与假模型服务的结果同表比较。")
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
