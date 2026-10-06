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
「双引擎分派」「跨进程写锁」「手写缓存」「配置读取」「并发闸门」「修复路径」这几件事都靠
*约定*而不是*类型或构造*来保证——属性恰好被挂上、调用方记得传对参数、
配置项恰好有人在读、闸门恰好是单例、修复写的恰好是真源那个引擎。
这类东西出错时不会报错，只会**结果悄悄不对**。
五轮下来（B 档、C 档、真实模型轮）在这一个模式上抓出并修掉了 **8 个严重缺陷**
（§5.1 的 4 个 + §2.4 的 `.gitignore` + §5.8 的 3 个：修复计数是空操作、
`has_keys()` 读失败即无鉴权 10 秒、`health()` 把检查失败报成正常），
外加 2 个中等（§5.7a 假闸门、§5.8d `rag-bench` 装不出来），
以及两个「**一句自我声明背后根本没有对应机制**」：
pyproject 声称跑过的双向导入探针从来不存在（§1.3），
`[project.scripts]` 声明的 `rag-bench` 在 wheel 里无处落地（§5.8d）。

B 档与 C 档执行轮各添了一条同类证据，值得单独记住：
- **闸门在默认配置下算出的错数正好等于期望数**（2 workers × 4 shards = 8 = 名义上限），
  所以它永远看不见（§5.7a）；
- **「修复成功」的返回值与「修复生效」是两件事**：`fixed: 1` 写进了
  一个已经不再被读取的引擎，界面数字一动不动（§5.8a）。

**工程实践的缺口比代码问题更该先补，而且已经补上了**：
本仓原先没有 git、没有 lint、没有 CI，也没有类型检查；
A 档四项与 mypy 均已落地，且**每一条门禁都有本地等价物**
（`tests/test_lint_gate.py` 跑 CI 那两条命令原样）。
补上的第二天就见了效——第一次真实 CI 运行就抓出 `.gitignore` 写错导致
**推上去的仓库从干净检出构建不起来**（§2.4），而这个问题在本地永远不可见。
然后 CI 自己也**被本审计的作者弄红过一次**：B5 提交带着两条 ruff 违规上了 main，
本地 551 条测试全绿（pytest 里没人跑 lint）。已补 `tests/test_lint_gate.py`（§5.7b）。

| 维度 | 结论 | 已修 / 已定案 | 仍开放 |
|---|---|---|---|
| 架构专业性 | 骨架正确；四对循环依赖已解，分派已收口 | A1 ✅ A2 ✅ A3 ✅（A4 前提被证伪，顺出 §5.8a） | M3 的长函数、以及「拿不到 meta 时默认静默回退」这条仍待显式化 |
| 可维护性 | 工具链从无到有：git / ruff / CI / **mypy** / 双向导入探针 | 5 项 + 注释漂移新增 4 条已修 | M3 其余 11 个长函数、M5 注释漂移（结构性，无机械手段） |
| 代码规范性 | 一致性高于一般个人项目 | C1 决定不做（有理由）、C2 **原判断不成立**、C3 已按新判据处理 | 无 |
| 性能 | **真实模型轮推翻了本维度的一条结论**（`nprobes` 不是旋钮，是哑配置），并按真实召回改了默认值 | P1 不做、P2 定案后被真实数据修正为「candidate_k 50→100」、P3 进程内那半已修、P6 已记录、P7 新增 | P3 跨进程那半、P4 段错误未定位、P5 的多并发部分、人工标注召回与模型选型 |

测试（下面一律按**收集到的条数**计，避免「passed 数」被 `perf` 标记的移出/移入扰动）：
491（改造前）→ 517（并发改造）→ 544（审计轮 +27）→ 548（A 档执行轮 +4）
→ 555（B 档执行轮 +7）→ **562**（C 档执行轮 +7：行为守卫改写 4 条原地替换不计数、
双向导入探针 1、入口可安装性 1、health 诚实性 2（含 1 条反向对照）、
fail-open 鉴权 2、reconcile 走对引擎 1，减去删掉的文本守卫 1，加 mypy 门槛 1）。
默认套件 **561 passed / 1 deselected**，`ruff check rag tools tests` 与
`python -m mypy` 均全绿（两条都同时挂在 CI 与 pytest 上）。
**但「本机全绿」在 §2.4b 之后被证明不够**：同一份代码在干净环境下是
545 passed / 2 skipped / 1 deselected，而那 2 个 skip 之前是以 404 的形式失败的。

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

### 1.2 A1 · 双 SQL 方言只收口了一半（中等，**已收口**）

**现象**：`chunks` 表没有意图型读接口。
**证据**：3 个模块在 `storage` 之外自己拼 where ——
`rag/api/chunks.py:66-80`（`doc_id`/`kb_id`/`text ILIKE`）、
`rag/retrieval/answer.py:61`、`rag/retrieval/search.py:80-106`。
**影响**：不是「现在出错」，而是**将来 chunks 也要搬引擎时**这三处各自
要理解方言差异，而它们今天没有任何机制提醒。

**当时的缓解**是一条**棘轮**（白名单 = 这 3 个文件，新增第 4 个就红），
因为它不假装债已还清。

**B 档已完成（2026-10-06）**：`repos/chunks.py` 补上 `chunk_filters` /
`chunks_page` / `chunk_prefilter` / `search_chunks` / `doc_ids_by_attr`，
三个模块改为传意图，棘轮随之升级为**零例外的不变量**
（`test_no_sql_text_outside_storage_layer`，工具清单含 `only_cols`）。

搬动过程顺带消灭了三样东西，每一样都是独立的问题：

- `filters["_store"] = store` 这条**隐式通道**：让同一个字典既当
  「用户可传的过滤条件」又当「内部携带的对象」，任何把它原样回显/转发的
  代码都会带出内部状态。现在 `store` 是显式参数。
- `api/chunks.py` 与 `repos/chunks.py` 各写一份**相同的列清单**
  （`CHUNK_LIST_COLS` / `LIST_COLS`）——两份迟早分叉。
- `answer.py` 手抄的一份 `chunk_id IN (...)`：仓储层的 `texts_by_id`
  一直在做同一件事，却**零调用方**。副本的存在恰好掩盖了它——
  这也是为什么「重复实现」和「死代码」经常是同一个 bug 的两面。

**验证口径要说清：没有跑延迟 A/B。** `test_perf_gates.py` 那组结构性门槛
（每请求查询次数、`builder == 0`、`rows <= limit`、分页不重不漏）全过，
这比在 3GB 机器上采一次噪声很大的延迟样本更能说明「没有回归」；
真正的吞吐对比留到 B6（embed 合批）一次做掉，那时本来就要建数据集。
全量套件本轮结束时 **561 passed / 1 deselected**（见 §0 的计数链）。
> 这里原来写的是「547 passed，ruff 全绿」——**那句 ruff 全绿对本节所属的那个提交
> （`f751957`）是不成立的**：干净检出上 `ruff check rag tools tests` 返回 2 条 `I001`。
> 一句话的自评必须能被重跑，否则它就会变成第二条 §2.5 的注释漂移；
> 现在这句话由 `tests/test_lint_gate.py` 代劳（§5.7b）。

### 1.3 A2 · 四对真循环依赖（中等，**已解** —— 2026-10-06 C 档执行轮）

**现象（审计当时的记录）**：全仓约 25 处函数内 `import rag.*`，掩盖着四对真环。
当时写的行号已经漂了，下面是复核后的位置与每对的**方向判断**——
「把 import 换个位置」不算解开，得先说谁该依赖谁。

