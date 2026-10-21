import { useEffect } from "react";
import type { ReactElement } from "react";
import AppBar from "@mui/material/AppBar";
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
import DarkModeIcon from "@mui/icons-material/DarkMode";
import LayersIcon from "@mui/icons-material/Layers";
import LightModeIcon from "@mui/icons-material/LightMode";
import ManageSearchIcon from "@mui/icons-material/ManageSearch";

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
      <AppBar position="static" color="inherit" elevation={2} sx={{ zIndex: 20 }}>
        <Toolbar variant="dense" sx={{ gap: 2, minHeight: 52 }}>
          <Box
            component="a"
            href="#/"
            sx={{
              display: "flex", alignItems: "center", gap: 1,
              color: "inherit", textDecoration: "none", flex: "none",
            }}
          >
            <LayersIcon color="primary" />
            <Typography variant="subtitle1" sx={{ fontWeight: 600, letterSpacing: ".01em" }}>
              Raggi
            </Typography>
          </Box>

          <Tabs
            value={section.key}
            textColor="primary"
            indicatorColor="primary"
            variant="scrollable"
            scrollButtons="auto"
            sx={{ minHeight: 40, flexShrink: 1, "& .MuiTab-root": { minHeight: 40, minWidth: 72, px: 2 } }}
          >
            {SECTIONS.map((s) => (
              <Tab key={s.key} value={s.key} component="a" href={s.href} label={s.label} />
            ))}
          </Tabs>

          <Box sx={{ flex: 1 }} />

          <Tooltip title="搜索与跳转（Command/Ctrl + K）">
            <Chip
              icon={<ManageSearchIcon />}
              label="搜索与跳转"
              variant="outlined"
              onClick={() => window.dispatchEvent(new CustomEvent("raggi:palette"))}
              sx={{ display: { xs: "none", sm: "flex" } }}
            />
          </Tooltip>

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
            variant="dense"
            sx={{
              minHeight: 36, borderTop: 1, borderColor: "divider",
              bgcolor: "background.paper",
            }}
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
      </AppBar>

      <Box
        component="main"
        id="main"
        sx={{ flex: 1, minHeight: 0, overflowY: "auto", bgcolor: "background.default" }}
      >
        {(route.name === "kb" || route.name === "doc") && (
          <WorkspaceTabs
            kbId={route.params.kbId ?? ""}
            tab={route.name === "doc" ? "docs" : route.tab}
          />
        )}
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
