import { useEffect, useRef, useState } from "react";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";
import SendIcon from "@mui/icons-material/Send";
import StopCircleIcon from "@mui/icons-material/StopCircle";

import { streamPost } from "../lib/sse";
import type { StreamHandle } from "../lib/sse";
import { href } from "../lib/router";
import type { Citation } from "../lib/types";

/**
 * 问答面板：基于检索结果的流式回答（SSE）。
 *
 * 两个必须保留的实现细节（都是 Svelte 版实测换来的）：
 *
 * 1. **streamPost 必须立刻返回 handle**，不能 await 到流结束——否则
 *    "停止"按钮在生成期间根本没渲染出来，"随时可中断"恰好失效。
 * 2. **增量按帧 flush**（rAF 合并）：每个 token 触发一次 setState 会让
 *    长答案 O(n²) 重渲染；合并到每帧一次，观感相同而 CPU 差一个数量级。
 */
export default function AnswerPanel({
  defaultQuestion = "",
  kbId = "",
}: {
  defaultQuestion?: string;
  kbId?: string;
}): React.ReactElement {
  const [question, setQuestion] = useState(defaultQuestion);
  const [answer, setAnswer] = useState("");
  const [citations, setCitations] = useState<Citation[]>([]);
  const [running, setRunning] = useState(false);
  const [info, setInfo] = useState("");
  const [error, setError] = useState("");

  const handleRef = useRef<StreamHandle | null>(null);
  const bufRef = useRef("");
  const rafRef = useRef<number | null>(null);
  const answerBox = useRef<HTMLDivElement | null>(null);
  /** 用户是否贴着底部：只有贴底时才自动跟随，往上翻看时不抢滚动 */
  const stick = useRef(true);

  // 组件卸载时断开流：否则用户走了，连接还在推数据
  useEffect(() => () => {
    handleRef.current?.stop();
    if (rafRef.current != null) cancelAnimationFrame(rafRef.current);
  }, []);

  // 从检索页换了一次查询再打开问答：问题框跟随当前查询词
  useEffect(() => {
    if (!running) setQuestion(defaultQuestion);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [defaultQuestion]);

  function flush(): void {
    if (rafRef.current != null) return;
    rafRef.current = requestAnimationFrame(() => {
      rafRef.current = null;
      setAnswer(bufRef.current);
    });
  }

  function ask(): void {
    const q = question.trim();
    if (!q || running) return;
    setAnswer("");
    bufRef.current = "";
    setCitations([]);
    setInfo("");
    setError("");
    setRunning(true);
    stick.current = true;

    handleRef.current = streamPost(
      "/api/answer/stream",
      { q, ...(kbId ? { kb_id: kbId } : {}), top_k: 8 },
      {
        onSources: (c) => setCitations(c as Citation[]),
        onDelta: (t) => {
          bufRef.current += t;
          flush();
        },
        onDone: (i) => {
          // 收尾时把缓冲里剩余的一并落盘（最后一帧可能还没跑）
          setAnswer(bufRef.current);
          setRunning(false);
          setInfo(`${i.took_ms}ms · ${i.mode}${i.degraded_reason ? ` · 已降级（${i.degraded_reason}）` : ""}`);
        },
        onError: (message, hint) => {
          setRunning(false);
          setError(hint ? `${message}（${hint}）` : message);
        },
      },
    );
  }

  // 流式输出时贴底跟随：读的是 answer/run，缺一个就只在开始那帧滚一次空内容
  useEffect(() => {
    const el = answerBox.current;
    if (!el || !running || !stick.current) return;
    el.scrollTop = el.scrollHeight;
  }, [answer, running]);

  function stop(): void {
    handleRef.current?.stop();
    handleRef.current = null;
    setAnswer(bufRef.current);
    setRunning(false);
  }

  return (
    <Paper variant="outlined" sx={{ p: 2 }}>
      <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1.5 }}>
        <Typography variant="subtitle2" sx={{ flex: 1 }}>
          问答（基于检索结果生成，带引用）
        </Typography>
        {info && <Typography variant="caption" color="text.secondary">{info}</Typography>}
      </Stack>

      <Stack direction="row" spacing={1} sx={{ mb: 2 }}>
        <TextField
          fullWidth size="small"
          placeholder="就当前知识库提问…"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") ask(); }}
          slotProps={{ htmlInput: { "aria-label": "提问" } }}
        />
        {running ? (
          <Button color="error" variant="outlined" startIcon={<StopCircleIcon />} onClick={stop}>
            停止
          </Button>
        ) : (
          <Button variant="contained" startIcon={<SendIcon />} disabled={!question.trim()} onClick={ask}>
            提问
          </Button>
        )}
      </Stack>

      {error && (
        <Alert
          severity="error"
          sx={{ mb: 2 }}
          action={<Button color="inherit" size="small" onClick={ask}>重试</Button>}
        >
          {error}
        </Alert>
      )}

      {citations.length > 0 && (
        <Box sx={{ mb: 2 }}>
          <Typography variant="caption" color="text.secondary">引用来源</Typography>
          <Stack direction="row" spacing={0.5} flexWrap="wrap" sx={{ mt: 0.5 }}>
            {citations.map((c) => (
              <Chip
                key={c.index}
                size="small"
                variant="outlined"
                label={`[${c.index}] ${c.title || "独立分块"}${c.page ? ` · 第 ${c.page} 页` : ""}`}
                component="a"
                href={c.doc_id ? href("kb", c.kb_id || kbId, "doc", c.doc_id) : undefined}
                clickable={!!(c.doc_id && (c.kb_id || kbId))}
                title={c.heading_path || ""}
              />
            ))}
          </Stack>
        </Box>
      )}

      {(answer || running) && (
        <Box
          ref={answerBox}
          onScroll={(e) => {
            const el = e.currentTarget;
            stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 48;
          }}
          sx={{ bgcolor: "action.hover", borderRadius: 1, p: 2, maxHeight: "48vh", overflowY: "auto" }}
        >
          {answer ? (
            <Typography variant="body2" sx={{ whiteSpace: "pre-wrap", lineHeight: 1.8 }}>
              {answer}
            </Typography>
          ) : (
            <Stack direction="row" spacing={1} alignItems="center">
              <CircularProgress size={14} />
              <Typography variant="body2" color="text.secondary">正在生成…</Typography>
            </Stack>
          )}
        </Box>
      )}
    </Paper>
  );
}