| 环 | 审计当时写的证据 | 复核结论 | 解法 |
|---|---|---|---|
| A `api ↔ server` | `api/__init__.py:360` ↔ `server.py:34` | 两条边都是**运行时调用**，不是类型依赖 ⇒ 谁在上没有答案 | 把共用的「启动期维护标记」这一小约定（`should_run_startup_maintenance` / `mark_maintenance_done`）移到 `rag/core/maintenance.py`；两条延迟导入都回到模块级 |
| B `plan ↔ repos/kbs` | `plan.py:28` ↔ `kbs.py:118,149` | `clamp_size`/`clamp_ratio`/`overlap_chars` 是**零依赖的纯数值限幅** —— 它没资格决定依赖方向 | 下沉成叶子 `rag/storage/chunk_limits.py`；`plan` **不再二次导出**（两个导入路径就是本仓在防的那种状态） |
| C `repos/documents ↔ repos/kbs` | `documents.py:418` ↔ `kbs.py:11` | 方向只能是 kbs→documents（documents 是更低的仓储） | `update_metadata` 的「目标知识库必须存在」校验改为**调用方注入 `kb_exists`**，不传就抛 `Invalid` —— 忘了传必须当场炸，而不是静默允许悬空 kb_id |
| D `tables ↔ repos/kbs` | `tables.py:195` ↔ `kbs.py:13` | `kbs`/`keys`/`plan` 三处的 `from ..tables import LanceStore` 其实**只出现在注解里**（都有 `from __future__ import annotations`）⇒ 运行时这条边根本不存在 | 归进 `if TYPE_CHECKING`；并验证性地把 `tables.stats()` 里两条 repos 导入提到模块级，双向探针仍全绿 ⇒ 它本来也不是环 |

**顺带抓到两处注释漂移（§2.5 的第 21、22 条）**：

- `tables.py` 那句「局部导入：`writer` 反向依赖本模块，模块级导入会成环」——
  **`writer` 这个模块从来没有存在过**；而 `escape_sql`/`fetch_rows` 的家在 `sql.py`，
  `repos` 只是再导出它们。绕道再导出，才凭空造出那条并不存在的「环」。
- `pyproject.toml` 声称「用 `pkgutil.walk_packages` 正序+逆序各导一遍 ⇒ 0 失败」，
  但**这个探针从来没进过测试套件**（全仓 grep 不到 `walk_packages`）。
  补成 `tests/test_import_order.py` 之后，它自己立刻报出第二个问题：
  `walk_packages` 只返回 40 个模块而磁盘上是 53 个 —— `rag/ingest`、`rag/models`、
  `rag.parsing` 都没有 `__init__.py`（隐式命名空间包），检查器看不见它们，
  于是那句「0 失败」从来没导入过 `pipeline` / `queue` / `splitter`。
  现在按文件系统枚举并断言数量，「枚举本身失效」会红而不是静默变绿。
  **这条改造过程中的每次导入变动都由这个探针双向验证过**（共 4 轮）。

**遗留**：函数内 import 仍有约 60 处，但性质已经查清并分类完毕 ——
第三方可选依赖的懒加载（langchain / pymupdf / docling / lancedb / pandas）、
以及纯粹多余的十几处。**剩下的不是环，也就不再是结构性问题**；
把它们一律提到模块级只会增加一次全仓改动的噪声，没有对应收益（刻意没做）。

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

### 1.5 A4 · 「重扫描型对账仍在请求路径上」——**前提核对后是错的**（改写；真缺陷另有其一）

审计当时写：`/health`、`/stats` 已快照化，但 `reconcile` 仍是同步全量比对，
属于「已声明的余量」。**C 档执行轮去动它之前先核对了调用点，结论是这句不成立**：

| 谁 | 在哪 | 频率 |
|---|---|---|
| `reconcile_doc_counts`（**写**型全量比对） | 只有 `api/system.py` 的 `POST /api/reconcile` —— 设置页那个「修复计数」按钮 | 用户点一次跑一次 |
| `health()` 里的计数比对（**只读**） | `GET /api/health` | 请求路径上，但早已进 3 秒快照 + 任何成功写作废快照 |

所以「把对账移出请求路径」这件事**没有对象可移**：写型对账本来就不在请求路径上，
只读那半本来就已经在快照后面。建议的第 10 项因此作废 —— 记下来是为了避免
下一轮又照着这条去「优化」一个不存在的热点。

但顺着这条线索去读 `reconcile_doc_counts`，读出一个**真缺陷**，
而且形状比原条目严重得多（本轮第 7 个严重问题，见 §5.8a）：它把修复结果
写进了 Lance 旧副本，而 documents 的真源在 S3 之后是 SQLite。
**用户点「修复」，接口回报 `fixed: 1`，真源一个字没改，`/api/health` 依旧 degraded，
全程没有任何异常。**再点一次，又成功一次。

一个「优化建议」被证伪、却顺出它掩盖的功能缺陷 —— 这条是本报告里
「先核对前提再动手」最划算的一次。

## 2. 可维护性

### 2.1 M1 · 没有版本控制（最高优先，**已完成**）

无 `.git`。本轮 20 多个文件的修改**无法 diff review、无法回滚、无法 bisect**。
在一个以「多引擎分派 + 手写并发件 + 跨进程锁」为特征的代码库里，
这是最需要安全网的地方。成本 5 分钟。我没有擅自 `git init`（需要授权）。

### 2.2 M2 · 没有 lint / 类型检查 / CI（中等，**三项全部完成**）

审计当时：`pyproject.toml` 只有 `[tool.pytest.ini_options]` 与打包配置；
无 ruff、无 mypy、无 `.github/workflows`。今天三项都在，且都有本地等价物。

**类型检查为什么在这里值钱，被实测证实了**：原猜测是
「双引擎分派的签名全是 `MetaStore | None`，三起事故全都是 None 走错分支」。
把 mypy 圈到 `rag/storage/` 跑第一轮，出 11 条，**没有一条是纯噪音**：

- 3 条是 `dict[str, int]` 被第一次赋值钉死后，赋 str 全红 ——
  这类推断会把**真的**赋错类型掩盖掉（`meta.stats` / `kbs.update_kb` / `plan.set_kb_plan`）；
- 2 条是 `list_jobs` 里同一个名字 `where` 在两个引擎间含义不同
  （SQLite 的 `WHERE …` 片段 vs Lance 的完整 filter 表达式）⇒ 拆成
  `sql_where` / `lance_where`，方向性错误从此在代码里看得见；
- 1 条 `bool(row) and row.get(...)` 根本无法把 `dict | None` 收窄 ——
  这正是「None 走到不该走的分支」那一类，只是这次被静态抓住了；
- 1 条是 lancedb `Vector(dim)` 的运行时参数化注解，静态检查器判非法，
  改写会破坏建表 ⇒ 全仓唯一一个 `type: ignore`，并写明原因。

**两个刻意不做的事**：不开 `--strict`（第一次就红 200 条的检查器只会被长期
`--ignore`，等于没有 —— 与 ruff 只选 E9/F/I 是同一个决定）；
不假装全仓已覆盖（`files` 就只有 `rag/storage`，其余目录要一个一个搬）。

**接成门禁而不是跑过一次**：CI 里 `python -m mypy`，范围只写在
`[tool.mypy] files` **一处**（命令行再写一遍就会出现「CI 查的目录」与
「本地查的目录」不一致，而那没人看得出来）；本地由
`tests/test_lint_gate.py::test_mypy_gate_matches_ci` 覆盖，并且断言
**被检查的文件数 ≥ 15** —— 否则 `files` 指错目录时 mypy 会输出
`no issues found in 0 source files` 然后绿灯，那是最漂亮的一种假绿。
实测把范围缩到单个文件时输出确实是 `1 source file`，这条断言是有效的。


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

### 2.4b M4b · 测试套件依赖开发者机器上的**未跟踪产物**（严重，已修 —— 由 CI 发现）

**现象**：CI 的 python job 从第一次运行起就失败，而本机 `pytest -q` 全绿。

**定案方法**（值得记下来，因为它不需要任何 CI 凭据）：Actions 的日志与
job summary 都要登录才能读，匿名 API 又受 IP 限流。所以**在本机复刻 CI**：

