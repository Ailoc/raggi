# Raggi 并发改造方案（2026-10-05）

> 前置阅读：[PERF-CONCURRENCY-2026-10-05.md](./PERF-CONCURRENCY-2026-10-05.md)（实测基线与根因，本方案每条改动都指向它的某节）。
> 本方案覆盖**阶段一（止血）+ 阶段二（存储分层 / 多进程）**，按里程碑排好依赖、验收门槛与回滚方式。
> 预估耗时以「一个人专注做」为单位，`0.5d` = 半天。

---

## 1. 终态长什么样

```
rag serve  （仍然：一个命令 · 一个端口 · 一个数据目录 · 零外部服务）
│
├─ 元数据引擎   data/raggi.db (SQLite, WAL)        ← 新增：jobs / apikeys / kbs / documents / outbox /
│                                                     idempotency / ratelimit / schema_version
├─ 向量引擎     data/lancedb (LanceDB)             ← 只留：chunks（vector + text_seg + 标量列）
├─ 原文归档     data/files 或 S3（不变）
│
├─ API 读进程 × N（N=cpu，无状态，监听同一端口）
│     鉴权（内存 LRU，0 次 DB 往返）· 检索 · 列表 · 详情 · 写请求只做「SQLite 落单 + 返回」
│
└─ 写入者（LanceDB 的唯一写入方）
      ├─ ingest worker × M（M=min(2,cpu)）：领任务 → 解析 → 分词 → 向量化 → 写 chunks → 索引维护
      └─ API 进程内的「即时写」通道：PATCH/POST/DELETE chunks 等少量同步端点，
          经 **跨进程文件锁** 串行化（见 D2），不改动 API 契约
```

### 数据分层表（谁去哪）

| 数据 | 现在 | 终态 | 迁移动机（实测） |
|---|---|---|---|
| `chunks`（vector + text_seg） | LanceDB | **LanceDB 不动** | 列存 + HNSW + tantivy 正是它的价值 |
| `jobs` | LanceDB | **SQLite** | 一次入库产生 ≈5 个版本/文件；无索引，每次 stage 更新全表扫（§2.6） |
| `apikeys` | LanceDB | **SQLite** | 每请求点查 5.7ms；`touch()` 继续制造版本（§2.3、R7） |
| `kbs` | LanceDB | **SQLite** | 小表高频列表；`kb_id` 无索引 |
| `documents` 元数据 | LanceDB | **SQLite** | 点查/分页/改标题/移动库都是 OLTP；现在 `doc_id` 无索引 → 5.68ms |
| `documents.text`（全文） | LanceDB 行内 | **`data/text/<doc_id>.txt`** | 20MB 正文塞进列存行里，让列表扫描与版本体积一起背锅（§2.6） |
| `Idempotency-Key` | 进程内 dict | **SQLite** | 多进程下必须共享，否则重试漏拦（§6.1） |
| 限流桶 | 进程内 dict | **SQLite（或按进程分摊）** | 同上 |

---

## 2. 三个需要你拍板的决策

### D1：是否放弃「单进程」这条设计前提

- **要**。实测单进程多线程并行度 **1.28×** 封顶，多进程 **2.68×**（§2.4）；HTTP 层 4 进程聚合 177 rps vs 单进程 70 rps。
- 代价：DESIGN §2「单进程单端口」与 §16「不支持 `--workers>1`」要改写。
- **兼容性可以保住**：对外仍是 `rag serve`、一个端口、一个 `data/`；进程由 `rag serve` 自己 fork 出来（supervisor 形态），systemd 单元不变。变的是「运行时不再只有一个 Python 进程」。
- 若**不要**：并发上限锁死在阶段一结束后的 ~400–500 rps（预估），拿不到那 2.5×，也无法把解析/向量化隔离出 API 进程。

### D2：LanceDB 的多进程写入怎么保证安全 —— 两条路，我建议先走 (a)

