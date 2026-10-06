# Raggi 深度审计报告（第二轮）

> **状态：全部问题已修复（2026-10-02）。** 回归测试见 `tests/test_audit_fixes.py`
> （45 项，锁定每条缺陷的具体行为）；全量 101 项通过，`tsc --noEmit` 零错误。
> 修复记录见文末「修复记录」。

> 日期：2026-10-02 ｜ 范围：`rag/` 全量 34 文件 + `web/src/` 全量 15 文件
> 方法：静态审查 + 隔离数据目录运行时复现（临时 LanceDB + 桩 embedder + TestClient），
> 未触碰运行中的 `data/` 实例。
> 基线：`pytest` 56 项全绿 —— **所有下列问题都在现有测试覆盖之外**。

## 与首轮报告（AUDIT-2026-10-02.md）的关系

逐条复核结果：

| 首轮编号 | 状态 |
|---|---|
| A1 重解析先删后写 | ✅ 已修复（改为向量化成功后才删旧块，失败置 `failed`） |
| A2 删库不清理独立分块 | ❌ **仍存在** |
| A3 删除分块不回写计数 | ❌ **仍存在** |
| A4 设置不落盘 / 路径不一致 | ❌ **仍存在**（且本轮发现更严重） |
| B1 空过滤 `doc_id IN ()` | ❌ **仍存在** |
| B2 异常一律归因 embedding | ❌ **仍存在**（本轮发现恶化：裸 500 + 栈回溯） |
| B3 reload 警告死代码 | ❌ **仍存在** |
| B4 URL 不抽正文 | ❌ **仍存在** |
| B5 前端丢弃 warnings | ❌ **仍存在** |
| C1 报告导出无入口 | ❌ 仍存在 |
| C2 列表硬截断 / 先截断后过滤 | ❌ 仍存在 |
| C3 Rerank 卡片无效字段 | ❌ 仍存在 |
| C4 重切分覆盖 created_at | ✅ 已修复 |
| C5 统计条丢格 | ❌ 仍存在 |
| C6 N+1 方案请求 | ❌ 仍存在 |
| C7 停用/启用无写锁 | ❌ 仍存在 |

**本轮新增 5 个问题，其中 2 个为安全漏洞（S 级）**，且 S1 可直接读取服务器任意文件。

---

## 结论概览

| 级别 | 编号 | 问题 | 一句话影响 |
|---|---|---|---|
| **S** | S1 | `/api/documents/url` 可读任意本地文件（`file://`） | 无鉴权时可直接把 `/etc/shadow`、`~/.ssh/id_rsa` 灌进索引并检索出来 |
| **S** | S2 | URL 入库无协议/内网校验（SSRF） | 可探测内网端口、云元数据服务（`169.254.169.254`） |
| A | A2 | 删除知识库不清理独立分块 | 删库后数据残留且仍可被检索 |
| A | A3 | 删除分块不回写 `chunk_count` | 计数永久漂移，健康检查永久 degraded |
| A | A4 | 设置「保存」不落盘 + 落盘/读取路径不一致 | 重启后配置全丢；`--data` 下配置从未生效过 |
| A | **A5** | `/resplit` 无异常兜底，裸抛 500 + 完整栈回溯 | 唯一未兜底的写端点；栈信息直泄给客户端 |
| B | B1 | 空过滤集 → `doc_id IN ()` → 503 | mime/engine 过滤在空集时必然报错 |
| B | B2 | 几乎所有异常包装成「embedding 服务不可用？」 | 维度失配/SQL 错误被伪装成模型故障 |
| B | B3 | `reload()` 警告是死代码（恒为空） | dim 误改无任何提示，写入链路静默瘫痪 |
| B | **B4** | 原始 HTML（含 `<script>`）直接入索引 | 索引污染、脚本内容进 FTS |
| C | C1–C7 | 见文末 | 报告导出无入口 / 硬截断 / N+1 等 |
| C | **C8** | `add_manual_chunk` 不校验 doc_id 存在 | 可造孤儿分块，health 永久 degraded |

---

## S 级：安全

### S1 `/api/documents/url` 支持 `file://` → 任意本地文件读取

`rag/parsing/loaders.py:69-74` 的 `load_url` 直接把用户 URL 交给 `urllib.request.urlopen`。
该函数不校验协议，`file://` 默认处理器生效，且**读取内容会被解析、切分、向量化并写入数据库**，
之后可通过 `/api/search` 检索出来，等于一个「读文件并回显」的原语。

