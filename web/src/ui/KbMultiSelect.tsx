import { useId, useMemo, useState } from "react";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Checkbox from "@mui/material/Checkbox";
import Divider from "@mui/material/Divider";
import InputAdornment from "@mui/material/InputAdornment";
import ListItemButton from "@mui/material/ListItemButton";
import ListItemText from "@mui/material/ListItemText";
import Popover from "@mui/material/Popover";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";
import { alpha } from "@mui/material/styles";
import type { Theme } from "@mui/material/styles";
import ExpandMoreIcon from "@mui/icons-material/ExpandMore";
import FolderOutlinedIcon from "@mui/icons-material/FolderOutlined";
import SearchIcon from "@mui/icons-material/Search";

import { kbIdsFromValue, kbValueFromIds } from "../lib/searchparams";
import type { Kb } from "../lib/types";
import { GlassPanel, usePopoverGlassLook } from "./glass";

/** 与后端 SearchFilters.kb_ids 的上限一致。 */
const MAX_KB_IDS = 50;

export interface KbMultiSelectProps {
  /** 逗号分隔的 kb_id；空串 = 全部知识库（含未分组文档）。 */
  value: string;
  onChange: (value: string) => void;
  kbs: Kb[];
  /**
   * composer：输入台里的药丸按钮；field：条件抽屉里的整行控件。
   * 两个入口共用同一套选择逻辑，避免两处对“已选几个库”的解释漂移。
   */
  variant?: "composer" | "field";
  id?: string;
}

const optionSx = (t: Theme) => ({
  borderRadius: 2,
  mb: 0.25,
  px: 1,
  py: 0.75,
  border: "1px solid transparent",
  alignItems: "flex-start",
  "& .MuiCheckbox-root": { p: 0.5, mt: 0.25 },
  "&.Mui-selected": {
    bgcolor: alpha(t.palette.primary.main, t.palette.mode === "dark" ? 0.18 : 0.08),
    borderColor: alpha(t.palette.primary.main, 0.22),
    "&:hover": {
      bgcolor: alpha(t.palette.primary.main, t.palette.mode === "dark" ? 0.24 : 0.12),
    },
  },
});

/**
 * 知识库多选浮层。
 *
 * 设计约定：
 * - “全部知识库”不是“选满所有库”，而是**不加范围条件**——它还会包含
 *   未分组文档；这个区别写在副标题里，避免用户以为两者等价。
 * - 列表顺序按知识库列表原序，勾选时不跳动；搜索只在视觉上过滤。
 * - 玻璃纸面沿用全站 Popover 的材质，`.lg-kb-picker` 额外补一层
 *   高光与内边距，不引入新的实色面板。
 */
