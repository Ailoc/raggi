import { useEffect, useId, useRef, useState } from "react";
import type { ElementType, KeyboardEvent } from "react";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import ListItemButton from "@mui/material/ListItemButton";
import ListItemText from "@mui/material/ListItemText";
import Popover from "@mui/material/Popover";
import Typography from "@mui/material/Typography";
import { alpha } from "@mui/material/styles";
import type { Theme } from "@mui/material/styles";
import CheckIcon from "@mui/icons-material/Check";
import ExpandMoreIcon from "@mui/icons-material/ExpandMore";
import HubIcon from "@mui/icons-material/Hub";
import ScatterPlotIcon from "@mui/icons-material/ScatterPlot";
import TextSnippetIcon from "@mui/icons-material/TextSnippet";

import { MODE_OPTIONS } from "../lib/searchparams";
import { GlassPanel, usePopoverGlassLook } from "./glass";

const MODE_ICONS: Record<string, ElementType> = {
  hybrid: HubIcon,
  vector: ScatterPlotIcon,
  fts: TextSnippetIcon,
};

const optionSx = (t: Theme) => ({
  borderRadius: 2,
  px: 1,
  py: 0.9,
  border: "1px solid transparent",
  "&.Mui-selected": {
    bgcolor: alpha(t.palette.primary.main, t.palette.mode === "dark" ? 0.18 : 0.08),
    borderColor: alpha(t.palette.primary.main, 0.22),
    "&:hover": {
      bgcolor: alpha(t.palette.primary.main, t.palette.mode === "dark" ? 0.24 : 0.12),
    },
  },
});

/**
 * 召回模式选择器。
 *
 * 与知识库范围共用同一套玻璃浮层：regular 材质、相同的 rim/lensing、
 * Paper 透明。每个模式有自己的图标——混合是融合节点、向量是散点、
 * 全文是文本片段——让“召回方式”不再只是一行文字。
 */
export default function RetrievalModeSelect({
  value,
  onChange,
}: {
  value: string;
  onChange: (mode: string) => void;
}): React.ReactElement {
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);
  const listId = useId();
  const open = Boolean(anchor);
  const look = usePopoverGlassLook();
  const optionRefs = useRef<Record<string, HTMLElement | null>>({});

  const current = MODE_OPTIONS.find((m) => m.value === value) ?? MODE_OPTIONS[0];
  const CurrentIcon = MODE_ICONS[current.value] ?? HubIcon;

  // Popover 的焦点陷阱会先落到第一个可聚焦元素；打开后主动聚焦当前项。
  useEffect(() => {
    if (!open) return;
    const timer = window.setTimeout(() => optionRefs.current[value]?.focus(), 0);
    return () => window.clearTimeout(timer);
  }, [open, value]);

  function choose(mode: string): void {
    onChange(mode);
    setAnchor(null);
  }

  function onOptionKey(e: KeyboardEvent<HTMLElement>, index: number): void {
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key)) return;
    e.preventDefault();
    const len = MODE_OPTIONS.length;
    const next = e.key === "ArrowDown" ? (index + 1) % len
      : e.key === "ArrowUp" ? (index - 1 + len) % len
        : e.key === "Home" ? 0 : len - 1;
    optionRefs.current[MODE_OPTIONS[next]!.value]?.focus();
  }

  return (
    <>
      <Button
        size="small"
        color="inherit"
        onClick={(e) => setAnchor(e.currentTarget)}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? listId : undefined}
        aria-label={`召回模式：${current.label}`}
        title={current.hint}
        startIcon={<CurrentIcon fontSize="small" />}
        endIcon={<ExpandMoreIcon fontSize="small" />}
        sx={{
          borderRadius: 999,
          px: 1.25,
          color: "text.secondary",
          fontWeight: 500,
          whiteSpace: "nowrap",
          "&:hover": { bgcolor: "action.hover", color: "text.primary" },
          "& .MuiButton-startIcon, & .MuiButton-endIcon": { flexShrink: 0 },
        }}
      >
        {current.label}
      </Button>

      <Popover
        open={open}
        anchorEl={anchor}
        onClose={() => setAnchor(null)}
        disableScrollLock
        anchorOrigin={{ vertical: "top", horizontal: "left" }}
        transformOrigin={{ vertical: "bottom", horizontal: "left" }}
        slotProps={{
          paper: {
            className: "lg-glass-popover-paper",
            sx: {
              width: "min(320px, calc(100vw - 24px))",
              maxWidth: "calc(100vw - 24px)",
              p: 0,
              pb: 1,
              border: 0,
              borderRadius: "18px",
              overflow: "visible",
              backgroundColor: "transparent",
              backgroundImage: "none",
              boxShadow: "none",
            },
          },
        }}
      >
        <GlassPanel
          radius={18}
          material="regular"
          look={look}
          className="lg-glass-popover lg-clip"
        >
          <Box
            id={listId}
            role="listbox"
            aria-label="召回模式"
            sx={{ p: 1, display: "flex", flexDirection: "column", gap: 0.5 }}
          >
            {MODE_OPTIONS.map((m, index) => {
              const Icon = MODE_ICONS[m.value] ?? HubIcon;
              const selected = m.value === current.value;
              return (
                <ListItemButton
                  key={m.value}
                  ref={(el: HTMLElement | null) => { optionRefs.current[m.value] = el; }}
                  role="option"
                  aria-selected={selected}
                  selected={selected}
                  onClick={() => choose(m.value)}
                  onKeyDown={(e) => onOptionKey(e, index)}
                  sx={optionSx}
                >
                  <Box
                    sx={(t) => ({
                      width: 30,
                      height: 30,
                      borderRadius: 2,
                      display: "grid",
                      placeItems: "center",
                      flex: "none",
                      bgcolor: alpha(
                        t.palette.primary.main,
                        selected
                          ? (t.palette.mode === "dark" ? 0.18 : 0.1)
                          : (t.palette.mode === "dark" ? 0.07 : 0.05),
                      ),
                      color: selected ? "primary.main" : "text.secondary",
                      "& svg": { fontSize: 18 },
                    })}
                  >
                    <Icon fontSize="small" />
                  </Box>
                  <ListItemText
                    primary={m.label}
                    secondary={m.hint}
                    slotProps={{
                      primary: { fontSize: 13.5, fontWeight: 500 },
                      secondary: { fontSize: 12 },
                    }}
                    sx={{ ml: 1.25 }}
                  />
                  <CheckIcon
                    fontSize="small"
                    sx={{
                      ml: 1,
                      color: "primary.main",
                      opacity: selected ? 1 : 0,
                      transition: "opacity .15s ease",
                    }}
                  />
                </ListItemButton>
              );
            })}
          </Box>
        </GlassPanel>
      </Popover>

      {/* 读屏器：选中后给一句确认，避免只有视觉状态变化 */}
      <Typography className="sr-only" role="status">
        {`当前召回模式：${current.label}`}
      </Typography>
    </>
  );
}