**实测**：
```python
load_url("file:///etc/passwd")
→ OK, 3275 chars, head='root:x:0:0:root:/root:/bin/bash\ndaemon:x:1:1:...'
```
`/etc/shadow`、`~/.ssh/id_rsa`、`~/.aws/credentials`、`.env` 同一路径均适用。
`POST /api/documents/url {"url": "file:///home/admin/.ssh/id_rsa"}` 即可完成，
随后 `POST /api/search {"q":"BEGIN PRIVATE KEY"}` 命中。

**放大因素**：`token` 默认为空（不启用鉴权）、`host` 默认 `0.0.0.0`，且 `data/config.toml` 从不存在
（见 A4）——**默认部署下这是未鉴权的远程可达端点**。

**修复**：
1. `load_url` 白名单 `{"http", "https"}`，其余协议直接拒绝；
2. 解析后校验 host，拒绝私有/回环/链路本地地址
   （`ipaddress.ip_address(host).is_private / is_loopback / is_link_local`）；
3. 入库正文做裁剪上限（如 5MB），避免读入超大文件；
4. 前端与文档明示该端点需要 `token` 鉴权。

### S2 SSRF：内网探测与云元数据窃取

同一端点接受任意 `http://` / `https://`，无内网限制，且服务端请求失败时
**错误信息回显给客户端**（`rag/api/documents.py:128-129`），使盲打变为可观测的端口扫描：

```
POST /api/documents/url {"url":"http://169.254.169.254/latest/meta-data/"}
→ 503 "URL 入库失败（抓取或解析失败）: HTTP Error 401: Unauthorized"   ← 云凭据接口
```

攻击面：云实例元数据（AK/SK/临时令牌）、内网管理后台、未鉴权的内部微服务。

**修复**：同 S1 的 1/2 项；另建议加出网代理白名单开关（`parser.allow_private_url`，默认 false）。

---

## A 级：数据 / 一致性

### A2 删除知识库不清理「独立分块」：残留且仍可被检索

**根因**：`rag/store/kbs.py:135-154` `delete_kb` 只级联删除 documents 表里属于该库的文档
（经 `delete_doc` 删其 chunks），**未处理 `chunks.kb_id = X AND doc_id = ''` 的独立分块**。

**实测**：
```
add_manual_chunk(store, emb, "standalone", None, kb_id)   # 1 条独立分块
delete_kb(store, kb_id)
chunks: 1 → 1      ← 未减少
leftover: [{'chunk_id': '...', 'kb_id': '<已删>', 'doc_id': ''}]
```
检索侧 `kb_id` 直接下推到 `chunks.kb_id`，因此已删除知识库的内容**仍会被搜出来**。
前端删除确认文案（`web/src/kbs.ts:136-142`「将同时删除…全部…分块」）与实际行为不符。

**测试盲区**：`tests/test_kbs.py:138` `test_delete_kb_cascades` 只种了**文档分块**
（走 `delete_doc` 被清掉），从未构造独立分块，故该 bug 逃过测试。

**修复**：`delete_kb` 末尾追加 `store.chunks.delete(where=f"kb_id = '{escape_sql(kb_id)}'")`
（文档分块已先删，剩余即独立分块）。

### A3 删除分块不回写 `documents.chunk_count`

`rag/chunk_edit.py:228-230` `remove_chunk` 仅删行，未调 `touch_doc_count`
（`edit_chunk` / `batch_edit_chunks` / `add_manual_chunk` / 重切分 都会调）。

**实测**：
```
DELETE /api/chunks/{id} → ok
GET  /api/health       → degraded, mismatch: [{'doc_id':'d1','stated':1,'actual':0}]
```
且无任何修复入口——只能靠重切分「意外」修复。degraded 长期存在会让健康检查失去信号价值
（这个信号本就是 S 级问题后唯一的自查手段）。

**修复**：`remove_chunk` 先取 `doc_id` 再 `touch_doc_count`；
建议一并加 `POST /api/reconcile` 批量对账（health 已算出全部 mismatch，可直接复用）。

### A4 设置「保存」不落盘 + 落盘路径 ≠ 读取路径（两层问题）

**第一层**：`web/src/system.ts:161-179` 调用 `PUT /api/models` **不带 `?persist=true`**，
后端 `persist` 默认 `False`（`rag/api/models.py:32`）——UI 提示「配置已保存并热切换」，实际重启即丢。
DESIGN §10 约定「PUT 落盘 config.toml」。

**第二层（本轮新发现的更严重问题）**：即使带 `persist=true`，落盘与读取也是两条不同的路径：