| | (a) 跨进程文件锁（推荐先做） | (b) 唯一写入者 + outbox 队列 |
|---|---|---|
| 做法 | `LanceStore._lock` 从 `threading.RLock` 换成 `fcntl.flock(data/lance-write.lock)`，临界区不变 | API 进程一律不写 Lance，所有改 chunks 的请求变成 job，由 worker 应用 |
| 契约变化 | **无**（`PATCH /chunks` 仍然同步返回） | 有：`POST/PATCH/DELETE /chunks`、`DELETE /documents`、`DELETE /kbs` 需改成异步或带 `?wait=` |
| 工作量 | `0.5d` | `3–4d` |
| 上限 | 写仍然串行（今天也是串行的），但**读不再被写拖**，且读能 ×N | 写也并行度=1（单 writer），但彻底消除读进程持锁；未来可加多 writer |
| 适用边界 | **`storage.backend=local` 且进程同机**（flock 语义）。s3 / 多机需要另一把锁 | 同左，s3 多机需换共享 DB 锁 |
| 附带验证 | 需要压测确认：`flock` + LanceDB 自身的 manifest CAS 是否会出现提交冲突重试 | — |

> 建议：**M3 用 (a) 立刻拿到多进程读；M4 之后如果写入成为新瓶颈，再上 (b)**。(a) 是 (b) 的安全垫底，两者不冲突。
> 另外把「LanceDB 并发提交冲突率」做成一个实验（`tools/bench/lance_commit_contention.py`），
> 这正是 DESIGN §17.1 待办里那条「多进程写入安全性未验证」——用数据关掉它，而不是继续回避。

### D3：OLTP 迁 SQLite 的表范围

- 建议：**`jobs` + `apikeys` 先迁**（M2 前半），再 `documents` + `kbs` + `text` 落盘（M2 后半）。
  前两张表收益最大（写放大 + 每请求鉴权）、且与 LanceDB 无列关联约束，风险最低。
- `documents` 迁移会牵动 `retrieval/search.py`（`_titles`、`_doc_ids_by_attr`）与 `health` 对账，放在后半程一次做完。

---

## 3. 里程碑

### M0 · 基线资产化（0.5d）——先做，否则后面无法证明「变快了」

诊断报告里的脚本目前散在 `/tmp/raggi_bench/`（重启即失）。先把它们变成仓库资产：

```
tools/bench/
  __main__.py          # rag-bench：只读矩阵 / 入库矩阵 / 归因 / 多进程 四个子命令
  closed_loop.py       # ← bench.py（每线程独立 client，这条纪律写进 README）
  ingest.py            # ← bench_ingest.py（wait / nowait）
  fake_embed.py        # ← OpenAI 兼容假 embedding 服务（默认 8390）
  gen_dataset.py       # ← 合成 N docs / M chunks 数据集，直接写 LanceDB（不依赖外部 API）
  report.py            # 输出 JSON 快照到 tests/perf/baseline-<date>.json，与上次对比
tests/test_perf_gates.py   # pytest -m perf：断言下面这些门槛，默认 skip，带 -m perf 才跑
```

**基线快照（把本报告的数字钉成回归线）**：

| 指标 | 当前实测 | M1 门槛 | M3 门槛 |
|---|---|---|---|
| `/api/documents` p50 @c=1 | 13.4ms | <6ms | <3ms |
| `/api/documents` 聚合 @c=32 | ~84 rps | >300 rps | >900 rps |
| `/api/search` p50（假 embedding） | 75.8ms | <30ms | <15ms |
| 入库吞吐 @c=32 | 2.81 docs/s | ≥c=1 的 90% | ≥12 docs/s |
| 48 次入库后 `jobs` 版本/行数 | 226/126 | <3×行数 | ≤行数（+常量） |
| 48 次入库后读延迟增幅 | +129% | <20% | <10% |

验收：`rag-bench quick` 一条命令出上面这张表；`pytest -m perf` 能红。

---

### M1 · 止血：零架构变更（2d，15 条并成 6 组 PR）

对应诊断报告 §5 全部条目。原则：**不动数据布局，只动「怎么查」和「查几次」**。

**PR-1 `storage/`：索引 + 窄列两步行 + 原生扫描（1d）**
- `tables.ensure_scalar_indexes()` 补：`documents.doc_id`、`jobs.job_id`、`kbs.kb_id`、`chunks.chunk_id`
  （实测点查 5.68 → 3.88ms）。
