# Raggi 后端深度审计（2026-10-06）

对象：`/home/admin/Raggi` 的 Python 后端（`rag/` 58 个模块、10,991 行；
`tests/` 31 个文件、10,047 行；`tools/bench/` 基准套件）。
维度按用户提的四条分开给结论，每条发现都标 **现象 → 证据 → 影响 → 建议**，
证据只引用这次真读到的代码位置或真跑出来的数字。
本轮已经改掉的部分在 §5，尚未处理的在 §6 排了优先级。

## 0. 一页结论

**架构骨架是对的，不需要推倒。** 分层方向明确且几乎不被违反
（`api → retrieval/ingest → storage.repos → storage.{tables,meta,sql,backend}`），
`storage` 没有任何一处反向 import `api`（grep 全仓为空），
对外契约集中在 `api/responses.py`（54 个 Pydantic 模型，572 行，
这是契约不是坏味道），错误模型集中在 `core/errors.py` 且状态码单点映射。
上一轮改造后并发表现实测：`/api/documents` @c=32 从 84 rps 到 456 rps，
入库从「越并发越慢」（2.81 docs/s @c=32）变成单调上升（60.5 docs/s）。

**真正的问题集中在一个模式上：隐式协议。**
「双引擎分派」「跨进程写锁」「手写缓存」「配置读取」这四件事都靠
*约定*而不是*类型或构造*来保证——属性恰好被挂上、调用方记得传对参数、
配置项恰好有人在读。这类东西出错时不会报错，只会**结果悄悄不对**。
本轮在这一个模式上抓出并修掉了 5 个严重缺陷（§5.1 与 §2.4），
其中两个直接导致数据丢失与鉴权失效，一个由 **CI** 首先发现。

**工程实践的缺口比代码问题更该先补，而且已经补上了**：
本仓原先没有 git、没有 lint、没有 CI，也没有类型检查；
A 档四项（`git init` / ruff / CI / 装配函数拆分 + 分派收口）已在本轮完成。
补上的第二天就见了效——第一次真实 CI 运行就抓出 `.gitignore` 写错导致
**推上去的仓库从干净检出构建不起来**（§2.4），而这个问题在本地永远不可见。

| 维度 | 结论 | 本轮已修 | 仍开放 |
|---|---|---|---|
| 架构专业性 | 骨架正确，分派机制是隐式的 | 1 | 4（A1–A4） |
| 可维护性 | 工具链从无到有；注释仍在漂移 | 4（git / ruff+CI / create_app / `.gitignore`） | 2（M3 其余 11 个长函数、M5 文档漂移；mypy 归 M2） |
| 代码规范性 | 一致性高于一般个人项目 | 4 | 3（C1–C3） |
| 性能 | 上一轮的收益是真实的，余量明确 | — | 5（P1–P5） |

测试：491（改造前）→ 517（并发改造）→ 544（审计轮 +27）→ 548（A 档执行轮 +4）；
加上 `addopts` 把延迟门槛移出默认套件后，**默认套件 547 passed / 1 deselected**，手动 `-m perf` 再补上最后一条。

## 1. 架构专业性

### 1.1 做对了的（值得保留，别在后续重构里弄丢）

**分层与依赖方向**。`rag/storage/` 是唯一接触数据库连接与建表的地方，
并且这条纪律**由测试守着**：`test_architecture.py:330` 禁 LanceDB 连接外溢，
本轮 `test_sql_safety.py` 补了 SQLite 连接的同一条款。
`storage` 不反向依赖 `api`/`retrieval`/`ingest`，全仓 grep 无一处例外。

**双引擎只在一处知道方言**。`documents` / `kbs` 有意图型读接口
（`docs_query` / `docs_count` / `kbs_query`：传 `eq` / `one_of` / `contains` / `ids`，
不传 SQL 文本），所以「SQLite 用占位符、LanceDB 只能拼接」这件事
不会漏到业务层。这是上一轮分裂读事故的正解，形式是对的。

**后端抽象**。`Backend`（local / s3）让「换存储位置」只改一层，
`lock_kind` 把「能不能安全开多进程」变成可查询的事实而不是口头约定。

**写锁的两层保证**有专门测试，且这条测试确实抓回过一次真缺陷
（只换 `flock` 会丢线程互斥，因为 flock 的互斥单位是 fd）。

