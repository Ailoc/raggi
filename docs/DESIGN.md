# Raggi 架构与设计文档

> 目标：**单进程、单端口、单数据目录**的极简高性能 RAG 系统。
> 技术底座：**LangChain（编排 / 加载 / 模型抽象）+ LanceDB（存储 / 索引 / 检索）**。
> 交付四大能力：**检索**、**多引擎文档解析入库与健全性**、**前端内容查看与报告**、**LLM/Embedding/Rerank 可配置可测试**。
> 其余能力一律不做（见 §1.3）。

版本基线（已核对 PyPI，2026-09）：`langchain 1.4.3`、`langchain-community 0.4.2`、`langchain-text-splitters 1.1.2`、`langchain-docling 3.0.0`、`langchain-unstructured 1.0.1`、`lancedb 0.39.0`、`pymupdf4llm 1.28.2`、`docling 2.131.0`、`unstructured 0.18.32`。

> **本版修订（2026-10-03）**：反映架构重写后的真实状态。较上一版的主要变化——
> 模块按职责重组（`core / storage / retrieval / ingest / models / api`）；
> 新增**入库队列**、**API 密钥体系**、**签名 URL**、**存储后端抽象（local | S3）**；
> 切分由三级改为**两级方案**；检索补**分数口径统一**与**分页/阈值/分组**；
> 前端**移除侧栏**并新增 **API 文档页**与**原文对照查看器**；
> 端点由 38 个增至 **47 个**（32 条路径）。

---

## 0. 需求边界

| 能力 | 状态 | 交付形态 |
|---|---|---|
| 检索 | ✅ 核心 | `POST /api/search`：LanceDB 原生 **hybrid（向量 + FTS）** + rerank + 过滤 + 分页 + 阈值 + 按文档分组 |
| 多引擎解析入库 | ✅ 核心 | LangChain Loader：**Docling / PyMuPDF4LLM / Unstructured** + 原生降级，失败自动回退 |
| 模型三件套可插拔 | ✅ 核心 | LLM / Embedding / Rerank 均支持**本地部署**与 **API 接入**，统一配置 + 连通性测试 |
| 分块内容编辑与重向量化 | ✅ 核心 | `PATCH /api/chunks/{id}`：改原文 → 重算向量 → 更新索引，含 FTS 索引失效治理 |
| 入库队列 | ✅ 核心 | 有界线程池（默认 2 工人）+ 队列容量 32，满则 503；`wait=false` 立即返回并轮询 `GET /api/jobs/{id}` |
| 前端查看与报告 | ✅ 核心 | 单页：检索 → 原文对照查看器 → 引用/报告导出 → 入库与健全面板 → 模型设置页 → **API 文档页**（Svelte 5 源码 → Vite 产出 `web/dist`，运行时零依赖） |
| 生成式问答 | ⚙️ 可选 | `features.answer` 默认关闭；开启后提供带引用标注的 `/api/answer` |
| 存储后端 | ⚙️ 可选 | `local`（默认，即目录）或 **S3 兼容**（RustFS / MinIO / AWS S3） |
| 其余 | ❌ | 见 §1.3 |

---

## 1. 设计原则与技术选型

### 1.1 原则

1. **零外部服务**：不依赖 Docker、向量数据库服务、Redis、MQ。LanceDB 是进程内嵌入式库（Rust 实现），数据即目录。
2. **单命令、单端口、单数据目录**：对外仍然是一条 `rag serve`、一个监听端口、
   一个可整体拷贝的 `data/`。运行时自 2026-10 起是**多进程**（`--processes auto`
   = `min(cpu,4)` 个只读 API 进程 + 入库队列），因为同一份存储读操作
   在同进程多线程下实测只有 **1.28×** 并行度、多进程是 **2.68×**
   （§12.1、`docs/PERF-IMPLEMENTATION-2026-10-05.md`）。
   多进程的安全前提是**跨进程写锁**（`data/lance-write.lock`，flock）
   与「OLTP 元数据搬进 SQLite」，见 §5 与 §16。`--processes 1` 一键退回单进程形态。
3. **LangChain 做编排，LanceDB 做检索**：两者职责严格边界（见 §1.2 关键取舍 ①）。
4. **依赖分层**：核心依赖最小化；Docling / Unstructured 等重型解析器为**可选 extras**，缺失时自动降级而非崩溃。
5. **可替换而非可配置**：Parser、Embedder、Reranker、LLM、StorageBackend 均为协议（Protocol）+ 实现。
6. **模块边界可测试**：只有 `rag/storage/` 允许接触数据库连接与建表；其余模块经由仓储函数访问。边界由测试守护（§15）。

### 1.2 关键取舍（重要）

| # | 取舍 | 结论与理由 |
|---|---|---|
| ① | 检索是走 `langchain-lancedb` 的 VectorStore 包装，还是直接调 LanceDB SDK？ | **直接调 LanceDB 原生 SDK**。LangChain 的 `VectorStore` 抽象只暴露 `similarity_search`，会屏蔽 LanceDB 的 **hybrid 检索、FTS 索引、reranker、SQL 过滤下推（prefilter）、标量索引、`nprobes/refine_factor` 调优、版本快照**等核心能力——这些正是本项目"高性能"的来源。`langchain-lancedb` 不作为检索主链路。 |
| ② | 向量由 LanceDB 内置 embedding registry 隐式计算，还是应用层显式计算？ | **应用层显式计算**（走模型层 `Embeddings` 接口）。理由：需要统一模型配置/热切换/连通性测试、批量重试与限流、以及"编辑后重算向量"的确定性控制。 |
| ③ | 元数据（文档表/任务表）放 SQLite 还是 LanceDB？ | **全部放 LanceDB**（多表）。理由：单存储引擎 = 单一备份/一致性模型。代价：无跨表事务与外键 → 用**两阶段写入 + 应用层对账**补偿（§5.5）。 |
| ④ | 解析器怎么选？ | 默认 `auto` 路由 + **失败回退链**，并把 `parser_engine` 写入 chunk 元数据用于溯源与质量对比。 |
| ⑤ | 是否引入 ORM/迁移框架？ | 否。表结构用 Pydantic `LanceModel` 声明；演进走**幂等材料化迁移**（建列 → 从旧 JSON 回填 → 新代码双读过渡）。 |
| ⑥ | 队列用 Celery/Redis 还是进程内有界池？ | **进程内有界线程池**。单机单进程下引入外部队列只会增加故障面；队列满时返回 503 而不是无限堆积，让调用方明确感知背压。 |
| ⑦ | 认证用单个静态 Token 还是一次性密钥？ | **可撤销的多密钥**（SHA-256 存储、读/写两级作用域）。单静态 Token 无法单独吊销、无法区分读写、泄露即全泄露。 |
| ⑧ | 前端如何加载受保护的原文文件？ | **HMAC 签名 URL**。`<iframe>` / `<img>` 无法携带 `Authorization` 头，签名 URL 是零依赖前提下唯一可行方案（TTL 1 小时）。 |

### 1.3 明确丢弃的能力

- 多租户、用户体系、OAuth、RBAC、细粒度文档权限（保留可撤销的 API 密钥，见 §9）
- 分布式、分片、集群、消息队列中间件（Celery / Redis / RabbitMQ）、任务调度平台
- GraphRAG / 知识图谱 / 实体关系抽取
- 多模态检索、图片/表格的结构化存储（解析器输出的 Markdown 表格只作为文本分块）
- 评估平台（RAGAS / A/B / 人工标注）、对话历史与多轮记忆
- Agent / 工具调用 / 工作流编排
- 前端工程化（框架、状态管理、构建产物、SSR）
- **多机并发写**：写锁是同机文件锁（flock），跨机不成立 ⇒ 多机共享一个 bucket
  时仍需单一写入者（§16）。同机多进程已支持（上一条在 2026-10 被这条取代）

---

## 1.4 选型回顾：LanceDB vs Chroma

针对本项目（单机、极简、LangChain 编排、hybrid 检索、分块编辑重向量化、FTS、版本回滚）的对比：

| 维度 | LanceDB | Chroma |
|---|---|---|
| 形态 | 嵌入式列存库（Rust），数据即目录，无独立服务进程 | 嵌入式（SQLite 底层 + hnswlib 向量文件）；也可连独立 server |
| 存储模型 | 列式（Lance），天然支持**多列、标量索引、版本管理、compaction** | 行式：meta 存 SQLite、向量存 hnswlib 文件，索引与数据分散 |
| 混合检索 | 原生 `query_type="hybrid"`（向量 + FTS 一次调用）+ 内置 Reranker | 需分别 `query` 与 `full_text_search` 后应用层融合；FTS/中文分词弱 |
| 中文关键词 | FTS 基于 Tantivy，可建 jieba 分词列，可控 | `full_text_search` 近似，同样依赖外部预处理 |
| 过滤 | `where(prefilter=True)` SQL 下推 + **标量索引 BTREE** | `where` 元数据过滤，无独立标量索引，大表偏慢 |
| 行级更新 | `update/delete/merge_insert` 一等公民 → **编辑重向量化一行落地** | `upsert/update`，更新向量需重算后 upsert，无 merge 语义 |
| 版本/回滚 | **内置版本快照 `versions/checkout/restore`** → 误删误改秒级回滚 | 无原生版本管理 |
| 性能 | 列存 + ANN（HNSW/PQ）+ `refine_factor`，大库优势明显 | hnswlib ANN 快，但规模化后 SQLite meta 成瓶颈 |
| LangChain | `langchain-lancedb`（可选）；本项目直接调原生 SDK 用满能力 | `langchain_chroma` 封装成熟，但同样只能 `similarity_search` |
| 运维/备份 | 拷目录即可（建议先 `optimize`） | 需同时拷 SQLite + 向量目录，二者易不一致 |
| 代价 | 引入 Lance 格式（pylance 较大），API 偏底层 | 上手快、文档友好，进阶能力需自研 |

**结论：本项目选 LanceDB 更合适**。决定性三点——(1) hybrid 检索与内置 Reranker 原生支持，省去自研融合；(2) `update/merge_insert` 让"分块编辑重向量化"成为一行操作；(3) 版本快照天然补偿无跨表事务的弱点。

---

## 2. 总体架构

```
┌──────────────────────────── 单进程 (uvicorn, 1 worker) ────────────────────────────┐
│ Web SPA (Svelte 5 → web/dist)             FastAPI  /api/*  (47 操作 / 32 路径)      │
│ ├ 知识库视图   ──fetch──►    ┌─ 认证中间件：API 密钥（read | write 作用域）          │
│ ├ 文档列表/上传              │  零密钥时管理端点开放（引导例外，§9）                │
│ ├ 分块编辑                   ├─ /api/search  /api/answer  /api/search/report      │
│ ├ 独立分块                   ├─ /api/kbs  /api/documents  /api/chunks  /api/jobs  │
│ ├ 检索页（参数分层）          ├─ /api/models  /api/embed  /api/keys               │
│ ├ 原文对照查看器              ├─ /api/health  /api/stats  /api/reconcile          │
│ ├ 设置页（模型/健康）         ├─ /api/reindex  /api/versions  /api/rollback        │
│ └ API 文档页（与 openapi    └─ StaticFiles  前端（签名 URL 供 iframe 取原文）      │
│    双向漂移测试守护）                                                              │
│                                            │                                       │
│  ┌──────── API 层 (rag/api/) ──────────────┼───────────────────────────────────┐   │
│  │ 路由：search · documents · chunks · kbs · jobs · models · apikeys · plan     │   │
│  │       auth(中间件) · system(health/stats/reindex/versions/rollback)          │   │
│  │ _common: raise_operation_error —— 领域异常 → HTTP 状态码的唯一映射点          │   │
│  └─────────────────────────────────────────┼───────────────────────────────────┘   │
│                                            │                                       │
│  ┌──────── 领域层 ─────────────────────────┼───────────────────────────────────┐   │
│  │ rag/retrieval/  search · answer · highlight                                  │   │
│  │ rag/ingest/     queue(有界池) · pipeline · splitter                          │   │
│  │ rag/models/     registry · embeddings · chat · rerank · testkit              │   │
│  │ rag/parsing/    router · loaders · normalize · segment                       │   │
│  │ rag/chunk_edit  编辑与重向量化（§8）                                          │   │
│  └─────────────────────────────────────────┼───────────────────────────────────┘   │
│                                            │                                       │
│  ┌──────── 存储层 (rag/storage/) ──────────┼───────────────────────────────────┐   │
│  │ backend(local | s3) · tables · schema · sql · plan · health                  │   │
│  │ repos/ documents · chunks · kbs · keys   ← 只有本层可接触数据库连接与建表     │   │
│  └─────────────────────────────────────────┼───────────────────────────────────┘   │
│                                            │                                       │
│  ┌──────── 基础设施 (rag/core/) ───────────┼───────────────────────────────────┐   │
│  │ config(pydantic-settings) · errors(领域异常) · signing(HMAC 签名 URL)        │   │
│  └──────────────────────────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────────────────────────┘
      │ 模型（本地或远端）                          │ 原文留档
      ▼                                             ▼
 Ollama / vLLM / llama.cpp（本地）· OpenAI 兼容 API（远端）   local: data/files/  |  s3: bucket
```

