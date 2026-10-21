import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import Accordion from "@mui/material/Accordion";
import AccordionDetails from "@mui/material/AccordionDetails";
import AccordionSummary from "@mui/material/AccordionSummary";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import ButtonBase from "@mui/material/ButtonBase";
import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
import FormControlLabel from "@mui/material/FormControlLabel";
import IconButton from "@mui/material/IconButton";
import InputAdornment from "@mui/material/InputAdornment";
import MenuItem from "@mui/material/MenuItem";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import Switch from "@mui/material/Switch";
import TextField from "@mui/material/TextField";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import CloseIcon from "@mui/icons-material/Close";
import DownloadIcon from "@mui/icons-material/Download";
import ExpandMoreIcon from "@mui/icons-material/ExpandMore";
import OpenInNewIcon from "@mui/icons-material/OpenInNew";
import QuestionAnswerIcon from "@mui/icons-material/QuestionAnswer";
import SearchIcon from "@mui/icons-material/Search";
import TuneIcon from "@mui/icons-material/Tune";

import { api, apiText } from "../lib/api";
import { go, href, useRoute } from "../lib/router";
import { setCrumbs, useStore } from "../lib/store";
import { DEFAULTS, PARAMS, searchUrl, toBody } from "../lib/searchparams";
import type { Document, ModelsConfig, SearchHit, SearchResponse, Snippet } from "../lib/types";
import AnswerPanel from "../ui/AnswerPanel";
import { toast } from "../ui/toast";

/**
 * 检索页：查询为主角 + 参数面板（草稿/应用两态）+ 结果 + 原文检视 + 导出 + 问答。
 *
 * URL 是唯一事实来源：查询词与**已应用**的参数都在 URL 上，改 URL 触发检索。
 * 参数面板持有草稿（draft），点「应用」才写进 URL——这条边界是守住的
 * 不变量：面板显示的参数必须与结果的来源一致，否则用户会以为"改了参数
 * 但结果没变"（或者更糟：以为结果来自旧参数）。
 */
