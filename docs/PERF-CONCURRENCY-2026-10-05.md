# Raggi 后端并发性能诊断与提升路线（2026-10-05）

> 目标：把「优秀的并发访问性能 / 极致并发」当成一个可度量、可验收的工程问题来解。
> 本文所有数字都是**本机实测**（4 核 / 7GB LXC，桌面环境常驻 load≈2.3），
> 复现脚本在 `/tmp/raggi_bench/`（见 §9）。**预估**均单独标注。

---

## 0. 一句话诊断

系统当前的并发上限不是由「FastAPI 不够快」决定的，而是由三件事决定的：

1. **每一个 API 请求都要付一次 LanceDB 查询的固定开销（≈7ms）**，而整个 ASGI 框架只要 1.8ms；
2. **把 OLTP 表（jobs / apikeys / documents / kbs）跑在 OLAP 列存引擎上**，于是每次小写都产生
   一个新版本 + 一个新数据文件，写入越多、读越慢（实测 48 次入库后读延迟翻 2.3 倍）；
3. **单进程 + GIL**：同进程多线程实测只有 1.28× 并行度，而多进程能做到 2.5–2.7×；
   架构上又被进程内 `RLock` 锁死在「只能 1 个 worker」（DESIGN §16）。

结果就是：读接口吞吐卡在 **80–150 rps** 且**完全不随并发提升**（延迟线性恶化），
写接口**并发越高吞吐越低**（6.5 → 2.8 docs/s，−57%），检索接口 **≈20 rps** 封顶。

---

## 1. 测量方法（为什么这些数字可信）

| 要点 | 做法 |
|---|---|
| 不碰真实数据 | 所有写入实验都在 `/tmp/raggi_*` 的 `data/` 副本上做；`data/` 未被修改 |
| 隔离外部模型 | 自建 OpenAI 兼容假 embedding 服务（`fake_embed.py:8390`），瞬时返回确定性随机向量，**未调用付费外部 API** |
| 避开已知陷阱 | 闭环压测用**每线程独立 `httpx.Client`**（DESIGN §12.1 记录过共享 client 导致误判的教训） |
| 规模可控 | 合成数据集：2000 文档 / 20000 分块 / 1024 维随机单位向量 + 与生产一致的索引（HNSW/FTS/BTree） |
| 归因到函数 | 同机另起「只减不加」的最小 FastAPI app（`framework.py`），把框架开销与存储开销分开量 |

一处诚实的限制：**真实 embedding / rerank 的网络往返没有测量**（避免调用付费服务）。
本文用假服务把这部分剥离出来，因此「检索接口 75.8ms」是**不含真实模型延迟的下界**。

---

## 2. 实测基线

### 2.1 端到端读性能（20k chunks / 2000 docs 数据集）

| 端点 | c=1 p50 | 吞吐上限 | c=32 p50 | 说明 |
|---|---|---|---|---|
| `GET /api/jobs`（空表） | **6.9ms** | 152–192 rps | 190ms | 最干净的「每请求固定开销」样本 |
| `GET /api/documents?limit=50` | 13.4ms | 77–115 rps | 355ms | 全表取回后 Python 分页 |
| `GET /api/kbs` | 12.3ms | 85–100 rps | 317ms | 2 次全表扫（kbs + documents 聚合） |
| `GET /api/health` | 40.7ms | 26–44 rps | **1076ms** | 全量扫 chunks ×2 + `list_versions()` |
| `GET /api/stats` | 95.7ms | **4.5–9.6 rps** | **5053ms** | 递归遍历 data 目录 + 列版本 |
| `GET /api/search`（hybrid，假 embedding） | 75.8ms | **≈20 rps** | 778ms(c=16) | 不含真实模型网络延迟 |

**读路径的核心症状：吞吐从 c=4 到 c=32 完全不涨，延迟却按 1∶1 增长。**
这是「一次只有一个人干活」的排队特征——服务端的实际并行度 ≈ 1。

### 2.2 单次开销归因（同机、同法）

```
最小 FastAPI + uvloop + orjson，返回 7 条 dict      p50 =  1.8ms   620 rps   ← 框架地板
上面 + 一次 lancedb search().select().to_list()     p50 = 11.1ms    38 rps   ← 真实形状
```

**一次 LanceDB 查询 ≈ 9.3ms，占了 85% 的请求时间。**
Pydantic 响应模型（含 `extra="allow"`）实测**不是**瓶颈（1.4–1.9ms，与裸返回同阶）。