---

## 3. 模型接入层（LLM / Embedding / Rerank）

三类模型统一由 `ModelRegistry` 提供，**同一份配置结构、同一套测试流程**，可指向本地进程或远端 API。

### 3.1 实现矩阵

| 类别 | 本地部署 | API 接入 | 载体 |
|---|---|---|---|
| **Embedding** | Ollama `bge-m3`；本地 HF 权重；vLLM | OpenAI 兼容 `/v1/embeddings`（SiliconFlow / 智谱 / 通义等） | `OllamaEmbeddings`、`HuggingFaceEmbeddings`、`OpenAIEmbeddings(base_url=...)` |
| **Rerank** | 本地 cross-encoder 权重 | **任意 `/rerank` 端点**（Cohere / Jina / SiliconFlow 等） | 自研 `HttpReranker`（§3.4） |
| **LLM** | Ollama / vLLM / llama.cpp（暴露 OpenAI 兼容端点） | 任意 OpenAI 兼容 Chat Completions | `ChatOllama`、`ChatOpenAI(base_url=..., api_key=...)` |

- **统一约定**：凡"OpenAI 兼容"的本地服务与云端 API 使用**同一个客户端**，仅 `base_url` / `api_key` / `model` 不同 → 本地⇄远端切换零代码。
- **自定义协议兜底**：不兼容 OpenAI 的服务，实现对应协议即可注册，配置里用 `provider: custom`。
- **维度治理**：`embed.dim` 与表内实际维度不一致时，`/api/health` 报 `dimension_mismatch`，写入与检索会被拒绝并给出明确指引，必须走 `/api/reindex`。注意——**维度不符不会被伪装成"embedding 服务不可用"**（§3.5）。

### 3.2 配置结构

```toml
[embed]
provider = "custom"            # ollama | openai | huggingface | custom
model    = "BAAI/bge-m3"
base_url = "https://api.siliconflow.cn/v1"
api_key  = "sk-…"
dim      = 1024
batch    = 32
concurrency = 2                # 免费档并发有限，保守取 2
normalize = true

[rerank]
enabled  = true
provider = "api"               # api | none
model    = "BAAI/bge-reranker-v2-m3"
base_url = "https://api.siliconflow.cn/v1"
api_key  = "sk-…"
top_n    = 8

[llm]
provider = "custom"            # openai | ollama | custom
model    = "Qwen/Qwen2.5-7B-Instruct"
base_url = "https://api.siliconflow.cn/v1"
api_key  = "sk-…"
temperature = 0.2
max_tokens  = 1024

[retrieve]                     # 检索默认值；前端把它显示成 placeholder，
  mode = "hybrid"              # 让「留空 = 用多少」变成可见的具体数字
  top_k = 8
  candidate_k = 100              # 2026-10-06 真实召回实测：50 时跨主题查询
                                 # recall@10 只有 0.91–0.96，100 = 1.0
  window = 1
  snippet_chars = 0
  nprobes = 20                   # **注意：当前 lancedb 0.39 + IvfHnswFlat 下无效**
                                 # （20 与 999 的 recall@10 逐项相同），别拿它调召回
  k_rrf = 60
  refine_factor = 10             # 降到 1–2 会让 recall@10 从 0.91–0.96 塌到 0.61–0.79

[storage]                      # local（默认）或 S3 兼容（RustFS / MinIO / AWS）
backend  = "local"
endpoint = ""
bucket   = ""
region   = ""
access_key = ""
secret_key = ""
allow_http = true              # 自建 RustFS 常用 http

[features]
answer = false                 # 生成式问答总开关，默认关闭（保持"只检索"的纯粹性）
```

> **密钥落盘**：`config.toml` 含明文密钥，权限应为 `0600`，且 `data/` 必须在 `.gitignore` 内。
> `GET /api/models` 的响应始终对 `api_key` 打码。

### 3.3 连通性测试（`POST /api/models/test`）

| kind | 测试内容 | 返回 |
|---|---|---|
| `embedding` | 嵌入一句固定探针文本 | `ok / dim / latency_ms / throughput_texts_per_s` |
| `rerank` | 对 3 条固定样例（1 相关 + 2 无关）打分 | `ok / scores[] / latency_ms / sorted_correctly` |
| `llm` | 一次短对话 | `ok / latency_ms / 回复前 200 字` |
| `all` | 依次执行以上三项 | 汇总表 + 结论 |

- 测试**不落库**、不改变生效配置。
- 失败返回结构化错误（连接拒绝 / 401 / 404 / 超时 / 维度不符），前端直接给出修复建议。

**已实测**（SiliconFlow）：embedding 1024 维 / 150ms / 118 texts·s⁻¹；rerank 213ms 排序正确；llm 461ms。

### 3.4 Rerank 的真实实现

`provider="api"` 走 HTTP `POST {base_url}/rerank`，并做三件事适配各家差异：

1. **自动探测并缓存前缀**：先试 `{base_url}/rerank`，失败再试 `{base_url}/v1/rerank`，成功后记住。
2. **兼容两种响应体**：接受 `results` 或 `data` 字段；分数接受 `relevance_score` 或 `score`。
3. **按 index 回填**：若服务只返回 `top_n` 条，按 `index` 回填到原候选顺序，未返回的置底——避免"重排后错位"。

**优雅降级**：任何失败都退回召回序打分、置 `degraded_reason`，并把 `score_kind` 标为 `rerank`。
> 这修掉了一个会误导用户的 bug：降级时分数曾被标成 `rrf`，用户以为是融合分。

### 3.5 错误分类（`raise_operation_error`）

领域异常到 HTTP 状态码的映射**只有一个地方**：

| 领域异常 | HTTP | 说明 |
|---|---|---|
| `EmbedUnavailable` | 503 | 模型服务确实不可达 |
| `DimensionMismatch` | 400 | **维度不符是配置错误，不是服务不可用** |
| `Invalid` | 400 | 参数非法（空标题、越界阈值等） |
| `NotFound` | 404 | 资源不存在 |
| `QueueFull` | 503 | 入库队列已满（§6） |

> 此前维度不符会被包装成"embedding 服务不可用"，把用户引向排查网络而不是配置；`/resplit` 还曾裸抛 500 并把完整 traceback 返回给客户端。

---

## 4. 文档解析层

### 4.1 引擎定位

| 引擎 | 载体 | 强项 | 代价 | 适用 |
|---|---|---|---|---|
| **Docling** | `langchain_docling.DoclingLoader` | 复杂排版：多栏、表格、图文混排、公式；内置 OCR 与版面模型 | 重（首次下载模型数百 MB），最慢 | PDF 论文/手册/财报等复杂版式 |
| **PyMuPDF4LLM** | 直接调 `pymupdf4llm.to_markdown(page_chunks=True)` | 极快，直接产出 LLM 友好的 Markdown，**保留页码映射** | 无表格语义与 OCR | 纯文本 PDF、大批量快速入库 |
| **Unstructured** | `langchain_unstructured.UnstructuredLoader` | 格式覆盖最广（PDF/DOCX/PPTX/XLSX/HTML/EML/图片…） | 依赖重；本地分区要求 Python ≥3.11 | 办公文档与长尾格式 |
| **native（内置）** | 自研：`markdown`/`txt` 直读 + `pypdf` 兜底；HTML 用标准库 `html.parser` 抽正文 | 零依赖、永不失败 | 质量最低 | 兜底 & 极简部署 |

> **为何不用 `PyMuPDF4LLMLoader`**：`langchain-community 0.4.2` 已移除该类；改用核心依赖 `pymupdf4llm` 的 `page_chunks=True`，反而直接拿到逐页文本与页码映射。此前所有 PDF 上传都会 500。

- 所有引擎统一归一化为内部 `ParsedDoc{text, pages[], headings[], meta}`。
- 每个 chunk 记录 `parser_engine`，可用于对比同一文档在不同引擎下的检索效果。

### 4.2 路由与回退

```
engine = auto (默认)
  ├ PDF（含扫描件/图表多） → docling（可配 ocr=true）
  ├ PDF（纯文本、批量）    → pymupdf4llm
  ├ DOCX/PPTX/XLSX/HTML/EML → unstructured（hi_res）
  ├ MD/TXT                 → native
  └ 失败 → 回退链：docling → pymupdf4llm → native（每步记录 error，最终 status=failed）
```
可按 `mime`/扩展名覆写，也支持单次请求 `?engine=docling` 强制指定。

### 4.3 URL 抓取的安全约束

`load_url` 有**协议白名单**（仅 `http`/`https`）与 **SSRF 防护**：拒绝环回、私网、链路本地地址，避免"用服务端当跳板探测内网"。

### 4.4 依赖与 Python 版本

- 核心（必装）：`langchain`、`langchain-community`、`langchain-text-splitters`、`lancedb`、`fastapi`、`uvicorn`、`pydantic-settings`、`pymupdf4llm`。
- 可选 extras：`[docling]`、`[unstructured]`、`[local-models]`、`[jieba]`。
- ⚠️ **Python 版本**：`unstructured 0.18+` 要求 **≥3.11**；其余依赖均 ≥3.10。因此基线定为 **3.11**。若必须在 3.10 运行，`UnstructuredLoader` 走 API 模式或锁 `unstructured<0.17`。

---

## 5. 存储层

### 5.1 后端抽象（`rag/storage/backend.py`）

| 后端 | 用途 | 说明 |
|---|---|---|
| `local`（默认） | 数据即目录 | LanceDB 直连路径；原文留档落 `data/files/` |
| `s3` | S3 兼容对象存储 | 支持 RustFS / MinIO / AWS S3。**path-style 寻址** + `allow_http`（自建服务常用 http）；LanceDB 经 `storage_options` 接入，原文归档走 `pyarrow.fs` |

关键设计：`local_path()` **仍然保留**——大 PDF 走 `FileResponse`（原生支持 HTTP Range，可比对象存储流式转发更好地支持拖拽进度）。S3 后端下无法本地映射的文件才走流式转发。

切换方式（环境变量，前缀 `RAG_`）：
```bash
RAG_STORAGE__BACKEND=s3
RAG_STORAGE__ENDPOINT=http://127.0.0.1:9000
RAG_STORAGE__BUCKET=raggi
RAG_STORAGE__ACCESS_KEY=...  RAG_STORAGE__SECRET_KEY=...
```

### 5.2 表结构（`lancedb.pydantic.LanceModel`）

```python
class Document(LanceModel):
    doc_id: str                      # uuid
    title: str
    source_uri: str | None
    mime: str
    parser_engine: str               # docling|pymupdf4llm|unstructured|native
    content_hash: str                # 去重 & 幂等
    char_count: int
    chunk_count: int
    text: str                        # 归一化后的纯文本（查看器与重切的依据）
    status: str                      # indexing|ready|failed
    error: str | None
    kb_id: str                       # ★ 真实列（原为 meta.kb_id）
    stored_file: str | None          # ★ 真实列（原为 meta.stored_file）
    meta: str                        # 其余零散元信息（JSON 字符串）
    created_at: str; updated_at: str

class Chunk(LanceModel):
    chunk_id: str                    # uuid，"编辑"的稳定主键
    doc_id: str
    ordinal: int                     # 文档内顺序 → 上下文窗口 / 定位
    text: str                        # 分块原文（可编辑）
    text_seg: str                    # 中文分词列（jieba，空格分隔）→ FTS 索引目标
    heading_path: str                # "3.2 > 配置" 面包屑
    page: int | None
    char_start: int; char_end: int   # 相对原文纯文本偏移 → 精确高亮
    token_count: int
    origin: str                      # parsed | manual
    edited: bool                     # 是否被人工改写
    enabled: bool                    # 停用后退出检索，但保留数据
    embed_model: str
    vector: Vector(1024)             # dim 由 [embed].dim 决定
    created_at: str; updated_at: str

class Job(LanceModel):
    job_id: str; doc_id: str
    stage: str                       # load|split|embed|write|index|done|failed
    progress: float; total: int
    parser_engine: str; error: str | None
    started_at: str; ended_at: str | None

class KnowledgeBase(LanceModel):
    kb_id: str; name: str; description: str
    chunk_size: int; overlap_ratio: int      # ★ 两级方案的具体值（§6）
    plan_custom: bool                        # 是否偏离系统默认
    created_at: str; updated_at: str

class ApiKey(LanceModel):
    key_id: str; name: str
    key_hash: str                    # ★ 只存 SHA-256，明文仅创建时返回一次
    scope: str                       # read | write
    created_at: str; last_used_at: str | None
    revoked_at: str | None           # 吊销幂等，保留行以便审计
```

