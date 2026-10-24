import Box from "@mui/material/Box";
import ButtonBase from "@mui/material/ButtonBase";
import IconButton from "@mui/material/IconButton";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import { alpha, useTheme } from "@mui/material/styles";
import useMediaQuery from "@mui/material/useMediaQuery";
import CloseIcon from "@mui/icons-material/Close";

import type { ChatTurn, Citation, SearchHit } from "../lib/types";
import ChunkReader from "./ChunkReader";
import { GlassBackdrop, GlassPanel, useGlassLook } from "./glass";

/**
 * 右侧阅读区。
 *
 * 它和对话内容是**同一页面层级**的兄弟节点：展开时对话列整体左移，
 * 而不是盖在内容上的一层浮层。窄屏没有左移的余地，退化成全屏阅读页。
 *
 * 区域里只有一件事——**正在读的那个分块的原文**：命中片段在原文里的
 * 位置用高亮标出。上面那条编号带是本轮证据的快速切换（编号与回答里的
 * 引用编号一致），点哪一条就读哪一条。
 */
export interface SourcePanelProps {
  /** 区域所属的轮次。 */
  turn: ChatTurn | null;
  /** 正在阅读的分块。 */
  chunkId: string | null;
  /** 引用不在本轮证据里时的兜底元信息。 */
  fallbackHit: SearchHit | null;
  /** 切换到另一个分块。 */
  onSelectChunk: (chunkId: string) => void;
  onClose: () => void;
}

