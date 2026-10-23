# Raggi 架构健康报告（2026-10-23）

范围：`rag/`（FastAPI + SQLite/WAL + LanceDB + 有界入库队列）与 `web/`
（React 19 + MUI 6 + Vite）。
方法：先建立可复现基线（`pytest` / `tsc` / `vite build` / Playwright），
再沿真实请求与入库热路径取证；对可疑的复杂度问题做**前后同一台机器**的
微基准；修复后用全量测试、E2E 与构建产物核对。本文只写已核实的结论。

基线（本轮改动前，含工作区在途改动）：**652 passed, 1 deselected**，
`ruff` / `mypy` / `tsc` / `npm run build` 全绿，Playwright 7/7。
最终：**657 passed, 1 deselected** + **7 E2E**，四道静态/构建门禁仍全绿。

---

## 1. 结论摘要

**可以信任的：** 单机范围内的分层是成立的，且关键边界有测试钉住——
OLTP 元数据在 SQLite、向量/全文在 LanceDB、写路径有跨进程锁、慢的
embedding 在锁外、入库队列有背压、鉴权/限流/幂等/快照缓存跨进程共享。
不是“看起来分层”，而是读入口、SQL 转义、错误映射都收口到了可检查的位置。

**本轮确认并修复的瓶颈（都有量化证据）：**

| 问题 | 位置 | 改前 | 改后 | 影响 |
|---|---|---|---|---|
| 标题面包屑对每个块从头扫描全部标题（O(块×标题)） | `rag/ingest/splitter.py` | 5000 标题 × 5000 块：`heading_path_for` 单独 **5.3s** | 完整 `split_document` **313ms** | 长 Markdown / 书籍类文档入库从秒级抖动降到亚秒 |
| 页码映射对每个块线性扫描全部页 span（O(块×页)） | `rag/ingest/pipeline.py` | 5000 块 × 1000 页：**560ms** | **2.2ms** | 长 PDF 入库不再随页数放大 |
| 知识库列表把全部 documents 行搬进 Python 累加 | `rag/storage/repos/kbs.py` | 50k 文档：**161ms**，内存随文档线性增长 | SQL `GROUP BY`：**46ms**，只返回知识库数行 | 打开应用 / 改库后的列表请求不再与文档总量线性绑定 |
| 所有路由视图与 API 文档数据进入首包 | `web/src/App.tsx` | 首包 JS **255.1KB gzip**（index 144.9） | **195.7KB gzip**（index 85.8） | 首屏下载/解析减少约 23% |
| 全局 store 整店订阅 + 流式每帧重建整页 | `web/src/lib/store.ts`、`Search.tsx`、`ChatThread.tsx` 等 | 任意 `busy/kbs/crumbs` 变化重渲染玻璃与页面；每个 delta 重建轮次数组 | 切片订阅；delta 只翻转一次状态；轮次/证据/输入台 memo | 加载期与流式期的无效渲染面显著缩小 |

**仍然值得做、但本轮未做的：** 真实模型往返下的检索 P95、健康/统计的
O(N) 全表扫描（已有缓存与单飞）、知识库计数的覆盖索引、以及三个
千行级视图的拆分。详见 §3。

---

## 2. 当前架构（证据与置信度）

| 层 | 事实 | 主要证据 | 置信度 |
|---|---|---|---|
| Web | React 19 + MUI 6 + Vite，产物 `web/dist` 由 FastAPI 静态托管；hash 路由 | `web/src/App.tsx`、`vite.config.ts`、`rag/api/__init__.py` | high |
| API | FastAPI；`/api/v1` 为规范路径，`/api` 为兼容别名；鉴权/正文上限/限流/请求 ID/CSP 在中间件 | `rag/api/__init__.py`、`rag/api/auth.py` | high |
| OLTP | SQLite WAL 保存 documents/kbs/jobs/apikeys/幂等/限流/代际；每线程连接 + `BEGIN IMMEDIATE` | `rag/storage/meta.py` | high |
| 检索存储 | LanceDB 保存 chunks（向量、`text_seg`、下推列）与 FTS；标量读走原生投影 | `rag/storage/tables.py`、`rag/storage/sql.py` | high |
| 入库 | 进程内有界队列（默认 2 worker / 容量 32），满时 503 背压；embedding 在写锁外并发 | `rag/ingest/queue.py`、`rag/ingest/pipeline.py` | high |
| 模型 | OpenAI 兼容 / Ollama / HuggingFace；查询向量有 TTL 缓存 + 同 key 单飞 | `rag/models/registry.py`、`rag/models/embeddings.py` | high |
| 原文归档 | `local` 或 S3 兼容对象存储；路由层只依赖 `Backend` 协议 | `rag/storage/backend.py` | high |
| 运行时 | 默认单进程；`RAGGI_PROCESSES=auto` 可多进程；写锁在 local 后端下是跨进程 flock | `rag/server.py`、`rag/storage/filelock.py` | high |
| 真实模型 P95 | 本机 Ollama 常不在线，未用真实凭据跑压测 | `tools/bench/` 的说明与既有性能文档 | unknown |

