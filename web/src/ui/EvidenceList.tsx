import { memo, useEffect, useRef, useState } from "react";
import type { KeyboardEvent as ReactKeyboardEvent, ReactNode } from "react";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import CircularProgress from "@mui/material/CircularProgress";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import { alpha, useTheme } from "@mui/material/styles";
import ContentCopyIcon from "@mui/icons-material/ContentCopy";
import ExpandMoreIcon from "@mui/icons-material/ExpandMore";
import OpenInNewIcon from "@mui/icons-material/OpenInNew";

import { api } from "../lib/api";
import { href } from "../lib/router";
import type { Document, SearchHit, Snippet } from "../lib/types";
import { GlassPanel, useGlassLook } from "./glass";
import { toast } from "./toast";

export interface EvidenceListProps {
  hits: SearchHit[];
  expanded: string | null;
  onToggle: (chunkId: string) => void;
  focusChunk?: string | null;
  onFocusHandled?: () => void;
  compact?: boolean;
  ariaLabel?: string;
}

/**
 * 证据流：卡片式的可键盘浏览列表，两种容器共用同一条渲染路径。
 *
 * 检索模式下面板较宽，卡片留出更多呼吸；对话右栏较窄，压紧间距。
 * 展开后的阅读器始终就地出现，不跳页、不开左右分栏。
 */
