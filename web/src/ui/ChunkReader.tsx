import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import CircularProgress from "@mui/material/CircularProgress";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import { alpha } from "@mui/material/styles";
import ContentCopyIcon from "@mui/icons-material/ContentCopy";
import OpenInNewIcon from "@mui/icons-material/OpenInNew";

import { api } from "../lib/api";
import { href, queryString } from "../lib/router";
import type { Chunk, SearchHit, Snippet } from "../lib/types";
import { toast } from "./toast";

export interface ChunkReaderProps {
  /** 要阅读的分块。 */
  chunkId: string;
  /** 检索命中：补齐元信息，并提供命中片段用于在原文里定位高亮。 */
  hit: SearchHit | null;
}

/**
 * 分块原文阅读器。
 *
 * 检索命中给的是**片段**（命中词附近的一小段），读者真正想看的是这个
 * 分块在原始文件里的完整文本。这里按 chunk_id 取回整块原文（`GET
 * /api/chunks/{id}`），把命中的那一段在原文里标出来，其余保持原样。
 *
 * 读不到原文（分块已删、无权限、请求失败）时退回检索片段，并说明原因，
 * 不给读者一片空白。
 */
export default function ChunkReader({
  chunkId, hit,
}: ChunkReaderProps): React.ReactElement {
  const [chunk, setChunk] = useState<Chunk | null>(null);
  const [phase, setPhase] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState("");
  const [showOriginal, setShowOriginal] = useState(false);

  useEffect(() => {
    const ctrl = new AbortController();
    setChunk(null);
    setPhase("loading");
    setError("");
    setShowOriginal(false);
    api<Chunk>(`/api/chunks/${encodeURIComponent(chunkId)}`, { signal: ctrl.signal })
      .then((c) => {
        if (ctrl.signal.aborted) return;
        setChunk(c);
        setPhase("ready");
      })
      .catch((e) => {
        if (ctrl.signal.aborted) return;
        setError(e instanceof Error ? e.message : String(e));
        setPhase("error");
      });
    return () => ctrl.abort();
  }, [chunkId]);

  const body = chunk
    ? (showOriginal && chunk.original_text ? chunk.original_text : chunk.text)
    : (hit?.snippet.text ?? "");
  const marks = showOriginal ? [] : locateSnippet(body, hit?.snippet);

  const docId = chunk?.doc_id || hit?.doc_id || "";
  const kbId = chunk?.kb_id || hit?.kb_id || "";

  async function copyText(): Promise<void> {
    try {
      await navigator.clipboard.writeText(body);
      toast("已复制原文");
    } catch {
      toast("复制失败，请手动选择文本", "err");
    }
  }

  return (
    <Box>
      <Stack
        direction="row"
        spacing={0.75}
        alignItems="center"
        useFlexGap
        flexWrap="wrap"
      >
        <Typography variant="subtitle2" sx={{ fontWeight: 600, lineHeight: 1.4 }}>
          {hit?.title || "独立分块"}
        </Typography>
        {chunk?.page != null && (
          <Typography variant="caption" color="text.secondary">
            第 {chunk.page} 页
          </Typography>
        )}
        {hit && (
          <Typography variant="caption" color="text.secondary">
            {hit.score.toFixed(3)} · {hit.score_kind}
          </Typography>
        )}
        {chunk?.edited && (
          <Typography variant="caption" color="warning.main">已修订</Typography>
        )}
      </Stack>

      <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 0.25 }}>
        {chunk?.heading_path || "未归类"} · 第 {(chunk?.ordinal ?? hit?.ordinal ?? 0) + 1} 块
      </Typography>

      <Box
        aria-label="分块原文"
        sx={{
          mt: 1.25,
          pt: 1.25,
          borderTop: 1,
          borderColor: "divider",
          fontSize: 14,
          lineHeight: 1.9,
          whiteSpace: "pre-wrap",
          wordBreak: "break-word",
          "& mark": {
            color: "inherit",
            bgcolor: (t) => alpha(t.palette.warning.main, 0.42),
            px: 0.25,
            borderRadius: 0.5,
          },
        }}
      >
        {phase === "loading" && (
          <Stack direction="row" spacing={1} alignItems="center">
            <CircularProgress size={14} />
            <Typography variant="body2" color="text.secondary">
              正在读取原文…
            </Typography>
          </Stack>
        )}
        {phase === "error" && (
          <>
            <Alert severity="warning" sx={{ mb: 1.5 }}>
              原文读取失败：{error}
            </Alert>
            {hit && (
              <>
                <Typography variant="caption" color="text.secondary" sx={{ fontWeight: 600 }}>
                  检索命中片段
                </Typography>
                <Box sx={{ mt: 0.5 }}>{hit.snippet.text}</Box>
              </>
            )}
          </>
        )}
        {phase === "ready" && <MarkedText body={body} marks={marks} />}
      </Box>

      {phase === "ready" && !body.trim() && (
        <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
          这个分块没有正文内容。
        </Typography>
      )}

      <Stack
        direction="row"
        spacing={0.75}
        useFlexGap
        flexWrap="wrap"
        sx={{ mt: 1.75, alignItems: "center" }}
      >
        <Button size="small" startIcon={<ContentCopyIcon />} onClick={() => void copyText()}>
          复制原文
        </Button>
        {docId && kbId && (
          <Button
            size="small"
            component="a"
            href={`${href("kb", kbId, "doc", docId)}${queryString({ chunk: chunkId })}`}
            startIcon={<OpenInNewIcon />}
          >
            在文档中打开
          </Button>
        )}
        {chunk?.edited && chunk.original_text && (
          <Button size="small" onClick={() => setShowOriginal((v) => !v)}>
            {showOriginal ? "看修订后" : "看修订前"}
          </Button>
        )}
      </Stack>

      {phase === "ready" && !docId && (
        <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 1 }}>
          这是独立分块，没有所属文档——上面的内容就是它的全文。
        </Typography>
      )}
    </Box>
  );
}