### 2.3 存储层函数级测量

| 操作 | p50 | 备注 |
|---|---|---|
| `documents.search().select(10列).to_list()`（2000 行） | 6.76ms | `to_arrow()` 6.38ms → Python 转换只占 0.4ms |
| `to_lance().to_table(columns=)`（同一份数据） | **2.26ms** | 换原生扫描路径 **3.0×** |
| `search().where("kb_id=...").limit(50).to_list()` | 6.20ms | `limit` 不是成本，固定开销才是 |
| `count_rows()` | 0.28ms | 便宜，可放心用 |
| 点查 `doc_id`（**无索引**） | 5.68ms | `documents.doc_id` / `jobs.job_id` / `kbs.kb_id` **都没索引** |
| 点查 `doc_id`（加 BTree 后） | 3.88ms | 只降 31% —— 固定开销仍在，换引擎才是解 |
| 向量检索 HNSW（20k） | 9.34ms | |
| FTS 检索（20k） | 3.63ms | |
| hybrid 检索（20k，不含 embedding） | 12.94ms | |
| `chunks.list_versions()`（172 版本） | 24.63ms | ≈0.17ms/版本，**线性** |
| `jobs.list_versions()`（379 版本） | 63.87ms | |
| `backend.stats()`（递归遍历 17MB 目录） | 64.48ms | 每次 `/api/stats` 都跑 |
| `ThreadPool` 提交+回收一次往返 | 0.043ms | `to_thread` 本身极便宜 |

### 2.4 并行度：线程 vs 进程（同一台机器、同一份数据）

```
同进程多线程  to_list ops/s： 1线程 143.6 | 2 177.6 | 4 184.5 | 8 182.7   → 封顶 1.28×
多进程        to_list ops/s： 1进程 160.4 | 2 265.6 | 4 430.3 | 8 401.1   → 4 进程 2.68×
HTTP 层多进程（总连接固定 16）：1 进程 69.6 rps/p50 203ms | 2 进程 121.2/117ms | 4 进程 177.0/72ms → 2.54×
```

**结论：这台机器的核是够的，是「Python 单进程」把它们关在了一起。**

### 2.5 写路径：并发越高越慢（坍塌曲线）

`ingest_workers=2`、假 embedding、每份配置都用全新数据副本：

| 客户端并发 | 吞吐 (docs/s) | p50 |
|---|---|---|
| 1 | **6.52** | 133ms |
| 4 | 5.40 | 746ms |
| 16 | 3.39 | 4196ms |
| 32 | **2.81** | **8023ms** |

> **加并发反而掉了 57%。** 同一台机器上把默认线程池从 8 提到 128 线程：
> 6.57 / 5.28 / 3.46 / 2.73 —— **毫无变化**。所以这不是线程池 starvation，而是**写放大 + 锁 + 排队**。
>
> 改成 `wait=false`（提交即返回）：48 个任务 6.1s 全部落地（≈**7.9 docs/s**），
> c=32 时前 32 个请求以 25.5 rps 提交完，随后 `503 入库队列已满（32/32）`。
> 即：**服务本身有 ~8 docs/s 的能力，是「同步等待 + 越写越慢的表」把它压成了 2.8。**

### 2.6 写放大 → 读放大的因果链（本报告最重要的一条）

48 次入库前后，同一份数据集的文件系统实况：

| 表 | 版本数 before → after | 数据文件数 before → after |
|---|---|---|
| `jobs` | 2 → **226** | — → **224** |
| `documents` | 4 → **68** | 1 → **65** |
| `chunks` | 22 → 58 | 1 → 33 |

即 **一次入库 ≈ 5 个 job 版本/文件 + 1.3 个 document 版本/文件**。
数据量只涨 2.4%（2000→2048 文档），但同一读端点的延迟：

| 端点 | 48 次入库前 | 之后 | 变化 |
|---|---|---|---|
| `GET /api/documents` | 13.4ms | **30.7ms** | **2.3×** |
| `GET /api/health` | 40.7ms | **89.2ms** | **2.2×** |