- `storage/sql.py` 新增标量读收口，走 **原生扫描**（实测 6.76 → 2.26ms）：

  ```python
  def scalar_rows(table, *, cols, where=None, limit=None, offset=None):
      """无向量、无 FTS 的标量读：走 lance 原生 scanner，绕开 lancedb 查询构建器。
      (columns/filter/limit/offset 都已验证在 lance.LanceDataset.to_table 上可用）
      向量/FTS/hybrid 检索仍然走 lancedb —— 那是它值钱的地方。"""
      return table.to_lance().to_table(columns=cols, filter=where,
                                       limit=limit, offset=offset).to_pylist()
  ```
- `repos/documents.list_documents` 改成**窄列两步分页**（这一步别踩坑）：
  Lance 侧**没有 `ORDER BY` 下推**——`LanceDataset.to_table` 的签名只有
  `columns / filter / limit / offset / nearest / batch_size`（已核实），所以直接
  `limit/offset` 下推的分页顺序实际由 fragment 布局决定，写入或 compaction 后会漂移。
  （漂移本身我没实测，但「无排序保证」足以否定直接下推的做法。）正确形态是：
  ① `scalar_rows(cols=["doc_id","created_at"], where=…)` 取回**排序键**（窄列、不带正文），
  Python 排序后切页；② 只对该页的 `doc_id IN (…)` 取回 `LIST_COLS`。
  ③ `total` 用 `count_rows(filter=…)`（实测 0.28ms）。
  比现状（全表取 `LIST_COLS` 含 `meta` 再切片）少 materialize 一个数量级的列，
  而且分页语义第一次变得确定。**M2 之后**这一步整体退化成一条带 `ORDER BY/LIMIT/OFFSET`
  的 SQL，才是真正干净的解。
- `queue.list_jobs`：同法（窄列排序 + 页内取行 + `count_rows` 出 total）；`keys.list_keys` 同理；
  `repos/kbs.list_kbs` 的 `documents` 聚合改成只取 `["kb_id","chunk_count"]` 窄列
  （或缓存 + 写时失效）。

**PR-2 `retrieval/`：消灭 N+1（0.5d）**
- `_titles()` 与 `_contexts()` 合并成**一次**查询：
  `doc_id IN (...) ` + 一次取回所需 ordinal 区间，Python 侧分桶；实测这一项省 25–32ms。
- `_doc_ids_by_attr`（mime/parser_engine 过滤）改走 PR-1 的 `scalar_rows`。

**PR-3 `api/`：鉴权零 DB 往返 + 观测端点降级（0.5d）**
- `keys.py`：`key_hash → row` 带锁 LRU（TTL 5–10s，签发/吊销主动失效）；`has_keys()` 结果缓存；
  `touch()` 改内存累计 + 后台每 30s 批量落盘（apikeys 版本增速从「每密钥每分钟」降到「每 30s 一次」）。
- 拆端点：`GET /health` → 纯 liveness（不查库）；`GET /health/deep` → 现有对账，结果缓存 10s、
  限 write 作用域、`threading.Semaphore(1)` 串行化；`backend.stats()` 目录遍历结果缓存 60s；
  `store.stats()` 的 `versions` 改成 `?versions=true` 可选（实测 `list_versions()` ≈0.17ms/版本，
  当前实例 379 版本 = 64ms）。

**PR-4 `storage/`：版本与碎片治理常态化（0.5d）——长跑变慢的根治**
- 已核实的可用 API：`tbl.compact_files()`、`tbl.cleanup_old_versions(older_than=…, delete_unverified=False)`、
  `tbl.optimize(cleanup_older_than=…)`（后者= 二者合一，本仓库现有 `optimize()` 已经在用它，只是没人调）。
- `schedule_maintenance()` 增加：对**全部 5 张表**周期执行 `optimize(cleanup_older_than=…)` /
  `compact_files()`，触发条件是「版本数或数据文件数 > 阈值」**或**「距上次 > N 分钟」，默认开启
  （现状：只有手工 `rag reindex` 会 `optimize()`，而且**只作用于 chunks 表**）。
  注意 `cleanup_old_versions` 这版**只有时间维度、没有 `retain=N`**，所以「版本数阈值」这一侧只能靠
  `compact_files()` 收敛 fragment、靠 `older_than` 收敛版本；真正想让版本数不涨，**只能少写**——
  这正是 M2 存在的理由（一次入库现在给 `jobs` 造 ≈5 个版本）。