```bash
python3 -m venv /tmp/civenv                       # 干净解释器
git archive HEAD | tar -x -C /tmp/ci_clean        # 无产物、无本地残留的工作树
/tmp/civenv/bin/python -m pip install -e "/tmp/ci_clean[dev]"   # 只装声明的依赖
cd /tmp/ci_clean && /tmp/civenv/bin/python -m pytest -q -rf
```

一跑就复现，两条用例：

```
tests/test_apikeys.py::test_static_assets_not_protected   assert 404 == 200
tests/test_e2e_flow.py::test_frontend_assets_served       assert 404 == 200
```

**根因**：`web/dist` 是不入库的 Vite 产物。本机永远有 ⇒ 常绿；
干净检出没有 ⇒ `/` 的挂载不存在 ⇒ 404。也就是说**这不是 CI 的问题，
是套件不干净**：它依赖开发者机器上一个未被跟踪的目录。

两处各自的问题不太一样，而且第二处更难看：

- `test_frontend_assets_served` 的 docstring 明写「测试不应强制依赖
  `npm run build`：未构建时跳过产物断言」，**但 skip 判断写在
  `assert client.get("/") == 200` 之后**。于是那句承诺从来没成立过。
- `test_static_assets_not_protected` 的主题是「静态路径不吃鉴权」，
  却把断言写成 `== 200`，等于把「鉴权没拦」和「产物存在」混为一谈。
  改为先断言不变量（不是 401/403），产物在场时才进一步要求 200。

**验证两个方向**：干净环境 42 passed + 1 skipped；本机 43 passed
（产物资源逐个取回的断言仍在跑）。CI 等价环境跑完整套件 545 passed / 2 skipped。

> 这一条也修正本文 §3 之前的一句判断：我曾把「本机全绿」当作状态写进文档。
> 本机全绿只证明**这一台机器**能过；能证明"任何检出都能过"的只有干净环境。

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
`pyflakes rag tools tests` 只剩 2 条报告，且都是有意标了 `noqa: F401` 的
可用性/可导入探测（uvloop、router 模块）——见 §3.3，那里也顺手纠正了
本报告原先写的「只剩 1 条」和「tests/ 有 27 条」这两个没重跑过的数字。

### 3.2 C1 · 80 列违规 39 行，分布在 22 个文件（轻微，**刻意不做**）

没有 formatter 强制执行，所以会持续增长。**决定不动**，理由与当初
把 `line-length` 设成 100 而不是 80 是同一条：现存 39 行超 80、只有 7 行超 100，
按 80 重排会得到一次「全文件挪动」的假改动提交，
那正是 code review 最想避免的噪声，而这批行没有一条影响正确性。
ruff 的 `E9` 只抓语法与未定义名，`E501` 刻意没开 —— 这也是一个**有记录的决定**，
不是漏配。

### 3.3 C2 · 「`tests/` 有 27 条 pyflakes 报告」——**已核，这条不成立**（作废）

C 档执行轮重跑 `python3 -m pyflakes rag tools tests`，实际只有 **2 条**，
且都是**有意**的 `# noqa: F401` 可用性/可导入探测
（`rag/server.py` 的 uvloop、`tests/test_architecture.py` 的 router 可导入）。
`per-file-ignores` 里只有 `tests/conftest.py`，所以不是被配置遮住的。

pyflakes 与 ruff 的差异也只是「pyflakes 不认 `# noqa`，ruff 认」——
即这两条不是漏网的，是**已声明的例外**。审计里那个 27 无从复现，
推测是把 `noqa` 抑制之前的某个状态抄了下来。
**结论：这条不是待办，删掉它；顺带把 §3.1 里「只剩 1 条」改成「2 条」**
（那句同样是抄来的数字，没有重跑过）。

这一条是本报告自己的一次 §2.5 注释漂移：**报告也会漂移**，
所以每条都标了「怎么重跑」（§7）。

### 3.4 C3 · 「23 处 `except Exception` 紧跟 `logger.debug`」（**已处理，并发现了比它更糟的两处**）

全仓实测 95 处宽异常处理器（`logger.debug` 23 / **完全无日志 36** /
`warning` 15 / `error` 2 / 重抛 18）。多数是刻意的且理由写在旁边，
这个取舍是对的。审计当时的建议是「按影响分两级」——执行时把判据收紧成一句：

> **这次失败会不会留下一个持续错误的状态，而用户和排障的人都没有第二个地方能看出来。**

按这句，两处比「debug 看不见」严重得多的问题被翻了出来，都在 §5.8：
`health()` 把「检查没跑成」写成「一切正常」（而且参与 `status` 判定）、
`has_keys()` 把「读不出有没有密钥」写成「没有密钥」⇒ **整个 API 无鉴权 10 秒**。
两者都不是「日志级别选错」，是**默认值选错了方向**。

剩下 6 处按新判据升到 `warning`（标量索引没建成 = 全表扫描、`fts_stale`
清不掉 = 健康检查永远显示待重建、`chunk_count` 回写失败 = 块数永远虚高、
`stored_file` 读失败 = 报「没有留档」而对象还在、任务阶段写失败 = 进度永远卡住、
取消状态读失败 = 用户的取消被丢弃）。

**明确留在 debug 的三类**，理由写进代码而不是留给下一个人重新怀疑：
失败信息已经进了 API 返回值的（`compact_*`）；探测型读取、取不到只影响显示的
（`vector_dim` / `list_indices` / 版本刷新 / 后端容量）；以及底层函数自己
已经 warning 过、再打一条就是重复噪声的（`queue._fail/_cancel/_mark_cancelling`
—— `jobs.set_job` 现在自己带 job_id 与 stage 报警）。

**一个副作用值得记下**：升日志级别时暴露出 `jobs` / `chunk_edit` / `sql`
三个模块**根本没有 logger**。也就是说这些错误路径一旦真走到，
报的不是那条 warning 而是 `NameError` —— 而错误路径平时没有测试覆盖，
所以这个坑可以无限期潜伏。补上日志器之后，这类笔误由 `ruff` 的 F821
（未定义名）代劳，而 F821 已经进了 CI 与本地门槛两条链路。

## 4. 性能

### 4.1 上一轮的收益是实测的（同一台 4 核机、同一份 2000 文档 / 20000 分块、**假 embedding**）

> **读这张表前必须知道**：全部数字都不含模型网络往返。真实模型轮实测
> 一次 query embed 是 86–155ms，`/api/search` 真实 p50 = **123ms**（本表 57.5ms）。
> 所以本表适合当**相对**尺子（谁比谁快多少倍、瓶颈在不在 Python），
> **不适合**当容量规划里的绝对延迟 —— 差 2.1 倍。见 §4.4c 发现 3。

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

### 4.2 P1 · `embed` 合批：前提经实测基本不成立，剩下的那半不值得做（已定案）

原判断（S5 遗留）：「并发入库时每个文档的分片各自打一次 HTTP，合批是入库吞吐
下一个最可期待的提升点」。**这句话是错的**，错在把「一次 HTTP 一批」当成「一次 HTTP 一条」。

实测（假 embedding，进程内计数，不联网不花额度；关键事实已固化成断言，命令见 §7）：

| # | 场景 | embed 请求次数 | 每次条数 | 墙钟 |
|---|---|---|---|---|
| A1 | 入库 15 chunks | 1 | [15] | 20ms |
| A2 | 入库 100 chunks | 2 | [64, 36] | 21ms |
| A3 | 入库 200 chunks | 4 | [64, 64, 64, 8] | 22ms |
| A4 | 入库 1000 chunks | 16 | 15×[64] + [40] | 89ms |
| B | 16 并发**同一**查询（冷缓存） | **16** | 16×[1] | 26ms |
| C | 16 并发同一查询（缓存已热） | 0 | — | 0ms |
| D | 16 并发**各异**查询 | 16 | 16×[1] | 24ms |
| E | **串行** 16 次同一查询 | 1 | [1] | 20ms |