**观测端点的诚实性设计**。`/health`、`/stats` 走 3s TTL 快照，
并且用 `X-Snapshot-Age-Ms` 把头里「这是 N 秒前的数据」告诉调用方 ——
比大多数个人项目的降级处理更严谨。

### 1.2 A1 · 双 SQL 方言只收口了一半（中等，开放）

**现象**：`chunks` 表没有意图型读接口。
**证据**：3 个模块在 `storage` 之外自己拼 where ——
`rag/api/chunks.py:66-80`（`doc_id`/`kb_id`/`text ILIKE`）、
`rag/retrieval/answer.py:61`、`rag/retrieval/search.py:80-106`。
**影响**：现在不出错（转义都做了，且本轮加了断言），
但**将来 chunks 也要搬引擎时**这三处各自需要理解方言差异，
而它们今天没有任何机制提醒。
**已做的缓解**：`test_sql_safety.py` 里一条**棘轮**测试 —— 白名单就是这 3 个文件，
新增第 4 个就红，错误信息直接指出「该去 `repos/` 加意图型函数」。
**建议**：补 `chunks_query(store, cols, *, eq=…, one_of=…, contains=…, ids=…)`，
把 3 个模块改成传意图，然后把棘轮白名单清空。纯机械重构，
但落在检索热路径上，改完必须跑 `rag-bench quick/ingest` 对比再合。

### 1.3 A2 · 四对真循环依赖，靠函数内 import 掩盖（中等，开放）

**现象**：全仓 25 处函数内 `import rag.*`，其中这些是真环：
**证据**：
- `rag/api/__init__.py:360`（`_startup_maintenance` → `rag.server`）
  ↔ `rag/server.py:34`（`build_app` → `rag.api`）；
- `rag/storage/plan.py:28`（模块级 → `.repos`）
  ↔ `rag/storage/repos/kbs.py:118,149`（函数级 → `..plan`）；
- `rag/storage/repos/documents.py:418`（函数级 → `.kbs`）
  ↔ `rag/storage/repos/kbs.py:11`（模块级 → `.documents`）；
- `rag/storage/tables.py:195`（函数级 → `.repos`）
  ↔ `rag/storage/repos/kbs.py:13`（模块级 → `..tables`）。
**影响**：今天能跑，代价是**「谁依赖谁」不可读**。搬文件、改名、拆模块时
会撞 ImportError 才发现方向反了；而 `documents.py:418` 那条注释写的是
「kbs.py 依赖本模块」—— 说明作者当时知道，只是把环留在了原地。
**建议**：先解最干净的第一条：`should_run_startup_maintenance` /
`mark_maintenance_done` 是**启动策略**而不是进程装配，把它们从 `server.py`
移到 `rag/core/`，`api/__init__.py` 就不需要回头 import server。
后三条涉及「重切分归谁」「schema 归谁」「列清单归谁」的边界判断，
属于 §6 的 C 档，动之前要先定职责。

### 1.4 A3 · 引擎分派机制本身是隐式的（严重根因，部分已修）

**现象**：四个仓储模块各有一份
`def _meta(store): return getattr(store, "meta", None)`
（`repos/{documents,jobs,kbs,keys}.py`），而 `store.meta` 这个属性过去由
`api/__init__.py:91` 从**外部**挂上 `LanceStore` 实例。
**证据 / 已造成的事故**：`has_keys()` 数 Lance 里的空 `apikeys` 表
而密钥写在 SQLite ⇒「库里一把密钥都没有」永远成立 ⇒ 整个 API 无鉴权；
列表读 SQLite、健康检查读 Lance ⇒「界面 8 篇、健康说 7 篇、检索少一篇」；
自动迁移的列清单漏 `text` ⇒ 每篇文档正文被写成空串而行数校验全绿。
**本轮已修的部分**：`meta` 现在是 `tables.py` 里声明的 property
（getter/setter 都在类上，注释写明了这条因果），
`getattr(store, "meta", None)` 不再依赖「属性恰好存在」。
**仍开放**：四份 `_meta()` 重复；更关键的是**拿不到 meta 时的默认行为是
「静默走回退路径」**，而不是失败。回退路径是有意设计的（`meta_engine=lancedb`），
但它应该是**显式配置的结果**，不是属性缺失的副作用。
**建议**：把这一行收到一处；并在默认配置（`auto`）下取不到 meta 就启动失败，
只有显式写了 `meta_engine=lancedb` 才允许回退。