> **`kb_id` 与 `stored_file` 的列化迁移**：上一版把 `kb_id` 存在 `meta` JSON 里，导致按知识库过滤无法走标量索引。现已提升为真实列，迁移**幂等**：建列 → 从旧 JSON 回填 → 未迁移的库读取时仍能正确回退。
> 同时把散落各处的 20 处 `json.loads` 收敛到 2 处（由测试守护）。

### 5.3 充分使用的 LanceDB 能力

| 能力 | 用途 |
|---|---|
| 向量索引 | `IVF_HNSW_SQ` / `HNSW` / `IVF_PQ`，按规模自适应 |
| 全文索引 | `create_fts_index(["text_seg"])`（tantivy 后端） |
| 标量索引 | `doc_id` / `origin` / `status` / **`kb_id`** → 让 `where` 走索引而非全扫 |
| 混合检索 | `search(query_type="hybrid").vector(q_vec).text(q_seg).where(..., prefilter=True)` |
| Reranker | `RRFReranker(K=60)` 融合；精排由 §3.4 的 HTTP reranker 承担 |
| 召回调优 | `.nprobes(n).refine_factor(r)` |
| 行级更新 | `table.update(where=..., values={...})` → 编辑重向量化的核心 |
| 幂等写入 | `merge_insert("content_hash").when_matched_update_all()` |
| 版本管理 | `versions()` / `checkout(v)` / `restore(v)` → 误删误编辑秒级回滚 |
| 维护 | `optimize()`（compaction）、`cleanup_old_versions()` |

> **注意**：`db.list_tables()` 返回的是 `ListTablesResponse`，不是表对象列表——直接遍历会对每个元素调用 `.open()` 而报 AttributeError。改用 `table_names()`，同时消除弃用告警（告警数 423 → 134）。

### 5.4 中文检索的两个硬问题与对策

1. **FTS 分词**：tantivy 默认分词对中文不友好（整句成词）。对策：**写入时生成 `text_seg`（jieba 预分词，空格分隔）并对该列建 FTS 索引**；查询时用同一分词器处理 query。
2. **短查询**：纯关键词通道对 ≤2 字查询召回差。对策：`hybrid` 时向量通道兜底；FTS 零命中且结果不足时自动放宽为 `vector` 单通道并带 `degraded_reason`。

### 5.5 一致性（无跨表事务/外键的补偿）

- **两阶段写入**：① `chunks.add(batch)` → ② `documents.merge_insert` 更新 `chunk_count/status`。失败则在 `jobs` 记录 stage，documents 保持 `failed`，由对账发现。
- **对账（`GET /api/health` / `POST /api/reconcile`）**：`COUNT(chunks WHERE doc_id=d) == documents.chunk_count`；孤儿 chunk；跨表 `embed_model` 一致性；`fts_stale_count`；向量索引覆盖状态；磁盘占用；版本数。
  > **检查跑不成不等于检查通过**（2026-10-06 修）：原先 `except Exception: fts_stale = 0`
  > 与 `except Exception: embed_model_mismatch = False` 会把「没查成」翻译成
  > 「一切正常」，而后者还直接参与 `status`。现在失败的检查名进 `checks_failed`，
  > 未知值用 -1 而不是 0，且**跑不成就不报 ok**。
  > 实际计数与对账共用 `repos/chunks.chunk_counts_by_doc` 一份口径
  > （原先两处各写一份，既可能算出不同结果，又一处会把全部分块行 materialize 进内存）。
- **修复**：`POST /api/reindex`（按 `documents` 重放，保留 `doc_id`/`chunk_id`，重建 FTS 与向量索引）；误操作用 `restore(version)` 回滚。
  > `POST /api/reconcile` 的写**必须走 `set_doc_fields`**（引擎分派）。
  > 它原先直接 `store.documents.update(...)` 写 Lance 旧副本，而 documents 的真源
  > 是 SQLite ⇒ 接口回报 `fixed: 1` 而 `/api/health` 的 `count_mismatch` 一条没少，
  > 全程无异常。由 `tests/test_meta_store.py::test_reconcile_fixes_the_engine_that_holds_the_truth` 钉住。
- **写入串行化**：写入路径由 `storage/filelock.py` 的**两层锁**保护 ——
  `threading.RLock`（线程互斥、可重入）+ `fcntl.flock(data/lance-write.lock)`
  （进程互斥）。只换 flock 会丢线程互斥（flock 的互斥单位是 fd，同进程多线程
  共用 fd 时第二个线程直接成功），所以两层都要，`tests/test_perf_gates.py` 有守卫。
  对象存储后端下本地锁不成立，自动退回进程内锁并告警（§16）。
- **OLTP 与向量分引擎**：`jobs`/`apikeys`/`kbs`/`documents` 走
  SQLite(WAL, `data/raggi.db`)，`chunks`（向量 + 全文）留在 LanceDB。
  理由是一条实测数据：一次入库曾在 `jobs` 表上留下 ≈5 个版本/文件，
  48 次入库后读延迟翻 2.3 倍（`docs/PERF-CONCURRENCY-2026-10-05.md` §2.6）。
  分派收在仓储层，`storage.meta_engine=lancedb` 是不用恢复备份的应急回退。

---

## 6. 切分与入库流水线

### 6.1 两级分块方案

| 层级 | 位置 | 说明 |
|---|---|---|
| **系统默认** | `[chunking]` 配置 | 全局兜底值 |
| **知识库级** | `kbs.chunk_size` / `overlap_ratio` | **始终是具体值**，创建时即材料化为当时的系统默认 |
| **文档级（可选）** | `documents.meta` | 单篇覆盖，不设则用所属知识库的值 |

> **为何去掉"继承全局"这一状态**：上一版允许知识库存 `0` 表示"运行时继承"，于是同一行数据的含义取决于读取时刻的全局配置——改一次全局值，所有历史知识库的切分行为静默改变，而文档里存的快照对不上。现在知识库**永远持有具体值**，`plan_custom` 只表示"是否偏离系统默认"。

`GET /api/documents/plans` 批量返回各文档的生效方案，供列表页标注「需重切」。

### 6.2 流水线

1. **加载**：`LoaderRouter` 选引擎 → `loader.load()` → `Normalizer`（提取 `page`、`heading_path`、表格转 Markdown、统一换行）。
2. **切分**：
   - Markdown 类输出：`MarkdownHeaderTextSplitter`（按 `#/##/###` 切，继承标题路径）→ `RecursiveCharacterTextSplitter`；
   - 纯文本：`RecursiveCharacterTextSplitter`；超长硬切兜底；
   - **必须保留 `char_start/char_end`**：由归一化后的纯文本累加计算，是高亮与查看器定位的唯一依据（不接受正则二次匹配）。
3. **向量化**：`embed_documents(batch)`，并发受限，失败退避重试 3 次；结果按序对齐，**禁止乱序写入**。
4. **写入**：构造 `pyarrow.Table` → `chunks.merge_insert` 幂等写入 → 更新 `documents` → `jobs.stage=done`。
5. **索引**：首次写入后创建向量索引与 FTS 索引；后续按阈值 `optimize()`。

### 6.3 入库队列（`rag/ingest/queue.py`）

| 项 | 行为 |
|---|---|
| 工人数 | `ingest_workers`，默认 2（有界线程池） |
| 队列容量 | 32；**满则返回 503**（`QueueFull`），不无限堆积 |
| 同步模式 | **默认**（向后兼容；约 19 个测试依赖） |
| 异步模式 | `wait=false` 立即返回 `job_id`，前端轮询 `GET /api/jobs/{job_id}` |
| 临时文件 | 上传文件的**所有权归队列**——此前 worker 还在读、请求已把临时文件删掉，导致偶发失败 |
| 取消 | `DELETE /api/jobs/{job_id}`，**如实区分处置方式**（见下） |

任务行在**入队时**即写入（不是开工时），因此排队中的任务也能被查询到；`doc_id` 在成功与去重两条分支上都会回写。

**取消的能力边界**（必须如实说明，不能一律显示"已取消"）：

| 任务状态 | 处置 | 说明 |
|---|---|---|
| 排队中 | **真取消** | `Future.cancel()` 从池里撤销，永不执行；任务行记 `cancelled` |
| 运行中 | **协作式取消** | 置取消标记，任务在**下一个阶段边界**（解析后、向量化后）自行退出。解析（PyMuPDF）与向量化（HTTP）都不响应中断，已发出的调用必须跑完 |
| 已终态 | 幂等 | `outcome=finished`，不报错 |

设计取舍：检查点只放在**阶段之间**——此刻没有任何半成品需要回滚，退出是干净的（重解析时旧分块还在，文档不会被标成 failed，也不会留下计数漂移）。`cancelled` 是终态，故不再出现在 `active_only=true` 的列表里。

---

## 7. 检索内核

```
query
 ├─ embed(query) → q_vec ；jieba(query) → q_seg
 ├─ LanceDB hybrid：search(query_type="hybrid").vector(q_vec).text(q_seg)
 │      .where(filters, prefilter=True).nprobes(n).refine_factor(r).limit(candidate_k)
 ├─ rerank：RRFReranker（融合）→ 可选 HTTP 精排（§3.4）→ top_k
 └─ 装饰：snippet 高亮（按 char 偏移切片，非正则）+ 上下文窗口（ordinal ±N）
```

### 7.1 统一分数口径

各通道原生分数含义不同、方向也不一致：

| 通道 | 原生字段 | 方向 |
|---|---|---|
| vector | `_distance` | **越小越相关** |
| fts | `_score` | 越大越相关 |
| hybrid | `_relevance_score` | 越大越相关 |

响应里的 `score` 一律归一化为**越大越相关**，并用 `score_kind` 说明当前口径（`vector` / `fts` / `rrf` / `rerank`），原始值保留在 `scores` 里。

> 这修掉了一个隐蔽的 bug：阈值比较的方向曾随模式反转——同一个 `score_threshold` 在 vector 模式下筛掉的是**最相关**的那批。

### 7.2 参数分层

| 层 | 参数 |
|---|---|
| **召回** | `candidate_k`、`nprobes`、`refine_factor`、`k_rrf`、`rerank` |
| **返回** | `offset`、`score_threshold`、`group_by_doc` |
| **展示** | `include_context`、`snippet_chars`、`highlight` |

- `mode`：`hybrid`（默认）| `vector` | `fts`；`candidate_k` 默认 50。
- 过滤：`kb_id`、`origin`、`parser_engine`、`mime`、`include_disabled`、`doc_ids` —— 经 `prefilter=True` 下推到扫描前，避免"先召回后过滤"导致的空结果。
- 响应含 `query`（回显）、`has_more`、`total_candidates`、`took_ms`、`degraded_reason`。
- 报告导出强制 `highlight=off`（Markdown 里不需要 `<mark>` 标签）。

### 7.3 前端参数可用性

- **参数分层展示**：「范围与模式」平铺在查询框下方，调优参数（`nprobes`/`k_rrf`/`refine_factor`）收进折叠的「高级参数」——17 个控件全部平铺会让第一次使用的人被术语劝退。
- **默认值可见**：数字输入框的 placeholder 取 `/api/models` 返回的 `retrieve` 生效值。空输入框既可能被读成 0 也可能被读成未设置，显示出来就不需要猜。
- **生效过滤可见**：生效的过滤条件渲染成可单独移除的 chip；0 命中时明确提示"先看看是不是上面的过滤条件挡住了"。过滤条件跨会话保留在 URL 里，不显示的话用户完全不知道它们还在生效。

---

## 8. 分块编辑与重向量化（核心能力）

**需求**：对于非文档解析产生的分块（手动录入 / API 直写），允许直接修改其原始内容并重新向量化。

### 8.1 数据约定

- `origin = manual | parsed`：`manual` 分块允许自由编辑；`parsed` 分块默认只读，需显式 `force=true` 才能改，改写后 `edited=true` 且保留 `original_text`。
- `chunk_id` 为稳定主键 → `table.update(where=f"chunk_id='{id}'", ...)` 精确定位。
- `enabled=false` 的分块**退出检索**但保留数据——是"临时屏蔽"而非删除。

### 8.2 写入路径