**这就是「用得越久越慢」的机制**，而且它同时打击读和写。
用户当前真实实例正好是这个规律的样本：`jobs` 78 行却有 **379 个版本**（`list_versions()` 要 64ms），
`documents` 120 版本、`chunks` 175 版本。
代码侧的放大器：`optimize()/cleanup_old_versions` **只在手动 `rag reindex` 时跑，且只作用于 chunks 表**；
`schedule_maintenance()` 不做任何 compaction / 版本清理。

### 2.7 一次检索里藏了 11 次数据库查询

`GET /api/search?mode=hybrid`（top_k=8，窗口=1）实测分解：

```
auth: apikeys.count_rows()                 0.3ms
hybrid 检索（向量+FTS+RRF）               12.9ms
_titles()      1 次 documents 扫描          6.8ms
_contexts()    **每个命中文档 1 次查询**    实测差值 25ms（上界随命中文档数线性，8 篇 ≈ 25–55ms）
框架 + 序列化                                ~2ms
                                    合计 ≈ 50.9（关上下文）/ 75.8（开上下文）ms
实测：include_context=false → 75.8ms 掉到 50.9ms；mode=fts → 26.2ms
```

`search.py` 里 `_contexts` 的注释写着「避免 N+1」，实际是**按 doc_id 合并后仍然每文档一次查询**——
它只把「每分块一次」优化成了「每文档一次」，没有真正做到一次取回。

---

## 3. 根因清单（按杠杆从大到小）

| # | 根因 | 证据 | 影响面 |
|---|---|---|---|
| **R1** | **OLTP 数据放在列存 + MVCC 引擎里**：jobs/apikeys/kbs/documents 是「点查 + 高频小写」画像，LanceDB 每次写 = 新版本 + 新 fragment，且**没有跨表事务** | §2.6、§2.3 | 读放大、写放大、版本无界增长、health 的 orphan/mismatch 类问题 |
| **R2** | **单进程结构性封顶**：进程内 `RLock` + 「跨进程写未验证」→ 只能 1 worker → 所有 Python 工作共享一个 GIL | §2.4、DESIGN §16、§2 目标「单进程单端口」 | 读、写、解析全互相饿 |
| **R3** | **每次查询 ~7ms 固定开销 + 无 limit 下推 + 无索引点查** | §2.2、§2.3、`list_documents` 取全表再切片 | 每个端点的地板 |
| **R4** | **检索的 N+1 上下文回捞** | §2.7 | 最热端点的 33–70% 时间 |
| **R5** | **写路径的锁与策略**：一把 `RLock` 同时保护「写入」和「全量索引重建」；`ensure_fts_index` 在 n<50000 时**每次入库都全量重建**；入库 worker **默认只有 2**（`ingest_workers`，可配）；`wait=true` 是默认，于是每个入库请求在整个流水线期间同步占住一条服务端线程 | §2.5、`tables.py:311-333`、`queue.py:34`、`_common.py:56` | 入库吞吐与尾延迟 |
| **R6** | **观测型端点是「重扫描」却与业务端点同池**：`health` = 全量扫 chunks ×2 + `list_versions()`；`stats` 额外递归遍历 data 目录。目前只在界面上按需调用（Settings→健康 / 数据面板），不是轮询热点——但任何脚本、监控或多标签页并发打开都会把它打成服务（实测 c=32 时 p50 **1–5s**、吞吐 4.5 rps）。另外 `GET /api/jobs` 全表扫 + Python 排序，而入库期间前端每 4s（队列看门狗）/ 700ms（单条提交后轮询）会打它一次 | §2.1、§2.3、`web/src/lib/queue.svelte.ts:17`、`web/src/lib/jobs.ts:44` | 一个重端点能把 8 条线程全部占住 |
| **R7** | **每请求固定成本里的鉴权往返**：`asyncio.to_thread(authenticate)` 每请求 ≥1 次 `count_rows`，配密钥后 +1 次点查 +1 次 `touch` 写（apikeys 也被写放大污染） | `api/__init__.py:173`、`auth.py:103,146`、`keys.py:192` | 每请求 0.3–7ms + apikeys 版本增长 |
| **R8** | **静态资源 `Cache-Control: no-cache`** 且走 `StaticFiles`（受 anyio 默认 40 token 线程限制） | `api/__init__.py:203-204` | SPA 每次访问都重新验证全部资源 |
| **R9** | **并发共享状态的隐患**（高并发下的正确性风险）：`Embedder._one_cache` 是无锁 dict 且「pop 第一个」不是 LRU；`_touch_cache` / `_finished` 无界增长；`merge_sub` 就地改全局 `settings` 子模型 | `models/embeddings.py:75,94-103`、`keys.py:51`、`queue.py:83`、`_common.py:83-94` | 缓存失效行为不可预期；配置读到半新半旧 |
| **R10** | **模型调用无连接治理**：`HttpReranker` 用 `urllib`（每请求新建 TCP、无 keep-alive）；`registry.chat()` **每次调用都新建 ChatOpenAI**（含其 httpx 连接池）；`_EMBED_SEMAPHORE(8)` 硬编码 | `models/rerank.py:111-117`、`registry.py:76-77`、`pipeline.py:149` | 检索/问答的延迟与外部服务限流风险 |