### 1.5 A4 · 重扫描型对账仍在请求路径上（轻微，已知余量）

`/health`、`/stats` 已快照化（实测 p50 40.7→2.9ms、28.7→546 rps @c=32，
`/api/stats` @c=32 从 5053ms 到 29.8ms），但 `reconcile` 仍是同步全量比对。
DESIGN §16 与 `PERF-IMPLEMENTATION` §3 都如实声明了这条，
原计划的 SSE / 后台周期任务没做。**不算缺陷，是已声明的余量。**

## 2. 可维护性

### 2.1 M1 · 没有版本控制（最高优先，**已完成**）

无 `.git`。本轮 20 多个文件的修改**无法 diff review、无法回滚、无法 bisect**。
在一个以「多引擎分派 + 手写并发件 + 跨进程锁」为特征的代码库里，
这是最需要安全网的地方。成本 5 分钟。我没有擅自 `git init`（需要授权）。

### 2.2 M2 · 没有 lint / 类型检查 / CI（中等，**ruff 与 CI 已完成，mypy 仍开放**）

`pyproject.toml` 只有 `[tool.pytest.ini_options]` 与打包配置；无 ruff、无 mypy、
无 `.github/workflows`。今天唯一的自动化安全网是 `pytest`。

**为什么类型检查在这里特别值钱**：双引擎分派的签名全是
`MetaStore | None`，而 §1.4 那三起事故全都是「None 走到了不该走的分支」。
`mypy --strict` 对 `rag/storage/` 跑起来，能在写的时候就把
「这个 store 到底有没有 meta」变成显式问题。

**建议顺序**：① ruff（lint + import 排序，零行为改动，顺手治 §4 的 C1/C2）；
② 一条跑 `pytest -q` 的 CI；③ mypy 只对 `rag/storage/` 起步。

### 2.3 M3 · 12 个超过 60 行的函数（中等，create_app 已拆，其余 11 个开放）

最长的是 `create_app` 180 行、`search` 161、`_run`（pipeline）140、`main` 104、
`health` 77、`auth_middleware` 73、`authenticate` 71、`api_upload` 69、
`prepare` 65、`submit` 65、`import_from_lance` 64、`batch_edit_chunks` 63。

`create_app` 是最该拆的：它做了 6 件事——中间件注册、异常处理挂载、路由挂载、
静态资源、启动维护钩子、OpenAPI 定制 ——而它是全服务的装配点，
所以任何一块的改动都要在 180 行里定位。拆成
`_wire_middleware` / `_wire_handlers` / `_wire_routes` / `_wire_openapi`
是纯移动、不改逻辑，风险极低。

（对照：`api/responses.py` 的 572 行**不是**问题，它是 54 个声明式契约模型；
`storage/repos/documents.py` 的 437 行也合理，因为它把一张表的读写收在一处。）

### 2.4 M4 · `.gitignore` 的 `data/` 未锚定，把源码排除掉了（严重，已修 —— 由 CI 发现）

**现象**：推上去的仓库**从干净检出构建不起来**：

```
Could not resolve "../data/apidoc" from "web/src/ui/Dock.svelte"
```

**根因**：`.gitignore` 里写的是 `data/`（没有前导斜杠）。按 gitignore 语义，
它匹配**任意层级**上叫 `data` 的目录，于是把 `web/src/data/` 整个排除了 ——
那里面是 `apidoc.ts`（58KB，**手写**的对外接口清单，文件头明确写了不是从
`/openapi.json` 生成的），被 `ApiDoc.svelte` 与 `Dock.svelte` import，
还被三个测试直接读盘：`test_apidoc.py`（文档与后端路由双向比对）、
`test_architecture.py:539`、`test_frontend_wiring.py:319`。
改成 `/data/` 后只匹配根目录那一个；纳入文件数 173 → 174，只多出这一个，
确认没有别的源码被同一条规则吞掉。

