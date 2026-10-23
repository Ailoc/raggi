"""前端结构守卫（React 19 + MUI 6 + Vite）。

迁移前这组断言针对 Svelte 源码；2026-10-10 整体迁移到 React 后逐条翻译，
保留的仍是**需要人守的不变量**——可访问性、跨页导航一致性、异步任务
必须有人等、加载三态、分页口径、展示格式统一、危险操作的能力面。
编译器/构建接管的（import 能不能解析、具名导出是否存在）继续不测：
构建失败本身就是断言。

写法约定（几轮事故换来的）：凡是"在源码文本里搜某个标记"的守卫，
一律先剥注释；文本断言要钉**可达的形态**而不是某段具体代码；关键断言
配变异用例（见 §迁移记录）。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
SRC = WEB / "src"
VIEWS = SRC / "views"
UI = SRC / "ui"
LIB = SRC / "lib"
DIST = WEB / "dist"
PKG = (ROOT / "package.json").read_text(encoding="utf-8")


def strip_comments(text: str) -> str:
    """剥掉 /* */、JSX 注释与整行 `//` 注释。

    结构守卫查的是代码；说明文字里提到被禁的写法是最正常的用法。
    只剥整行 `//`，避免误伤 `https://` 这类字符串。
    """
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"\{\s*/\*.*?\*/\s*\}", "", text, flags=re.S)
    return "\n".join(
        ln for ln in text.split("\n") if not ln.lstrip().startswith("//")
    )


def read(path: Path) -> str:
    return strip_comments(path.read_text(encoding="utf-8"))


def all_sources() -> list[Path]:
    return sorted(list(SRC.rglob("*.ts")) + list(SRC.rglob("*.tsx")))


def component_sources() -> dict[str, str]:
    """视图 + 组件：可访问性与副作用守卫的范围。"""
    out: dict[str, str] = {}
    for d in (VIEWS, UI):
        for f in sorted(list(d.rglob("*.tsx"))):
            out[f"{d.name}/{f.relative_to(d)}"] = read(f)
    return out


# ---- 构建产物与入口 ----------------------------------------------------

def test_react_source_tree_exists():
    """React 入口与壳层必须在；旧 Svelte 源码不得残留。

    迁移期保留 old `.svelte` 是为了回退，收尾后它们只会在"守卫仍绿"的
    假象里腐烂——本文件曾因此整模块 skip。
    """
    assert (SRC / "App.tsx").is_file(), "缺少 App.tsx"
    assert (SRC / "main.tsx").is_file(), "缺少 main.tsx"
    assert not list(SRC.rglob("*.svelte")), "仍有 .svelte 源码残留"
    assert not list(SRC.rglob("*.svelte.ts")), "仍有 .svelte.ts 源码残留"
    assert '"svelte"' not in PKG, "package.json 仍依赖 svelte"
    assert "svelte-check" not in PKG, "package.json 仍保留 svelte-check"


def test_index_html_loads_react_entry():
    """index.html 必须挂载 React 入口，且无 JS 时有可读内容。"""
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert 'id="root"' in html, "缺少挂载点 #root"
    assert "/src/main.tsx" in html, "未加载 React 入口"
    assert 'src="/src/main.ts"' not in html, "仍指向已删除的 Svelte 入口"
    assert 'lang="zh-CN"' in html
    assert "skip-link" in html, "缺少跳转主内容链接"


def test_build_output_when_present_is_fresh():
    """产物存在时不得比源码旧（否则会对着旧 bundle 调 bug）。"""
    entry = DIST / "index.html"
    if not entry.exists():
        pytest.skip("尚未构建（npm run build）")
    sources = [p for p in SRC.rglob("*")
               if p.suffix in (".ts", ".tsx") and p.is_file()]
    stale = [p.name for p in sources if p.stat().st_mtime > entry.stat().st_mtime]
    assert not stale, f"产物已过期，请重新构建：{sorted(stale)[:6]}"


# ---- 可访问性 ----------------------------------------------------------

def test_focus_is_not_removed():
    """焦点必须可见。MUI 组件自带 focus-visible；视图不得再抹掉 outline。"""
    offenders: list[str] = []
    for path, code in component_sources().items():
        if re.search(r'outline:\s*["\']none["\']', code):
            offenders.append(path)
    assert not offenders, f"这些组件移除了焦点环且无等效替代: {offenders}"

    theme = read(SRC / "theme.ts")
    assert ".skip-link:focus" in theme, "跳转链接聚焦后没有出现在视口内"
    assert "CssBaseline" in (SRC / "main.tsx").read_text(encoding="utf-8")


def test_navigation_is_real_links():
    """导航项必须是原生链接：中键新开、复制链接、状态栏预览都要保留。

    回归防护：历史上用 button + go() 假装导航，这三项能力一起丢了。
    """
    app = read(SRC / "App.tsx")
    assert 'component="a"' in app and "href={s.href}" in app, \
        "顶栏导航不是链接"
    tabs = read(UI / "WorkspaceTabs.tsx")
    assert 'component="a"' in tabs and 'href={href("kb", kbId, t.key)}' in tabs, \
        "工作区标签不是可分享的链接"
    nav = read(LIB / "nav.ts")
    assert '"检索", href: "#/search"' in nav, "主导航缺少检索入口"


def test_labels_are_bound_to_controls():
    """输入控件必须有可访问名：TextField 的 label 或 aria-label。

    只有 placeholder 不算——输入后消失，读屏也读不到。
    """
    for path, code in component_sources().items():
        for m in re.finditer(r"<TextField\b", code):
            block = code[m.start(): m.start() + 1600]
            if "label=" in block or "aria-label" in block:
                continue
            raise AssertionError(f"{path} 存在无可访问名的 TextField: {block[:90]}")
        # 原生输入只允许隐藏的文件选择框
        for m in re.finditer(r"<input\b[^>]*>", code):
            tag = m.group(0)
            if 'type="file"' in tag and "hidden" in tag:
                continue
            raise AssertionError(f"{path} 存在裸 <input>，应使用受控 TextField: {tag}")


def test_action_columns_have_headers():
    """操作列必须有可读表头（读屏用户要知道这一列是什么）。"""
    blob = "\n".join(component_sources().values())
    assert blob.count('<span className="sr-only">操作</span>') >= 4, \
        "文档 / 分块 / 任务 / 密钥表格的操作列缺少可读表头"


def test_interactive_rows_do_not_hijack_inner_controls():
    """行内按钮的 Enter/Space 不能被行的 keydown 吃掉。

    React 版：行自身可聚焦，但键盘处理必须先判断事件来源；
    操作区点击要 stopPropagation，否则点编辑会顺带切走选中项。
    """
    view = read(VIEWS / "DocView.tsx")
    assert "tabIndex={0}" in view, "行不可聚焦，键盘交互无从谈起"
    assert "e.target !== e.currentTarget" in view, "未判断事件来源"
    assert "e.stopPropagation()" in view, "行内控件会冒泡触发行点击"


# ---- 跨页一致性 --------------------------------------------------------

def test_subpages_have_a_way_back():
    """库工作区四个子页都要有同一层的切换入口（工作区标签条）。"""
    for name in ("KbDocs.tsx", "KbChunks.tsx", "DocView.tsx", "KbSettings.tsx"):
        view = read(VIEWS / name)
        assert "setCrumbs(" in view, f"{name} 未写入面包屑"
    app = read(SRC / "App.tsx")
    assert "WorkspaceTabs" in app, "壳层未渲染工作区标签条"
    assert 'route.name === "kb" || route.name === "doc"' in app, \
        "标签条未覆盖文档详情页（进去后就没有回来的地方了）"


def test_error_states_offer_action():
    """错误态必须给出下一步，而不是只报错。"""
    for path, code in component_sources().items():
        for m in re.finditer(r'severity="error"', code):
            seg = code[m.start(): m.start() + 700]
            assert "重试" in seg or "重新检测" in seg, \
                f"{path} 的错误态没有可执行动作"


# ---- 加载三态 / 异步任务 ------------------------------------------------

def test_migrated_list_views_use_the_three_state_loader():
    """列表视图必须区分 initial / ready / failed。

    回归防护：单一 loading 布尔让轮询每次都重挂表格与工具栏，用户正在
    敲的过滤词每 2.5 秒丢一次焦点。
    """
    for name in ("KbDocs.tsx", "KbChunks.tsx", "DocView.tsx", "KbSettings.tsx", "Jobs.tsx"):
        code = read(VIEWS / name)
        assert '"initial" | "ready" | "failed"' in code, f"{name} 未使用三态加载"
        assert "const [loading, setLoading]" not in code, \
            f"{name} 退回单一 loading 布尔"
        assert "limit +=" not in code, f"{name} 用 limit 增长分页，会在 500 处卡死"


def test_async_ops_have_a_waiter():
    """`wait=false` 提交的任务必须有人盯到终态，并在视图销毁时中止。"""
    ops = read(LIB / "ops.tsx")
    assert ops.count("onQueued?.(r)") >= 2, "reparse/resplit 未把 job_id 交给调用方"

    view = read(VIEWS / "DocView.tsx")
    assert "waitForJob" in view and "watchJob" in view, "文档页没有等待并订阅任务"
    assert "jobAbort.current?.abort()" in view, "任务订阅没有随视图销毁而中止"

    docs = read(VIEWS / "KbDocs.tsx")
    assert "waitForJob" in docs, "批量入库没有等待单个文件到终态"
    assert "abortRef.current?.abort()" in docs, "上传轮询没有随对话框卸载中止"
    assert "refreshJobs" in docs and "cancelJob" in docs, "文档列表未订阅/取消任务"


def test_job_cancel_uses_server_wording():
    """取消的能力边界必须转述服务端，不能一律说"已取消"。"""
    cancel = read(LIB / "jobcancel.ts")
    assert 'method: "DELETE"' in cancel and "/api/jobs/" in cancel
    assert "r.message" in cancel, "未转述服务端按 outcome 组织的结果"
    jobs = read(VIEWS / "Jobs.tsx")
    assert "cancelJob(" in jobs, "任务页又自己拼了一套取消请求"
    docs = read(VIEWS / "KbDocs.tsx")
    assert "cancelDocJob" in docs and "job_id" in docs
    assert "停止后" in docs, "入库面板未说明取消的能力边界"


# ---- 已移除的设计约束不得回潮 ------------------------------------------

def test_frontend_uses_a_single_api_prefix():
    """前端只走 /api；/api/v1 是后端兼容别名，不该在调用侧出现。"""
    offenders: list[str] = []
    for f in all_sources():
        if f == SRC / "data" / "apidoc.ts":
            continue
        if re.search(r"[\"'`]/api/v1", read(f)):
            offenders.append(f.name)
    assert not offenders, f"这些文件把前缀写成了 /api/v1: {offenders}"


def test_dead_shell_tokens_stay_removed():
    """三代外壳死掉的令牌不得回来（侧栏/常驻栏都已退役）。"""
    blob = "\n".join(read(f) for f in all_sources())
    for token in ("--rail-w", "--dock-w", "--rail-handle-w", "--dock-collapsed-w"):
        assert token not in blob, f"残留上一代外壳令牌: {token}"


def test_shell_is_single_column():
    """壳层是"顶栏 + 单列内容区"，不得再长出侧边栏。"""
    app = read(SRC / "App.tsx")
    for token in ("Drawer", "with-rail", "ContextRail", "rail-toggle"):
        assert token not in app, f"壳层又出现了侧栏形态: {token}"


def test_full_height_chain_has_no_gap():
    """is-full 页面的高度链每层都要能传下去。

    回归防护：中间层漏 `min-height: 0` 时内容被 overflow:hidden 吃掉，
    只表现为"底部内容看不到"，不会有任何报错。
    """
    app = read(SRC / "App.tsx")
    assert re.search(r"flex:\s*1,\s*minHeight:\s*0", app), \
        "main 缺少 flex:1 + minHeight:0，内容会被裁掉"
    assert 'id="main"' in app


def test_no_native_confirm_or_alert_in_views():
    """破坏性操作不用浏览器原生弹窗（无法设置焦点、样式不可控、阻塞渲染）。"""
    pattern = re.compile(r"\bwindow\.confirm\s*\(|\bwindow\.alert\s*\(|\balert\s*\(")
    for path, code in component_sources().items():
        assert not pattern.search(code), f"{path} 使用了原生 confirm/alert"


def test_no_dom_queries_in_views():
    """视图不得手动按 id/选择器查 DOM，也不得整块替换 innerHTML。"""
    for path, code in component_sources().items():
        assert "document.querySelector(" not in code, f"{path} 仍在手动查询 DOM"
        assert "document.getElementById(" not in code, f"{path} 仍按 id 搜元素"
        assert "innerHTML = " not in code, f"{path} 仍在整块替换 innerHTML"


def test_kb_card_actions_are_outside_the_card_link():
    """卡片操作按钮不能落在铺满整卡的链接覆盖层里（点删除会进库）。"""
    view = read(VIEWS / "KbList.tsx")
    assert view.index("</CardActionArea>") < view.index("<CardActions"), \
        "卡片操作区在 CardActionArea 内，点击会被整卡链接吞掉"


# ---- 兼容 / 参数 / 检索 -------------------------------------------------

def test_search_draft_cannot_diverge_from_results():
    """参数改动先进草稿，提交才写 URL——面板与结果集不能各说各话。"""
    view = read(VIEWS / "Search.tsx")
    filters = read(UI / "SearchFilters.tsx")
    assert "const [draft, setDraft]" in view, "参数没有暂存区"
    assert "应用筛选" in filters and "撤销修改" in filters, "缺少应用/撤销入口"
    assert "dirty" in view and "dirty" in filters, "参数改动没有可见提示"
    assert "setApplied(" in view, "应用动作没有写回本地参数"
    assert "searchUrl" not in view and "withParam" not in read(LIB / "searchparams.ts"), \
        "检索参数仍在写 URL"
    assert 'val("kb_id")' not in view, "结果元信息读了未生效的参数"


def test_export_goes_through_api():
    """导出报告必须走统一请求层（否则拿不到鉴权头与统一错误解析）。"""
    view = read(VIEWS / "Search.tsx")
    assert "apiText(" in view, "导出未走统一请求层"
    assert 'fetch("/api/search/report"' not in view, "导出仍在裸用 fetch"


def test_bare_search_lands_on_empty_composer():
    """裸 #/search 必须是空态输入台；上次查询只存在于用户显式打开链接时。"""
    router = read(LIB / "router.ts")
    assert "rememberSearch" not in router and "lastSearch" not in router, \
        "路由层仍在恢复上次检索"
    assert "location.replace(prev)" not in router
    view = read(VIEWS / "Search.tsx")
    assert "qFromUrl" not in view, "检索词仍在读 URL"
    assert 'query.trim() !== ""' in view, "空态判断没有基于本地查询词"
    assert "newSearch" in view and "raggi:newSearch" in view, "缺少回到空态的入口"


