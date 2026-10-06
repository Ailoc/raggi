# Raggi 前端交互重写方案

> 日期：2026-10-04 ｜ 范围：`web/src/` 全量（8339 行）+ `tests/test_frontend_wiring.py` 守卫
> 方法：逐文件静态审查 + 交叉验证（本文所有 file:line 均为当次实测，非推测）
> 基线：`pytest tests/test_frontend_wiring.py` **13 passed**；`svelte-check` **0 errors / 0 warnings**；`web/dist` 与源码同步
> 配套：`docs/DESIGN.md` §11、`docs/AUDIT-2026-10-02.md`、`docs/DESIGN-AUDIT-2026-10-03.md`

---

## 0. 结论先行

**该重写的是表现层，不该重写的是数据层。** `web/src/` 里真正承载"界面"的是四块：`App.svelte`(150) + `views/`(3000) + `ui/`(700) + `styles/app.css`(1177)，约 5000 行。其余 1400 行 `lib/` + `types.ts`(383) + `data/apidoc.ts`(1124) 与视图无关，可原样沿用——逐个模块核对后，**没有一个需要推倒重写**，只有 4 处需要小改（见 §4）。

当前前端的病根不是"不够好看"，而是三件结构性的事，它们决定了"改 UI 布局"在今天为什么代价极高：

1. **布局系统实际上已经失效**。三条关键 CSS 是死代码：超宽屏限宽、窄屏隐藏表格列、分块行选中样式（§1.1）。也就是说现在既没有宽度约束，也没有真正的响应式体系——界面"土"不是审美问题，是骨架没人管。
2. **刷新模型把「loading 态」和「数据变化」绑成了一个变量**。`loading` 只有真/假，于是每次轮询都等于"整页重进加载态"：过滤框被卸载吞掉焦点（§1.3.1）、重解析后状态永不更新（§1.3.2）、检索参数与结果集脱节（§1.3.3）。这类问题无法靠调样式解决。
3. **没有可复用的组件层**，同一原语在 4~6 个视图各写一遍：`fmtTime` 有 **6 份实现**、骨架屏 CSS **4 份**、对话框焦点陷阱 **2 份**、多选逻辑 **2 份逐字重复**、5 张表格 **3 种容器组合**（§1.4）。所以"改一下布局"的真实成本是改 9 处且必然漂移。

**正确的切法**：新建「外壳 + 布局系统 + 原语组件」三层，视图只声明内容而不声明布局；把加载语义拆成 `initial / refreshing / error` 三态；把 ops 层的展示副作用改为注入。这样"焕然一新"是一次改壳 + 逐视图迁移，而不是改一处崩九处。

**必须先解决的元问题**：`tests/test_frontend_wiring.py` 的 13 条守卫里有 **5 条会直接卡住新布局**（它们把类名、文案、文件名写死在断言里）。重写前要先决定每条是「保留约束」「改名换义」还是「连理由一起更新」——见 §2。

### 0.1 已锁定方向（2026-10-04 决定）

| 决策 | 选定 | 由此产生的两个硬后果 |
|---|---|---|
| **D1 导航** | **常驻双栏工作台**（左侧对象树 + 右侧多标签内容区） | ① `test_rail_css_and_tokens_removed` 必然冲突——它按字符串禁 `.rail`/`--rail-w`/`--rail-handle-w`。**不能只靠改名过关**：要把它替换成真正锁住"折叠可自救"的等价断言（§2.2、§3.6.3）。② 对象树需要新的数据策略：后端没有树接口，靠 `/api/kbs` + `/api/documents?kb_id=&q=&limit≤500` 懒加载，5000 篇文档时**树不能成为唯一导航**（§3.6.2）。 |
| **D2 视觉基调** | **换一套基调**（冷中性 + 单一强调色、无边框分层卡片） | ① 现在的主色 `--amber` **同时承载两种语义**：品牌/主操作 与 **检索命中强调**。换新基调若仍用单一强调色，高亮会与主按钮、链接同色 → 必须先把它拆成 `--accent` 与 `--hit` 两个令牌，再改色（§3.6.4，涉及 57 处使用点）。② 去掉边框要靠层级阴影撑，但今天**全站只有 1 个阴影令牌**（`--shadow-pop`，深浅各一份定义）而 `border: 1px solid` 有 **26 处** + `border-color: var(...)` **17 处** → 必须先建 elevation 刻度，否则无边框 = 没有层级（§3.6.5）。 |
| **D3 节奏** | **分阶段推进（§5：0、1、1.5、2–7 共 9 步）**（未表态，取本方案推荐：可归因优先） | 阶段 1 就把「外壳 + dock + 新令牌 + elevation 刻度」建完，阶段 1.5 单独做配色迁移，旧视图塞进新壳即产生可见变化，再逐批迁移（§5）。 |


---

## 1. 现状体检（证据）

### 1.1 骨架层：三条决定布局的 CSS 从未生效

| 位置 | 规则 | 事实 |
|---|---|---|
| `app.css:236-237` | `.view{display:none}` / `.view.active{display:block}` | 全仓库**没有任何组件产出 `class="view"`**（grep 确认 0 处）。这是 tsc 直出时代的残留。 |
| `app.css:240-242` | `@media(min-width:1400px){.view.active{max-width:1360px;margin-inline:auto}}` | 依赖上一条 → **超宽屏限宽居中从未生效**。DESIGN.md 描述的"限制内容宽度避免长文本无限拉伸"与实际不符，现在是全出血。 |
| `app.css:1153-1154,1175` | `table.docs [data-col="engine"\|"time"\|"chunks"]{display:none}` | 没有任何视图写 `data-col` → **窄屏隐藏次要列的规则是死的**。实际行为只剩 `app.css:547` 的 `table.docs{min-width:800px}`：横向滚动，而 KbDocs 每行 6 个操作按钮被推出屏幕。 |
| `app.css:887-891` | `tr[data-chunk]{cursor:pointer}` / `.is-selected` | DocView 用的是 `class="chunk-row"`（`DocView.svelte:342`）并在本地 `:456-457` 重新定义 → 全局那份是死的。 |
| `App.svelte:104` | `<div class="crumbbar-actions">` | 永远是空的固定槽位。 |

**结论**：不能假设"现在的界面是按设计长成的"。重写第一步是把布局骨架当作一等公民重建，并让 DESIGN.md 与代码重新对齐。

### 1.2 只有 4 种临时布局，没有网格体系

九张视图归拢后只有四种形状，且各自用 flex/grid 硬凑：

- **A** 页头 + 卡片网格（KbList）
- **B** 页头 + tabs + 表格（Jobs、Settings，以及 KbDocs/KbChunks/KeysPanel 的表格内核）
- **C** 页头 + 双栏 split（DocView、Search、AnswerPanel）
- **D** 页头 + `.stack` 套 `.panel`（KbSettings、Settings、ApiDoc）

没有共享网格：`app.css` 里 **7 处**各自声明 `grid-template-columns`（`:450` auto-fill 280、`:747` auto-fit 180、`:789` 1fr/1fr、`:862` 1.15fr/1fr、`:914` auto-fit 320、`:922` auto-fit 250、`:361` repeat(2)），其余全是 flex。唯一一致的只有间距令牌 `--u1…--u9`（`app.css:12-13`）。

**两处直接导致视觉错乱**：

1. **DocView 有两套 `.docsplit/.pane/.pane-head/.pane-body`**：全局 `app.css:860-885` 与 scoped `DocView.svelte:418-437`。scoped 的 `.pane-body{min-height:320px}`（`:437`）压掉了全局的 `flex:1;min-height:0;overflow:auto` → 出现嵌套滚动容器。断点也打架：scoped `1024px`（`:424`）vs 全局 `1100px`（`app.css:1139-1144`），**1024–1100px 区间内同一屏幕有两套答案**。
2. **Toast 叠罗汉**：`app.css:975-977` 给 `.toast` 定了 `position:fixed; bottom/inset-inline-end:24px; z-index:60`，而 `Toaster.svelte:23` 的 scoped `.toast` 重定义了外观却**不含 position**。Svelte 的 scoped 类只提高特异性、不覆盖未声明属性 → 每条 toast 各自钉死在同一坐标互相压住，外层 `.toaster` 的 `flex-direction:column;gap` 形同不存在。多条提示同时出现时只能看到最后一条。

另外 `UploadPanel / KeysPanel / StateBlock / Icon` **没有任何 @media**：dropzone（`app.css:633-649`，padding 36/24）、`.filelist` 行、`.qbar` 的 `<kbd>`、以及 5 按钮的 `.page-head-actions` 都只在宽屏成立。

### 1.3 刷新模型是交互病灶的根源（重写重点）

#### 1.3.1 轮询吞掉焦点（KbDocs）

`KbDocs.svelte:51` 的 `load()` 第一行是无条件 `loading = true`；`:102-104` 的轮询回调 `pollTimer = setTimeout(() => void load(), 2500)` 走的正是同一个 `load()`。而过滤工具栏被 `:274` 的 `{#if !loading && items.length > 0}` 门控。

结果：**有任务在跑时，每 2.5 秒整表被 4 行骨架替换一次，工具栏连同输入框被卸载重建**。用户正在敲的过滤词会丢失焦点（`filter` 状态还在，但光标没了、下拉被打断）。对比 Jobs 用 `load(true)` 做静默刷新（`Jobs.svelte:36`）——同一个仓库里两种刷新语义。

#### 1.3.2 重解析后状态永不更新（DocView，不可达状态）

`docops.ts:93,110` 用 `?wait=false` 提交 reparse/resplit（异步，秒回），而 `DocView.svelte:113-119` 在调用后只 `await load()` **一次**——此时流水线才刚启动。DocView 全文没有任何 `setTimeout/setInterval/refreshJobs`（grep 确认）。

结果：页面停在"仍在处理"的旧状态，**唯一出路是离开页面再回来**。这是设计文档 §11.2 明确要防的"用户不知道该等还是该刷新"。

#### 1.3.3 检索参数与结果集脱节（Search）

`Search.svelte:66` 的 `setParam()` 只做 `go(withParam(query, key, value))` 改 hash；触发重跑的 `$effect` 只依赖 `q`（`:54-58`，而 `q` 是 `$derived(query.get("q"))`，`:17`）。改 `top_k` / `mode` / `score_threshold` / `group_by_doc` 等 **12 个 `onchange={setParam}` 控件**（`:273,278,284,290,314,319,325,333,348,353,375,382`）会更新 URL、更新参数面板显示值，但**不会重新检索**。

结果：屏幕上同时存在"参数面板显示新值"和"结果集来自旧参数"，且没有任何"待重新检索"提示。比无脑重跑更糟——它在说谎。（`.resultbar` 用的 `res.mode` 是服务端回显，于是元信息行和参数下拉还会互相矛盾。）

#### 1.3.4 其余刷新/竞态问题

| 位置 | 问题 |
|---|---|
| `KbSettings.svelte:111-113` | 整页被 `{#if error}…{:else if kb && plan}` 包住，**没有 loading 分支** → 拉取期间整屏空白（其它视图都有骨架屏）。 |
| `KbSettings.svelte:63` | `saveBase()` 成功后重新 `load()` → 用户在"分块方案"面板里尚未提交的编辑被静默抹掉。两个保存按钮（`:148`、`:189`）无脏值跟踪、无"未保存"标记。 |
| `DocView.svelte:194,320` | 虚拟化视口高度硬编码 `VIEWPORT_H = 560` 并写成 inline style，而同栏高度实际由 CSS（`--topbar-h`/`calc(100vh-…)`）决定 → 两者迟早不一致。 |
| `Jobs.svelte:65` | 每 2 秒为了拿标题全量拉 `/api/v1/documents?limit=500`；`:24` `limit=100` 是硬顶且**没有 `.listmore`**，而 `:85-93` 的 tab 计数是"当前页计数"却显示得像全局计数。 |
| `Jobs.svelte:46,65` | 前缀用 `/api/v1/*`，全站其余用 `/api/*`（后端双挂载，`rag/api/__init__.py:217-218`）→ 两套事实。 |
| `App.svelte:33` + `KbList.svelte:22` | `/api/kbs` 启动时被拉两次。 |
| `KbDocs.svelte:203-207` | 过滤在**客户端**对截断后的 200 条做 substring；后端 `documents.py:279-286` 早已支持 `q / status / parser_engine / mime / offset`，前端只传了 `kb_id + limit`（`:56`）。`total=5000` 时提示"没有匹配的文档"是假的。 |

### 1.4 没有组件层：同一原语的多份实现（已 grep 定位）