入库路径早就合批了：`pipeline._embed_concurrently` 把整篇 texts 按
`embed.batch`（默认 64）切片，`ThreadPoolExecutor` 并发，`ex.map` 保序
（`rag/ingest/pipeline.py:155-172`）。唯一读 `batch` 的地方就是这一行，
所以「batch 配了没人读」这个怀疑也被否掉了。

真正没实现的只有**查询路径的跨请求 in-flight 合并**。B 与 E 是同一批 16 条
相同查询，只差在并发与否：**串行 1 次请求，并发 16 次**（A/B/C 三行里
`TTLCache` 只缓存**已完成**的结果，不合并正在飞的同 key 请求）。
这是一个可证的 cache stampede —— 但也仅此而已：

- **可测的是重复请求数（16 vs 1），不可测的是真实流量里它发生的频率**。
  合批省的是「同一时刻热点 query 高度重复」那部分，而本机没有任何真实
  流量分布可以支撑这个频率假设（§4.6 第 1 条）。
- **代价是确定的**：S5 设想的队列式合批要引入 10–20ms 等待窗口，
  等于把单次检索 p50 的尾部绑到「最慢的那条同批请求」上，
  并把一个请求的失败半径扩大到整批。为省一个频率未知的重复请求付这个代价，不划算。
- **有一个不引入等待窗口的替代**：in-flight 合并（同 key 只发一次，其余等待同一结果）
  拿到 B→E 的全部收益，且不改变任何一条请求的返回内容。
  真要收益，先做这个，落点在 `Embedder._one_cache` 那一层
  （键已带模型名，见 `embeddings.py:106`）。

**决定：不做队列式合批。** 两个关键事实都固化成了断言，不会随本轮脚本一起蒸发：
合批形状 → `tests/test_queue.py::test_embed_batches_by_configured_size`；
「不合并 in-flight」这个**有记录的现状** →
`tests/test_cache.py::test_embed_one_does_not_merge_inflight_identical_queries`
（它故意断言「会重复调用」，谁实现了合并它会红 —— 那时要连本节的决定一起改，
而不是顺手把断言改小）。

### 4.3 P2 · 检索参数敏感性：已测完，**结论是「不要按延迟调默认值」**（已定案）

工具：`tools/bench/paramsweep.py`（`rag-bench param-sweep`）。
数据集 20000 分块 / dim=1024 / 12 查询 / top_k=10 / 每格 9 次重复；
除延迟外还量**与基准配置的 top-10 重合度** —— 只看延迟会把默认值调到更快但更差。

单机单进程、随机向量（成本真实，排序只是弱代理）。基准 = 现在的默认值
`candidate_k=50 / nprobes=20 / refine_factor=10`。单位是**单次查询毫秒**。

**mode=vector**（基准 p50 ≈ 32–36ms，三次独立基准测量互相差 ±10%）

| 参数 | 值 | 单次 p50 | 相对成本 | top-10 重合 |
|---|---|---|---|---|
| refine_factor | 1 | 14.3ms | 0.40× | **0.275** |
| refine_factor | 2 | 17.0ms | 0.48× | 0.408 |
| refine_factor | 5 | 24.2ms | 0.68× | 0.75 |
| refine_factor | 10 | 34.0ms | 0.95× | 1.0（基准） |
| refine_factor | 20 | 53.9ms | 1.51× | 0.917 |
| candidate_k | 10 | 15.3ms | 0.48× | **0.408** |
| candidate_k | 20 | 20.0ms | 0.63× | 0.708 |
| candidate_k | 100 | 47.9ms | 1.50× | 0.917 |
| nprobes | 4→80 | 31.2–32.5ms | 1.00–1.04× | **全部 1.0** |

**mode=hybrid**（基准 ≈37ms）：`refine_factor=1` → 18.3ms / 0.5× / 重合 0.667；
`=2` → 0.57× / 0.733；`=5` → 0.74× / 0.875；`=20` → 1.43× / 0.95。
`candidate_k=10` → 0.54× / 0.733。nprobes 同样全是 1.0 重合、±16% 噪声。
**mode=fts**（基准 ≈6.4ms）：`candidate_k` 10→100 成本 0.96–1.07×，重合全 1.0
—— 全文通道根本不在这个旋钮上花钱。

四条结论：

1. **`refine_factor` / `candidate_k` 减半延迟的代价是换掉大部分结果集**：
   vector 模式下 `refine_factor=1` 只有 27.5% 的 top-10 与基准相同。
   所以「48ms→22.6ms 减半」这类数字**不能**当作免费性能。
   （§4.1 那对 48/22.6ms 是 `scan-paths` 的另一条路径，绝对值比本表高约 1.3×，
   但**比值 0.47 vs 本表 0.5 一致** —— 结论方向相同，本表为准。）
   → 真实模型轮把这句从「换掉结果集」升级成「**换差**」：`refine_factor=1` 时
   跨主题查询的 recall@10 = 0.61–0.79（§4.4c 发现 2）。
   而本条当时下的「默认值不动」那半句已被推翻：`candidate_k` 已从 50 改成 100。
2. ~~**`nprobes` 在当前索引形态（IvfHnswFlat）上既免费又无效**：4→80 成本不变、
   top-10 重合恒为 1.0。它不是延迟旋钮；能不能当召回旋钮，这份数据回答不了，
   需要真实 embedding + 标注集才能定。~~
   **→ 这条被真实模型轮推翻，而且推翻得比原判断更彻底**：`nprobes` 不是
   「召回能力未知」，而是**根本不作用** —— nprobes=20 与 999 的 recall@10
   逐项相同，而值确实写进了查询构造器（§4.4c 发现 1）。
   这是本报告里最值得留着的一处**错**：随机向量给出过一个看起来证据很强的结论
   （「4→80 重合恒 1.0」），真数据给出的却是「这个配置项是哑的」。
   **代理指标的可信度上限，就是它那批数据的上限** —— 而这一点只有在换成
   能产生真答案的数据集之后才暴露出来。
3. **`refine_factor=20` 是被支配的**：多付 50% 延迟，结果还与基准不一致（0.917）。
   没有任何理由超过 10。反方向更关键：降到 1–2 只省 5–9ms，
   却让 recall@10 塌 30 个百分点（§4.4c）—— 所以 10 这个值现在有质量证据了。
4. **fts 通道的参数不敏感**（6.4ms，改 candidate_k 无变化）——
   之前把「检索慢」归因到候选量上，对 hybrid/vector 成立，对 fts 不成立。

顺带记一条**方法论**：这个工具的第一版把「12 个查询一批计时」的总毫秒当成
单次 p50 打印出来（382ms vs 真实 32ms，12× 误读），是 `_timed` 没有除回批量条数。
已在工具里修掉（`divide=len(qv)`，键名改成 `p50_per_query_ms`，报表显式标单位）。
一个单位标错的测量工具比没有工具更糟——它会让人拿着 12 倍的差去做决策。

### 4.4 P3 · 跨进程的全局闸仍不存在（轻微，已声明；进程内那条已修）

`ScaledSemaphore` 的多进程总并发 = 进程数 × 配置值，这条边界仍然成立，
真要做全局闸需要跨进程信号量，属于「多机」议题，现在做只会增加故障面。

但**进程内那一半原本是假的**：见 §5.7（本轮抓到并修掉的第 6 个隐式协议缺陷）。

### 4.4b P6 · `_distance` 靠 Lance 的「自动投影」才在（中等，**已记录并已被现有断言守住**）

`SEARCH_COLS` 是显式投影列表，里面**没有**分数列，但 `retrieval/search.py:_score_of`
读的就是 `_distance` / `_score` / `_relevance_score`。它们之所以在行 dict 里，
是因为 Lance 的 scoring autoprojection 在补 —— 而 scanner **每次查询**都在为此打一条
Rust Deprecation 警告：「目前会自动补，将来不补了，请调
`disable_scoring_autoprojection` 采纳新行为」。一次 20000 分块的参数扫描里
这类警告有 3436 行（≈1.7 条/查询）。

实测了这个依赖断掉的形状（`_score_of` 的兜底是 `return 0.0, "none"`）：