- `ensure_fts_index` 的「n<50000 每次全量重建」改成「积压 ≥ 阈值 **或** 距上次 ≥ 间隔」；
  单块编辑只置 `fts_stale`，由批量重建消费。
- 实测依据：48 次入库产生 226 个 job 版本 / 224 个数据文件（§2.6）；`list_versions()` ≈0.17ms/版本。

**PR-5 并发安全与运行时额度（0.5d）**
- `Embedder._one_cache` → 带锁 LRU（`OrderedDict` + `Lock`，容量可配）；`_touch_cache`、
  `IngestQueue._finished` 加上界。
- `uvicorn` 启动时显式 `loop.set_default_executor(ThreadPoolExecutor(max_workers=cpu*16))` +
  `anyio` limiter 提额。**如实说明**：实测本机默认值 `min(32, cpu+4)=8` 线程，且 8→128 对入库坍塌
  **毫无影响**——这条不是当前瓶颈，是「不知道什么时候会踩的暗坎」，属于便宜的保险。
- `merge_sub` 不再就地 `setattr` 全局 `settings`，改为构造新模型后原子替换引用。

**PR-6 前端与静态资源（0.5d）**
- `/assets/*` → `public, max-age=31536000, immutable`（Vite 已是 hash 文件名），`index.html` 保持 no-cache；
  现状 `api/__init__.py:203` 对所有非 `/api` 一律 `no-cache`。
- 入库进度从「700ms 轮询 `/api/jobs`」改为一条 SSE（复用 `/answer/stream` 的实现风格）。

**M1 验收**：M0 表格里的「M1 门槛」全绿；`pytest -q` 现有 300+ 项不红（尤其 `test_concurrency.py` 的
乐观并发断言、`test_perf.py` 的锁范围断言）。
**回滚**：PR 之间无耦合，任一条可单独 revert。

---

### M2 · MetaStore：SQLite(WAL) 承载 OLTP（3–4d，含迁移）

**新增**

```
rag/storage/meta.py        # open_meta_store(settings) -> MetaStore（唯一允许 sqlite3.connect 的地方）
rag/storage/meta_schema.py # 表定义 + schema_version + 迁移列表
rag/storage/repos/{jobs,apikeys,kbs,documents}.py   # 仓储签名不变，内部改走 meta
rag/core/textstore.py      # data/text/<doc_id>.txt 的原子读写（documents.text 落盘）
```

**连接纪律**（多进程/多线程都安全的前提）

```python
class MetaStore:
    """SQLite(WAL) 元数据引擎。连接按线程隔离——sqlite3 连接不是线程安全的。"""
    def __init__(self, path: Path):
        self._local = threading.local()
        self.path = path
    def _conn(self):
        c = getattr(self._local, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=5.0, isolation_level=None,
                                check_same_thread=True)
            c.row_factory = sqlite3.Row
            c.executescript("""
                PRAGMA journal_mode=WAL;      -- 多读单写；读者不被写者阻塞
                PRAGMA synchronous=NORMAL;    -- WAL 下安全且快
                PRAGMA busy_timeout=5000;
                PRAGMA foreign_keys=ON;
                PRAGMA cache_size=-16000;
            """)
            self._local.c = c
        return c
```

要点：
- **写并发**：`BEGIN IMMEDIATE` 包裹「读—改—写」，配合 `busy_timeout`；这正是 jobs 状态机的正确载体
  （取代今天的 `store.write_lock()` 在 OLTP 路径上的滥用）。
- **jobs 领取**（为 M4 的多 worker 预留）：

  ```sql
  UPDATE jobs SET stage='running', worker=?, started_at=?
  WHERE job_id = (SELECT job_id FROM jobs WHERE stage='queued'
                  ORDER BY started_at LIMIT 1)
  RETURNING job_id;
  ```
- **跨表一致性**（这是把 §16 两条风险一起解掉的地方）：`documents` + `jobs` + `outbox` 在**同一个
  SQLite 事务**里提交；chunks 的实际变更由 `outbox` 描述、写入者应用并标记完成 —— 幂等可重放。
  今天「删旧块 → 写新块」两次独立提交留下的 `0 分块但 status=ready`、孤儿 chunk、
  `count_mismatch` 这一整类问题从此有机制保证。
