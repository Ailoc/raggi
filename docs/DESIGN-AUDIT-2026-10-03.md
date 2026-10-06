# Raggi 设计审计与重构报告

> 日期：2026-10-03 ｜ 范围：`rag/`（后端）+ `web/src/`（前端）全量
> 方法：静态审查 + 隔离环境实测 + 真实数据目录迁移验证
> 配套测试：238 项（新增 21 项见 `tests/test_doc_viewer.py`）

## 一、结论概览

| 级别 | 问题 | 状态 |
|---|---|---|
| **P0** | PDF 解析完全失效（所有 PDF 入库 500） | ✅ 已修 |
| **P0** | `documents.meta` JSON 承载 4 种语义，20 处防御式解析 | ✅ 已重构 |
| **P1** | 无原文预览能力（留档文件无法通过 HTTP 访问） | ✅ 已实现 |
| **P1** | 知识库过滤无法下推，四处全表扫描 | ✅ 已修 |
| **P2** | `documents` 表缺列默认值兜底，加列即打挂老调用点 | ✅ 已修 |
| **P2** | 解析器依赖已 sunset 的 langchain 包装类 | ✅ 已修 |

---

## 二、P0-1：PDF 解析完全失效

**现象**：任何 PDF 上传都返回 500，且错误信息是「所有解析引擎均失败: None」。

**根因**：`rag/parsing/loaders.py` 依赖
`langchain_community.document_loaders.PyMuPDF4LLMLoader`，而
langchain-community 0.4.2 **已移除该符号**（该包正在 sunset，官方建议迁移到独立集成包）。

影响链：`pymupdf4llm` 引擎导入失败 → 回退 `native` → `native` 对 PDF 也调同一个函数 → 同样失败 → 抛错。**PDF 是 RAG 最主要的文档类型，这条链路整个不可用。**

**修复**：直接调用 `pymupdf4llm.to_markdown(page_chunks=True)`（该库本就是核心依赖，不需要 langchain 包装）。附带收益：一次调用拿到逐页文本，由此建立**页码映射**，检索结果才能显示「第 N 页」并支持跳页。

**验证**：`test_pdf_ingest_works_without_langchain_loader`、`test_pdf_pages_mapped`。

---

## 三、P0-2：`documents.meta` 的设计异味

**问题**：一个 TEXT 列塞进四种语义——`kb_id`、`stored_file`、`chunking`、`snapshot`。

后果有三，且都是结构性的：

1. **20 处防御式解析**，写法一致但各自为政：
   ```python
   kb_id = (json.loads(meta) if isinstance(meta, str) else meta).get("kb_id") or ""
   ```
   任一调用点漏判类型就静默拿到空串。没有 schema、没有校验，任何形状都能写进去。
2. **无法下推到 SQL**。按知识库过滤只能取回全表、在 Python 里逐行 `json.loads` 再筛。
   `list_kbs` / `get_kb` / `delete_kb` / `doc_ids_in_kb` / `list_documents` 五处都是这么写的。
3. **改一个字段会波及其它字段**。例如重解析时要把 `stored_file` 保留下来，只能整体读写 JSON。

**重构**：把承载查询语义的两个字段提升为真实列。

```python
class Document(LanceModel):
    kb_id: str = ""                     # 原是 meta.kb_id
    stored_file: Optional[str] = None   # 原是 meta.stored_file
    meta: str = "{}"                    # 仅剩 chunking（嵌套、展示用）
```

配套幂等迁移 `_backfill_doc_columns_from_meta()`：加列后从历史 meta 搬值，
只处理「新列为空且 meta 有值」的行，**不清空 meta**（旧版本回滚仍可读）。

**在真实数据上验证过**：6 篇存量文档全部迁移成功，`chunking` 结构未破坏，知识库计数不变。

**结果**：20 处解析点 → **2 处**（迁移函数本身 + chunking 的集中 helper），
且有测试 `test_no_scattered_meta_parsing` 锁住这个上限。

---

## 四、P1：原文预览

**问题**：上传的原始文件被留档到 `data/files/{doc_id}{ext}`，但**没有任何 HTTP 端点能取到它**——前端无法展示原文。

**实现**：`GET /api/documents/{doc_id}/file`。

### 关键技术问题：iframe 无法携带 Authorization 头

浏览器内嵌预览必须用 `<iframe>` / `<img>` / `<embed>`，这些标签**不能自定义请求头**。一旦启用鉴权，直接把 `/api/documents/{id}/file` 放进 `src` 必然 401。

**方案是标准的签名 URL**（HMAC）：