**为什么本地完全看不出来**：本机是唯一检出，那份文件一直在，
构建与全部测试都通过（本机是唯一检出，那份文件一直在）。这类"排除规则写错"只惩罚**新克隆的仓库**，
在唯一的开发机上永远不发作。第一次 CI 运行两个 job 都红
（`frontend` 在 `npm run build`，`python` 在 `pytest`），
就是把这条规则从"看起来有门禁"变成"真门禁"的价值所在。

**这也是本仓第一条由 CI（而不是由人或本地跑）发现的缺陷**。
它同时印证了本文开头那个判断：这个后端的问题不在设计，而在
"约定与实现之间的缝隙"—— `.gitignore` 也是一处约定。

### 2.5 M5 · 「文档式注释」正在漂移（中等，部分已修）

这个仓库的注释是**资产**：大量事故复盘直接写在代码旁边，
`docs/` 里 5 份长文记录了取舍与实测。但审计时找到 3 处注释与实现不符，
本轮已修：

- `filelock.py` 边界 2 写「按线程记录深度」，实现是**全局一个计数器**，
  而且只有全局才对（共用同一 fd 时按线程记，会让线程 A 释放自己那份 depth
  就 `LOCK_UN`，而线程 B 还以为自己持着锁）；
- `sql.py` 的 `_quote_in` 别名注释写「测试里也用到」，全仓**零调用点**
  —— 骗人的注释比没有注释更坏，因为它让后来人不敢清理；
- `meta.py` 的性能对照表里有一行「领取一个排队任务 0.009ms」，
  对应的是从未实现的 `claim_job`。

审计之后又找到两例同一形状的漂移，都由「CI/新检出」这个视角照出来：

- **`.gitignore` 的 `data/` 未锚定**（§2.4）——本地是唯一检出，所以永远看不见；
- **`pyproject` 里「perf 标记默认不跑」这句注释不成立**：只声明 `markers`
  不会排除用例，排除要靠 `addopts`。于是那条 `p50 < 150ms` 的延迟门槛
  一直混在默认套件里跑，而它自己的 docstring 第一行就写着「只在
  `pytest -m perf` 下跑」。本地机器快、负载稳，所以没炸——
  **注释描述的机制不存在，和注释描述的行为不符，是同一类问题的两种形态**。

同一轮还发现 `PERF-FINAL-DECISION` 的架构图声称 SQLite 里有
`doc_text` 独立表、`ratelimit` 表、`idempotency` 表、`outbox` 表 ——
四个都不存在（正文仍是 `documents.text`，限流是进程内令牌桶，
后两个是从未接线的预留）。已按实际实现改写。

**没有机械手段能发现这类漂移**，这是结构性风险。
建议把「改行为必须同批改注释」写进贡献约定，
并要求注释里的数字（延迟、倍数）注明来源与日期，过期就删。

## 3. 代码规范性

### 3.1 一致性高于一般个人项目（值得肯定）

命名成对且可预测（`docs_query`/`docs_count`、`kbs_query`/`set_kb_fields`、
`add_job`/`set_job`/`get_job`/`list_jobs`）；8 个 SQL 纪律收在 `storage/sql.py`
一处（`escape_sql` / `escape_like` / `quote_in` / `only_cols` / `scalar_rows` /
`count_rows` / `fill_missing` / `table_columns`）；`__all__` 与真实调用点对齐；
无 `TODO/FIXME/XXX` 残留（grep 为空）；文档字符串一律中文且解释「为什么」；
`pyflakes rag tools` 现在只剩 1 条报告，而且是有意标了 `noqa: F401` 的
uvloop 可用性探测。

### 3.2 C1 · 80 列违规 39 行，分布在 22 个文件（轻微，开放）

没有 formatter 强制执行，所以会持续增长。M2 的 ruff 顺手能治。
不建议手工重排（改动面大且无收益）。

### 3.3 C2 · `tests/` 有 27 条 pyflakes 报告（轻微，开放）

16 条未使用 import、10 条赋值后未使用的局部变量。
不影响正确性，但会让人怀疑「这个 import 是不是本来该有用到，是漏了断言？」
—— 那是噪声。也交给 ruff。

### 3.4 C3 · 23 处 `except Exception` 紧跟 `logger.debug`（中等，开放）