def test_settings_subpages_have_one_entry_list():
    """设置子页入口清单只能有一处：设置页自己的 tabs。"""
    counts: dict[str, list[str]] = {}
    for f in all_sources():
        if f.name == "Settings.tsx":
            continue
        found = set(re.findall(r"#/settings/([a-z]+)", read(f)))
        if len(found) >= 2:
            counts[f.name] = sorted(found)
    assert not counts, f"设置子页清单出现了第二处: {counts}"


def test_apidoc_filter_is_in_url_and_list_shares_its_source():
    """API 文档：过滤词进 URL、左栏与过滤同源、选中态进 URL。"""
    page = read(VIEWS / "ApiDoc.tsx")
    assert "query: URLSearchParams" in page
    assert "encodeURIComponent(want)" in page, "过滤词写进 URL 前未编码"
    assert "fromUrl !== pushed.current" in page, "URL 变化未回写到过滤框"
    assert "setTimeout(" in page and "clearTimeout(t)" in page, "URL 同步未 debounce"
    # 依赖必须在同步阶段读出（写在 setTimeout 回调里 effect 不会重跑）
    assert page.index("const want = filter.trim()") < page.index("const t = setTimeout(")
    assert "const shown = needle" in page, "列表未跟随过滤结果"
    assert "shown.map(" in page, "左栏未渲染过滤后的分组"
    assert "${shownCount} / ${total}" in page, "过滤后未显示命中数/总数"
    assert 'query.get("ep")' in page, "选中态未由 URL 派生"
    assert 'sp.set("ep"' in page, "左栏点击未把选中端点写进 URL"