export default function KbMultiSelect({
  value, onChange, kbs, variant = "composer", id,
}: KbMultiSelectProps): React.ReactElement {
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);
  const [query, setQuery] = useState("");
  const listId = useId();
  const open = Boolean(anchor);
  const look = usePopoverGlassLook(variant === "field");

  const ids = useMemo(() => kbIdsFromValue(value), [value]);
  const selected = useMemo(() => new Set(ids), [ids]);
  const byId = useMemo(() => new Map(kbs.map((k) => [k.kb_id, k])), [kbs]);
  const selectedKbs = ids.map((kbId) => byId.get(kbId)).filter((k): k is Kb => !!k);
  const atLimit = ids.length >= MAX_KB_IDS;

  const label = ids.length === 0
    ? "全部知识库"
    : ids.length === 1
      ? selectedKbs[0]?.name ?? "已失效知识库"
      : `${selectedKbs[0]?.name ?? "已选"} 等 ${ids.length} 个`;
  const title = ids.length === 0
    ? "全部知识库（含未分组文档）"
    : `已选：${ids.map((kbId) => byId.get(kbId)?.name ?? "已失效知识库").join("、")}`;

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return kbs;
    return kbs.filter((k) =>
      k.name.toLowerCase().includes(needle)
      || (k.description ?? "").toLowerCase().includes(needle));
  }, [kbs, query]);

  // 触摸设备不自动聚焦搜索框：一打开就弹键盘会顶掉半个视口。
  const autoFocus = useMemo(
    () => typeof window !== "undefined"
      && window.matchMedia?.("(pointer: fine)").matches === true,
    [],
  );

  /** 按知识库列表原序排列，保证两次相同选择得到同一个参数字符串。 */
  function ordered(next: string[]): string[] {
    const pos = new Map(kbs.map((k, i) => [k.kb_id, i]));
    return [...new Set(next)].sort((a, b) => {
      const ia = pos.get(a);
      const ib = pos.get(b);
      if (ia === undefined && ib === undefined) return a.localeCompare(b);
      if (ia === undefined) return 1;
      if (ib === undefined) return -1;
      return ia - ib;
    });
  }

  function commit(next: string[]): void {
    const sorted = ordered(next);
    if (sorted.length === ids.length && sorted.every((kbId, i) => kbId === ids[i])) return;
    onChange(kbValueFromIds(sorted));
  }

  function toggle(kbId: string): void {
    if (!selected.has(kbId) && atLimit) return;
    commit(selected.has(kbId) ? ids.filter((x) => x !== kbId) : [...ids, kbId]);
  }

  function openPicker(e: React.MouseEvent<HTMLElement>): void {
    setQuery("");
    setAnchor(e.currentTarget);
  }

  function closePicker(): void {
    setAnchor(null);
  }

  function selectFiltered(): void {
    commit([...ids, ...filtered.map((k) => k.kb_id)].slice(0, MAX_KB_IDS));
  }

  const trigger = variant === "field" ? (
    <Box>
      <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 0.5 }}>
        知识库
      </Typography>
      <Button
        id={id}
        fullWidth
        variant="outlined"
        color="inherit"
        onClick={openPicker}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? listId : undefined}
        aria-label={`知识库范围：${label}`}
        title={title}
        sx={{
          justifyContent: "space-between",
          textTransform: "none",
          px: 1.5,
          py: 0.75,
          borderRadius: 1.5,
          color: "text.primary",
          borderColor: "divider",
        }}
      >
        <Box sx={{ display: "flex", alignItems: "center", gap: 1, minWidth: 0 }}>
          <FolderOutlinedIcon
            fontSize="small"
            sx={{ color: ids.length > 0 ? "primary.main" : "text.secondary" }}
          />
          <Box
            component="span"
            sx={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
          >
            {label}
          </Box>
        </Box>
        <Box sx={{ display: "flex", alignItems: "center", gap: 0.5, flexShrink: 0 }}>
          {ids.length > 1 && (
            <Typography variant="caption" color="primary.main" sx={{ fontWeight: 600 }}>
              {ids.length}
            </Typography>
          )}
          <ExpandMoreIcon fontSize="small" sx={{ color: "text.disabled" }} />
        </Box>
      </Button>
    </Box>
  ) : (
    <Button
      id={id}
      size="small"
      color="inherit"
      onClick={openPicker}
      aria-haspopup="listbox"
      aria-expanded={open}
      aria-controls={open ? listId : undefined}
      aria-label={`知识库范围：${label}`}
      title={title}
      startIcon={<FolderOutlinedIcon fontSize="small" />}
      endIcon={<ExpandMoreIcon fontSize="small" />}
      sx={{
        borderRadius: 999,
        px: 1.25,
        fontWeight: 500,
        color: ids.length > 0 ? "primary.main" : "text.secondary",
        maxWidth: { xs: 190, sm: 280 },
        whiteSpace: "nowrap",
        overflow: "hidden",
        "&:hover": { bgcolor: "action.hover", color: "text.primary" },
        "& .MuiButton-startIcon, & .MuiButton-endIcon": { flexShrink: 0 },
      }}
    >
      <Box
        component="span"
        sx={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
      >
        {label}
      </Box>
    </Button>
  );

  return (
    <>
      {trigger}
      <Popover
        open={open}
        anchorEl={anchor}
        onClose={closePicker}
        disableScrollLock
        disableAutoFocus={!autoFocus}
        anchorOrigin={{
          vertical: variant === "composer" ? "top" : "bottom",
          horizontal: "left",
        }}
        transformOrigin={{
          vertical: variant === "composer" ? "bottom" : "top",
          horizontal: "left",
        }}
        slotProps={{
          paper: {
            // 纸面本身保持透明：材质交给 open-glass-ui 的 GlassPanel，
            // 与卡片/对话框共用同一套 rim、lensing、高光与投影。
            className: "lg-glass-popover-paper",
            sx: {
              width: "min(360px, calc(100vw - 24px))",
              maxWidth: "calc(100vw - 24px)",
              p: 0,
              // 透明纸面留 8px 内边距：玻璃与触发器之间有一道呼吸缝，
              // 不靠 margin（MUI 的 Popover 定位不会计入 margin）。
              pt: variant === "field" ? 1 : 0,
              pb: variant === "composer" ? 1 : 0,
              border: 0,
              borderRadius: "20px",
              overflow: "visible",
              backgroundColor: "transparent",
              backgroundImage: "none",
              boxShadow: "none",
            },
          },
          // 二级浮层（条件面板里的知识库选择）加一层局部遮罩：底下是
          // 密集表单，不压暗会和列表文字糊在一起。
          ...(variant === "field"
            ? {
                backdrop: {
                  sx: {
                    backgroundColor: "rgba(15, 23, 42, 0.18)",
                    backdropFilter: "blur(1px)",
                  },
                },
              }
            : {}),
        }}
      >
        <GlassPanel
          radius={20}
          material="regular"
          look={look}
          className="lg-kb-picker lg-clip"
        >
          <Box
            sx={{
              display: "flex",
              flexDirection: "column",
              maxHeight: { xs: "calc(100dvh - 96px)", sm: 460 },
            }}
          >
        <Box sx={{ p: 1.25, pb: 1, borderBottom: 1, borderColor: "divider" }}>
          <TextField
            size="small"
            fullWidth
            autoFocus={autoFocus}
            placeholder="搜索知识库名称或描述…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            slotProps={{
              htmlInput: { "aria-label": "搜索知识库" },
              input: {
                startAdornment: (
                  <InputAdornment position="start">
                    <SearchIcon fontSize="small" />
                  </InputAdornment>
                ),
              },
            }}
            sx={(t) => ({
              "& .MuiOutlinedInput-root": {
                borderRadius: 2,
                bgcolor: alpha(
                  t.palette.background.paper,
                  t.palette.mode === "dark" ? 0.08 : 0.42,
                ),
                "& fieldset": { borderColor: alpha(t.palette.text.primary, 0.12) },
              },
            })}
          />
        </Box>

        <Box
          id={listId}
          role="listbox"
          aria-multiselectable="true"
          aria-label="知识库范围"
          sx={{
            flex: 1,
            minHeight: 0,
            // 浮层在触发器上方展开；列表上限按“顶栏以下、触发器以上”的
            // 实际高度反推。1280×800 / 1024×768 / 780×600 这类矮视口
            // 若固定 280px，面板会被挤到顶栏或触发器上。
            maxHeight: {
              xs: "clamp(130px, calc(100dvh - 570px), 190px)",
              sm: "clamp(150px, calc(100dvh - 570px), 280px)",
            },
            overflowY: "auto",
            px: 1,
            pb: 2.5,
            // 列表不是总能看到底：用底部渐隐代替“切一半”的硬边，同时
            // 提示还能继续滚动；末尾留出等高的 padding，滚到底时最后
            // 一项仍能完整露出。
            maskImage: "linear-gradient(to bottom, #000 calc(100% - 20px), transparent)",
            WebkitMaskImage: "linear-gradient(to bottom, #000 calc(100% - 20px), transparent)",
          }}
        >
          <ListItemButton
            role="option"
            aria-selected={ids.length === 0}
            selected={ids.length === 0}
            onClick={() => { if (ids.length > 0) onChange(""); }}
            sx={optionSx}
          >
            <Checkbox checked={ids.length === 0} readOnly tabIndex={-1} disableRipple size="small" />
            <ListItemText
              primary="全部知识库"
              secondary="不限制范围，包含未分组文档"
              slotProps={{
                primary: { fontSize: 13.5, fontWeight: 500 },
                secondary: { fontSize: 12 },
              }}
            />
          </ListItemButton>

          {filtered.map((k) => {
            const on = selected.has(k.kb_id);
            return (
              <ListItemButton
                key={k.kb_id}
                role="option"
                aria-selected={on}
                selected={on}
                disabled={!on && atLimit}
                onClick={() => toggle(k.kb_id)}
                sx={optionSx}
                title={k.description || k.name}
              >
                <Checkbox checked={on} readOnly tabIndex={-1} disableRipple size="small" />
                <ListItemText
                  primary={(
                    <Stack direction="row" alignItems="center" spacing={0.75} sx={{ minWidth: 0 }}>
                      <Box
                        component="span"
                        sx={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
                      >
                        {k.name}
                      </Box>
                      <Typography
                        component="span"
                        variant="caption"
                        color="text.secondary"
                        sx={{ flexShrink: 0, fontVariantNumeric: "tabular-nums" }}
                      >
                        {k.doc_count} 文档
                      </Typography>
                    </Stack>
                  )}
                  secondary={k.description || `${k.chunk_count} 个分块`}
                  slotProps={{
                    primary: { component: "div", fontSize: 13.5, fontWeight: 500 },
                    secondary: { noWrap: true, fontSize: 12 },
                  }}
                />
              </ListItemButton>
            );
          })}

          {filtered.length === 0 && (
            <Box sx={{ px: 1.5, py: 3, textAlign: "center" }}>
              <Typography variant="body2" color="text.secondary">
                没有匹配「{query.trim()}」的知识库
              </Typography>
              <Button size="small" onClick={() => setQuery("")} sx={{ mt: 0.5 }}>
                清空搜索
              </Button>
            </Box>
          )}
        </Box>

        <Divider />
        <Stack direction="row" alignItems="center" spacing={1} sx={{ px: 1.5, py: 1 }}>
          <Typography
            variant="caption"
            color="text.secondary"
            sx={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
          >
            {ids.length === 0 ? "全部知识库" : `已选 ${ids.length} 个知识库${atLimit ? "（上限）" : ""}`}
          </Typography>
          {query.trim() !== "" && filtered.length > 0 && (
            <Button size="small" onClick={selectFiltered} sx={{ minWidth: 0 }}>
              全选结果
            </Button>
          )}
          <Button size="small" onClick={closePicker}>完成</Button>
        </Stack>
          </Box>
        </GlassPanel>
      </Popover>
    </>
  );
}