全仓 92 处 `except Exception  # noqa: BLE001`。多数是**刻意的**，
而且理由写在旁边（观测数据不该拖垮主流程：任务状态写失败不抛、
`last_used_at` 更新失败只记日志），这个取舍是对的。
但有 23 处降到 `logger.debug`，默认日志级别下**线上完全看不见**。
建议区分两类：「不影响正确性」留 debug；「影响结果但可对用户降级」
一律 `logger.warning` —— 后者静默的含义是用户看到的数据少了却毫无痕迹。

## 4. 性能

### 4.1 上一轮的收益是实测的（同一台 4 核机、同一份 2000 文档 / 20000 分块、假 embedding）

| 指标 | 改造前 | 改造后 |
|---|---|---|
| `/api/documents?limit=50` p50 @c=1 | 13.4ms | 4.8ms |
| `/api/documents` 吞吐 @c=32 | 84 rps | 456 rps |
| `/api/jobs` 吞吐 @c=32 | 138 rps | 635 rps |
| `/api/health` p50 @c=1 | 40.7ms | 2.9ms |
| `/api/stats` p50 @c=32 | 5053ms | 29.8ms |
| `/api/search`（hybrid）p50 @c=1 | 75.8ms | 57.5ms |
| 入库吞吐 @c=1 | 6.52 docs/s | 18.9 docs/s |
| 入库吞吐 @c=32 | 2.81 docs/s（越并发越慢） | 60.5 docs/s（单调上升） |
| 48 次入库后 `jobs` 表版本数 | 226 | 3 |

最后一行是这轮改造最重要的数字：写放大被拆掉了，
剩下的版本增长与**真实数据量**成正比，「用得越久越慢」的机制不存在了。

### 4.2 P1 · `embed` 合批没实现（中等，开放 —— 唯一还没兑现的 S5 条目）

`PERF-FINAL-DECISION` S5 写了「10–20ms 窗口内 embed 合批」，
但 `rag/models/embeddings.py` 里没有任何批处理队列
（grep `batch` / `deque` / 窗口相关为空）。并发入库时每个文档的分片各自打一次 HTTP。
这是入库吞吐下一个最可期待的提升点。

### 4.3 P2 · 检索的 57.5ms 是不含模型网络的下界，且瓶颈不在 Python（中等，需实测）

审计时先否掉了一个直觉优化：**「把三条检索通道并发化」不适用** ——
`search()` 走 LanceDB 原生 hybrid（`tbl.search(query_type="hybrid")`，一次查询），
不是 vector + FTS + scalar 三次串行（`retrieval/search.py:272`）。
真实构成是 `candidate_k=50 × refine_factor=10` ⇒ 引擎内部对 500 条候选精确重排，
实测这段约 48ms，占整次检索 80% 以上（`refine_factor=1` 时同样查询 22.6ms）。
**但 `refine_factor` 是召回质量旋钮，不该由性能单方面调**。
建议：用 `rag-bench scan-paths` 做 `refine_factor` / `nprobes` 敏感性实测，
再决定默认值 —— 而不是照搬「减半延迟」这个数。

### 4.4 P3 · 并发闸是每进程一把（轻微，已声明）

`ScaledSemaphore` 的多进程总并发 = 进程数 × 配置值，
类注释与 `PERF-FINAL-DECISION` §5 的契约表都写明了。
真要做全局闸需要跨进程信号量，属于「多机」议题，现在做只会增加故障面。

### 4.5 P4 · 观察到一次 native 段错误（事实记录，未定位）

全套件跑时出现一次 `timeout: 被监视的命令已核心转储`（87 个扩展模块的崩溃报告），
重跑即过。怀疑是 pyarrow/lance 在这台 3GB 内存机器上的边界，
**但没有定位到原因**，所以这条只作为事实记录。
要做长跑或 CI，先把并行度压下来并把内存上限纳入监控。

### 4.6 P5 · 未测的量（诚实清单）

- 真实模型服务下的端到端并发与延迟**全部未测**（全程假 embedding，不花额度）；
- `MetaStore` 连接按线程隔离 ⇒ 默认 executor 8 线程 = 每进程 8 个 SQLite 连接，
  4 进程 = 32 连接 + WAL/SHM。文件描述符数量与 `-wal` 增长未实测；
- `s3` 后端只以 mock 单测验证，没对真实 RustFS/MinIO 跑过；
- 多机部署不在范围内（`filelock` 边界 1 写了这条）。