```
PATCH /api/chunks/{id}  { text, reembed=true, force=false }
  → 校验（origin/权限/长度）
  → text_seg = jieba(text)（配置开启时）
  → vector = embeddings.embed_documents([text])[0]
  → table.update(where=f"chunk_id='{id}'",
                 values={"text","text_seg","vector","edited":true,"embed_model","updated_at"})
  → FTS 一致性：fts_stale += 1
      · chunk 总量 < 5 万：同步 create_fts_index(replace=True)（亚秒级）
      · 否则：累计到阈值（默认 200）或空闲窗口触发后台重建
  → 返回 { chunk_id, edited, embed_model, took_ms, fts_stale }
```

- 批量编辑：`PATCH /api/chunks` 接受数组，一次 embed 批量、一次 `merge_insert` 落地。
- 批量启停：`PATCH /api/chunks/batch-enabled`（`{chunk_ids, enabled}`）一次写入完成，**要么全改要么全不改**——前端原先在客户端循环调单条端点，200 条就是 200 个请求且中途失败后「哪些块被停用了」无法回答。任一 ID 不存在即整体 404，不做部分成功。
- 批量删文档：`DELETE /api/documents/batch`（`{doc_ids}`）一次加锁并**在删除前先取出留档文件名**（删掉后就查不到了）；已不存在的 ID 被跳过并在 `skipped` 里如实回报。
- 手动新增：`POST /api/chunks`（`origin=manual`，无 `doc_id` 时归入虚拟文档，同样可被检索与查看）。
- 删除：`DELETE /api/chunks/{id}`；删除文档用 `table.delete(where="doc_id=...")`。

### 8.3 一致性要点

- 向量索引无需立即重建（未索引部分查询时补充扫描），但**FTS 索引必须重建**才会反映新词——这是本设计中最容易被忽略的坑，故用 `fts_stale_count` 显式治理并在前端展示。
- 编辑不改 `char_start/char_end` 的语义：若改写导致长度变化，`char_end` 标记为 `null` 并置 `offset_valid=false`，查看器降级为按 `ordinal` 定位。

---

## 9. 认证与访问控制

### 9.1 API 密钥

| 项 | 设计 |
|---|---|
| 存储 | **只存 SHA-256 哈希**，明文仅在创建时返回一次 |
| 格式 | `rg_z27h7qxm_eZKNFA4u…`（前缀便于识别与日志脱敏） |
| 传输 | `Authorization: Bearer <key>` 或 `X-API-Key: <key>` |
| 校验 | `hmac.compare_digest`（常量时间，防时序侧信道） |
| 作用域 | `read`：检索/查看/列表；`write`：入库/编辑/删除/配置/密钥管理 |
| 吊销 | 幂等；行保留以便审计 |
| 索引 | `key_hash` 建 BTREE |

**引导例外**：密钥数为 0 时，管理端点开放——否则第一个密钥创建后立刻生效，浏览器后续的 `loadKeys` 会 401，形成自锁。这是**刻意的**，不是遗漏。

**防提权**：`read` 密钥可以**列出**但不能创建/吊销密钥——否则 read 就能给自己升到 write。

**旧凭据兼容**：`RAG_TOKEN` 仍可用，等价于 `write` 作用域。

### 9.2 签名 URL

`<iframe>` / `<img>` 无法携带 `Authorization` 头，因此原文查看走 **HMAC 签名 URL**：

- TTL **1 小时**（早先 10 分钟，实测看 PDF 时中途过期太频繁）。
- 路径穿越防护 + 常量时间比较。
- `local` 后端用 `FileResponse`（原生 Range 支持）；`s3` 后端流式转发。

> 也考虑过直接放宽该路由的鉴权，但那等于把原文暴露给任何能访问端口的人——签名 URL 在零依赖前提下是唯一可行的折中。

### 9.3 中间件注意点

认证失败**必须在中间件里显式构造响应**。Starlette 的 `ExceptionMiddleware` 位于栈的更内层，在中间件里 `raise HTTPException` 会变成 **500 空响应体**——排查时几乎无从下手。

---

## 10. API 总表

全部端点均可被外部客户端编程调用（不限前端页面）；配置 `cors_origins` 后支持跨域；设置 API 密钥后需 `Authorization: Bearer <key>`（§9）。**端点数的双向漂移由 `tests/test_apidoc.py` 守护**：文档列了不存在的端点会失败，后端新增而未记录也会失败。

### 10.1 契约（OpenAPI 3.1）

机器可读契约在 `/openapi.json`（Swagger UI 在 `/docs`），并由 `tests/test_openapi_contract.py` 守卫：

