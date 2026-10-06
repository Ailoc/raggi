# Raggi 并发最终方案（2026-10-05）· 决策版

> 前置：[诊断报告](./PERF-CONCURRENCY-2026-10-05.md)（实测基线）、[改造方案](./PERF-REFACTOR-PLAN-2026-10-05.md)（含 §8 引擎选型复核）。
> 本文只回答一件事：**如果只能选一条路，走哪条。** 所有取舍都已定，不再摆选项。

---

## 1. 结论

**双引擎分层 + 单命令多进程。LanceDB 不替换，但降级为「只管两件事：向量与中文全文」；
所有 OLTP 语义迁到 SQLite(WAL)，由它成为唯一真源。**

```
rag serve（仍然：一个命令 · 一个端口 · 一个 data/ · 零外部服务）
│
├─ data/raggi.db      SQLite WAL  ← 真源：jobs / apikeys / kbs / documents 元数据
│                                     doc_text（正文，独立表，列表/聚合永不触碰）
│                                     schema_version
│                                     （审计轮 2026-10-06：`idempotency` / `outbox` 是
│                                       本方案当时预留、实现最终落在别处的**零调用点
│                                       投机 schema**，已从 DDL 删除；`ratelimit` 从未建表，
│                                       限流是进程内令牌桶。正文也没有独立表，仍是
│                                       `documents.text` —— 靠列表侧不投影它来隔离开销）
├─ data/lancedb       LanceDB     ← 只留 chunks：vector + text_seg + 下推列(doc_id/kb_id/ordinal/enabled)
├─ data/files         原文归档（不变）
│
├─ API 进程 ×N（N=min(cpu,4)，只读为主）
│    鉴权(内存 LRU，0 次查库) · 检索 · 列表 · 详情 · 落单/写 SQLite
│    改 chunks 的同步端点：在 **flock 跨进程写锁** 下直写 Lance（语义与今天完全一致）
│
└─ 入库 worker ×1–2
     领 jobs（SQLite `UPDATE…RETURNING`，0.009ms）→ 解析 → 分词 → 向量化 → 写 chunks
     → 索引维护 → compaction/版本回收 → 应用 outbox（跨引擎级联）
```

### 为什么是这个而不是别的

| 判断 | 依据（全部实测） |
|---|---|
| **不能把 LanceDB 全换掉** | SQLite FTS5 的「bm25 + LIMIT」成本与命中总数成正比：高频中文词 22–32ms，tantivy 只要 4ms。换过去是 6–8× **回归**，而检索质量依赖中文关键词通道。 |
| **但 LanceDB 不能继续当元数据库** | 点查 5.68ms vs SQLite 0.008ms（700×）；分页 6.76 vs 0.100ms；一次入库在 `jobs` 造 ≈5 个版本/文件，48 次入库后读延迟翻 2.3×。「越用越慢」是引擎错配，不是调参问题。 |
| **多进程是唯一的量级来源** | 同进程多线程 1.28× 封顶，多进程 2.68×；HTTP 层 1/2/4 进程 = 70/121/177 rps（p50 203→72ms）。这与存储引擎无关，换库买不到。 |
| **写锁用 flock，不用「全部异步化」** | 保留 `PATCH /chunks`、启停、删除的**同步语义**（今天对用户是对的：停用一块就该立刻搜不到）。flock 成本 0.5d，零契约变化；「唯一写入者 + outbox」只用于**本来就重的跨引擎级联**（删文档、移动库、重切分）。 |
| **不引入 Postgres/Qdrant** | 与「极简单机、零外部依赖」直接冲突（本机连 pg 二进制都没有），且元数据会多出一个真源。它们是这个项目**横向扩容那天**的目标，不是现在。 |

---

## 2. 明确不做（Non-goals）

1. 不替换检索引擎、不上 FTS5、不上 pg_search/Qdrant/Milvus。
2. 不做「读路径缓存层」（除两处有明确收益的：query→vector LRU、鉴权 LRU）。
   列表类缓存不做——M2 之后列表就是 SQLite 0.1ms，加缓存只会带来一致性债。