| 原语 | 份数与位置 | 差异 |
|---|---|---|
| `fmtTime` | **6 份**：`KbList:39`、`KbDocs:226`、`KbSettings:104`、`Jobs:103`、`Settings:106`、`KeysPanel:128` | 两种风格并存：`slice(0,16).replace("T"," ")` 与本地化格式；空值占位分别是 `""` 与 `"—"` → 同站两种时间写法。 |
| 骨架屏 `.rows/.row/.skeleton/@keyframes pulse` | **4 份**：`KbList:112-114`、`KbDocs:445-447`、`Jobs:278-280`、`Search:503-507` | 各自遮蔽全局 `.skeleton`（`app.css:968-973`），且**动画都不是同一个**：全局用 `shimmer`，4 份本地各写一个 `pulse`；`KbList` 还额外定高 132px。 |
| `.tabs/.tab` | **2 份**：`Jobs:245-259`（`<button>` 版）、`Settings:378-384`（`<a>` 版） | 同一视觉两种语义、两种键盘行为。 |
| 对话框 scrim/panel + `@keyframes fade,pop` + **焦点陷阱 Tab/Escape 遍历** | **2 份**：`ConfirmDialog:24-43,81-117`、`FormDialog:73-92,157-193` | 近似复制，只差 `min(460px)` vs `min(560px)/max-height:86vh`。 |
| 多选（`SvelteSet` + `selAllBox` + `indeterminate` effect + allPicked/somePicked/pickedCount/toggleAll） | **2 份逐字重复**：`KbDocs:40-41,210-224`、`DocView:32-33,147-162` | — |
| `table.docs` 表格 | **5 处、3 种容器**：`.table-wrap>.table-scroll>table`（`KbDocs:333-335`、`KbChunks:110-112`、`Jobs:176-178`）；只有 `.table-scroll` + inline max-height（`DocView:320-322`）；只有 `.table-scroll` 无 wrap（`KeysPanel:163-164`） | 横向滚动与阴影边界三种表现。 |
| `.listmore`（"已显示 N / 共 M" + 再加载） | **3 份**：`KbDocs:409`、`KbChunks:155`、`DocView:403`；Jobs 完全没有 | `KbChunks:155-162` 嵌在两层 `{#if}` 里且缩进错位。 |
| `.statline/.toolbar/.bulkbar/.inflight/.qbar/.effective` | **各只出现在 1 个视图** | 说明这些不是"体系"，是一次性补丁。 |

`dialog.svelte.ts:33` 与 `form.svelte.ts:54` 是"同时只允许一个"的单例：第二个打开请求会**静默把第一个 resolve 成 false**。视图多了以后这会变成难查的丢操作。

### 1.5 信息架构：位置指示与操作入口错位

- **删除知识库有 3 个入口、2 套确认路径**：`KbList:92-105`（卡片脚）、`KbDocs:248`（页头）、`KbSettings:217-227`（危险区）。同一破坏性动作，用户学到的是"随便哪儿都能删，但弹窗长得不一样"。
- **KB 级动作离对象最远**：入库/删除/设置/独立分块全在页头（`KbDocs:235-250`，5 个按钮），其中 3 个是 `<a>` 伪装成 `.btn`。
- **下钻 4 层**：库列表 → 文档列表 → 文档详情 → 分块，靠 `backlink`（`KbChunks:72`、`DocView`）+ 面包屑来回搬。独立分块（`#/kb/{id}/standalone`）复用同一个 `KbChunks` 组件（`App.svelte:115-117`），是"两个路由一个视图"的特例。
- **检索结果没有深链**：命中只能进右栏检视器（`Search:454-489`），**永远跳不到文档详情页**，也无法把"某条命中"分享/收藏出去。
- **ApiDoc 自相矛盾**：接口索引 `:173-187` 渲染全部 `GROUPS` 而忽略 `filter`（`:35-43` 只过滤 `shown`）→ 搜一个词，索引仍列全部端点；过滤词不进 URL → 结果不可分享；`jump()` 靠 `setTimeout(…,50)` 滚动 + `classList.add("is-flash")`（`:91-100`）→ 键盘焦点丢失。
- **路由按位置索引取段**：`DocView.svelte:20-21` 用 `parts[1]/parts[3]`。加一级路径就得改一堆视图。

### 1.6 与设计约定不一致的小项

`DESIGN.md` §11 写明"图标统一来自 SVG 精灵，**不再混用 emoji 或 unicode 字符**"。实际 `Search.svelte:423` 用 `<span aria-hidden="true">✕</span>` 当关闭图标（全仓库唯一一处，grep 确认）。重写时统一走 `Icon`。

### 1.7 后端已有、前端没用上的能力（新交互的空间）

| 能力 | 位置 | 现状 |
|---|---|---|
| 文档列表服务端过滤 `q/status/parser_engine/mime` + `offset` 分页 | `documents.py:279-286` | 只用了 `kb_id+limit`，过滤在截断页里客户端做（`KbDocs:56,203`） |
| `PATCH /chunks` 批量编辑 `{edits:[…]}` | `chunks.py:154` | 前端未用，批量是逐条发 |
| `GET /stats`、`GET /versions`、`POST /rollback` | API 总表 §10 | **完全没有 UI**。而版本回滚是单机库最重要的自救手段 |
| `POST /embed` | API 总表 | 未用 |
| `POST /search/report` | `Search.svelte:170-175` | 绕过 `api()` 裸 `fetch`，不享受统一错误与鉴权提示 |

---

## 2. 硬约束：13 条守卫逐条处置

`tests/test_frontend_wiring.py`（13 passed）不是普通测试，是**事故复盘的固化**。重写要么满足它，要么连理由一起改它——不能靠改名绕过。

### 2.1 会直接卡住新布局的 5 条

| 守卫 | 断言（写死的东西） | 重写的处置选择 |
|---|---|---|
| `test_focus_outline_not_removed` | 除了不许 `outline:none`，还**白名单写死了 `.compose:focus` 与 `.qbar input` 两个选择器，并断言它们仍在源码里**（`:70-83`） | 若新设计换掉"入库面板 `.compose`"或"检索栏 `.qbar`"命名/组件，**测试直接红**。要么保留这两个类名，要么同步更新白名单并写明新的等效替代。 |
| `test_doc_page_has_back_link` | 断言 `App.svelte` 含 `class="crumbbar"` + `crumbs.items`；`KbDocs.svelte` 含 `setCrumbs(` + **字面量 `"知识库", href: "#/"`** | **D1 选双栏工作台 → 这条必然冲突**（位置指示改由 dock 的选中态承担，`KbDocs` 也并入工作区标签）。处置：**保留 crumbbar 但降级为"内容区内部层级"指示**（库内多标签时仍有价值），并把守卫改写成等价断言："任意层级的当前对象都能在壳层被读出"。禁止简单删除这条守卫——它防的是各页自拼面包屑导致的层级漂移。 |
| `test_rail_css_and_tokens_removed` | `app.css` 里不得出现 `.rail`、`--rail-w`、`--rail-handle-w` | **D1 选双栏工作台 → 这条必然冲突。注意它的真实理由**（docstring）：旧侧栏折叠后把切换入口一起藏了，且 `localStorage` 跨刷新保留 → 用户无法自救的死锁。它禁的不是"侧栏"，是**不可恢复的状态**。处置：新组件命名 `.dock`/`--dock-w`（不是钻字符串空子，而是它确实是个规则不同的新组件），**同时新增 §2.2 的可自救守卫**。旧守卫改名后自然通过，因此**保留它**——它继续挡住 `.rail` 死令牌回潮。只改名不加新守卫等于绕过了上一场事故的复盘。 |
| `test_kb_card_actions_clickable` | 要求 `KbList.svelte` 含 `kbcard-acts`，`app.css` 含 `.kbcard-foot` **且 `z-index: 1`** | 卡片重设计必须保留这两个类名（含 z-index），或改写该守卫。理由要留住：`::after{inset:0}` 覆盖层画在非定位元素之上，点"删除"会命中链接。 |
| `test_build_output_when_present_is_fresh` | 源码 mtime 不得晚于 `web/dist/index.html` | 每阶段收尾必须 `npm run build`。 |

### 重写必须继续满足的 8 条（不挡路，但要照做）

`test_source_tree_exists` / `test_index_html_loads_svelte_entry`（保留 `#app` 与 `/src/main.ts`）；`test_views_use_native_controls`（views 里禁 `role="button"`——曾经的 `<article role="button">` 整卡导航弄丢了中键新开、右键复制链接、键盘行为）；`test_labels_are_bound_to_controls`（views 里每个 input/select/textarea 都要 `aria-label` 或 label 配对）；`test_tables_have_action_column_header`（`<th class="actions">` 内要有 `sr-only`）；`test_error_states_offer_action`（任何 `kind="error"` 后 320 字符内要有 `action=` 与 `onaction=`）；`test_no_native_confirm_or_alert_in_views`；`test_no_innerhtml_dom_queries`（当前 0 处，已验证）。

> ⚠️ `test_labels_are_bound_to_controls` 与 `test_error_states_offer_action` **只扫 `views/*.svelte`**。把表单控件或错误态搬进 `ui/` 组件会让检查失效——那是漏洞，不是解法。新原语组件应自带 aria 要求，别靠"移出 views"过关。

### 2.2 新增守卫：常驻 dock 必须可自救（D1 的验收条件）

双栏工作台把"上一代侧栏"复活了，所以要把那次事故变成一条**机器能查**的规则，而不是靠自觉。建议在 `tests/test_frontend_wiring.py` 新增：

```python
def test_dock_collapse_is_self_recoverable():
    """常驻导航的折叠态必须能自救。

    回归防护（第一代侧栏）：折叠后切换入口被一起隐藏，localStorage 又让
    折叠态跨刷新保留 → 用户重新打开浏览器仍然是折叠的，且没有地方可点。
    """
    app = (SRC / "App.svelte").read_text(encoding="utf-8")
    dock = (SRC / "ui" / "Dock.svelte").read_text(encoding="utf-8")

    # 1) 折叠开关必须在 dock 容器之外——否则折叠时把它一起藏了
    assert re.search(r'class="dock\b', app), "dock 未由壳层渲染"
    toggle_at = app.find("dock-toggle")
    dock_close = app.rfind("</Dock>")
    assert 0 < toggle_at < dock_close, "折叠开关必须在 <Dock> 之外，折叠后仍可见"

    # 2) 不得持久化折叠态；只允许持久化宽度
    persisted = re.findall(r'localStorage\.setItem\(\s*"[^"]*"', app + dock)
    assert not any("collaps" in s.lower() or "dockopen" in s.lower() for s in persisted), \
        "折叠态不得跨刷新保留（这正是第一代死锁的成因）"

    # 3) 宽度持久化必须 clamp，防止拖出可视区后找不回
    assert re.search(r"Math\.min\(|clamp\(", dock), "dock 宽度必须 clamp 在视口范围内"

    # 4) 折叠态必须仍可键盘到达全部导航
    assert "aria-expanded" in app and "aria-controls" in app, "折叠开关缺少展开状态播报"
    assert "reset" in (app + dock).lower(), "缺少一键恢复默认布局的入口"
```

这四条就是 §3.6.3 的折叠契约的可执行版本；实现时先写测试再写 dock，避免事后补。

另外 `tests/test_apidoc.py` 双向锁端点漂移，`data/apidoc.ts`(1124 行) 是它的产物；重写 ApiDoc 视图时**不要动这份数据**，只动渲染。

---

## 3. 目标设计

### 3.1 决策记录（原备选取舍，供追溯；结论见 §0.1）

| 决策 | 选定 | 未选项及其代价 |
|---|---|---|
| D1 导航 | **B 常驻双栏工作台**（左对象树 + 右多标签内容区） | A 工作区标签：改动面最小（只动 1 条守卫）、零死锁风险，但观感变化有限 → 放弃。C 命令面板驱动：对 ApiDoc/Settings 这类长表单页不合适，新用户没有可看的结构 → 放弃。**B 的代价**：2 条守卫要正面处理（§2）、对象树数据策略要新设计、折叠契约要立成守卫（§2.2）。 |
| D2 视觉基调 | **B 换一套基调**（冷中性 + 单一强调色 + 无边框分层卡片） | A 系统化延续：能保住琥珀资产，但变化只来自密度与层级 → 放弃。C 双密度：可作 B 落地后的增量。**B 的代价**：`--amber` 语义拆分 57 处（§3.6.4）+ elevation 刻度从零建立（§3.6.5）。现有 OKLCH 令牌体系、27 个 SVG 图标精灵、首帧主题脚本**继续继承**，只换色相与承载方式。 |
| D3 节奏 | **分阶段**（§5：0、1、1.5、2–7 共 9 步） | 一次性替换 9 视图：会让"界面崩"与"逻辑崩"混在一起不可归因，且 §1.3 的加载语义与守卫要同步改 → 放弃。 |

### 3.2 布局系统（重建骨架）

```
┌───────────────────────────────────────────────────────────────────┐
│ 顶栏 brand │ 全局命令/检索入口 │ 队列摘要 │ 主题 │ ▤ dock 折叠开关 │ 52px
├──────────────┬────────────────────────────────────────────────────┤
│ .dock        │ 标签条 [文档] [分块] [设置] [原文对照]  ×  │ + 页动作 │ 44px
│ ┌──────────┐ ├────────────────────────────────────────────────────┤
│ │section 切换│ │                                                    │
│ │ 库/检索/  │ │  <main class="content">                            │
│ │ 任务/设置/ │ │   单层滚动 · 12 栏网格 · max-width 1440             │
│ │ API       │ │   min-width: 0                                     │
│ ├──────────┤ │                                                    │
│ │上下文对象树 │ │                                                    │
│ │ 库 → 文档  │ │                                                    │
│ └──────────┘ │                                                    │
│ 240↔320px 可拖 │                                                    │
└──────────────┴────────────────────────────────────────────────────┘
.shell { display: grid; grid-template-columns: var(--dock-w) minmax(0, 1fr); }
```

要点：