**信任边界：** 代码结构与静态契约可以信任；真实 provider 的延迟、限流
行为与多进程写冲突率仍属于“未在本轮实测”的区域，不能拿本报告的
假 embedding 数字当线上承诺。

---

## 3. 剩余瓶颈与建议（按 ROI 排序）

### 3.1 检索的真实上限是模型网络，不是本地代码（高影响，未改）

既有诊断给出的本地部分：hybrid 查询（LanceDB 一次完成向量 + FTS）
约 50–75ms；本轮没有推翻这个量级。真实部署里 embedding / rerank 的
网络往返通常更大。已具备的止损：query→vector TTL 缓存、同 key 单飞、
精排失败降级、超时与重试只在 `Embedder` 一层。

**建议：** 用真实 provider 跑 `rag-bench`，把 P50/P95 与“本地检索耗时 /
模型耗时”分开记录；在没有这份数据前，不应继续微调 `candidate_k` /
`nprobes` 这类参数。

### 3.2 健康/统计端点是 O(N) 全表扫描（中，已有缓存）

`/api/health` 每次现算会做 `chunks.count_rows()`、`chunk_counts_by_doc`
（全 `doc_id` 列扫描）、`fts_stale` 过滤计数、`embed_model` 去重扫描；
`/api/stats` 还会递归遍历数据目录。当前用 3s TTL + per-key 单飞 + 写代际
失效控制住了并发放大，单机规模下可接受。数据量到十万级后，建议：
- 把 chunk/doc 计数改为写路径增量维护（或 SQLite 聚合）的事实表；
- `/health` 只报增量指标，深度对账移到显式 `fresh=true` / 后台任务。

证据：`rag/storage/health.py`、`rag/api/system.py`、`rag/core/cache.py`。

### 3.3 知识库计数仍是每次 O(docs)（中低）

本轮已把“搬运 + Python 累加”换成 SQL 聚合（50k 行 161→46ms），但
`GROUP BY` 仍要扫全表。现有索引 `idx_docs_kb(kb_id, created_at, doc_id)`
不覆盖 `chunk_count`。如果 `/api/kbs` 变成高频端点，可加覆盖索引
`documents(kb_id, chunk_count)`，或把计数挂在写代际缓存上。
证据：`rag/storage/meta.py` 的 DDL、`rag/storage/repos/kbs.py`。

### 3.4 三个千行级视图（可维护性，中）

`Search.tsx`（1016 行）、`KbDocs.tsx`（1042 行）、`DocView.tsx`（771 行）
同时承担取数、竞态、草稿状态与复杂 JSX。本轮只做了渲染边界（memo /
稳定回调 / 懒加载），没有拆文件。建议按“数据 hook + 面板组件”渐进拆分，
每步保留现有前端守卫与 E2E；不要为了拆而改行为。

### 3.5 长列表未虚拟化（低，当前有上界）

文档/分块表格与证据列表都是直接渲染。证据列表默认 `top_k` 很小；
表格有分页上界（≤500）。在页面大小被调大或单页文档数继续增加前，
可用 `content-visibility: auto` 或虚拟列表；当前不是首要瓶颈。

### 3.6 首包 vendor 仍约 110KB gzip（低）

React + MUI + Emotion + 玻璃库被合并为一个 vendor 块（拆开会形成
react↔MUI 循环）。路由分包后 index 已从 86KB gzip 起步，继续压 vendor
的收益低于维护成本；不建议为此引入按需 CDN（与离线单机定位冲突）。

### 3.7 测试套件约 5 分钟 + 性能门禁曾受全局 patch 污染（工程效率，低）

全量 657 条用例串行约 5 分钟，主要耗时在真实 LanceDB/SQLite 吞吐与
CLI 子进程。可评估 `pytest-xdist` 或把 `test_server_cli` 这类慢组
单独标记；不要为了提速而把集成路径 mock 掉。

另一个已修的门禁问题：`QueryCounter` 进程级 patch 了 Lance 的查询入口，
全量运行时其它测试遗留的后台维护线程可能把查询计入当前窗口，造成
`test_search_query_count_does_not_grow_with_hits` 偶发假红（本轮全量
实测遇到，单独运行则稳定通过）。现在计数器按被测 store 的 URI 限定，
保留原有的 N+1 检测能力，同时消除跨测试干扰。
证据：`tests/test_perf_gates.py`。

### 3.8 已记录但未变的边界