---

## 4. 「极致并发」在这台机器上意味着什么（验收目标）

不承诺玄学数字，给可断言的目标（4 核 / 无 GPU / 桌面环境常驻 load≈2.3）：

| 维度 | 现状（实测） | 阶段一后（预估） | 阶段二后（预估） |
|---|---|---|---|
| 管理类读吞吐（documents/kbs） | 80–115 rps，c↑ 无收益 | 350–500 rps，随 c 近线性 | **1200–2000 rps**（N 读进程） |
| 读 p50 @ c=32 | 287–355ms | 60–90ms | **<30ms** |
| 检索吞吐（本地 embedding） | ≈20 rps | 60–80 rps | **200–300 rps** |
| 检索 p50（不含模型网络） | 75.8ms | 20–25ms | <10ms（含结果缓存则更低） |
| 入库吞吐 @ c=32 | **2.81 docs/s（越并发越慢）** | ≥6 docs/s（不再坍塌） | **15–25 docs/s**，随核数近线性 |
| 长跑稳定性 | 48 次写入后读慢 2.3× | 版本可控 | **恒定**（compaction 常态化） |

> **一个必须先说清的外部天花板**：真实部署里 `POST /api/search` 的 P95 主要由
> SiliconFlow embedding（+rerank）的网络往返决定（约 200–600ms/次，**本次未测**）。
> 后端能做到的是：不让它排队、不重复调用（缓存）、不把它放大成级联重试。
> **在检索这条路上，「极致并发」= 模型调用的治理（缓存 + 连接池 + 合批 + 闸），而不是更多线程。**

---

## 5. 阶段一：止血（1–2 天，零架构变更，不动数据目录结构）

> 目标：读 3–5×，写路径不再坍塌，长跑不再变慢。全部改动局限在
> `storage/`、`retrieval/`、`api/`、`core/` 的既有边界内。

**A. 把每次请求的固定 7ms 砍掉**

1. **补标量索引**（`tables.ensure_scalar_indexes`）：`documents.doc_id`、`jobs.job_id`、
   `kbs.kb_id`、`chunks.chunk_id`、`documents.status`。现在这四张表几乎每个点查都是全表扫。
2. **limit/offset/排序下推**：`list_documents` 改成 `.offset().limit()` + `count_rows(filter=…)` 单独取
   total；`list_jobs` 加 `where` + `limit` 下推并停止 Python 端全表排序；`list_kbs` 的聚合改成一次带
   `group by` 的下推查询或直接缓存。
3. **只读路径改走原生扫描**：对「无向量、无 FTS」的标量读（`get_document`、`stored_file`、
   `find_by_hash`、`_titles`、`_contexts`），用 `tbl.to_lance().to_table(columns=…, filter=…)`
   ——实测 6.76 → 2.26ms。向量/FTS 检索仍走 lancedb 查询构建器。
   （落点：`storage/sql.fetch_rows` 加一个 `scalar_rows(table, cols, where, limit, offset)` 收口，
   仓储层调用它，架构边界测试仍成立。）

**B. 消灭检索的 N+1**

4. `_contexts` + `_titles` **合并成一次查询**：`doc_id IN (...) AND ordinal BETWEEN lo AND hi`
   一次取回，Python 侧按 doc 分桶（数据量只有 top_k×窗口）。实测关掉上下文回捞即从 75.8ms 降到
   50.9ms（−25ms），而 `_titles` 还要再花一次 ~6.8ms 的扫描——合并后这两次查询一起消失。
   纯 FTS 通道的下界是 26.2ms，说明这条路径上仍可期望 ~25–35ms（不含模型网络）。

**C. 鉴权与观测端点**

