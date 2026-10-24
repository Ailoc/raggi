import {
  lazy, Suspense, useEffect, useLayoutEffect, useMemo, useRef, useState,
} from "react";
import type { ReactElement } from "react";
import Box from "@mui/material/Box";
import Breadcrumbs from "@mui/material/Breadcrumbs";
import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
import IconButton from "@mui/material/IconButton";
import Link from "@mui/material/Link";
import Tab from "@mui/material/Tab";
import Tabs from "@mui/material/Tabs";
import Toolbar from "@mui/material/Toolbar";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import { alpha } from "@mui/material/styles";
import DarkModeIcon from "@mui/icons-material/DarkMode";
import LightModeIcon from "@mui/icons-material/LightMode";

import { CONTENT_MAX } from "./lib/layout";
import { SECTIONS, sectionOf } from "./lib/nav";
import { go, href, useRoute } from "./lib/router";
import {
  loadKbs, setTheme, startQueueWatch, stopQueueWatch, useQueue, useStoreSelect,
} from "./lib/store";
import KbList from "./views/KbList";

// 首页（知识库总览）随首包加载；其余视图按路由分包。
// 改前所有视图 + 1114 行 API 文档数据都进同一个 index 块：首屏必须
// 下载并解析用户根本还没打开的页面。默认路由是 kbs，因此 KbList 留下。
const KbDocs = lazy(() => import("./views/KbDocs"));
const KbChunks = lazy(() => import("./views/KbChunks"));
const KbSettings = lazy(() => import("./views/KbSettings"));
const Jobs = lazy(() => import("./views/Jobs"));
const Search = lazy(() => import("./views/Search"));
const DocView = lazy(() => import("./views/DocView"));
const Settings = lazy(() => import("./views/Settings"));
const ApiDoc = lazy(() => import("./views/ApiDoc"));
import { ToastHost } from "./ui/toast";
import CommandPalette from "./ui/CommandPalette";
import WorkspaceTabs from "./ui/WorkspaceTabs";
import { GlassBackdrop, GlassPanel, useGlassLook } from "./ui/glass";
import { Wordmark } from "./ui/Wordmark";

/**
 * 壳层：AppBar（品牌 + 分段导航 + 命令面板入口 + 队列 + 主题）+ 面包屑条 +
 * 主滚动区 + 全局挂件。
 *
 * 结构承接 Svelte 版的既有结论（那些结论是几轮实测换来的，不该因换框架丢掉）：
 *
 * 1. **没有侧栏**：常驻导航栏试过三代都删了（与主内容重复）。跳转靠顶栏
 *    分段 + 命令面板（⌘K）。
 * 2. **顶栏是唯一常驻导航**，位置指示由面包屑承担，各页只往里写数据。
 * 3. **滚动归主区**：body 不滚（CssBaseline 里写了 overflow:hidden），
 *    顶栏与面包屑因此在滚动中常驻。
 */

function QueueChip(): ReactElement | null {
  const queue = useQueue();
  if (queue.active <= 0) return null;
  return (
    <Chip
      size="small"
      color="primary"
      variant="outlined"
      label={`队列 ${queue.active}`}
      onClick={() => go(href("jobs"))}
      sx={{ mr: 1 }}
    />
  );
}

/**
 * busy 单独订阅：它每次 API 调用都会 +1/-1，放在 App 的订阅里会让
 * 壳层（以及 route 之外的整棵视图树）跟着抖动。
 */
function BusyIndicator(): ReactElement | null {
  const busy = useStoreSelect((s) => s.busy);
  if (busy <= 0) return null;
  return <CircularProgress size={16} aria-label="正在加载" />;
}

function RouteFallback(): ReactElement {
  return (
    <Box sx={{ display: "grid", placeItems: "center", py: 10 }}>
      <CircularProgress size={24} aria-label="正在加载页面" />
    </Box>
  );
}