1. **外壳是一层 CSS Grid，dock 宽度是唯一变量**：`grid-template-columns: var(--dock-w) minmax(0,1fr)`。`--dock-w` 由拖拽把手写入并 clamp 在 `240–min(320px, 38vw)`；**注意命名用 `--dock-w`/`.dock` 而非 `--rail-w`/`.rail`**（§2.2）。
2. **一条真正生效的宽度约束**：`--content-max: 1440px` 挂在 `.content` 上（替代今天失效的 `.view.active`，`app.css:236-242`）。DESIGN.md 与代码重新对齐。
3. **滚动链路只有一层**：`body > #app > .shell > .content`，杜绝 §1.2 的 DocView 嵌套滚动（`DocView.svelte:437` 的 `min-height:320px` 覆盖掉全局 `overflow:auto` 那一类问题不再可能重现，因为容器由 `<SplitView>` 提供而非视图自写）。
4. **列显隐真正落地**：表格列声明 `data-col`，`@media` 规则据此隐藏次要列（engine/time/chunks）——把今天死掉的 `app.css:1153,1175` 变成活的，替代"横向滚动把操作按钮推走"。
5. **原语组件层（新建 `web/src/ui/primitives/`，一次实现全站复用）**：
   `<PageHeader>` `<Section>` `<DataTable>`（列定义 + 行操作槽 + 空/错/骨架三态 + 内置 `.listmore` + 可选多选）`<SkeletonRows>` `<Tabs>`（统一 `<button>` 版）`<Toolbar>`（过滤/批量/选择计数）`<SplitView>`（双栏 + 单一断点 + 可选拖拽比例）`<StatLine>` `<DialogBase>`（焦点陷阱/scrim/动画一份，Confirm 与 Form 共用）、以及新的 `<Dock>` `<ObjectTree>` `<SectionSwitcher>`。
6. **格式化单一来源**：新建 `lib/format.ts` 收敛 6 份 `fmtTime`（+ bytes/duration/相对时间），语义占位统一 `"—"`。
7. **修 Toast 定位冲突**：`.toaster` 负责 `position:fixed`，`.toast` 只负责外观（`app.css:975-977` vs `Toaster.svelte:23`）。
8. **动效**：沿用 `lib/motion.ts` 的 `prefers-reduced-motion` 归零（`motion.ts:15-16` 只用 `matchMedia`，视图无关）；新组件**不得**再各自写一遍 `@media (prefers-reduced-motion)`（今天有 8 份）。
9. **不引入任何运行时第三方 UI 库**——与项目"零运行时依赖、构建只在开发机"的原则冲突（DESIGN §11）。

### 3.3 信息架构：dock 承担 section 切换，对象树随 section 变

**关键设计**：dock 不是"知识库列表"，而是 **section-aware 的上下文栏**。顶栏那 5 项全局导航（`App.svelte:47-53`）降级为 dock 顶部的 section 切换器，于是每个 section 都获得自己的第二栏——这才是双栏真正的价值，而不是只给知识库加侧栏：

| section | dock 里放什么 | 数据从哪来 |
|---|---|---|
| 知识库 | 库树（库 → 该库文档，懒加载）+ 当前库的标签入口 | `/api/kbs`（已含 `doc_count`/`chunk_count`）；展开时 `/api/documents?kb_id=&q=&limit≤500` |
| 检索 | 历史查询 + 常用范围（库/模式）+ 参数摘要 | 后端无历史接口 → 浏览器侧存储（明确标注为"本机历史"，不伪装成服务端数据） |
| 任务 | 运行中/排队中任务前 N 条 + 阶段脉冲 | `/api/jobs?limit=`（已有；改造成静默轮询） |
| 设置 | 模型 / 密钥 / 数据与版本 三项 | 静态 |
| API | **端点分组目录（跟随过滤）** | `data/apidoc.ts` 的 `GROUPS`——顺带根治 §1.5 的"索引忽略过滤词" |

路由随之扁平化（下钻 4 层 → 2 层）：

```
#/{kbId}/docs                工作区标签：文档（入库面板内联、服务端过滤、行操作、批量）
#/{kbId}/chunks              全库分块（含独立块，服务端 only_standalone=true）
#/{kbId}/settings            基本信息 / 分块方案 / 危险操作（唯一删除入口，3→1）
#/{kbId}/doc/{docId}         原文对照（SplitView，?chunk= 支持深链到某块）
#/search?q=…                 检索 + 问答（dock 仍显示库树，可边看结果边换库）
#/jobs                       任务全量页（dock 摘要是它的子集，不重复实现）
#/settings/{models|keys|data}  data 为新增：/stats /versions /rollback
#/apidoc                     接口文档（索引进 dock）
#/                           库总览：卡片大盘 + 新建/导入（dock 已能切换，故此页退化为"管理入口"）
```

路由层配套：`router.svelte.ts` 的 name switch（`:18-33`）改成 **section + pattern 表**，输出命名字段 `{section, kbId, docId, tab}`，干掉 `parts[1]/parts[3]` 位置索引（`DocView.svelte:20-21`）；`nav`（`App.svelte:47-53`）与 `activeNav` 的三元折叠（`:55-60`）由同一张 section 表派生，不再手写折叠逻辑。

**`#/` 是否还要留卡片页**：留，但职责收窄为"跨库总览与新建/导入"。因为 dock 树按库组织，而库的元信息（描述、方案徽标、分块数）在卡片上更易读；同时 §2 的 `test_kb_card_actions_clickable` 仍要求 `kbcard-acts`/`.kbcard-foot{z-index:1}` 这两个类名（§1.1 的覆盖层事故），保留卡片可少改一条守卫。

### 3.4 交互模型（逐页，每条都对应 §1 的证据）

**① 加载语义拆三态（全局，收益最大的一条）**

```ts
state: "initial" | "ready"     // 首屏才显示骨架
status: "idle" | "refreshing"  // 后台刷新：工具栏/输入框/滚动位置都不动
error: string                  // 保留上一次的行 + 顶部 inline 重试条
```

- 修 §1.3.1：轮询走 `refreshing`，骨架只出现在首屏 → 过滤框不再被卸载。
- `App.svelte:33` 与 `KbList:22` 的双拉取合并成一个库列表 store，其余视图订阅。

**② 列表：服务端过滤 + 真分页 + 批量**

- 过滤框 debounce 300ms 打 `?q=`；状态/引擎/MIME 做成 chips（后端 `documents.py:279-282` 全支持）；分页用 `offset`。
- 截断诚实化：`已显示 200 / 共 5000`，过滤词命中 0 条时说明"在服务端范围内无匹配"而不是"没有匹配的文档"。
- 行操作从 6 个平铺按钮（`KbDocs:388-401`）改为「主操作内联 + 次要进 `<RowMenu>`」，仍是原生 `<button>`（守 §2 的 native controls）。
- 批量删除/启停走已有的 `DELETE /documents/batch`、`PATCH /chunks/batch-enabled`，多选逻辑收进 `<DataTable>`。

**③ 任务：从"一个页面"变成"贯穿全局的进度层"**

- 壳层常驻队列摘要（running/queued 计数 + 脉冲），点开抽屉看阶段流 `queued→parse/fetch→load→split→embed→write→done` 与**完整失败原因**（今天 `err-text` 被 `max-width:22ch` 截断，`Jobs:271-275`）。
- 处理中的文档在行内显示细进度条 + 停止；入库任务用后端已回写的 `doc_id`（DESIGN-AUDIT §9.2）直接链到文档。
- 轮询改造：Jobs 不再每 2s 拉 500 篇文档取标题（`Jobs.svelte:65`），改成按 `kb_id` 过滤 + 仅对缺标题的批量补拉；`limit=100` 硬顶换成真分页；tab 计数与"当前页计数"分开标注。

**④ 分块 / 原文对照**

- 单一 `<SplitView>`，一个断点（消灭 §1.2 的 1024 vs 1100 打架）。比例可拖拽，**且必须能一键复位、不持久化折叠态**（旧侧栏教训）。
- 视口高度从元素测量得来（`bind:clientHeight`），删掉 `VIEWPORT_H=560`（`DocView:194`）。
- **重解析/重切分后进入"等待态"并订阅任务**，完成时自动刷新（修 §1.3.2 不可达状态）；这是新交互里最实质的一条。
- 块选中 → 左栏定位（PDF `#page=N` 已可用）；补 `↑/↓` 切块、`Enter` 编辑、`Esc` 收起；原文高亮自动滚入视区（Search 已做 `Search:47-52`，DocView 没做）。
- 变高行（预览卡、富文本块）会让定高 windowing 数学失效 → 二选一：保持定高单行（`virtual.ts` 不动），或用 `content-visibility:auto` 替代手写窗口化（见 §4-3）。

**⑤ 检索：参数与结果一致**

- 参数改动**要么立刻重跑（debounce），要么显式"应用"并高亮"参数已改动，结果待刷新"**——不能再停在 §1.3.3 的静默脱节。URL 只在执行检索时写入。
- 命中卡片显示分数口径、页码、来源库，并新增「在文档中打开」深链 `#/kb/{id}/doc/{docId}?chunk={cid}`。
- 问答面板改会话式：delta 批处理（缓冲 flush）避免每 token 重解析整串引用（`AnswerPanel:68` 累加 + `:151` 每次 `splitCitations(answer)` → O(n²)）；`[n]` 点击定位；保留真实可中断（`sse.ts:105-110` 的 `handle.stop()`）。
- `✕` 换成 `Icon`（§1.6）；`/` 快捷键（`Search:95-101`）现在只排除 INPUT/TEXTAREA，会误触 `<select>`、Ctrl+/、对话框打开时——重写时统一走一个快捷键层，判断焦点所在与对话框栈是否非空。
- 导出报告改走 `api()`，享受统一错误与 401 鉴权提示（现 `Search:170-175` 裸 fetch）。

**⑥ 设置与"数据"**

- 标签保持 models/keys，新增 **`data` 标签**：`/stats`（行数/索引状态/版本数/磁盘占用）+ `/versions` + `/rollback`。今天这三条端点**完全没有 UI**，而它是单机库最重要的一层自救能力。
- 表单加脏值跟踪与"未保存"标记，保存分片提交（修 `KbSettings:63` 重载抹编辑）。
- `testResult`（`Settings:78`）从裸 JSON `<pre>` 改成逐字段可读结论 + 耗时；`testing !== ""` 禁用三个按钮改为按卡片独立。

**⑦ 对话框与反馈**

- `DialogBase` 统一 scrim/焦点陷阱/动画；`dialog.svelte.ts:33` 与 `form.svelte.ts:54` 的单例语义要显式化（队列化，或后开者被拒绝并提示），别再静默 resolve 第一个为 false。
- 保留并强化 §11.2 的三条好约定：错误态必带重试、破坏性操作量化影响（"将删除 342 个分块"）、上传失败只移除成功项。

**⑧ ApiDoc**：过滤词进 URL、索引跟随过滤结果、用 `scroll-margin-top` + `<details>` 原生展开替代 `setTimeout(50)` 滚动。

### 3.5 密度与节奏（与配色无关的部分，仍要改）

把今天散落的 7 处 ad-hoc `grid-template-columns`（`app.css:361,450,747,789,862,914,922`）收成一套 12 栏 + 具名布局；页面标题从 20px（`app.css:255`）降到 17px，统计信息（`.statline`）并入标签条，减少每张页头吃掉约 90px；表格行高统一 34px（这是 `virtual.ts:20 ROW_H` 的前提，不能随意改）并用 `font-variant-numeric: tabular-nums` 对齐数字列；语义色（`--green` 6 处 / `--red` 27 处 / `--yellow` 7 处）继续只用于状态，不承载布局信息。

### 3.6 双栏工作台与新配色的落地细节

#### 3.6.1 dock 的结构

```
<Dock>  (grid item，宽度 = --dock-w)
├ <SectionSwitcher>   5 个 section（图标 + 短标签）；键盘 1–5 或 ↑↓
├ <context slot>      随 section 变（库树 / 历史 / 任务 / 设置项 / 端点目录）
└ <footer>            主题切换、队列摘要、本机凭据状态
<button class="dock-toggle" aria-expanded aria-controls>   ← 在 <Dock> 之外（顶栏）
<div class="dock-handle" role="separator">                 ← 拖拽把手，键盘 ←/→ 可调
```

`aria-expanded`/`aria-controls` 是 §2.2 守卫要查的，不是装饰：读屏用户需要知道这个区域能不能展开、展开了什么。

#### 3.6.2 对象树的数据策略（5000 篇文档时树不能成为唯一导航）

后端没有"树"接口，可用的是 `/api/kbs`（含 `doc_count`/`chunk_count`）与 `/api/documents?kb_id=&q=&limit≤500`（`documents.py:279-286`）。因此：

- **库层全量常驻**（库数通常 ≪ 20，`/api/kbs` 一次拉全，与 `App.svelte:33` 共用同一份 store，消除 §1.3.4 的双拉取）。
- **文档层展开时懒加载**：每个库首次展开只取 200 条 + 服务端 `q` 过滤（dock 内输入即过滤，debounce 300ms），`total > 已取` 时在树里显示"还有 N 篇，用过滤或去列表页"。
- **树与内容区不共享分页**：dock 是"跳转器"，`#/{kbId}/docs` 的 `<DataTable>` 才是权威列表。这条要明确——否则 5000 篇的库会把 dock 变成第二个列表控件，还拿不到分页语义。
- **树也要虚拟化**：复用 `windowOf`（`virtual.ts:23-40`），但树行是变高的（库行 30px、文档行 26px、缩进不同）→ 定高假设不成立，按 §4-3 的结论改用分组虚拟化或 `content-visibility:auto`。
- 当前库/文档的选中态由路由派生（不是独立 state），保证"刷新/后退/分享后 dock 与内容区一致"。

#### 3.6.3 折叠契约（把上一代侧栏事故的教训写成规则）

1. 折叠开关**永远在 dock 外**（顶栏），所以不存在"折叠后没有地方可点"。
2. **折叠态不跨刷新持久化**：只持久化宽度 `--dock-w`。刷新后一律回到展开——这是与旧实现的关键差别，也是 §2.2 第 2 条断言的内容。
3. 折叠后保留 **56px 图标条**而不是完全隐藏：section 仍可达，树内容收起。窄屏（<900px）默认折叠 + 浮层展开，且浮层带 scrim 与 `Esc` 关闭。
4. 宽度必须 clamp：`240 ≤ --dock-w ≤ min(320px, 38vw)`，防止把手拖出视口后找不回（§2.2 第 3 条）。
5. 顶栏必须提供"恢复默认布局"（§2.2 第 4 条），且 dock 的滚动位置不进入路由。