export default function Search(): React.ReactElement {
  const route = useRoute();
  const { kbs } = useStore();
  const qFromUrl = route.query.get("q") ?? "";

  const [q, setQ] = useState(qFromUrl);
  useEffect(() => setQ(qFromUrl), [qFromUrl]);
  const [resp, setResp] = useState<SearchResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<SearchHit | null>(null);
  const [answerOpen, setAnswerOpen] = useState(false);
  const [paramsOpen, setParamsOpen] = useState(false);
  const [answerEnabled, setAnswerEnabled] = useState(false);
  const inputRef = useRef<HTMLInputElement | null>(null);
  // 请求序号 + AbortController：快速改参数/后退时，慢的旧请求不能覆盖新结果
  const reqSeq = useRef(0);

  /** 已应用的参数（来自 URL） */
  const applied = useMemo(() => {
    const out: Record<string, string> = {};
    for (const p of PARAMS) out[p.key] = route.query.get(p.key) ?? DEFAULTS[p.key] ?? "";
    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [route.query.toString()]);

  /** 草稿：编辑中、尚未应用 */
  const [draft, setDraft] = useState<Record<string, string>>(applied);
  useEffect(() => {
    setDraft(applied);
  }, [applied]);

  const dirty = useMemo(
    () => PARAMS.filter((p) => (draft[p.key] ?? "") !== (applied[p.key] ?? "")).length,
    [draft, applied],
  );

  useEffect(() => {
    setCrumbs([{ label: "检索" }]);
    // 问答是 features.answer 控制的功能；入口必须按真实开关禁用，
    // 而不是让用户点了「提问」才收到 503。
    void api<ModelsConfig>("/api/models")
      .then((m) => setAnswerEnabled(m.features?.answer === true))
      .catch(() => setAnswerEnabled(false));
  }, []);

  // `/` 聚焦查询框（表单控件里不接管，与旧版同口径）
  useEffect(() => {
    const onKey = (e: KeyboardEvent): void => {
      const t = e.target as HTMLElement | null;
      if (t && (/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || t.isContentEditable)) return;
      if (t && t.closest('[role="dialog"]')) return;
      if (e.key === "/" && !e.ctrlKey && !e.metaKey) {
        e.preventDefault();
        inputRef.current?.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const run = useCallback(async (signal?: AbortSignal): Promise<void> => {
    const seq = ++reqSeq.current;
    if (!qFromUrl.trim()) {
      setResp(null);
      setSelected(null);
      setError("");
      setBusy(false);
      return;
    }
    setBusy(true);
    setError("");
    try {
      const r = await api<SearchResponse>("/api/search", {
        method: "POST",
        body: toBody(qFromUrl, route.query),
        signal,
      });
      if (seq !== reqSeq.current) return;   // 更新的检索已经在飞
      setResp(r);
      setSelected(r.results[0] ?? null);
    } catch (e) {
      if (seq !== reqSeq.current) return;
      if (signal?.aborted || (e instanceof DOMException && e.name === "AbortError")) return;
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      if (seq === reqSeq.current) setBusy(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [qFromUrl, route.query.toString()]);

  // URL 变化即检索（唯一数据流：改 URL → 检索）。
  // 清理时中止上一发：否则旧参数的结果可能在新结果之后落地，
  // 屏幕上的参数与结果集再次脱节。
  useEffect(() => {
    const ctrl = new AbortController();
    void run(ctrl.signal);
    return () => ctrl.abort();
  }, [run]);

  function submit(): void {
    const text = q.trim();
    if (!text) return;
    // 参数是用户的设置：换查询词不该把它们清掉，只更新 q。
    // searchUrl 会丢掉等于默认值的项，保持分享链接精简。
    go(searchUrl(text, applied));
  }

  function applyParams(): void {
    const clean: Record<string, string> = {};
    for (const p of PARAMS) {
      const v = draft[p.key] ?? "";
      if (v !== (DEFAULTS[p.key] ?? "")) clean[p.key] = v;
    }
    go(searchUrl(qFromUrl, clean));
  }

  async function exportReport(): Promise<void> {
    try {
      const md = await apiText("/api/search/report", {
        method: "POST",
        body: toBody(qFromUrl, route.query),
      });
      const blob = new Blob([md], { type: "text/markdown;charset=utf-8" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `检索报告-${new Date().toISOString().slice(0, 10)}.md`;
      a.click();
      URL.revokeObjectURL(a.href);
      toast("报告已导出");
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), "err");
    }
  }

  return (
    <Box sx={{ p: { xs: 2, md: 3 }, maxWidth: 1440, mx: "auto" }}>
      {/* ---- 查询区 ---- */}
      <Paper
        variant="outlined"
        sx={{ p: 2, mb: 2, position: "sticky", top: 0, zIndex: 5, bgcolor: "background.paper" }}
      >
        <Stack direction="row" spacing={1} alignItems="center">
          <TextField
            inputRef={inputRef}
            fullWidth
            placeholder="输入查询词，回车检索（按 / 聚焦）"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") submit(); }}
            slotProps={{
              input: {
                startAdornment: (
                  <InputAdornment position="start"><SearchIcon /></InputAdornment>
                ),
              },
              htmlInput: { "aria-label": "检索查询词" },
            }}
          />
          <Button variant="contained" onClick={submit} disabled={!q.trim() || busy}>
            检索
          </Button>
          <Tooltip title="参数（17 项，含过滤与高级）">
            <Button
              variant={paramsOpen ? "contained" : "outlined"}
              color="inherit"
              startIcon={<TuneIcon />}
              sx={{ whiteSpace: "nowrap" }}
              onClick={() => setParamsOpen((v) => !v)}
            >
              参数{dirty > 0 ? ` (${dirty})` : ""}
            </Button>
          </Tooltip>
          <Tooltip
            title={answerEnabled
              ? "基于检索结果生成回答（SSE 流式）"
              : "生成式问答未启用：在配置中设置 features.answer=true 后重启"}
          >
            <span>
              <Button
                variant={answerOpen ? "contained" : "outlined"}
                color="inherit"
                startIcon={<QuestionAnswerIcon />}
                sx={{ whiteSpace: "nowrap" }}
                disabled={!answerEnabled}
                onClick={() => setAnswerOpen((v) => !v)}
              >
                问答
              </Button>
            </span>
          </Tooltip>
        </Stack>

        {/* 生效参数摘要（有非默认项时显示，点 × 清除） */}
        <Stack direction="row" spacing={1} flexWrap="wrap" sx={{ mt: appliedChips(applied).length ? 1.5 : 0 }}>
          {appliedChips(applied).map(([k, v]) => (
            <Chip
              key={k}
              size="small"
              variant="outlined"
              label={`${PARAMS.find((p) => p.key === k)?.label ?? k}: ${v}`}
              onDelete={() => {
                const next = { ...applied };
                delete next[k];
                go(searchUrl(qFromUrl, next));
              }}
            />
          ))}
        </Stack>
      </Paper>

      {/* ---- 参数面板 ---- */}
      {paramsOpen && (
        <Paper variant="outlined" sx={{ p: 2, mb: 2 }}>
          <Stack direction="row" spacing={2} alignItems="center" sx={{ mb: 2 }}>
            <Typography variant="subtitle2" sx={{ flex: 1 }}>
              参数面板{dirty > 0 && ` · ${dirty} 项未应用`}
            </Typography>
            <Button size="small" onClick={() => setDraft(applied)} disabled={dirty === 0}>
              丢弃修改
            </Button>
            <Button size="small" variant="contained" onClick={applyParams} disabled={dirty === 0}>
              应用并检索
            </Button>
          </Stack>
          <Stack direction={{ xs: "column", md: "row" }} spacing={2} flexWrap="wrap" useFlexGap>
            {PARAMS.filter((p) => p.group === "base").map((p) => (
              <ParamControl key={p.key} p={p} value={draft[p.key] ?? ""} kbs={kbs}
                onChange={(v) => setDraft((d) => ({ ...d, [p.key]: v }))} />
            ))}
          </Stack>
          <Accordion disableGutters elevation={0} sx={{ mt: 1, "&:before": { display: "none" } }}>
            <AccordionSummary expandIcon={<ExpandMoreIcon />}>
              <Typography variant="body2" color="text.secondary">
                过滤与高级参数（{PARAMS.filter((p) => p.group !== "base").length} 项）
              </Typography>
            </AccordionSummary>
            <AccordionDetails>
              <Stack direction={{ xs: "column", md: "row" }} spacing={2} flexWrap="wrap" useFlexGap>
                {PARAMS.filter((p) => p.group !== "base").map((p) => (
                  <ParamControl key={p.key} p={p} value={draft[p.key] ?? ""} kbs={kbs}
                    onChange={(v) => setDraft((d) => ({ ...d, [p.key]: v }))} />
                ))}
              </Stack>
            </AccordionDetails>
          </Accordion>
        </Paper>
      )}

      {/* ---- 问答面板 ---- */}
      {answerOpen && (
        <Box sx={{ mb: 2 }}>
          <AnswerPanel defaultQuestion={qFromUrl} kbId={applied.kb_id ?? ""} />
        </Box>
      )}

      {/* ---- 结果区 ---- */}
      {error && (
        <Alert
          severity="error"
          sx={{ mb: 2 }}
          action={<Button color="inherit" size="small" onClick={() => void run()}>重试</Button>}
        >
          {error}
        </Alert>
      )}

      {busy && (
        <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 2 }}>
          <CircularProgress size={16} />
          <Typography variant="body2" color="text.secondary">检索中…</Typography>
        </Stack>
      )}

      {resp && !busy && (
        <>
          <Stack direction="row" spacing={2} alignItems="center" sx={{ mb: 1.5 }}>
            <Typography variant="body2" color="text.secondary" sx={{ flex: 1 }}>
              「{resp.query}」 · 显示 {resp.results.length} 条
              {resp.total_candidates > resp.results.length
                ? `（候选 ${resp.total_candidates} 条，可调大「返回条数」查看更多）`
                : ""}
              {" · "}{resp.took_ms}ms · {resp.mode}
              {resp.degraded_reason && ` · 已降级（${resp.degraded_reason}）`}
            </Typography>
            <Button size="small" startIcon={<DownloadIcon />} onClick={() => void exportReport()}>
              导出报告
            </Button>
          </Stack>

          {resp.results.length === 0 && (
            <Alert severity="warning">
              没有匹配的结果。试试去掉过滤条件、放宽分数阈值，或换成更短的关键词。
            </Alert>
          )}

          <Box
            sx={{
              display: "grid", gap: 2,
              gridTemplateColumns: { xs: "1fr", lg: selected ? "minmax(0,1.1fr) minmax(0,1fr)" : "1fr" },
              alignItems: "start",
            }}
          >
            <Stack spacing={1}>
              {resp.results.map((h) => (
                <ResultCard
                  key={h.chunk_id}
                  hit={h}
                  active={selected?.chunk_id === h.chunk_id}
                  onClick={() => setSelected(h)}
                />
              ))}
            </Stack>

            {selected && (
              <DocPeek hit={selected} onClose={() => setSelected(null)} />
            )}
          </Box>
        </>
      )}

      {!resp && !busy && !error && (
        <Alert severity="info">
          输入查询词后回车。检索在知识库之上执行：混合（向量 + 全文）、纯向量、纯全文三种模式，
          参数面板里可以调过滤与高级选项。
        </Alert>
      )}
    </Box>
  );
}

/** 已应用的非默认参数（用于 chip 摘要） */
function appliedChips(applied: Record<string, string>): Array<[string, string]> {
  return Object.entries(applied).filter(
    ([k, v]) => v && v !== (DEFAULTS[k] ?? ""),
  );
}

/** 参数控件：按 kind 映射到 MUI 控件（规格表是唯一来源，见 searchparams.ts） */
function ParamControl({
  p, value, kbs, onChange,
}: {
  p: (typeof PARAMS)[number];
  value: string;
  kbs: Array<{ kb_id: string; name: string }>;
  onChange: (v: string) => void;
}): React.ReactElement {
  if (p.key === "kb_id") {
    return (
      <TextField id={p.id} select size="small" label={p.label} value={value}
        onChange={(e) => onChange(e.target.value)} sx={{ minWidth: 180 }}>
        <MenuItem value="">全部知识库</MenuItem>
        {kbs.map((k) => <MenuItem key={k.kb_id} value={k.kb_id}>{k.name}</MenuItem>)}
      </TextField>
    );
  }
  if (p.key === "mode") {
    return (
      <TextField id={p.id} select size="small" label={p.label} value={value || "hybrid"}
        onChange={(e) => onChange(e.target.value)} sx={{ minWidth: 150 }}>
        <MenuItem value="hybrid">混合（向量 + 全文）</MenuItem>
        <MenuItem value="vector">纯向量</MenuItem>
        <MenuItem value="fts">纯全文</MenuItem>
      </TextField>
    );
  }
  if (p.key === "group_by_doc") {
    return (
      <TextField id={p.id} select size="small" label={p.label} value={value || "false"}
        onChange={(e) => onChange(e.target.value)} sx={{ minWidth: 140 }}>
        <MenuItem value="false">否</MenuItem>
        <MenuItem value="true">是（每篇文档留最优块）</MenuItem>
      </TextField>
    );
  }
  if (p.kind === "bool") {
    return (
      <FormControlLabel
        sx={{ minWidth: 140 }}
        control={
          <Switch
            id={p.id}
            checked={value === "true"}
            onChange={(e) => onChange(e.target.checked ? "true" : "false")}
          />
        }
        label={p.label}
      />
    );
  }
  if (p.kind === "num") {
    return (
      <TextField
        id={p.id}
        size="small" type="number" label={p.label} value={value}
        onChange={(e) => onChange(e.target.value)} sx={{ width: 150 }}
      />
    );
  }
  return (
    <TextField
      id={p.id}
      size="small" label={p.label} value={value}
      onChange={(e) => onChange(e.target.value)} sx={{ width: 180 }}
    />
  );
}

/** 检索片段：纯文本 + 高亮区间（**纯插值**，不注入 HTML） */
function SnippetText({ snippet }: { snippet: Snippet }): React.ReactElement {
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

function ResultCard({
  hit, active, onClick,
}: {
  hit: SearchHit;
  active: boolean;
  onClick: () => void;
}): React.ReactElement {
  return (
    <Paper
      variant="outlined"
      sx={{
        borderColor: active ? "primary.main" : "divider",
        bgcolor: active ? "action.selected" : "background.paper",
        "&:hover": { bgcolor: "action.hover" },
      }}
    >
      <ButtonBase
        onClick={onClick}
        sx={{ display: "block", width: "100%", textAlign: "left", p: 1.5 }}
      >
      <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 0.5 }}>
        <Chip size="small" color="primary" variant="outlined"
          label={hit.score.toFixed(3)} title={`相关性（${hit.score_kind}，越大越相关）`} />
        <Typography variant="body2" sx={{ fontWeight: 500, flex: 1, minWidth: 0 }}
          noWrap title={hit.title || "（独立分块）"}>
          {hit.title || "（独立分块）"}
        </Typography>
        {hit.page != null && <Chip size="small" label={`第 ${hit.page} 页`} variant="outlined" />}
        {!hit.enabled && <Chip size="small" color="error" label="已停用" variant="outlined" />}
      </Stack>
      {hit.heading_path && (
        <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 0.5 }}
          noWrap>
          {hit.heading_path}
        </Typography>
      )}
      <Typography variant="body2" color="text.secondary" sx={{ lineHeight: 1.7 }}>
        <SnippetText snippet={hit.snippet} />
      </Typography>
      </ButtonBase>
    </Paper>
  );
}

/** 原文检视：按命中偏移在全文里高亮（偏移失效时如实降级为不定位） */
function DocPeek({ hit, onClose }: { hit: SearchHit; onClose: () => void }): React.ReactElement {
  const [doc, setDoc] = useState<Document | null>(null);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);

  useEffect(() => {
    const ctrl = new AbortController();
    setDoc(null);
    setError("");
    if (!hit.doc_id) return () => ctrl.abort();
    void api<Document>(
      `/api/documents/${encodeURIComponent(hit.doc_id)}?include_chunks=false`,
      { signal: ctrl.signal },
    )
      .then(setDoc)
      .catch((e) => {
        if (ctrl.signal.aborted) return;
        setError(e instanceof Error ? e.message : String(e));
      });
    return () => ctrl.abort();
  }, [hit.doc_id, retry]);

  const body = doc?.text ?? "";
  const canLocate = doc != null && hit.offset_valid
    && hit.char_start >= 0 && hit.char_end <= body.length && hit.char_end > hit.char_start;

  return (
    <Paper variant="outlined" sx={{ position: "sticky", top: 84, maxHeight: "72vh", display: "flex", flexDirection: "column" }}>
      <Stack direction="row" spacing={1} alignItems="center" sx={{ p: 1.5, borderBottom: 1, borderColor: "divider" }}>
        <Typography variant="body2" sx={{ fontWeight: 500, flex: 1, minWidth: 0 }} noWrap>
          {hit.title || "（独立分块）"}
        </Typography>
        {hit.doc_id && (
          <Tooltip title="在文档工作台打开">
            <Button size="small" endIcon={<OpenInNewIcon />}
              href={href("kb", hit.kb_id, "doc", hit.doc_id)}>
              打开
            </Button>
          </Tooltip>
        )}
        <IconButton size="small" aria-label="关闭原文检视" onClick={onClose}>
          <CloseIcon fontSize="small" />
        </IconButton>
      </Stack>
      <Box sx={{ p: 2, overflowY: "auto", flex: 1 }}>
        {error && (
          <Alert
            severity="error"
            action={<Button color="inherit" size="small" onClick={() => setRetry((n) => n + 1)}>重试</Button>}
          >
            {error}
          </Alert>
        )}
        {/* 独立分块没有文档可加载：直接呈现片段本身。
            早先把这一支写在 `doc &&` 里面，于是它永远不显示——
            而 loading 转圈也永远停不下来（doc 永远是 null）。 */}
        {!error && !hit.doc_id && (
          <Alert severity="info">
            这是独立分块（不属于任何文档），显示其内容：
            <Typography variant="body2" sx={{ mt: 1, whiteSpace: "pre-wrap" }}>
              {hit.snippet.text}
            </Typography>
          </Alert>
        )}
        {!error && hit.doc_id && !doc && <CircularProgress size={18} />}
        {!error && hit.doc_id && doc && (
          <>
            {!hit.offset_valid && (
              <Alert severity="info" sx={{ mb: 1 }}>
                该分块被编辑过，偏移已失效——下面是全文，不再定位。
              </Alert>
            )}
            {(
              <Typography
                variant="body2"
                component="pre"
                sx={{
                  whiteSpace: "pre-wrap", wordBreak: "break-word",
                  fontFamily: "inherit", lineHeight: 1.8, m: 0,
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
            )}
          </>
        )}
      </Box>
    </Paper>
  );
}