| 行内容 | `_score_of` 返回 |
|---|---|
| `{"_distance": 0.2}` | `(0.9, "cosine_similarity")` |
| `{}` | `(0.0, "none")` |

也就是：真到那一天，vector 检索**不报错**，而是每条命中都拿 0. 分 ——
`score_kind` 变 `none`、`score_threshold` 把所有结果过滤光。标准 §4 开头那句话的形状。

两点决定：

1. **不调那个开关**。消除警告的代价是把一条隐式依赖变成静默退化，不值。
2. **不加新测试**：这条依赖已经有行为断言在守 ——
   `tests/test_search_api.py::test_score_kind_matches_mode` 逐模式要求
   vector→`cosine_similarity`、fts→`bm25`、hybrid→`rrf|bm25`，
   哪一条分数列消失，对应模式立刻红；`test_score_threshold_filters`
   则在全 0 分时因「阈值未生效」红。升级 lance 时会先撞上它们，而不是线上。
   真正缺的是**说明**，已补在 `rag/storage/repos/chunks.py:159-171`
   （原来那句「分数列由查询自己附加」恰好把这条依赖说成了无关巧合，
   属于 §2.5 的注释漂移）。


### 4.4c P7 · **真实模型测量轮**：`nprobes` 是个无效配置，而检索延迟的大头根本不在引擎（已定案）

前四轮的 embedding 全是假的（不联网、不花额度）。这一轮用户提供了
硅基流动的凭据，于是 §6 那条「卡在没有真实数据上」的清单终于能测完。
**测出来的第一条就是本报告此前一个结论是错的**，先把这条放在最前面。

**怎么测的（方法决定了结论的边界）**
用 `BAAI/bge-m3`（1024 维，正好与仓库默认 `dim` 一致）真实向量化了一份
2000 篇 × 5 块 = **10000 块**的中文技术语料，索引形态与生产完全一致
（IvfHnswFlat / COSINE / max_connections 20 / construction_ef 300）。
ground truth 用 **numpy 穷举余弦 kNN** —— 所以这里的 `recall@10` 是
「ANN 有没有找回穷举排序的前 10 名」，**不是**「人类觉得哪 10 段最相关」。
向量分布真实，文本是模板生成的（6 个主题的受控句子 + 受控随机数填槽）。

查询分两类，这个区分是关键：**单主题查询**（贴近真实流量）与
**跨主题混合查询**（把 k 个不同块的向量相加再归一）。后者的真近邻天然散在
多个 IVF 分区里 —— 只有这种负载才能分辨 `nprobes`，第一版实验恰恰因为
没做这个区分而差点给出假结论。

#### 发现 1：`nprobes` 在 lancedb 0.39 + IvfHnswFlat 上**完全无效**

| 查询构成 | recall@10（nprobes=20） | recall@10（nprobes=999） |
|---|---|---|
| 1 块（单主题） | 1.0 | 1.0 |
| 2 块混合 | 0.9125 | 0.9125 |
| 4 块混合 | 0.9583 | 0.9583 |
| 8 块混合 | 0.9167 | 0.9167 |

四类查询逐项**完全相同**，延迟也一样（23.2 vs 23.3ms）。另外单独扫过
`nprobes ∈ {1,2,5,20,50,100,200,1000}`：recall 一位小数都不变。
而构造器层面值**确实传下去了** —— `builder.__dict__` 里
`_minimum_nprobes/_maximum_nprobes` 从 `None` 变成了设定的数，
但扫描计划不认它（`chunks.py:300` 那条 `.nprobes(nprobes)` 写得没毛病）。
索引也确实在：`list_indices` 里 `vector_idx / IvfHnswFlat`，
`num_indexed_rows=10000, num_unindexed_rows=0`。

⇒ **推翻 §4.3 的结论第 2 条**。那一版说「nprobes 既不花钱也不改结果 ⇒
它不是延迟旋钮；能不能换召回这份数据答不了」。真实数据的答案更强：
**它既不是延迟旋钮，也不是召回旋钮，它在当前驱动形态下根本不作用** ——
这是一个「配置项被读取、被传递、然后被引擎忽略」的哑旋钮，
和 §5.4 那两个「写进 config.toml 却没人读」的配置属于同一类，只是更隐蔽：
这一条**有人读、有人传**，看上去一切正常。

已落地的处置：`RetrieveConfig.nprobes` 的注释直接写明无效（防止下一个人
拿它调召回）；字段与传参**保留**，因为升级后可能自动生效。

#### 发现 2：真正决定召回的是 `candidate_k × refine_factor`，默认值据此改了

| 配置 | recall@10（2/4/8 块混合） | 引擎 p50 |
|---|---|---|
| `limit=50 rf=1` | 0.61 / 0.79 / 0.71 | ~19ms |
| `limit=100 rf=1` | 0.61 / 0.79 / 0.71 | ~19–24ms |
| **旧默认 `50×10`** | **0.9125 / 0.9583 / 0.9167** | ~23ms |
| **新默认 `100×10`** | **1.0 / 1.0 / 1.0** | ~35–43ms |

单主题查询（真实流量的主体）在所有配置下都已经是 1.0 —— 所以这不是
「线上一直在丢结果」，而是**跨主题/聚合式查询在默默少召回**：
旧默认每 10 条正确结果丢 0.4–0.9 条。

⇒ **默认值改动：`candidate_k` 50 → 100**，代价是引擎 p50 +12ms。
放在端到端里看这个代价很小（见发现 3：一次检索要 86–155ms 的 query embed），
买到的是「召回不再取决于查询碰巧落在几个簇里」。
`refine_factor` 保持 10：把它降到 1–2 只省 5–9ms，却让 recall 塌 30 个百分点。

**必须同时记下的限制**：已有 `data/config.toml` 里 pin 了 `candidate_k = 50`，
所以这次代码默认值的改动**不会自动作用到老安装**（配置文件优先）。
我没有写 `data/`（本轮全部实验落在 `/tmp/raggi_real/`）；要让老安装生效，
需要把那一行改成 100 或删掉该行。**这条本身就是 §5.4 那类问题的镜像**：
一个默认值改动在已有部署上等于没改，而界面不会有任何提示。

#### 发现 3：真实模型下，检索延迟的大头**不是引擎**，是 query embed

| 项 | 实测 |
|---|---|
| 单条 query embed（真网络往返，冷） | **86–155ms** |
| 整次 `/api/search`（hybrid，含引擎+返回层） | **p50 123ms**（min 30ms = 缓存命中，max 1576ms） |
| 同一查询命中 `embed_one` 缓存后 | **~32ms** |
| 再打开 `bge-reranker-v2-m3` 精排 | **p50 563ms**（+440ms） |
| 真实批量向量化吞吐（batch=64 / conc=4） | **409 texts/s**（10000 块 / 24.5s） |
| 批次经济学 | 1 条/请求 = **86ms/条**；64 条/请求 = **3.7ms/条**（省 96%） |

三条直接后果：

1. **§4.1 那张表里 `/api/search` 57.5ms 是「不含模型网络的下界」**，
   现在有了真实数：123ms。假 embedding 的基准在**读路径**上是可用的相对尺子，
   但在绝对延迟上会误导容量规划 —— 差了近 2.1 倍。
2. **`embed_one` 缓存是检索路径上性价比最高的组件**（123ms → 32ms），
   而不是任何引擎参数。这让 §4.2 那条「不做队列式合批」的决定更有底气：
   查询向量根本无法合批（一次检索一个问题），能省的只有重复查询。
   同时给 in-flight 合并标好了价：每个并发重复查询省一整次 86–155ms 往返。
3. **精排的真实代价是 440ms**，比整条检索其余部分加起来还贵 ——
   `rerank.enabled` 默认 False 是对的，若要用它，
   UI 与超时预算都得按「一次搜索半秒」来设计。
   另注意 max 1576ms 那个尾巴：真实 provider 的抖动是这台机器上
   检索尾延迟的主要来源，`timeout`（默认 60s）与熔断的价值因此是实打实的。