def test_apidoc_copy_has_fallback_and_live_region():
    """剪贴板不可用时给选区兜底，复制结果对读屏可见。"""
    page = read(VIEWS / "ApiDoc.tsx")
    assert 'id="apiDocLive"' in page and 'aria-live="polite"' in page
    assert "announce(" in page, "未向 live region 播报复制结果"
    assert "selectText(" in page, "复制失败时没有选中文本兜底"
    assert "已选中，按 Ctrl+C 复制" in page
    dom = read(LIB / "dom.ts")
    assert "selectNodeContents" in dom, "兜底实现里不再真的选中文本"


def test_api_code_cards_are_flat_glass():
    """API 文档的代码卡与路径条用平面玻璃，不带材质投影。

    回归防护：库给每块玻璃挂了 `0 16px 48px -8px` 的大范围暗影。代码卡
    上下紧挨着排，上一张的阴影会压到下一张的顶边，看起来像连成一片。
    """
    page = read(VIEWS / "ApiDoc.tsx")
    assert page.count("<GlassPanel flat") >= 2, \
        "代码卡 / 路径条又带上了材质投影"
    css = (UI / "glass.css").read_text(encoding="utf-8")
    assert ".lg-glass-flat" in css, "缺少平面玻璃变体"
    assert "box-shadow: none !important" in css, "平面玻璃没有真正去掉投影"


def test_amber_aliases_stay_retired():
    """过渡别名退役后不得回潮。"""
    blob = "\n".join(read(f) for f in all_sources())
    assert "--amber" not in blob


# ---- 展示格式化 / 未保存 / 存储 -----------------------------------------

def _call_bodies(text: str, call: str) -> list[str]:
    """按花括号配对抽出 `call({ … })` 的调用体（正则处理不了嵌套）。"""
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
    """确认框正文渲染纯文本（渲染器不解析 Markdown）。"""
    offenders: list[str] = []
    for f in all_sources():
        for call in _call_bodies(read(f), "confirmDialog"):
            for lit in re.findall(r"`[^`]*`|\"[^\"]*\"|'[^']*'", call, flags=re.S):
                if "**" in lit:
                    offenders.append(f"{f.name}: {lit[:60]}")
    assert not offenders, "确认框正文出现 Markdown 记号:\n  " + "\n  ".join(offenders)


def test_display_formatting_has_a_single_source():
    """时间与字节的展示格式只能在 lib/format.ts 定义一次。"""
    fmt = (LIB / "format.ts").read_text(encoding="utf-8")
    for name in ("fmtTime", "fmtTimeShort", "fmtAgo", "fmtBytes", "fmtMs", "fmtSpan"):
        assert f"export function {name}" in fmt, f"lib/format.ts 缺少 {name}"

    offenders: list[str] = []
    for path, code in component_sources().items():
        if re.search(r"(const|function)\s+fmt[A-Z]\w*\s*[=(]", code):
            offenders.append(f"{path}: 自定义 fmt*")
        if "toLocaleString(" in code or 'replace("T"' in code:
            offenders.append(f"{path}: 就地格式化日期")
    assert not offenders, "展示格式化必须来自 lib/format.ts:\n  " + "\n  ".join(offenders)


def test_unsaved_edits_are_tracked_per_section():
    """有未提交改动时必须看得见，且基本信息与分块方案各自判脏。"""
    code = read(VIEWS / "KbSettings.tsx")
    assert "savedBase" in code and "savedPlan" in code, "缺少分节已保存快照"
    assert "baseDirty" in code and "planDirty" in code, "缺少分节脏判定"
    assert "有未保存的改动" in code and "方案有未保存的改动" in code, \
        "未保存提示不可见"
    assert "disabled={!baseDirty" in code and "disabled={!planDirty" in code, \
        "保存按钮未随脏状态禁用"
    save_base = code[code.index("async function saveBase"):]
    save_base = save_base[:save_base.index("\n  }")]
    assert "load()" not in save_base, "saveBase 又整页重载，会冲掉方案面板的编辑"