- **检索层适配**：`retrieval/search.py` 的 `_titles` / `_doc_ids_by_attr` 改查 SQLite（一次 `IN` 查询，
  预估 <0.1ms）；`health` 的 `actual_by_doc` 聚合仍来自 Lance（那是真源），
  `stated` 来自 SQLite，对账语义不变。
- **Ctx 签名**：`Ctx(settings, store, registry, backend=None)` → 增 `meta=None` 关键字参数并**默认从
  `settings.data_dir` 自建**，这样 `tests/test_architecture.py` 里手搭 `Ctx(...)` 的既有用法不被打断。
- **架构边界测试同步更新**：`test_no_lancedb_connection_outside_storage` 旁边加一条
  `sqlite3.connect` 只允许出现在 `rag/storage/`（沿用同一条纪律，而不是放宽）。

**迁移：双写 → 切读 → 收尾（每步可退）**

配置开关（`config.toml`，默认随版本升级）：

```toml
[storage]
meta_engine = "lancedb"   # lancedb | shadow | sqlite
```

| 步骤 | 动作 | 回滚 |
|---|---|---|
| ① `rag migrate --engine sqlite` | 建 SQLite 表；把 4 张 OLTP 表 + `documents.text` 全量搬过去；**逐表校验行数 + 逐行 `hash` 比对**，不一致就报错退出 | 无写入，直接删 `raggi.db` |
| ② `meta_engine=shadow` | 写：Lance 为主、SQLite 影子写；读：仍走 Lance；后台对账把差异打到 `/health/deep` 与日志 | 改回 `lancedb` |
| ③ `meta_engine=sqlite` | 读切 SQLite，写仍双写（保留一条退路） | 改回 `shadow` |
| ④ 收尾（下个版本） | 停止双写，`apikeys/jobs/kbs/documents` 的 Lance 表转为**只读归档**或直接删 | 有 `data.bak-<ts>` 快照 |

- 迁移前自动做 `data/` 快照（DESIGN §14 已有备份脚本，复用）。
- **启动守卫**：`schema_version` 落后且 `meta_engine != lancedb` → 拒绝启动并给出 `rag migrate` 指令，
  而不是带病运行（与 `_ensure_cols` 现在「宁可启动失败」的纪律一致）。

**M2 验收**（预估，需实测确认）：点查 5.68ms → **<0.05ms**；`/api/documents` p50 <2ms；
一次入库产生的 Lance 版本从 ≈5 降到 **1**（只剩 chunks）；`apikeys` 表不再随流量增长版本。

---

### M3 · 多进程：读进程 ×N + 跨进程写锁（1.5–2d）

1. `LanceStore.write_lock()` 换成 **文件锁**（D2 方案 a）：

   ```python
   class FileLock:
       """同机跨进程串行化 LanceDB 写入与索引维护。
       仅限 storage.backend=local；对象存储/多机需要共享 DB 锁（见 README 边界）。"""
       def __init__(self, path): self._fd = open(path, "a+")
       def __enter__(self): fcntl.flock(self._fd, fcntl.LOCK_EX); return self
       def __exit__(self, *exc): fcntl.flock(self._fd, fcntl.LOCK_UN)
   ```
   临界区范围**保持今天的划分不变**（去重检查+写库 / 索引维护），因此
   `test_perf.py` 那条「写锁不覆盖 embedding」的断言仍然成立。
2. `rag serve` 变成 supervisor：`--processes auto`（默认 `cpu`）用 `multiprocessing` 起 N 个
   uvicorn 读进程，共用监听 socket；worker 进程 M 个；任一子进程异常退出 → supervisor 重启并计入日志。
3. **实验关掉 §17.1 那条待办**：`tools/bench/lance_commit_contention.py` 跑「N 进程并发写同一张表」，
   量冲突率、重试次数、最终一致性。把结论写回 DESIGN §16。
4. 鉴权 LRU / 限流桶 / 幂等键的多进程语义：LRU 允许每进程各持一份（只影响 TTL 内的可见性，如实写进文档）；
   幂等键与限流走 M2 的 SQLite（跨进程一致）。