#### 3.6.4 配色改造不是换色相，而是先拆语义（57 处）

今天 `--amber` 同时是**主操作/品牌**和**命中强调/选中**。实测使用点：`app.css` 内 37 处 `var(--amber*)` + 组件 scoped 内 20 处（`AnswerPanel.svelte:258-323` 引用最密集：引用角标 `[n]`、流式光标、激活引用卡都靠它）。若直接换成单一冷强调色，检索高亮将与主按钮、链接同色，命中信息被读成"可点的东西"。

所以第一步是令牌拆分，第二步才是换色：

| 新令牌 | 取代 | 语义 | 代表位置 |
|---|---|---|---|
| `--accent` | `--amber`（主操作向） | 品牌、主按钮、拖拽把手、tab 激活 | `app.css:148,333-336,629` |
| `--hit` / `--hit-soft` / `--hit-line` | `--amber-soft`/`--amber-line`（命中向） | 检索 `<mark>`、上下文命中条、选中行 inset、引用角标 | `app.css:826,850,855,890` + `AnswerPanel:259,300,315` |
| `--focus` | 保留 | 焦点环，必须与上面两者都不同色相 | `app.css:116,400,407,435` |
| `--ok/--warn/--danger` | `--green/--red/--yellow` | 仅状态，改名以避免误用为布局色 | 6/27/7 处 |

换色相时的硬约束：① 深浅两套都要重推导（`app.css:8-51` 与 `:53-75` 是两份完整调色板，不能只改深色）；② `mark` 文本对比度 ≥ 4.5:1（现在是 `--amber` 文字压 `--amber-soft` 底，换冷色时最容易翻车的一处）；③ `--focus` 在新基调下仍需与 `--accent`、`--hit` 肉眼可分；④ 首帧主题脚本（`web/index.html:9-17`）与 `localStorage("raggi.theme")` 行为不动。

#### 3.6.5 无边框需要分层，而分层刻度今天不存在

实测：`app.css` 里 `border: 1px solid` **26 处**、`border-color: var(...)` **17 处**，而阴影只有 `--shadow-pop` **1 个令牌**（深浅各一份定义，`:45-46,73-74`），全站 `box-shadow` 仅 4 处（其中 `:681` 是假焦点环、`:890` 是选中 inset 条）。也就是说当前视觉层级**完全由边框承担**——这正是"去掉边框"最大的隐藏成本。

落地顺序（不能颠倒，否则中途界面会散架）：

1. 先建 `--elev-0..3` 阴影刻度（每档 2 层：接触影 + 投影，深色用不透明黑、浅色用低饱和色影）+ 复用现有 5 级背景（`--bg / --bg-sunken / --surface / --surface-2 / --surface-3`，`app.css:20-24`）。
2. 再定"卡片用 elev、分隔用 bg-sunken 沟槽、表单用内凹输入"三条替代边框的规则，逐组件替换那 43 个 border 依赖点。
3. **注意 `app.css:45` 的阴影含 `oklch(0 0 0 / .55)`，浅色下必须重新定义**，否则无边框 + 弱阴影在浅色主题里会糊成一片——这是无边框设计最常见的失败点。
4. 无边框后 `:focus-visible`（`app.css:115-119`）与 `test_kb_card_actions_clickable` 的 `z-index` 要求更加重要：可点区域只能靠 elev 与光标表达，边框不再是安全网。

---

## 4. 逻辑层只需 4 处小改（其余原样沿用）

逐个模块核对后，`lib/` 与 `ui/*.svelte.ts` **没有一个需要推倒重写**：`types.ts`(383) / `api.ts`(138) / `sse.ts`(147) / `source.ts`(64) / `crumbs.svelte.ts`(18) / `toast.svelte.ts`(26) 完全视图无关（`api.ts` 只碰 `localStorage`，`:18-32`；`sse.ts` 只有 `fetch` + `AbortController`）。要动的是这 4 处：

**1）ops 层的 `lib → ui` 反向依赖**（真实存在，已 grep 定位）：`docops.ts:8-10`、`kbops.ts:9-11`、`chunkops.ts:9-11`、`docbatch.ts:8`、`jobcancel.ts:10` 都 import `../ui/*`。于是一个"改标题"函数同时是：API 调用 + 确认门（`docops.ts:123-129`）+ 表单对话（`docops.ts:21-53`）+ toast（`docops.ts:51,66,96`）+ 文案（`jobs.ts:25-35` 阶段名、`docops.ts:81-90` 引擎选项）。

> 做法：给 ops 注入 `Presenter`（`confirm/form/toast` 三个方法），ops 只声明"这个操作要确认、量化影响是什么"。**低成本路线**：新 UI 继续提供同名 `toast/confirmDialog/formDialog` store，ops 一行不改也能跑——第一批这么干，第二批再抽 Presenter。否则重写时每个操作都要在"改视图"和"改 ops"之间来回。

**2）`router.svelte.ts`**：`parse()` 的 name switch（`:18-33`）就是那份 5 项导航表；`go()` 里硬编码 `#/search` 记忆逻辑（`:62-66`）。保留 `parse/syncRoute/href/queryString` 管道，把 name switch 换成 **pattern 表**，并让 `App.svelte` 的 `nav`（`:47-53`）与 `activeNav` 折叠（`:55-60`）从同一张表派生。

**3）`virtual.ts`**：`windowOf()`（`:23-40`）是纯数学、零 DOM，但它要求①每行恰好 `ROW_H=34`（由 `DocView.svelte:460` 的 `tbody tr{height:34px}` 在模块外维持）②调用方给视口高（现在是硬编码 560）③padTop/padBottom 要用 spacer `<tr><td colspan>` 实体化（`DocView:337-339,396-398`）。**改行内标记没问题，改行高或改变高就不成立**。→ 把 34/560 两个魔数改成测量值，或对该列表改用 `content-visibility:auto`。

**4）`sse.ts`**：加可选的批处理 `onDelta`（缓冲 flush，供会话式问答）；流内错误路径（`:66-74`）复用 `api.ts` 的 `errorMessage`/`authHint`（`:76-99`），否则流中 401 会丢掉"未配置凭据"提示；`StreamHandlers.onSources` 的 `unknown[]`（`:20`）改成 `Citation[]`。

另有一处该顺手修的漂移：`jobs.ts:10-22` 自己重新声明了 `Job`（缺 `kb_id`），与 `types.ts:237` 分道扬镳；`jobcancel.ts:12` 的 `CancelOutcome` 与 `types.ts:258` 重复。删本地声明、统一从 `types.ts` 导入——DESIGN §11 写明 `types.ts` 是前端响应形状的唯一事实来源。

---

## 5. 分阶段路线（每阶段都可发布、都可归因）

| 阶段 | 内容 | 验收 |
|---|---|---|
| **0 守卫盘点与配色定稿** | D1/D2 已定（§0.1）；把 §2 的 13 条逐条标「保留 / 改名换义 / 新增等价守卫」；先出**新调色板与 elevation 刻度**的令牌稿（深+浅两套 + 对比度实测），再动组件 | 守卫清单确认；`tokens.css` 里 `--accent/--hit/--focus/--elev-*` 齐备且浅色对比度达标 |
| **1 外壳 + dock + 布局系统** | 重写 `App.svelte` 为 dock 外壳；新建 `ui/Dock.svelte` `<ObjectTree>` `<SectionSwitcher>`；**先写 §2.2 那条守卫再实现折叠**；`app.css` 拆 `tokens/layout/components`；建 `ui/primitives/`；修 `.content` 限宽、滚动单层、Toast 定位；把 43 个 border 依赖点逐步换成 elev | 旧视图塞进新壳即产生可见变化；`npm run build && npm run check` 绿；`pytest tests/test_frontend_wiring.py` 绿（含新增的 dock 守卫） |
| **1.5 配色迁移** | 按 §3.6.4 先拆 `--amber → --accent/--hit`（机械替换 57 处，不改观感），跑一轮确认高亮仍读作"命中"；**再**换色相与无边框化 | 拆分与换色分两个提交——同时做会无法判断对比度问题是谁引起的 |
| **2 加载三态 + 服务端过滤/分页** | 引入 `initial/refreshing/error`；KbDocs/KbChunks 接 `?q=/status=/offset=`；`<DataTable>` 落地并吃掉 5 张表 + `.listmore` ×3 | **轮询不再吞焦点**（可实测：跑一个入库任务，在过滤框里敲字，光标不丢） |
| **3 工作区标签化** | 路由改 `#/{kbId}/{tab}`；`KbDocs/KbChunks/KbSettings` 并入同一工作区的三个标签；删除入口 3→1；section 切换器与对象树接管导航；pattern 路由（§4-2） | 下钻 4 层 → 2 层；`/api/kbs` 启动只拉一次；dock 树与内容区选中态一致（刷新/后退/分享后不错位） |
| **4 原文对照** | `<SplitView>` 单断点；测量视口；reparse/resplit 订阅任务 + 完成自动刷新；块键盘导航 + 高亮滚动 | **不可达状态消除**：点重解析后无需离开页面即看到状态推进 |
| **5 检索与问答** | 参数一致性（应用/待刷新）；命中深链；会话式问答 + delta 批处理；快捷键层；导出走 `api()` | 改 `top_k` 后不再出现"参数新、结果旧" |
| **6 设置/数据/ApiDoc** | 新增 `data` 标签（`/stats` `/versions` `/rollback`）；脏值跟踪；ApiDoc 过滤进 URL | 端点覆盖补上 3 条；模型/密钥/健康体验一致 |
| **7 ops 解耦（可选）** | 抽 `Presenter`；统一 `types.ts` | ops 层不再 import `../ui/*`（grep 应为 0） |

每阶段收尾固定动作：`npm install --include=dev`（若变更）→ `npm run check` → `npm run build` → `pytest tests/test_frontend_wiring.py tests/test_apidoc.py` → 浏览器实测（**隔离 data + 桩 embedding**，不动 `data/` 与运行实例；注意 Ollama 常不在运行，检索类验证要预备 503 分支）→ **同步更新 `DESIGN.md` §11**（视图表、交互约定、构建注意三处都要跟上新现实）。

---

## 6. 风险清单

1. **重演死锁，只是换了名字**（D1 的首要风险）。旧守卫禁的不是"侧栏"而是"不可恢复的状态"。改名 `.dock` 只让测试变绿，不等于问题解决——所以 §2.2 的四条断言要**先写再实现**：开关在 dock 外、折叠态不持久化、宽度 clamp、有复位入口。逐条读守卫 docstring 再决定怎么处理，比绕过它重要。
2. **dock 退化成第二个列表控件**（D1 的结构性风险）。5000 篇文档的库里，树既拉不全也没有分页语义（§3.6.2）。规则要钉死：dock 只负责"跳转 + 服务端过滤 + 前 200 条"，权威列表永远是内容区的 `<DataTable>`；否则两处分页各说各话，比现在更难用。
3. **换基调 + 无边框 = 没有层级**（D2 的首要风险）。今天 43 个 border 依赖点撑着全部视觉层级，而阴影只有 1 个令牌（§3.6.5）。必须先建 `--elev-*` 刻度再拆边框；且浅色主题下阴影要单独重推导（`app.css:73-74`），否则"无边框"在浅色里直接散架。
4. **`--amber` 语义混用被原样带入新调色板**（D2 的隐蔽风险）。57 处使用点里主操作与检索命中混用同一色（§3.6.4）。**令牌拆分与换色相必须分两次提交**，一起改则对比度问题无法归因。
5. **静默刷新引入竞态**。旧请求覆盖新结果：沿用 `Search.svelte:107,124` 已有的 `viewerSeq` 序号守卫，抽成通用的 stale-guard，别在阶段 4/5 各写一遍。
6. **windowing 与新行设计冲突**。定高 34px 是 `virtual.ts:20` 的数学前提；分块预览想要变高（以及 dock 树本身行高不一）就必须同时换掉窗口化方案，二者不能只改一个。
7. **动效与无障碍回退**。`prefers-reduced-motion`（`motion.ts` + 全局 `app.css:129`）与 focus-visible 环要在新组件里一次做对；`test_focus_outline_not_removed` 会在你移除 `.compose:focus`/`.qbar input` 时报红——那是提醒，不是噪声。无边框化后可点区域更依赖焦点环与 elev 表达，这条风险被放大。
8. **构建/托管链路不能破坏**：产物只写 `web/dist`（`emptyOutDir`），运行时由 FastAPI `StaticFiles` 托管，**运行时零依赖**；验证新产物时用带查询串的地址绕开 `index.html` 缓存（DESIGN §11.3）。
9. **不要引入第三方 UI 框架**（与单机零依赖原则冲突），也不要顺手把 `types.ts` 变成第二份事实来源——新增端点返回类型一律加进 `types.ts`。dock 的历史查询/宽度等浏览器侧数据要显式标注为"本机"，不伪装成服务端状态。
10. **破坏性验证**：涉及删库/回滚的新 `data` 标签，只在隔离目录 + 桩 embedding 下验证，绝不拿 `data/` 试（Ollama 常不在线，检索类验证要预备 503 分支）。

---

## 7. 附：文件级处置表