def test_storage_panel_is_reachable_and_versions_stay_retired():
    """存储面板必须可达；版本管理（列表 + 回滚）不得回潮。"""
    panel = read(UI / "DataPanel.tsx")
    assert "/api/stats" in panel
    assert '"initial" | "ready" | "failed"' in panel, "面板缺加载/失败态"

    settings = read(VIEWS / "Settings.tsx")
    assert re.search(r'\{\s*key:\s*"data"', settings), "设置页 tabs 缺少存储分区"
    assert "<DataPanel" in settings, "设置页未渲染存储面板"

    offenders: list[str] = []
    for f in all_sources():
        code = read(f)
        for token in ("/api/versions", "/api/rollback"):
            if token in code:
                offenders.append(f"{f.name}: {token}")
    sys_code = (ROOT / "rag" / "api" / "system.py").read_text(encoding="utf-8")
    for route in ('@router.get("/versions")', '@router.post("/rollback")'):
        if route in sys_code:
            offenders.append(f"system.py: {route}")
    assert not offenders, f"版本管理已整体退役，能力面不得回潮: {offenders}"


def test_plan_form_cannot_silently_flip_to_inherit():
    """分块方案表单不得把"留空"写成"恢复继承"。"""
    code = read(VIEWS / "KbSettings.tsx")
    sp = code[code.index("async function savePlan"):]
    sp = sp[:sp.index("\n  }")]
    assert sp.index("64 && s <= 8192") < sp.index('method: "PUT"'), \
        "分块大小校验没有挡在 PUT 之前"
    assert "0 && r <= 50" in sp, "重叠比例未校验"
    assert "分块大小需在 64–8192 之间" in sp and "return;" in sp
    assert re.search(r"plan = await api<KbPlan>\(", code) or \
        re.search(r"fresh = await api<KbPlan>\(", sp), \
        "savePlan 未重取方案，effective 会变成前端自己拼的第二个口径"
    assert "load()" not in sp, "savePlan 又整页重载，会抹掉另一面板的编辑"


def test_kb_plan_badge_uses_the_backends_custom_definition():
    """"自定义/默认"必须按后端口径（与默认值比较，容差 0.05）。"""
    code = read(VIEWS / "KbSettings.tsx")
    assert "own.custom" not in code, \
        "又用 own.custom 判断知识库是否自定义：两级模型下它恒为 true"
    assert "isCustomPlan(plan.own, plan.global)" in code
    assert "0.05" in code
    for name in ("KbSettings.tsx", "KbList.tsx", "KbDocs.tsx"):
        assert "继承系统默认" not in read(VIEWS / name), \
            f"{name} 用「继承系统默认」描述一次性的数值抄写"


# ---- 分页 / 多选 -------------------------------------------------------

def test_pagination_is_shared_and_uses_backend_total():
    """四个分页视图都交给 MUI TablePagination，count 来自后端 total。"""
    for name in ("KbDocs.tsx", "KbChunks.tsx", "DocView.tsx", "Jobs.tsx"):
        code = read(VIEWS / name)
        assert "<TablePagination" in code, f"{name} 没有共享分页块"
        assert "count={total}" in code, f"{name} 的分页数不是后端 total"
        assert "limit +=" not in code
        assert "< limit" not in code, "分页判断退回了「已取回条数 < 请求上限」"


def test_empty_state_cannot_swallow_the_filter_bar():
    """筛到 0 命中时不许把过滤框换成"库里还没有数据"。"""
    chunks = read(VIEWS / "KbChunks.tsx")
    bar = chunks.index('placeholder="过滤分块内容…"')
    empty = chunks.index("没有包含")
    assert bar < empty, "过滤框渲染在空态之后，0 命中时用户无法撤销过滤"
    assert '${q}' in chunks[empty:empty + 120], "空态没有区分「有过滤词」"
    assert "items.filter((c) => c.text" not in chunks, \
        "前端仍在已取回数组里筛第二遍"


def test_row_selection_lives_in_one_place():
    """多选状态与全选/半选口径只有一份实现（usePicks）。"""
    sel = read(LIB / "selection.ts")
    for signature in ("export function usePicks", "const all = useCallback",
                      "const some = useCallback", "const setAll = useCallback",
                      "const toggle = useCallback"):
        assert signature in sel, f"共享多选缺少 {signature}"
    for name in ("KbDocs.tsx", "KbChunks.tsx", "DocView.tsx"):
        code = read(VIEWS / name)
        assert "usePicks(" in code, f"{name} 没有使用共享多选"
        for banned in ("selAllBox", "toggleAll", ".indeterminate ="):
            assert banned not in code, f"{name} 又自己实现了一遍多选: {banned}"


# ---- 壳层 / 命令面板 / 上传 --------------------------------------------

def test_command_palette_wiring():
    """命令面板：⌘K、跳转走 href、检索意图经 searchintent 交接。"""
    app = read(SRC / "App.tsx")
    assert "raggi:palette" in app and "CommandPalette" in app, "壳层没有接入命令面板"
    assert '(e.key === "k" || e.key === "K")' in app, "⌘K 未注册"
    pal = read(UI / "CommandPalette.tsx")
    assert "href(" in pal, "面板跳转没有走 router 的构造器"
    assert "setPendingSearch(" in pal and "raggi:search" in pal, \
        "面板发起检索没有走 searchintent（检索词不再写 URL）"
    assert 'href("kb", d.kb_id' in pal, "文档跳转缺 kb_id"

    docs = read(VIEWS / "KbDocs.tsx")
    assert "pokeQueue()" in docs, "入库提交后没有立刻刷新队列摘要"
    ops = read(LIB / "ops.tsx")
    assert ops.count("pokeQueue()") >= 2, "重解析/重切提交后没有 poke 队列"


def test_compose_upload_keeps_failures_and_binds_target():
    """入库：多文件、目标库锁定、失败项保留可重试。"""
    view = read(VIEWS / "KbDocs.tsx")
    assert 'fd.append("kb_id", target)' in view, "提交未使用锁定的目标库"
    assert 'fd.append("engine", engine)' in view, "解析引擎没有随请求提交"
    assert "multiple" in view, "文件选择不支持多选"
    assert "succeeded" in view and "!succeeded.has(f)" in view, \
        "未区分成功/失败项（失败的应留在队列里供重试）"
    assert "cancelCurrent" in view, "缺少停止当前入库的入口"
    assert 'key={kbId}' in view, "切换知识库时未重置入库队列"


# ---- 模型 / 健康 / 文档工作台 ------------------------------------------

def test_models_panel_tracks_dimension_and_dirtiness():
    """模型页：维度是危险变更、脏状态可见、测试结果按 kind 分开。"""
    code = read(VIEWS / "Settings.tsx")
    assert "savedSnapshot" in code, "模型配置缺少已保存快照"
    assert "JSON.stringify(models) !== savedSnapshot" in code, "脏判定不是派生值"
    assert "有未保存的改动" in code, "未保存提示不可见"
    assert "变更向量维度" in code and "destructive: true" in code, \
        "维度变更没有量化后果并要求确认"
    assert "testing[kind]" not in code and "testing.embedding" in code, \
        "三类模型的测试状态必须分别记录（一个在跑不该锁死另两个）"
    assert "latency_ms" in code and "sorted_correctly" in code, \
        "连通性结果没有翻译成结论"


