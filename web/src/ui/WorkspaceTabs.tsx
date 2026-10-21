import type { ReactElement } from "react";
import Box from "@mui/material/Box";
import Tab from "@mui/material/Tab";
import Tabs from "@mui/material/Tabs";
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
    <Box
      sx={{
        maxWidth: 1440,
        mx: "auto",
        px: { xs: 2, md: 3 },
        pt: 2,
      }}
    >
      <Tabs
        value={current}
        aria-label="知识库视图"
        variant="scrollable"
        scrollButtons="auto"
        sx={{ minHeight: 40, borderBottom: 1, borderColor: "divider" }}
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
            sx={{ minHeight: 40, minWidth: 96, gap: 0.5 }}
          />
        ))}
      </Tabs>
    </Box>
  );
}