5. 端口与部署不变：systemd 单元、`rag serve --port`、签名 URL（`UrlSigner` 读的是磁盘密钥文件，
   多进程同目录即可）都照旧。

**M3 验收**：M0 表格「M3 门槛」；实测预期 —— 聚合读吞吐 ≈ 单进程 ×2.5（本报告的 2.68× 是上界，
桌面负载会吃掉一部分）。

---

### M4 · 流水线出核（1–2d，可选但推荐）

把「CPU 重的解析 + 向量化」从 API 进程彻底移出：worker 进程池按 `jobs` 表领取任务，
API 进程只做落单与查询。收益：`wait=true` 时代的「一条连接占住一个服务线程 150ms–8s」消失，
入库不再挤占读路径的 GIL。
- 契约影响：`POST /documents?wait=true` 保留（内部改为等待 job 完成事件，而非占线程），
  `wait=false` 成为**默认**并带 `Retry-After` 背压语义。
- `ingest_workers` 默认从 2 提到 `min(4, cpu)`（M1 的表治理做完后，锁不再是首要限制因素）。

---

### M5 · 模型调用治理（1–2d，检索并发的真实上限）

1. **query→vector 带锁 LRU**（键=模型+文本哈希，容量 4k–32k）；
2. **共享 `httpx.AsyncClient`**（`Limits(max_connections=100, max_keepalive_connections=20)`）
   替换 `HttpReranker` 里的 `urllib`（每请求新建 TCP、无 keep-alive）与 `registry.chat()` 每请求新建
   `ChatOpenAI`（连带新建其连接池）；
3. **合批**：10–20ms 窗口内的 embed 请求聚成一个 batch；
4. **闸可调**：`_EMBED_SEMAPHORE` 从硬编码 8 改为配置项，按 provider 限额设定；
   429 走 `Retry-After` + 抖动退避（§12.1 记录过级联重试风暴）；
5. **rerank 熔断**：连续失败 N 次后短路降级（今天已有降级，缺的是不再打远端）；
6. 短 TTL 结果缓存：`(query, filters, top_k, mode) → result`，按 `kb_id` 写时失效。

> 这一节全部数字都**依赖真实模型服务**，本方案刻意不预估；M0 的假 embedding 服务只能证明
> 「后端不再排队」，证明不了外部往返。上线前用 `rag-bench --real-embed` 单独量一轮。

---

## 4. 对外契约变化清单（前端 / API 调用方需要知道的）

| 变化 | 影响 | 处理 |
|---|---|---|
| `GET /api/health` 变纯 liveness | 界面 Settings→健康 页拿不到对账字段 | 前端改调 `/api/health/deep`（M1 同期改） |
| `GET /api/stats` 的 `versions` 需 `?versions=true` | 数据面板少一列 | 面板按需请求，默认不显示 |
| 写锁跨进程后 `max_upload_mb`/队列语义不变 | 无 | — |
| `POST /documents` 默认 `wait=false`（M4） | 老脚本以为同步返回 | 保留 `?wait=true`；OpenAPI 与 ApiDoc 页同步改说明 |
| `documents.text` 移到 `data/text/` | 直接读 Lance 表的外部脚本会少一列 | `GET /documents/{id}` 契约不变；CHANGELOG 标注 |
| 多进程后鉴权 LRU 有 ≤10s 吊销延迟 | 安全语义变化 | 吊销接口顺带广播失效（同机可写一个 epoch 号进 SQLite，读进程比对） |

---

## 5. 风险与回滚

| 风险 | 概率 | 缓解 | 退路 |
|---|---|---|---|
| SQLite 迁移搬坏数据 | 中 | 双写 shadow + 行数&哈希校验 + 迁移前快照 | `meta_engine=lancedb` 一键回 |
| `flock` 与 LanceDB 内部 manifest CAS 叠加出提交冲突 | 中 | M3-③ 的实测实验先量冲突率；写路径已有 `merge_insert` 幂等 | 退回单写进程（`--processes 1` 只影响扩展性不影响功能） |
| 多进程让内存翻倍（7GB 机器） | 高 | N 默认 `min(cpu, 4)`；每进程按需打开表句柄已有缓存；M0 的 bench 加 RSS 采样 | 降 N；worker 与 API 分时 |
| 前端轮询改造引入回归 | 中 | 分两步：先做 M1 的服务端治理（收益 90% 来自这里），再做 SSE | 保留轮询路径不删 |
| 版本清理（`cleanup_old_versions`）误删仍在读的版本 | 中 | 这版 API **只有 `older_than` 时间维度**（无 `retain`）：`older_than` 取远大于最长请求/检索时长的值（如 1 小时），`delete_unverified=False` 保持默认；读者走 MVCC 快照，只依赖尚未回收的版本 | 关掉 cleanup，只留 `compact_files()`（碎片收敛与版本回收是两件事） |
| 阶段一收益被误当成「已经够了」 | 高 | M0 的门槛表把「阶段二才拿得到的数字」写清；每次 PR 报告差值 | — |