3. 不改 API 的同步/异步语义（除 `POST /documents` 的 `wait` 默认值，见 §4）。
4. 不追求多机。跨机需要共享 DB 锁与 s3 后端，`storage.backend=s3` 已存在，但那是另一件事。
5. 不动 `data/` 目录形态：终态仍然是「拷走这个目录就等于备份全部」。

---

## 3. 真源边界（这条必须先定，否则实现会摇摆）

| 事实 | 真源 | 理由 |
|---|---|---|
| 文档元数据、正文、知识库、密钥、任务状态 | **SQLite** | OLTP 画像；需要事务与毫秒级点查 |
| 分块的向量、`text_seg`、`ordinal`、`enabled`、`kb_id`、`doc_id` | **LanceDB** | 检索下推需要它们在同一个引擎里 |
| 「某文档有哪些块 / 每块是否启用」的**变更** | 同步写 Lance（flock 内） | 停用立刻生效，语义不退化 |
| 「删文档级联删块 / 移动知识库改块归属 / 重切分」 | **outbox + worker** | 本来就是重操作；换来的是跨引擎的原子可见性与可重放 |

⇒ 代价要说清：**跨引擎的两类操作有秒级滞后窗口**（块已归属旧 kb_id 但文档已移动）。
处置：`/health/deep` 报 `pending_outbox` 数与最老一条的年龄；前端在移动/删除后提示「索引更新中」，
并把对账（`reconcile`）从手工按钮变成 worker 的常驻兜底。

---

## 4. 执行顺序（5 步，每步独立交付、独立可回滚、独立可测量）

### S1 · 基线资产化 + 一个半小时的版本实验（0.5d）
- 把压测脚本固化成 `tools/bench/`（`rag-bench quick|full|scan-paths|ingest`）+
  `tests/test_perf_gates.py`（`-m perf`），把本文 §6 的门槛表写成断言。
- **顺手做那个未验证的可能**：把 lancedb/pylance 升级到 `<0.40` 之外的新版本，在**隔离数据副本**上
  重跑同一组 `scan-paths`（`pyproject.toml` 现在钉在 `lancedb>=0.39,<0.40`，而索引类型、
  `LanceModel` 维度声明、`storage_options` 键名都跨版本变过——所以只在副本上量，不碰 `data/`，
  也不在量完之前改依赖）。
  每查询 6.8ms 而原生 scanner 2.26ms，差值极可能是这一版 Python 查询构建器的实现问题；
  如果升级后掉下来，S2 的「原生 scanner」改造可以省一半。**这一步花 1.5h，可能改掉后面 1d 的工作量。**
- 回滚：无（只加文件）。

### S2 · 止血：把每请求 11.3ms 压到 ~3ms（1.5d）
1. 补标量索引：`documents.doc_id`、`jobs.job_id`、`kbs.kb_id`、`chunks.chunk_id`（5.68→3.88ms）。
2. 标量读走 `to_lance().to_table(columns, filter, limit, offset)`（6.76→2.26ms）；
   收口在 `storage/sql.scalar_rows()`，**向量/FTS 仍走 lancedb**（那是它值钱的地方）。
3. 分页改「窄列取排序键 → 按页取行 → `count_rows` 出 total」两步
   （Lance 侧**无 ORDER BY 下推**，直接 limit/offset 分页的语义不成立）。
4. 检索的 N+1 消掉：`_titles` + `_contexts` 合并成一次查询（实测省 25–32ms）。
5. 鉴权零查库：`key_hash→row` 带锁 LRU（TTL 5–10s，签发/吊销主动失效）+ `has_keys()` 缓存
   + `touch()` 内存累计、30s 批量落盘。
6. `GET /health` 拆成 liveness（不查库）与 `/health/deep`（现有对账，缓存 10s + 信号量 1 串行）；
   `backend.stats()` 的目录遍历缓存 60s；`versions` 改按需。
7. **版本与碎片治理常态化**：全部 5 张表按「版本/文件数超阈值 或 距上次超时」跑
   `optimize(cleanup_older_than=…)` / `compact_files()`；`ensure_fts_index` 从「n<50000 每次全量重建」
   改成阈值触发。（这版 API 只有 `older_than`、没有 `retain=N`，所以真正止血靠第 8 步少写。）
8. 并发隐患修：`Embedder._one_cache` 无锁 dict → 带锁 LRU；`_touch_cache`/`_finished` 加上界；
   `merge_sub` 不再就地改全局 `settings`。