**花费与复现**：本轮 API 消耗约 32 万 token（10000 块 + 30 条查询 + 少量探测），
rerank 24 次调用。脚本在 `/tmp/raggi_real/`（`build_real_dataset.py` /
`recall_tool.py` / `probe_embed.py`），都不含凭据 —— key 只从
`/tmp/raggi_real/.env` 读，**没有写进仓库任何文件**（包括 `data/config.toml`）。
复现命令见 §7。



全套件跑时出现一次 `timeout: 被监视的命令已核心转储`（87 个扩展模块的崩溃报告），
重跑即过。怀疑是 pyarrow/lance 在这台 3GB 内存机器上的边界，
**但没有定位到原因**，所以这条只作为事实记录。
要做长跑或 CI，先把并行度压下来并把内存上限纳入监控。

### 4.5 P4 · 观察到一次 native 段错误（事实记录，未定位）

全套件跑时出现一次 `timeout: 被监视的命令已核心转储`（87 个扩展模块的崩溃报告），
重跑即过。怀疑是 pyarrow/lance 在这台 3GB 内存机器上的边界，
**但没有定位到原因**，所以这条只作为事实记录。
要做长跑或 CI，先把并行度压下来并把内存上限纳入监控。

### 4.6 P5 · 未测的量（诚实清单）

- ~~真实模型服务下的端到端并发与延迟**全部未测**~~ → **已测**（§4.4c）：
  query embed 86–155ms、`/api/search` p50 123ms（max 1576ms，provider 抖动主导尾延迟）、
  开精排 563ms、真实批量向量化 409 texts/s。
  **仍未测**的是「多并发下」的真实模型表现（本轮是单并发串行测的，
  并发一上来 provider 侧限流与本地闸门都会参与，那是另一次测量）；
- `MetaStore` 连接按线程隔离 ⇒ 默认 executor 8 线程 = 每进程 8 个 SQLite 连接，
  4 进程 = 32 连接 + WAL/SHM。文件描述符数量与 `-wal` 增长未实测；
- `s3` 后端只以 mock 单测验证，没对真实 RustFS/MinIO 跑过；
- 多机部署不在范围内（`filelock` 边界 1 写了这条）。

## 5. 已修（四轮累计：**8 个严重** + 3 个中等 + 一批中轻，全部有对应测试）

> 中等三处 = §5.7a 假闸门、§5.8d 声明的入口装不出来、§5.8e 声明的探针不存在。
> 后两条同属一类：**配置/文档里声明了某个机制，而这个机制从来没有实现过**——
> 它们不像功能缺陷那样会被用户碰到，但它们让「已经验证过」这句话变成假的。

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

### 5.7 B 档执行轮：闸门是假的、CI 被自己人弄红、以及两条决定（2026-10-06）

**(a) embedding 并发闸形同虚设（§4.4 的第 6 个隐式协议缺陷，已修）**

`rag/ingest/pipeline.py:125-129` 用五行注释解释了这道闸门为什么必须存在：
「并发任务数 × 每个任务内的分片会相乘（默认 2×4=8），无上限时远端会限流，
而 `_is_retryable` 认 429 ⇒ 每个分片各自退避重试 3 次 ⇒ 级联重试风暴」。
`_embed_gate()` 的 docstring 也写着「进程级」。

**但它每次调用新建一把。** `gate = _embed_gate(cfg)` 在 `_embed_concurrently`
函数体内，作用域是「一个文档」。实测（假 embedding，每请求睡 50ms，数最大在途）：

| docs | chunks/doc | concurrency | 修复前峰值在途 | 修复后 | 应得上限 |
|---|---|---|---|---|---|
| 1 | 200 | 4 | 4 | 4 | 8 |
| 4 | 200 | 4 | **16** | 8 | 8 |
| 8 | 200 | 4 | **32** | 8 | 8 |

修复前的峰值随文档数**线性增长** —— 也就是那个乘积一点没被封顶，
注释描述的事故机制完好地保留着。默认配置下它恰好看不出来：
`ingest_workers=2` × `concurrency=4` = 8 = 期望上限 2×4 = 8，**两个错数正好相等**。
只要 `ingest_workers` 调到 3 以上（或 `rag serve --processes N`，每个进程一套队列）
闸门就消失，而这正是 S4 多进程形态落地后的使用方式。

修法：进程级按 permits 缓存 + 双检锁（两个文档同时起步只能有一个实例进字典）。
守卫三条（`tests/test_queue.py`）：闸门实例跨调用复用、
4 文档 × 4 分片峰值 ≤ permits、以及一条**变异检查** ——
把 `_embed_gate` 退回「每次新建」后峰值必须 > permits。
第三条是必要的：它证明前一条测的是闸门，而不是恰好线程数少。
`peak > 1` 也单独断言了，否则「串行也算过」会让上界断言变成空话。

顺带在 docstring 里补了一条本来就没做到的边界：**这道闸只覆盖入库路径**，
`api/models.py:94` 的连通性探针直连 `embedder.embed`，不排队。

**(b) `main` 被我自己弄红了一次（流程缺陷，已补本地门槛）**

B5 那个提交（`f751957`）把两条 `ruff I001` 推上了 main，CI 当场红，
而**本地 551 条测试全绿** —— pytest 里没有任何一条跑过 ruff，
所以「CI 是门槛」只在推上去之后才成立。那两条 I001 正是本轮源码手术留下的残迹
（AST 删函数留下的空行、手工插进去的 import 顺序）。

补了 `tests/test_lint_gate.py`：把 CI 那条 `ruff check rag tools tests`
原样在本地跑一遍（ruff 不在就 skip）。验证过它会红：临时插一个未排序 import
→ `FAILED`。**门槛重复一份不贵（0.09s），漏一次的代价是一次红色的 main。**

**(c) 两条「不做」的决定**

§4.2（不做队列式 embed 合批）与 §4.3（不按延迟调 `refine_factor`/`nprobes` 默认值）
都是**测完之后的否证**，不是跳过。写清楚理由、把可复现的判据留在 §7，
是为了下次不必再花一轮去怀疑同一件事。

**(d) CI 里有一条会随机红的测试，已经改成不赌时序**

本轮为修闸门跑全量套件，4 次全量跑里 `test_stage_progress_observed` **红了一次**
（`seen == ['done']`：第一次轮询任务就已经终态），而它单跑 5/5 绿。
它靠「桩睡 0.12s vs 每 20ms 轮询」制造观测窗口 —— 那是一次赛跑，
负载稍高就输。这类红的结局不是「发现问题」，而是**门禁被忽略**，
所以必须处理而不是标注为已知 flake。

改法：`_GatedEmbedder` 把 provider 卡在一个事件上，测试在「provider 确实还卡着」
的时候去读阶段，然后放行。窗口由事件构造，与机器快慢无关。
顺带把断言从「非终态就行」收紧成「阶段必须是 `embed`」——
前者在「整条流水线只在结束时写一次任务行」的回归下依然会绿
（它会看到创建时的 `queued`），等于没测进度。
本文件里 `_VerySlowEmbedder` 的注释早就写下过同一条教训（不要用绝对墙钟阈值），
这条测试是同一个坑的第二份证据。


### 5.8 C 档执行轮：三个新的严重缺陷，都藏在「被审计说错的那条」旁边（2026-10-06）

**(a) `POST /api/reconcile` 在默认配置下是个会回报成功的空操作（严重）**

§1.5 那条「对账还在请求路径上」被核对为**不成立**之后，顺着调用点读进函数本体，
读出了这个：它写 `store.documents.update(...)` —— **Lance 旧副本**，
而 documents 的真源在 S3 之后是 SQLite，读又一律走引擎分派的 `docs_query`。

```
用户点「修复计数」 → 接口返回 fixed: 1
                 → 真源里的 999 一个字没改
                 → /api/health 的 count_mismatch 一条没少
                 → 再点一次，又成功一次
```