---

## 6. 顺序与工作量

```
M0 (0.5d) ──► M1 (2d) ──► M2 (3–4d) ──► M3 (1.5–2d) ──► M4 (1–2d) ──► M5 (1–2d)
  基线资产     止血         SQLite 分层     多进程读        worker 出核     模型治理
                              ▲                ▲
                     不依赖 M1，可并行准备   依赖 M2（幂等/限流要跨进程）
```

合计 **8.5–12 人日**。如果只能做一半，做 **M0+M1+M2 前半（jobs+apikeys）**——
这三块拿到的是「不再越用越慢」+「每请求固定开销从 11ms 降到 ~3ms」，是收益/风险比最好的部分。

---

## 7. 需要写回文档的地方

- `docs/DESIGN.md`：§2 目标（单进程 → 单命令/单端口/单数据目录）、§12 性能设计表
  （补「OLTP/OLAP 分层」「跨进程写锁」「compaction 常态化」）、§16 风险表
  （「多进程写入未验证」→ 用 M3-③ 的实测结论替换）、§17.1 待办相应移除。
- 本方案的两处「文档与实现不符」也顺手改掉：§12 声称的 `optimize()/cleanup_old_versions()`
  后台触发（实际只手工）与「解析放 `ThreadPoolExecutor(min(4,cpu))`」（实际在队列 worker 线程内联）。

---

## 8. 引擎选型复核：LanceDB 到底是不是阻碍（2026-10-05 追加实测）

结论先说：**LanceDB 是「每请求固定开销」和「越用越慢」的载体，但它不是向量/全文检索的阻碍，
换了它也拿不到并发量级**。三项都在本机实测。

### 8.1 同一批 OLTP 形态：LanceDB vs SQLite(WAL)

| 形态 | LanceDB（现状） | SQLite（内存库实测） | 倍数 |
|---|---|---|---|
| 点查 `doc_id` | 5.68ms（无索引）/ 3.88ms（加 BTree） | **0.008ms** | 470–700× |
| 列表 `WHERE kb_id ORDER BY created_at LIMIT 50` | 6.76ms（全表 materialize 后 Python 切片） | **0.100ms** | 68× |
| `count(*) WHERE kb_id=?` | 1.19ms | **0.030ms** | 40× |
| `GROUP BY kb_id` 聚合（health 那类） | ≈3.5ms 起（全列扫 + to_pylist） | **0.653ms** | 5× |
| 领取一个排队任务（`UPDATE…SELECT LIMIT 1 RETURNING`） | 不支持（得读—改—写两步 + 全局锁） | **0.009ms** | — |
| 一次小写的副作用 | **≈5 个新版本 + 5 个数据文件** | 一个 WAL 页追加 | — |

⇒ 这一层**没有任何一条靠「换更快的向量库」能解决**：它是「用 OLAP 引擎做 OLTP」的错配，
解法是 M2（数据分层），不是替换 LanceDB。

### 8.2 向量通道：能不能干脆不要 ANN 引擎？

| 方案 | 20k 条 | 100k 条 | 备注 |
|---|---|---|---|
| LanceDB HNSW（现状） | 9.34ms | 预算内（§12 P95<150ms） | 索引维护有成本，但已被实测证明不贵（FTS 20k 重建 0.15s） |
| numpy 暴力 cosine + top-50 | **7.65ms**（82MB） | **23.3ms**（410MB） | 精确、零索引、零版本膨胀；代价是常驻内存与 float32 体积 |