- `?exp=<时间戳>&sig=<HMAC(secret, doc_id:exp)>`
- 默认 10 分钟有效，**与 doc_id 绑定**（拿 A 的签名读不到 B）
- 密钥落盘到 `data/.url_secret`（多 worker 需共享同一把）
- 未启用鉴权时退化为无签名直链，保持单机自用的零摩擦

**安全防护**：
- `safe_join()` 拒绝路径分隔符、点开头，并校验 resolve 后仍在 `files_dir` 内（挡符号链接跳出）
- 签名用 `hmac.compare_digest` 常数时间比较
- 加目录名 + 加列名双重 join 校验

**验证**：字节一致性、Content-Type、inline/attachment、篡改签名 403、过期签名 403、跨文档签名 403、路径穿越拦截。

---

## 五、P1：查询下推

`list_kbs` / `get_kb` / `delete_kb` / `doc_ids_in_kb` 原先都取回 `documents` 全表
再逐行解析 meta 过滤。`kb_id` 成为真实列后改为 SQL 过滤：

```python
store.documents.search().select(["doc_id"]).where(f"kb_id = '{escape_sql(kb_id)}'")
```

`list_documents` 的 `status` / `parser_engine` / `mime` / `kb_id` 也一并下推。
`ensure_scalar_indexes` 已为 `kb_id` 建 BTREE（chunks 表早就有，documents 新增）。

---

## 六、P2：其它修复

**`upsert_documents` 缺默认值兜底**：分块表早有 `_fill_defaults`，
文档表没有。结果 documents 一加列，所有只传部分列的老调用点全部
撞上 Arrow 的 partial-schema 报错。已抽出通用的 `_fill()` 供两张表共用——
**新增列不再会打挂老调用点**。

**解析失败信息不可读**：解析器返回空（典型是扫描件）时 `last_err` 为 `None`，
错误消息变成「所有解析引擎均失败: None」。现在区分「报错」与「抽不出文本」，
后者给出可行动提示（需要 OCR 解析器）。

---

## 七、前端原文对照查看器

### 技术选型（经过调研）

| 方案 | 依赖 | 分块→原文联动 | 结论 |
|---|---|---|---|
| **浏览器原生 PDF 查看器**（iframe + `#page=N`） | **零** | ✅ 跳页 | **采用** |
| PDF.js | ~1MB + worker | ✅ 精细（页内高亮） | 不采用 |
| Google Docs Viewer | 外部服务 | ❌ | 不采用 |

主流浏览器（Chrome / Firefox / Safari / Edge）都内置 PDF 查看器，
且支持 URL fragment `#page=N` 直接跳页——**分块到页码的联动用这一条就够**。
项目原本就是零运行时依赖（vanilla TS + tsc），引入 PDF.js 会打破这个前提，
而它带来的额外能力（页内文本层高亮）对「按块定位」这个目标并非必需。

### 布局

```
文档详情页
├ 页头：返回链接 / 标题 / 字数 · 分块数 · 方案 / 操作按钮
└ 双栏（各自独立滚动）
   ├ 左：原文        ← PDF(iframe) / 图片(img) / 文本(pre) / 下载卡片
   └ 右：分块列表    ← 点击任一块即在左侧定位
```

### 按类型分派

| 类型 | 渲染 | 定位方式 |
|---|---|---|
| PDF | `<iframe>` 内置查看器 | `#page=N` 跳页 |
| 图片 | `<img>` | 无法按位置定位（会说明） |
| 文本 / Markdown | `<pre>` | 字符偏移高亮 |
| docx / pptx / xlsx | 下载卡片 | 无法内联（会说明） |
| 无留档（粘贴文本入库） | 回退到解析全文 | 字符偏移高亮 |

### 两个实现细节

**文本高亮用 `Range` 而非 `innerHTML` 替换**。原文含任意 HTML 字符，
字符串替换需要重新转义整段文本，极易引入注入或显示错乱；
`Range.surroundContents` 由浏览器保证结构正确。

**偏移失效时不定位**。分块被编辑过（`offset_valid=false`）后字符偏移
不再对应原文，此时按偏移高亮会把用户引到无关段落——宁可不动，
并在提示行说明原因。

---

## 九、后续修复（第二轮）

首轮报告后继续处理了剩余问题，全部有回归测试。

### 9.1 入库队列（架构缺口）

**问题**：入库完全同步——HTTP 请求要一直等到「解析 → 切分 → 向量化 → 写库」
全部结束。上传 50MB PDF 会让连接挂住数分钟，客户端易超时；
且并发无上限，多个上传同时涌入会全部挤在 LanceDB 写锁与 embedding 服务上
互相争抢，整体吞吐反而更低。`jobs` 表按队列设计（stage/progress），
但**只写不读、没有任何消费者**。