| | 路径来源 | 值 |
|---|---|---|
| 落盘 | `Settings.config_file` = `self.data_dir / "config.toml"` | 跟随 `--data` |
| 读取 | `model_config["toml_file"]` 硬编码字面量 | **恒为 `data/config.toml`（相对 cwd）** |

`settings_customise_sources`（`rag/config.py:84`）在**类定义期**就取 `cls.model_config.get("toml_file")`，
而 `server.py:23` 的 `settings.data_dir = Path(args.data)` 发生在**导入之后**，
故 TOML 源无法感知 `--data`。实测：
```
settings.config_file            -> data/config.toml
Settings.model_config[toml_file] -> data/config.toml   ← 与 --data 无关
```
用 `rag serve --data /srv/raggi` 启动时：配置写到 `/srv/raggi/config.toml`，
重启却去读 `./data/config.toml`（不存在）→ **配置静默失效，且无任何报错**。

现状 `data/config.toml` 确实不存在——意味着**所有在设置页做过的修改都没留下过痕迹**。

**修复**：
1. 前端带 `?persist=true`（或后端 PUT 默认改为落盘）；
2. 把 TOML 读取改为延迟到实例化时基于最终 `data_dir` 解析——
   例如去掉 `model_config` 里的硬编码，在 `settings_customise_sources` 中读
   `cls.data_dir / "config.toml"`，并保证 `--data` 在构造 `Settings` **之前**生效
   （`server.py` 改为先 `os.environ["RAG_DATA_DIR"]=args.data` 再 import/构造）。

### A5 `/resplit` 是唯一无异常兜底的写端点：裸 500 + 完整栈回溯

`rag/api/plan.py:78-90` 只捕获 `KeyError` / `ValueError`，其余异常直通。
对比 `documents.py` / `search.py` / `models.py` 都有 `except Exception → HTTPException`。

**实测**：
```
POST /api/documents/{id}/resplit  （embedding 维度失配）
→ HTTP 500，响应体/日志含完整 traceback：
   File "rag/ingest/pipeline.py", line 346, in resplit_doc
   File "rag/ingest/pipeline.py", line 207, in _run
   RuntimeError: vector length mismatch: dim 1024 vs 4
```
A1 的修复本身是有效的（实测旧分块保留、`status=failed`），但**错误出口不一致**：
用户在同一页面上「重解析」得到 503 + 人话，「重切分」得到裸 500。

**修复**：补 `except EmbedUnavailable` → 503，兜底 `except Exception` → 500 + 原始摘要
（不要沿用 B2 的「embedding 服务不可用？」话术，见下）。

---

## B 级：功能错误

### B1 过滤命中 0 文档 → `doc_id IN ()` → 503

`rag/retrieve.py:61-72` `_doc_ids_by_attr` 无匹配时返回 `[]`（而非 `None`），
`_in_clause([])`（:56-58）生成非法 SQL。

**实测**：
```
_build_where({"mime":"application/x-nonexistent","_store":store})
→ 'enabled = true AND doc_id IN ()'
→ ValueError: Error parsing statement: ... WHERE enabled = true AND doc_id IN ()
   (sql parser error: Expected: an expression, found: ))
```
`mime` / `parser_engine` 过滤均受影响；`store=None` 时才返回 `None` 短路。

**修复**：`ids == []` 时返回恒假条件（`doc_id IN ('__none__')`）或直接短路返回 0 结果。

### B2 异常一律归因「embedding 服务不可用？」，且掩盖真实原因

`rag/api/search.py:32-34`、`documents.py:67-69/89-91/127-129`、`models.py:80-82`
把所有 `Exception` 包装为 503 +「（…embedding 服务不可用？）」。

**实测**：把 embedder 换成返回维度错误向量，SQL 层错误、Arrow 维度失配全部
被报成「embedding 服务不可用？」。已有全局 `EmbedUnavailable` handler（`rag/api/__init__.py:130`），
各端点无需重复捕获——反而掩盖了它。

**修复**：删除这些 catch-all，或至少区分 `EmbedUnavailable`（503）与其它（500 + 原生摘要）。

### B3 `reload()` 警告是死代码（恒为空），dim 误改静默瘫痪写入

`rag/models/registry.py:29-35` `_build` 里 `embed_cfg=s.embed` 存的是**同一对象引用**；
`merge_sub`（`rag/api/_common.py:37`）`setattr` 就地修改它。
`reload()` 里 `old.embed_cfg.dim != self.settings.embed.dim`（:43）比较的是**自己和自己**。