function EvidenceList({
  hits,
  expanded,
  onToggle,
  focusChunk = null,
  onFocusHandled,
  compact = false,
  ariaLabel = "检索证据",
}: EvidenceListProps): React.ReactElement {
  const theme = useTheme();
  const look = useGlassLook("card");
  const rowRefs = useRef<Map<string, HTMLElement>>(new Map());

  useEffect(() => {
    if (!focusChunk) return;
    const el = rowRefs.current.get(focusChunk);
    if (!el) return;
    el.scrollIntoView({ behavior: "smooth", block: "center" });
    el.focus({ preventScroll: true });
    onFocusHandled?.();
  }, [focusChunk, hits, onFocusHandled]);

  function onRowKey(
    e: ReactKeyboardEvent<HTMLElement>, index: number, hit: SearchHit,
  ): void {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      onToggle(hit.chunk_id);
      return;
    }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const next = hits[index + (e.key === "ArrowDown" ? 1 : -1)];
      if (next) rowRefs.current.get(next.chunk_id)?.focus();
      return;
    }
    if (e.key === "Escape" && expanded === hit.chunk_id) {
      e.preventDefault();
      onToggle(hit.chunk_id);
    }
  }

  return (
    <Box
      component="ol"
      aria-label={ariaLabel}
      sx={{
        listStyle: "none",
        m: 0,
        p: 0,
        display: "flex",
        flexDirection: "column",
        gap: compact ? 1 : 1.25,
      }}
    >
      {hits.map((hit, index) => {
        const isOpen = expanded === hit.chunk_id;
        return (
          <Box component="li" key={hit.chunk_id}>
            <GlassPanel
              radius={12}
              look={look}
              style={{
                border: `1px solid ${isOpen
                  ? theme.palette.primary.main
                  : alpha(theme.palette.text.primary, 0.1)}`,
                overflow: "hidden",
                boxShadow: isOpen
                  ? `0 0 0 3px ${alpha(theme.palette.primary.main, 0.1)}`
                  : "none",
                transition: "border-color .15s, box-shadow .15s",
              }}
            >
            <Box
              ref={(el: HTMLElement | null) => {
                if (el) rowRefs.current.set(hit.chunk_id, el);
                else rowRefs.current.delete(hit.chunk_id);
              }}
              role="button"
              tabIndex={0}
              aria-expanded={isOpen}
              aria-controls={`evidence-${hit.chunk_id}`}
              onClick={() => onToggle(hit.chunk_id)}
              onKeyDown={(e) => onRowKey(e, index, hit)}
              sx={{
                display: "flex",
                alignItems: "flex-start",
                gap: compact ? 1.25 : 1.75,
                p: compact ? 1.5 : 2,
                cursor: "pointer",
                "&:hover": { bgcolor: "action.hover" },
                "&:focus-visible": {
                  outline: "2px solid",
                  outlineColor: "primary.main",
                  outlineOffset: -2,
                },
                "@media (prefers-reduced-motion: no-preference)": {
                  transition: "background-color .15s",
                },
              }}
            >
              <Box
                sx={{
                  width: compact ? 24 : 28,
                  height: compact ? 24 : 28,
                  borderRadius: "50%",
                  flex: "none",
                  display: "grid",
                  placeItems: "center",
                  bgcolor: index === 0
                    ? "primary.main"
                    : (t) => alpha(t.palette.text.primary, 0.06),
                  color: index === 0 ? "primary.contrastText" : "text.secondary",
                  fontSize: compact ? 11 : 12,
                  fontWeight: 700,
                  fontVariantNumeric: "tabular-nums",
                  mt: 0.25,
                }}
              >
                {index + 1}
              </Box>

              <Box sx={{ flex: 1, minWidth: 0 }}>
                <Stack
                  direction="row"
                  spacing={0.75}
                  alignItems="center"
                  useFlexGap
                  flexWrap="wrap"
                  sx={{ mb: 0.5 }}
                >
                  <Typography
                    component="h3"
                    variant={compact ? "body2" : "subtitle2"}
                    sx={{ fontWeight: 600, lineHeight: 1.4, wordBreak: "break-word" }}
                  >
                    {hit.title || "独立分块"}
                  </Typography>
                  {hit.page != null && (
                    <Typography variant="caption" color="text.secondary">
                      第 {hit.page} 页
                    </Typography>
                  )}
                  {!hit.enabled && (
                    <Typography variant="caption" color="error.main">已停用</Typography>
                  )}
                  {hit.edited && (
                    <Typography variant="caption" color="text.secondary">已修订</Typography>
                  )}
                  <Box sx={{ flex: 1 }} />
                  <Box
                    sx={{
                      px: 0.75,
                      py: 0.15,
                      borderRadius: 999,
                      bgcolor: (t) => alpha(t.palette.primary.main, 0.08),
                      color: "primary.main",
                      fontSize: 11,
                      fontWeight: 600,
                      fontVariantNumeric: "tabular-nums",
                      whiteSpace: "nowrap",
                    }}
                  >
                    {hit.score.toFixed(3)} · {hit.score_kind}
                  </Box>
                </Stack>

                <Typography
                  variant="caption"
                  color="text.secondary"
                  noWrap
                  sx={{ display: "block" }}
                >
                  {hit.heading_path || "未归类"} · 第 {hit.ordinal + 1} 块
                </Typography>

                <Typography
                  variant="body2"
                  color="text.secondary"
                  sx={{
                    mt: 0.5,
                    lineHeight: 1.8,
                    display: "-webkit-box",
                    WebkitLineClamp: isOpen ? "unset" : compact ? 2 : 3,
                    WebkitBoxOrient: "vertical",
                    overflow: "hidden",
                    "& mark": {
                      color: "inherit",
                      bgcolor: (t) => alpha(t.palette.warning.main, 0.38),
                      px: 0.25,
                      borderRadius: 0.5,
                    },
                  }}
                >
                  <SnippetText snippet={hit.snippet} />
                </Typography>
              </Box>

              <ExpandMoreIcon
                fontSize="small"
                sx={{
                  color: "text.disabled",
                  transform: isOpen ? "rotate(180deg)" : "none",
                  transition: "transform .18s",
                  mt: 0.5,
                }}
              />
            </Box>

            {isOpen && (
              <EvidencePanel
                key={hit.chunk_id}
                hit={hit}
                compact={compact}
                onClose={() => onToggle(hit.chunk_id)}
              />
            )}
            </GlassPanel>
          </Box>
        );
      })}
    </Box>
  );
}

