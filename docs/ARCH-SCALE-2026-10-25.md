# 万级并发架构改造（2026-10-25）

> 目标口径：**1 万个在飞请求（≈10k+ QPS）**。这是分布式系统目标，
> 不是单机调参目标。本文记录改造顺序、当前进度与验收方式。
> 单机默认行为不受影响：不配 `redis.url` 时一切与改造前一致。

## 1. 容量模型（先把目标换算成资源）

`并发 = QPS × 平均延迟`。10k 在飞、单次 500ms ⇒ 20k QPS；即使 100ms 也要 100k QPS。
按这个口径拆：

| 组件 | 单请求成本 | 10k QPS 需要的量级 |
|---|---|---|
| API/编排（Python） | 2–10ms CPU | ≈20–100 核（多副本） |
| 查询 embedding | 一次往返 / 或缓存命中 | 缓存命中 70% 时仍需 3k 次/秒 embedding |
| 向量检索 | 10–50ms CPU | 数百核或 GPU 向量库分片 |
| 元数据读 | 0.1–1ms | Postgres + 连接池 + 只读副本 |
| 响应体 | 20–100KB | 2–10 Gbps 出口 |

**结论**：API 层不是瓶颈，embedding 与向量检索才是。改造顺序必须
先把"共享缓存 + 无状态 API"做出来，再换存储，最后加 embedding 容量。

## 2. 当前架构的硬上限（改造对象）

| 组件 | 现状 | 为什么在万级并发下不成立 |
|---|---|---|
| LanceDB | 嵌入式库，每进程一份句柄与索引 | 扩读即扩内存；无服务端缓存/分片/副本；写入靠跨进程文件锁（`storage/filelock.py`），多机下退化为进程内锁并告警 |
| SQLite | `jobs/keys/kbs/documents` 元数据 | 单写者；跨机共享不成立 |
| `IngestQueue` | 进程内线程池（2 worker / 32 排队） | 队列与 worker 都是本进程的，无法跨副本协调 |
| 进程内缓存 | TTLCache（健康/统计/query embedding） | 多副本命中率被摊薄；写失效只作用于本进程 |
| 限流 | 进程内 / SQLite 令牌桶 | 多副本下额度会乘以副本数 |

## 3. 目标拓扑

```
CDN / 静态
   │
 L7 负载均衡（nginx / Envoy / ALB）
   │
 API 无状态副本 ×N ── Redis/Valkey（query embedding、检索结果、限流）
   ├─ Postgres（元数据，读写分离）
   ├─ 向量服务（Qdrant/Milvus，分片+副本）
   ├─ GPU embedding 服务（TEI/vLLM，continuous batching）
   ├─ LLM 网关（SSE 长连接池）
   └─ 外部队列（Redis Streams/Kafka）+ 入库 worker ×N
        └─ 对象存储（原文）
```

## 4. 改造阶段与进度

### 阶段 1：可插拔接口 + 共享缓存 + 分布式限流（本轮，已完成）

- [x] `rag/core/ports.py`：`CacheBackend` / `LimiterBackend` 协议
- [x] `rag/core/redis.py`：惰性客户端（可选依赖 `raggi[scale]`）、`RedisCache`
- [x] `rag/core/cache.py`：`LayeredCache`（L1 单飞 + L2 共享），**单飞锁改为用完即回收**（改前每个新查询永久留一把锁）
- [x] query embedding 走共享缓存：多副本共享同一份查询向量
- [x] `RedisRateLimiter`：Lua 原子令牌桶，Redis 不可用时退回进程内桶
- [x] 配置：`[redis] url/namespace/timeout_ms/max_connections`、`[cache] max_items/ttl_seconds`、`rate_limit_backend = auto|memory|redis`
- [x] 部署骨架：`deploy/Dockerfile`、`deploy/docker-compose.yml`（Valkey + API ×2）
- [x] 测试：分层缓存/降级/单飞锁回收（`tests/test_shared_cache.py`）、真实 Valkey 集成（`tests/test_redis_integration.py`，需 `RAG_TEST_REDIS_URL`）

默认值仍是 `redis.url=""`：不装依赖、不连外部服务，行为与改造前逐字节一致。

### 阶段 2：任务与状态的"出进程"

#### 2a 任务规格化 + Redis Streams + 独立 worker（本轮完成）

- [x] `rag/ingest/jobspec.py`：`IngestJob`（file / text / url / reparse / resplit）
  可 JSON 往返；`run_ingest_job` 是本地队列与外部 worker 共用的唯一执行入口
- [x] API 四个入库端点 + 重切分端点不再传可调用对象，只 `queue.submit_job(job)`
- [x] `rag/ingest/redis_queue.py`：XADD + consumer group + XAUTOCLAIM 接管崩溃
  worker 的 pending（至少一次）；取消标记走 Redis + 任务行双写