def test_health_panel_speaks_unknowns_honestly():
    """健康页：检查没跑成时必须说"未知"，不能当成"没问题"。"""
    code = read(VIEWS / "Settings.tsx")
    assert "/api/reindex" in code and "/api/reconcile" in code, "缺少维护动作"
    assert "checks_failed" in code, "未区分「查到问题」与「无法确认」"
    assert "未知（检查没跑成）" in code, "FTS 未跑成时没有如实标注"
    assert "不能说一致" in code, "嵌入模型未查到却可能被读成一致"


def test_plan_editing_ui():
    """分块方案：库级 + 文档级都能改，且标出「需重切」。"""
    kb_set = read(VIEWS / "KbSettings.tsx")
    assert "/plan" in kb_set and 'method: "PUT"' in kb_set
    ops = read(LIB / "ops.tsx")
    assert "/plan" in ops and 'method: "PUT"' in ops, "文档级方案未写回"
    doc = read(VIEWS / "DocView.tsx")
    assert "分块方案" in doc and "editDocPlanDialog" in doc
    assert "需重切" in doc, "方案变更后未提示需要重新切分"
    docs = read(VIEWS / "KbDocs.tsx")
    assert "documents/plans" in docs, "文档列表未批量读取方案（会产生 N+1）"
    assert "需重切" in docs, "列表未标注「需重切」状态"


def test_document_rename_ui():
    """重命名要能在界面上完成，而不是只能删了重传。"""
    ops = read(LIB / "ops.tsx")
    assert "/api/documents/" in ops and 'method: "PUT"' in ops
    assert "重命名文档" in ops, "缺少重命名对话"
    for name in ("KbDocs.tsx", "DocView.tsx"):
        assert "renameDocDialog" in read(VIEWS / name), f"{name} 缺少重命名入口"


def test_source_panel_is_opt_in():
    """文档页原文按需展开；深链带 chunk 时自动展开；状态同步进 URL。"""
    view = read(VIEWS / "DocView.tsx")
    assert re.search(r'route\.query\.get\("src"\) === "1"', view), \
        "原文面板的初值不再是「URL 带 src=1 才展开」"
    assert 'route.query.get("chunk")' in view, "深链带 chunk 时应自动展开"
    assert re.search(r'queryString\(\{[^}]*src:', view), "切换原文未同步进 URL"
    assert re.search(r"if \(!showSrc \|\| kind !== \"text\"", view), \
        "折叠时仍会去抓原文（一次没人看的请求）"


def test_pdf_frame_fits_container_without_double_scroll():
    """PDF 以 A4 内接矩形定尺寸：iframe 永远完整落在容器里。"""
    view = read(VIEWS / "DocView.tsx")
    assert "const A4_RATIO = 595 / 842" in view
    assert "Math.min(availH, availW / A4_RATIO)" in view
    assert "frameSize.w" in view and "frameSize.h" in view, \
        "iframe 尺寸未由内接矩形下发（回到 CSS 100% 会重新溢出）"
    assert 'minHeight: "60vh"' not in view


def test_chunk_pane_is_bounded_not_stretched():
    """分块面板随内容走，不主动撑满剩余高度。"""
    view = read(VIEWS / "DocView.tsx")
    assert "DataGrid" not in view, "重度表格又回来了（面板会被撑成一屏空白）"
    assert 'aria-label="分块列表"' in view and 'aria-label="原文"' in view
    assert re.search(r'<GlassPanel[^>]*aria-label="分块列表"', view)
    assert "height: \"100%\"" not in view.split('aria-label="分块列表"')[1][:400]


def test_doc_view_chunk_management():
    """分块要能编辑 / 新增 / 删除 / 批量启停。"""
    view = read(VIEWS / "DocView.tsx")
    for label in ("编辑", "删除", "新增分块", "批量停用", "批量启用"):
        assert label in view, f"缺少「{label}」入口"
    ops = read(LIB / "ops.tsx")
    assert "/enabled" in ops, "启停未走 /chunks/{id}/enabled（停用应保留数据）"
    assert "force: true" in ops, "解析分块编辑须显式 force，否则后端 403"
    assert 'method: "DELETE"' in ops
    chunks = read(VIEWS / "KbChunks.tsx")
    assert "../lib/ops" in chunks, "独立分块页未复用分块操作"


# ---- API 文档 / XSS ----------------------------------------------------

def test_no_raw_html_injection_of_server_data():
    """任何组件都不得用 dangerouslySetInnerHTML（XSS 面）。"""
    for f in all_sources():
        code = read(f)
        assert "dangerouslySetInnerHTML" not in code, \
            f"{f.name} 使用了原始 HTML 注入"


def test_snippet_renders_marks_natively():
    """高亮必须是原生 <mark>，而不是拼 HTML 字符串。"""
    evidence = read(UI / "EvidenceList.tsx")
    assert "<mark" in evidence, "检索片段未用 <mark> 渲染高亮"
    assert "snippet.marks" in evidence, "未使用后端返回的高亮区间"
    for path in (VIEWS / "Search.tsx", UI / "EvidenceList.tsx", UI / "ChatThread.tsx"):
        assert "escapeHtml" not in read(path), f"{path.name} 仍在手工转义 snippet"


def test_apidoc_inline_markup_is_safe_by_construction():
    """API 文档的行内标记用 React 节点解析，不做 HTML 注入。"""
    page = read(VIEWS / "ApiDoc.tsx")
    assert "function inline(" in page
    assert "dangerouslySetInnerHTML" not in page
    assert "<strong" in page and "component=\"code\"" in page


# ---- 设置入口 / 命令面板收尾 -------------------------------------------

def test_section_table_is_the_single_nav_source():
    """顶栏与命令面板共用 SECTIONS，不各抄一份。"""
    nav = read(LIB / "nav.ts")
    for token in ('key: "kbs"', 'key: "search"', 'key: "apidoc"'):
        assert token in nav, f"nav.ts 缺少 section {token}"
    app = read(SRC / "App.tsx")
    assert "SECTIONS" in app
    pal = read(UI / "CommandPalette.tsx")
    assert "SECTIONS" in pal


def test_queue_polling_is_generation_guarded():
    """队列轮询只能有一条链，StrictMode 的卸载不能留下孤儿定时器。

    回归防护（本轮实测）：React StrictMode 会挂载→卸载→再挂载，第一次
    `queueTick()` 的 promise 在 stop 之后才 resolve；若 `.finally(queueSchedule)`
    不看世代，就会在新会话里再排一条链——两条轮询同时打接口。
    """
    store = read(LIB / "store.ts")
    assert "queueGen" in store, "队列轮询没有世代号"
    assert "gen !== queueGen" in store, "旧世代的 tick/schedule 未被丢弃"
    stop = store[store.index("export function stopQueueWatch"):]
    stop = stop[:stop.index("\n}")]
    assert "queueGen += 1" in stop, "停止轮询没有推进世代"