5. **鉴权零查库**：`key_hash → (row, expire_at)` 的**带锁 LRU**（TTL 5–10s，签发/吊销时主动失效）；
   `has_keys()` 结果同样缓存；`touch()` 改为内存累计 + 每 30s 由后台线程批量落盘一次
   （实测这能把 apikeys 表的版本增速从「每分钟每密钥一次写」降到「每 30s 一次」，
   同时把 `auth` 从「≥1 次 DB 往返」变成 0 次）。
6. **拆 health**：`GET /api/health` 变成纯 liveness（不查库，<1ms）；
   现有全量对账移到 `GET /api/health/deep`，结果缓存 10s、限 write 作用域、串行执行（同一时刻最多一个）；
   `backend.stats()` 的目录遍历结果缓存 60s；`stats()` 不再默认调 `list_versions()`（改成可选参数）。
   这一条的价值不在「减少轮询」，而在**别让一个 40–90ms 的重扫描和 2ms 的业务请求抢同一批线程**。
7. **前端配合**：静态资源 `/assets/*` 上 `Cache-Control: public, max-age=31536000, immutable`
   （Vite 产物本就是 hash 文件名），`index.html` 保留 no-cache；
   入库期间的状态获取改成一条 SSE（复用 `/answer/stream` 已有的实现风格），
   取代 700ms / 4s 的 `GET /api/jobs` 轮询。

**D. 写路径的并发纪律**

8. **锁分级**：`_lock` 拆成 `write_lock`（写库）、`index_lock`（建索引）、`compact_lock`；
   索引维护不再与写入抢同一把锁。
9. **FTS 不再每次全量重建**：`ensure_fts_index` 改为「积压行数 ≥ 阈值 **或** 距上次 ≥ N 秒」才重建，
   并把 `FTS_REBUILD_ALWAYS_BELOW=50000` 这个「小于五万就每次都全量重建」的策略改成增量/小批。
10. **版本与碎片治理常态化**（最关键的一条长跑修复）：`schedule_maintenance()` 里对
    **全部 5 张表**周期性执行 `optimize()` / `compact_files()` / `cleanup_old_versions(retain=…)`，
    触发条件是「版本数或 fragment 数超阈值」而不是只按时间，且默认开启而非只靠手工 `rag reindex`。
11. **显式设置默认线程池**：`loop.set_default_executor(ThreadPoolExecutor(max_workers=cpu*16))`
    + `anyio` limiter 提额。实测它**不是**当前瓶颈（8→128 无变化），但本机默认值只有 **8**
    （`min(32, cpu+4)`）而每个请求要用 2 次 `to_thread`，是个不知何时会踩到的暗坎；顺手修掉。
12. **入库默认改成异步优先**：`wait` 默认 `false`（或保留 `true` 但加超时后返回 202 + job_id），
    让 HTTP 侧不再长期占用等待资源。

**E. 并发安全（高并发下会变正确性问题）**

13. `Embedder._one_cache` → 带锁的真 LRU（`collections.OrderedDict` + `Lock`，容量可调）；
14. `_touch_cache` / `IngestQueue._finished` 加上界；
15. `merge_sub` 不再就地 `setattr` 全局 `settings` 子模型——改为构造新模型并原子替换引用，
    避免并发请求读到半新半旧的配置。

**阶段一验收**：`/api/documents` p50 < 5ms（c=1）、聚合 ≥350 rps（c=32）；
`/api/search` p50 < 30ms；c=32 入库吞吐 ≥ c=1 的 90%；48 次入库后 `jobs` 版本 <60、
`documents` 读延迟增幅 <20%。

---

## 6. 阶段二：结构性改造（3–7 天，**并发量级的主来源**）

### 6.1 存储分层：OLTP 交给 SQLite(WAL)，LanceDB 只留向量与 FTS

| 数据 | 现在 | 建议 | 为什么 |
|---|---|---|---|
| `chunks`（向量 + `text_seg`） | LanceDB | **LanceDB（不变）** | 列存 + ANN + tantivy 正是它的价值所在 |
| `documents` 元数据 | LanceDB | **SQLite** | 点查/按库分页/改标题/计数——纯 OLTP |
| `jobs` | LanceDB | **SQLite** | 高频小写 + 轮询点查，是今天最严重的写放大器 |
| `apikeys` | LanceDB | **SQLite** | 每请求点查 + `last_used_at` 写 |
| `kbs` | LanceDB | **SQLite** | 小表、频繁列表 |
| `documents.text`（全文） | LanceDB 行内 | **SQLite（或 `data/text/<doc_id>.txt`）** | 列表查询不该顺带把 20MB 正文纳入扫描范围 |