- **响应模型**：47 个端点全部声明返回类型（`rag/api/responses.py` 是唯一事实来源），`/openapi.json` 里 200 响应带真实 schema，客户端可生成类型；仅字节流（原文下载）与 Markdown 报告例外，两者显式声明各自的媒体类型。
- **行模型用 `extra="allow"`**：LanceDB 行结构随表列演进，未知列透传而不是被静默丢弃——契约里已知字段是文档化的，加列不会让 API 悄悄少返回数据。
- **安全方案**：`ApiKeyBearer`（Authorization: Bearer）与 `ApiKeyHeader`（X-API-Key）两种等价凭据声明在 OpenAPI 里，`/docs` 有 Authorize 入口；真正的校验仍在鉴权中间件（支持密钥与旧版静态 token，并放行静态资源与预检）。
- **分组**：每个操作都带 tag（documents / chunks / knowledge-bases / plans / search / answer / models / keys / jobs / system），并按资源给出 tag 说明。
- **错误契约**：统一 `{"detail": "..."}`（模型 `ErrorOut`）；401/403 对所有鉴权后的操作成立，资源端点声明 404，检索/入库声明 503（模型服务不可用或队列已满），上传声明 413，原文丢失声明 410。领域异常（`rag/core/errors.py`）由全局 handler 兜底映射为对应状态码，不会再退化成裸 500。
- **字段别名**：`KbPlanOut.global` 因 Python 关键字在模型里命名为 `global_`，通过 alias 序列化回 `global`，测试锁住这一行为。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/search` | 检索（§7）：`{q, mode?, top_k?, kb_id?, offset?, score_threshold?, group_by_doc?, …}` |
| GET | `/api/v1/search` | 检索（query 参数形式） |
| POST | `/api/v1/search/report` | 导出 Markdown 检索报告 |
| POST | `/api/v1/answer` | 生成式问答（`features.answer` 未开启返回 503） |
| POST | `/api/v1/answer/stream` | 问答流式输出（SSE：sources → delta* → done，引用先于正文） |
| GET | `/api/v1/kbs` | 知识库列表（含 `doc_count` / `chunk_count` 聚合） |
| POST | `/api/v1/kbs` | 创建知识库 |
| GET | `/api/v1/kbs/{kb_id}` | 知识库详情 |
| PUT | `/api/v1/kbs/{kb_id}` | 重命名 / 更新描述 |
| DELETE | `/api/v1/kbs/{kb_id}` | 删除知识库（级联文档、分块与留档原文） |
| GET | `/api/v1/kbs/{kb_id}/plan` | 知识库分块方案（含系统默认对照） |
| PUT | `/api/v1/kbs/{kb_id}/plan` | 设置分块方案 |
| POST | `/api/v1/documents` | 入库：`multipart(file)`；参数 `engine`、`kb_id`、`wait` |
| POST | `/api/v1/documents/text` | 纯文本入库 |
| POST | `/api/v1/documents/batch` | 批量文本入库（逐条独立成/败） |
| POST | `/api/v1/documents/url` | URL 入库（正文抽取，含 SSRF 防护） |
| GET | `/api/v1/documents` | 列表：`?q=&status=&parser_engine=&mime=&kb_id=&limit=&offset=` |
| GET | `/api/v1/documents/{doc_id}` | 文档详情：元数据 + 原文 + chunk 索引 |
| PUT | `/api/v1/documents/{doc_id}` | **更新可编辑元信息（标题）** |
| DELETE | `/api/v1/documents/{doc_id}` | 删除文档、chunks 与留档原文 |
| DELETE | `/api/v1/documents/batch` | 批量删除文档（一次加锁，级联分块与留档原文；skipped 回报已不存在的） |
| POST | `/api/v1/documents/{doc_id}/reparse` | 用留档原文重新解析（保留 `doc_id` 与归属） |
| POST | `/api/v1/documents/{doc_id}/resplit` | 重新切分并重算向量（保留手工块） |
| GET | `/api/v1/documents/{doc_id}/plan` | 文档的分块方案（含继承来源） |
| PUT | `/api/v1/documents/{doc_id}/plan` | 覆盖单篇分块方案 |
| GET | `/api/v1/documents/plans` | 批量取多篇文档的生效方案（列表页标注「需重切」用） |
| GET | `/api/v1/documents/{doc_id}/file` | 原文文件（签名 URL，§9.2） |
| GET | `/api/v1/chunks` | 分块列表：`?doc_id=&q=&limit=&offset=` |
| GET | `/api/v1/chunks/{chunk_id}` | 单个分块详情 |
| POST | `/api/v1/chunks` | 新增手动分块 |
| PATCH | `/api/v1/chunks/{chunk_id}` | 编辑原文 + 重向量化（§8） |
| PATCH | `/api/v1/chunks` | 批量编辑：`{edits: [{chunk_id, text, force?}]}` |
| PATCH | `/api/v1/chunks/{chunk_id}/enabled` | 停用/启用（退出或恢复检索） |
| DELETE | `/api/v1/chunks/{chunk_id}` | 删除单个分块 |
| PATCH | `/api/v1/chunks/batch-enabled` | 批量停用/启用分块（一次写入，不触发 embedding） |
| GET | `/api/v1/models` | 模型配置（`api_key` 打码）+ **`retrieve` 生效默认值** |
| PUT | `/api/v1/models` | 模型配置写入（落 `config.toml`；`embed.dim` 变更带 reindex 警告） |
| POST | `/api/v1/models/test` | 三类模型连通性与性能测试（§3.3） |
| POST | `/api/v1/embed` | 文本向量化：`{texts: [...]}` |
| GET | `/api/v1/keys` | 密钥列表（**只显示哈希前缀与元信息**） |
| POST | `/api/v1/keys` | 创建密钥（明文仅此一次返回） |
| DELETE | `/api/v1/keys/{key_id}` | 吊销密钥（幂等） |
| GET | `/api/v1/jobs` | 任务列表 |
| GET | `/api/v1/jobs/{job_id}` | 任务进度（stage / progress / error / `terminal`） |
| DELETE | `/api/v1/jobs/{job_id}` | 取消入库任务（排队中真取消；运行中协作式，在阶段边界退出） |
| GET | `/api/v1/health` | 健全性：计数对账、孤儿、`fts_stale_count`、维度一致性、版本数、磁盘；`checks_failed` 列出**根本没跑成的检查**（-1 = 未知，不是 0） |
| GET | `/api/v1/stats` | 容量统计：行数 / 索引状态 / 版本数 / 磁盘占用 |
| POST | `/api/v1/reconcile` | 执行对账并修复发现的不一致 |
| POST | `/api/v1/reindex` | 重建索引（可选 `engine` 重解析） |
| GET | `/api/v1/versions` | LanceDB 表版本快照列表 |
| POST | `/api/v1/rollback` | 回滚指定表到历史版本 |
| GET | `/` | 前端静态页 |

**错误响应统一为 `{"detail": "..."}`**，且不泄露内部路径与 traceback。

---

## 11. 前端设计（Svelte 5 + Vite）

源码 `web/src/`（`.svelte` + `.ts`），Vite 构建产物到 `web/dist/`，由 `StaticFiles(directory=web/dist, html=True)` 托管——**构建只在开发机上发生，运行时零依赖**。

为何换成 Svelte：早前是手写 DOM + `tsc` 直出，视图间的重复渲染代码、手动查询与 `innerHTML` 替换让界面既难改也容易出错（`$("#x")?.` 找不到元素直接抛错的陷阱即一例）。Svelte 用编译器把「状态 → 视图」交给框架，输入过滤、行内操作与对话组件不再各自维护 DOM。

构建与检查：

```bash
npm install --include=dev
npm run build     # Vite → web/dist（构建前自动清空该目录）
npm run check     # svelte-check --tsconfig ./tsconfig.json（0 error 才放行）
npm run dev       # 开发用 Vite dev server（独立端口）
```

- **仓库内只有源码**：`web/dist/` 已 gitignore；`web/src/` 下不得出现编译产物（否则会形成两份事实来源）。产物缺失或过期会在启动时记录日志（`_check_web_build()`），`tests/test_frontend_wiring.py` 亦会拦截。
- **`svelte.config.js`** 只配 `vitePreprocess()`，供 svelte-check 与编辑器解析 `<script lang="ts">`；构建本身由 `vite.config.ts` 驱动（`base: "./"`，产物可被任意子路径托管）。
- **`types.ts`**：与 `rag/api/*.py` 的实际返回结构一一对应，是前端唯一的响应形状事实来源。
- **路由**是 hash 路由；`lib/router.svelte.ts` 把 hash 解析为 `$state`，视图由 Svelte 条件渲染切换。
- **图标**统一来自 `index.html` 里的 SVG 精灵（`#i-*`，24×24 同描边），经 `ui/Icon.svelte` 使用；**不再混用 emoji 或 unicode 字符**——字符图标在不同字体下的粗细与占位各不相同，界面会显得杂。
- **面包屑**由 `lib/crumbs.svelte.ts` 集中管理：视图在加载时 `setCrumbs(...)`，面包屑栏统一渲染（只有一层时隐藏）。它现在位于**内容列顶部**而不是顶栏下方——位置指示属于当前内容，dock 已经承担了"在哪个 section"这一层。

### 11.1 视图

| 路由 | 视图 |
|---|---|
| `#/` | 知识库总览：卡片网格（文件夹图标/描述/文档·分块·更新时间/方案徽标）+ 刷新/新建。**卡片上只有"进入设置"，没有删除**——删除库的入口全站唯一，在设置页危险区 |
| `#/kb/{id}/docs` | 工作区·文档标签：入库面板（文件/文本/URL 三个标签页 + 解析引擎选择）、页头动作（入库/刷新）、表格（标题/方案/引擎/块数/状态/入库时间 + 行内操作）、服务端标题过滤（`?q=` + debounce）、offset 分页、处理中静默轮询 |
| `#/kb/{id}/chunks` | 工作区·分块标签：该库**全部**分块，含不属于任何文档的独立块；范围用 `?scope=standalone` 切换（服务端 `only_standalone`），不再是单独一页。内容过滤走服务端 `?q=`（分页前过滤，`total` 是命中块数），0 命中时过滤框与「清空过滤」仍在——不许把空结果说成"库里没有分块" |
| `#/kb/{id}/doc/{id}` | 文档详情（仍在工作区内，标签条常驻）：**左侧原文对照 + 右侧分块表**（虚拟化窗口渲染），行内编辑（重向量化）/停用启用/删除、批量启停、新增分块、文档重命名、分块方案覆盖、重新解析/重新切分 |
| `#/kb/{id}/settings` | 工作区·设置标签：基本信息 / 分块方案 / 内容概览 / 危险操作（**唯一的删除库入口**，删除需输入库名）。两个可编辑面板**各自判脏**——共用的那次重载曾把方案面板里未提交的编辑抹掉；方案表单还拦住"取消勾选继承 + 大小留空"（后端把 `chunk_size=0` 解释成恢复继承，放过去就是一次静默错写） |
| `#/jobs` | 任务列表：队列全景（阶段/耗时/产出/失败原因全文可展开）、四类筛选、有任务在跑才轮询（静默）、进行中可直接停止 |
| `#/search` | 检索页（§7.3）：页内查询栏（`/` 聚焦）、常用参数平铺 + 高级参数折叠、生效过滤 chips、结果列表 + **右栏原文检视**（上下文与整篇高亮）、可展开的**流式问答面板**（引用先出、`[n]` 可点、真实可中断） |
| `#/settings/models` | 模型设置：三卡片（provider/model/base_url/api_key/dim，密钥可显隐）+ 每卡片独立连通性测试（结果给"连通/不通 + 延迟/维度/吞吐/排序是否正确"的结论，不再甩一坨 JSON）+ 保存并热切换（维度变更需二次确认）。**有未保存改动时才可见可点**：脏判定比的是打开时的快照，改回原值即恢复禁用 |
| `#/settings/keys` | 访问密钥：签发（名称/权限/有效期/备注）、一次性明文展示、状态/最近使用/过期、吊销；**本机凭据**面板管理浏览器侧 token 与密钥 |
| `#/settings/health` | 健康页：键值化状态（✔/⚠ 徽标）+ 索引状态 + 重建索引 / 修复计数 + 原始 JSON 折叠区 |
| `#/settings/data` | 数据与版本：容量（文档/分块/任务数、数据与索引目录占用、磁盘剩余、已建索引）+ LanceDB 表版本列表 + **回滚**。这三个端点此前完全没有界面，而回滚是单机库最重要的一层自救手段。回滚按表生效不跨表，所以确认要手输表名，后端的 `note`（"仅回滚指定表；请用 /api/health 对账"）原文照抄并给出对账入口 |
| `#/apidoc` | API 文档页：概览（Base URL/鉴权/错误约定）+ 接口索引 + 分组端点卡片（参数表、curl/示例与复制）。**过滤词同步进 URL**（`#/apidoc?q=…`，可分享可后退），索引与卡片渲染同一份过滤结果，命中分组自动展开——否则会出现"匹配 8 / 51 却一张卡片都没有" |

**工作区与路由**。一个知识库的四个视图不再是四级下钻，而是同一位置的三个标签（`文档 / 分块 / 设置`），标签条由壳层渲染、带库名，文档详情也在它下面。路由是**模式表 + 命名参数**（`lib/router.svelte.ts` 的 `PATTERNS`），视图只接 `{kbId}` / `{docId}` 具名 props，不再用 `parts[1]`、`parts[3]` 这种位置索引——加一级路径过去要同时改路由和一堆视图，漏一处就拿到 `undefined`。

旧地址不会 404：`#/kb/{id}` 与 `#/kb/{id}/standalone` 在 `syncRoute()` 里被 `location.replace` 改写成新的完整路径（用 replace 是为了不留"点进去又立刻被改写"的历史记录）。改写放在 `syncRoute` 而不是只在启动时做，否则书签、手敲、外部贴入的链接会让地址栏停在旧形态，用户复制到的 URL 和当前界面对不上。

**布局：顶栏 → dock（常驻上下文栏）+ 内容列**。外壳是一层 CSS Grid：`grid-template-columns: var(--dock-w) var(--dock-handle-w) minmax(0,1fr)`，三个子项**显式占列**（靠自动放置的话，把手一旦 `display:none` 就会脱离网格流，把内容列往前挪进那条 0px 宽的列里，界面塌成一条竖排文字的窄带）。

dock 是 **section-aware** 的：顶栏那 5 项全局导航已移进 dock 顶部的 section 切换器（`lib/nav.ts` 的 section 表是导航的单一事实来源——原先 nav 数组、`activeNav` 折叠三元、路由 name switch 三处各写一遍，必然漂移），于是每个 section 都有自己的第二栏：知识库给库→文档两层树、任务给运行中队列、设置给四个子页（与 `Settings.svelte` 的 tabs 一一对应，由 `test_dock_settings_entries_match_the_page_tabs` 钉住）、API 给端点分组目录。

> **历史**：侧栏曾被整体移除，因为第一代实现折叠后把切换入口一起隐藏，而折叠态又写进 `localStorage` 跨刷新保留——用户重开浏览器仍是折叠的且无处可点，成了无法自救的死锁。这次把它做回来，前提是那两条教训被写成了守卫（`test_dock_collapse_is_self_recoverable`、`test_dock_persists_only_width`），见 §11.2 的折叠契约。

### 11.2 交互约定

- **dock 的折叠契约**（四条，前两条由静态守卫锁住）：① 折叠开关属于顶栏、画在 `<Dock>` **之前**，所以它不可能被自己折叠的那个容器藏起来；② **折叠态不落盘**，只有宽度落盘（`raggi.dock.width`），刷新一律回到展开，窄屏的默认折叠是按视口现算的、不是记忆；③ 宽度必须 clamp 在 `240–min(320px, 38vw)`，防止拖出视口后找不回；④ 顶栏常驻「恢复默认布局」入口，折叠开关带 `aria-expanded`/`aria-controls`。窄屏（<900px）dock 改为浮层 + scrim + `Esc` 收起。
- **断点只有一处**：`lib/dock.svelte.ts` 的 `NARROW_QUERY`；`styles/shell.css` 的 `@media (max-width: 900px)` 必须与之一致（CSS 读不到 JS 常量，靠注释钉住）。
- **导航项必须是 `<a href>`**，不用 `<button onclick="location.hash=…">`——否则中键新开、右键复制链接、状态栏预览全丢。`onclick` 只用于收起窄屏浮层，不拦截默认跳转。
- **加载状态是三态，不是一个布尔**：列表视图统一用 `lib/loadstate.svelte.ts` 的 `loader()`——`phase: initial|ready|failed` 决定要不要骨架屏，`refreshing` 只表示"数据在更新"，`error` 在已有数据时降级成一条横幅而不抹掉整屏。规则是**已有数据就不进 initial**，所以轮询/手动刷新/改过滤条件都自然走静默路径，不需要调用方各自记得传"静默"标志。回归防护：旧写法把"没有数据"和"数据在更新"合成一个 `loading`，于是文档列表的 2.5 秒轮询每次都重进加载态，把过滤工具栏连同输入框卸载重建——**用户正在敲的过滤词丢焦点**。分页判断同理必须基于后端 `total`（`Pager.more` = `offset < total`），且不要用 `limit += N` 增长：服务端把 limit 夹在 500，第三次点击会被静默压回。**`Pager` 的 `offset`/`total` 必须是 `$state`**：它们是普通字段时，读 `pager.total` 不建立任何依赖，标签只在"恰好有别的重渲染"时才更新——实测筛到 3 块却显示"匹配 5 块"，而且这种 bug 会偶然正确，根因留着不动。「再加载」块只有一份（`ui/ListMore.svelte`）：四份复制曾漂成三种口径（`pager.loaded` vs `rows.length`）、只有一份带 `aria-busy`；同时 `load(append)` 必须真的用 offset 追加——`DocView` 的按钮当时调的是无参 `load()`，写死 `offset=0` 替换整页，超过 200 块的文档后面的块永远到不了。
- **过滤要分清"没取到"和"没有匹配"**：`GET /documents` 与 `GET /chunks` 都支持 `q`，且都在**分页前**过滤、`total` 是过滤后的数，所以标题/内容过滤一律走服务端 + debounce（前端不再对已取回的数组筛第二遍——两处各筛一次迟早给出两个答案）。曾经 `/chunks` 没有 `q`，只能在已加载的窗口里筛，文案就必须写"在已加载的 N 块中匹配 M 块"；补齐参数后这句已改为真实命中数。**如果哪天某个列表接口不支持服务端过滤，界面就不许摆一个看起来像全库结果的 `N / M`。**
- **effect 里别顺手读状态**：切库的 `$effect` 若读了 `filter`（哪怕只是为了同步一个变量），用户每敲一个字都会让它重跑——重置视图、清空数据、并在 cleanup 里把轮询定时器清掉。这类"读依赖"要显式包进 `untrack()`。
- **反过来，effect 的依赖必须在同步阶段读出来**：`$effect(() => { const t = setTimeout(() => { const v = foo.trim(); … }, 300) })` 里 `foo` 是在回调中读的，effect 建立时没建立依赖 → **它永不重跑**，debounce 同步看起来像"功能没做"（实测：地址栏一直停在 `#/apidoc`）。正确写法是把 `const v = foo.trim()` 提到 `setTimeout` 之前。这一类错误 `svelte-check` 完全无声，只能靠形态守卫（`test_apidoc_filter_is_in_url_and_index_shares_its_source` 钉住两者先后顺序）。
- **跨路径生效的行为要放在所有路径的收口点上**：hash 变化的唯一收口是 `syncRoute()`，而 `go()` 只覆盖编程式跳转。dock 的「检索」是 `<a href="#/search">`，它不经过 `go()`——"恢复上次检索"写在 `go()` 里时对真实点击一直是死的。守卫也不许只钉"某字符串出现过"：`lastSearch()`、`location.replace` 躺在死分支或注释里时守卫照样绿，要钉**可达的形态**，并对每条断言做变异验证。
- **`0` 是合法值**：显示数字段不要写 `Number(x) || null`（空表的 `0 B` 会变成 `—`），要显式 `Number.isFinite`。缺字段和值为 0 是两件不同的事。
- **对话框正文是纯文本**：`ConfirmDialog` 用 `{cur.body}` 渲染（必须如此——正文里是表名等运行时值，能塞 Markdown 就能塞注入面）。所以 `body` 里不要写 `**粗体**`，星号会原样显示，而最该看清的那句反而成了噪声（`test_dialog_text_is_plain_text`）。
- **知识库的"自定义 vs 默认"按后端口径判断**：两级模型下每个库都持有具体数值（`storage/repos/kbs.py:90-97`），`plan.own.custom` 恒为真、没有信息量；`plan_custom` 的定义是"与系统默认是否不同，容差 0.05"（`api/kbs.py:18-31`），前端必须复用这个判据。文案也不许写"继承系统默认"——勾选保存只是把**当前**默认抄进本库，日后改系统默认不会传到这里；写"继承"就是承诺一个后端做不到的联动。文档级（`doc_plan`）的 `own.custom` 仍然表示"有没有覆盖"，可以直接用。
- **就地更新只能补自己那半张表**：保存成功后既不整页 `load()`（会抹掉另一面板未提交的编辑，阶段 6 的原始缺陷），也不在前端重算后端派生值（`effective` 还带着 `overlap_chars`/`source`，重算等于把切分口径实现第二遍，实测会让"当前生效"停在旧值）。正确做法是**只重取本面板那个接口**，其余状态原样保留。
- **浏览器副作用只有一份实现，收在 `lib/actions.ts`**（阶段 7）。视图/组件里禁止 `getElementById`、`querySelector`、`scrollIntoView`、`classList.add/remove`、`createRange/getSelection`、自己写 `.scrollTop =`；由 `test_dom_side_effects_live_in_one_place` 拦。这不是洁癖，是两组实证：焦点陷阱曾有两份——`ConfirmDialog` 把 Escape/Tab 挂在**面板元素**上，而"焦点一直在面板里"这个前提在对话框里根本不成立（提交中按钮被禁用或被移除，焦点就落回 body），于是**那个框既关不掉也走不出去**；两份的可聚焦清单还漂了（一份排除 disabled 输入，一份不排除，而 focus 一个 disabled 元素是**静默失败**）。`revealHit` 也曾在 `Search` 与 `DocView` 里逐字重复。留在视图里的命令式调用只应是"这份元素独有、且无法声明化"的那些（主题写 `<html>`、下载造 `<a>`、剪贴板选区、`visibilityState` 避让）。
- **滚动的前置条件是目标已有布局盒**。折叠分组用 `hidden` 实现时，卡片元素是**一直挂载**的，"解除 hidden"和"改 key"在同一次 flush 里，`scrollIntoView` 落在还没有布局盒的元素上——表现为"点了索引只闪一下，页面不动"（实测踩过）。修法是调用方 `await tick()` 等渲染完成再改 key，**不是**在 action 里 `requestAnimationFrame`：隐藏文档不跑动画帧，那会让滚动在后台标签页里静默丢掉。`reveal` 另外在 `prefers-reduced-motion` 下把 smooth 退回即时。
- **状态是值唯一的来源，别让 DOM 当存储**。`<select id="engine">` 曾没有绑定，提交时靠 `document.getElementById(...)?.value ?? "auto"` 读——"元素没找到"和"值就是 auto"是同一种表现，用户选的引擎被静默换掉。同理，检索结果的方向键不能用 `parentElement.children[i ± 1]` 找下一个（模板多包一层就跳错行且不报错），要按 `chunk_id` 认元素。
- **贴底/跟随这类"看着有实现"的效果，要检查它读的依赖**。`$effect(() => { if (running && el) el.scrollTop = el.scrollHeight })` 读了 `running` 和元素，**没读正文**，于是只在流式开始那一刻滚了一次空内容。共享 action 的参数里带上"变化的值"（`{ follow, text }`），才算得出"新字到了"。

- **破坏性动作的入口必须唯一**：删除知识库以前有 3 个入口（卡片脚、文档列表页头、设置页危险区）且走 2 套确认路径，用户学到的是"哪儿都能删，但弹窗长得不一样"。现在只剩设置页一处。同理，"回到上一级"由工作区标签条统一承担，子页不再各自摆 backlink（两套控件指向同一处，迟早漂移）。
- **URL 只描述"已生效"的检索参数**：控件改动先进暂存区（`draft`），点「应用并重查」才经 `searchparams.ts` 的 `searchUrl()` 写进 URL 并触发检索，同时给「放弃改动」出口。回归防护：旧实现每个控件 `onchange` 直接改写 URL，而触发检索的 effect 只依赖 `q`——改 top_k / 模式 / 阈值 / 过滤 全都不会重跑，屏幕上"参数面板是新值、结果集是旧值"且毫无提示。这不是慢，是**在说谎**。同理，结果条上的"范围/模式"必须读已生效值，不能读暂存区。
- **问答流式的 delta 要按帧落地**：`answer += text` 每 token 触发一次模板里的 `splitCitations(answer)` 整串重解析，是 O(n²)；攒到一帧再刷新即可保持逐字观感。停止/结束/出错时必须立即 flush，否则已生成的文字会被半截缓冲吃掉。
- **`wait=false` 提交的任务必须有订阅者**：重新解析 / 重新切分 / 入库都是异步的，接口回来时流水线才刚启动。提交方拿到 `job_id` 后要用 `waitForJob(jobId, onTick, signal)` 盯到终态再刷新，并在视图销毁时 `abort()`——否则页面永远停在"仍在处理"，用户只能离开再回来，而循环还会在后台继续打接口到超时（约 10 分钟）。由 `tests/test_frontend_wiring.py::test_async_ops_have_a_waiter` 锁住。**提示文案不许承诺界面做不到的事**：这里曾经写的是"处理完成后状态自动更新"，而当时并没有任何自动更新。
- **双栏对照由 `ui/SplitView.svelte` 唯一实现**：它的 `.pane-body` 自己不滚（只做 flex 列），滚动权交给槽位里的内容——两层滚动容器会让内层拿不到稳定的 scrollTop，虚拟化窗口也就量不准。高度由壳层的 `.main.is-full` 沿 flex 链交下来，不用 `calc(100vh - … - 210px)` 猜页头高度；窄屏在 app.css 的 1100px 块里整体退回普通文档流。
- **对话组件**：确认用 `confirmDialog()`（量化影响 + 破坏性操作要求输入名称），表单用 `formDialog()`（提交在对话内执行，失败就地显示且不关闭，避免丢掉刚敲的内容）；**禁止原生 `confirm()` / `alert()`**（由 `tests/test_frontend_wiring.py` 静态拦截）。
- **错误态必须可重试**：错误态统一带「重试」按钮。后端重启期间打开列表页，只给一句话的话用户只能猜是不是要刷新浏览器。
- **删除/变更要有量化影响**："将删除 342 个分块" 而不是 "确定删除？"。
- **上传失败要保留**：批量上传只移除**成功**的条目，失败项留在队列供重试——否则失败文件被静默清出，用户只能凭记忆重新选。同时后台仍在处理的任务不报成失败。
- **长列表窗口化**：分块表按视口只渲染可见行（`lib/virtual.ts`，定高 34px + 过扫描 8 行），节点数与数据量脱钩；行高固定是前提——变高行需要逐行测量，在长列表上得不偿失。注意窗口化依赖"面板自身可滚"：窄屏（<1100px）双栏退回普通文档流时面板不再受限（`clientHeight == scrollHeight`），`windowOf` 会算出"全部可见"而渲染所有行——这是回退行为而不是 bug，但拿它做性能断言时要先确认 measured 高度。
- **动效尊重系统设置**：`lib/motion.ts` 在 `prefers-reduced-motion: reduce` 下把时长归零（而不是变快）。

### 11.3 构建期注意

- `npm run build` 会先清空 `web/dist/` 再产出（Vite `emptyOutDir`）。产物是哈希文件名，构建后刷新页面即可拿到新版本；**index.html 本身可能被浏览器缓存**，验证新产物时用带查询串的地址强制刷新。
- `svelte-check` 依赖 `svelte.config.js`；缺失时会回退去读 `vite.config.ts`，而旧版 `@sveltejs/load-config` 认不出 vite-plugin-svelte 5 的插件名，表现为每个 `.svelte` 文件都报 "No Svelte configuration found in vite config"。
- Python 模块**不会热重载**：后端改动必须重启服务，否则会看到"改了没生效"的幻象（曾因此误判为 `undefined` / `NaN`）。

---

## 12. 性能设计

| 项 | 措施 |
|---|---|
| 检索 | hybrid 单次调用（Rust 原生）；`prefilter` 下推 + 标量索引；只 `select` 需要的列。`nprobes/refine_factor` 旋钮的**敏感性与 top-k 重合度已实测**（审计 §4.3）：`refine_factor=1` 省 60% 时间但只剩 27.5% 的 top-10 与基准相同 ⇒ **默认值不动**；`nprobes` 在 IvfHnswFlat 上既不花钱也不改结果（4→80 重合恒 1.0），它不是延迟旋钮 |
| 写入 | 批量 `pyarrow.Table` + `merge_insert` 幂等；embedding 按 `embed.batch` 合批 + 分片并发 + **进程级**闸门（`embed.concurrency`，`models/http.ScaledSemaphore`，按 permits 缓存的单例）；解析在入库队列 worker 线程内联。<br>> 这道闸曾每次调用新建一把，等于每文档一张独立门票、乘积没被封顶（审计 §5.7a，2026-10-06 修复）。<br>> 它**只覆盖入库路径**，`/api/models/test` 的探针直连 provider；且仍是每进程一把，多进程总并发 = 进程数 × 配置值 |
| 元数据 | OLTP（jobs/apikeys/kbs/documents）走 SQLite(WAL) 的复合索引；分块与向量留 LanceDB。点查实测 5.68ms → 0.008ms |
| 版本治理 | 全部表的 `optimize(cleanup_older_than=…)` 由后台维护按阈值触发（改前只有手工 `rag reindex` 且只作用 chunks，于是版本无界增长） |
| 观测端点 | `/health`、`/stats` 结果缓存 3s + `X-Snapshot-Age-Ms` 头；任何成功的写作废快照（否则用户改完看到的还是旧账） |
| 队列 | 有界（32），满则 503 —— 用背压代替无界堆积 |
| 索引 | 按规模选 `HNSW`/`IVF_HNSW_SQ`/`IVF_PQ`；后台阈值触发 `optimize()` 与版本回收；FTS 重建受「积压阈值 + 最小间隔」双重节流（改前是「n<50000 每次入库全量重建」） |
| 存储 | 列存；版本快照按需保留（默认 10） |
| 内存 | 向量 `float32`；不缓存全文，按 `chunk_id` 按需读取 |

**预算**（10 万 chunk / 1024 维）：hybrid 检索 P95 < 150ms；入库吞吐瓶颈在 embedding 服务与 Docling 解析。

### 12.1 单机并发实测

基准测试结论（8 核）：**吞吐 9.5 req/s**，并行效率约 1.8× 后趋于平台期；LanceDB FTS 单通道可扩展 28×。

> **一个被修正两次的错误结论**：最初把"并发退化"归因于 GIL，随后归因于 LanceDB 的全局事件循环，**两者都是错的**。真实原因是**基准工具本身**——多个线程共享同一个 `httpx.Client`，而它不是线程安全的。改用每线程独立 client 后，吞吐从 5.4 提升到 9.5 req/s。
> 结论：单机确实能并行，瓶颈是硬件与 IO，不是语言。「没有队列机制」这一条则是当时唯一正确的判断。

### 12.2 2026-10 复测（4 核 LXC，2000 文档 / 20000 分块，假 embedding）

改造前后对照，同一台机器、同一份数据集，工具在 `tools/bench/`：

| 指标 | 改造前 | 改造后 |
|---|---|---|
| `/api/documents?limit=50` 吞吐 @c=32 | 84 rps | **456 rps** |
| `/api/health` 吞吐 @c=32 | 28.7 rps | **546 rps** |
| `/api/stats` p50 @c=32 | 5053ms | **29.8ms** |
| 入库吞吐 @c=32 | 2.81 docs/s（**越并发越慢**） | 60.5 docs/s（单调上升） |
| 同进程多线程 / 多进程的存储读并行度 | 1.28× / 2.68× | 走多进程 |
| 48 次入库在 `jobs` 表产生的版本 | 224 | **0** |

两条被**实测否定**的猜测，记在这里免得再投一次：
① 默认线程池 8→128 对入库坍塌毫无影响（不是线程饥饿）；
② Pydantic 响应模型不是瓶颈（含 `extra="allow"` 时与裸返回同阶）。

另外修正一句诊断报告里我自己写错的话：「原生 scanner 比查询构建器快 3×」
是拿 6 列比 10 列得出的；同列集重测两者常常打平（7.74ms vs 7.61ms）。
读路径真正的收益来自**排序分页下推**（8.63 → 2.50ms，且成本从 O(表行数)
变成 O(页大小)）与**换元数据引擎**（同类列表查询在 SQLite 上 0.100ms）。

**仍未测**：真实模型服务下的端到端并发。本轮全程用假 embedding（不花额度），
所以 `/api/search` 的 57.5ms 是**不含模型网络往返**的下界。

---

## 13. 目录结构

```
Raggi/
├─ rag/
│  ├─ core/                    # 基础设施
│  │  ├─ config.py             # pydantic-settings：RAG_* 环境变量 + config.toml
│  │  ├─ errors.py             # 领域异常（EmbedUnavailable / DimensionMismatch / Invalid / NotFound / QueueFull）
│  │  └─ signing.py            # HMAC 签名 URL
│  ├─ storage/                 # ★ 唯一可接触数据库连接与建表的层
│  │  ├─ backend.py            # local | s3（RustFS / MinIO / AWS）
│  │  ├─ tables.py             # connect / 建表 / 索引 / 写入锁
│  │  ├─ schema.py             # Document / Chunk / Job / KnowledgeBase / ApiKey
│  │  ├─ sql.py                # SQL 转义与构造
│  │  ├─ plan.py               # 两级分块方案 + 幂等材料化迁移
│  │  ├─ health.py             # 对账、孤儿检测、fts_stale、reindex
│  │  └─ repos/                # documents · chunks · kbs · keys
│  ├─ retrieval/
│  │  ├─ search.py             # hybrid + rerank + 过滤 + 分数归一 + 分页
│  │  ├─ answer.py             # 生成式问答（带引用）
│  │  └─ highlight.py          # 按 char 偏移切片高亮
│  ├─ ingest/
│  │  ├─ queue.py              # 有界线程池 + 任务行 + 背压
│  │  ├─ pipeline.py           # load→split→embed→write→index
│  │  └─ splitter.py           # splitters + 偏移计算
│  ├─ models/
│  │  ├─ registry.py           # 构建/缓存/热切换
│  │  ├─ embeddings.py         # ollama | openai(兼容) | huggingface | custom
│  │  ├─ chat.py               # ChatOllama | ChatOpenAI(base_url) | custom
│  │  ├─ rerank.py             # HttpReranker（前缀探测 + 双重响应体兼容 + 优雅降级）
│  │  └─ testkit.py            # 连通性/性能测试
│  ├─ parsing/
│  │  ├─ router.py             # 引擎路由 + 回退链
│  │  ├─ loaders.py            # Docling / PyMuPDF4LLM / Unstructured / native（含 SSRF 防护）
│  │  ├─ normalize.py          # → ParsedDoc（page/heading/table/偏移）
│  │  └─ segment.py            # jieba 分词（可选）
│  ├─ api/                     # 路由层
│  │  ├─ auth.py               # API 密钥中间件 + 作用域
│  │  ├─ _common.py            # raise_operation_error —— 异常→状态码唯一映射
│  │  ├─ search.py  documents.py  chunks.py  kbs.py  jobs.py
│  │  ├─ models.py  apikeys.py    plan.py    answer.py  system.py
│  │  ├─ schemas.py            # 请求模型
│  │  ├─ responses.py          # 响应模型 + 共享错误契约
│  │  └─ __init__.py           # Ctx / create_app（安全方案声明 / tag 元数据 / 领域异常兜底）
│  ├─ chunk_edit.py            # 编辑与重向量化（§8）
│  ├─ api.py                   # 兼容入口
│  └─ server.py                # uvicorn 入口 + StaticFiles（含 uvloop 自动探测）
├─ web/
│  ├─ src/                      # 前端源码（唯一需编辑处）
│  │  ├─ main.ts  App.svelte
│  │  ├─ views/                 # 路由级视图（KbList/KbDocs/KbChunks/DocView/KbSettings/Search/Settings/ApiDoc）
│  │  ├─ ui/                    # 组件（对话/表单/上传面板/状态块/图标/密钥面板）
│  │  ├─ lib/                   # api · router · crumbs · stores · types · virtual · motion
│  │  │                         #   chunkops · docops · kbops · jobs · searchparams · source
│  │  ├─ data/apidoc.ts         # API 文档数据
│  │  └─ styles/app.css
│  ├─ dist/                     # Vite 产物（gitignore；StaticFiles 托管）
│  └─ index.html
├─ data/{lancedb/, files/, config.toml}   # config.toml 权限应为 0600
├─ tests/                      # conftest.py 隔离 RAG_DATA_DIR，测试不读本机配置
├─ package.json  tsconfig.json  vite.config.ts  svelte.config.js
├─ pyproject.toml              # extras: docling / unstructured / local-models / jieba
└─ docs/{DESIGN.md, AUDIT-*.md, DESIGN-AUDIT-*.md}
```

> **`rag/api/routes/` 的拆分被主动放弃**：纯粹的文件搬运却要改动全部路由注册，风险大于收益。

---

## 14. 部署

```bash
npm install --include=dev && npm run build    # 前端：Vite 构建 web/src → web/dist
pip install -e ".[docling,unstructured]"      # 或最小集：pip install -e .
rag serve --host 0.0.0.0 --port 8000 --data ./data
```

- **前端构建只需一次**；若目标机不装 node，直接拷贝已构建的 `web/dist/` 即可——构建与部署解耦（产物是纯静态文件，运行时零依赖）。
- **数据目录**：`data/lancedb/`（含版本）+ `data/files/`（原文留档）+ `data/config.toml`（含密钥，`chmod 600`）。备份 = 目录拷贝（离线）或先 `optimize()` 后拷（在线）。
- **对象存储**：设 `RAG_STORAGE__BACKEND=s3` 及配套变量即可切到 RustFS / MinIO / S3，代码不变。
- **systemd**：`Restart=always`；**单 worker**（见 §16）。
- **幂等**：`POST /documents`、`/documents/text`、`/documents/url` 支持 `Idempotency-Key` 头（TTL 24h，进程内存储）；同键重试回放首次结果并带 `idempotent_replay: true`，**不会产生第二份文档**。不给键时行为不变。
- **并发编辑**：`PATCH /chunks/{id}` 可带 `updated_at`（客户端读到的那一版）；期间被别人改过则返回 **409** 而非静默覆盖。不带该字段沿用「最后写入者胜」。
- **可运维性**：每个响应带 `X-Request-ID`（上游传入则沿用，未捕获异常会把它写进日志并回传引用）；`rate_limit_writes_per_min` 限**写**操作（默认 0=关闭，检索与幂等入库豁免），按调用方分桶，超限返回 429 + `Retry-After`。
- **本地模型侧车**：Ollama（`ollama serve`）或 vLLM——不进入本进程，避免 torch 依赖污染应用环境；均由 `/api/models/test` 验证。
- **Python 基线 3.11**（`unstructured` 约束）。

---

## 15. 测试与验收

当前 **561 个测试通过**（默认套件；另有 1 条延迟门槛带 `perf` 标记，由 `addopts = ["-m", "not perf"]` 排除在默认套件外，手动 `pytest -m perf` 跑，共 562 条）。计数链与每轮加减什么在 [ARCH-AUDIT §0](./ARCH-AUDIT-2026-10-06.md)。
> 最近一次全量架构审计：[ARCH-AUDIT-2026-10-06.md](./ARCH-AUDIT-2026-10-06.md)
> ——四个严重缺陷（都属「默认路径上静默出错」这一类）在其中列了现象/证据/影响/建议，
> 并已修；尚未处理的结构性债按 A/M/C/P 编号排了优先级。

- **默认套件的边界**：`addopts = ["-m", "not perf"]` 把绝对延迟门槛关在默认套件外。此前只声明了 `markers = [...]`——那只是允许这个标记名，**不会**排除它，于是 `test_latency_budgets`（断言 `p50 < 150ms`）一直每天在默认套件里跑，而它自己的 docstring 第一行写着「只在 `pytest -m perf` 下跑」。注释与行为现在由 addopts 对齐；两条命令都验过（默认 547 passed / 1 deselected；`-m perf` 单跑通过）。
- **隔离**：`tests/conftest.py` 把 `RAG_DATA_DIR` 指向临时目录。必须如此——`Settings()` 会读 `{data_dir}/config.toml`，测试若不隔离就会读到开发机上的生产配置（实测：本地开启 `features.answer` 后，断言"默认关闭"的测试立刻失败）。
- **静态守卫**（`test_frontend_wiring.py`，33 条）：产物齐全且不旧于源码、无原生 `confirm()/alert()`、键盘与 `tabindex` 合规、控件有可访问名、无手动 DOM 查询/`innerHTML` 替换、卡片操作按钮不被链接覆盖层吞掉、浏览器副作用只允许有 `lib/actions.ts` 一份实现。每条文本型守卫都先剥注释，且都做过变异验证（把要防的问题改回去确认它会红）——已经有两条守卫因为只钉"字符串出现过"而被证明是假绿。
  > 曾针对「手写 HTML + tsc 直出」校验 import 可解析与 DOM id 存在；改用 Svelte 后这两类由编译器与组件结构接管，保留只会变成噪声，已移除。
- **交互回归**（`test_architecture.py` / `test_doc_viewer.py`）：分块表窗口化（`windowOf` + 占位行 + 固定行高）、分块编辑/删除/批量启停、独立分块页（`only_standalone=true`）、知识库/文档两级方案编辑、文档重命名、表单对话挂载、异步重解析/重切分入口、流式问答前端（`lib/sse.ts` 四事件齐全 + `AnswerPanel` 真中断）、取消入口（`lib/jobcancel.ts` + 文档列表/入库面板两个入口）、批量操作确实走批量端点。
  > 文档级动作（重命名/方案/重解析/重切分/删除）集中在 `lib/docops.ts`，知识库级动作在 `lib/kbops.ts`——列表页与详情页共用同一套文案与队列语义，测试断言指向模块而不是某个视图。
- **契约守卫**（`test_openapi_contract.py`，14 项）：每个 /api 操作的 200 响应都有 schema、非 JSON 端点（字节流 / Markdown 报告 / SSE 事件流）声明媒体类型、安全方案存在且每个操作都声明 security、tag 分组齐全且只用声明过的 tag、错误契约（401/403/404/503/422）出现在对应操作上、`global` 字段别名不漂移、契约里只有 `/api/v1` 而旧前缀行为一致且同样受鉴权；另有两项用真实请求确认错误体就是 `{"detail": str}`。
- **批量与流式**（`test_batch_ops.py` 11 项 / `test_answer_stream.py` 7 项）：批量启停的原子性与去重（不做部分成功）、批量删除级联分块且不留孤儿、`skipped` 如实回报；SSE 事件顺序 **sources → delta\* → done**（引用必须先于正文）、与非流式共享同一套引用、列表形态 content 的归一化、空结果/空输出/error 三种边界都给出明确事件而非断连。
- **任务取消与归属**（`test_job_cancel.py` 11 项）：排队中是真取消（撤销后不产出文档）、运行中是协作式取消（阶段边界退出、旧分块完好、文档不被误标 failed）、已终态幂等、取消后不再出现在活动任务列表、任务行记录 kb_id 且列表/单条都带 terminal、jobs 表补列幂等。
- **写入安全**（`test_concurrency.py` 8 项 / `test_idempotency.py` 13 项）：基于旧版本的分块编辑返回 409 且**不覆盖**已提交的内容，重读新版本后可正常提交，不带版本时沿用「最后写入者胜」以兼容旧客户端；仓储层返回是否真的更新了行。幂等键同键重试回放首次结果（带 `idempotent_replay` 标记）、不同键互不干扰、键按端点作用域、失败不缓存、条目按 TTL 与上限回收；上传端点的幂等检查发生在**读文件之前**，重试不会把大文件再读一遍。
- **可运维性**（`test_rate_limit.py` 21 项）：请求 ID 贯穿每个响应（含错误路径与鉴权失败），上游传入的 ID 会被沿用、不可信值被替换；限流默认关闭，写操作限流而检索与幂等入库豁免，429 带 Retry-After，按调用方分桶、匀速恢复、长期不用的桶会被回收。
- **文档漂移**（`test_apidoc.py`）：API 文档与 `openapi.json` 双向比对。
- **行为断言**（`test_audit_fixes.py`，45 项）：错误响应不含 `Traceback`、不含内部路径、不含误导向的"embedding 服务不可用"。
- **架构边界**：只有 `storage/` 可接触数据库连接与建表。
  > 该规则曾写得过宽而误报 5 处——**导入 LanceDB 的算法原语（`RRFReranker`、`ColumnOrdering`）是允许的**，被禁止的是连接与建表。规则已相应收窄。
  > 2026-10-06 审计轮把同一条纪律补到了第二个引擎上：`sqlite3.connect` 也只许出现在 `storage/`。
  > 当时它还是一条**棘轮**（允许 3 个模块例外，只禁止扩大）；B 档补完
  > `repos/chunks.py` 的 `chunk_filters` / `chunks_page` / `chunk_prefilter` / `search_chunks`
  > 之后已收紧为**零例外的不变量**（`test_no_sql_text_outside_storage_layer`）：
  > storage 之外任何文件导入 `escape_sql`/`scalar_rows`/`fetch_rows`/`count_rows`/`only_cols` 都会红。
- **SQL 文本层**（`test_sql_safety.py`，18 项）：含 `'` 的**合法**值必须照样查得回来（转义做过头会让
  用户看到「文档存在但搜不到」且全程无报错）；`' OR 1=1--` 匹配不到任何行；LIKE/ILIKE 里的
  `%` `_` 必须保持字面量；**投影过白名单**（列名无法做成占位符，`SELECT {', '.join(cols)}` 是唯一
  能改写查询本体的位置，且要在引擎分派**之前**收窄——只收一条路径等于没收）；
  DDL 里不许存在零调用点的表（投机 schema 看起来像契约，会诱导后来人在上面写代码）；
  表形状在三处声明（LanceModel / SQLite DDL / 迁移用的列清单），**两条边逐条比对**——
  丢 `text` 那次迁移事故就是「列清单落后于真实形状」，而迁移恰恰按列清单取投影；
  以及 `Settings` 的每个顶层字段都要在业务代码里有读取点（写进 config.toml 却没人读的配置是在骗用户）。
- **引擎分派的单一实现点**（`test_sql_safety.py::test_engine_dispatch_has_exactly_one_implementation`）：
  「这次读 SQLite 还是 LanceDB」只允许有 `repos/_engine.py:meta_of()` 一处实现，四个仓储模块一律
  `from ._engine import meta_of as _meta`。这条守的是**复制**而不是正确性：四个模块曾各写一份
  `getattr(store, "meta", None)`，形状一致但没有强制，任何一处漏了分派就静默走回退路径——
  无鉴权事故与「列表 8 篇 / 健康说 7 篇」事故都是这个形状。
- **启动期的回退规则**（`test_meta_store.py`，3 项）：元数据引擎初始化失败时，
  **只有** SQLite 里一行数据都没有才允许退回 LanceDB；已有数据则抛 `Invalid` 拒绝启动。
  改前一律 `log.error + return None`，注释理由是「不带分裂状态启动」——但**退回旧路径本身就是
  分裂状态**（SQLite 里已有的数据不会同步回 Lance，此后读写全在旧引擎上）。
  三项分别覆盖：已有数据必须拒启 / 空库可以安全回退 / 探测必须真是只读
  （`sqlite_holds_data` 若忘写 `mode=ro`，会在这条错误路径上凭空建出一个空库，
  下次启动就被当成「已有数据」——**防分裂的守卫自己制造分裂**，这是新测试抓出来的第一个 bug）。
- **手写并发件**（`test_cache.py`，12 项）：`TTLCache` 站在鉴权与观测两条关键路径上，
  改造前直接测试为 0。逐条钉住它注释里声称的承诺：按项 TTL、并发写下上界不被突破、LRU 次序、
  `invalidate` 立即可见、键作用域（跨数据目录串键就是越权）、`None` 也是有效值、
  以及 **`loader` 在锁外执行**——这条是它存在的性能理由，用一个线程在 loader 里等事件、
  另一个线程必须仍能读写来验证。
  > 另 2 项测的是**站在它上面的** `Embedder.embed_one`：同文本第二次不许再打远端、
  > **换 embedding 模型必须重算**（缓存键里带模型名，否则拿到另一个模型空间的向量）；
  > 以及一条**故意断言现状**的测试——缓存不合并「正在飞」的同 key 请求（stampede），
  > 这是审计 §4.2 里被否证后**保留**的行为，将来实现 in-flight 合并时它会红，
  > 届时该连决定一起改而不是把断言改小。
- **入库并发闸与合批形状**（`test_queue.py`，4 项）：`_embed_gate` 必须跨调用复用同一实例、
  4 文档 × 4 分片的**峰值在途 ≤ permits**、`peak > 1`（否则「串行也算过」会让上界断言变成空话），
  以及一条**变异检查**：把闸门退回「每次新建」后峰值必须突破 permits——它证明上一条测的是闸门而不是线程数。
  另钉 `embed.batch` 的合批形状（200 chunks / batch=64 → `[64,64,64,8]`，15 chunks → 一次打完）：
  这个配置全仓只有一个读取点，改坏了不会有任何测试红。
  背景见审计 §5.7a：闸门原本每次调用新建一把，**「任务数 × 分片数」这个乘积从未被封顶**，
  而默认配置下 2×4 恰好等于名义上限 8，所以看不出来。
- **CI 门槛的本地副本**（`test_lint_gate.py`，2 项）：把 CI 那两条命令
  （`ruff check rag tools tests`、`python -m mypy`）原样在本地跑一遍。理由很硬：B5 提交
  带着两条 ruff 违规上了 main，CI 红而本地 551 条测试全绿——pytest 里没人跑 lint。
  mypy 那条**额外断言被检查的文件数 ≥ 15**：`[tool.mypy] files` 指错目录时
  mypy 会输出 `no issues found in 0 source files` 然后绿灯，那是最漂亮的一种假绿。
- **双向导入探针**（`test_import_order.py`，2 项）：按**文件系统**枚举模块
  （不是 `pkgutil.walk_packages` —— 它看不见 `rag/ingest`、`rag/models`、`rag/parsing`
  这三个没有 `__init__.py` 的命名空间包，合计 13 个模块），在子进程里正序与逆序各导一遍。
  必须用子进程：清 `sys.modules` 会让后续测试拿到第二份 `RaggiError` 类对象，
  `pytest.raises` 的身份判断随之失效。同一文件还检查
  **`[project.scripts]` 声明的入口能不能装出来** —— `rag-bench` 曾经不能。
- **鉴权配置的默认值方向**（`test_audit_regressions.py` 2 项）：`has_keys()` 读不出来时
  必须抛（→ 503），**不许回答「没有密钥」**——两处调用点都把 False 当放行条件，
  而结果会被缓存 10 秒，于是「一次瞬时读失败」= 「10 秒的无鉴权 API」。
  另一项断言异常不会被缓存成某个假设值。
- **健康检查不许把失败翻译成正常**（同上文件 2 项，含一条**反向对照**）：
  检查跑不成 ⇒ 进 `checks_failed`、计数用 -1 而不是 0、`status` 不许是 ok；
  反向对照断言「全部查成时 `checks_failed` 是空列表」，否则前一条靠永远填上就能过。
- **对账写对引擎**（`test_meta_store.py` 1 项）：`POST /api/reconcile` 必须改正
  **真源所在引擎**里的值，并且 `/api/health` 要跟着变好。
  只断言接口返回的 `fixed: 1` 是抓不到这个 bug 的 —— 它当时就在回报 1。
- **守卫自身的守卫**：`test_audit_regressions.py::test_auth_is_threaded_off_event_loop`
  原来是 `inspect.getsource(create_app)` 里找一行字串。把中间件体拆出去之后它变红，
  而**行为一点没变**——这恰好证明它是假绿的一种：字串还在就算过，行为怎么坏它都照过。
  已改成行为断言：利用「工作线程里 `asyncio.get_running_loop()` 必然抛 RuntimeError」
  判定 `authenticate` 到底跑在哪个线程上（确定性、无计时、与函数叫什么/放在哪里无关）。
  > 同一文件里还剩 5 处 `inspect.getsource` 型守卫（`:264/:397/:407/:426/:460`），
  > 本轮没有一并重写，已记进审计报告的 C 档第 11 项。前端那侧的同类教训见上一条静态守卫。
- **集成**：同一 PDF 用不同引擎入库均可检索；编辑 `manual` 分块后新词可检索且 `fts_stale=0`；删除文档无孤儿 chunk；`/api/models/test` 三类模型均返回 ok（断网时明确失败而非 500）。
- **前端控件一致性**：解析 HTML 得到真实控件类型，与 `PARAMS` 表交叉校验。
  > 曾发现 `group_by_doc` 声明为 `bool` 但实际是 `<select>`，导致该参数永远提交 `false`。
- **构建新鲜度**：`web/src/**` 不得新于 `web/dist/index.html` 产物（未构建时该断言 `pytest.skip`，"按需构建"不被测试倒逼）。

---

## 16. 风险与权衡

| 风险 | 影响 | 缓解 |
|---|---|---|
| FTS 索引在 `update` 后不自动刷新 | 编辑后的新词检索不到 | `fts_stale_count` 治理 + 小库同步重建 / 大库阈值重建，前端可见 |
| LanceDB 无跨表事务 | 写入中断可能不一致 | 两阶段写入 + `merge_insert` 幂等 + 对账 + 版本回滚 |
| 多进程写入（同机） | 数据损坏 | 已由 `flock` 跨进程写锁 + OLTP 迁 SQLite 解决：API 进程不再写 LanceDB 的元数据，chunks 写在锁内串行（§5、§12.2）。**多机**仍不支持 |
| 多机并发写 | 数据损坏 | 本地文件锁跨机不成立；`storage.backend=s3` 时自动退回进程内锁并告警。要多机必须引入共享锁/单一写入者，届时另立方案 |
| SQLite 与 LanceDB 两份真源 | 元数据与分块漂移 | 每张表只有一个真源（OLTP=SQLite，chunks=LanceDB）；首启一次性导入带**行数校验**，校验不过就拒绝启用；`meta_engine=lancedb` 是应急回退。双引擎结果由 `test_two_engines_agree_on_every_observed_result` 逐端点对齐 |
| 中文 FTS 分词效果 | 关键词通道召回差 | jieba `text_seg` 列；短查询由向量通道兜底 |
| Docling/Unstructured 依赖重、需下载模型 | 部署变慢 | 可选 extras + 回退链；默认 PDF 走 PyMuPDF4LLM |
| URL 抓取被用作 SSRF 跳板 | 探测内网 | 协议白名单 + 私网/环回地址拒绝 |
| 密钥以明文存于 `config.toml` | 泄露 | `chmod 600`；`data/` 必须 gitignore；`GET /api/models` 始终打码；界面上的密钥应定期轮换 |
| 签名 URL 泄露 | 原文被读取 | TTL 1 小时；路径穿越防护 + 常量时间比较 |
| 更换 embedding 模型 | 维度不匹配导致检索失效 | `embed_model` + 维度校验，明确报 `dimension_mismatch` 并强制 `reindex` |
| LangChain 版本演进快 | API 变动 | 锁定版本区间；第三方封装在 `rag/models`、`rag/parsing` 内部，外部只依赖协议 |

---

## 17. 里程碑

| 阶段 | 交付 | 验收 |
|---|---|---|
| M0 骨架 | `pyproject`（含 extras）+ LanceDB 建表 + `/api/health` | 启动即健康检查通过，各表创建成功 |
| M1 解析入库 | Loader 路由 + 三引擎 + 回退链 + 切分 + embed + 写入 + jobs | PDF/MD/DOCX 入库，进度可查，无孤儿 |
| M2 检索 | 向量索引 + FTS + hybrid + rerank + prefilter + 高亮 + 上下文 | 中文短查询有兜底；分数口径统一 |
| M3 模型层 | 三类模型配置 + `/api/models/test` + 可选 `/api/answer` | 三类一键测试通过，本地⇄API 切换零代码 |
| M4 编辑 | `PATCH /api/chunks` + 重向量化 + FTS 一致性治理 | 改后新词可检索，`fts_stale=0` |
| M5 前端与加固 | 视图 + 报告导出 + systemd + 备份脚本 | 单命令部署 |
| **M6 架构重写** | 模块按职责重组；入库队列；API 密钥；签名 URL；存储后端抽象；两级分块 | 302 测试通过；真实模型端到端可用 |
| **M7 真实模型接入** | 接入 SiliconFlow：`bge-m3`（1024 维）+ `bge-reranker-v2-m3` + `Qwen2.5-7B` | 语义检索 4/4 命中（关键词同为 0/4）；rerank 首次对接即吻合并正确排序 |

### 17.1 待办

- 分块「批量启停」目前是前端循环调用单条端点（20 条 = 20 个请求）；可加 `PATCH /api/chunks/enabled` 批量端点。
- `rag/api/routes/` 拆分（已评估放弃）。
- ~~多进程写入安全性与 `--workers > 1` 的验证~~（2026-10 已做：同机多进程 +
  跨进程写锁；剩「多机」不在范围内）
- ~~`outbox` 表已建但只有对账类操作用得到~~（2026-10-06 审计轮：这张表
  **零调用点**，连同 `idempotency` 表与 `jobs.worker` 列一起从 DDL 删除，
  并加了 `test_no_dead_tables_in_meta_ddl` 防止投机 schema 长回来。
  「删文档 / 移动知识库」的跨引擎级联仍是两步写 + `flock` + 启动期对账，
  要做成事务性重放会动到前端契约，仍未做）。
- 真实模型服务下的端到端并发**仍未测**（本轮全程用 `tools/bench` 的假 embedding，
  不花额度）。`/api/search` 那 57.5ms 是不含模型网络往返的下界。