没有任何异常。这是本仓第 **7** 个「隐式协议」事故，也是第一个发生在
**用户主动点击的修复动作**上的 —— 修复路径自己不可信，比功能坏更难发现。
测试 `test_reconcile_fixes_the_engine_that_holds_the_truth` 先失败（`assert 999 != 999`）
后通过，证明它抓的是这个。改法是回到既有的两条纪律：写走 `set_doc_fields`
（它的存在理由就是「只有一个地方知道两个方言怎么写」，这处是唯一点绕行者），
实际计数与 health 共用 `chunk_counts_by_doc`（顺带 23.2ms → 10.7ms，20000 分块实测）。

**(b) `has_keys()` 读失败时返回 False ⇒ 整个 API 无鉴权 10 秒（严重）**

`auth.py` 有两处把 `has_keys() == False` 当**放行**条件（无凭据直通、
零密钥时对 ADMIN_PREFIXES 开放），而这个值会被 `TTLCache` 缓存 10 秒。
所以一次瞬时读失败的后果不是「某次请求慢」，而是 **10 秒的全体放行**。
它自己上方的注释就写着「数错了不是性能问题而是**安全**问题」，
错误分支却写着放行值。现在：失败抛 `ApiKeysUnavailable` → `authenticate` 映射 503，
异常不进缓存（测过第二次仍然真去查）。非放行用途的 `_file_url`
按「已启用鉴权」签名 —— 那个方向的误判不会造成死链。

**(c) `health()` 把「检查没跑成」报成「一切正常」（严重）**

`except Exception: fts_stale = 0` 与 `except Exception: embed_model_mismatch = False`，
后者还直接参与 `status` 计算。检查失败被翻译成两个肯定回答 + 一行 debug 日志。
现在未知进 `checks_failed`、计数用 -1 而不是 0（0 会被界面渲染成绿色徽章）、
**跑不成就不许说 ok**；前端与 apidoc 同步区分「查出问题」和「无法确认」。

**(d) `rag-bench` 从来装不出来（中等）**

`[project.scripts]` 声明了 `rag-bench = "tools.bench.__main__:main"`，
而 `packages.find` 只收 `rag*` ⇒ wheel 里没有 `tools/`。
实测干净 venv 装 wheel 后 `ModuleNotFoundError: No module named 'tools'`。
`pip install -e .` 恰好能跑（源码目录本身在 sys.path 上），所以这条**只在真装一次时现形**
—— 与 §2.4 的 `.gitignore` 是同一课，也是 §7 里那条「本机全绿 ≠ 装得起来」的第三次验证。

**(e) 那条「双向导入探针」从来没存在过（中等）**

见 §1.3。`pkgutil.walk_packages` 看不见三个命名空间包，
意味着 pyproject 里那句「0 失败」从来没检查过 `pipeline`/`queue`/`splitter`。

**(f) 四对循环依赖已解、mypy 已进门禁、文本型守卫已清零** —— 细节在 §1.3 / §2.2 / §5.7，
这轮另加一条方法论：`inspect.getsource` 型守卫的**危害被本轮做成了可重跑的证据**
（把 `score_kind` 退回取首行，旧守卫不红、新断言红），而不只是一句劝告。

**(g) 自伤记录两条**（写在这里是因为它们都是「新写的测试第一次跑就抓到」的变体）：
一条测试里写了 `finally: del auth_mod.apikeys.has_keys` —— `del` 没有「恢复原值」语义，
把函数从模块里删掉，一次带走 16 条测试；另一条是 health 的两条测试夹具不自洽
（`stated != actual`），于是 `degraded` 是从 `count_mismatch` 来的，
证明不了它声称要证明的那条路径 —— 断言当时是绿的，但**测的是别的东西**。


## 6. 推进顺序与当前状态

**A 档（1 天内，低风险）—— 四项已全部完成（2026-10-06）**

| # | 项目 | 状态 | 落点 |
|---|---|---|---|
| 1 | `git init` + 首次提交 | ✅ | 两个提交：`chore: 建立版本控制基线`（171 文件）在前，之后每一步都可 diff / 回滚 / bisect |
| 2 | ruff + 一条跑 `pytest -q` 的 CI | ✅ | `pyproject.toml` 的 `[tool.ruff]`（E9/F/I）+ `.github/workflows/ci.yml`；B5 弄红之后又补了 `tests/test_lint_gate.py`，让本地 `pytest -q` 覆盖 CI 那条命令（§5.7b） |
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

关于 CI 的三句实话（这一节改过三次，每次都是因为它自己说得过头了）：

- 本仓现在有 remote（`git@github.com:Ailoc/raggi.git`），CI **真的在跑**。
  它已经在把守，不是「期望的门禁」。
- **但它被弄红过一次，是这次审计的作者自己弄的**：B5 提交 `f751957` 带着两条
  `ruff I001` 上了 main（本地 551 条测试全绿，因为 pytest 里没有 lint）。
  现在有两道独立证据：干净检出（`git archive HEAD`）上跑门禁命令返回 2 errors；
  Actions 侧 `f751957` 的 check-run 是 **python=failure / frontend=success**
  —— 红在 lint 那一步而不是测试，与推断完全一致。
  修好之后 `61835d6` 两条 check-run 都是 **success**。
- 查 CI 状态用 `commits/<sha>/check-runs` 这**一个**端点：本机没有 gh CLI，
  匿名 API 限流 60 次/小时（本轮实测剩 14），而日志与 job summary 仍要登录。
  修法见 §5.7(b)，本地等价命令写在 §7。

**B 档（2–4 天，需要实测护航）—— 三项都有了结论**
5. ✅ **已完成** `chunks_*` 意图接口，收掉 3 个模块的自拼 SQL（A1），
   棘轮白名单已清空并升级为零例外不变量；延迟 A/B 未跑，理由见 §1.2。
6. ✅ **测完，决定不做**（P1）：前提「每个分片各打一条 HTTP」在入库路径上是错的，
   那里早就按 `embed.batch` 合批；剩下查询路径的 in-flight 合并，
   收益频率无法在本机测得、而队列式合批的代价（等待窗口 + 失败半径）是确定的。
   数据与判定见 §4.2。**调查过程里抓到同形的真缺陷**（§5.7a）。
7. ✅ **测完，决定不改默认值**（P2）：`tools/bench/paramsweep.py` +
   `rag-bench param-sweep`，成本与 top-k 重合度一起量。结论是
   「按延迟调 `refine_factor`/`nprobes` 会把默认值调到更快但更差」——
   vector 模式下 `refine_factor=1` 只有 27.5% 的 top-10 与基准相同。
   数据、四条结论与那条「单位坑」见 §4.3。

**C 档（先定职责再动）—— 四项全部完成（2026-10-06 C 档执行轮）**

| # | 项目 | 状态 | 落点 / 结论 |
|---|---|---|---|
| 8 | 解掉四对循环依赖（A2） | ✅ | 每对都先定方向再动：标记函数下沉 `core/maintenance.py`、限幅下沉 `storage/chunk_limits.py`、kb 存在性校验改调用方注入、三处注解级 `LanceStore` 归 `TYPE_CHECKING`。由新建的双向导入探针在两种次序下逐一验过 |
| 9 | mypy 从 `rag/storage/` 起步（M2 ③） | ✅ | 首轮 11 条无一纯噪音；CI + 本地门槛各一份，并断言「被检查文件数 ≥ 15」防 `files` 指错目录那种假绿。**不开 strict** |
| 10 | `reconcile` 移出请求路径（A4） | ⚠️ **前提被证伪** | 它本来就只在手工端点上；顺线读函数却读出 §5.8a 那个「修复计数是空操作」的严重缺陷。条目作废，缺陷已修 |
| 11 | 5 处 `inspect.getsource` 守卫改行为断言 | ✅ | 全仓 tests/ 里已无活动 `inspect` 断言（只剩注释里的历史说明）。并第一次把「文本守卫是假绿工厂」做成可重跑证据：把 `score_kind` 退回取首行，**旧守卫不红、新断言红** |