export default function SourcePanel({
  turn,
  chunkId,
  fallbackHit,
  onSelectChunk,
  onClose,
}: SourcePanelProps): React.ReactElement {
  const look = useGlassLook("card");
  const theme = useTheme();
  const wide = useMediaQuery(theme.breakpoints.up("md"));
  const hits = turn?.hits ?? [];
  const selected = chunkId
    ? (hits.find((h) => h.chunk_id === chunkId) ?? fallbackHit)
    : null;

  // 编号与回答里的引用编号对齐；没有被引用过的证据按召回顺序编号。
  const citationIndex = new Map<string, number>();
  for (const c of turn?.citations ?? []) {
    if (!citationIndex.has(c.chunk_id)) citationIndex.set(c.chunk_id, c.index);
  }

  return (
    <Box
      component="aside"
      aria-label="证据阅读区"
      sx={{
        // 与对话列同层：固定占位，主列因此左移而不是被盖住。
        flex: { xs: "none", md: "0 0 clamp(340px, 28vw, 400px)" },
        width: { xs: "100%", md: "auto" },
        position: { xs: "fixed", md: "relative" },
        inset: { xs: 0, md: "auto" },
        zIndex: { xs: 20, md: "auto" },
        minHeight: 0,
        display: "flex",
        flexDirection: "column",
        p: { xs: 1.5, md: 1.5 },
      }}
    >
      <Box sx={{ position: "relative", flex: 1, minHeight: 0, display: "flex" }}>
        {/*
          玻璃底下要有东西可折射：极光是整页的一片渐变，落到这么大一块
          玻璃上几乎是纯色，于是玻璃塌成一张白卡片。局部背景层给这块
          玻璃自己的光，边缘与倒角才有东西可弯。
        */}
        <GlassBackdrop variant="panel" radius={22} />
        <GlassPanel
          radius={22}
          look={look}
          // 宽屏：背景是极光，玻璃透出光即可。窄屏：玻璃底下是对话
          // 本身，得磨砂到读不清底下的字，否则正文没法读。
          material={wide ? "clear" : "frosted"}
          style={{
            position: "relative",
            zIndex: 1,
            flex: 1,
            minHeight: 0,
            display: "flex",
            flexDirection: "column",
            overflow: "hidden",
          }}
        >
        <Box
          sx={{
            px: 2,
            py: 1.5,
            flex: "none",
            borderBottom: 1,
            borderColor: (t) => alpha(t.palette.text.primary, 0.08),
          }}
        >
          <Stack direction="row" spacing={1} alignItems="flex-start">
            <Box sx={{ flex: 1, minWidth: 0 }}>
              <Typography variant="caption" color="text.secondary">
                本轮证据
              </Typography>
              <Typography
                variant="body2"
                sx={{ fontWeight: 600, mt: 0.25, wordBreak: "break-word" }}
              >
                {turn?.question ?? ""}
              </Typography>
              <Typography variant="caption" color="text.secondary" aria-live="polite">
                {turn?.evidenceLoading && hits.length === 0
                  ? "正在检索…"
                  : `${hits.length} 条证据`
                    + (turn?.searchTookMs != null ? ` · ${turn.searchTookMs}ms` : "")}
              </Typography>
            </Box>
            <IconButton aria-label="关闭证据面板" onClick={onClose} size="small">
              <CloseIcon fontSize="small" />
            </IconButton>
          </Stack>

          {hits.length > 0 && (
            <Stack
              direction="row"
              spacing={0.5}
              useFlexGap
              flexWrap="wrap"
              sx={{ mt: 1.25 }}
            >
              {hits.map((hit, i) => {
                const active = hit.chunk_id === chunkId;
                const label = citationIndex.get(hit.chunk_id) ?? i + 1;
                return (
                  <ButtonBase
                    key={hit.chunk_id}
                    onClick={() => onSelectChunk(hit.chunk_id)}
                    aria-pressed={active}
                    aria-label={`阅读证据 ${label}`}
                    sx={{
                      minWidth: 28,
                      height: 28,
                      px: 0.75,
                      borderRadius: 1.5,
                      border: 1,
                      borderColor: active ? "primary.main" : "divider",
                      bgcolor: active
                        ? (t) => alpha(t.palette.primary.main, 0.12)
                        : "transparent",
                      color: active ? "primary.main" : "text.secondary",
                      fontVariantNumeric: "tabular-nums",
                      fontSize: 12,
                      fontWeight: 600,
                      transition: "background-color .15s, border-color .15s",
                      "&:hover": {
                        bgcolor: (t) => alpha(t.palette.primary.main, 0.08),
                      },
                      "&:focus-visible": {
                        outline: "2px solid",
                        outlineColor: "primary.main",
                        outlineOffset: 1,
                      },
                    }}
                  >
                    {label}
                  </ButtonBase>
                );
              })}
            </Stack>
          )}
        </Box>

        <Box sx={{ flex: 1, minHeight: 0, overflowY: "auto", px: 2, py: 2 }}>
          {turn?.searchError && (
            <Box sx={{ mb: 1.5 }}>
              <Typography variant="body2" color="error.main">
                证据检索失败：{turn.searchError}
              </Typography>
            </Box>
          )}

          {chunkId && (
            <ChunkReader chunkId={chunkId} hit={selected} />
          )}

          {!chunkId && (
            <Typography variant="body2" color="text.secondary">
              {turn?.status === "stopped"
                ? "已停止生成，证据检索也已被取消。"
                : hits.length > 0
                  ? "点上面任意一条编号，即可在这里读它的原文。"
                  : "这一轮没有检索到证据，回答可能缺少依据。"}
            </Typography>
          )}
        </Box>
        </GlassPanel>
      </Box>
    </Box>
  );
}

/**
 * 引用本身携带的字段比证据条目少（没有相邻上下文，也没有原文偏移）。
 * 引用不在本轮证据里时用它兜底，至少让读者看到命中片段。
 */
export function hitFromCitation(citation: Citation): SearchHit {
  return {
    chunk_id: citation.chunk_id,
    doc_id: citation.doc_id,
    kb_id: citation.kb_id ?? "",
    title: citation.title,
    heading_path: citation.heading_path,
    page: citation.page,
    ordinal: citation.ordinal,
    char_start: -1,
    char_end: -1,
    offset_valid: false,
    score: citation.score,
    score_kind: "",
    scores: { vector_distance: 0, fts: 0, rrf: 0 },
    snippet: citation.snippet,
    parser_engine: "",
    edited: false,
    enabled: true,
    origin: "parsed",
    context: {},
  };
}