def test_document_detail_omits_unused_chunk_index():
    """文档详情不再传输前端用不到的分块索引（大文档会成倍放大响应体）。"""
    doc = read(VIEWS / "DocView.tsx")
    assert "include_chunks=false" in doc, "文档工作台仍在拉取未使用的分块索引"
    evidence = read(UI / "EvidenceList.tsx")
    assert "include_chunks=false" in evidence, "原文检视仍在拉取未使用的分块索引"


def test_search_requests_are_cancellable_and_sequenced():
    """快速改参数/后退时，旧检索结果不能覆盖新结果。"""
    view = read(VIEWS / "Search.tsx")
    assert "AbortController" in view and "signal," in view, "检索请求不可取消"
    assert "searchSeq.current" in view, "缺少请求序号，慢的旧响应仍可能覆盖新结果"
    assert "ctrl.abort()" in view, "URL 变化时没有中止上一发请求"


def test_sse_reports_truncated_stream():
    """流在没有 done/error 的情况下结束，必须报连接中断而不是静默成功。"""
    sse = read(LIB / "sse.ts")
    assert "terminal" in sse and "连接已中断" in sse
    assert 'ev === "done" || ev === "error"' in sse


def test_jobs_filter_and_pagination_are_server_side():
    """任务页的筛选/offset 必须进请求，不能在当前页 items 上二次筛。"""
    code = read(VIEWS / "Jobs.tsx")
    assert "&status=${status}" in code, "任务筛选没有下推到服务端"
    assert "offset=${targetPage * PAGE_SIZE}" in code, "offset 没有发给服务端"
    assert "items.filter((j)" not in code, "仍在当前页做客户端筛选"


def test_answer_entry_respects_feature_flag():
    """对话入口必须依据 /api/models 的 features.answer 禁用。"""
    page = read(VIEWS / "Search.tsx")
    composer = read(UI / "SearchComposer.tsx")
    assert "features?.answer" in page
    assert "disabled={!answerEnabled}" in composer
    assert "features.answer=true" in composer, "未启用时没有给出可行动说明"


def test_search_shows_candidate_total_not_just_page_size():
    """检索结果条要区分“显示条数”和“候选总数”，否则 8 条看起来像全部。"""
    code = read(VIEWS / "Search.tsx")
    assert "total_candidates" in code
    assert "显示 {searchResp.results.length} 条" in code


# ---- 液态玻璃 ------------------------------------------------------------

def test_glass_material_does_not_require_webgpu():
    """玻璃材质不得依赖 WebGPU。

    回归防护：上一版用 liquid-glass 的真折射——折射场要由 vgpu/WGSL 渲染，
    没有 WebGPU 的机器上库里一个玻璃面都不画（贴图就绪前还是
    `visibility:hidden`，卡片会整个消失）。open-glass-ui 的默认渲染器是
    纯 CSS，Linux/无 GPU 的机器上照样是玻璃。
    """
    assert "liquid-glass" not in PKG, "又引回了依赖 WebGPU 的 liquid-glass"
    assert "vgpu" not in PKG, "又引回了 WebGPU 运行时"
    engine = read(UI / "glass.tsx")
    assert 'renderer="auto"' in engine, \
        "玻璃渲染器没有锁定为 auto（auto 才是纯 CSS，且不会偷偷升级）"
    assert "navigator.gpu" not in engine, "玻璃路径里还有 WebGPU 能力判定"


def test_glass_theme_follows_the_app_theme():
    """玻璃系统的明暗必须跟着应用主题走。

    没有主题边界时库的 token 默认按深色算：浅色用户会拿到一块深色玻璃。
    """
    engine = read(UI / "glass.tsx")
    assert "appearance: theme" in engine, "玻璃系统没有跟随应用主题"
    main = read(SRC / "main.tsx")
    assert "GlassProvider" in main, "入口没有挂载玻璃系统的主题边界"
    assert re.search(r"style:\s*\{[^}]*height", engine), \
        "玻璃系统的主题边界没有接管高度，壳层的高度链会多一层"


def test_dialogs_menus_and_tooltips_use_glass():
    """所有浮层都要有玻璃语言：对话框、菜单、下拉、Popover、Tooltip。"""
    engine = read(UI / "glass.tsx")
    theme = (SRC / "theme.ts").read_text(encoding="utf-8")
    css = (UI / "glass.css").read_text(encoding="utf-8")
    assert "GlassDialogPaper" in engine, "没有为 MUI Dialog 提供玻璃纸面"
    assert "PaperComponent: GlassDialogPaper" in theme, \
        "对话框没有全局换用玻璃纸面"
    assert ".lg-dialog-paper" in css, "对话框纸面缺少玻璃材质的收尾样式"
    for cls in (
        ".MuiMenu-paper",
        ".MuiPopover-paper",
        ".MuiAutocomplete-paper",
        ".MuiTooltip-tooltip",
    ):
        assert cls in css, f"{cls} 没有玻璃样式"


def test_topbar_is_glass_and_has_no_search_entry():
    """顶栏是玻璃面，且不再挂「搜索与跳转」按钮。

    回归防护：顶栏从 AppBar 换成玻璃面后，入口按钮一度被顺手留下；
    命令面板本身没删（⌘K 仍然可用），删掉的只是顶栏上那个按钮。
    """
    app = read(SRC / "App.tsx")
    assert "<GlassPanel" in app, "顶栏不是玻璃面"
    assert "搜索与跳转" not in app, "顶栏仍有「搜索与跳转」按钮"
    assert "raggi:palette" in app, "⌘K 命令面板入口丢了"


def test_glass_focus_state_does_not_draw_a_ring():
    """玻璃面的聚焦不许画描边。

    回归防护（两次返工）：
    - 先是 `outline: 2px solid var(--lg-focus, currentColor)`——变量缺失，
      退回正文**字色**，浅色下是一圈黑框（实测 outline-color
      rgb(17,19,21)）；
    - 换成主色后变成一圈蓝色线条，又不是想要的风格。
    现在的做法是让**材质自己**反应（边缘变亮、面板抬起），不画描边、
    不加颜色。输入框的焦点提示是文本光标（MUI 把输入元素的 outline
    归零了，光标就是提示）。
    """
    css = (UI / "glass.css").read_text(encoding="utf-8")
    rule = css[css.index(".lg-composer:focus-within"):]
    rule = rule[:rule.index("}")]
    assert "outline" not in rule, "玻璃面的聚焦又画上了描边"
    assert "--ogui-glass-rim" in rule, "聚焦没有用材质系数表达"


def test_glass_card_hover_ring_color_is_defined():
    """卡片悬停描边的颜色必须真的下发。

    回归防护：样式表里写 `var(--lg-hover-ring, <兜底色>)`，变量一旦缺失
    就用兜底色——深色下是一条几乎看不见的暗环。
    """
    engine = read(UI / "glass.tsx")
    css = (UI / "glass.css").read_text(encoding="utf-8")
    assert '"--lg-hover-ring"' in engine, "悬停描边颜色变量没有下发到主题边界"
    assert "var(--lg-hover-ring" in css, "悬停描边没有用主题下发的颜色变量"


