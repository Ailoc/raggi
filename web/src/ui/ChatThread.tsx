import { memo, useCallback, useEffect, useRef } from "react";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import ButtonBase from "@mui/material/ButtonBase";
import CircularProgress from "@mui/material/CircularProgress";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import { alpha, useTheme } from "@mui/material/styles";
import LibraryBooksIcon from "@mui/icons-material/LibraryBooks";
import SmartToyIcon from "@mui/icons-material/SmartToy";
import StopCircleIcon from "@mui/icons-material/StopCircle";

import { MODE_OPTIONS } from "../lib/searchparams";
import type { ChatTurn, Citation } from "../lib/types";
import { SnippetText } from "./EvidenceList";
import { GlassPanel, useGlassLook } from "./glass";

interface ChatThreadProps {
  turns: ChatTurn[];
  activeTurnId: string | null;
  runningTurnId: string | null;
  onSelectTurn: (turnId: string) => void;
  onStop: () => void;
  onRetry: (turnId: string) => void;
  onCitation: (turnId: string, citation: Citation) => void;
}

/** 对话记录：用户气泡靠右，Raggi 的回答带头像与浅色卡片；引用可跳回证据。 */
export default function ChatThread({
  turns,
  activeTurnId,
  runningTurnId,
  onSelectTurn,
  onStop,
  onRetry,
  onCitation,
}: ChatThreadProps): React.ReactElement {
  const look = useGlassLook("card");
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const stick = useRef(true);
  const prevCount = useRef(turns.length);

  // 事件回调经 ref 转发：父组件每次渲染都会新建这些函数，直接透传会
  // 让 memo 化的行组件全部失效（流式时每帧都会重渲染整段对话）。
  const handlers = useRef({ onSelectTurn, onStop, onRetry, onCitation });
  useEffect(() => {
    handlers.current = { onSelectTurn, onStop, onRetry, onCitation };
  });
  const selectTurn = useCallback(
    (id: string) => handlers.current.onSelectTurn(id), []);
  const stopTurn = useCallback(() => handlers.current.onStop(), []);
  const retryTurn = useCallback(
    (id: string) => handlers.current.onRetry(id), []);
  const openCitation = useCallback(
    (id: string, citation: Citation) =>
      handlers.current.onCitation(id, citation), []);

  // 新消息与流式增量都走高頻更新；只有用户贴着底部时才跟随，
  // 向上翻看旧回答时不抢滚动位置。新增一轮时重新贴底。
  useEffect(() => {
    if (turns.length > prevCount.current) stick.current = true;
    prevCount.current = turns.length;
    const el = scrollRef.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [turns]);

  return (
    <Box
      ref={scrollRef}
      aria-label="对话记录"
      onScroll={(e) => {
        const el = e.currentTarget;
        stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 64;
      }}
      sx={{
        height: "100%",
        overflowY: "auto",
        px: { xs: 2, sm: 3 },
        py: { xs: 2.5, sm: 3 },
      }}
    >
      <Box sx={{ maxWidth: 780, mx: "auto", width: "100%" }}>
        {turns.map((turn) => (
          <ChatTurnRow
            key={turn.id}
            turn={turn}
            active={turn.id === activeTurnId}
            running={turn.id === runningTurnId}
            look={look}
            onSelectTurn={selectTurn}
            onStop={stopTurn}
            onRetry={retryTurn}
            onCitation={openCitation}
          />
        ))}
      </Box>
    </Box>
  );
}

type GlassLook = ReturnType<typeof useGlassLook>;

interface ChatTurnRowProps {
  turn: ChatTurn;
  active: boolean;
  running: boolean;
  look: GlassLook;
  onSelectTurn: (turnId: string) => void;
  onStop: () => void;
  onRetry: (turnId: string) => void;
  onCitation: (turnId: string, citation: Citation) => void;
}

/**
 * 一轮对话。memo 是流式的关键：只有正在生成的那一轮对象会变，
 * 其余轮次引用不变，因此不会被每帧的增量文本带着重渲染。
 */
const ChatTurnRow = memo(function ChatTurnRow({
  turn,
  active,
  running,
  look,
  onSelectTurn,
  onStop,
  onRetry,
  onCitation,
}: ChatTurnRowProps): React.ReactElement {
  const theme = useTheme();
  return (
    <Box sx={{ mb: 3.5 }}>
      <Box sx={{ display: "flex", justifyContent: "flex-end", mb: 2 }}>
        <Box
          sx={{
            maxWidth: "86%",
            bgcolor: "primary.main",
            color: "primary.contrastText",
            borderRadius: "18px 18px 6px 18px",
            px: 2,
            py: 1.25,
            boxShadow: "0 6px 18px -12px rgba(0,0,0,.5)",
            whiteSpace: "pre-wrap",
            wordBreak: "break-word",
          }}
        >
          <Typography variant="body2" sx={{ lineHeight: 1.7 }}>
            {turn.question}
          </Typography>
        </Box>
      </Box>

      <Box sx={{ display: "flex", gap: 1.5, alignItems: "flex-start" }}>
        <Box
          sx={{
            width: 30,
            height: 30,
            borderRadius: "50%",
            flex: "none",
            display: "grid",
            placeItems: "center",
            bgcolor: (t) => alpha(t.palette.primary.main, 0.12),
            color: "primary.main",
            mt: 0.25,
          }}
        >
          <SmartToyIcon fontSize="small" />
        </Box>

        <Box sx={{ flex: 1, minWidth: 0 }}>
          <Stack
            direction="row"
            spacing={1}
            alignItems="center"
            useFlexGap
            flexWrap="wrap"
            sx={{ mb: 0.75 }}
          >
            <Typography variant="body2" sx={{ fontWeight: 600 }}>Raggi</Typography>
            <StatusText turn={turn} />
            <Box sx={{ flex: 1 }} />
            <Button
              size="small"
              startIcon={<LibraryBooksIcon fontSize="small" />}
              onClick={() => onSelectTurn(turn.id)}
              sx={{
                minWidth: 0,
                borderRadius: 999,
                color: active ? "primary.main" : "text.secondary",
                bgcolor: active
                  ? (t) => alpha(t.palette.primary.main, 0.08)
                  : "transparent",
              }}
            >
              {turn.evidenceLoading ? "正在找证据…" : `证据 ${turn.hits.length}`}
            </Button>
            {running && (
              <Button
                size="small"
                color="error"
                startIcon={<StopCircleIcon fontSize="small" />}
                onClick={onStop}
                sx={{ minWidth: 0, borderRadius: 999 }}
              >
                停止
              </Button>
            )}
          </Stack>

          <GlassPanel
            radius={16}
            look={look}
            style={{
              border: `1px solid ${active
                ? theme.palette.primary.main
                : alpha(theme.palette.text.primary, 0.1)}`,
              borderRadius: "4px 16px 16px 16px",
              boxShadow: active
                ? `0 0 0 3px ${alpha(theme.palette.primary.main, 0.1)}`
                : "none",
              transition: "border-color .15s, box-shadow .15s",
            }}
            sx={{ px: 2, py: 1.75 }}
          >
            {turn.answer ? (
              <Typography
                variant="body1"
                sx={{ whiteSpace: "pre-wrap", wordBreak: "break-word", lineHeight: 1.9 }}
              >
                {turn.answer}
                {turn.status === "streaming" && (
                  <Box
                    component="span"
                    aria-hidden="true"
                    sx={{ color: "primary.main", ml: 0.5 }}
                  >
                    ▍
                  </Box>
                )}
              </Typography>
            ) : turn.status === "searching" || turn.status === "streaming" ? (
              <Stack direction="row" spacing={1} alignItems="center">
                <CircularProgress size={14} />
                <Typography variant="body2" color="text.secondary">
                  {turn.status === "searching" ? "正在检索证据…" : "正在生成回答…"}
                </Typography>
              </Stack>
            ) : turn.status === "stopped" ? (
              <Typography variant="body2" color="text.secondary">
                {turn.evidenceLoading ? "已停止生成，证据检索也已被取消。" : "已停止生成。"}
              </Typography>
            ) : turn.status === "done" ? (
              <Typography variant="body2" color="text.secondary">没有生成内容。</Typography>
            ) : null}

            {turn.searchError && (
              <Alert severity="warning" sx={{ mt: 1.5 }}>
                证据检索失败：{turn.searchError}
              </Alert>
            )}

            {turn.error && (
              <Alert
                severity="error"
                sx={{ mt: 1.5 }}
                action={(
                  <Button color="inherit" size="small" onClick={() => onRetry(turn.id)}>
                    重试
                  </Button>
                )}
              >
                {turn.error}
              </Alert>
            )}

            {turn.citations.length > 0 && (
              <Stack
                direction="row"
                spacing={0.75}
                useFlexGap
                flexWrap="wrap"
                sx={{ mt: turn.answer || turn.error ? 1.75 : 0 }}
              >
                {turn.citations.map((c) => (
                  <ButtonBase
                    key={`${turn.id}-${c.index}-${c.chunk_id}`}
                    onClick={() => onCitation(turn.id, c)}
                    sx={{
                      maxWidth: "100%",
                      textAlign: "left",
                      border: 1,
                      borderColor: "divider",
                      borderRadius: 1.5,
                      bgcolor: "action.hover",
                      px: 1.1,
                      py: 0.65,
                      "&:hover": {
                        borderColor: "primary.main",
                        bgcolor: (t) => alpha(t.palette.primary.main, 0.1),
                      },
                      "&:focus-visible": {
                        outline: "2px solid",
                        outlineColor: "primary.main",
                      },
                    }}
                  >
                    <Stack direction="row" spacing={0.75} alignItems="center">
                      <Typography
                        variant="caption"
                        color="primary.main"
                        sx={{ fontVariantNumeric: "tabular-nums", fontWeight: 600 }}
                      >
                        [{c.index}]
                      </Typography>
                      <Box sx={{ minWidth: 0 }}>
                        <Typography
                          variant="caption"
                          sx={{ fontWeight: 600, display: "block" }}
                          noWrap
                        >
                          {c.title || "独立分块"}
                          {c.page != null ? ` · 第 ${c.page} 页` : ""}
                        </Typography>
                        {c.snippet.text && (
                          <Typography
                            variant="caption"
                            color="text.secondary"
                            noWrap
                            sx={{ display: "block", maxWidth: 260 }}
                          >
                            <SnippetText snippet={c.snippet} />
                          </Typography>
                        )}
                      </Box>
                    </Stack>
                  </ButtonBase>
                ))}
              </Stack>
            )}
          </GlassPanel>
        </Box>
      </Box>
    </Box>
  );
});

function StatusText({ turn }: { turn: ChatTurn }): React.ReactElement | null {
  if (turn.status === "searching") return null;
  if (turn.status === "streaming") {
    return <Typography variant="caption" color="primary.main">正在生成…</Typography>;
  }
  if (turn.status === "error") {
    return <Typography variant="caption" color="error.main">生成失败</Typography>;
  }
  if (turn.status === "stopped") {
    return <Typography variant="caption" color="text.secondary">已停止</Typography>;
  }
  const mode = MODE_OPTIONS.find((m) => m.value === turn.mode)?.label ?? turn.mode ?? "";
  const bits = [
    turn.tookMs != null ? `${turn.tookMs}ms` : "",
    mode,
    turn.degradedReason ? `降级：${turn.degradedReason}` : "",
  ].filter(Boolean);
  return (
    <Typography variant="caption" color="text.secondary">
      {bits.join(" · ") || "已完成"}
    </Typography>
  );
}