- [x] 队列上限按**消费组积压**（lag + pending）计算，不是 XLEN；ACK 后 XTRIM
- [x] `rag worker` CLI（复用 `server.build_ctx()`）；compose 增加 worker 服务
- [x] `wait=true` 在远程队列下轮询任务行直到终态（超时 600s → 504）
- [x] 测试：真实 Valkey 端到端——API 只入队，worker 跑完真实 pipeline，
  任务行到 `done`、文档落库（`tests/test_redis_integration.py`）

已知边界（2b 要解决）：

- `jobs`/元数据仍是本机 SQLite：**worker 必须与 API 共享同一个数据目录**，
  还不能跨机器；
- 远程队列的 `wait=true` 只能给出 ready/failed/cancelled 三态，本地队列
  的 `duplicate` 细分状态与 `chunk_count` 由文档行回读；
- `file` 任务的 `path` 是共享卷上的工作副本，尚未改成对象存储 key。

#### 2b 元数据迁 Postgres（已完成）

- [x] `rag/storage/pg_meta.py`：`PostgresMetaStore`，与 `MetaStore` 同一套方法
  （query/upsert/tx/columns/count/state…）；方言差异只在本文件里处理：
  占位符 `?`→`%s`（跳过字符串字面量）、`LIKE`→`ILIKE`（对齐 SQLite 的
  大小写不敏感）、`REAL`→`DOUBLE PRECISION`（时间戳不掉精度）
- [x] 配置：`storage.meta_engine = auto|sqlite|lancedb|postgres` +
  `storage.postgres_url`；单机默认仍是 SQLite
- [x] 切换命令：`rag migrate --to-postgres`（SQLite → PG 幂等复制四张真源表
  与 `meta_state`，可重复执行）
- [x] 顺带修掉一处 SQLite 专有写法：幂等清理里的 `rowid IN (...)` 改成
  行值 `IN (SELECT …)`，两个引擎都能跑
- [x] 测试：`tests/test_postgres_store.py`（真实 PG，`RAG_TEST_PG_URL` 才跑）
  —— schema 反射/默认值、文档 CRUD 与 LIKE 大小写、分页、任务与知识库、
  事务回滚、代际、共享限流与幂等、SQLite→PG 复制
- [x] 端到端：e2e 服务以 `meta_engine=postgres` 启动，入库、任务行、检索、
  知识库列表全部正常

**仍未解决（2c / 阶段 3）**：向量仍在**嵌入式 LanceDB** 的本地卷上——
它的写锁是同机 flock，**同一台 Docker 主机上的多进程安全，跨主机不安全**；
上传文件仍是共享卷上的路径而不是对象存储 key。跨机器部署要等向量库替换
（阶段 3）与 S3 原文归档。

### 阶段 3：存储替换（向量）

#### 3a 结构化过滤（已完成）

- [x] `rag/storage/filters.py`：`ChunkFilter` 把过滤条件变成**数据**
  （enabled / origin / kb(_ids) / doc_ids / mime|parser_engine 解析出的
  文档白名单 / 独立分块 / 正文子串），`to_sql()` 只服务当前引擎
- [x] 检索侧 `chunk_filter()` 返回结构化对象，`search_chunks(where=…)`
  同时接受对象与旧 SQL 字符串；`chunk_prefilter()` 保留为 SQL 兼容包装
- [x] 列表侧 `list_filter()`；`chunks_page` 内部改走结构化对象
- [x] 空集合**直接报错**，恒假必须显式用 `NEVER`（`IN ()` 是语法错误，
  被吞掉就退化成「不加过滤」）
- [x] 测试：11 条（渲染/互斥/转义/恒假/兼容入口/真实 Lance 检索）
  `tests/test_chunk_filter.py`

#### 3b 适配器核心（已完成，尚未接线）

- [x] `rag/storage/qdrant_store.py`：collection schema（dense + sparse/IDF）、
  点位 upsert/get/delete、payload 更新与正文改写、`ChunkFilter →
  qdrant.Filter`（空白名单落成恒假，不退化成"不过滤"）
- [x] 检索三通道：vector（cosine 相似度折成应用层约定的余弦距离）、
  fts（jieba → sparse + IDF）、hybrid（dense+sparse 两路 prefetch + RRF 融合）
- [x] 分数列对齐 `_score_of()` 的既有约定：`_distance` / `_score` /
  `_relevance_score`
- [x] 真实 Qdrant 6 条集成测试（过滤/恒假/全文/融合/删除/改写）
  `tests/test_qdrant_store.py`（`RAG_TEST_QDRANT_URL` 才跑）
- [x] `deploy/docker-compose.yml` 增加 qdrant 服务；可选依赖 `raggi[vector-qdrant]`

#### 3b-2 读路径补齐（已完成，未接线）

- [x] `page()`：scroll + range 索引全序键（(ordinal<<32)|chunk_id 哈希），
  分页在 ordinal 并列处不重复不漏项；`count()` 走精确计数