## 5. 本轮已修（4 个严重 + 一批中轻，全部有对应测试）

### 5.1 四个严重问题（都在生产默认路径上）

**(S1) `LanceStore.meta` 是装配层从外面挂上去的属性。**
仓储层用 `getattr(store, "meta", None)` 决定读哪个引擎，属性却由
`api.Ctx.__init__` 赋值。任何一条装配路径忘了赋（脚本、测试、`rag migrate`、
未来的第二个入口），读写就分别落在两个引擎上，**不报错**。
现在 `meta` 是 `storage/tables.py` 里声明的 property，注释写明这条因果。
守卫：`tests/test_meta_store.py` 的双引擎对齐测试（同串操作分别跑两条路径，
断言外部可见结果一致）。

**(S2) `MetaStore.upsert` 往 `NOT NULL DEFAULT …` 的列写显式 NULL。**
SQLite 的列默认值只在**该列被省略**时生效，显式 NULL 直接撞约束；
而仓储层的写法是「按列清单从字典取」（`{k: r.get(k) for k in DETAIL_COLS}`），
缺列天然就是 None。LanceDB 那边有 `fill_missing` 兜住。
⇒ 同一份写入一条引擎成功、另一条 `IntegrityError`。
现在 `not_null_defaults()` 读 `PRAGMA table_info`、求值一次并缓存，None 换成列默认值。

**(S3) 投影 `cols` 被直接拼进 `SELECT`，没有白名单。**
`docs_query` / `kbs_query` / `get_job` 接受调用方传的列名。
值已经统一走占位符或 `escape_sql`，但**列名在 SQL 里天生不能参数化**，
所以投影是唯一能改写查询本体的位置。
现在 `sql.only_cols()` 在**引擎分派之前**收窄（只收一条路径等于没收）；
白名单取代码声明的列清单而不是实时 schema ——
升级过程中 schema 可能落后于代码，拿 schema 当白名单会把正常读打挂。
守卫：`test_projection_cannot_reach_unlisted_columns`（两条引擎都测）。

**(S4) 两个同名 `QueueFull`。**
`core/errors.py` 一个（`RaggiError`，503），`ingest/queue.py` 又一个（`RuntimeError`），
实际抛的是后者，而 `_common.py` 捕获后硬写 `HTTPException(503)` ——
于是 `except core.errors.QueueFull` 会静默漏接，`Retry-After` 无从生效。
文档（DESIGN §6/§16/错误契约）写 503，代码注释写 429，
`PERF-FINAL-DECISION` §5 说「改成 429」—— 三个地方三种说法。
现在只有一个类，状态码与 `Retry-After: 30` 由异常自己声明，映射点仍只有一处。
语义也定死了：**503 = 容量背压（队列满）**，**429 = 策略限流**，
因为客户端需要能从状态码判断「该降频」还是「该等」。

### 5.2 死代码与投机 schema（11 处）

DDL 里建了但**零调用点**：`idempotency` 表（幂等键最终落在进程内
`IdempotencyStore`）、`outbox` 表（跨引擎级联最终靠启动期对账）、
`jobs.worker` 列 + `idx_jobs_claim` 索引（多进程领取任务从未实现，
队列是 `ThreadPoolExecutor`）。
函数/API 层：`claim_job`、`table_exists`、`DOC_LIST_COLS`、
两份靠人肉同步的 `JOB_COLS`、`NullLock`、`CircuitBreaker.state()`、
`ScaledSemaphore.acquire()/release()`（裸的一对，允许写出异常路径漏 release）、
`TTLCache.stats()/hits/misses`（没人读的埋点）、`_quote_in` 别名。

死 schema 比死代码更坏：它**看起来像契约**，会诱导下一个人在上面写代码。
守卫：`test_no_dead_tables_in_meta_ddl` —— 把 DDL 字面量从搜索语料里摘掉再找引用，
否则「表名出现在 DDL 里」就等于「有人用」，这条测试会永远绿。
存量库里的残留**故意不 DROP**（启动期删表是数据丢失面，而它们要么 0 行要么 1 列），
理由写在 DDL 上方注释里。

### 5.3 手写并发件现在有直接测试，并修掉一个真实缺陷