**实现** `rag/queue.py`：

- 进程内有界线程池（`ingest_workers`，默认 2）——入库是重操作，
  并发过高只会互相争抢，2 个足以让解析与向量化的 IO 重叠；
- 队列长度上限（默认 32），满了返回 503，快速失败好过无限堆积；
- 任务行在**入队时**写入（stage=queued），前端拿到 job_id 即可查状态；
- `wait=true`（默认）阻塞等结果，行为与引入队列前一致；
- `wait=false` 立即返回 job_id，配 `GET /api/jobs/{job_id}` 轮询。

**阶段流转**：`queued → parse/fetch → load → split → embed → write → done/failed`。

**实测**：`wait=false` 提交耗时 **22ms**（原先要等完整条流水线）。

### 9.2 任务记录回写 doc_id

**问题**：异步调用方只拿得到 job_id，而任务行从不回写产出的 doc_id——
任务结束后无从知道这批内容对应哪篇文档，只能去列表里靠标题猜。

**修复**：`_set_job_doc()` 在 doc_id 确定时（含重复命中的分支）立即回写。

### 9.3 原文链接有效期过短

**问题**：签名 URL 的 TTL 是 600 秒。但它是嵌进 `<iframe>` 里**给人阅读**的——
读一份 PDF 动辄十几分钟，读到一半点右侧分块时 iframe 会带过期签名重新加载，
直接变成 403 空白页，用户完全无从理解。

**修复**：TTL 提到 1 小时（覆盖一次阅读会话），并加测试锁住下限。

### 9.4 列表静默截断

**问题**：文档列表硬截断 200 条、分块 500 条，而后端返回的 `total`
**被前端完全丢弃**——没有提示、没有分页，超出部分静默消失。

**修复**：显示「已显示 N / 共 M 篇」并提供「再加载」按钮。
分页判断基于后端 `total` 而非已取回条数（否则「刚好取满」时会漏判）。

### 9.5 `$("#x")?.` 的陷阱

**问题**：`$()` 是**严格查询——找不到元素直接抛错**，`?.` 根本没机会生效。
分页按钮是条件渲染的，写成 `$("#docsMoreBtn")?.` 后，
在「文档不足一页」这个**最常见**的场景必定抛错，把整个列表打成错误态。

**修复**：条件渲染的元素一律用 `find()`（缺失返回 null），
并加静态守卫禁止 `$("#x")?.` 这种自相矛盾的写法（全库排查出 3 处）。

### 9.6 其它

| 项 | 处理 |
|---|---|
| `resolve` 函数空结果报错信息为 `None` | 区分「引擎报错」与「抽不出文本」，后者提示需要 OCR |
| uvloop 未启用 | 启动时自动探测，缺了退回 asyncio 默认循环 |
| `build/` 陈旧副本（36 个文件，与源码不一致） | 删除并加入 .gitignore，避免污染全文搜索 |

---

## 十、仍未解决（截至当日第三轮更新）

| 项 | 说明 |
|---|---|
| **多进程扩展** | 单进程吞吐上限约 9.5 req/s（8 核后进入平台期）。多 worker 需要先确认 LanceDB 的跨进程写安全性（当前写锁是进程内的） |
| ~~真实 embedding 服务~~ | ✅ 已接入硅基流动 `BAAI/bge-m3`（1024 维，与表宽一致），存量向量已用真实模型重嵌入，`/api/health` 转绿 |
| ~~rerank 未验证~~ | ✅ 已接入 `BAAI/bge-reranker-v2-m3`（`provider=api`），检索响应标注分数口径为 rerank |

> 上述两项通过 `/api/models/test` 实测连通，并以真实检索验证排序变化；
> 之后的模型热切换只需在设置页填写凭据（密钥回显 `***`，回传掩码不会覆盖真实值）。

---

## 验证汇总

| 项目 | 结果 |
|---|---|
| `pytest` | **255 passed**（撰写时） |
| `tsc --noEmit` | 零错误（撰写时） |
| 接口数 | 46（文档与后端逐条比对一致） |
| 浏览器实测 | 各页面正常，零 console 错误 |

> 本报告撰写后，前端从「手写 DOM + tsc 直出」迁移到 **Svelte 5 + Vite**
> （`web/dist` 由 FastAPI 托管），分块表增加窗口化渲染，并补全了当时
> 缺失的分块管理（编辑/删除/批量启停）、独立分块页与两级分块方案编辑。
> 当前测试数为 **304 passed**，详见 `docs/DESIGN.md` §11/§15。