**这一轮另外补上的（不在原清单里，是顺着 C 档挖出来的）**：
§5.8b `has_keys()` 的 fail-open 鉴权窗口、§5.8c `health()` 把检查失败报成正常、
§5.8d `rag-bench` 从来装不出来、§5.8e 「双向导入探针」这句话背后从来没有探针、
§3.4 六处「吞掉且无痕」升 `warning` 并暴露三个模块根本没有 logger。

**真实模型轮（2026-10-06，用户提供了硅基流动凭据）—— 原「卡在没有真实数据上」三项全部有答案**
12′. `nprobes` 能不能换召回 → **它根本不作用**（nprobes=20 与 999 的 recall@10 逐项相同，
     值确实进了查询构造器）。原 §4.3 结论第 2 条据此推翻。
13′. in-flight 合并值不值得做 → 已标价：每个并发重复查询省一整次 86–155ms 往返；
     真实查询向量的成本是**缓存命中 32ms / 未命中 123ms**，所以优先级最高的仍然是
     已有的 `embed_one` 缓存，而不是合并。合并仍是可选项（§4.2 的决定不变，但依据更硬）。
14′. 真实模型下的端到端并发 → **部分完成**（单并发的绝对延迟已测，多并发仍未测）。
     另外默认值据真实召回改了：`candidate_k` 50 → 100（跨主题查询 recall@10 0.91→1.0，
     引擎 +12ms）；`data/config.toml` 里 pin 的 50 需要手工改，我没有写 `data/`。

**仍然未测的（真实模型轮没覆盖到的部分）**
15. **多并发下的真实模型表现**。本轮的 123ms / 563ms 都是单并发串行测的。
    并发一上来，provider 侧限流与本地那道进程级闸门（§5.7a 刚修好的那个）会一起参与，
    `429 → 每个分片各自退避重试 3 次` 这个级联路径**至今没有被真实压过**。
    这是当前最值得做的一次测量，因为它同时检验闸门、熔断与合批三件事。
16. **人工标注的相关性**。§4.4c 的 recall 是「相对穷举 kNN」，
    它只能证明 ANN 找回了向量排序的前 10 名，**不能**证明那些块对用户有用。
    要回答后者需要一份人工标注（哪怕 30 条查询 × 相关性分级）。
17. **embedding 模型选型**。bge-m3 与 Qwen3-Embedding-0.6B 在同一份语料上的
    召回/精度差别没测过；这决定的是检索质量上限，比任何引擎参数都重要。
    （服务上四个 embedding 模型都可用，成本是一次重建数据集。）

**刻意没做的一件事**：没有把 `sqlite_holds_data` 挂到 `LanceStore.meta`
属性上做「自动探测」。那会让每次构造 store 都碰一次文件系统——
包括测试与 `rag-bench` 的脚本路径，而那里的速度是有意义的。
探测只允许出现在启动的失败分支上。

## 7. 怎么复现这次审计的检查

```bash
cd /home/admin/Raggi
python3 -m pytest -q                                    # 561 passed, 1 deselected
python3 -m pytest -q -m perf                             # 那条延迟门槛，手动跑
# 干净环境复现（等价于 CI；本机全绿不代表这里全绿，见 §2.4b）：
#   python3 -m venv /tmp/civenv && git archive HEAD | tar -x -C /tmp/ci_clean
#   /tmp/civenv/bin/python -m pip install -e "/tmp/ci_clean[dev]"
#   (cd /tmp/ci_clean && /tmp/civenv/bin/python -m pytest -q -rf)
python3 -m pytest -q tests/test_sql_safety.py \
                   tests/test_cache.py \
                   tests/test_meta_store.py \
                   tests/test_perf_gates.py             # 55 条守卫，11s
python3 -m ruff check rag tools tests                     # 门禁（E9/F/I），当前全绿
python3 -m pyflakes rag tools tests                      # 只剩 2 条有意的 noqa 探测（见 §3.3）
python3 -m mypy                                        # 类型门禁，范围由 pyproject 决定
```

B 档两项的复现（都不碰 `data/`，都不打真实模型）：

```bash
# §4.3 检索参数敏感性：先造 2000 文档 / 20000 分块的临时数据集到 /tmp/ps/data
#（python3 -m tools.bench gen --data /tmp/ps/data --docs 2000 --chunks-per 10），然后
python3 -m tools.bench param-sweep --data /tmp/ps/data \
       --queries 12 --repeats 9 --out /tmp/ps/sweep.json
#   报表单位是「单次查询毫秒」；重合度=与基准配置的 top-10 交集比例。
#   注意：跑之前确认这台 4 核机没有别的负载（load 3.0 时低延迟端会漂 ±35%）。

# ===== 真实模型轮（需要凭据；key 只放 /tmp，绝不进仓库）=====
# 1) 建真实向量数据集（bge-m3，2000 篇 × 5 块）
set -a && . /tmp/raggi_real/.env && set +a
python3 /tmp/raggi_real/build_real_dataset.py      # 约 32 万 token、24.5s
# 2) 召回 vs 参数：ground truth = numpy 穷举精确 kNN
python3 /tmp/raggi_real/run_recall.py              # 见 §4.4c 的两张表
# 3) nprobes 是否作用：把 nprobes 从 20 拉到 999，逐项比对 recall 与 p50
#    （结论「不作用」的判别式：两者完全相同，而 builder.__dict__ 里
#      _minimum_nprobes/_maximum_nprobes 确实变了 —— 两条都要看才不算蒙对）
# 4) 真实端到端延迟与批次经济学
python3 /tmp/raggi_real/probe_embed.py             # 86ms/条 → 3.7ms/条（batch 64）

# §5.7a 闸门失效的复现已固化成测试，直接看三条：
python3 -m pytest -q tests/test_queue.py -k gate -v
#   test_embed_gate_is_reused_within_the_process   —— 单例
#   test_concurrent_ingests_share_one_gate         —— 峰值在途 <= permits
#   test_gate_actually_throttles_when_shared       —— 退回 per-call 必须红
# §4.2 的合批形状与「不合并 in-flight」两条现状也固化成了测试：
python3 -m pytest -q tests/test_queue.py::test_embed_batches_by_configured_size \
                   tests/test_cache.py::test_embed_one_does_not_merge_inflight_identical_queries

# ===== C 档执行轮 =====
python3 -m mypy                                    # 范围写在 [tool.mypy] files，别在命令行重复
python3 -m pytest -q tests/test_import_order.py -v # 双向导入探针 + 入口可安装性
# 三条新严重缺陷各自的复现（都是「先看它红，再看它绿」）：
python3 -m pytest -q \
  tests/test_meta_store.py::test_reconcile_fixes_the_engine_that_holds_the_truth \
  "tests/test_audit_regressions.py::test_unreadable_auth_config_is_503_not_anonymous_pass_through" \
  "tests/test_audit_regressions.py::test_failed_health_checks_are_reported_as_unknown_not_ok"

# §5.8d「声明的入口装不出来」这类问题只有真装一次才看得见。三步，全在 /tmp：
python3 -m pip wheel . --no-deps --no-build-isolation -w /tmp/wh
python3 -m venv --system-site-packages /tmp/instvenv
/tmp/instvenv/bin/python -m pip install --no-deps /tmp/wh/raggi-*.whl \
  && /tmp/instvenv/bin/rag-bench --help
#   修之前这一步直接 ModuleNotFoundError: No module named 'tools'。
#   同一条思路也适用于 §2.4 的 .gitignore 与 §2.4b 的未跟踪产物。
```

本轮全部实验在 `/tmp/raggi_*`、`/tmp/dbg_*`、`/tmp/ps`、`/tmp/wh`、`/tmp/instvenv*`
的副本或临时目录上进行，`data/` 未被任何写操作触碰；
模型调用全部走假 embedding（进程内计数或 `tools/bench`），未产生真实额度消费。