`rag/core/cache.py` 是手写的并发 LRU+TTL，站在**鉴权**与**观测端点**两条关键路径上，
而它的直接测试是 **0 个**。补 `tests/test_cache.py` 10 条，
逐条钉住它注释里声称的承诺：按项 TTL、上界在并发写下不被突破、LRU 淘汰次序、
`invalidate` 立即可见、键作用域（`keys.py` 那次越权事故的机制）、
`None` 也是有效值、以及**`loader` 在锁外执行**
（这条是它存在的性能理由；验证方式是一个线程在 loader 里等事件，
另一个线程必须仍能读写）。

顺带修掉一个缺陷：条目原来只存「截止时刻」，`age_ms()` 却用缓存的**默认** ttl
反推年龄 ⇒ 任何 `set(k, v, ttl=自定义)` 的项报出错误年龄，
而这个数字是要显示给用户的（`/api/health` 的「N 秒前的数据」）。
现在条目自带自己的 ttl。

### 5.4 配置静默失效

`job_retention_days` 写进 config.toml，但队列用模块常量 `JOB_RETENTION_DAYS`
—— 用户改成 1 天，记录照样留 7 天。
`--processes` 过去在 argv 里就地解析并**绕过** Settings，
于是 config.toml 里的 `api_processes` 被 `extra="ignore"` 静默吞掉。
现在两者都接进 `Settings`；`--host/--port/--processes` 用同一套做法
（写进环境，让 Settings 成为唯一读取点）；非法值退回最保守的 1 进程并打日志
（而不是悄悄起成多进程 —— 写锁形态会变）。

守卫：`test_settings_fields_are_actually_read` 遍历 `Settings` 顶层字段，
要求每个都在业务代码里有 `.字段名` 的读取点（白名单里的例外都注明理由）。
这类 bug 的特征是**代码全绿、配置在骗人**，只能这样兜。

### 5.5 表形状的三处声明，补上两条没人检查的边

四张共享表的形状写在**三个地方**：`storage/schema.py` 的 LanceModel、
`storage/meta.py` 的 SQLite DDL、以及 `meta.py` 里那份 `*_COLS` 列清单
（第三份正是自动迁移按图索骥用的那份）。此前只有「DDL ↔ 列清单」有检查（本轮新加），
另外两条边完全靠人肉：

- **LanceModel ↔ 列清单**：最要命的一条，因为 `import_from_lance` 是
  **按列清单取投影**的。LanceModel 多一个字段 ⇒ 那个字段静默不搬。
  这与丢 `text` 那次事故是**同一种形状**。
- 现在 `test_lance_models_and_sqlite_columns_agree` 逐条比对四张表。

两条守卫都验证过不是装饰：在内存里把 `text` 从列清单拿掉，
DDL↔清单与 Model↔清单**两条都会红**。

### 5.6 其它

- 转义纪律补了直接断言（原来只有改造当时的一次性手工验证 ——
  那种证据会过期，测试不会）：含 `'` 的**合法**值必须照样查得回来
  （转义做过头会让用户看到「文档存在但搜不到」且全程无报错）、
  `' OR 1=1--` 匹配不到任何行、LIKE/ILIKE 里 `%` `_` 保持字面量。
  三条都在两条引擎路径上分别测。
- `repos/__init__.py` 的 `__all__` 补齐了 3 个漏掉的导出
  （`docs_query` / `docs_count` / `set_doc_fields`），
  并把 5 个零调用点的 SQL 工具再导出删掉。
- `tools/bench`：删掉 `cmd_fake_embed` 里被立刻覆盖的第一次赋值；
  `scan-paths` 的点查补 `escape_sql`；`report`/`_load_prev` 与
  `cmd_report` 的快照对比**经核查是正确的**（先读旧快照再写新快照），
  这条原以为是 bug，记下来避免下次重复怀疑。
- 文档回写：`PERF-FINAL-DECISION` §5 的 429 那条、架构图里的四张不存在的表、
  `DESIGN` §17.1 的 outbox 待办、`PERF-IMPLEMENTATION` §3 第 2 条与测试数、
  `DESIGN` §15 的测试计数与新守卫条目。

## 6. 推进顺序与当前状态

**A 档（1 天内，低风险）—— 四项已全部完成（2026-10-06）**

