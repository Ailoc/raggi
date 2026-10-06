"""前端结构守卫（Svelte 5 + Vite）。

这组测试原本针对「手写 HTML + tsc 直出」的结构：校验 import 可解析、
DOM id 存在、`$()` 的可选链陷阱等。改用 Svelte 后，其中一部分
**由编译器或构建流程接管**，保留它们只会变成噪声：

- import 是否可解析 → Vite/rollup 解析失败就构建失败，构建本身就是断言
- 具名导出是否真的存在 → 同上
- `$("#x")?.addEventListener` 的陷阱 → Svelte 用绑定，不存在手写查询
- index.html 里的 DOM id 清单 → 组件各自拥有自己的标记

因此这里只保留**仍然需要人守的**不变量：可访问性、跨页导航一致性、
构建产物新鲜度、以及被显式移除的设计约束不要回潮。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
SRC = WEB / "src"
VIEWS = SRC / "views"
STYLES_DIR = SRC / "styles"
STYLES = STYLES_DIR / "app.css"
DIST = WEB / "dist"


def read_styles() -> str:
    """拼接 styles/ 下所有 CSS。

    样式层正在从单文件 app.css 拆成 tokens/shell/components。守卫查的是
    **不变量是否仍然成立**（焦点色存在、侧栏令牌没回潮、操作按钮有 z-index），
    不是它写在哪个文件里——所以按目录读，而不是把拆分方案绑死在测试上。
    """
    return "\n".join(
        p.read_text(encoding="utf-8")
        for p in sorted(STYLES_DIR.glob("*.css"))
    )


def strip_css_comments(css: str) -> str:
    """去掉 CSS 注释。

    守卫查的是**规则**，注释里的说明不可能影响渲染。不剥注释的话，
    一句"这里不复用 .rail 那套死令牌"的解释就会把守卫判红——
    而这种误报的唯一结局是有人把说明删掉，或者把守卫删掉。
    本文件里 test_focus_outline_not_removed 早就是这么做的。
    """
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def strip_html_comments(markup: str) -> str:
    """去掉 Svelte/HTML 注释，理由同上（见 test_no_native_confirm_or_alert_in_views）。"""
    return re.sub(r"<!--.*?-->", "", markup, flags=re.S)


def strip_code_comments(text: str) -> str:
    """去掉 HTML 块注释、`/* */` 块注释与整行的 `//` 注释。

    结构守卫查的是**代码**。这一版重写里已经被"注释误伤"绊了三次：
    解释文字里写 `.rail` 判红了侧栏守卫、写 `<Dock>` 判红了折叠守卫、
    写 `limit += 200` 判红了分页守卫。说明性注释恰恰最需要提到被禁的
    写法，所以这类守卫必须先看代码、后看注释——否则每次事故复盘的
    文字都在给下一次误报埋雷。
    只剥"整行 //"是为了不动 `https://…` 这类串里的双斜杠。
    """
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(ln for ln in text.split("\n") if not ln.lstrip().startswith("//"))


def media_blocks(css: str) -> list[tuple[str, str]]:
    """按大括号配对拆出所有 `@media <条件> { ... }`，返回 (条件, 块体)。

    不用正则匹配块体：`.*?` 配上负向前看在这种嵌套结构上一定会骗人
    （实测把某条规则从 1100px 块里删掉，正则照样"命中"——它只是越过
    了右花括号，在后面的文本里找到了那个选择器）。
    """
    out: list[tuple[str, str]] = []
    for m in re.finditer(r"@media\s+([^{]+)\{", css):
        cond = m.group(1).strip()
        i = m.end()
        depth = 1
        start = i
        while i < len(css) and depth:
            if css[i] == "{":
                depth += 1
            elif css[i] == "}":
                depth -= 1
            i += 1
        if depth == 0:
            out.append((cond, css[start:i - 1]))
    return out


def persisted_keys(*sources: str) -> list[str]:
    """取 localStorage.setItem 用到的键名，允许键名写在常量里。

    只认字面量的话，`setItem(WIDTH_KEY, …)` 这种（更好的）写法会被读成
    "什么都没存"——于是守卫在最该生效的配置上反而是空的。
    """
    blob = "\n".join(sources)
    consts = dict(re.findall(
        r'const\s+([A-Za-z_$][\w$]*)\s*=\s*["\']([^"\']+)["\']', blob))
    keys: list[str] = []
    for m in re.finditer(r"localStorage\.setItem\(\s*([^,]+?),", blob):
        arg = m.group(1).strip()
        if arg[:1] in ("\"", "'"):
            keys.append(arg[1:-1])
        else:
            keys.append(consts.get(arg, f"<未解析:{arg}>"))
    return keys


# ---- 构建产物 ----------------------------------------------------------

def test_source_tree_exists():
    """源码目录必须在——否则后面所有静态断言都会变成空转。"""
    assert SRC.is_dir(), "缺少 web/src"
    assert (SRC / "App.svelte").is_file(), "缺少 App.svelte"
    assert (SRC / "main.ts").is_file(), "缺少入口 main.ts"


def test_index_html_loads_svelte_entry():
    """index.html 必须挂载 Svelte 入口，而不是某个已删除的旧产物。"""
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert 'id="app"' in html, "缺少挂载点 #app"
    assert "/src/main.ts" in html, "未加载 Svelte 入口"
    assert "/js/main.js" not in html, "仍指向已废弃的 tsc 产物"
    # 无 JS 时不该白屏：主题脚本之外要有可读内容
    assert 'lang="zh-CN"' in html


def test_build_output_when_present_is_fresh():
    """产物存在时不得比源码旧。

    dist 不入库（见 .gitignore），所以 CI 上可能没有产物；有产物时才校验。
    这条拦住的是「改了源码没重新构建，然后对着旧界面调 bug」。
    """
    entry = DIST / "index.html"
    if not entry.exists():
        pytest.skip("尚未构建（npm run build）")
    sources = [p for p in SRC.rglob("*")
               if p.suffix in (".ts", ".svelte", ".css") and p.is_file()]
    stale = [p.name for p in sources if p.stat().st_mtime > entry.stat().st_mtime]
    assert not stale, f"产物已过期，请重新构建：{sorted(stale)[:6]}"


# ---- 可访问性 ----------------------------------------------------------

def test_focus_outline_not_removed():
    """焦点必须可见：键盘用户要知道自己在哪（WCAG 2.4.7）。

    `outline: none` 本身不是罪——只要该处**真的**有等效替代。允许的例外
    按选择器白名单管理，且附近必须写明为什么，否则就是回退。
    """
    raw = read_styles()
    css = re.sub(r"/\*.*?\*/", "", raw, flags=re.S)

    # 允许的例外：选择器 → 等效替代是什么
    allowed = {
        ".compose:focus": "面板只作程序化聚焦落点，描框会被读成错误状态",
        ".qbar input": "焦点环画在外层 .qbar 的 :focus-within 上",
    }

    found = set(re.findall(r"([^{};/]+?)\s*\{[^{}]*?outline:\s*none", css))
    offenders = []
    for sel in found:
        sel = sel.strip().split("\n")[-1].strip()
        if any(sel.endswith(k) or k in sel for k in allowed):
            continue
        offenders.append(sel)
    assert not offenders, f"这些选择器移除了焦点样式且无等效替代: {offenders}"

    # 每个例外都要在源码里有说明，避免白名单变成"以后随便加"
    for key, why in allowed.items():
        norm = re.sub(r"\s+", "", key)
        assert re.sub(r"\s+", "", raw).count(norm) >= 1, f"白名单里的 {key} 已不存在"
        assert why.split("，")[0][:6] in raw or "等效" in raw, f"{key} 的例外缺少说明"

    assert "--focus" in css, "缺少焦点色令牌"
    assert "input:focus-visible" in css, "输入控件没有 focus-visible 焦点环"


def test_views_use_native_controls():
    """交互元素用原生标签，不用 div 假装按钮。

    回归防护：曾经用 <article role="button"> 做整卡导航，结果中键新开标签、
    右键复制链接、浏览器自带的键盘行为全部失效。
    """
    for f in VIEWS.glob("*.svelte"):
        body = strip_code_comments(f.read_text(encoding="utf-8"))
        assert 'role="button"' not in body, f"{f.name} 用 role 冒充按钮，应改用原生元素"


def test_labels_are_bound_to_controls():
    """输入控件必须有可访问名：label 包裹、id 关联或 aria-label。

    只有 placeholder 不算——它在输入后消失，读屏也读不到。

    必须先剥注释：这条守卫被"说明文字里提到某个标签名"误伤过——
    一段解释 `<select>` 焦点行为的注释，把整个文件判成了"有无可访问名
    缺失的控件"。守卫查的是**渲染出来的控件**，注释里的标签名不会渲染。
    """
    for f in VIEWS.glob("*.svelte"):
        body = strip_code_comments(f.read_text(encoding="utf-8"))
        # 显式关联：<label for="x"> 与控件 id="x" 配对也算有可访问名
        labelled = set(re.findall(r'<label[^>]*\bfor="([^"]+)"', body))
        for m in re.finditer(r"<(input|select|textarea)\b[^>]*>", body):
            tag = m.group(0)
            if 'type="hidden"' in tag:
                continue
            if "aria-label" in tag or "id={p.id}" in tag:
                continue
            idm = re.search(r'\bid="([^"]+)"', tag)
            if idm and idm.group(1) in labelled:
                continue
            # 落在 <label> 里的控件靠包裹获得名字
            before = body[: m.start()]
            if before.rfind("<label") > before.rfind("</label>"):
                continue
            raise AssertionError(f"{f.name} 存在无可访问名的控件: {tag[:70]}")


def test_tables_have_action_column_header():
    """操作列必须有表头（读屏用户要知道这一列是什么）。"""
    for f in VIEWS.glob("*.svelte"):
        body = f.read_text(encoding="utf-8")
        for m in re.finditer(r"<th class=\"actions\"\s*>(.*?)</th>", body, re.S):
            assert "sr-only" in m.group(1), f"{f.name} 的操作列表头缺少可读文本"


# ---- 跨页一致性 --------------------------------------------------------

def test_doc_page_has_back_link():
    """文档页要有返回知识库的入口，而不是只能靠浏览器后退。

    面包屑集中在顶栏下方（App.svelte 渲染，视图用 setCrumbs 写入）：
    各页自拼面包屑时层级顺序与文案很快就开始漂移。
    """
    app = (SRC / "App.svelte").read_text(encoding="utf-8")
    assert 'class="crumbbar"' in app, "缺少面包屑栏"
    assert "crumbs.items" in app, "面包屑栏未渲染共享状态"
    docs = (VIEWS / "KbDocs.svelte").read_text(encoding="utf-8")
    assert "setCrumbs(" in docs, "文档列表未写入面包屑"
    assert '"知识库", href: "#/"' in docs, "面包屑未指回知识库列表"


def test_error_states_offer_action():
    """错误态必须给出下一步，而不是只报错。

    回归防护：后端重启期间打开列表页，只给一句话的话用户只能猜
    是不是要刷新浏览器。
    """
    for f in VIEWS.glob("*.svelte"):
        body = f.read_text(encoding="utf-8")
        if 'kind="error"' not in body:
            continue
        seg = body.split('kind="error"')[1][:320]
        assert "action=" in seg and "onaction=" in seg, \
            f"{f.name} 的错误态没有可执行动作"


# ---- 已移除的设计约束不得回潮 ------------------------------------------

# 已经迁到三态加载模型（lib/loadstate.svelte.ts）的列表视图
MIGRATED_LIST_VIEWS = ("KbDocs.svelte", "KbChunks.svelte", "Jobs.svelte",
                       "KbList.svelte", "DocView.svelte", "Search.svelte")

# 仍在用单一 loading 布尔的视图。**只减不增**，清空后也别留着当摆设：
# 新写的视图若走老路，这条守卫会立刻把它点名出来。
PENDING_LOADING_BOOL: dict[str, str] = {}


def test_migrated_list_views_use_the_three_state_loader():
    """迁过的三个列表视图必须走 loader/Pager，不得退回 loading 布尔。

    回归防护：旧实现把"没有数据"和"数据在更新"合成一个 loading，
    于是 KbDocs 的 2.5 秒轮询每次都重进加载态——表格换成骨架屏、
    过滤工具栏连同输入框被卸载，**用户正在敲的过滤词丢焦点**。
    """
    for name in MIGRATED_LIST_VIEWS:
        body = strip_code_comments((VIEWS / name).read_text(encoding="utf-8"))
        assert "loadstate.svelte" in body, f"{name} 未使用三态加载模型"
        assert "let loading = $state(" not in body, \
            f"{name} 退回单一 loading 布尔：刷新会重挂整个列表"
        # 服务端把 limit 夹在 500（documents.py:283 / chunks.py:49），
        # `limit += 200` 的第三次点击会被静默压回，按钮还在但点了没用
        assert "limit +=" not in body, f"{name} 用 limit 增长分页，会在 500 条处卡死"


def test_loading_bool_allowlist_only_shrinks():
    """还在用 loading 的视图必须与这份带阶段说明的清单完全一致。

    两个方向都要拦住：已迁完的视图退回旧写法（集合变小没被察觉），
    以及新写的视图直接走老路（集合变大）。清单里每一项都写着它
    归哪个阶段处理，所以"还没做"是可见的待办，而不是隐形的默认。
    """
    found = {
        f.name: f for f in VIEWS.glob("*.svelte")
        if "let loading = $state(" in strip_code_comments(f.read_text(encoding="utf-8"))
    }
    assert set(found) == set(PENDING_LOADING_BOOL), (
        "加载态清单与实际不符。新增视图请一并迁到 lib/loadstate.svelte.ts，"
        f"或在 PENDING_LOADING_BOOL 里注明所属阶段。实际: {sorted(found)}")


def test_frontend_uses_a_single_api_prefix():
    """前端只走一种 API 前缀。

    后端把每个路由挂两遍（rag/api/__init__.py:217-218：/api/v1 是正式的，
    /api 是不进 schema 的兼容别名），所以写哪个都能跑——正因如此这个
    不一致一直没被发现：Jobs 用 /api/v1，其余全用 /api。
    只查字符串字面量，注释里提到 /api/v1 说明历史不算违规。

    例外：data/apidoc.ts 是**接口文档数据**，它列的就是对外的正式路径
    /api/v1/*（与 openapi.json 双向比对），那不是"前端在调哪个前缀"，
    所以不在本守卫范围内。
    """
    offenders = []
    for f in list(VIEWS.glob("*.svelte")) + list((SRC / "ui").glob("*.svelte")) \
            + list((SRC / "lib").glob("*.ts")):
        body = strip_code_comments(f.read_text(encoding="utf-8"))
        if re.search(r"[\"'`]/api/v1", body):
            offenders.append(f.name)
    assert not offenders, f"这些文件把前缀写成了 /api/v1: {offenders}"


# ---- 异步任务必须有订阅者 ----------------------------------------------

def test_async_ops_have_a_waiter():
    """`wait=false` 提交的任务必须有人盯到终态。

    回归防护：重新解析 / 重新切分都是异步提交的，接口回来时流水线才刚
    启动。旧实现提交后只 load() 一次，页面就永远停在"仍在处理"——
    **用户只能离开页面再回来**才看得到结果，而当时的提示文案写的却是
    "状态自动更新"。文案承诺了界面做不到的事，比不承诺更糟。
    """
    ops = (SRC / "lib" / "docops.ts").read_text(encoding="utf-8")
    # 两个操作都要把 job_id 交给调用方
    assert ops.count("onQueued?.(r)") >= 2, "reparse/resplit 未把 job_id 交给调用方"

    view = strip_code_comments((VIEWS / "DocView.svelte").read_text(encoding="utf-8"))
    assert "waitForJob" in view, "文档页没有等待任务结束"
    assert "watchJob" in view, "文档页没有订阅重新解析/重新切分"
    # 视图销毁后还在轮询 = 往已卸载的组件里写状态，而且白打接口到超时
    assert "onDestroy" in view and "abort" in view, "任务订阅没有随视图销毁而中止"


def test_pane_layout_defined_once():
    """双栏骨架只能有一份定义。

    回归防护：`.docsplit/.pane/.pane-head/.pane-body` 曾在 app.css 与
    DocView 的 scoped 样式里各写一份。scoped 的 `min-height:320px` 压掉了
    全局的 `overflow:auto`，两栏各自又长出滚动条；断点也一边 1024px
    一边 1100px，1024–1100 之间同一屏幕宽度有两套答案。
    """
    css = strip_css_comments(read_styles())
    for sel in (".docsplit", ".pane-head", ".pane-body"):
        assert sel not in css, f"全局样式里又出现了 {sel}——双栏骨架归 SplitView 管"
    split = (SRC / "ui" / "SplitView.svelte").read_text(encoding="utf-8")
    assert ".pane-body" in split, "SplitView 未拥有双栏骨架样式"

    # 断点只有一处，而且它在壳层（.main.is-full 的窄屏回退），不在组件里。
    # 这里刻意去 app.css 的媒体块里找那条规则：如果哪天它被搬进 SplitView
    # 或者断点又多了第二个值，这条断言会红——比一个永远为真的
    # count("max-width:") <= 1 有用。
    blocks = [(c, b) for c, b in media_blocks(css) if ".main.is-full" in b]
    assert blocks, "窄屏下 .main.is-full 没有回退规则（双栏会困在不滚动的容器里）"
    conds = {c for c, _ in blocks}
    assert conds == {"(max-width: 1100px)"}, \
        f".main.is-full 的回退断点必须只有一个且与 SplitView 一致，实际: {sorted(conds)}"
    view = (VIEWS / "DocView.svelte").read_text(encoding="utf-8")
    assert ".docsplit" not in view, "DocView 又自己写了一套双栏样式"


def test_virtual_row_height_is_measured_not_guessed():
    """虚拟化视口高度必须由测量得到。

    回归防护：DocView 曾写死 `VIEWPORT_H = 560` 并把它同时用作 CSS 的
    max-height 和 windowOf 的视口高，而栏高实际由 CSS flex 链决定——
    两个来源迟早不一致，表现为"滚到底还有行没渲染"或大片空白。
    """
    view = strip_code_comments((VIEWS / "DocView.svelte").read_text(encoding="utf-8"))
    assert "VIEWPORT_H" not in view, "又出现了写死的视口高度"
    assert "clientHeight" in view, "分块表视口高度未从元素测量"


# ---- 检索参数与结果必须一致 --------------------------------------------

def test_search_params_cannot_diverge_from_results():
    """改参数不得静默地让"面板显示的参数"与"结果的来源"不一致。

    回归防护：12 个 onchange 都直接 `go(withParam(...))` 改写 URL，而触发
    检索的 effect 只依赖 `q`——改 top_k / 模式 / 阈值 / 过滤 全都不会重跑。
    屏幕上于是同时存在"参数面板是新值"和"结果集来自旧参数"，没有任何提示。
    这不是慢，是**在说谎**。
    """
    raw = (VIEWS / "Search.svelte").read_text(encoding="utf-8")
    view = strip_code_comments(raw)
    # 控件改动只能进暂存区
    assert "draft[key] = value" in view, "setParam 未写入暂存区，改动会立刻改写 URL"
    # 导航只能发生在提交这一条路上
    assert view.count("go(") == 1, "除提交外还有别处在改写 URL"
    assert "function apply()" in view, "缺少显式的提交入口"
    # 待应用状态必须可见，并给出去除它的出口
    assert "{#if dirty}" in raw, "参数改动没有可见提示"
    assert "应用并重查" in raw and "放弃改动" in raw, "缺少应用/放弃入口"
    # 结果条元信息必须读**已生效**值，否则应用之前就在描述不存在的检索
    assert 'query.get("kb_id")' in view, "结果元信息读了未生效的参数"
    assert 'val("kb_id")' not in view, "结果元信息读了暂存区"


def test_export_goes_through_api():
    """导出报告必须走统一的请求层。

    回归防护：这里曾绕过 api() 直接 fetch，于是拿不到鉴权头也没有统一的
    错误解析——后端一启用密钥，导出就变成裸 401，用户看到的只是一段
    没人读得懂的响应体。
    """
    view = strip_code_comments((VIEWS / "Search.svelte").read_text(encoding="utf-8"))
    assert "apiText(" in view, "导出未走统一请求层"
    assert 'fetch("/api/search/report"' not in view, "导出仍在裸用 fetch"


def test_rail_css_and_tokens_removed():
    """侧栏已整体移除，样式与令牌不得残留。

    回归防护：侧栏折叠后切换入口被一起隐藏，localStorage 又让状态跨刷新
    保留——变成一个用户无法自行恢复的死锁。移除比修补更合理。

    阶段 1 把常驻导航做回来了（.dock），但这条守卫**继续保留**：
    新组件是不同的一套规则（见 test_dock_collapse_is_self_recoverable），
    旧的 .rail 令牌不该以任何形式回来。
    """
    css = strip_css_comments(read_styles())
    assert "--rail-w" not in css, "残留侧栏宽度令牌"
    assert "--rail-handle-w" not in css, "残留侧栏把手令牌"
    assert ".rail" not in css, "残留侧栏样式"


def test_no_native_confirm_or_alert_in_views():
    """破坏性操作不用浏览器原生弹窗。

    原生 confirm/alert 无法量化影响、无法设置焦点、样式不可控，
    且会阻塞渲染线程（轮询期间弹窗会让整个页面卡住）。
    Svelte 视图里应使用组件化对话框。
    """
    for f in VIEWS.glob("*.svelte"):
        body = f.read_text(encoding="utf-8")
        # 去除注释后再查，避免注释里的说明触发误报
        code = re.sub(r"<!--.*?-->", "", body, flags=re.S)
        assert not re.search(r"\bconfirm\s*\(", code), f"{f.name} 使用了原生 confirm"


def test_no_innerhtml_dom_queries():
    """视图不得手动查询 DOM 或整块替换 innerHTML。

    这两件事正是本次重写要消除的：手动查询在元素缺失时抛异常且
    `?.` 兜不住；innerHTML 全量替换会清掉焦点、滚动位置，并让
    元素每次都是新节点——CSS 过渡永远没有起点可插值。
    """
    for f in VIEWS.glob("*.svelte"):
        body = f.read_text(encoding="utf-8")
        assert "document.querySelector(" not in body, f"{f.name} 仍在手动查询 DOM"
        assert "innerHTML = " not in body, f"{f.name} 仍在整块替换 innerHTML"


def test_kb_card_actions_clickable():
    """卡片操作按钮必须抬到「铺满整卡的链接覆盖层」之上。

    回归防护：KbList 曾把删除按钮直接放在卡片头部，而
    .kbcard-link::after（inset:0）是定位元素、画在非定位元素之上——
    点「删除」实际命中的是链接，直接进了知识库。
    """
    view = (SRC / "views" / "KbList.svelte").read_text(encoding="utf-8")
    assert "kbcard-acts" in view, "缺少操作按钮容器"
    css = read_styles()
    assert ".kbcard-foot" in css and "z-index: 1" in css, \
        "操作按钮未抬到链接覆盖层之上（.kbcard-foot 需要 z-index）"


# ---- 常驻导航（dock）必须可自救 ---------------------------------------

def _dock_files() -> dict[str, str]:
    return {
        "App.svelte": (SRC / "App.svelte").read_text(encoding="utf-8"),
        "Dock.svelte": (SRC / "ui" / "Dock.svelte").read_text(encoding="utf-8"),
        "dock.svelte.ts": (SRC / "lib" / "dock.svelte.ts").read_text(encoding="utf-8"),
    }


def test_dock_collapse_is_self_recoverable():
    """常驻导航的折叠态必须能让用户自己恢复。

    回归防护（第一代侧栏）：折叠后**切换入口被一起隐藏**，而折叠态又写进
    localStorage 跨刷新保留 → 用户重开浏览器仍是折叠的，且没有地方可点，
    只能靠改存储自救。那次修复是把侧栏整体移除了。

    现在做常驻 dock，规矩必须先立住，而不是等出事再撤：
      1. 折叠开关归壳层所有，且画在 <Dock> 之前——所以它不可能被折叠藏起来；
      2. 折叠态不跨刷新保留（只允许持久化宽度，宽度撑不死人）；
      3. 宽度必须 clamp 在视口范围内，防止拖出屏幕外找不回；
      4. 开关要播报展开状态，且存在一键恢复默认布局的入口。
    """
    files = _dock_files()
    app = strip_html_comments(files["App.svelte"])
    dock = strip_html_comments(files["Dock.svelte"])
    store = files["dock.svelte.ts"]

    # 1) 开关在 Dock 之外
    assert "<Dock" in app, "dock 未由壳层 App.svelte 渲染"
    assert "dock-toggle" in app, "缺少折叠开关"
    assert app.index("dock-toggle") < app.index("<Dock"), \
        "折叠开关必须画在 <Dock> 之前（壳层），否则折叠时会把自己一起藏掉"
    assert "dock-toggle" not in dock, \
        "折叠开关不得由 Dock 自己拥有——它必须在被折叠的容器之外"

    # 2) 折叠态不得持久化
    persisted = persisted_keys(app, dock, store)
    forbidden = ("collaps", "hide", "open", "expand", "shown")
    offenders = [k for k in persisted if any(f in k.lower() for f in forbidden)]
    assert not offenders, f"折叠态被持久化了: {offenders}（跨刷新保留即成死锁）"

    # 3) 宽度必须 clamp：下界保证可用，上界保证不超出视口
    assert "Math.max" in store and "Math.min" in store, \
        "dock 宽度必须 clamp（Math.min/Math.max），否则能拖到找不回的位置"

    # 4) 展开状态可播报 + 有复位入口
    assert "aria-expanded" in app and "aria-controls" in app, \
        "折叠开关缺少 aria-expanded/aria-controls，读屏用户不知道能否展开"
    assert "reset" in (app + dock + store).lower(), "缺少恢复默认布局的入口"


def test_dock_persists_only_width():
    """dock 落盘的键必须只有宽度那一个。

    上一条管"折叠态不能持久化"，这条管"别把别的 UI 状态偷偷也塞进去"——
    每种跨刷新保留的界面状态都是一个潜在的无法恢复点。
    """
    store = (SRC / "lib" / "dock.svelte.ts").read_text(encoding="utf-8")
    keys = persisted_keys(store)
    assert keys, "dock 宽度应当被记住（拖到合适的宽度后刷新还在）"
    assert not any(k.startswith("<未解析:") for k in keys), f"键名解析失败: {keys}"
    assert all("width" in k for k in keys), \
        f"dock 只应持久化宽度，实际写入: {keys}"


# ---- 阶段 6：展示格式化 / 数据与版本 / 未保存跟踪 ----------------------

def _call_bodies(text: str, call: str) -> list[str]:
    """按花括号配对抽出 `call({ … })` 的调用体。

    用配对而不是正则：块体里有嵌套花括号和跨行模板字符串，
    `.*?` 会越界（本文件 media_blocks 的注释里记过一次同样的坑）。
    """
    out: list[str] = []
    for m in re.finditer(re.escape(call) + r"\(\{", text):
        i, depth = m.end(), 1
        while i < len(text) and depth:
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
            i += 1
        if depth == 0:
            out.append(text[m.start():i])
    return out


def test_dialog_text_is_plain_text():
    """确认框正文里不得出现 Markdown 记号。

    回归防护：`ConfirmDialog` 用 `{cur.body}` 渲染**纯文本**（它必须如此——
    正文里常有表名、文件名等运行时值，能塞 Markdown 就能塞进注入面）。
    阶段 6 新写的回滚确认在这条链上漏了：界面上显示成「且**不可撤销**」，
    最该被看清的一句话反而成了一串星号。
    """
    offenders: list[str] = []
    files = list(VIEWS.glob("*.svelte")) + list((SRC / "ui").glob("*.svelte")) \
        + list((SRC / "lib").glob("*.ts"))
    for f in files:
        # 必须先剥注释：本文件 strip_code_comments 的注释里就写着"这类误报的
        # 唯一结局是有人删掉说明或者删掉守卫"。第一次写完这条守卫时，
        # 它就是被自己那段"不要写 ** Markdown "的说明注释判红的。
        text = strip_code_comments(f.read_text(encoding="utf-8"))
        for call in _call_bodies(text, "confirmDialog"):
            # 只看字符串字面量：JSDoc 里写 **粗体** 是正常用法
            for lit in re.findall(r"`[^`]*`|\"[^\"]*\"|'[^']*'", call, flags=re.S):
                if "**" in lit:
                    offenders.append(f"{f.name}: {lit[:60]}")
    assert not offenders, \
        "确认框正文字符串里出现 Markdown 记号（渲染为纯文本，星号会直接显示）:\n  " \
        + "\n  ".join(offenders)


def _component_files() -> dict[str, str]:
    """视图 + 组件，两条都要扫。

    只扫 views/ 会漏掉 ui/：`fmtMs` 曾经就藏在 AnswerPanel 里，
    而它恰好是本次要收敛掉的那 6 份重复实现之一。
    """
    out: dict[str, str] = {}
    for d in (VIEWS, SRC / "ui"):
        for f in d.glob("*.svelte"):
            out[f"{d.name}/{f.name}"] = f.read_text(encoding="utf-8")
    return out


def test_display_formatting_has_a_single_source():
    """时间与字节的展示格式只能在 lib/format.ts 定义一次。

    回归防护：`fmtTime` 曾在 6 个文件里各写一遍，而且是两种互不相同的
    风格——`slice(0,16).replace("T"," ")` 出 `2026-10-04 12:30`，
    `toLocaleString("zh-CN")` 出 `2026/10/4 12:30:00`，空值一个给 `""`
    一个给 `"—"`。同一站点的两张表并排看像两个产品，而"哪张表的时间
    格式是对的"这种问题根本不该由用户来判断。
    """
    fmt = (SRC / "lib" / "format.ts").read_text(encoding="utf-8")
    for name in ("fmtTime", "fmtTimeShort", "fmtAgo", "fmtBytes", "fmtMs", "fmtSpan"):
        assert f"export function {name}" in fmt, f"lib/format.ts 缺少 {name}"

    offenders: list[str] = []
    for path, body in _component_files().items():
        code = strip_code_comments(body)
        # 本地再写一份 fmt* 就等于第 7 份重复实现
        if re.search(r"(const|function)\s+fmt[A-Z]\w*\s*[=(]", code):
            offenders.append(f"{path}: 自定义 fmt* ")
        if "toLocaleString(" in code or 'replace("T"' in code:
            offenders.append(f"{path}: 就地格式化日期")
    assert not offenders, "展示格式化必须来自 lib/format.ts:\n  " + "\n  ".join(offenders)


def test_unsaved_edits_are_tracked_per_section():
    """有未提交改动时必须看得见，且每节各自判脏。

    两处回归防护：
    1. 设置页保存按钮此前**永远可点**，看不出改了没有——热切换模型是有
       代价的动作，用户需要一个"你还没保存"的信号。
    2. 库设置页两个面板共用一次 `load()` 刷新：先改分块方案、再顺手改
       描述并保存，整页重载把方案面板里未提交的编辑**静默抹掉**。
       所以快照必须按节拆分，保存必须就地更新而不是整页重载。
    """
    settings = strip_code_comments((VIEWS / "Settings.svelte").read_text(encoding="utf-8"))
    assert "savedSnapshot" in settings, "设置页缺少已保存快照"
    assert "$derived(" in settings and "!== savedSnapshot" in settings, "脏判定不是派生值"
    assert "{#if dirty}" in (VIEWS / "Settings.svelte").read_text(encoding="utf-8"), \
        "未保存提示不可见"
    assert "disabled={!dirty}" in settings, "保存按钮未随脏状态禁用"

    kb = (VIEWS / "KbSettings.svelte").read_text(encoding="utf-8")
    code = strip_code_comments(kb)
    for flag in ("baseDirty", "planDirty"):
        assert f"const {flag} = $derived" in code, f"缺少 {flag} 分节脏判定"
        assert "{#if " + flag + "}" in kb, f"{flag} 没有显示成可见提示"
        assert f"!{flag}" in code, f"{flag} 没有用来禁用保存按钮"

    # 快照只能是字段值的纯函数，且写在唯一的那两个 helper 里。
    # 曾经 savedPlan 在 load() 内联算一次、在 snapshotPlan() 又按另一套
    # 字段算一次，两处算法不同 → "脏"取决于上一次走了哪条路。
    assignments = [a for a in re.findall(r"saved(?:Base|Plan)\s*=\s*([^;]+)", code)
                   if "$state" not in a]   # 声明行 `let savedBase = $state("")` 不算写入
    assert len(assignments) >= 4, f"快照写入点异常，实际找到 {len(assignments)} 处"
    for a in assignments:
        assert "Snap(" in a, f"快照写入未经过唯一 helper: {a.strip()}"
    assert "JSON.stringify([inherit" not in code, "又出现内联的 plan 快照"

    # 保存基本信息不得整页重载（会抹掉方案面板的未提交编辑）
    save_base = code[code.index("async function saveBase"):]
    save_base = save_base[:save_base.index("\n  }")]
    assert "load()" not in save_base, "saveBase 又整页重载，会冲掉方案面板的编辑"

    # 拉取期间不能是白屏：这一支曾经整个不存在
    assert 'phase === "initial"' in code, "库设置页缺少加载态分支"


def test_data_and_versions_page_is_reachable_and_guarded():
    """/stats、/versions、/rollback 必须有界面，回滚必须有两道闸。

    回归防护：这三个端点此前**完全没有界面**（`rag/api/system.py:27,35,44`）。
    回滚是单机库最重要的一层自救手段——误删、错误的批量停用、切分方案
    改坏之后，没有它就只能删数据目录重来。而它同时是最危险的操作，
    所以界面要保守：按表回滚要明说，后端返回的 note 要如实显示。
    """
    panel_path = SRC / "ui" / "DataPanel.svelte"
    assert panel_path.is_file(), "缺少数据与版本面板"
    panel = panel_path.read_text(encoding="utf-8")
    code = strip_code_comments(panel)

    for path in ("/api/stats", "/api/versions", "/api/rollback"):
        assert path in code, f"面板未访问 {path}"

    rb = code[code.index("async function rollback"):]
    rb = rb[:rb.index("\n  }")]
    assert "confirmDialog" in rb, "回滚未走组件化确认对话框"
    assert "destructive: true" in rb, "回滚确认未标记为破坏性操作"
    assert "requireTyping" in rb, "回滚需手输表名——按表生效的动作不能靠一次点击确认"
    assert "r.note" in rb, "回滚未显示后端返回的说明（版本按表生效，必须如实转达）"

    # 入口可达：设置页 tabs 里必须有 data 这一节并渲染面板
    settings = (VIEWS / "Settings.svelte").read_text(encoding="utf-8")
    assert re.search(r'\{\s*key:\s*"data"', settings), "设置页 tabs 缺少「数据与版本」"
    assert "<DataPanel" in settings, "设置页未渲染数据与版本面板"

    # 三态与骨架
    assert 'phase === "failed"' in code and 'phase === "initial"' in code, \
        "面板缺加载/失败态（读取失败时会停在骨架屏）"


def test_plan_form_cannot_silently_flip_to_inherit():
    """分块方案表单不得把"留空"写成"恢复继承"。

    回归防护（浏览器实测）：取消勾选「用系统默认值」后把大小清空再保存，
    前端会发 `chunk_size: 0`——而 `rag/api/schemas.py:122` 写明
    "None/0 = 继承全局"。于是服务端把库改回继承，界面却回一句
    "分块方案已保存"：用户想要自定义，得到的是继承，而且要到下一个文档
    入库才看得出来。上下界取自 rag/storage/plan.py:35-36，不自立一套。
    """
    code = strip_code_comments((VIEWS / "KbSettings.svelte").read_text(encoding="utf-8"))
    sp = code[code.index("async function savePlan"):]
    sp = sp[:sp.index("\n  }")]

    # 校验必须在发请求之前
    assert sp.index("64 && s <= 8192") < sp.index('method: "PUT"'), \
        "分块大小校验没有挡在 PUT 之前"
    assert "0 && r <= 50" in sp, "重叠比例未校验（后端限幅 0-50%）"
    assert 'toast("分块大小需在 64–8192 之间' in sp and "return;" in sp, \
        "非法值没有明确告知用户为什么没保存"

    # 恢复继承后要把生效值填回输入框，否则界面同时出现"当前生效 512"和空字段
    assert "size = plan.global.chunk_size" in sp and "ratio = plan.global.overlap_ratio" in sp, \
        "恢复继承后输入框留空，与「当前生效」自相矛盾"

    # 「当前生效」是后端算的（还有 overlap_chars / source），界面只能重取，
    # 不能自己拼——实测过只补 own 的版本：徽章和输入框都变了，这一行还停在旧值。
    assert re.search(r"plan = await api<KbPlan>\(", sp), \
        "savePlan 未重取方案，effective 会是前端自己拼的第二个口径"
    # 而重取只能取本面板：整页 load() 会抹掉基本信息里的未提交编辑
    assert "load()" not in sp, "savePlan 又整页重载，会抹掉另一面板的编辑"


# ---- 阶段 7：浏览器副作用只有一份实现 --------------------------------

def _all_component_sources() -> dict[str, str]:
    """views/ + ui/ + 壳层 App.svelte。

    原来的 `_component_files()` 不含 App.svelte，而壳层恰恰是历史上第二份
    焦点/滚动副作用的常见落点（拖拽时它就直接改过 `document.body.classList`）。
    """
    out = _component_files()
    out["App.svelte"] = (SRC / "App.svelte").read_text(encoding="utf-8")
    return out


def test_pager_fields_are_reactive():
    """Pager 的 offset/total 必须是 `$state`。

    回归防护：它们是普通字段时，界面读 `pager.total` / `pager.more` 没有建立
    任何响应式依赖，只有**别的 `$state` 变了、模板顺带重渲染**时才跟着更新。
    实测表现是筛到 3 块却写着"匹配 5 块"，以及"已加载 X / Y"停在旧值——
    这类 bug 最阴的地方是它会偶然正确：加一个无关的重渲染就"好了"。
    """
    ls = (SRC / "lib" / "loadstate.svelte.ts").read_text(encoding="utf-8")
    cls = ls[ls.index("export class Pager"):]
    for field in ("offset", "total"):
        assert re.search(rf"{field} = \$state\(", cls), \
            f"Pager.{field} 不是 $state，读它的界面不会随取数更新"


def test_empty_state_cannot_swallow_the_filter_bar():
    """筛到 0 命中时不许把过滤框换成"库里还没有数据"。

    回归防护（浏览器实测，210 块的库）：`KbChunks` 的空态分支只判
    `items.length === 0`，于是搜一个不存在的词会得到——标题「这个库里还没有分块」
    （其实有 210 块）、过滤框连同「清空过滤」整块消失，用户既看不见自己筛了什么、
    也没有出口撤销，只能刷新。而它下面那个真正为该情况写的分支因此永远不可达。

    这条不变量在 `KbDocs` 上是成立的（阶段 2 修过一次同类问题），所以两个视图一起钉。
    """
    chunks = strip_code_comments((VIEWS / "KbChunks.svelte").read_text(encoding="utf-8"))
    assert "{:else if items.length === 0 && !filter.trim()}" in chunks, \
        "空库分支必须排除「有过滤词」的情况，否则筛空时过滤框会被换掉"
    # 过滤工具栏要排在"没有匹配"空态之前——否则那个空态一显示，撤销入口就没了。
    # 锚点用 markup 里的 `class="chunkfilter"`：带点的 `.chunkfilter` 是 CSS 写法，
    # 它在文件末尾，拿它比位置会得到永远相反的答案（第一版就是这么写错的）。
    bar = chunks.index('class="chunkfilter"')
    empty = chunks.index("没有匹配的分块")
    assert bar < empty, "过滤框渲染在空态之后，0 命中时用户无法撤销过滤"

    docs = strip_code_comments((VIEWS / "KbDocs.svelte").read_text(encoding="utf-8"))
    assert "{#if items.length === 0}" in docs and "{#if filter}" in docs, \
        "文档页要分清「没有匹配」和「库里没有」两种空"


def test_pagination_block_is_shared_and_really_appends():
    """「再加载」块只许有一份，而且它必须真的追加。

    两条各自有实证：

    1. **四份复制已经漂了**：已加载数三处用 `pager.loaded`、Jobs 用
       `rows.length`；`aria-busy` 只有 KbDocs 那份写了，另外三个按钮在请求期间
       对读屏用户毫无反馈——同一个动作在四个地方有四种可访问性；
       `Math.min(size, remaining)` 抄了四遍。
    2. **DocView 的按钮是假的**：模板里的「再加载 N 块」调 `load()`，而 `load()`
       当时没有 append 参数、写死 `offset=0` 并替换列表。实测 520 块的文档：
       点一次只是把第一页重新拉一遍，`pager.advance` 照常累加，于是"已加载
       400 / 520"显示着、第 520 块却永远到不了。
    """
    views = {n: strip_code_comments((VIEWS / n).read_text(encoding="utf-8"))
             for n in ("Jobs.svelte", "KbChunks.svelte", "KbDocs.svelte", "DocView.svelte")}
    hand_rolled = [n for n, c in views.items() if 'class="listmore"' in c]
    assert not hand_rolled, f"这些视图又自己写了一遍分页块: {hand_rolled}"
    for n, c in views.items():
        assert "<ListMore" in c, f"{n} 未使用共享的分页块组件"
        # 口径统一：已加载数只能来自 pager，不能各数各的
        assert "loaded={pager.loaded}" in c, f"{n} 的已加载数不是来自 pager"

    # 必须剥注释再查属性：`aria-busy` 这个词在这个组件的说明文字里也出现，
    # 不剥的话删掉真实属性守卫照样绿（本仓库第八次栽在同一处）。
    lm = strip_code_comments((SRC / "ui" / "ListMore.svelte").read_text(encoding="utf-8"))
    assert "aria-busy={busy || undefined}" in lm, "共享组件丢了可访问性反馈（原来就只有一份有）"
    assert "Math.min(size, remaining)" in lm, "「再加载多少个」的公式必须只有一处"
    assert "disabled={busy}" in lm, "请求期间按钮必须禁用，否则会被连点成多次追加"

    # append 必须是真的：参数存在、并且用 offset 接在后面
    dv = views["DocView.svelte"]
    assert "async function load(append = false)" in dv, "DocView.load 没有 append 参数"
    assert "chunks = ap ? [...chunks, ...r.items] : r.items" in dv, "追加时不能替换整个列表"
    assert "const offset = append ? pager.offset : 0" in dv, "分页必须走 offset"
    assert "void load(true)" in dv, "分页按钮没有请求下一页"
    # 而"再取一页"绝不能写成"换个大 limit 重取"：服务端把 limit 夹在 500
    for n, c in views.items():
        assert "limit +=" not in c, f"{n} 又用 limit 增长分页"


def test_dom_side_effects_live_in_one_place():
    """浏览器副作用只有一个实现点：`lib/actions.ts`。

    两条实证，不是预防性的洁癖：

    1. **焦点陷阱曾有两份，而且已经漂了。** `ConfirmDialog` 把 Escape/Tab 挂在
       **面板元素**上，`FormDialog` 挂在 **window** 上。挂元素的那个有前提"焦点
       一直在面板里"，而对话框里这个前提不成立——提交中按钮被禁用或被移除，焦点
       就落回 body，于是**这个框既关不掉也走不出去**，而它正盖着整屏。两份的
       选择器也不同：一份排除了 disabled 输入，另一份不排除，而被 focus 的
       disabled 元素会**静默失败**。
    2. **`revealHit` 在 `Search` 与 `DocView` 里逐字相同**（连注释都没改），
       而 `AnswerPanel` 的"贴底"effect 只读了 `running` 没读正文，于是只在流式
       开始那一刻滚了一次空内容——功能看起来实现了，实际从来没跟过。
    """
    actions = (SRC / "lib" / "actions.ts").read_text(encoding="utf-8")
    for name in ("trap", "focusables", "focusFirst", "reveal", "pinBottom",
                 "selectText", "syncScroll", "isTypingTarget"):
        # focusFirst 是 async（它要 await tick），所以两种声明都算。
        # 必须带左括号：`export function focusables` 是 `export function focusablesX`
        # 的子串，只查名字的话改名（= 那份共享实现没了）照样绿。
        assert re.search(rf"export (?:async )?function {name}\(", actions), \
            f"lib/actions.ts 缺少 {name}"

    banned = {
        "document.getElementById": "按 id 全页搜元素：拿不到就是静默跳过（表现为点了没反应）",
        # 查 `querySelector` 这个**词**而不是 `.querySelector(`：写成
        # `form?.querySelector<HTMLInputElement>(…)` 时带泛型参数，括号形式匹配不到，
        # 而它恰恰是最常见的那种写法（变异测试实测漏过一次）。
        "querySelector": "子树里按名字搜元素：改名或包一层就静默拿到 null",
        "scrollIntoView(": "滚动要走 lib/actions.ts 的 reveal",
        "classList.add": "改 class 走 class: 状态绑定",
        "classList.remove": "改 class 走 class: 状态绑定",
        "document.createRange": "选区操作收在 lib/actions.ts 的 selectText",
        "window.getSelection": "同上",
        ".scrollTop = ": "贴底要走 lib/actions.ts 的 pinBottom",
    }
    offenders: list[str] = []
    for path, body in _all_component_sources().items():
        code = strip_code_comments(body)
        for pat, why in banned.items():
            if pat in code:
                offenders.append(f"{path}: {pat} — {why}")
    assert not offenders, "视图/组件里又出现了自己的 DOM 副作用:\n  " + "\n  ".join(offenders)

    # 焦点陷阱的选择器只许有一份：两份必然漂（本次就是漂了的那两份）
    blob = actions + "\n".join(strip_code_comments(v) for v in _all_component_sources().values())
    assert blob.count("button:not([disabled])") == 1, \
        "可聚焦元素清单出现了第二份——这就是漂移的开始"

    # reveal 不许无条件 smooth。实测依据：平滑滚动由动画帧驱动，隐藏文档里
    # 根本不推进（scrollTop 停在 0，目标元素原地不动），reduced-motion 用户
    # 也不该看到它。两种情况都要退回即时定位。
    assert 'want === "smooth" && reduced()' in actions, \
        "reveal 没有按 reduced-motion 退回即时滚动"

    # 两个对话框都必须走同一个 trap，且键盘不许只挂在面板上
    for dlg in ("ConfirmDialog.svelte", "FormDialog.svelte"):
        text = (SRC / "ui" / dlg).read_text(encoding="utf-8")
        assert "use:trap" in text, f"{dlg} 未使用共享的 trap action"
        assert "onkeydown={onkeydown}" not in text, \
            f"{dlg} 又把键盘挂回面板元素：焦点离开面板时对话框将关不掉"

    # 表单值必须有唯一来源（以前引擎是 getElementById 读的，读不到就静默用 auto）
    upload = strip_code_comments((SRC / "ui" / "UploadPanel.svelte").read_text(encoding="utf-8"))
    assert "bind:value={engine}" in upload, "解析引擎没有状态来源，提交值可能与界面显示不一致"


def test_kb_plan_badge_uses_the_backends_custom_definition():
    """知识库的"自定义/默认"必须按后端口径判断，文案不得说成"继承"。

    回归防护（浏览器实测）：保存"用系统默认值"之后，同一块面板里
    「当前生效 512 · 重叠 12.5%」和徽章「本知识库自定义」并排出现。
    原因是前端拿 `plan.own.custom` 当"是否自定义"——而两级模型下每个库
    都持有具体数值（rag/storage/plan.py:140-142、storage/repos/kbs.py:90-97
    明确"不留 chunk_size=0 表示继承"），那个字段恒为真。后端早已改判据：
    `plan_custom` = 与系统默认相比（容差 0.05，rag/api/kbs.py:18-31）。
    同一个概念有两套答案时，界面上的那一个必须跟着权威定义走。
    """
    code = strip_code_comments((VIEWS / "KbSettings.svelte").read_text(encoding="utf-8"))
    # 按**字段名**而不是 `plan.own.custom` 这种带变量名的写法来查：
    # 变异测试发现，写死前缀的断言放过了一条等价的 `inherit = !p.own.custom`。
    assert "own.custom" not in code, \
        "又用 own.custom 判断知识库是否自定义：两级模型下它恒为 true"
    assert "isCustomPlan(plan.own, plan.global)" in code, "未按后端 plan_custom 的判据比较"
    assert "0.05" in code, "与后端 plan_custom 的容差(0.05)必须一致，否则两处判定会分叉"

    # "继承"这个词本身就是错的：勾选保存只是把当前默认抄进本库，
    # 日后改系统默认不会传到这里。文案承诺了后端做不到的联动。
    for name in ("KbSettings.svelte", "KbList.svelte", "KbDocs.svelte"):
        text = strip_code_comments((VIEWS / name).read_text(encoding="utf-8"))
        assert "继承系统默认" not in text, \
            f"{name} 用「继承系统默认」描述了一个一次性的数值抄写"


def test_last_search_restore_sits_on_the_hashchange_path():
    """恢复上次检索必须写在 syncRoute 里，而不是 go() 里。

    回归防护：dock 的「检索」是 `<a href="#/search">`——导航项只能是 a，
    否则中键新开、复制链接、状态栏预览全丢（DESIGN 里已有这条）。而 `<a>`
    的点击不经过 `go()`，所以恢复逻辑写在 go() 里时，**对真实点击路径一直是
    死的**：地址栏停在裸 #/search，检索框是空的，用户以为上次检索丢了。
    """
    router = strip_code_comments(
        (SRC / "lib" / "router.svelte.ts").read_text(encoding="utf-8"))
    sync = router[router.index("export function syncRoute"):
                 router.index("export function rememberSearch")]
    go_body = router[router.index("export function go"):]
    go_body = go_body[:go_body.index("\n}")]

    # 只查"字段名出现过"会被证明是空的：把条件改成 if(false)，lastSearch()
    # 仍然躺在死分支里，守卫照样绿。所以钉的是**可达的形态**。
    assert 'bare === "/search" || bare === "/search?"' in sync, \
        "syncRoute 里没有同时判断两种裸 #/search 形态（#/? 与 #/ 的差异见 router 注释）"
    assert "lastSearch()" in sync and "location.replace(prev)" in sync, \
        "syncRoute 未恢复上次检索（每条 hashchange 都该经过这里）"
    assert "lastSearch()" not in go_body, \
        "恢复逻辑又搬回 go()：<a href> 的点击路径不经过它，等于没修"
    # 用赋值而不是 replace 会在历史里留下裸 #/search：后退一格又被恢复成
    # 同一次检索，用户退不出去
    assert "location.hash = prev" not in sync, "恢复必须 replace，否则会造出退不出去的历史"

    # 也不能反过来"修"——把导航项改成 button + go() 就能让旧逻辑生效，
    # 但那会把 <a> 带来的中键/复制链接能力整体赔进去
    switch = (SRC / "ui" / "SectionSwitcher.svelte").read_text(encoding="utf-8")
    assert '<a class="section-item" href={s.href}' in switch, \
        "导航项不再是 <a href>：中键新开与复制链接会丢"


def test_dock_settings_entries_match_the_page_tabs():
    """dock 的设置子树必须和设置页的标签一一对应。

    这是本项目反复出现的那类漂移：同一份清单在两处各写一遍。设置页加了
    「数据与版本」之后，dock 仍只列 3 项——于是这个子页只能先进设置页、
    再点标签才到得了，而 dock 声称自己是"每个 section 的第二栏"。
    """
    def keys(path: Path, marker: str) -> set[str]:
        blob = strip_code_comments(path.read_text(encoding="utf-8"))
        seg = blob[blob.index(marker):]
        seg = seg[:seg.index("];")]
        return set(re.findall(r'key:\s*"([\w-]+)"', seg))

    tabs = keys(VIEWS / "Settings.svelte", "const tabs = [")
    dock = keys(SRC / "ui" / "Dock.svelte", "const settingsLinks = [")
    assert tabs and dock, "没读到两份清单，守卫别变成空转"
    assert tabs == dock, f"dock 与设置页子页不一致: tabs={sorted(tabs)} dock={sorted(dock)}"
    # 每一项还得真的指向对应路由（标签是模板 `#/settings/{t.key}`，dock 写死）
    dock_raw = (SRC / "ui" / "Dock.svelte").read_text(encoding="utf-8")
    missing = [k for k in dock if f'#/settings/{k}' not in dock_raw]
    assert not missing, f"dock 里有对不上路由的设置项: {missing}"


def test_apidoc_filter_is_in_url_and_index_shares_its_source():
    """API 文档的过滤词进 URL，且索引与卡片必须同源。

    三处回归防护：
    1. 过滤词只存内存：调好一次过滤，刷新就没了，也没法把"所有 /chunks
       相关端点"这种视图分享给别人——这一页本来就是给人查的。
    2. 索引渲染 `GROUPS`（全量）而卡片渲染 `shown`（已过滤）：搜一个词，
       卡片只剩 3 个、索引仍列 51 个，点索引跳到一个已被过滤掉的端点。
    3. 跳转靠 `setTimeout(..., 50)` 赌渲染完成，机器慢一点就"点了没反应"。
    """
    page = (VIEWS / "ApiDoc.svelte").read_text(encoding="utf-8")
    code = strip_code_comments(page)

    # 1) URL 是过滤词的真相来源之一
    assert "query: URLSearchParams" in code, "页面未接收 URL 查询参数"
    assert re.search(r'go\(\s*want\s*\?\s*"/apidoc\?q="', code), "过滤词未写进 URL"
    assert "encodeURIComponent" in code, "过滤词写进 URL 前未编码"
    # 双向：外部改 URL（后退/手敲）也要跟随，否则又是一次界面与 URL 不一致
    assert "fromUrl !== pushed" in code, "URL 变化未回写到过滤框"
    # 但不得每敲一个字就改写 hash（后退键会被塞进几十条历史）
    assert re.search(r"const t = setTimeout\(", code) and "clearTimeout(t)" in code, \
        "URL 同步未 debounce"

    # debounce 的依赖必须在**同步阶段**读出来。
    # 回归防护：写成 `const t = setTimeout(() => { const want = filter.trim() … })`
    # 时，effect 建立时没读到 filter，于是它永不重跑——实测地址栏一直停在
    # #/apidoc，看起来像"同步功能压根没做"。svelte-check 不会报这一类错。
    i_want = code.find("const want = filter.trim()")
    i_timer = code.find("const t = setTimeout(")
    assert i_want != -1 and i_timer != -1 and i_want < i_timer, \
        "过滤 effect 的依赖没有同步读出来：filter 变了它不会重跑"

    # 2) 索引与卡片同源
    assert "const indexed = $derived(shown)" in code, "索引未跟随过滤结果"
    assert "{#each indexed as g" in page, "索引未渲染过滤后的分组"
    assert "{#each GROUPS as" not in page, "索引又去渲染全量分组"
    assert "匹配 {shownCount} / {total}" in page, "过滤后未显示命中数/总数"

    # 过滤命中的分组必须自动展开：只有首组默认展开，搜一个命中的是别的组
    # 时卡区会一张都不渲染，只剩"匹配 8 / 51"——实测暴露过一次
    assert "open.has(id)" in code and "persistOpen(new Set([...open, ...ids]))" in code, \
        "过滤命中的分组未自动展开（会出现'有匹配但一张卡片都没有'）"
    # 而自动展开必须有终止条件，否则写 open 会反复触发自己
    assert "if (ids.every((id) => open.has(id))) return;" in code, \
        "自动展开缺少终止条件，会形成写 open 的循环"

    # 3) 跳转不赌时间、不查 DOM，滚动归共享 action
    #
    # 这条原来钉的是 `await tick()` 与 `epEls[`——即"视图自己等渲染再定位"。
    # 阶段 7 把滚动并进 lib/actions.ts 的 reveal 之后两者都不需要了：action 在
    # 元素挂载时就跑一次，"先展开分组再跳过去"根本没有要等的东西。
    # 钉实现细节的守卫会在**正确的重构**上变红（本文件已记过两次这个教训），
    # 所以现在钉不变量：视图里不许出现定时赌渲染、手动查 DOM、自己调滚动 API。
    assert "use:reveal={{ key: flashId" in page, "端点卡片未把滚动交给共享 reveal action"
    assert "scrollIntoView(" not in code, "视图又自己调 scrollIntoView（应走 lib/actions.ts）"
    assert "document.getElementById" not in code, "索引跳转回到手动 DOM 查询"
    jump = code[code.index("function jump"):]
    jump = jump[:jump.index("\n  }")]
    # 旧写法是 setTimeout(…, 50)：赌"50ms 内 Svelte 该渲染完了"。
    # 高亮消散用的 1400ms 定时器不算赌渲染，所以只查跳转这一段。
    assert ", 50)" not in jump, f"跳转路径又用固定延时赌渲染: {jump}"
    # 等渲染**要**有：分组是用 hidden 折叠的，"解除 hidden"和"改 key"同批 flush 时
    # reveal 落在还没有布局盒的元素上（实测：点了只闪不滚）。但只许等渲染，不许查 DOM。
    assert "await tick()" in jump, "跳转未等渲染（折叠分组里的目标不会滚动）"
    assert "flashId = id" in jump and "scrollIntoView" not in jump and "epEls" not in jump, \
        f"跳转应当只改状态、把滚动交给 reveal: {jump}"
