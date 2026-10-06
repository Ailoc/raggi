# 并发改造实施记录（2026-10-05）

对应 [PERF-FINAL-DECISION-2026-10-05.md](./PERF-FINAL-DECISION-2026-10-05.md) 的 S1–S5。
本文只写**已落地什么、实测拿到什么、哪些没做**。
基线数字来自 [PERF-CONCURRENCY-2026-10-05.md](./PERF-CONCURRENCY-2026-10-05.md)。

复现方式（全部在仓库内）：

```bash
python3 -m tools.bench gen --data /tmp/bench/data            # 2000 文档 / 20000 分块
python3 -m tools.bench fake-embed --port 8390 &              # 假模型服务（不花额度）
python3 -m rag.server serve --data /tmp/bench/data --port 8340 &
python3 -m tools.bench quick   --base http://127.0.0.1:8340 --concs 1 8 32
python3 -m tools.bench ingest  --base http://127.0.0.1:8340 --concs 1 4 16 32
python3 -m tools.bench scan-paths --data /tmp/bench/data     # 存储层开销归因
```

## 1. 实测结果（同一台 4 核机、同一份 20k 分块数据集、假 embedding）

| 指标 | 改造前 | 改造后 | 倍数 |
|---|---|---|---|
| `/api/documents?limit=50` p50 @c=1 | 13.4ms | **4.8ms** | 2.8× |
| `/api/documents?limit=50` 吞吐 @c=32 | 84 rps | **456 rps** | **5.4×** |
| `/api/jobs?limit=50` p50 @c=1 | 6.9ms | **3.3ms** | 2.1× |
| `/api/jobs` 吞吐 @c=32 | 138 rps | **635 rps** | **4.6×** |
| `/api/kbs` 吞吐 @c=32 | 88 rps | 170 rps | 1.9× |
| `/api/health` p50 @c=1 | 40.7ms | **2.9ms** | **14×** |
| `/api/health` 吞吐 @c=32 | 28.7 rps | **546 rps** | **19×** |
| `/api/stats` p50 @c=32 | 5053ms | **29.8ms** | **170×** |
| `/api/search`（hybrid）p50 @c=1 | 75.8ms | 57.5ms | 1.3× ← 见 §4 |
| 入库吞吐 @c=1 | 6.52 docs/s | **18.9 docs/s** | 2.9× |
| 入库吞吐 @c=32 | **2.81 docs/s（越并发越慢）** | **60.5 docs/s（单调上升）** | **21.5×** |
| 测试 | 491 passed | **513 passed** | — |

写放大（48 次入库前后，LanceDB `_versions` 目录条目数）：

| 表 | 改造前 | 改造后 |
|---|---|---|
| `jobs` | 2 → **226** | 3 → **3** |
| `documents` | 4 → **68** | 4 → **4** |
| `chunks` | 22 → 58 | 22 → **70**（= +48，一次入库一个版本） |

⇒ 「用得越久越慢」的机制被拆掉了：OLTP 写不再产生列存版本，
剩下的版本增长与**真实数据量**成正比。

## 2. 落地内容

### S1 基准资产化
- `tools/bench/`：`gen` / `fake-embed` / `quick` / `ingest` / `scan-paths` / `report`，
  控制台脚本 `rag-bench`（`pyproject.toml`）。
- `tools/bench/README.md` 记下三条纪律，其中最关键的一条是
  **每线程独立 httpx client**：共享 client 曾让「并发退化」被误判两次（DESIGN §12.1）。
- `tests/test_perf_gates.py`：12 条**结构性**门槛（每请求查询次数、
  是否 materialize 全表、LIKE 通配符转义、分页不重不漏、写锁的两层保证）。
  延迟类门槛在 `@pytest.mark.perf` 下，默认不跑。
  已验证门槛**不是装饰**：把 `list_documents` 退回旧写法，`builder == 0` 那条会红。