多进程下鉴权缓存/健康快照最多等一个 TTL；限流与幂等依赖 `MetaStore`
（`meta_engine=lancedb` 时退回进程内）；SSRF 仍存在极窄的 DNS rebinding
窗口；CSP 的 `style-src-attr 'unsafe-inline'` 是 MUI 内联 style 的硬需求。
这些是部署形态约束，不是本轮回归。

---

## 4. 可维护性评估

**做得好的（有测试或结构守卫）：**
- 存储层边界清楚：SQL 文本只在 storage 层出现，documents/kbs 各有唯一读入口；
  mypy 只圈 storage，但圈住的正是历史事故高发区。
- 请求与数据的“静默错误”防得多：非法过滤产生恒假条件、错误引擎读取、
  缓存单飞、写锁内校验、队列满 503、SSE 无终态报错。
- 前端守卫覆盖可访问性、深链、分页口径、异步三态、CSP、批量端点等
  跨层契约；E2E 覆盖关键路径。
- 性能门槛断言“查询次数/是否取全表”，而不是易抖的绝对延迟——这比
  写一个 150ms 的睡眠断言更可维护。

**主要债务：** 大视图体量；注释承担了过多“设计文档”职责，容易出现
文档漂移（本轮修掉了 Svelte/MUI 9/测试计数三处）；测试套件时长。

**总体判断：** 在“单机、单租户、可整体拷贝”的产品边界内，这套架构
是专业且自洽的；它没有假装支持分布式，也没有为了“看起来现代”引入
额外基础设施。可维护性的短板集中在 UI 视图粒度，而不是后端分层。

---

## 5. 追踪矩阵（节点 → 证据 → 测试）

| 节点 | 主要证据 | 回归测试 | 置信度 |
|---|---|---|---|
| Web 壳层 / 路由 | `web/src/App.tsx`、`web/src/lib/router.ts` | `test_frontend_wiring.py`、`e2e/app.spec.ts` | high |
| 检索工作台 / 流式 | `web/src/views/Search.tsx`、`web/src/lib/sse.ts` | `test_streaming_answer_ui`、E2E 对话用例 | high |
| API 装配 / 中间件 | `rag/api/__init__.py` | `test_security_hardening.py`、`test_audit_regressions.py` | high |
| SQLite 元数据 | `rag/storage/meta.py`、`repos/documents.py` | `test_meta_store.py` | high |
| LanceDB chunks/索引 | `rag/storage/tables.py`、`repos/chunks.py` | `test_perf_gates.py`、`test_search_api.py` | high |
| 入库流水线 | `rag/ingest/pipeline.py`、`splitter.py`、`queue.py` | `test_queue.py`、`test_parsing_layer.py` | high |
| 检索内核 | `rag/retrieval/search.py`、`rag/retrieval/answer.py` | `test_search_api.py`、`test_answer_stream.py` | high |
| 模型注册 / 缓存 | `rag/models/registry.py`、`embeddings.py` | `test_model_clients.py`、`test_cache.py` | high |
| 原文归档 local/S3 | `rag/storage/backend.py` | `test_audit_fixes.py` 等 | high |
| 真实 provider 延迟 | 无本机凭据 | `-m perf` / `rag-bench` 待跑 | unknown |

---

## 6. 活体架构检查（可重复执行）

```bash
python3 -m ruff check .
python3 -m mypy
npm run check
npm run build
python3 -m pytest -q          # 657 passed, 1 deselected
npm run test:e2e              # 7 passed（需系统 Chrome）
pytest -m perf -q             # 手动：延迟门槛
```

建议在代码评审里增加两条**易执行**的检查（尚不是 CI 硬门禁）：
1. `git diff --name-only` 里出现 `rag/storage/`、`rag/ingest/`、`web/src/lib/`
   的改动时，同步检查本报告 §2/§5 的对应行是否需要更新；
2. 构建产物预算：首包 `index` gzip 超过 110KB 时在评审中说明原因
   （本轮 85.8KB；不再退回单包）。

---

## 7. 本轮验证记录

- `ruff check .`：通过
- `mypy`：通过（17 个 storage 源文件）
- `npm run check`：通过
- `npm run build`：通过；index 266.77KB（gzip 85.80），vendor 365.20KB
  （gzip 109.85），路由块按需加载
- `pytest -q`：**657 passed, 1 deselected**（改动前 652 passed）
- `npm run test:e2e`：**7 passed**（含懒加载后的检索/对话/API 文档/深链/CSP）
- 微基准（同一台 4 核 LXC，Python 3.10）：
  - 5000 标题 × 5000 块：标题路径 5.3s → 完整切分 313ms
  - 5000 块 × 1000 页：560ms → 2.2ms
  - 50k 文档计数聚合：161ms → 46ms（SQLite）
  - 前端首包：255.1KB gzip → 195.7KB gzip