def test_light_glass_look_does_not_clip_the_backdrop_to_white():
    """浅色玻璃的 brightness 必须小于 1。

    回归防护：材质在浅色下的基准是 `brightness(1.12)`，而应用的白底是
    `#fafafa`（250/250/250）——1.12 乘上去三个通道全部越过 255 被**截断**，
    玻璃里透出来的背景变成纯白，卡片塌成一张白卡片（实测卡片内部像素
    (255,255,255)，而它背后的页面是 (194,210,224)）。玻璃看上去跟普通
    界面没有区别。
    """
    engine = read(UI / "glass.tsx")
    m = re.search(r"const LIGHT_BRIGHTNESS = ([0-9.]+)", engine)
    assert m, "浅色玻璃没有单独的亮度常量"
    assert float(m.group(1)) < 1, \
        f"浅色玻璃的 brightness={m.group(1)} 会把背景截断成纯白，玻璃就白做了"


def test_composer_is_one_glass_surface_without_a_colored_backdrop():
    """检索输入台只有一层玻璃，且不再自带局部彩色底。

    回归防护：曾经在玻璃面板背后叠了一层 `GlassBackdrop variant="panel"`
    （蓝紫渐变），输入框看起来像"自带背景色"，与页面其他玻璃面不一致；
    而且兄弟背景层的圆角必须与面板成对维护。现在背景交给壳层统一环境光，
    输入台只保留玻璃本身。
    """
    composer = read(UI / "SearchComposer.tsx")
    assert "COMPOSER_RADIUS" in composer, "输入台的圆角常量丢了"
    assert composer.count("radius={COMPOSER_RADIUS}") == 1, \
        "输入台又铺了兄弟背景层（会带来色偏与圆角维护成本）"
    assert "<GlassBackdrop" not in composer, "输入台又自带了一层局部底色"


def test_cards_and_composer_go_through_the_glass_engine():
    """知识库卡片与检索输入台必须走玻璃材质。

    知识库卡片与检索输入台的环境底色都由壳层统一提供（`App.tsx` 的
    shell 形态），页面自己不再铺局部背景层——两片光叠在一起，顶栏和
    页面就接不上；输入台还会多出一块"自带背景色"的色偏。
    """
    kbs = read(VIEWS / "KbList.tsx")
    assert "<GlassPanel" in kbs, "知识库卡片没有走玻璃材质"
    assert "<Card " not in kbs, "知识库卡片又退回了普通 Paper 卡片"
    composer = read(UI / "SearchComposer.tsx")
    assert "<GlassPanel" in composer, "检索输入台没有走玻璃材质"
    assert "<GlassBackdrop" not in composer, \
        "检索输入台又铺了带颜色的局部底色：应只保留玻璃，背景交给壳层环境光"


def test_shell_provides_the_shared_ambient_backdrop():
    """环境底色由壳层统一提供，顶栏与内容区共用同一片光。

    回归防护：底色曾经各视图自己铺一层，结果顶栏与页面之间出现断层，
    而且同一片光叠了两遍（颜色被叠加得更艳）。
    """
    app = read(SRC / "App.tsx")
    assert 'variant="shell"' in app, "壳层没有提供共用的环境底色"
    for name in ("KbList.tsx", "Search.tsx"):
        code = read(VIEWS / name)
        assert 'variant="shell"' not in code, f"{name} 又自己铺了一层环境底色"
        assert 'variant="page"' not in code, f"{name} 还在用旧的页面底色形态"


def test_ambient_backdrop_drifts_slowly_and_can_be_stopped():
    """环境光要有缓慢漂移，且尊重"减少动态"偏好。

    只允许动 transform（合成层）：动 background-position 会让整屏渐变
    每帧重绘，背景一动态风扇就转起来。
    """
    engine = read(UI / "glass.tsx")
    css = (UI / "glass.css").read_text(encoding="utf-8")
    assert "lg-aurora-blob" in engine, "壳层没有渲染动态光斑"
    assert css.count("@keyframes lg-aurora-drift-") >= 3, "光斑动画周期不足三套"
    assert "will-change: transform" in css, "光斑没有走合成层"
    assert "prefers-reduced-motion: reduce" in css, "缺少减少动态偏好的停用规则"


# ---- 响应式布局 ---------------------------------------------------------

def test_viewport_filling_views_subtract_the_measured_shell_header():
    """填满视口的视图必须用壳层实测顶栏高度，而不是 100% / 100vh-84。

    回归防护：顶栏在滚动区内部且高度随面包屑、工作区标签变化。写死值
    会让工作台多出一个顶栏的高度，页面出现无意义的纵向滚动，检索页的
    浮动输入台也会偏出底部。

    注意区分**外层工作台**与**子滚动层**：外层必须用 FILL_HEIGHT；
    检索页的滚动容器在外层内部，用 height:100% 撑满它是正确的——旧断言
    全局禁止 `height: "100%"`，会把这种合法结构误判成回归。
    """
    app = read(SRC / "App.tsx")
    assert "--shell-header-h" in app, "壳层没有下发顶栏实测高度"
    assert "ResizeObserver" in app, "顶栏高度变化（面包屑/滚动）不会同步"

    search = read(VIEWS / "Search.tsx")
    assert "calc(100% - var(--shell-header-h" in search, \
        "检索工作台没有按剩余视口定高"
    # 空态 / 检索 / 对话三个外层工作台都要用 FILL_HEIGHT
    assert search.count("height: FILL_HEIGHT") >= 3, \
        "有外层工作台退回了 100% 定高（页面会多出一条纵向滚动）"

    api = read(VIEWS / "ApiDoc.tsx")
    assert "calc(100% - var(--shell-header-h" in api, \
        "API 文档没有按剩余视口定高"
    assert "100vh - 84px" not in api, "API 文档又写死了顶栏高度"


def test_scrollable_tables_clip_inside_the_glass_panel():
    """宽表必须裁在玻璃面板内并固定表格布局。

    回归防护：TableContainer 自己会横向滚动，但 Chrome 仍把子内容的
    scrollWidth 算进 main。不裁边时 430px 视口能横向拖动整页、表头跟着
    跑；不固定布局时，小屏几列一起把表格撑到 687px。
    """
    for name in ("KbDocs.tsx", "KbChunks.tsx", "DocView.tsx", "Jobs.tsx"):
        view = read(VIEWS / name)
        assert 'className="lg-clip"' in view, f"{name} 的表格面板没有裁边"
        assert 'tableLayout: "fixed"' in view, f"{name} 宽表未固定布局，窄屏会撑破面板"
    keys = read(UI / "KeysPanel.tsx")
    assert 'className="lg-clip"' in keys and 'tableLayout: "fixed"' in keys
    css = (UI / "glass.css").read_text(encoding="utf-8")
    assert ".lg-clip" in css and "overflow: hidden" in css