### S2 止血
- `storage/sql.py`：新增 `scalar_rows()`（lance 原生投影 + **排序分页下推**）、
  `count_rows()`、`escape_like()`。踩到并记进注释的两个坑：
  排序列必须出现在投影里（否则 `TakeExec requires _rowaddr/_rowid`）；
  lancedb 的 `search().order_by().limit().offset()` 走另一条执行路径也会撞这个错。
- 标量索引补齐：`documents.doc_id`、`jobs.job_id`、`jobs.started_at`、
  `kbs.kb_id`、`chunks.chunk_id`。
- 列表分页：`list_documents` / `list_jobs` / `list_keys` / `list_kbs` /
  `GET /api/chunks` 全部下推，`total` 用 `count_rows`。
- 检索：`_titles` + `_contexts` 从「1 + N 篇文档」次查询合并成 2 次；
  顺带修掉一个哑字段——`SearchHit.parser_engine` 一直读 chunks 上**不存在**的列，
  所以恒为空串，现在从 documents 侧一次带出。
- 命中行显式 `select`：改前 hybrid 会把 `vector`（1024 维）与 `text_seg`
  一起搬回 Python，单行 dict 22.8KB。
- 鉴权：`key_hash → row` 带锁 TTL 缓存（5s）+ `has_keys` 缓存（10s）+
  `touch` 分钟级节流；签发/吊销主动失效。**键必须带 store.uri** ——
  不带的话一个进程挂多个数据目录时，A 库签发的密钥会在 B 库被判为有效。
- 观测端点：`/health`、`/stats` 结果缓存 3s + `X-Snapshot-Age-Ms` 响应头，
  **任何成功的写都会作废快照**（否则「点了修复计数再刷新还是旧账」）。
- 版本治理：`schedule_maintenance` 现在对**全部 5 张表**按
  「版本数超阈值 或 距上次超时」做 `optimize(cleanup_older_than=…)`；
  FTS 重建从「n<50000 每次全量」改成「积压阈值 或 最小间隔」。
- 并发安全：`Embedder._one_cache` 裸 dict → 带锁 TTL LRU（键含模型名，换模型自动失效）；
  `_touch_cache` 加上界；`merge_sub` 不再就地改全局 `settings`；
  `IngestQueue` 的集合上界沿用原逻辑但改走仓储。

### S3 MetaStore（SQLite/WAL）
- 新增 `storage/meta.py`：WAL + `synchronous=NORMAL` + `busy_timeout=5000`、
  每线程独立连接、`BEGIN IMMEDIATE`、`upsert`/`tx`/参数化查询。
- 表：`documents` / `jobs` / `kbs` / `apikeys` / `idempotency` / `outbox` / `meta_state`，
  带正确的复合索引（`(kb_id, created_at DESC, doc_id)` 等）。
- 仓储层全部按 `getattr(store, "meta", None)` 分派，**旧 Lance 表保留不动** ⇒
  `storage.meta_engine=lancedb` 是一个不需要恢复备份的应急回退开关。
- `auto` 模式首启自动从 LanceDB 一次性导入（带行数校验，校验不过**拒绝启用**，
  不留「一半 SQLite 一半 Lance」的分裂态）；显式入口 `rag migrate [--check]`。
- 分块与向量**明确留在 LanceDB**：实测把全文通道换成 SQLite FTS5 会让高频中文词
  从 ~4ms 退化成 22–32ms（方案 §8.3）。
- 新增 `tests/test_meta_store.py`：其中
  `test_two_engines_agree_on_every_observed_result` 让 20 个外部可见结果
  在两条引擎路径上逐一对齐。**它抓到了一个真 bug**：`has_keys()` 还在数
  LanceDB 那张表，而密钥已写进 SQLite ⇒ 「库里一把密钥都没有」永远成立 ⇒
  整个 API 变成无鉴权。这条现在写进了 `keys.py` 的注释里。

