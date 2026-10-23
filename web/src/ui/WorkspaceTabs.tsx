import type { ReactElement } from "react";
import Box from "@mui/material/Box";
import Tab from "@mui/material/Tab";
import Tabs from "@mui/material/Tabs";
import { alpha } from "@mui/material/styles";
import DescriptionIcon from "@mui/icons-material/Description";
import LayersIcon from "@mui/icons-material/Layers";
import SettingsIcon from "@mui/icons-material/Settings";

import { href } from "../lib/router";

/**
 * 工作区标签条：文档 / 分块 / 设置在同一层切换。
 *
 * 用 `<a href>` 而不是按钮：中键新开、右键复制链接、状态栏预览都要保留，
 * 而且这三个地址本来就该可分享。MUI 的 Tab 支持 `component="a"`，
 * 因此视觉是标签、语义仍是链接。
 *
 * 它渲染在壳层顶栏玻璃**内部**（`App.tsx`），所以这里不再自带边框、
 * 最大宽度和页面级内边距——任何一条内部分隔线都会把一片完整的玻璃
 * 切成两截。选中态用与主栏同款的淡色药丸，而不是下划线。
 */
const TABS = [
  { key: "docs", label: "文档", icon: <DescriptionIcon fontSize="small" /> },
  { key: "chunks", label: "分块", icon: <LayersIcon fontSize="small" /> },
  { key: "settings", label: "设置", icon: <SettingsIcon fontSize="small" /> },
] as const;

export default function WorkspaceTabs({
  kbId,
  tab,
}: {
  kbId: string;
  tab: string;
}): ReactElement {
  const current = TABS.some((t) => t.key === tab) ? tab : "docs";
  return (
    <Box sx={{ px: { xs: 1, md: 2 }, pb: 0.75 }}>
      <Tabs
        value={current}
        aria-label="知识库视图"
        variant="scrollable"
        scrollButtons="auto"
        sx={{
          minHeight: 38,
          "& .MuiTabs-indicator": { display: "none" },
          "& .MuiTab-root": {
            minHeight: 34,
            minWidth: 88,
            px: 1.5,
            mx: 0.25,
            gap: 0.5,
            borderRadius: 999,
            transition: "background-color .18s ease, color .18s ease",
          },
          "& .MuiTab-root.Mui-selected": (t) => ({
            backgroundColor: alpha(
              t.palette.primary.main,
              t.palette.mode === "dark" ? 0.2 : 0.11,
            ),
            color: t.palette.primary.main,
          }),
        }}
      >
        {TABS.map((t) => (
          <Tab
            key={t.key}
            value={t.key}
            component="a"
            href={href("kb", kbId, t.key)}
            icon={t.icon}
            iconPosition="start"
            label={t.label}
          />
        ))}
      </Tabs>
    </Box>
  );
}
