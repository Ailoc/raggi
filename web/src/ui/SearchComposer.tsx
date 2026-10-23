import { memo } from "react";
import Box from "@mui/material/Box";import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
import IconButton from "@mui/material/IconButton";
import InputBase from "@mui/material/InputBase";
import Stack from "@mui/material/Stack";
import ToggleButton from "@mui/material/ToggleButton";
import ToggleButtonGroup from "@mui/material/ToggleButtonGroup";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import AddCommentIcon from "@mui/icons-material/AddComment";
import ArrowUpwardIcon from "@mui/icons-material/ArrowUpward";
import ChatBubbleOutlineIcon from "@mui/icons-material/ChatBubbleOutline";
import SearchIcon from "@mui/icons-material/Search";
import StopCircleIcon from "@mui/icons-material/StopCircle";
import TuneIcon from "@mui/icons-material/Tune";

import type { Kb } from "../lib/types";
import { GlassPanel, useGlassLook } from "./glass";
import KbMultiSelect from "./KbMultiSelect";
import RetrievalModeSelect from "./RetrievalModeSelect";

export type SearchMode = "search" | "chat";

/**
 * 输入台的艺术指导：比卡片更实、更亮——它是页面上唯一需要一直盯着
 * 的控件，压在检索结果上时底下有内容在动，玻璃必须更"糊"一点才读得出
 * 文字。`lensing` 拉高让边缘读出厚度，光照角度转到左上，和页面顶部
 * 极光的方向一致。
 *
 * 明暗两套在 `glassLook()` 里（浅色必须压低 brightness，否则背景被
 * 截断成纯白，玻璃就白做了）。
 */
const COMPOSER_RADIUS = 26;

export interface ComposerChip {
  key: string;
  label: string;
  title?: string;
}

export interface SearchComposerProps {
  mode: SearchMode;
  onModeChange: (mode: SearchMode) => void;
  value: string;
  onValueChange: (value: string) => void;
  onSend: () => void;
  onStop?: () => void;
  running: boolean;
  compact: boolean;
  answerEnabled: boolean;
  kbValue: string;
  retrievalMode: string;
  onKbChange: (kbValue: string) => void;
  onRetrievalModeChange: (mode: string) => void;
  onOpenFilters: () => void;
  onNewChat?: () => void;
  chips: ComposerChip[];
  onRemoveChip: (key: string) => void;
  kbs: Kb[];
  inputRef?: React.Ref<HTMLInputElement>;
}

/**
 * 智能体输入台。
 *
 * 空闲时它是页面中心唯一的主角；发送后 compact 版本下沉到底部。
 * 范围/召回/条件都用浮层菜单而不是带悬浮标签的表单控件——输入台
 * 只保留一个问题输入和一个发送动作，其余设置一眼可见、点开才展开。
 */