**实测**：
```
merge_sub(settings, "embed", {"dim": 32})   # 模拟 PUT /api/models
registry.reload() → warnings: []            ← 恒空
bundle.embed_cfg is settings.embed → True
```
后果：dim 改了以后所有入库/编辑持续失败（Arrow 维度失配），
错误又被 B2 伪装成「embedding 服务不可用」，用户完全无从定位。
而 `POST /api/reindex`（`rag/api/system.py:63-67`）只重建索引、**不改表 schema**，
`rag/store/lancedb.py:72-76` 只是打一条 warning 日志——**reindex 修不好维度问题**，唯一出路是把 dim 改回去。

**修复**：
1. `_build` 前 `copy.deepcopy` 旧配置，或在 `reload` 前快照 `dim`/`model` 数值；
2. `dim` 输入框改为危险操作（二次确认 + 明确「会导致写入失效，必须回退或迁移数据」）；
3. 修正提示语：说明 reindex 无法修复维度变更；
4. 维度不匹配时 `PUT /api/models` 应直接拒绝或返回 409，而非静默接受。

### B4 URL 入库不做正文抽取：原始 HTML（含 `<script>`）入索引

**实测**（`load_url` 返回的 `page_content`）：
```
'<!DOCTYPE html><html><head><title>RealTitle</title><style>.x{}</style></head>
 <body><script>var secret=1;</script><nav>menu</nav><p>Real body text here.</p></body></html>'
含 <script>: True    含 DOCTYPE: True    title 取自: http://...（URL，而非 <title>）
```
DESIGN §10 承诺「网页正文抽取」。后果：脚本/样式/导航文本污染 FTS 索引与向量，
`<script>` 里的密钥、内网地址一并入库并可被检索。

**修复**：抽正文（BeautifulSoup / readability / unstructured 任一），
至少剥 `script/style/nav/header/footer`，title 取 `<title>`。

---

## C 级：体验 / 边界

- **C1 报告导出无入口**：后端 `POST /api/search/report` 已实现（`rag/api/search.py:73`），
  `web/src` 全库 grep `report` **零命中** —— DESIGN §0 四大交付之一无 UI 入口。
- **C2 硬截断**：文档 `limit=200`、分块 `limit=500` 且无分页提示；`/api/chunks` 无 `total`、无稳定排序。
  `web/src/kbchunks.ts:74-76` 先取该库**前 500 条全部分块**再过滤 `doc_id=""`——
  文档分块越多，独立分块越可能整个不可见。建议后端支持 `doc_id` 过滤参数与分页。
- **C3 Rerank 卡片无效字段**：`web/src/system.ts:37-40` 含 `base_url`/`api_key`，
  后端 `RerankConfig`（`rag/config.py:23-28`）没有这两项，`merge_sub`（:32）静默忽略。
- **C5 统计条丢格**：`web/src/docs.ts` 的 `refreshKb` 重建 `#kbStats` 时不含「分块方案」格（`openKb` 有）。
- **C6 N+1 请求**：`web/src/docs.ts:114-116` 对每个文档单独请求 `/plan`，
  200 文档 = 200 请求（且是 `Promise.all` 并发打满）。
- **C7 停用/启用无写锁**：`rag/chunk_edit.py:213-225` `set_chunk_enabled` 未包
  `store.write_lock()`（`edit_chunk` / `add_manual_chunk` / `remove_chunk` 都包了），与并发入库存在竞态。
- **C8 `add_manual_chunk` 不校验 doc_id 存在**：`rag/api/chunks.py:65-72` 接受任意 `doc_id`，
  `touch_doc_count` 对不存在的文档静默 no-op（`rag/chunk_edit.py:45-53` 吞异常），
  分块成为孤儿 → `health.orphan_chunks > 0` → 永久 degraded。实测可写入不存在的 `d1`。

---

## 安全面总结

| 项 | 状态 |
|---|---|
| SQL 注入 | ✅ `escape_sql` 全覆盖（所有 where 子句均转义，含 `_doc_ids_by_attr`、`_contexts`） |
| XSS（高亮） | ✅ `rag/highlight.py` 先转义再打 `<mark>`；前端 `esc()` 覆盖模板插值 |
| 路径穿越 | ✅ 留档原文按 `doc_id` 命名（非用户可控文件名）；`stored_file` 从 DB 读 |
| 文件上传 | ✅ 有 `max_upload_mb` 上限，临时文件在 `finally` 清理 |
| **URL 协议** | ❌ **无白名单，`file://` 可读任意本地文件（S1）** |
| **SSRF** | ❌ **无内网限制，错误回显可观测（S2）** |
| **鉴权默认** | ⚠️ `token=""` 默认关闭 + `host=0.0.0.0` + 配置文件从不生成 → 默认部署等于无鉴权暴露 |