| 文件 | 处置 | 说明 |
|---|---|---|
| `App.svelte`(150) | **重写为 dock 外壳** | grid 外壳 + `<Dock>` + `<SectionSwitcher>`；`nav`/`activeNav`（`:47-53`、`:55-60`）改由 section 表派生；折叠开关与复位入口留在壳层（§2.2 断言 1）；队列摘要、对话框挂载 |
| `ui/Dock.svelte` `<ObjectTree>` `<SectionSwitcher>` | **新建** | section-aware 上下文栏；宽拖拽把手（`role="separator"` + 键盘调节）；命名避开 `.rail`/`--rail-w` |
| `views/*.svelte`(9, 3000) | **重写** | 按 §5 阶段 2–6 分批迁移；`KbDocs/KbChunks/KbSettings` 合并为一个工作区三标签 |
| `ui/StateBlock / Toaster / ConfirmDialog / FormDialog / Icon` | **重写其表现**，store 不动 | 引入 `DialogBase`；修 toast 定位；`toast.svelte.ts`/`dialog.svelte.ts`/`form.svelte.ts` 保留（单例语义要显式化） |
| `ui/AnswerPanel / UploadPanel / KeysPanel` | **重写** | 会话式问答 / 内联入库 / 密钥表；注意 `KeysPanel:163` 缺 `.table-wrap` |
| `ui/icons.ts` + `index.html` 精灵 | **保留 + 扩充** | 27 个名字；新图标要同时改两处；禁 emoji/unicode（现仅 `Search:423` 一处违例） |
| `styles/app.css`(1177) | **拆分重写** | 拆 `tokens/layout/components`；间距节奏与 5 级背景保留；**新增 `--accent/--hit/--focus/--ok/--warn/--danger/--elev-0..3` 与 `--dock-w/--content-max`**；`.view`、`[data-col]`、`tr[data-chunk]` 三处死代码清理（`[data-col]` 要重新落地成活的）；`.toast` 定位修正；DocView scoped 覆盖问题消除 |
| `lib/types.ts / api.ts / sse.ts / source.ts / crumbs.svelte.ts` | **原样保留** | 视图无关；`sse.ts` 加批处理属增强 |
| `lib/stores.svelte.ts / jobs.ts / motion.ts / virtual.ts / router.svelte.ts / searchparams.ts / jobcancel.ts / docops.ts / kbops.ts / chunkops.ts / docbatch.ts` | **保留 + 小改** | 见 §4；`searchparams.ts` 的 `PARAMS.id`（DOM id）可去掉但要补 aria-label；`withParam` 里硬编码的 `"/search"`（`:72`）随路由表更新 |
| `data/apidoc.ts`(1124) | **不动** | 与 `test_apidoc.py` 双向锁端点 |
| `web/index.html` | **保留** | 首帧主题脚本、SVG 精灵、`#app`、`/src/main.ts`（后四项是守卫断言） |
| `tests/test_frontend_wiring.py` | **保留 + 新增 + 改写** | 与 D1/D2 必然冲突的 4 条：`.rail` 禁令（改名 `.dock` 后旧守卫保留，**另加 §2.2 可自救守卫**）、`crumbbar` 断言（改写为"任意层级当前对象可在壳层读出"）、focus 白名单 `.compose:focus`/`.qbar input`（组件改名要同步白名单）、`kbcard-acts`/`.kbcard-foot{z-index:1}`（卡片保留则不动）。每条更新都保留 docstring 里的事故理由 |

---

## 8. 阶段 0–1 完成记录（2026-10-04）

已落地：dock 双栏外壳 + 新令牌层 + 队列摘要 + 2 条新守卫。9 个视图未迁移，仍用旧组件样式（`--amber*` 别名保证它们在新配色下继续可用）。

**新增/改动文件**

| 文件 | 内容 |
|---|---|
| `styles/tokens.css` | 冷中性色阶（深浅两套）、`--accent`/`--hit`/`--focus` 语义拆分、`--elev-0..3`、`--dock-w`/`--content-max`；文件末尾是 `:root, :root[data-theme="light"]` 的过渡别名块 |
| `styles/shell.css` | dock / section 切换器 / 对象树 / 把手 / 队列 chip / 窄屏浮层 |
| `styles/app.css` | §5 主体骨架改为 grid 外壳（删掉 `.view` 与失效的 1400px 限宽）；`.toast` 去掉 `position:fixed` |
| `lib/dock.svelte.ts` | 折叠/宽度状态、`clampWidth()`、`resetLayout()`、`NARROW_QUERY` |
| `lib/nav.ts` | section 表（导航单一来源），取代 `App.svelte` 的 nav 数组 + `activeNav` 折叠三元 |
| `lib/queue.svelte.ts` | 队列摘要轮询（活跃 4s / 空闲 25s / 隐藏页暂停） |
| `ui/Dock.svelte`、`ui/SectionSwitcher.svelte`、`ui/ObjectTree.svelte` | 壳层组件 |
| `App.svelte` | 重写为外壳：顶栏含折叠开关与队列 chip、`.shell` 三列网格、crumbbar 移入内容列、快捷键 1–5 |

**实现期间发现的三个问题（都不是计划里预判的，属于"只有跑起来才知道"的那类）**

1. **网格自动放置把内容区压成 56px 竖条**。把手在折叠态是 `display:none`、窄屏下也是 → 它脱离网格流后，自动放置把 `.workspace` 往前挪一列，挪进那条 0px 宽的列里，文字变成一字一行。**修法**：三个子项显式 `grid-column: 1/2/3`，让"某个子项消失"不改变别人的列。窄屏媒体查询还必须连 `.shell.is-collapsed` 一起覆盖（它特异性更高，漏掉就仍保留 56px 图标列）。
2. **别名只写了深色一套，导致同一按钮两种颜色**。app.css 在 `:root[data-theme="light"]`（0,2,0）里也定义了 `--amber`，只在 `:root`（0,1,0）写的别名压不过它 → 深色走新主色、浅色仍是旧琥珀。**修法**：别名块选择器写成 `:root, :root[data-theme="light"]`。
3. **`900px` 断点散在三处**（shell.css 媒体查询、`initDock`、`App` 的 `matchMedia`）。已收敛为 `NARROW_QUERY` 常量 + CSS 侧耦合注释。

**守卫变更（每条都保留原事故理由）**

- 新增 `test_dock_collapse_is_self_recoverable`、`test_dock_persists_only_width`（§2.2 的四条断言）。**已做变异测试验证有效性**：把开关移进 Dock → 判红；把折叠态写进 localStorage → 判红。
- `read_styles()` 改为按 `styles/*.css` 拼接——守卫查的是不变量，不是它写在哪个文件。
- `test_rail_css_and_tokens_removed` 与两条 dock 守卫现在**先剥注释再断言**。原因很实际：我写的说明文字"旧守卫禁的是 .rail 那套死令牌"把守卫判红了，而 `href={n.href}` 那条也是被注释里的 `<Dock>` 误伤。会因文档字符串报红的守卫，最后只会逼人删掉说明或删掉守卫。同文件里 `test_no_native_confirm_or_alert_in_views` 早就是这么做的。
- `test_apidoc.py::test_view_registered_in_html_and_router`、`test_search_api.py::test_topbar_has_no_param_controls` 改读 `lib/nav.ts` 与 `ui/SectionSwitcher.svelte`（导航搬了家）。后者顺手补锁一条新规则：**导航项必须是 `<a href>`**——我第一版用了 `<button onclick>`，中键新开/右键复制链接全丢，正是 `test_views_use_native_controls` 记录过的老错。

**验证结果**

| 项 | 结果 |
|---|---|
| `svelte-check` | 0 errors / 0 warnings |
| `npm run build` | 通过 |
| `pytest tests/test_frontend_wiring.py` | **15 passed**（原 13 + 新 2） |
| `pytest`（全量） | **391 passed** |
| 浏览器实测（隔离实例，端口 8123，data 在 /tmp，已销毁） | 7 条路由全部渲染，**console 零消息**；桌面分支实测 `240+5+286`（展开）/ `56+475`（折叠），折叠往返正常；窄屏浮层 x=0 宽 320 + scrim + `aria-expanded` 正确 |

**一处刻意偏离计划**：§5 阶段 1 原写"app.css 拆 tokens/layout/components"。实际只拆出 `tokens.css` + `shell.css`，`app.css` 作为遗留组件层保留未动。理由：同一批改动里既动壳层又重排 43 个边框依赖点，回归无法归因；且 `test_focus_outline_not_removed` 的白名单（`.compose:focus`/`.qbar input`）要求那些选择器继续存在。components.css 的拆分并入阶段 2–6，随视图迁移逐块搬。

**下一步（阶段 2）**：加载三态 `initial/refreshing/error` + 服务端过滤与分页（`documents.py:279-286` 早已支持 `q/status/parser_engine/mime/offset`）+ `<DataTable>` 吃掉 5 张表与 3 份 `.listmore`。验收要点仍是那条：**有任务在跑时在过滤框里敲字，光标不丢**。

---

## 9. 阶段 2 完成记录（2026-10-04）

`<DataTable>` 抽组件**没有**放进这一批——它和"加载语义"是两件事，捆在一起会让回归无法归因。阶段 2 只做加载模型与取数正确性，组件抽象留到阶段 3/4 随视图迁移一起做。

**新增 `lib/loadstate.svelte.ts`**

- `loader()` → `phase: initial|ready|failed` + `refreshing` + `error`，带请求序号守卫（并发时只认最后一次结果）。核心规则：**已有数据就不进 initial**，所以轮询、手动刷新、改过滤条件全都自然走 refreshing，不需要调用方各自记得传"静默"标志。
- 刷新失败且手上有数据 → 保留表格 + 一条 `role="alert"` 横幅，不再把整页换成错误态（后端重启时旧行为会凭空抹掉用户正在看的一屏）。
- `Pager` → offset 追加。取代 `limit += 200`：服务端把 limit 夹在 500（`documents.py:283`、`chunks.py:49`），第三次点击会被静默压回，按钮还在但点了不多一条。

**三个视图迁移**

| 视图 | 变化 |
|---|---|
| `KbDocs` | 过滤框走**服务端** `?q=`（debounce 300ms）。`documents.py` 的 q 在分页前过滤、`total` 是过滤后的数，所以语义可靠；删掉那层"在截断的一屏里再筛一遍"的 `shown`；轮询走同一套三态 |
| `KbChunks` | 同模型 + offset 追加；首屏从一行"加载中…"改成骨架屏（与兄弟页一致）；**文案诚实化**——`/api/chunks` 没有 `q` 参数（`chunks.py:36-38`），所以写"在已加载的 N 块中匹配 M 块"，不假装是全库过滤 |
| `Jobs` | 同模型；前缀 `/api/v1` → `/api`；**标题不再每 2 秒全量拉 500 篇**，改为只补缺失 doc_id、每轮上限 12 个、取不到回落 UUID；`limit=100` 硬顶换成 offset 追加 + tab 计数标注"仅覆盖已加载 N 条"；失败原因从 `max-width:22ch` 截断改成 `<details>` 全文（title 在触屏上读不到） |

顺手修掉一个链接 bug：`Jobs.docHref` 原来写反成 `if (!j.kb_id) return "#/kb/" + kb_id`——kb_id 为空时拼出 `#/kb/`，点上去跳到库列表，与它自己注释的"两者都有才能链回去"矛盾。

**运行时验证抓出的两个自伤（静态检查看不见）**

1. **kbId effect 读了 `filter`**（为了同步 lastQuery），于是"用户敲一个字"就让这个 effect 重跑 → 重置视图、清空 items、并且 cleanup 把 `pollTimer` 清掉，**轮询链当场断掉**。修法是用 `untrack()` 包住重置块。这是我在浏览器里追"为什么没有请求"时发现的——一开始误判成"焦点保住了所以没请求"，把请求日志完整打出来才看清。
2. **工具栏 gate 用 `items.length > 0`**：过滤到零结果时输入框被抽走，用户后面敲的字全落在空处；改成 `filter` 非空也保留后，又发现"清空过滤→请求返回"之间 items 短暂为空仍会闪。最终 gate 用 `kb.doc_count`（库内真实篇数，不随过滤变化）——过渡期不闪。

**焦点保持实测（阶段 2 的验收动作）**：embedding 服务当时不可达（真实入库拿不到文档行），所以用**客户端 fetch 桩**在隔离实例上驱动——纯浏览器侧，不写任何后端数据。四步序列每步都断言"输入框是同一个 DOM 节点 + 焦点还在 + 值没丢"：

| 步骤 | sameNode | focused | 行数 |
|---|---|---|---|
| 敲一个命中词 | ✔ | ✔ | 1 |
| 敲到无命中 | ✔ | ✔ | 0 |
| 清空过滤 | ✔ | ✔ | 5 |
| 跨过一轮 2.5s 轮询 | ✔ | ✔ | 1 |

旧实现在第 1 步之后的每次轮询都会把这四行全变成 ✘。

**守卫变更**：新增 3 条（`test_migrated_list_views_use_the_three_state_loader`、`test_loading_bool_allowlist_only_shrinks`、`test_frontend_uses_a_single_api_prefix`），`test_frontend_wiring.py` 现 **18 条**。新守卫同样做过变异验证：往 KbChunks 注入真实的 `let loading = $state(` 与 `limit += 200` → 判红；还原后转绿。另更新 2 条既有守卫以适应重构、且**不降低强度**：`only_standalone` 接受 `URLSearchParams` 写法；分页判断改查 `pager.more` 并额外断言 `Pager.more` 的定义是 `offset < total`、视图里不得出现 `< limit` 式判断。