- **不违反项目定位**：`sqlite3` 是 Python 标准库，零外部服务、仍是单数据目录（`data/raggi.db` + WAL），
  「极简单机」不变。
- **量级**：点查 5.68ms → **≈0.02ms**（预估，SQLite 主键查询）；`jobs` 状态更新不再产生 fragment；
  `/api/documents`、`/api/kbs` 不再受版本/碎片增长拖累。
- **顺带解决的正确性问题**（DESIGN §16 的两条风险）：documents 与 jobs 的「读—改—写」进事务；
  `删旧块 → 写新块` 用 SQLite 事务 + outbox 表 + Lance 幂等 `merge_insert` 重放，
  orphan_chunks / count_mismatch / 半截重解析这一整类问题从机制上消失。
- **迁移**：仓储层已收口在 `rag/storage/repos/*`（`documents.py` / `chunks.py` / `kbs.py` / `keys.py`），
  换引擎主要改这一层 + `LanceStore` 的职责边界；`tests/` 里的架构边界规则（只有 `storage/` 碰 DB）继续成立。

### 6.2 进程拓扑：让核真的并行（实测 2.5–2.7×）

```
supervisor（`rag serve` 仍然是单命令、单端口）
├─ API 读进程 × N（N = cpu，无状态）    鉴权(缓存) + SQLite 读 + Lance 向量/FTS 检索
├─ 写 / 入库 worker × 1–2               唯一 LanceDB 写入者：领 jobs → 解析 → 分词 → 向量化 → 写 → 建索引
└─（可选）解析池                        PyMuPDF/Docling 子进程，避免 CPU 重活卡事件循环
```

要点：

- **API 进程不写 LanceDB** ⇒ DESIGN §16 的「多进程写未验证」风险**自动消解**：唯一写入者是 worker。
  worker 内部保留现在的 `RLock` 语义即可（它本来也是串行的）。
- 写请求路径：API 进程把任务写进 `jobs`（SQLite，单语句、毫秒级）→ 立刻返回 `job_id` →
  worker 用 `UPDATE … WHERE stage='queued' ORDER BY started_at LIMIT 1` 乐观领取。
  这样 `max_pending`/背压仍在，但「上传 = 占住一条连接 150ms–8s」的形态消失。
- 静态资源：交给 `index.html` 长缓存 + `/assets/*` immutable（阶段一已做），或反代；
  不必占用 API 进程的线程额度。
- **需要用户拍板的取舍**：这与 DESIGN §2「单进程单端口」和 §16「不支持 `--workers>1`」直接冲突。
  可保持对外的兼容性（仍然一个命令、一个端口、一个数据目录），但**运行时不再是单进程**。
  如果不接受这条，读吞吐天花板就锁在 ~500 rps（阶段一后的单进程上限），拿不到那 2.5×。

### 6.3 模型调用治理（检索并发的真实上限）

1. **query embedding 缓存**：带锁 LRU（容量 4k–32k，键=模型+文本哈希），命中即省掉一次网络往返；
2. **连接池**：`httpx.AsyncClient(limits=Limits(max_connections=100, max_keepalive_connections=20))`
   单例复用，替换 `urllib`（`rerank.py`）与每次新建的 `ChatOpenAI`（`registry.chat()`）；
3. **合批**：把 10–20ms 窗口内的 embed 请求聚成一个 batch（对远端只算一次调用，QPS 放大 N 倍）；
4. **闸门可调**：`_EMBED_SEMAPHORE` 从硬编码 8 变成配置项，并按 provider 限额设定；
   重试策略改为「429 时按 `Retry-After` 退避 + 抖动」，避免 §12.1 记录过的级联重试风暴；
5. **rerank 熔断**：连续失败 N 次后短路降级（`_apply_rerank` 已有降级，缺的是不再打远端）。

---

## 7. 阶段三：把上限继续抬高（需要真实负载与硬件验证）

1. **读进程水平扩展**：读路径无状态 ⇒ 进程数或实例数线性加（配合对象存储后端 `storage.backend=s3`，
   代码里已经实现）；