9. 静态资源 `/assets/*` → `immutable`（Vite 已是 hash 名），`index.html` 保持 no-cache；
   入库进度从 700ms/4s 轮询改为一条 SSE。
10. 显式 `set_default_executor(max_workers=cpu*16)` + anyio limiter 提额。
    诚实标注：实测 8→128 对入库坍塌**无影响**，这条是便宜的保险，不是本次瓶颈。

**S2 结束即可交付**：读 ~3×、入库不再坍塌、不再「越用越慢」。这是收益/风险最好的一段。

### S3 · MetaStore：SQLite 成为真源（3d，双写迁移）
- 新增 `rag/storage/meta.py`（唯一允许 `sqlite3.connect` 的地方，架构边界测试同步扩一条）、
  `meta_schema.py`（含 `schema_version`）、`core/textstore` 改为 `doc_text` 独立表（列表/聚合永不触碰正文）。
- 连接纪律：每线程独立连接 + `journal_mode=WAL / synchronous=NORMAL / busy_timeout=5000 / foreign_keys=ON`；
  「读—改—写」用 `BEGIN IMMEDIATE`，取代今天把 OLTP 塞进全局 `RLock` 的做法。
- `jobs` 领取：`UPDATE jobs SET stage='running', worker=? WHERE job_id=(SELECT … WHERE stage='queued'
  ORDER BY started_at LIMIT 1) RETURNING job_id`（本机 sqlite 3.37.2，`RETURNING` 可用，实测 0.009ms）。
- `outbox` 表 + worker 应用 + 幂等重放，承接跨引擎级联；`documents+jobs+outbox` 同事务提交
  ⇒ 彻底消除 `0 分块但 status=ready` / 孤儿 chunk / `count_mismatch` 这一整类问题（DESIGN §16 两条风险一并了结）。
- 迁移四步，每步可退：`rag migrate`（全量搬 + 行数与逐行哈希校验，不一致就退出）→
  `meta_engine=shadow`（Lance 主 + SQLite 影子写 + 差异打到 `/health/deep`）→ `=sqlite`（读切走，仍双写）→
  下版本停双写。迁移前自动快照 `data.bak-<ts>`。
- 顺序：**先 `jobs`+`apikeys`（无列关联、风险最低、收益最大）**，再 `documents`+`kbs`+`doc_text`。

### S4 · 多进程（1.5d）
- `LanceStore.write_lock()`：`threading.RLock` → `fcntl.flock(data/lance-write.lock)`，
  **临界区范围一个字节都不改**（`test_perf.py` 里「写锁不覆盖 embedding」那条断言因此继续成立）。
  边界如实写进文档：仅 `storage.backend=local` 且同机进程；s3/多机需要共享 DB 锁，届时再说。
- `rag serve` 变 supervisor：默认 N=`min(cpu,4)` 个读进程（共用监听 socket）+ M=`min(2,cpu)` 个 worker，
  子进程异常退出自动重启并计入日志；`--processes 1` 一键退回今天的单进程形态。
- 顺手把 §17.1 那条「多进程写 Lance 未验证」用实验关掉：`tools/bench/lance_commit_contention.py`
  量 N 进程并发写的冲突率/重试/最终一致性，结论写回 DESIGN。
- 内存实测（4 进程 RSS × 每进程表句柄缓存）加进 `rag-bench` 输出——这台机器只有 3GB 可用。

### S5 · 模型调用治理（1d，检索并发的真实上限）
query→vector 带锁 LRU（键=模型+文本哈希，容量 4k）；共享 `httpx.AsyncClient`
（`max_connections=100 / keepalive=20`）替换 `HttpReranker` 的 `urllib` 与每请求新建的 `ChatOpenAI`；
10–20ms 窗口内 embed 合批；`_EMBED_SEMAPHORE` 从硬编码 8 改成配置项，429 走 `Retry-After`+抖动退避；
rerank 连续失败 N 次短路熔断（今天已有降级，缺的是不再打远端）。

合计 **~7.5 人日**。只做一半就停在前两步（S1+S2=2d）。

---

## 5. 契约与前端配合（全部改动仅此四项）