// 对话流式输出时证据数组本身不变；没有 memo 时每帧都会把整列卡片
// （含展开面板里的全文）重渲染一遍。
export default memo(EvidenceList);

/** 片段渲染：纯文本 + 结构化高亮区间，不使用 HTML 注入。 */
export function SnippetText({ snippet }: { snippet: Snippet }): React.ReactElement {
  const parts: ReactNode[] = [];
  let cur = 0;
  for (const [s, e] of snippet.marks) {
    if (s > cur) parts.push(snippet.text.slice(cur, s));
    parts.push(<mark key={`${s}-${e}`}>{snippet.text.slice(s, e)}</mark>);
    cur = e;
  }
  parts.push(snippet.text.slice(cur));
  return <>{parts}</>;
}

/**
 * 行内证据阅读器。
 *
 * 渐进展开：命中片段 → 相邻上下文 → 按需读取全文。全文请求只在用户
 * 明确点击后发出，避免每个展开动作都变成一次文档下载。
 */
function EvidencePanel({
  hit, compact, onClose,
}: {
  hit: SearchHit;
  compact?: boolean;
  onClose: () => void;
}): React.ReactElement {
  const [wantFull, setWantFull] = useState(false);
  const [doc, setDoc] = useState<Document | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [retry, setRetry] = useState(0);

  useEffect(() => {
    if (!wantFull || !hit.doc_id) return;
    const ctrl = new AbortController();
    setLoading(true);
    setLoadError("");
    void api<Document>(
      `/api/documents/${encodeURIComponent(hit.doc_id)}?include_chunks=false`,
      { signal: ctrl.signal },
    )
      .then((d) => { if (!ctrl.signal.aborted) setDoc(d); })
      .catch((e) => {
        if (ctrl.signal.aborted) return;
        setLoadError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => { if (!ctrl.signal.aborted) setLoading(false); });
    return () => ctrl.abort();
  }, [wantFull, hit.doc_id, retry]);

  const contexts = Object.entries(hit.context)
    .map(([ordinal, text]) => [Number(ordinal), text] as const)
    .filter(([ordinal]) => Number.isFinite(ordinal) && ordinal !== hit.ordinal)
    .sort((a, b) => a[0] - b[0]);

  const body = doc?.text ?? "";
  const canLocate = doc != null && hit.offset_valid
    && hit.char_start >= 0 && hit.char_end <= body.length && hit.char_end > hit.char_start;

  async function copyCitation(): Promise<void> {
    const lines = [
      hit.title || "独立分块",
      hit.heading_path,
      hit.page != null ? `第 ${hit.page} 页` : "",
      hit.snippet.text,
    ].filter(Boolean);
    try {
      await navigator.clipboard.writeText(lines.join("\n"));
      toast("已复制引用");
    } catch {
      toast("复制失败，请手动选择文本", "err");
    }
  }

  return (
    <Box
      id={`evidence-${hit.chunk_id}`}
      role="region"
      aria-label="证据详情"
      onKeyDown={(e) => { if (e.key === "Escape") onClose(); }}
      sx={{
        px: compact ? 2 : 2.5,
        pb: compact ? 2 : 2.5,
      }}
    >
      <Box
        sx={{
          border: 1,
          borderColor: "divider",
          borderLeft: 3,
          borderLeftColor: "primary.main",
          borderRadius: 1.5,
          px: 1.75,
          py: 1.5,
        }}
      >
        <Typography variant="caption" color="text.secondary" sx={{ fontWeight: 600 }}>
          命中片段
        </Typography>
        <Typography
          variant={compact ? "body2" : "body1"}
          sx={{
            mt: 0.5,
            lineHeight: 1.9,
            "& mark": {
              color: "inherit",
              bgcolor: (t) => alpha(t.palette.warning.main, 0.38),
              px: 0.25,
              borderRadius: 0.5,
            },
          }}
        >
          <SnippetText snippet={hit.snippet} />
        </Typography>
      </Box>

      {contexts.length > 0 && (
        <Box sx={{ mt: 2 }}>
          <Typography variant="caption" color="text.secondary" sx={{ fontWeight: 600 }}>
            相邻上下文
          </Typography>
          <Stack spacing={1.25} sx={{ mt: 0.75 }}>
            {contexts.map(([ordinal, text]) => (
              <Box key={ordinal}>
                <Typography
                  variant="caption"
                  color="text.disabled"
                  sx={{ fontVariantNumeric: "tabular-nums" }}
                >
                  第 {ordinal + 1} 块
                </Typography>
                <Typography
                  variant="body2"
                  color="text.secondary"
                  sx={{ whiteSpace: "pre-wrap", lineHeight: 1.8 }}
                >
                  {text}
                </Typography>
              </Box>
            ))}
          </Stack>
        </Box>
      )}

      <Stack
        direction="row"
        spacing={0.75}
        useFlexGap
        flexWrap="wrap"
        sx={{ mt: 2, alignItems: "center" }}
      >
        <Button size="small" startIcon={<ContentCopyIcon />} onClick={() => void copyCitation()}>
          复制引用
        </Button>
        {hit.doc_id && hit.kb_id && (
          <Button
            size="small"
            component="a"
            href={href("kb", hit.kb_id, "doc", hit.doc_id)}
            startIcon={<OpenInNewIcon />}
          >
            在文档中打开
          </Button>
        )}
        {hit.doc_id && (
          <Button size="small" onClick={() => setWantFull((v) => !v)}>
            {wantFull ? "收起全文" : "读取全文"}
          </Button>
        )}
        <Box sx={{ flex: 1 }} />
        <Button size="small" onClick={onClose} sx={{ color: "text.secondary" }}>
          收起
        </Button>
      </Stack>

      {wantFull && (
        <Box
          sx={{
            mt: 2,
            border: 1,
            borderColor: "divider",
            borderRadius: 1.5,
            px: 2,
            py: 1.5,
            maxHeight: 380,
            overflowY: "auto",
          }}
        >
          {!hit.doc_id && (
            <Typography variant="body2" color="text.secondary">
              这是独立分块，没有所属文档；上方片段就是它的全文。
            </Typography>
          )}
          {hit.doc_id && loading && (
            <Stack direction="row" spacing={1} alignItems="center">
              <CircularProgress size={14} />
              <Typography variant="body2" color="text.secondary">正在读取全文…</Typography>
            </Stack>
          )}
          {loadError && (
            <Alert
              severity="error"
              action={<Button color="inherit" size="small" onClick={() => setRetry((n) => n + 1)}>重试</Button>}
            >
              {loadError}
            </Alert>
          )}
          {doc && !loading && !loadError && (
            <>
              {!hit.offset_valid && (
                <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 1 }}>
                  该分块被编辑过，偏移已失效——下面是全文，不再定位。
                </Typography>
              )}
              <Typography
                variant="body2"
                component="pre"
                sx={{
                  whiteSpace: "pre-wrap",
                  wordBreak: "break-word",
                  fontFamily: "inherit",
                  lineHeight: 1.8,
                  m: 0,
                  "& mark": {
                    color: "inherit",
                    bgcolor: (t) => alpha(t.palette.warning.main, 0.38),
                    px: 0.25,
                  },
                }}
              >
                {canLocate ? (
                  <>
                    {body.slice(0, hit.char_start)}
                    <mark>{body.slice(hit.char_start, hit.char_end)}</mark>
                    {body.slice(hit.char_end)}
                  </>
                ) : (
                  body
                )}
              </Typography>
            </>
          )}
        </Box>
      )}
    </Box>
  );
}