- [x] `facts()`：按文档计数 / 待重排行 / 模型集合（Qdrant 的 sparse 随点更新，
  待重建恒为 0）；`vector_dim()` 读 collection 配置
- [x] 10 条真实 Qdrant 测试：加 page 稳定性、过滤、facts、vector_dim

**下一步（3c，接线）**：把 `chunks_page` / health / 分块编辑 / 入库写路径
改为经后端分派；`storage.vector_engine=qdrant` 切换；Lance→Qdrant 迁移工具；
E2E 回归。

1. ~~元数据：SQLite → Postgres~~（阶段 2b 已完成）；
2. 向量：LanceDB → Qdrant（或 Milvus）。必须对齐四件事：
   - FTS 能力（Qdrant 全文索引 vs 当前 tantivy）
   - hybrid + RRF 融合口径（`score_kind` 语义不能变）
   - 过滤下推（`kb_id` / `doc_id` / `enabled` / `score_threshold`）
   - 分页与 `total` 的计数口径
3. 写路径：入库 worker 成为唯一写者；API 不再直接写向量库。

### 阶段 4：容量与治理

#### 4a 读路径背压与分段计时（已完成）

- [x] **查询侧 embedding 闸门**：`embed.query_concurrency`（每进程，默认 0=不限）
  + `embed.query_wait_ms`（默认 50ms）。不同查询抢槽位，拿不到直接
  `503 + Retry-After: 1`；**相同查询在单飞锁上合流，不占额外槽位**。
  检索路径此前完全没有并发上限，几百个不同查询会同时打爆 provider。
- [x] **分段计时**：`took_ms` 改为端到端（含 embedding），新增
  `embed_ms` / `search_ms`。改前 80–150ms 的模型往返被排除在计时外，
  看起来检索很快，实际瓶颈看不见。
- [x] compose 里给多副本形态显式配置 `RAG_EMBED__QUERY_CONCURRENCY=32`
- [x] 测试：闸门拒绝 / 单飞豁免 / 503 映射 / 计时对账（`tests/test_query_gate.py`）

#### 4b 准入控制（已完成）

- [x] `api_max_inflight`（每进程，默认 0=关闭）：在飞请求超过上限直接
  `503 + Retry-After: 1`，把「无限排队后一起超时」换成快速失败
- [x] `/api/health`、`/api/stats` 不受准入控制（探针被限流会判死实例；
  stats 自己算进计数就读不出真实值）
- [x] `/api/stats` 暴露 `inflight` / `max_inflight` / `peak_inflight`（实时，不走快照）
- [x] 测试：计数/拒绝/健康放行/SSE 长连接占槽/默认关闭（`tests/test_admission.py`）
- [x] compose 给多副本形态配置每副本 1000

#### 4c 仍待做

- 自动扩缩：按在飞请求 / CPU / p95 扩 API 副本；
- 自动扩缩：按 CPU / 在飞请求 / p95 扩 API 副本；
- LLM/embedding 连接池与超时预算；SSE 网关长连接参数；
- 压测：k6/wrk 场景（1 → 10 → 50 → 100 → 300 → 1k 并发），记录 p50/p95/错误率；
- 观测：每个依赖的 RED 指标 + 缓存命中率 + 队列积压。

## 5. 怎么启用阶段 1

```bash
# 1) 装可选依赖（只有多副本形态需要）
pip install -e ".[scale]"

# 2) 配 Redis（环境变量或 config.toml）
export RAG_REDIS__URL=redis://127.0.0.1:6379/0
export RAG_REDIS__NAMESPACE=raggi
export RAG_CACHE__MAX_ITEMS=2048
# 可选：共享写限流
export RAG_RATE_LIMIT_WRITES_PER_MIN=120

# 3) 启动（本地起一个 Valkey 即可验证共享缓存）
docker compose -f deploy/docker-compose.yml up --build
```

验证共享额度（两个客户端模拟两个副本）：

```bash
RAG_TEST_REDIS_URL=redis://127.0.0.1:6379/0 pytest tests/test_redis_integration.py -q
```

## 6. 风险与回滚

- **缓存层故障**：L2 读失败按未命中、写失败只告警；限流失败退回进程内桶。
  Redis 挂掉只会变慢，不会 500。
- **配置回滚**：清空 `redis.url` 即回到纯进程内形态，无需代码回滚。
- **数据一致性**：query embedding 缓存键含模型名，换模型自动失效；不含用户数据。
- **不要**在阶段 2 完成前把写流量打到多副本：LanceDB 写入需要单写者。

## 7. 未决问题

1. 向量库选型：Qdrant（运维简单、FTS 可用）vs Milvus（更大规模、组件多）；
2. embedding 部署：自建 GPU（TEI/vLLM）还是继续用托管 API；
3. 元数据是否同时引入连接池中间件（pgbouncer）；
4. 是否需要多租户隔离（按 kb 分 collection / 行级安全）。