1. `GET /api/health` 变纯 liveness；界面「设置→健康」改调 `/api/health/deep`。
2. `GET /api/stats` 的 `versions` 需 `?versions=true`（数据面板按需请求）。
3. `POST /documents` 的 `wait` 默认改为 `false`（保留 `?wait=true` 完全兼容）；
   `QueueFull` 带 `Retry-After`。
   **落地时与本条有偏差，且偏差是对的**：这里原写「从 503 改 429」，实际保持
   **503 + `Retry-After: 30`**。理由是 429 已经归策略限流
   （`rate_limit_writes_per_min`），503 归容量背压（队列满）——两者对客户端
   含义不同（「你打得太频繁」vs「我在忙，稍后再来」），混用就再也无法从状态码
   判断该降频还是该等。顺带修掉一个真实缺陷：`core/errors.py` 与
   `ingest/queue.py` 各有一个同名 `QueueFull`，抛的是后者（`RuntimeError`），
   于是 `except core.errors.QueueFull` 会静默漏接。现在只有一个类，
   状态码与 `Retry-After` 由异常自己声明。见 `docs/ARCH-AUDIT-2026-10-06.md` §1.1。
4. 入库进度由轮询改 SSE；`/api/jobs/{id}` 保留不动（外部脚本还在用）。
外加一处安全语义要写清：**吊销密钥后最多 10s 仍可能被已缓存的 LRU 放行**（用 SQLite 里的 epoch 号
让各进程立刻对齐，吊销接口顺带推进 epoch）。

---

## 6. 验收门槛（S1 写成断言，之后每次 PR 跑）

| 指标 | 现状（实测） | S2 后 | S4 后 |
|---|---|---|---|
| `/api/documents` p50 @c=1 | 13.4ms | <5ms | <2ms（SQLite 点查 µs 级） |
| `/api/documents` 聚合 @c=32 | ~84 rps | >300 rps | >900 rps |
| `/api/search` p50（假 embedding，不含模型网络） | 75.8ms | <30ms | <15ms |
| 入库吞吐 @c=32 | 2.81 docs/s（越并发越慢） | ≥c=1 的 90%（≈6 docs/s） | ≥12 docs/s |
| 48 次入库后 `jobs` 版本数 | 226（行数 126） | <3× 行数 | ≤ 行数（Lance 只剩 chunks 被写） |
| 48 次入库后读延迟增幅 | +129% | <20% | <10% |
| 聚合读并行度 | 1.28×（线程封顶） | 1.28× | **≈2.5×** |

任何一项红，当次 PR 不算完成。真实模型往返（SiliconFlow embedding/rerank）**至今未测**，
所以 `POST /api/search` 的绝对 P95 要等你允许用真实凭据跑 `rag-bench --real-embed` 才算数——
后端能保证的是「不让它排队、不重复调用、不放大成级联重试」。

---

## 7. 三个最要紧的风险与退路

| 风险 | 处置 | 退路 |
|---|---|---|
| SQLite 迁移搬坏数据 | 双写 shadow + 行数&逐行哈希校验 + 迁移前 `data.bak-<ts>` 快照 | `meta_engine=lancedb` 一个配置项回退 |
| `flock` 与 LanceDB 自身 manifest CAS 叠加出提交冲突 | S4 的实验先量冲突率；写路径已有 `merge_insert` 幂等 | `--processes 1` 立刻退回单进程形态（只影响扩展性，不影响功能） |
| 跨引擎级联的秒级滞后被用户当成 bug | `/health/deep` 报 `pending_outbox` 与最老条目年龄；前端提示「索引更新中」；worker 常驻对账兜底 | 把该操作从 outbox 挪回同步写（flock 下），代价是请求变慢 |

---

## 8. 一句话版本

**LanceDB 留下但只做向量+中文全文（它的 tantivy 实测比 FTS5 快 6–8 倍，换不得）；
OLTP 全部下沉到 SQLite(WAL)（点查 5.68ms → 0.008ms，一次入库 5 个版本 → 0）；
写锁从进程内 `RLock` 换成 `flock`，于是读进程可以 ×N（实测并行度 1.28× → 2.5×）；
先做止血（2 天，收益 3× 且零架构变更），再做分层与多进程（5.5 天，拿到量级）。**