`test_loading_bool_allowlist_only_shrinks` 用的是"清单只减不增"的写法：仍用 loading 布尔的视图必须精确等于 `PENDING_LOADING_BOOL` 那份**带所属阶段注释**的映射（KbList→阶段 3、DocView→阶段 4、Search→阶段 5）。这样"还没做"是可见待办，新写的视图也无法悄悄走老路。

**第三次遇到同一课**：结构守卫会被**注释里的说明文字**误伤——我在注释里写 `limit += 200` 直接把新分页守卫判红了。已加 `strip_code_comments()`（剥 HTML 块注释、`/* */`、整行 `//`），且只在全行匹配处剥 `//`，以免动到 `https://…`。事故复盘的文字恰恰最需要提到被禁的写法，守卫必须先看代码、后看注释。

---

## 10. 阶段 3 完成记录（2026-10-04）

**路由改成模式表 + 命名参数**（`router.svelte.ts` 的 `PATTERNS`）。视图不再收 `parts: string[]` 后各自 `parts[1]`/`parts[3]`，而是收具名 props：`{kbId}`、`{kbId, docId}`、`{kbId, scope}`。`ObjectTree` 的选中态也改从 `route.params` 派生，dock 高亮与内容区显示的对象因此不可能不一致。

一个刻意偏离：方案 §3.3 写的是 `#/{kbId}/{tab}`，实际保留 `kb` 段做成 `#/kb/{kbId}/{tab}`。短版会让 `#/search/docs` 这类地址与顶层 section 路由同形，路由得靠"这段像不像 ID"消歧——少一个斜杠不值得换来一层隐式判断。

**工作区标签条**（`ui/WorkspaceTabs.svelte`）：一个知识库的文档 / 分块 / 设置变成同一位置的三个标签，文档详情也在它下面。下钻从 4 层降到 2 层。标签是 `<a href>`，三个地址都可分享、可中键新开。

**旧地址改写放在 `syncRoute()` 而不是只在启动时做**。第一版只在 `startRouter()` 里改写，浏览器实测发现：站内导航到 `#/kb/{id}` 时视图渲染正常，但地址栏停在旧形态，用户复制到的 URL 和当前界面对不上。现在任何一次导航命中旧模式都会 `location.replace` 成完整路径（replace 是为了不留"点进去又立刻被改写"的历史）。

**分块标签合并独立分块**：`#/kb/{id}/standalone` 那一页变成"分块"标签上的 `?scope=standalone` 范围切换（服务端 `only_standalone`，不是客户端筛）。这顺带补上一个真实缺口——旧结构里 `KbChunks` 永远 `only_standalone=true`，所以"这个库的所有分块"在界面上**根本没有入口**。

**删除入口 3 → 1**：卡片脚与文档列表页头的删除去掉，只留设置页危险区。`KbDocs` 页头也从 5 个动作（入库/刷新/独立分块/设置/删除）收敛到 2 个（入库/刷新），其余由标签条承担。子页的 backlink 一并撤掉——"回去"由标签条统一提供，两套控件指向同一处迟早漂移。

**`/api/kbs` 启动只拉一次**：`stores.svelte.ts` 的 `loadKbs()` 带 in-flight 去重（并发调用共享同一个 promise），App 与 KbList 都用它。浏览器实测 `performance.getEntriesByType('resource')` 里 `/api/kbs` 恰好 1 条。KbList 同时迁到三态加载模型，已从 `PENDING_LOADING_BOOL` 清单移除。

**验证**（隔离实例，端口 8124，data 在 /tmp，用完销毁）：

| 检查 | 结果 |
|---|---|
| 旧地址 `#/kb/{id}` | → `#/kb/{id}/docs`，标签高亮"文档" ✔ |
| 旧地址 `#/kb/{id}/standalone` | → `#/kb/{id}/chunks?scope=standalone`，标题"独立分块"，范围按钮"仅独立块"按下 ✔ |
| 范围切回"全部" | URL → `#/kb/{id}/chunks`，标题"全部分块" ✔ |
| 设置标签 | 危险区存在，页面上删除类按钮只有 1 个（"删除本知识库"）✔ |
| 文档列表页头 | 只剩「入库 / 刷新」✔；卡片脚 trash 数量 = 0 ✔ |
| dock 树跟随路由 | 导航进某库后该库自动展开且高亮 ✔ |
| `/api/kbs` 请求数 | 1 ✔ |
| console | 零消息 ✔ |
| `svelte-check` / 全量 pytest | 0 errors 0 warnings / **394 passed** |

**守卫变更**：更新 4 条以适应重构、意图全部保留——
`test_apidoc` 的 `'parts[0] === "apidoc"'` 改为查模式表条目；
`test_architecture::test_backlink_on_every_subpage` 重写为 `test_subpages_have_a_way_back`，改查"标签条覆盖了 kb 与 doc 两种路由、且生成的是 `<a href>`"（原来查的是每页有没有 backlink）；
`test_standalone_chunks_surface` 的 `"/standalone" in KbDocs` 改为查分块标签的 `scope=standalone` 入口 + `only_standalone` 仍下推 + 设置页入口；
`MIGRATED_LIST_VIEWS` 加入 KbList、`PENDING_LOADING_BOOL` 相应减到 2 项（DocView→阶段 4、Search→阶段 5）。

**下一步（阶段 4）**：`<SplitView>` 统一双栏（消灭 DocView 里 scoped 1024px 与全局 1100px 两套断点、以及被 scoped `min-height:320px` 压掉的 `overflow:auto`）、视口高度改为测量（删 `VIEWPORT_H=560`）、**reparse/resplit 后订阅任务并在完成时自动刷新**（今天点完只能离开再回来）、块级键盘导航与原文高亮滚入视区。DocView 也在 `PENDING_LOADING_BOOL` 里，这一批要顺手清掉。

---

## 11. 阶段 4 完成记录（2026-10-04）

**`ui/SplitView.svelte`** 成为双栏的唯一实现：两个 `<section class="pane" aria-label>`，各自 head/body 槽位，用 `--elev-2 + --elev-edge` 表层级而非边框（对齐 D2）。它的 `.pane-body` **自己不滚动**，只做 `flex:1; min-height:0; overflow:hidden` 的 flex 列，把滚动权交给槽位里的内容——否则右栏会套出两层滚动容器，内层拿不到稳定的 scrollTop，虚拟化窗口也就量不准。

**高度不再靠猜**：删掉 `calc(100vh - … - 210px)` 这个魔法数。壳层给 `.main` 加 `is-full`（`route.name === "doc"`），整条 flex 链把剩余高度交下来；窄屏（<1100px）在 app.css 里把这条链整体退回普通文档流。页头换行、标签条出现都不会让布局失准。

**DocView 重写**：迁到三态 loader + `Pager`；视口高度用 `ResizeObserver` 测 `clientHeight`，删掉 `VIEWPORT_H = 560`；`↑/↓` 在分块间移动选中并自动滚进视区、把焦点交给新行；文本高亮块用 `use:revealHit` action 滚进视区（action 直接拿到元素，不违反"视图不手动查询 DOM"）。

**异步任务订阅（这批最重要的一条）**：`reparseDoc` / `resplitDoc` 现在通过 `onQueued` 把 `job_id` 交给调用方，DocView 用 `waitForJob(jobId, onTick, signal)` 盯到终态并自动刷新；页头显示当前阶段，任务在跑时禁用这两个按钮。`waitForJob` 新增 `AbortSignal` 参数，`onDestroy` 里中止——否则用户早走了，循环还在后台打接口到超时（约 10 分钟），并且往已卸载的组件里写状态。原来的 toast 写的是"处理完成后文档状态自动更新"，而界面根本做不到：文案承诺了实现没提供的能力，比不承诺更糟。

**顺带清掉的**：DocView 里与全局重复的第二套 `.docsplit/.pane/.pane-head/.pane-body`（scoped 的 `min-height:320px` 曾压掉全局 `overflow:auto`）、断点 1024 vs 1100 打架、三个从未被任何代码引用的 id（`docSplit`/`sourceBody`/`chunkWrap`）、以及 app.css 里死的 `tr[data-chunk]`。

**守卫**：`test_frontend_wiring.py` 现 **21 条**（新增异步订阅、双栏唯一定义、视口必须实测三条）。`test_doc_viewer.py::test_doc_view_is_two_pane` 重写——它原先查的就是那三个死 id，等于把"存在三个没用的 id"当成契约；改查"视图用 SplitView + 两个带 aria-label 的面板"。`test_pagination_uses_api_total_not_item_count` 的 DocView 部分改查 `pager.more`。

**一个守卫自己先错了**：`test_pane_layout_defined_once` 第一版用正则 `@media \(max-width: (\d+)px\)\s*\{(…)*?\.main\.is-full` 找断点，实测把 `.main.is-full` 那条规则整段删掉后**它照样命中**——正则越过右花括号在后面的文本里找到了选择器。改成按大括号配对切块再查块体（`media_blocks()`），并自检过三种情形：正常命中 1100、删规则后为空、多加一个断点后为两个。教训：**文本型守卫必须验证它会失败**，否则它可能只是永远为真。

**验证矩阵**：`svelte-check` 0/0；`npm run build` 通过；全量 pytest **397 passed**；浏览器实测（隔离实例 8125，data 在 /tmp，已销毁）确认 `is-full` 正确施加、窄屏下 `.main` overflow 回退为 `auto`、标签条渲染、错误态带重试、console 仅有我故意访问不存在文档产生的两条 404。

**未验证项（诚实记录）**：双栏的**视觉呈现**与"点重新切分→任务完成→页面自动刷新"这条链路**没有做端到端实测**。原因是这台机器上 Ollama 不可达、配置的 embedding 是远端 API（要用你的密钥、消耗你的配额），拿不到真实文档行；而浏览器侧 fetch 打桩这一轮被权限策略拦下，我没有绕过。要补的话，在有可用 embedding 的环境里做两件事即可：(1) 打开任一篇 PDF 文档，确认两栏各自滚动、外层不滚、点块能跳页；(2) 点「重新切分」，等它跑完，**不离开页面**，确认块数与状态自己更新了。

---

## 12. 阶段 5 完成记录（2026-10-04）

**核心修复：参数与结果不再脱节。** 契约改成 **URL 只描述"产生了当前这批结果的参数"**：控件读写 `draft`（暂存区），只有「应用并重查」才经 `searchUrl()` 提交进 URL 并触发检索；同时给出「放弃改动」出口。`withParam`（"改一个参数就立刻改写 URL"的旧路径）随之删除——它正是脱节的成因。

配套：结果条上的"范围"改读 `query.get("kb_id")`（已生效值）而不是 `val()`（暂存值），否则应用之前那行元信息就在描述一次不存在的检索——同一个谎换个地方不能说两次。

**其余四项**：
- `Search` 迁到三态 loader，`PENDING_LOADING_BOOL` 清单**清空**（9 张视图全部走同一套加载语义）。
- 命中卡片新增「在文档中打开」深链 `#/kb/{kbId}/doc/{docId}?chunk={cid}`；`DocView` 消费一次该参数（`chunkConsumed` 标志 + 换文档时重置）。卡片结构同时改掉一个无效 HTML：`<a>` 原本会嵌在 `<button>` 里，现在主操作是按钮、深链是它的兄弟节点。
- `/` 快捷键补齐排除条件：`<select>`、contenteditable、`Ctrl/Meta/Alt + /`、以及确认框/表单框开着时。旧实现只排除了 INPUT/TEXTAREA。
- 导出报告改走新增的 `apiText()`，与全站共用鉴权头与错误解析；`✕` 字符换成 `Icon name="close"`（DESIGN §11 的图标约定，全仓库唯一一处违例）。
- `AnswerPanel` 的 delta 改为缓冲 + 按帧 flush，`splitCitations` 的整串重解析从"每 token 一次"降到"每帧最多一次"（原为 O(n²)）；`stop()` 时立即 flush，已生成的文字不会被半截缓冲吃掉。

**端到端实测（真实请求，未打桩）**：embedding 仍不可达，但这一批要验的是**交互契约**，503 不影响计数。用网络日志核对：

| 动作 | `POST /api/search` 次数 | 界面 |
|---|---|---|
| 打开 `#/search?q=测试` | 1 | 正常发起 |
| 把「返回条数」8 → 20 | **+0** | 出现横幅「已修改 1 项参数（返回条数），下面的结果还是上一次检索的」 |
| 点「应用并重查」 | **+1** | URL → `#/search?q=测试&top_k=20`，横幅消失 |

即"改参数不静默重跑、也不静默不跑"，而是把不一致摊开并给出口。快捷键三项也实测通过：`/` 能聚焦查询框、焦点在 `<select>` 上时不抢占、`Ctrl+/` 不被拦截。

> 一个测量教训：`performance.getEntriesByType('resource')` 当场数出 1 次，让我以为"应用后没发请求"；网络日志实际是 2 次。计数要走 `list_network_requests`，别信自己刚写的那次数组长度。

**守卫**：`test_frontend_wiring.py` 增至 **23 条**（新增参数脱节、导出走请求层两条），`test_search_api.py` 的 URL 真源测试收紧为"URL 描述已生效参数"并禁止 `withParam` 复活。第四次撞到注释误伤——我在 `onkeydown` 的说明里写了 `<select>`，`test_labels_are_bound_to_controls` 把整个文件判成"有无可访问名缺失的控件"；已给该守卫与 `test_views_use_native_controls` 加上剥注释。

**验证结果**：`svelte-check` 0/0、`npm run build` 通过、全量 pytest **399 passed**、隔离实例（8126，data 在 /tmp，已销毁）实测如上，你的 `data/` 未被触碰。