export default function App(): ReactElement {
  const route = useRoute();
  const theme = useStoreSelect((s) => s.theme);
  const crumbs = useStoreSelect((s) => s.crumbs);
  const section = sectionOf(route);
  const barLook = useGlassLook("bar");
  // 页面在顶部时顶栏完全透明，和页面融为一体；开始滚动后才浮出玻璃。
  const [scrolled, setScrolled] = useState(false);
  const mainRef = useRef<HTMLDivElement | null>(null);
  const headerRef = useRef<HTMLDivElement | null>(null);

  /*
   * 把顶栏实测高度写进 CSS 变量，供需要“填满剩余视口”的视图使用。
   *
   * 顶栏高度不是常数：面包屑、工作区标签会让它从 84 长到 164，
   * 滚动后顶部留白收起又少 20px，小屏是另一套值。检索工作台和 API
   * 文档如果按 100% 或 100vh-84 算高度，就会比可用空间多出正好一个
   * 顶栏——输入台被挤出视口、页面多出一条无意义的纵向滚动。
   */
  useLayoutEffect(() => {
    const el = headerRef.current;
    if (!el) return;
    const root = document.documentElement;
    const sync = (): void => {
      root.style.setProperty(
        "--shell-header-h",
        `${Math.ceil(el.getBoundingClientRect().height)}px`,
      );
    };
    sync();
    const ro = new ResizeObserver(sync);
    ro.observe(el);
    return () => {
      ro.disconnect();
      root.style.removeProperty("--shell-header-h");
    };
  }, []);

  // 换页回到顶部：上一页的滚动位置不该带进新页面（API 页尤其依赖
  // 顶栏处于"未滚动"状态来计算三栏的可用高度）。
  // 只认路径，不认查询串：API 文档过滤时 URL 会带 q=，不能因此跳顶。
  const routeKey = `${route.name}|${route.parts.join("/")}`;
  useEffect(() => {
    mainRef.current?.scrollTo({ top: 0 });
    setScrolled(false);
  }, [routeKey]);

  /** 进入检索 section 时通知已挂载的检索页清空查询（URL 里没有 q 可清）。 */
  function openSection(key: string, href: string): void {
    if (key === "search") {
      window.dispatchEvent(new CustomEvent("raggi:newSearch"));
    }
    go(href);
  }

  useEffect(() => {
    void loadKbs().catch(() => {
      // 知识库列表拉不到不该阻塞界面：各视图会各自报错并可重试
    });
    startQueueWatch();
    return () => stopQueueWatch();
  }, []);

  // ⌘K / Ctrl+K 打开命令面板。必须挂在 window 上且抢在其它快捷键之前——
  // 它是唯一带修饰键的壳层快捷键。
  useEffect(() => {
    const onKey = (e: KeyboardEvent): void => {
      if ((e.metaKey || e.ctrlKey) && !e.altKey && (e.key === "k" || e.key === "K")) {
        e.preventDefault();
        window.dispatchEvent(new CustomEvent("raggi:palette"));
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // 路由出口只在 route 变化时重建：App 因 crumbs/theme 重渲染时，
  // React 会跳过整棵视图子树（context 更新仍会穿透到消费者）。
  const view = useMemo(() => (
    <Suspense fallback={<RouteFallback />}>
      <View route={route} />
    </Suspense>
  ), [route]);

  return (
    <Box sx={{ display: "flex", flexDirection: "column", height: "100%" }}>
      {/* 内容区只依赖 route：顶栏滚动/主题/crumbs 变化时不必让整页重渲染。 */}
      {/*
        环境底色：固定在视口最底层，顶栏与内容区共用同一片光。
        顶栏的玻璃糊的是这层底色，与下面的页面才接得上。
      */}
      <GlassBackdrop variant="shell" />

      <Box
        component="main"
        id="main"
        ref={mainRef}
        // 抬到底色之上：环境底色是 z-index 0 的固定层，内容区要在它上面。
        onScroll={(e) => setScrolled(e.currentTarget.scrollTop > 8)}
        sx={{
          flex: 1, minHeight: 0, overflowY: "auto",
          position: "relative", zIndex: 1, bgcolor: "transparent",
        }}
      >
        {/*
          顶栏在滚动区**里面**并 sticky：内容从它下面滑过去，
          玻璃才有东西可糊。放在 main 外面的话，底下永远是一片空的
          背景色，玻璃就退化成一条半透明的色带。
        */}
        <Box
          component="header"
          ref={headerRef}
          sx={{
            position: "sticky", top: 0, zIndex: 20, pb: 1,
            // 顶栏与正文同宽（全站统一的 CONTENT_MAX）+ 同样的内边距。
            // 它不再横跨浏览器两侧；页面在顶部时整条玻璃完全隐形，
            // 只有开始滚动后才浮出来，默认状态真正"沉"进页面里。
            maxWidth: CONTENT_MAX, mx: "auto", px: { xs: 2, md: 3 },
            // 顶部时留出呼吸；滚动后贴到视口顶端，避免内容从玻璃上方
            // 的缝隙里露出来。
            pt: scrolled ? 0 : { xs: 2, md: 2.5 },
            transition: "padding-top .2s ease",
          }}
        >
          {/*
            玻璃比正文左右各多出 8/16px：里面再补同量的内边距，
            导航文字仍然与正文对齐，但玻璃边缘不会贴着字。
          */}
          <Box sx={{ position: "relative", mx: { xs: -1, md: -2 } }}>
          <GlassPanel
            radius={18}
            className={`lg-topbar${scrolled ? "" : " lg-topbar--immersive"}`}
            look={barLook}
            style={{ overflow: "hidden", boxShadow: "none" }}
          >
            <Toolbar
              disableGutters
              variant="dense"
              sx={{ gap: { xs: 1, sm: 2 }, minHeight: 56, px: { xs: 1, md: 2 } }}
            >
              <Box
                component="a"
                href="#/"
                sx={{
                  display: "flex", alignItems: "center",
                  color: "inherit", textDecoration: "none", flex: "none",
                  borderRadius: 999, pr: 0.5,
                  // 320px 级窄屏：品牌也要让位，否则五个分区只能
                  // 靠横向滑动才能点到 API。
                  "@media (max-width: 359px)": { display: "none" },
                }}
              >
                {/*
                  品牌字标：内联 SVG，跟主题走同一条造型线（见 ui/Wordmark.tsx）。
                */}
                <Wordmark
                  sx={{
                    // 字标比原来的圆形图标宽：小屏必须同时收字标高度
                    // 和 Tab 内边距，否则五个分区放不下，API 会被挤掉。
                    height: { xs: 17, sm: 26 },
                    width: "auto",
                    display: "block",
                  }}
                />
              </Box>

              <Tabs
                value={section.key}
                // 显式跳转：保留 `<a href>` 是为了中键新开/复制链接，但不依赖
                // 浏览器对锚点默认行为的处理——内嵌/隐藏文档里默认跳转可能不触发。
                onChange={(_e, key: string) => {
                  const target = SECTIONS.find((s) => s.key === key);
                  if (target) openSection(target.key, target.href);
                }}
                textColor="primary"
                indicatorColor="primary"
                variant="scrollable"
                scrollButtons="auto"
                sx={{
                  minHeight: 40,
                  flexShrink: 1,
                  // 选中态用一层很淡的玻璃药丸，而不是默认的下划线：
                  // 顶栏本身就是玻璃，下划线会像贴在玻璃上的一条实色胶带。
                  "& .MuiTabs-indicator": { display: "none" },
                  "& .MuiTab-root": {
                    minHeight: 36,
                    minWidth: { xs: 0, sm: 72 },
                    px: { xs: 0.7, sm: 2 },
                    mx: 0.25,
                    fontSize: { xs: 12.5, sm: 14 },
                    borderRadius: 999,
                    transition: "background-color .18s ease, color .18s ease",
                  },
                  "& .MuiTab-root.Mui-selected": (t) => ({
                    backgroundColor: alpha(t.palette.primary.main, t.palette.mode === "dark" ? 0.2 : 0.11),
                    color: t.palette.primary.main,
                  }),
                }}
              >
                {SECTIONS.map((s) => (
                  <Tab
                    key={s.key}
                    value={s.key}
                    component="a"
                    href={s.href}
                    label={s.label}
                    // 点当前已选中的「检索」也要回到空态（清掉查询词），
                    // 而 onChange 在 value 不变时不会触发。
                    onClick={() => openSection(s.key, s.href)}
                  />
                ))}
              </Tabs>

              <Box sx={{ flex: 1 }} />

              <BusyIndicator />
              <QueueChip />

              <Tooltip title={theme === "dark" ? "切换到浅色主题" : "切换到深色主题"}>
                <IconButton
                  aria-label={theme === "dark" ? "切换到浅色主题" : "切换到深色主题"}
                  onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
                >
                  {theme === "dark" ? <LightModeIcon /> : <DarkModeIcon />}
                </IconButton>
              </Tooltip>
          </Toolbar>

          {crumbs.length > 1 && (
            <Toolbar
              disableGutters
              variant="dense"
              className="lg-topbar-crumbs"
              sx={{ minHeight: 36, px: { xs: 1, md: 2 } }}
            >
              <Breadcrumbs separator="/" aria-label="面包屑" sx={{ fontSize: 13 }}>
                {crumbs.map((c, i) =>
                  i === crumbs.length - 1 || !c.href ? (
                    <Typography key={i} variant="body2" color="text.secondary">
                      {c.label}
                    </Typography>
                  ) : (
                    <Link
                      key={i}
                      href={c.href}
                      underline="hover"
                      variant="body2"
                      color="text.secondary"
                    >
                      {c.label}
                    </Link>
                  ),
                )}
              </Breadcrumbs>
            </Toolbar>
          )}

          {/*
            工作区标签条并入顶栏玻璃：它原来独立挂在页面流里，自带一条
            下边框，和玻璃顶栏之间会切出第二道硬边。并进来之后，主栏、
            面包屑、工作区标签共用一片材质，顶栏才是一个整体。
          */}
          {(route.name === "kb" || route.name === "doc") && (
            <WorkspaceTabs
              kbId={route.params.kbId ?? ""}
              tab={route.name === "doc" ? "docs" : route.tab}
            />
          )}
          </GlassPanel>

          {/*
            玻璃底部的柔化层：只做模糊、不带颜色，用遮罩淡出。
            没有它时玻璃面在底部突然收边，像一条贴上去的色带；有了它，
            内容从顶栏下面滑出来时是渐进的，不会有一道硬缝。
          */}
          <Box
            aria-hidden
            className={`lg-topbar-fade${scrolled ? "" : " lg-topbar-fade--hidden"}`}
          />
          </Box>
        </Box>

        {view}
      </Box>

      <ToastHost />
      <CommandPalette />
    </Box>
  );
}

/** 路由出口。未迁移的视图给一个**如实说明**的占位，而不是空白或假界面。 */
function View({ route }: { route: ReturnType<typeof useRoute> }): ReactElement {
  switch (route.name) {
    case "kbs":
      return <KbList />;
    case "kb": {
      const kbId = route.params.kbId ?? "";
      if (route.tab === "chunks") {
        return <KbChunks kbId={kbId} scope={route.query.get("scope") ?? ""} />;
      }
      if (route.tab === "settings") return <KbSettings kbId={kbId} />;
      return <KbDocs kbId={kbId} />;
    }
    case "doc":
      return (
        <DocView
          key={`${route.params.kbId ?? ""}/${route.params.docId ?? ""}`}
          kbId={route.params.kbId ?? ""}
          docId={route.params.docId ?? ""}
        />
      );
    case "search":
      return <Search />;
    case "jobs":
      return <Jobs />;
    case "settings":
      return <Settings tab={route.tab} />;
    case "apidoc":
      return <ApiDoc query={route.query} />;
    default:
      return <KbList />;
  }
}