2. **检索层缓存/预热**：热门查询结果 2–5s TTL + 写时按 `kb_id` 失效；索引常驻（HNSW 加载预热）；
3. **写入侧**：解析池按核数扩，向量化与写库解耦（解析/向量化并行、写入单点串行），
   `merge_insert` 从「每文档一次」聚合成「每 T 秒一批」——**列存最喜欢批量，最怕小写**；
4. **可观测性**：`X-Response-Time-Ms` 已有，补 `db_queries` / `embed_calls` 计数头与
   `/api/health/deep` 里的版本数、fragment 数、compaction 落后量——
   否则 §2.6 这类「越用越慢」只能靠人肉发现；
5. **压测常态化**：把 §9 的脚本固化成 `tests/perf/` + 阈值断言（CI 跑小规模、夜间跑全量），
   任何让读路径重新引入全表扫的改动都会被拦下。

---

## 8. 建议的落地顺序（如果只做三件事）

1. **阶段一的第 4、6、10 条**（消灭检索 N+1 / health 降级 / 版本与碎片治理常态化）——
   这三条分别对应最热端点的最大单项开销、最容易被轮询打死的端点、以及「越用越慢」的根因；
2. **阶段二的 6.1（jobs + apikeys 搬 SQLite）**——不动 documents 也能拿掉大部分写放大与每请求鉴权开销；
3. **阶段二的 6.2（读进程 ×N）**——实测唯一能突破 1.28× 并行度天花板的手段。

---

## 9. 复现材料

| 文件 | 用途 |
|---|---|
| `/tmp/raggi_bench/bench.py` | 闭环并发压测（每线程独立 httpx client，输出 rps/p50/p95） |
| `/tmp/raggi_bench/bench_ingest.py` | 并发入库压测（wait / nowait 两模式） |
| `/tmp/raggi_bench/framework.py` | 开销归因：A 裸返回 / B 响应模型 / C to_thread / D 真 LanceDB / E 两者叠加 |
| `/tmp/raggi_bench/scale2.py` | 同进程多线程 vs 多进程 的 LanceDB 读并行度 |
| `/tmp/raggi_bench/mpr.py` | N 个服务进程共享数据目录的聚合读吞吐 |
| `/tmp/raggi_bench/scan_paths.py` | `to_list` / `to_arrow` / 原生 `to_table` / 向量 / FTS / hybrid 单查询开销 |
| `/tmp/raggi_bench/gil.py`、`micro.py` | GIL 假设的早期版本与函数级微测量 |
| `/tmp/raggi_bench/runner.py` | 可调「默认线程池大小 / ingest_workers」的启动器（用于 §2.5 的排除实验） |
| `/tmp/raggi_bench/fake_embed.py` | OpenAI 兼容假 embedding 服务（端口 8390） |
| `/tmp/raggi_bench/gen_scale.py` | 2000 文档 / 20000 分块合成数据集生成器 |

数据集副本：`/tmp/raggi_scale/data`（20k 分块，索引齐）、`/tmp/raggi_bench/data`（真实 `data/` 的副本）。
**`/home/admin/Raggi/data/` 未被写入。** 收尾时清理过上一轮会话遗留的 `rag.server`（`--data /tmp/raggi_s6 --port 8127`，
自 10月04 起常驻），本报告的实验进程已全部停止。

---

## 10. 与现有文档的一致性

- DESIGN §12.1 记录的「吞吐 9.5 req/s」与本文 §2.1 的 80–150 rps 不矛盾：那次测的是**含真实模型调用**的
  端到端；本文刻意把模型剥离，才能把「存储层固定开销」单独量出来。
- DESIGN §12 表格里「解析放 `ThreadPoolExecutor(min(4,cpu))`」与实现不符：
  实际是 `pipeline._embed_concurrently` 里的 `ThreadPoolExecutor(workers)` + 进程级信号量 8，
  解析在 `ingest_workers=2` 的队列线程里跑。
- DESIGN §12 表格「索引：后台阈值触发 `optimize()` 与 `cleanup_old_versions()`」与实现不符：
  `schedule_maintenance()` **既不调 `optimize()` 也不清版本**，只有手工 `rag reindex` 会。
  这一条正是 §2.6 长跑变慢的直接原因。
- DESIGN §16「多进程写入未验证，故不支持 `--workers>1`」在现有存储布局下是**正确**的判断；
  本文的建议不是推翻它，而是通过「唯一写入者」把多进程变成安全的。