⇒ 单看向量，**≤100k chunk 时暴力检索甚至略快于现在的 HNSW**。所以「向量引擎」不是必须换的东西，
但它确实**不是不可替代**——如果哪天真要把 LanceDB 换成「一个 mmap 的向量数组文件」，向量这半边是可行的。

### 8.3 全文通道：SQLite FTS5 **不能**替换 LanceDB 的 tantivy（这是关键否定）

FTS5 建库 20000 行（沿用现有 jieba 预分词列 + `tokenize='unicode61'`）4.9s、5.0MB，但查询成本
**与命中总数成正比**（要先给全部命中算 bm25 再排序取 top-50），而 tantivy 有 WAND 式提前终止：

| 查询 | 命中总数 | SQLite FTS5 | LanceDB FTS（现状） |
|---|---|---|---|
| 稀有词 `alpha42` | 1 | 0.04ms | ~4ms |
| `测试`（高频单词） | 20000 | **22.1ms** | ~4ms |
| `并发 AND 性能` | 20000 | **26.4ms** | 3.2ms |
| 四词 AND | 20000 | **31.9ms** | ~4ms |
| 高频 AND 稀有 | 1 | 1.26ms | ~4ms |

⇒ **「全部收进 SQLite」这条看起来很优雅的路，会在中文高频词上把检索打慢 6–8 倍**。
本仓库的检索质量恰恰依赖中文关键词通道（DESIGN M2/M7 的记录），因此**FTS 必须留在 LanceDB**。

### 8.4 候选技术路径（针对本项目的约束：4 核无 GPU · 零外部服务 · 极简单机）

| 路径 | 能买到什么 | 代价 | 建议 |
|---|---|---|---|
| **P1 保留 LanceDB（只当向量+FTS）+ OLTP 去 SQLite** | 点查 500×+、写放大归零、跨表事务、M2 全部收益 | 迁移 3–4d，两套引擎的边界要靠仓储层钉住 | ✅ **本方案采用** |
| P2 全部收进 SQLite（元数据+FTS5+sqlite-vec） | 单引擎、ACID、运维最简 | §8.3：中文高频词 22–32ms（**回归**）；sqlite-vec 的 vss 索引在 100k+ 上不成熟 | ❌ 全文通道过不去 |
| P3 Postgres + pgvector(+pg_search) | 真并发控制、真事务、可扩展到千万级向量、SQL 表达力 | **要给这台机器加一个服务**，与「极简单机/零外部依赖」冲突（且本机现在没有 pg 二进制） | 只在「多人协作 / 多机 / 需要横向扩容」时作为迁移目标 |
| P4 Qdrant / Weaviate / Milvus | 向量侧独立扩展、payload 索引 | 加服务 + 元数据仍需另一个真源（双写一致性）；中文 FTS 还得自建 | ❌ 对本项目是净增复杂度 |
| P5 只做 M1+M3（不动存储布局） | 3–5×，最快见效 | 「每请求 7ms 地板」和「越用越慢」仍在 | ✅ 作为 M1 阶段的形态，不是终点 |

### 8.5 天花板归属：换引擎买不到的东西

**并行度不在存储引擎里。** 同一份 LanceDB 读：同进程多线程 1.28× 封顶，多进程 2.68×（诊断报告 §2.4）。
所以顺序上 M2（分层）与 M3（多进程）是**正交且都要**的：
只做 M2 → 每请求从 11.3ms 降到 ~2ms，但仍然是「一个 GIL 在干活」；
只做 M3 → 拿到 ×2.5，但每个请求仍然白付 7ms，且写入继续把表搞得越来越慢。

### 8.6 一个尚未覆盖的廉价实验点

本仓库把 lancedb 钉在 `>=0.39,<0.40`（`pyproject.toml`）。**每查询 ~6.8ms 的固定开销有可能是这个
版本 Python 侧查询构建器的实现问题**（同一份数据走 `to_lance().to_table()` 只要 2.26ms，说明
Rust/磁盘侧不慢）。建议在 M0 之后加一条 `rag-bench scan-paths`，把同一组测量在升级后的
lancedb/pylance 上重跑一遍——**如果固定开销随版本下降，M1 的部分改动可以省掉**。
这条属于「未验证的可能性」，不能作为决策依据。