**下一步（阶段 6）**：设置页与 API 文档页。设置页要加脏值跟踪与"未保存"标记（`KbSettings.svelte:63` 的 `saveBase` 成功后重新 `load()` 会抹掉未提交的方案编辑；整页无 loading 分支 → 拉取期间白屏）、`testResult` 从裸 JSON 改成逐字段结论、并新增 **`data` 标签**接上今天完全没有 UI 的 `/stats`、`/versions`、`/rollback`；API 文档页要把过滤词同步进 URL、接口索引跟随过滤结果、用 `scroll-margin-top` 替代 `setTimeout` 滚动定位。

---

## 13. 阶段 6 完成记录（2026-10-05）

**范围**：展示格式化收敛、设置页（模型/密钥/健康/**数据与版本**）、库设置页、API 文档页。

### 13.1 做了什么

- **`lib/format.ts`（新）**：`fmtTime / fmtTimeShort / fmtAgo / fmtBytes / fmtMs / fmtSpan` 一处定义。原先 `fmtTime` 在 6 个文件里各写一遍，而且是两种互斥风格（`slice(0,16).replace("T"," ")` 出 `2026-10-04 12:30`，`toLocaleString("zh-CN")` 出 `2026/10/4 12:30:00`，空值一个 `""` 一个 `"—"`）。收敛时又发现第 7 份：`AnswerPanel.svelte:117` 的本地 `fmtMs`。现在全站除文件名（`rag-report-2026-10-05.md`）外不再有任何就地日期格式化。
- **`ui/DataPanel.svelte`（新）+ 设置页 `data` 标签**：接上此前**完全没有界面**的 `/stats`、`/versions`、`/rollback`（`rag/api/system.py:27,35,44`）。回滚按表生效、不跨表，所以后端 `note` 原文照抄进 toast，界面也明说"其他表不会一起回滚，回滚后请到健康页对账"。回滚确认走 `confirmDialog({destructive, requireTyping: table})`。
- **设置页**：`tab` 改为命名参数（`#/settings/{tab}`，不再位置索引）；`testResult` 从一坨 `JSON.stringify` 的 `<pre>` 改成逐类结构化结论（连通/不通、延迟、维度、吞吐、排序是否正确、sample）；禁用状态按 kind 记（原先点一张卡片的测试会锁死三张）；新增 `dirty` 跟踪，保存按钮只在真有改动时可用。
- **库设置页**：补上**原本不存在的加载分支**（旧模板 `{#if error}{:else if kb && plan}` 在拉取期间两个条件都不成立 → 白屏）；两个面板各自判脏；`saveBase` 改为就地更新 + `void loadKbs()`，不再整页重载（那正是"先改方案、再改描述并保存 → 方案编辑被静默抹掉"的成因）；删除库统一走 `removeKbDialog`。
- **API 文档页**：过滤词双向同步进 URL（`#/apidoc?q=…`，debounce 300ms 以免后退键被塞几十条历史）；索引改渲染 `indexed = $derived(shown)`（此前索引渲染 `GROUPS` 全量、卡片渲染 `shown`，搜一个词就出现"卡片 3 个 / 索引 51 个"，点索引还跳到已被过滤掉的端点）；跳转从 `setTimeout(…, 50)` 改为 `await tick()` + `bind:this` 元素表 + `class:is-flash` 状态驱动高亮，滚动定位交给 `scroll-margin-top`（替代手算偏移量）。

### 13.2 验证时发现并修掉的 8 个问题

全部有浏览器或接口证据，不是读代码读出来的怀疑。

1. **真值陷阱把 0 显示成缺失**。版本表「大小」列全是 `—`，而接口返回 `total_files_size: "0"`。根因 `fmtBytes(Number(meta(v,k)) || null)`——`0 || null` 变成 null，"0 B"被当成"没有这个字段"。改成 `numMeta()` 显式判 `Number.isFinite`。实测修复前后：`… 0 0 — 回滚到此版本` → `… 0 0 0 B 回滚到此版本`。
2. **确认框正文里写了 Markdown**。`ConfirmDialog` 用 `{cur.body}` 渲染纯文本（必须如此：正文带表名等运行时值，能塞 Markdown 就能塞注入面），而我新写的回滚确认写了 `且**不可撤销**`，无障碍树里原样显示成星号——最该看清的一句话反而成了噪声。全仓扫描确认只有这一处，已加守卫。
3. **`$effect` 的依赖读在了定时器回调里**。`const t = setTimeout(() => { const want = filter.trim(); … }, 300)`：effect 建立时没读到 `filter`，于是永不重跑，实测地址栏一直停在 `#/apidoc`。把 `filter.trim()` 提到同步阶段后，`#/apidoc?q=chunks` 正常写入，且输入框焦点保持。`svelte-check` 对这一类错误完全无声。
4. **过滤命中 8 条却渲染 0 张卡片**。只有首组默认展开，搜 `chunks` 命中的是别的组 → 卡区空渲染，页面只剩"匹配 8 / 51"和 8 条索引链接，看着像过滤把内容清光了。改为命中分组自动展开（写回同一个 `open`，用户仍可手动收起；带终止条件避免自触发）。
5. **"恢复上次检索"对真实点击路径一直是死的**。恢复逻辑写在 `go()` 里，而 dock 的「检索」是 `<a href="#/search">`（导航项只能是 a，否则中键/复制链接全丢），点击不经过 `go()`。实测点一次 dock：`#/search`、检索框空。移到所有 hashchange 都经过的 `syncRoute()`，并用 `location.replace` 避免历史里留下裸条目导致退不出去。
   **更值得记的一笔**：`test_architecture.py:267` 原来钉的是字符串 `location.hash = prev`——守卫把"逻辑写在 `go()` 里"当成了契约，所以功能死了很久而守卫一直绿。已改成钉"恢复在 `syncRoute` 这个收口点上"，并对四种变异逐个验红。
6. **库设置页的徽章与同一行的生效值互相打脸**。保存"用系统默认值"成功后，界面同时显示「当前生效 512 · 重叠 12.5%」和「本知识库自定义」。根因是前端拿 `plan.own.custom` 当"是否自定义"，而两级模型下每个库都持有具体数值（`storage/repos/kbs.py:90-97` 明确"不留 chunk_size=0 表示继承"），该字段恒为真；后端早已弃用它——`plan_custom` 改为"与系统默认相比，容差 0.05"（`api/kbs.py:18-31`）。接口侧也能看到两份答案并存：`GET /api/kbs/{id}` 返回 `plan_custom: false`，`GET /api/kbs/{id}/plan` 返回 `own.custom: true`。前端统一到后端口径，并把文案从"继承系统默认"改成"与系统默认一致"——勾选保存只是把当前默认**抄**进本库，日后改系统默认不会传到这里，写"继承"就是承诺一个后端做不到的联动。
7. **空分块大小会被静默写成"恢复继承"**。取消勾选继承 + 大小留空 → 前端发 `chunk_size: 0`，而 `schemas.py:122` 写明 0 = 继承：用户想要自定义，得到的是继承，界面还回一句"分块方案已保存"。补前置校验（64–8192 / 0–50，界幅取自 `storage/plan.py:35-36` 不另立一套），实测拦下后**没有**发出 PUT。
8. **`effective` 是后端派生值，不能在前端拼**。就地更新只补 `own` 时，"当前生效"停在 600 而徽章和输入框都已是新值。改为保存后重取 `/plan`（`effective` 还带着 `overlap_chars`、`source`，前端重算等于把切分口径实现第二遍），但**只重取本面板**，不整页重载。实测：`当前生效 700 字符 · 重叠 15%` + 徽章一致 + 脏标记清零。

另外收敛了两处同源风险：`go()` 现在归一化 `#/…` 与 `/…` 两种写法（否则 `href()` 与手写调用在"裸 #/search 恢复"上行为不同）；`KbSettings` 的快照改由 `baseSnap()/planSnap()` 唯一计算——此前 `load()` 内联拼一次、`snapshotPlan()` 按另一套字段拼一次，"脏"取决于上一次走的是哪条路。

### 13.3 端到端实测（隔离实例 8131，data 在 `/tmp/raggi-ui-s6`）

你的 `data/` 与 8127 上的运行实例未被触碰；模型连通性测试**没有点击**（会用到你配置的 key，且 Ollama 常不在线）。

| 链路 | 观测 |
|---|---|
| 回滚全流程 | 手输 `chunk` 时确认按钮仍禁用，输入完整表名才可点 → `POST /api/rollback` 200 → 成功 toast + 后端 `note` 原文（`仅回滚指定表；请用 /api/health 对账`）→ 版本表 6 行变 7 行且「当前」徽章落到新版本 |
| 设置页脏值 | 未改动：无提示、保存禁用；改一个字符：出现「有未保存的改动」并解禁；**改回原值：提示消失并重新禁用**（说明比的是快照而不是"有没有敲过"） |
| ApiDoc 过滤 | 输入 `chunks` → `#/apidoc?q=chunks`、索引 51→8、卡片 8 张、焦点保持；带 `?q=reindex` 深链进入能回填；`history.back()` 后输入框跟回 `reindex` |
| ApiDoc 跳转 | 点末组索引项 → 该组自动展开并滚动，目标卡片顶边停在 112px（`scroll-margin-top` 生效，未被粘住页头遮住） |
| 库设置页 | 编辑描述只解禁「保存基本信息」，「保存方案」仍禁用；自定义 700/15 保存后生效值、徽章、输入框三处一致 |

### 13.4 守卫与验证

`test_frontend_wiring.py` 从 **23 条增至 32 条**（格式化唯一来源、确认框纯文本、分节脏值与快照唯一 helper、方案表单前置校验、库级自定义口径、数据面板可达与回滚双闸、ApiDoc 的 URL/索引/自动展开/同步依赖）。每条新守卫都做变异验证：把它声称守住的那个改回去，确认会变红。

**两轮变异暴露出两条假绿守卫**，都不是代码问题而是守卫本身有洞：

- `lastSearch()` 与 `location.replace` 的**存在性**断言：把条件改成 `if (false)`、或把 `replace` 只写进注释，守卫照样绿。改成钉"可达的形态"（判断分支文本 + `location.replace(prev)` 调用形式）后，`if(false)`、去掉 `lastSearch()`、`replace→赋值`、去掉 `"/search?"` 变体、导航项改成 `<button>` 五种变异逐个变红。
- `plan.own.custom` 的字面量断言放过了一条等价的 `inherit = !p.own.custom`。改成按**字段名** `own.custom` 查，两条等价写法都能拦。

两次自伤值得留着：

- **注释误伤第 6 次**。新守卫把"确认框不要写 `**Markdown**`"这句说明里的示例当成了违例。`strip_code_comments` 的 docstring 早就写过这类误报的结局是"有人删掉说明或者删掉守卫"——这次是我没先剥注释。
- **变异脚本把变异态留在了工作树里**。第一版脚本按"循环结束后统一还原"写，而备份清单漏了 `DataPanel.svelte`；后来 `router.svelte.ts` 那次也在 `assert` 崩溃前没还原，导致 `location.replace(prev)` 一度是 `location.hash = prev`。现在改成每例 `try/finally` 即时还原并用 sha256 校验还原成功。**守卫的守卫也需要验证**——上一段那条假绿守卫已经演示过一次"看着有锁、其实没锁"。

顺带纠正一次测量误判：一度以为 `#/search` 恢复没生效，实际是浏览器仍在跑**上一个构建产物**（`script.src` 指到 `index-CfhNWYzZ.js`，服务端已返回 `index-BPRyDhoN.js`）。DESIGN §11.3 写过的 index.html 缓存坑，我第三次踩上。以后判断"改动没生效"之前，先核对页面实际加载的产物名。

13.3 之后又补的一处：设置页加了 `data` 标签，但 dock 的「设置」子树仍只列 3 项——那个子页只能先进设置页再点标签才到得了。补成第 4 项，并新增 `test_dock_settings_entries_match_the_page_tabs` 把两份清单钉在一起（这仍是本项目最常见的那类病：同一份清单两处各写一遍）。实测 dock 展开后为「模型设置 / 访问密钥 / 健康与索引 / 数据与版本」四项。

**验证手段的边界**（要如实记下）：这一批的浏览器验证全部是**结构化与行为**的——DOM 查询、无障碍树快照、网络日志。会话内可用的 in-app browser 视口只有 319×293 且没有可见表面（`take_screenshot` 直接返回 `NATIVE_BROWSER_VIEWPORT_UNAVAILABLE`），所以桌面宽度下的双栏观感、配色层级、dock 展开态的排版**这一轮没有做过像素级复核**；窄视口反而顺带验证了 dock 默认折叠 + 展开自救这条路径。`scroll-margin-top` 那条是几何量测（目标卡片顶边 112px），与视口宽度无关。

**最终验证**：`svelte-check` 0 errors / 0 warnings、`npm run build` 通过、全量 pytest **408 passed**（前端守卫 32 + 架构守卫收紧 1）。

**剩余缺口**：模型连通性测试的结构化结论渲染只做了静态与类型验证，没有真实响应可看（需要可用的 embedding/LLM 服务）；`data` 面板的 `indexes` 为空时的形态未测（本机库里总有索引）。

**下一步（阶段 7，可选）**：把 `Search`/`DocView` 里剩下的过程式 DOM 操作抽成 `Presenter`，让 `Svelte` 组件只持有状态。前三阶段的结构问题已清完，这一项不再有交互缺陷支撑，只在代码可读性上成立。

---

## 14. 阶段 7 完成记录（2026-10-05）

**范围**：原计划写的是"把 `Search`/`DocView` 里剩下的过程式 DOM 操作抽成 `Presenter`"。做完普查后**换了形状**：Svelte 5 里对应物是 action，不是又一个持有 DOM 的类；真正的问题也不是"有命令式代码"，而是**同一行为各写一遍后已经漂了**。所以这一步交付的是 `lib/actions.ts`——浏览器副作用的唯一实现点。

### 14.1 普查结果（30 个调用点，逐个定级）

| 定级 | 数量 | 处理 |
|---|---|---|
| 重复实现（已漂移） | 2 | 焦点陷阱 ×2、`revealHit` ×2 → 合并进 `lib/actions.ts` |
| 脆弱/错值 | 4 | `children[i±1]` 找下一个命中、`getElementById("engine")` 读引擎、`getElementById("pdfFrame")`、`form.querySelector("input[name=q]")` → 改状态/`bind:this` |
| 合理的命令式（保留并写明理由） | 9 | 主题写 `<html>`、下载造 `<a>`、`window.innerWidth` clamp、`visibilityState` 避让、`signal` abort、`activeElement` 判定、剪贴板选区、`window` 级 hashchange/keydown |
| 壳层状态误用 DOM | 1 | `document.body.classList.add("is-resizing")` → `class:is-resizing={resizing}` + CSS 选择器搬到 `.shell` |

### 14.2 交付

`lib/actions.ts`：`FOCUSABLE` / `focusables` / `trap` / `focusFirst` / `reveal` / `pinBottom` / `selectText` / `syncScroll` / `isTypingTarget`。DocView 的 `syncScroll` 也搬了进去——**留在视图里就等于在边界上开例外，而例外清单正是漂移的起点**。迁移点：`ConfirmDialog`、`FormDialog`、`Search`、`DocView`、`ApiDoc`、`AnswerPanel`、`UploadPanel`、`App`。

### 14.3 验证时发现并修掉的 6 件事

1. **确认框的键盘曾挂在面板元素上**。`ConfirmDialog` 用 `onkeydown={onkeydown}`（面板），`FormDialog` 挂在 window 并写了理由："焦点在不在面板内不该决定对话框是否可操作"。挂面板的那个有前提"焦点一直在面板里"，而提交中按钮被禁用/移除时这个前提就不成立了——**框既关不掉也走不出去，而它正盖着整屏**。实测（焦点强制在面板外派发 Escape）：`closedBecauseEscapeFromOutside=true`；Tab 从面板外被拉回环内、从末位环绕到首位。
2. **初始焦点会送给一个 disabled 按钮**。`requireTyping` 未填对时确认按钮是禁用的，而旧代码非破坏性框一律 `confirmBtn?.focus()`——`focus()` disabled 元素**静默失败**，焦点留在 body。现在按策略选：要手输→输入框，破坏性→取消，其余→确认。实测删除库对话框打开时焦点在"输入 阶段7回归库 以确认"的输入框里。
3. **`AnswerPanel` 的贴底从来没跟过**。`$effect(() => { if (running && el) el.scrollTop = el.scrollHeight })` 同步读了 `running` 和元素，**没读正文**，所以只在流式开始那一刻滚了一次——那时还没有字。改为 `use:pinBottom={{ follow: running, text: answer }}`，`text` 变化才算"新字到了"（否则无关重渲染会把往回翻找引用的用户弹回底部）。
4. **检索方向键靠 DOM 位置找下一个命中**：`e.currentTarget.parentElement.children[i ± 1].querySelector("button")`。模板多包一层或某条命中不渲染按钮就跳错行，且不报错。改成 `hitEls[chunk_id]` + `await tick()`。实测：`sel 0→1`，焦点落到第二条（标题"布偶猫笔记 1"）。
5. **解析引擎的值可能不是界面上那个**。`<select id="engine">` 没有绑定，提交时 `document.getElementById("engine")?.value ?? "auto"`——元素没找到与"值就是 auto"长得一模一样，用户选 docling、实际入库 auto。改成 `bind:value={engine}`。
6. **我自己的重构把跳转弄坏了**（这条最值钱）。把 `epEls` + 手动滚动改成 `use:reveal` 之后，从**折叠分组**点索引：flash 出现但页面不动（`top=8651, scrollTop=0`）。原因是折叠用 `hidden` 实现，卡片元素一直挂载着，"解除 hidden"和"改 key"在同一次 flush 里，`scrollIntoView` 落在一个还没有布局盒的元素上。第一反应是"下一帧再滚"（`requestAnimationFrame`），**但这个环境里 rAF 根本不触发**——隐藏文档不跑动画帧；这不是测量噪声，后台标签页里它就是静默不滚。最终形态：调用方 `await tick()` 等渲染完成再改 key，`reveal` 保持同步滚动并在 `prefers-reduced-motion` 下退回即时定位。实测（用桩 `matchMedia` 走即时分支）：`scrollTop 0→8495`，目标顶边 112px（`scroll-margin-top` 生效）。

### 14.4 守卫与验证方法

- `test_frontend_wiring.py` **32 → 33 条**，新增 `test_dom_side_effects_live_in_one_place`：视图/组件里禁止 `getElementById` / `querySelector` / `scrollIntoView(` / `classList.add|remove` / `createRange` / `getSelection` / `.scrollTop = `；可聚焦清单全仓只许一份；两个对话框必须 `use:trap` 且不许把键盘挂回面板；`reveal` 必须尊重 reduced-motion；`UploadPanel` 的引擎必须有状态来源。
- **两条旧守卫被这次重构正确地拦下**，因为它们钉的是实现位置而不是不变量：`test_apidoc.py::test_copy_has_text_selection_fallback`（要求 `selectNodeContents` 出现在视图文件里）和 ApiDoc 的 `await tick()` 断言。两处都改成跨文件查意图（"失败时真的会选中"、"只等渲染、不查 DOM、不自己滚"）。
- **变异测试又抓到两条假绿**：① `export function focusables` 是 `export function focusablesX` 的**前缀子串**，改名字（等于共享实现没了）守卫照样绿 → 断言加左括号；② `.querySelector(` 匹配不到 `querySelector<HTMLInputElement>(…)` 这种带泛型参数的写法 → 改成禁 `querySelector` 这个词。9 条变异最终逐个变红，逐例 `try/finally` 还原并校验 sha256。
- **本轮起了一个带确定性桩 embedding 的隔离实例**（`build_lc_embeddings` 换成词袋哈希，数据在 `/tmp`，端口 8132），因为阶段 7 要验的三条路径（方向键换命中、高亮滚入视区、深链）都必须有**真实命中结果**。这也补上了阶段 4 遗留的一项：现在有数据可看检索→检视器链路。
- 环境事实（记下以免下次误判）：这个会话的 in-app browser 是隐藏文档，`press_key` 的按键不送达（改用 `dispatchEvent` 派发同种事件验证处理逻辑，差异只在 `isTrusted`），`take_screenshot` 不可用，且 `behavior:"smooth"` 与 `requestAnimationFrame` 都不推进。所以本轮的滚动/按键证据全部来自结构化测量而非像素。

**验证结果**：`svelte-check` 0 errors / 0 warnings、`npm run build` 通过、全量 pytest **409 passed**、隔离实例（8132，`/tmp/raggi-ui-s7`，已销毁）实测如上；`data/` 与运行中的 8127 实例未被触碰。

**未验证**：文件上传时 `engine` 进入 FormData 的真实提交（需要真实 File）；`pinBottom` 在真实流式输出下的表现（无可达 LLM）——两者的实现与静态契约已验，行为未跑通。

**重写路线至此收尾**：阶段 0–7 全部落地。剩下的都是可选的润色（`<DataTable>` 收编第五张表、KbChunks 的客户端过滤升级为服务端参数），没有已知的交互缺陷在等着修。

---

## 15. 收尾润色（2026-10-05）

用户点头做"可选润色"的两项。做完发现这两项里藏着一个真 bug 和一个真能力缺口——润色从来不是只看表面。

### 15.1 分块内容过滤下推到服务端

`GET /api/chunks` 原来只有 `doc_id/kb_id/only_standalone/limit/offset`，所以分块页只能在**已取回的那一屏**里筛，文案被迫写"在已加载的 N 块中匹配 M 块"。现在加了 `q`，口径与 `GET /documents` 完全一致：**分页前过滤，`total` 是过滤后的块数**，大小写不敏感。

改动要点：
- `rag/api/chunks.py`：新增 `q`；把列清单抽成 `CHUNK_LIST_COLS`（有了第二条取数路径之后，列清单写两遍迟早不一致——少一个 `offset_valid`，前端高亮就静默不生效）。**顺带拆掉一个哑雷**：原来的局部变量就叫 `q`（`q = ctx.store.chunks.search()`），加参数若不改名，赋值会把过滤词本身覆盖掉，而且类型都是对象/字符串，运行时不报错。
- 前端 `KbChunks`：debounce 300ms 后走服务端，删掉本地那遍 `items.filter(...)`（两处各筛一次迟早给出两个答案），请求参数用 `lastQuery` 而不是 `filter`——后者可能是用户刚敲下、还没提交的半个词。
- 文案随之改口：`匹配 N 块`、`已加载命中 X / Y 块`，0 命中时说"没有匹配的分块"并给出「清空过滤」。

真实验证（桩实例 8133，210 个独立分块）：`q=猫` 返回 `total=3`；`limit=2&offset=0` 给 c1/c3，`offset=2` 给 c4——**第三个命中只能靠翻页拿到，这正是客户端过滤做不到的那件事**。界面上筛「批量便签 203」得到 1 行 + 「匹配 1 块」，清空回到 5 行，全程焦点在过滤框里没丢。

### 15.2 分页块只留一份，顺手抓出 DocView 的假追加

四张表的「再加载」块是四份复制，且已经漂了：已加载数三处 `pager.loaded`、Jobs 用 `rows.length`；`aria-busy` 只有 KbDocs 那份写了（同一个动作四个可访问性）；`Math.min(size, remaining)` 抄了四遍。合并成 `ui/ListMore.svelte` 时逐块对照，发现 DocView 那块不只是重复——

**DocView 的「再加载 N 块」是假的。** `load()` 没有 append 参数，写死 `offset=0` 并**替换** `chunks`，而 `pager.advance` 照常累加。实测 520 块的文档：点一次只是把第一页重新拉一遍，标签却会显示"已加载 400 / 520 块"，**第 520 块永远到不了**。修法是 `load(append=false)`：追加时用 `offset=pager.offset`、`chunks = [...chunks, ...r.items]`，并且不重取文档与方案、不动选中项与多选、不重跑深链消费。

修复后的实测（520 块文档）：初始 200 行 / 「已加载 200 / 520 块」→ 点「再加载 200 块」→ 400 行、首行仍是第 1 段（证明是接上而不是替换）→ 点「再加载 120 块」→ 520 行、末行"第520段"、分页块自动消失。

`Math.min(size, remaining)` 在第二次点击时给出的正是 120 而不是 200，这条公式现在只有一份。`limit += 200` 那条旧教训（服务端把 limit 夹在 500，第三次点击被静默压回）搬进了 `ListMore` 的注释，不会因为复制块被删掉而失传。

### 15.3 一个测量教训（又一次）

我一度以为 `Pager` 的字段是响应式的——**它们不是**：`offset`/`total` 是普通字段，界面读 `pager.total` 没有建立任何依赖，只有别的 `$state` 变了、模板顺带重渲染时才跟着更新。实测表现就是筛到 3 块却写着"匹配 5 块"。改成 `= $state(0)` 之后标签立刻正确。这类 bug 最阴的地方在于**它会偶然正确**：加一个无关重渲染就"修好了"，而根因还在。已加 `test_pager_fields_are_reactive`。

顺带纠正一次我自己的误判：看到 520 块全部渲染成 `<tr>` 时我以为窗口化失效，实际是这个会话的浏览器视口只有 319px 宽，触发了双栏的窄屏文档流回退——面板本身不滚（`clientHeight == scrollHeight == 18408`），`windowOf` 于是算出"全部可见"。这是设计里的回退行为，不是 bug。

### 15.4 守卫与验证

- 新增 4 条：`test_chunk_content_filter_is_server_side`（架构侧，同时钉后端参数、前端用法、文案口径与接口文档）、`test_pager_fields_are_reactive`、`test_empty_state_cannot_swallow_the_filter_bar`、`test_pagination_block_is_shared_and_really_appends`；`tests/test_api_new.py` 新增一条跨页过滤的行为用例。
- **两条守卫一开始是假的**，都被变异测试打出来：① 定位接口文档条目时用了带闭合引号的 `summary: "…）"`，而真实文案末尾有句号，导致 `index()` 抛异常、整条守卫在干净代码上就红了；② 排序锚点写成 CSS 形式的 `.chunkfilter`（它在文件末尾的 `<style>` 里），于是"工具栏在空态之前"这个判断永远反向。修正后 8 条变异逐个变红，每例 `try/finally` 还原并校验 sha256。
- `KbChunks` 的空态分支原本只判 `items.length === 0`，把过滤框连同「清空过滤」一起换成"这个库里还没有分块"（明明有 890 块）——而它下面那个专为 0 命中写的分支因此**永远不可达**。改成 `items.length === 0 && !filter.trim()`，实测 0 命中时过滤框、`匹配 0 块`、清空按钮都在。

**验证结果**：`svelte-check` 0/0、`npm run build` 通过、全量 pytest **414 passed**（前端守卫 36 条）。桩实例 8133 与 `/tmp/raggi-ui-s8` 已销毁，`data/` 与 8127 上的实例未被触碰。

**仍未覆盖**：Jobs 的分页在真实大量任务下的表现（本机任务数不足）；`aria-busy` 在请求期间的瞬时值只能静态验证（隐藏视口里两次采样都错过了 in-flight 窗口）。