### S4 跨进程写锁 + 多进程
- `storage/filelock.py`：**两层**保证 —— `threading.RLock`（线程互斥、可重入）
  + `fcntl.flock`（进程互斥）。只换 flock 会丢线程互斥（flock 的互斥单位是 fd，
  同进程多线程共用 fd 时第二个线程直接成功）；这条真丢过一次，
  两个并发测试同时变红，现在有专门的守卫。
- `rag serve` 默认 `--processes auto` = `min(cpu, 4)`，仍然单命令/单端口/单数据目录；
  `--processes 1` 一键退回旧形态。uvicorn 走 `factory=True` + import 串，
  每个 worker 自建 store。
- 启动期索引维护加标记文件收敛（否则 N 个进程把建索引/重建 FTS 干 N 遍，
  还要排队抢跨进程写锁）。
- 跨进程取消：`is_cancelled()` 现在以任务行为准（内存集合只作本进程快路径），
  否则多进程下「点停止」有 1-1/N 的概率命中不到跑它的进程。
- 读进程不再写 Lance：所有 documents/kbs/jobs/apikeys 写走 SQLite，
  chunks 写走跨进程锁 —— DESIGN §16 那条「不支持 `--workers>1`」的前提已不成立。

### S5 模型调用治理
- `models/http.py`：进程级共享 `httpx.Client`（连接池 + keep-alive）、
  可配并发闸 `ScaledSemaphore`、`CircuitBreaker`。
- rerank 从 `urllib`（每请求新建 TCP、无 keep-alive）换成共享池，
  并加连续失败熔断（3 次 / 冷却 30s）。
- `registry.chat()` 不再每请求新建 `ChatOpenAI`（连带其连接池）——惰性复用。
- embedding 并发闸从硬编码 8 改为按 `embed.concurrency` 定尺寸。
- query→vector 缓存见 S2 的 `Embedder`。

## 3. 明确没做（以及为什么）

1. **真实模型服务的端到端延迟仍未测**。全程用假 embedding，因为不想花真实额度。
   ⇒ 表里 `/api/search` 的 57.5ms 是**不含模型网络往返**的下界。
   要量：`python3 -m tools.bench ingest --real-embed`（换 `--base` 指向真实配置的服务）。
2. **跨引擎级联没有做成「事务性重放」**。删文档 / 移动知识库仍是
   「SQLite 事务 + Lance 写」两步，靠 `flock` 串行化 + 固定顺序 +
   启动期 `reconcile` 对账兜住崩溃窗口。
   （本条原文说「`outbox` 表已建但只有 `reconcile` 一类操作用得到」——
   那张表**从来没有调用点**，审计轮的
   `test_no_dead_tables_in_meta_ddl` 把它抓了出来并连同 `idempotency` 表、
   `jobs.worker` 列一起从 DDL 删除；理由见 `storage/meta.py` DDL 上方注释。）
   改同步端点为异步会动前端契约，收益（崩溃窗口的原子性）不值得在这一轮换。
3. **前端进度从轮询改 SSE 没做**。服务端已经把 `wait=false` 这条路铺好，
   且 `/api/jobs` 从 138 rps 提到 635 rps，轮询不再是瓶颈点。
4. **`_EMBED_SEMAPHORE` 仍是每进程一把**（多进程下总并发 = 进程数 × 配置值）。
   已在代码注释里写明；真要做全局闸需要跨进程信号量，那属于「多机」议题。

## 4. 一处需要修正的旧结论

`/api/search` 只快了 1.3×。原因不在代码，而在**它本来就是 CPU 密集型**：
hybrid 里 `candidate_k=50 × refine_factor=10` 意味着对 500 条候选做精确重排，
实测这一段在 20k 分块上是 ~48ms，占掉整次检索的 80%以上。
`refine_factor=1` 时同样查询 22.6ms（**几乎减半**），但那是**召回质量**的旋钮，
不该由性能改造单方面改配置。要提检索吞吐，按顺序应该是：
① 用真实模型量一轮，确认瓶颈在哪一侧；② 再决定是否调
`retrieve.refine_factor` / `candidate_k`；③ 加核或降并发档位。