---

## 修复记录（2026-10-02）

全部问题已修复。回归测试：`tests/test_audit_fixes.py`（45 项），
全量 `pytest` 101 项通过，`tsc --noEmit` 零错误。

| 编号 | 修复方式 | 涉及文件 |
|---|---|---|
| **S1** | `load_url` 加协议白名单（仅 http/https），`file://` 直接拒绝 | `rag/parsing/loaders.py` |
| **S2** | DNS 解析后逐个校验 IP，拒绝 private/loopback/link-local/multicast/reserved，堵住元数据地址与「域名指向内网」 | `rag/parsing/loaders.py` |
| **B4** | 基于 `html.parser` 的正文抽取（stdlib，无新依赖）：剥除 script/style/nav/head 等，title 取 `<title>`，入库加 8MB 上限 | `rag/parsing/loaders.py`、`rag/ingest/pipeline.py` |
| **A2** | `delete_kb` 末尾追加 `chunks.delete(kb_id=...)`，覆盖独立分块；新增 `reconcile_doc_counts()` | `rag/store/kbs.py` |
| **A3** | `remove_chunk` 先取 `doc_id` 再删除，回写 `chunk_count` | `rag/chunk_edit.py` |
| **C8** | `add_manual_chunk` 校验 `doc_id` 对应文档存在，否则 404；新增 `POST /api/reconcile` | `rag/chunk_edit.py`、`rag/api/chunks.py`、`rag/api/system.py` |
| **A4** | ① TOML 读取路径改为按 `data_dir` 动态解析（`_toml_path()`）；② `--data/--host/--port` 在 import 前写入环境变量；③ `PUT /api/models` 默认 `persist=true`；④ **额外修复**：`save_config` 把 `dict` 写成 TOML inline table，否则 `parser.overrides` 会写成 `"{}"` 导致重启时 ValidationError —— 即「保存的配置永远读不回来」 | `rag/config.py`、`rag/server.py`、`rag/api/models.py`、`web/src/system.ts` |
| **B1** | 空 doc_id 集合转恒假条件 `doc_id IN ('__no_match__')`，返回 0 条而非 503 | `rag/retrieve.py` |
| **B2** | 新增 `raise_operation_error()` 统一分类：EmbedUnavailable→503、KeyError→404、ValueError→400、其它→500 + 原始摘要（不再伪装成 embedding 问题，也不回传 traceback） | `rag/api/_common.py` 及 search/documents/models/answer 端点 |
| **A5** | `/resplit` 补齐异常兜底（此前是唯一裸抛 500 的写端点） | `rag/api/plan.py` |
| **B3** | `ModelRegistry` 改存配置**副本**并维护标量快照，警告不再恒空；dim 变更提示明确说明「reindex 无法修复」；前端 dim 输入改为二次确认的危险操作 | `rag/models/registry.py`、`web/src/system.ts`、`web/src/state.ts` |
| **B5** | 前端解析并展示 `warnings`；保存后回读实际生效值 | `web/src/system.ts` |
| **C1** | 检索页新增「导出报告」按钮（`POST /api/search/report`），带 `downloadText()` 工具 | `web/src/search.ts`、`web/src/api.ts`、`web/index.html` |
| **C2** | `/api/chunks` 新增 `only_standalone`（过滤下推到 SQL）+ `total` + `limit/offset` + 稳定排序；前端独立分块页改用服务端过滤 | `rag/api/chunks.py`、`web/src/kbchunks.ts` |
| **C3** | Rerank 卡片移除后端不支持的 `base_url`/`api_key`，改显示真实的 `device` 字段 | `web/src/system.ts` |
| **C5** | 抽取 `kbStatsHTML()` 共享函数，入库刷新与首次打开渲染一致，「分块方案」格不再消失 | `web/src/docs.ts` |
| **C6** | 新增 `GET /api/documents/plans` 批量接口，列表页由 N 个请求降为 1 个（router 注册顺序已调整，避免被 `/api/documents/{doc_id}` 抢先匹配） | `rag/api/plan.py`、`rag/api/__init__.py`、`web/src/docs.ts` |
| **C7** | `set_chunk_enabled` 纳入 `store.write_lock()` | `rag/chunk_edit.py` |

顺带修复：`merge_sub` 改从类上读 `model_fields`（消除 Pydantic 2.11 弃用告警）、
文档正文上限 `MAX_DOC_CHARS`、上传加 `User-Agent`/`Accept` 头。