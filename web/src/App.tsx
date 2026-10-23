import { useEffect, useRef, useState } from "react";
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
import LayersIcon from "@mui/icons-material/Layers";
import LightModeIcon from "@mui/icons-material/LightMode";

import { CONTENT_MAX } from "./lib/layout";
import { SECTIONS, sectionOf } from "./lib/nav";
import { go, href, useRoute } from "./lib/router";
import { loadKbs, setTheme, startQueueWatch, stopQueueWatch, useQueue, useStore } from "./lib/store";
import KbList from "./views/KbList";
import KbDocs from "./views/KbDocs";
import KbChunks from "./views/KbChunks";
import KbSettings from "./views/KbSettings";
import Jobs from "./views/Jobs";
import Search from "./views/Search";
import DocView from "./views/DocView";
import Settings from "./views/Settings";
import ApiDoc from "./views/ApiDoc";
import { ToastHost } from "./ui/toast";
import CommandPalette from "./ui/CommandPalette";
import WorkspaceTabs from "./ui/WorkspaceTabs";
import { GlassBackdrop, GlassPanel, useGlassLook } from "./ui/glass";

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

export default function App(): ReactElement {
  const route = useRoute();
  const { theme, busy, crumbs } = useStore();
  const section = sectionOf(route);
  const barLook = useGlassLook("bar");
  // 页面在顶部时顶栏完全透明，和页面融为一体；开始滚动后才浮出玻璃。
  const [scrolled, setScrolled] = useState(false);
  const mainRef = useRef<HTMLDivElement | null>(null);

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

  return (
    <Box sx={{ display: "flex", flexDirection: "column", height: "100%" }}>
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
              sx={{ gap: 2, minHeight: 56, px: { xs: 1, md: 2 } }}
            >
              <Box
                component="a"
                href="#/"
                sx={{
                  display: "flex", alignItems: "center", gap: 1.25,
                  color: "inherit", textDecoration: "none", flex: "none",
                  borderRadius: 999, pr: 0.5,
                }}
              >
                <Box
                  sx={(t) => ({
                    width: 30, height: 30, borderRadius: "50%",
                    display: "grid", placeItems: "center",
                    bgcolor: alpha(t.palette.primary.main, t.palette.mode === "dark" ? 0.2 : 0.12),
                    boxShadow: `inset 0 0 0 1px ${alpha(t.palette.primary.main, 0.18)}`,
                  })}
                >
                  <LayersIcon color="primary" sx={{ fontSize: 18 }} />
                </Box>
                <Typography variant="subtitle1" sx={{ fontWeight: 600, letterSpacing: ".01em" }}>
                  Raggi
                </Typography>
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
                    minWidth: 72,
                    px: 2,
                    mx: 0.25,
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

              {busy > 0 && <CircularProgress size={16} aria-label="正在加载" />}
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

        <View route={route} />
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