同理修正一条我自己在诊断报告里写下的话：
「原生 scanner 比 lancedb 查询构建器快 3×」——那是**6 列 vs 10 列**的比法。
同列集重测，两者常常打平（7.74ms vs 7.61ms）。真正的收益来自
**排序分页下推**（8.63 → 2.50ms，且成本从 O(表行数) 变 O(页大小)）
和**换元数据引擎**（同类列表查询在 SQLite 上 0.100ms）。
`storage/sql.py` 与相关注释已按这个结论改写。

## 5. 改动清单

新增：`rag/core/cache.py`、`rag/storage/filelock.py`、`rag/storage/meta.py`、
`rag/storage/repos/jobs.py`、`rag/models/http.py`、
`tools/bench/*`、`tests/test_perf_gates.py`、`tests/test_meta_store.py`

改动：`rag/server.py`（supervisor / migrate / 维护标记）、`rag/api/__init__.py`
（Ctx.meta、线程池、快照失效、静态资源缓存）、`rag/api/{chunks,documents,system}.py`、
`rag/storage/{sql,tables,health,plan}.py`、`rag/storage/repos/{chunks,documents,kbs,keys,__init__}.py`、
`rag/retrieval/search.py`、`rag/ingest/{queue,pipeline}.py`、`rag/models/{embeddings,registry,rerank}.py`、
`rag/core/config.py`、`rag/chunk_edit.py`、`pyproject.toml`、`tests/test_perf.py`、`tests/test_architecture.py`

`data/` 未被本轮任何写操作触碰（全部实验在 `/tmp/raggi_*` 副本上）。

## 6. 审计轮（2026-10-06）追加

上表 §1 里「测试 513 passed」这个数已被后续几轮推进：**517**（修掉迁移丢正文
与分裂读）→ **544**（审计轮 +27）→ **548**（A 档执行轮）→ **554 passed / 1 deselected**
（B 档执行轮：并发闸门、合批形状、`embed_one` 缓存承诺、本地 lint 门槛，
以及一条随机红测试换成事件闸门版本）。审计轮的完整发现、评级与建议顺序在
[ARCH-AUDIT-2026-10-06.md](./ARCH-AUDIT-2026-10-06.md)；这里只记三件与本文
结论直接相关的事：

1. **本文 §2 S2/S3 的落地里查出两个默认路径缺陷**：自动迁移的列清单漏了
   `text`（每篇文档正文被写成空串，而「行数一致」的校验全绿）；
   以及列表读 SQLite、健康检查/检索读 LanceDB 的**分裂读**。
   后者由 `docs_query` / `docs_count` / `kbs_query` 这组**意图型读接口**收掉
   （传 `eq` / `one_of` / `contains`，不传 SQL 文本），
   `rag/storage/repos/documents.py:265`。
2. **又查出两个同类缺陷**：`MetaStore.upsert` 往 `NOT NULL DEFAULT` 列写显式
   NULL（SQLite 报错、Lance 静默补默认值 ⇒ 双引擎分叉）；
   以及投影 `cols` 被直接拼进 `SELECT` 且**无白名单**（列名无法参数化）。
   现在 `sql.only_cols()` 在引擎分派**之前**收窄，两条路径都收。
3. **本文 §2 S2 提到的 `escape_like` 纪律，审计时补了直接断言**：
   `tests/test_sql_safety.py` 在两条引擎路径上分别测「含 `'` 的合法值必须
   查得回来」「`%` / `_` 必须保持字面量」「`' OR 1=1--` 匹配不到任何行」。
   原来的证据只有改造当时的一次性手工验证 —— 那种证据会过期，测试不会。