function SearchComposer({
  mode,
  onModeChange,
  value,
  onValueChange,
  onSend,
  onStop,
  running,
  compact,
  answerEnabled,
  kbValue,
  retrievalMode,
  onKbChange,
  onRetrievalModeChange,
  onOpenFilters,
  onNewChat,
  chips,
  onRemoveChip,
  kbs,
  inputRef,
}: SearchComposerProps): React.ReactElement {
  const look = useGlassLook("composer");

  const sendLabel = mode === "search" ? "检索" : "发送";
  const canSend = value.trim().length > 0 && (mode === "search" || answerEnabled);

  return (
    <Box sx={{ width: "100%" }}>
      {/*
        输入台只做一层玻璃：底下铺的是壳层统一的环境光，不再自带一块
        局部彩色底。之前那层 panel 背光会在输入框里留下蓝紫色调，
        看起来像"输入框自带背景色"，和页面其余玻璃面也不一致。
      */}
      <Box sx={{ position: "relative" }}>
        <GlassPanel radius={COMPOSER_RADIUS} className="lg-composer" look={look}>
          <InputBase
            inputRef={inputRef}
            value={value}
            onChange={(e) => onValueChange(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault();
                if (!running) onSend();
              }
            }}
            multiline
            minRows={1}
            maxRows={compact ? 4 : 6}
            placeholder={mode === "chat"
              ? compact ? "继续追问…" : "输入问题，开始对话…"
              : compact
                ? "继续检索…"
                : "输入问题，选择检索或对话…"}
            inputProps={{ "aria-label": "检索或对话输入", maxLength: 2000 }}
            sx={{
              display: "block",
              px: { xs: 2.25, sm: 3 },
              pt: compact ? 1.6 : 2.4,
              pb: compact ? 0.6 : 1,
              fontSize: compact ? 15 : 20,
              lineHeight: 1.7,
              "& textarea::placeholder": { color: "text.disabled", opacity: 1 },
            }}
          />

          {chips.length > 0 && (
            <Stack
              direction="row"
              spacing={0.75}
              useFlexGap
              flexWrap="wrap"
              sx={{ px: { xs: 1.75, sm: 2.25 }, pb: 0.5 }}
            >
              {chips.map((chip) => (
                <Chip
                  key={chip.key}
                  size="small"
                  label={chip.label}
                  title={chip.title}
                  onDelete={() => onRemoveChip(chip.key)}
                  sx={{
                    borderRadius: 999,
                    bgcolor: "action.hover",
                    "& .MuiChip-deleteIcon": { fontSize: 15 },
                  }}
                />
              ))}
            </Stack>
          )}

          <Stack
            direction="row"
            spacing={0.5}
            useFlexGap
            flexWrap="wrap"
            alignItems="center"
            sx={{ px: { xs: 1.25, sm: 1.5 }, pt: 0.5, pb: compact ? 1.25 : 1.5 }}
          >
            <ToggleButtonGroup
              exclusive
              size="small"
              value={mode}
              onChange={(_e, next: SearchMode | null) => { if (next) onModeChange(next); }}
              aria-label="检索或对话"
              sx={(t) => ({
                // 玻璃上的分段控件：底槽与选中态都保持半透明，
                // 不透明的白块贴在玻璃上会像贴纸。
                bgcolor: (t.palette.mode === "dark"
                  ? "rgba(255,255,255,.07)"
                  : "rgba(255,255,255,.34)"),
                borderRadius: 999,
                p: 0.4,
                gap: 0.4,
                "& .MuiToggleButtonGroup-grouped": {
                  border: 0,
                  borderRadius: "999px !important",
                  px: 1.4,
                  py: 0.45,
                  fontSize: 13,
                  color: "text.secondary",
                  textTransform: "none",
                  "&.Mui-selected": {
                    bgcolor: (t.palette.mode === "dark"
                      ? "rgba(255,255,255,.16)"
                      : "rgba(255,255,255,.78)"),
                    color: "text.primary",
                    boxShadow: "0 1px 3px rgba(0,0,0,.12)",
                    "&:hover": {
                      bgcolor: (t.palette.mode === "dark"
                        ? "rgba(255,255,255,.2)"
                        : "rgba(255,255,255,.85)"),
                    },
                  },
                },
              })}
            >
              <ToggleButton value="search" aria-label="检索模式">
                <SearchIcon fontSize="small" sx={{ mr: 0.6 }} />
                检索
              </ToggleButton>
              <Tooltip
                title={answerEnabled
                  ? "保留上下文，连续追问"
                  : "生成式对话未启用：在配置中设置 features.answer=true 后重启"}
              >
                <Box component="span" sx={{ display: "inline-flex" }}>
                  <ToggleButton value="chat" aria-label="对话模式" disabled={!answerEnabled}>
                    <ChatBubbleOutlineIcon fontSize="small" sx={{ mr: 0.6 }} />
                    对话
                  </ToggleButton>
                </Box>
              </Tooltip>
            </ToggleButtonGroup>

            <KbMultiSelect
              value={kbValue}
              onChange={onKbChange}
              kbs={kbs}
            />

            <RetrievalModeSelect
              value={retrievalMode}
              onChange={onRetrievalModeChange}
            />

            <Button
              size="small"
              color="inherit"
              startIcon={<TuneIcon fontSize="small" />}
              onClick={onOpenFilters}
              aria-haspopup="dialog"
              sx={{
                borderRadius: 999,
                px: 1.25,
                color: chips.length > 0 ? "primary.main" : "text.secondary",
                fontWeight: 500,
                whiteSpace: "nowrap",
                "&:hover": { bgcolor: "action.hover" },
              }}
            >
              条件{chips.length > 0 ? ` ${chips.length}` : ""}
            </Button>

            <Box sx={{ flex: 1 }} />

            {mode === "chat" && onNewChat && (
              <Tooltip title="开始新对话">
                <IconButton size="small" aria-label="开始新对话" onClick={onNewChat}>
                  <AddCommentIcon fontSize="small" />
                </IconButton>
              </Tooltip>
            )}

            {running ? (
              onStop ? (
                <Tooltip title="停止生成">
                  <IconButton
                    aria-label="停止生成"
                    onClick={onStop}
                    sx={{
                      border: 1,
                      borderColor: "error.main",
                      color: "error.main",
                      width: 36,
                      height: 36,
                    }}
                  >
                    <StopCircleIcon fontSize="small" />
                  </IconButton>
                </Tooltip>
              ) : (
                <CircularProgress size={22} aria-label="处理中" sx={{ mr: 0.5 }} />
              )
            ) : (
              <Tooltip title={canSend ? sendLabel : (mode === "chat" ? "输入问题后发送" : "输入问题后检索")}>
                <span>
                  <IconButton
                    aria-label={sendLabel}
                    onClick={onSend}
                    disabled={!canSend}
                    sx={{
                      width: 36,
                      height: 36,
                      bgcolor: "primary.main",
                      color: "primary.contrastText",
                      "&:hover": { bgcolor: "primary.dark" },
                      "&.Mui-disabled": {
                        bgcolor: "action.disabledBackground",
                        color: "action.disabled",
                      },
                    }}
                  >
                    <ArrowUpwardIcon fontSize="small" />
                  </IconButton>
                </span>
              </Tooltip>
            )}
          </Stack>
        </GlassPanel>
      </Box>

      {!compact && (
        <Typography
          variant="caption"
          color="text.secondary"
          sx={{ display: "block", textAlign: "center", mt: 1.5 }}
        >
          Enter 发送 · Shift+Enter 换行 · / 聚焦输入
        </Typography>
      )}
    </Box>
  );
}

// 流式回答每帧都会更新工作台，但输入台的 props 在此期间不变；
// memo 让这道边界挡住整棵表单子树（含两个浮层选择器）的重渲染。
export default memo(SearchComposer);