def test_topbar_keeps_every_section_reachable_on_narrow_screens():
    """360px 下五个分区要全部可见，不能靠横向滑动才发现设置/API。"""
    app = read(SRC / "App.tsx")
    assert 'display: { xs: "none", sm: "block" }' in app, \
        "小屏品牌文字没有让位，导航会被挤出容器"
    assert 'minWidth: { xs: 0, sm: 72 }' in app, \
        "小屏 Tab 仍保留 72px 最小宽度"


def test_filter_panel_is_a_glass_dialog_not_a_sidebar():
    """完整条件必须是浮动玻璃面板，不能退回右侧 Drawer。

    17 个字段在笔记本高度下塞不进一个窄下拉；居中面板 + 分类纵向分区 +
    组内多列控件既保住功能，也避免侧边栏挤压主内容。纸面必须复用
    GlassFilterPaper（真玻璃），而不是 MUI 默认 Paper。
    """
    filters = read(UI / "SearchFilters.tsx")
    assert "<Dialog" in filters and "Drawer" not in filters, \
        "条件面板又变回侧边栏/抽屉"
    assert "PaperComponent={GlassFilterPaper}" in filters, \
        "条件面板没有使用玻璃纸面"
    glass = read(UI / "glass.tsx")
    assert "GlassFilterPaper" in glass and "usePopoverGlassLook" in glass, \
        "条件面板材质没有和输入台/浮层统一"
    css = (UI / "glass.css").read_text(encoding="utf-8")
    assert ".lg-filter-panel" in css and "overflow: hidden" in css, \
        "玻璃纸面没有裁圆角，头尾会跟着内容滚动"
    assert "应用后写入地址" not in filters, \
        "参数不再写 URL，面板副标题还在说“写入地址”"


def test_kb_scope_picker_is_multi_select_searchable_and_glass():
    """知识库范围必须是一个可搜索的多选浮层，而不是单值下拉。

    回归防护：后端只认单库时，界面上的“多选”会看起来选了两个库、
    实际只查一个。范围以 filters.kb_ids 数组下发，且入口只有一份，
    输入台与条件抽屉共用，避免两处对已选范围的解释漂移。
    """
    picker = read(UI / "KbMultiSelect.tsx")
    for token in (
        "搜索知识库名称或描述",
        'role="listbox"',
        'aria-multiselectable="true"',
        "Checkbox",
        "全部知识库",
        "lg-kb-picker",
    ):
        assert token in picker, f"多选浮层缺少：{token}"

    composer = read(UI / "SearchComposer.tsx")
    filters = read(UI / "SearchFilters.tsx")
    assert "<KbMultiSelect" in composer, "输入台没有接入多选浮层"
    assert "<KbMultiSelect" in filters, "条件抽屉没有接入同一个多选浮层"

    params = read(LIB / "searchparams.ts")
    assert "kbIdsFromValue" in params and "filters.kb_ids = kbIds" in params, \
        "多选范围没有折成 filters.kb_ids"
    assert "GlassPanel" in picker and 'material="regular"' in picker, \
        "多选浮层没有走 open-glass-ui 的玻璃材质，又退回 CSS 白板"
    assert "usePopoverGlassLook" in picker, \
        "多选浮层与输入台用了两套材质，透明度/模糊会明显不一致"
    css = (UI / "glass.css").read_text(encoding="utf-8")
    assert ".lg-glass-popover-paper" in css and "transparent" in css, \
        "Popover 纸面没有让位给玻璃材质，实色背景会盖住玻璃"


def test_retrieval_mode_picker_uses_glass_icons_and_keyboard():
    """召回模式浮层必须与知识库浮层同材质，且每个模式带独立图标。"""
    mode = read(UI / "RetrievalModeSelect.tsx")
    for token in (
        "GlassPanel",
        'material="regular"',
        "usePopoverGlassLook",
        "lg-glass-popover",
        "HubIcon",
        "ScatterPlotIcon",
        "TextSnippetIcon",
        'role="listbox"',
        "ArrowDown",
    ):
        assert token in mode, f"召回模式浮层缺少：{token}"
    composer = read(UI / "SearchComposer.tsx")
    assert "<RetrievalModeSelect" in composer, "输入台没有接入召回模式浮层"
    assert "<Menu" not in composer, "召回模式又退回了普通 MUI Menu（CSS 白雾而非玻璃）"


def test_glass_popover_material_is_shared():
    """两个输入台浮层必须共用同一套玻璃 look，避免再次出现厚白板。"""
    glass = read(UI / "glass.tsx")
    assert "usePopoverGlassLook" in glass, "缺少输入台浮层的共用材质"
    assert "0.15" in glass and "1.1" in glass,         "浮层材质没有保留“略厚于输入台”的填充/模糊补偿"


# ---- 首包与流式渲染成本 ----------------------------------------------------

def test_routes_are_code_split_and_global_state_is_scoped():
    """路由按需加载，全局状态按切片订阅。

    回归防护：改前 8 个视图与 1114 行 API 文档数据同在一个 index 块
    （gzip 145KB）；且任何一次 `busy +1` 都会让玻璃面板与整棵壳层
    重渲染。两者都不会报错，只会让首屏和加载期变慢。
    """
    app = read(SRC / "App.tsx")
    for view in ("KbDocs", "KbChunks", "KbSettings", "Jobs", "Search",
                 "DocView", "Settings", "ApiDoc"):
        assert f'lazy(() => import("./views/{view}"))' in app, \
            f"{view} 没有按路由分包（首包会重新变胖）"
    assert "<Suspense" in app, "懒加载路由缺少 Suspense 边界"
    assert "useStoreSelect((s) => s.theme)" in app, "壳层仍在整店订阅全局状态"
    assert "<BusyIndicator" in app, "busy 指示器没有独立订阅"

    main = read(SRC / "main.tsx")
    assert "useStoreSelect((s) => s.theme)" in main, \
        "根组件仍整店订阅：busy/kbs 变化会重渲染整个应用"
    glass = read(UI / "glass.tsx")
    assert "useStore()" not in glass, "玻璃面板仍在整店订阅（每块玻璃都跟着 busy 重渲染）"


def test_streaming_updates_are_coalesced():
    """流式回答每帧只更新正文，不得重建整段对话/输入台。

    回归防护：delta 事件原本每个都 `map` 轮次数组（新数组 → 整页
    重渲染），且证据列表、输入台没有 memo；一轮长回答会把整棵
    工作台按 token 重渲染。
    """
    search = read(VIEWS / "Search.tsx")
    assert 'current.status !== "searching") return prev' in search, \
        "每个 delta 仍在重建轮次数组"
    assert "onToggle={toggleChatExpanded}" in search, \
        "证据列表的 onToggle 不是稳定引用，memo 会失效"

    thread = read(UI / "ChatThread.tsx")
    assert "const ChatTurnRow = memo(" in thread, "对话轮次没有 memo"
    evidence = read(UI / "EvidenceList.tsx")
    assert "export default memo(EvidenceList)" in evidence, "证据列表没有 memo"
    composer = read(UI / "SearchComposer.tsx")
    assert "export default memo(SearchComposer)" in composer, "输入台没有 memo"