/**
 * 在原文中定位命中片段，并换算成原文坐标。
 *
 * 后端 `make_snippet` 只是对分块文本做原样切片，不改写字符，因此片段
 * 一定是原文的子串，可以精确 indexOf；片段里的高亮区间（命中词）再平
 * 移到原文坐标——**高亮的是命中词，不是整段片段**（短分块的片段就是
 * 全文，整段标黄等于什么都没标）。
 *
 * 找不到就整段不高亮：宁可没有标记，也不做会标错位置的模糊猜测。
 */
function locateSnippet(
  body: string, snippet: Snippet | undefined,
): Array<[number, number]> {
  if (!snippet?.text || !body) return [];
  let at = body.indexOf(snippet.text);
  let shift = 0;
  if (at < 0) {
    const trimmed = snippet.text.trim();
    if (!trimmed) return [];
    at = body.indexOf(trimmed);
    if (at < 0) return [];
    shift = snippet.text.indexOf(trimmed);
  }
  return snippet.marks
    .filter(([s, e]) => e > s)
    .map(([s, e]) => [at + s - shift, at + e - shift] as [number, number])
    .filter(([s, e]) => s >= 0 && e <= body.length);
}

/** 正文 + 高亮区间。纯文本插值，不拼 HTML。 */
function MarkedText({
  body, marks,
}: {
  body: string;
  marks: Array<[number, number]>;
}): React.ReactElement {
  if (marks.length === 0) return <>{body}</>;
  const parts: ReactNode[] = [];
  let cur = 0;
  for (const [s, e] of marks) {
    if (s < cur) continue;
    if (s > cur) parts.push(body.slice(cur, s));
    parts.push(<mark key={`${s}-${e}`}>{body.slice(s, e)}</mark>);
    cur = e;
  }
  parts.push(body.slice(cur));
  return <>{parts}</>;
}