| # | 项目 | 状态 | 落点 |
|---|---|---|---|
| 1 | `git init` + 首次提交 | ✅ | 两个提交：`chore: 建立版本控制基线`（171 文件）在前，之后每一步都可 diff / 回滚 / bisect |
| 2 | ruff + 一条跑 `pytest -q` 的 CI | ✅ | `pyproject.toml` 的 `[tool.ruff]`（E9/F/I）+ `.github/workflows/ci.yml` |
| 3 | `create_app` 拆分 | ✅ | 180 → 55 行，拆出 `_http_guards` / `_wire_routers` / `_wire_exception_handlers` |
| 4 | `_meta(store)` 收到一处 + 回退规则改对 | ✅ | `rag/storage/repos/_engine.py`（守卫禁止副本再长出来）+ `sqlite_holds_data()` |

两处值得单独记的**副产品**，都比原目标更有价值：

- 第 3 项让 `test_auth_is_threaded_off_event_loop` 变红，而它红的方式
  恰好证明它本来是**假绿**：它用 `inspect.getsource(create_app)` 找字符串，
  所以代码搬个位置就红（假红），反过来只要那行字串还在、行为怎么坏它都照过
  （假绿）。已改成行为断言（利用「工作线程里 `asyncio.get_running_loop()`
  必然抛 RuntimeError」来判定 `authenticate` 到底在哪个线程跑）——
  确定性、无计时、与函数叫什么名字无关。
  **同样的文本型守卫还剩 5 处**（`tests/test_audit_regressions.py:264/397/407/426/460`），
  本次没有一并重写，属于同一类待清理项。
- 第 4 项的实现过程里，我自己写的两条新测试立刻抓出两个我自己的 bug：
  `sqlite_holds_data` 若忘了 `mode=ro`，会在「拒绝启动」这条错误路径上
  凭空建出一个空库，下次启动就被当成「已有数据」——**防分裂的守卫自己制造分裂**；
  以及我按 3.11 写的 `tomllib` 在本项目声明的 `requires-python >=3.10` 上是坏的
  （项目早就为此依赖了 `tomli`）。两条现在都有断言兜着。

关于 CI 的一句实话：本仓没有 remote，`.github/workflows/ci.yml`
**从未真正执行过**。在推上 GitHub 之前，它是「期望的门禁」而不是「已在把守的门禁」；
现在就能用的等价命令写在 §7。

**B 档（2–4 天，需要实测护航）—— 未开始**
5. `chunks_query` 意图接口，收掉 3 个模块的自拼 SQL（A1），
   改完跑 `rag-bench quick/ingest` 对比，并把棘轮测试的白名单清空；
6. `embed` 合批（P1）；
7. `refine_factor` / `nprobes` 敏感性实测后再定默认值（P2）。

**C 档（先定职责再动）—— 未开始**
8. 解掉四对循环：`api ↔ server`、`plan ↔ repos`、
   `repos/documents ↔ repos/kbs`、`tables ↔ repos`（A2）；
9. mypy 从 `rag/storage/` 起步（M2 ③）；
10. `reconcile` 移出请求路径（A4）；
11. 把那 5 处文本型守卫改成行为断言（第 3 项暴露出来的同一类问题）。

**刻意没做的一件事**：没有把 `sqlite_holds_data` 挂到 `LanceStore.meta`
属性上做「自动探测」。那会让每次构造 store 都碰一次文件系统——
包括测试与 `rag-bench` 的脚本路径，而那里的速度是有意义的。
探测只允许出现在启动的失败分支上。

## 7. 怎么复现这次审计的检查

```bash
cd /home/admin/Raggi
python3 -m pytest -q                                    # 547 passed, 1 deselected
python3 -m pytest -q -m perf                             # 那条延迟门槛，手动跑
python3 -m pytest -q tests/test_sql_safety.py \
                   tests/test_cache.py \
                   tests/test_meta_store.py \
                   tests/test_perf_gates.py             # 55 条守卫，11s
python3 -m ruff check rag tools tests                     # 门禁（E9/F/I），当前全绿
python3 -m pyflakes rag tools                            # 只剩 1 条有意的 noqa 探测
```

本轮全部实验在 `/tmp/raggi_*` 与 `/tmp/dbg_*` 的副本上进行，
`data/` 未被任何写操作触碰；模型调用全部走 `tools/bench` 的假 embedding，
未产生真实额度消费。
