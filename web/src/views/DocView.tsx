import { useCallback, useEffect, useRef, useState } from "react";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Checkbox from "@mui/material/Checkbox";
import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
import Divider from "@mui/material/Divider";
import IconButton from "@mui/material/IconButton";
import Menu from "@mui/material/Menu";
import MenuItem from "@mui/material/MenuItem";
import { GlassPanel, useGlassLook } from "../ui/glass";
import Skeleton from "@mui/material/Skeleton";
import Stack from "@mui/material/Stack";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TablePagination from "@mui/material/TablePagination";
import TableRow from "@mui/material/TableRow";
import TextField from "@mui/material/TextField";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import AddIcon from "@mui/icons-material/Add";
import DeleteIcon from "@mui/icons-material/Delete";
import DescriptionIcon from "@mui/icons-material/Description";
import DownloadIcon from "@mui/icons-material/Download";
import EditIcon from "@mui/icons-material/Edit";
import MoreVertIcon from "@mui/icons-material/MoreVert";
import OpenInNewIcon from "@mui/icons-material/OpenInNew";
import RefreshIcon from "@mui/icons-material/Refresh";
import VisibilityIcon from "@mui/icons-material/Visibility";
import VisibilityOffIcon from "@mui/icons-material/VisibilityOff";

import { api } from "../lib/api";
import { CONTENT_MAX } from "../lib/layout";
import { stageLabel, waitForJob } from "../lib/jobs";
import {
  addChunkDialog, batchSetEnabled, deleteChunk, deleteDocDialog,
  editChunkDialog, editDocPlanDialog, moveDocDialog, renameDocDialog,
  reparseDoc, resplitDoc, setChunkEnabled,
} from "../lib/ops";
import { go, href, queryString, useRoute } from "../lib/router";
import { usePicks } from "../lib/selection";
import { downloadUrl, pageUrl, sourceKind } from "../lib/source";
import { invalidateKbs, kbName, loadKbs, setCrumbs, useStore } from "../lib/store";
import type { Chunk, ChunkList, DocPlan, Document } from "../lib/types";
import { strategyLabel } from "../lib/types";
import { toast } from "../ui/toast";

const PAGE_SIZE = 20;
const A4_RATIO = 595 / 842;

/**
 * 文档工作台：分块表是主角，原文按需展开（`?src=1`）。
 *
 * 三条从旧版继承的判断（都有实测背景）：
 * 1. 原文默认折叠；深链带 `?chunk=` 时自动展开并定位。
 * 2. 分块面板随内容收缩，不被 `flex: 1` 撑成一屏空白。
 * 3. 分页在服务端，深链块可能不在第一页——先按 ordinal 找到它所在的页。
 */
export default function DocView({
  kbId,
  docId,
}: {
  kbId: string;
  docId: string;
}): React.ReactElement {
  const look = useGlassLook("card");
  const route = useRoute();
  const { kbsLoaded } = useStore();

  const [doc, setDoc] = useState<Document | null>(null);
  const [plan, setPlan] = useState<DocPlan | null>(null);
  const [chunks, setChunks] = useState<Chunk[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(0);
  const [filter, setFilter] = useState("");
  const [phase, setPhase] = useState<"initial" | "ready" | "failed">("initial");
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<Chunk | null>(null);
  const [jobNote, setJobNote] = useState("");
  const [menuEl, setMenuEl] = useState<HTMLElement | null>(null);
  const picks = usePicks();

  const [showSrc, setShowSrc] = useState(
    () => route.query.get("src") === "1" || !!route.query.get("chunk"),
  );
  const [textBody, setTextBody] = useState("");
  const [textError, setTextError] = useState("");
  const [frameSize, setFrameSize] = useState<{ w: number; h: number } | null>(null);

  const jobAbort = useRef<AbortController | null>(null);
  const reqSeq = useRef(0);
  const chunkConsumed = useRef(false);
  const pageRef = useRef(0);
  const qRef = useRef("");
  const sourceScroll = useRef<HTMLDivElement | null>(null);
  const sourceBox = useRef<HTMLDivElement | null>(null);
  const hitRef = useRef<HTMLElement | null>(null);
  const rowRefs = useRef<Record<string, HTMLTableRowElement | null>>({});

  const kind = doc ? sourceKind(doc.mime ?? "", doc.stored_file ?? "") : "download";
  const fileUrl = doc?.file_url ?? "";
  const pageNum = selected?.page ?? null;

  const load = useCallback(
    async (targetPage = pageRef.current, query = qRef.current): Promise<void> => {
      const seq = ++reqSeq.current;
      try {
        const sp = new URLSearchParams({
          doc_id: docId,
          limit: String(PAGE_SIZE),
          offset: String(targetPage * PAGE_SIZE),
          include_disabled: "true",
        });
        if (query) sp.set("q", query);
        // 文档与方案每页都取：深链块可能落在第 N 页，不能只在第 0 页
        // 拉文档，否则 doc 永远为 null，页面会误判成"加载失败"。
        const [d, p, r] = await Promise.all([
          api<Document>(`/api/documents/${encodeURIComponent(docId)}?include_chunks=false`),
          api<DocPlan>(`/api/documents/${encodeURIComponent(docId)}/plan`).catch(() => null),
          api<ChunkList>(`/api/chunks?${sp.toString()}`),
        ]);
        if (seq !== reqSeq.current) return;
        setDoc(d);
        if (p) setPlan(p);
        setChunks(r.items);
        setTotal(r.total ?? 0);
        setPhase("ready");
        setError("");

        const wantId = !chunkConsumed.current ? route.query.get("chunk") : null;
        const want = wantId ? r.items.find((c) => c.chunk_id === wantId) ?? null : null;
        if (want) chunkConsumed.current = true;
        setSelected((prev) => {
          if (want) return want;
          if (prev && r.items.some((c) => c.chunk_id === prev.chunk_id)) return prev;
          return r.items[0] ?? null;
        });
        const maxPage = Math.max(0, Math.ceil((r.total ?? 0) / PAGE_SIZE) - 1);
        if (targetPage > maxPage) {
          pageRef.current = maxPage;
          setPage(maxPage);
          void load(maxPage, query);
        }
      } catch (e) {
        if (seq !== reqSeq.current) return;
        setPhase((ph) => (ph === "ready" ? "ready" : "failed"));
        setError(e instanceof Error ? e.message : String(e));
      }
    },
    [docId, route.query],
  );

  /** 深链块可能不在第 1 页：用单块接口拿 ordinal 再换算页码。 */
  const locateChunkPage = useCallback(async (): Promise<number | null> => {
    const target = route.query.get("chunk");
    if (!target || chunkConsumed.current) return null;
    try {
      const c = await api<Chunk>(`/api/chunks/${encodeURIComponent(target)}`);
      if (c.doc_id !== docId || c.ordinal == null) return null;
      return Math.floor(c.ordinal / PAGE_SIZE);
    } catch {
      return null;
    }
  }, [docId, route.query]);

  useEffect(() => {
    setCrumbs([
      { label: "知识库", href: "#/" },
      { label: kbName(kbId), href: href("kb", kbId, "docs") },
      { label: doc?.title ?? "文档" },
    ]);
  }, [kbId, doc?.title, kbsLoaded]);

  useEffect(() => {
    chunkConsumed.current = false;
    pageRef.current = 0;
    qRef.current = "";
    setPhase("initial");
    setSelected(null);
    picks.clear();
    setTextBody("");
    setTextError("");
    void (async () => {
      const targetPage = await locateChunkPage();
      pageRef.current = targetPage ?? 0;
      setPage(targetPage ?? 0);
      await load(targetPage ?? 0, "");
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [docId]);

  // 文本原文按需抓取：折叠时不发请求；签名过期时重新领一次 file_url
  useEffect(() => {
    if (!showSrc || kind !== "text" || !fileUrl || textBody) return;
    let alive = true;
    async function run(): Promise<void> {
      try {
        const r = await fetch(fileUrl);
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const text = await r.text();
        if (alive) setTextBody(text);
      } catch (e) {
        if (!alive) return;
        if (/^HTTP (401|403)$/.test(e instanceof Error ? e.message : "")) {
          try {
            const fresh = await api<Document>(
              `/api/documents/${encodeURIComponent(docId)}?include_chunks=false`);
            if (fresh.file_url) {
              setDoc(fresh);
              const r2 = await fetch(fresh.file_url);
              if (!r2.ok) throw new Error(`HTTP ${r2.status}`);
              const text = await r2.text();
              if (alive) setTextBody(text);
              return;
            }
          } catch (inner) {
            if (alive) setTextError(inner instanceof Error ? inner.message : String(inner));
            return;
          }
        }
        if (alive) setTextError(e instanceof Error ? e.message : String(e));
      }
    }
    void run();
    return () => { alive = false; };
  }, [showSrc, kind, fileUrl, textBody, docId]);

  useEffect(() => {
    const t = setTimeout(() => {
      const next = filter.trim();
      if (next === qRef.current) return;
      qRef.current = next;
      pageRef.current = 0;
      setPage(0);
      picks.clear();
      void load(0, next);
    }, 300);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filter]);

  // V 切换原文（页面级：切换会重建 DOM，挂在行上的实现只能生效一次）
  useEffect(() => {
    const onKey = (e: KeyboardEvent): void => {
      if (e.key !== "v" && e.key !== "V") return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const t = e.target as HTMLElement | null;
      if (t && (/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || t.isContentEditable)) return;
      if (t && t.closest('[role="dialog"]')) return;
      e.preventDefault();
      toggleSrc();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showSrc, selected]);

  // PDF 内接矩形：iframe 永远完整落在容器里（单层滚动）
  useEffect(() => {
    if (!showSrc || kind !== "pdf") return;
    const el = sourceBox.current;
    if (!el) return;
    const measure = (): void => {
      const cs = getComputedStyle(el);
      const availW = el.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
      const availH = el.clientHeight - parseFloat(cs.paddingTop) - parseFloat(cs.paddingBottom);
      if (availW <= 8 || availH <= 8) return;
      const h = Math.min(availH, availW / A4_RATIO);
      setFrameSize({ w: Math.round(h * A4_RATIO), h: Math.round(h) });
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [showSrc, kind]);

  // 选中块变化：文本原文滚到高亮处
  useEffect(() => {
    if (!showSrc || kind !== "text") return;
    hitRef.current?.scrollIntoView({ block: "center" });
  }, [selected?.chunk_id, showSrc, kind, textBody]);

  function toggleSrc(): void {
    const next = !showSrc;
    setShowSrc(next);
    go(
      href("kb", kbId, "doc", docId)
      + queryString({
        src: next ? "1" : undefined,
        chunk: route.query.get("chunk") ?? undefined,
      }),
    );
  }

  async function watchJob(jobId: string, label: string): Promise<void> {
    jobAbort.current?.abort();
    const ac = new AbortController();
    jobAbort.current = ac;
    setJobNote(`${label}：排队中`);
    const job = await waitForJob(jobId, (j) => setJobNote(`${label}：${stageLabel(j.stage)}`), ac.signal);
    if (ac.signal.aborted) return;
    setJobNote("");
    if (!job) {
      toast(`${label}仍在后台执行，稍后可手动刷新查看`, "err");
      return;
    }
    toast(
      job.stage === "failed"
        ? `${label}失败：${job.error || "未知原因"}`
        : job.stage === "cancelled" ? `${label}已取消` : `${label}完成`,
      job.stage === "failed" ? "err" : "ok",
    );
    await load();
  }

  useEffect(() => () => jobAbort.current?.abort(), []);

  const planStale = !!plan?.snapshot
    && (plan.snapshot.chunk_size !== plan.effective.chunk_size
      || Number(plan.snapshot.overlap_ratio) !== Number(plan.effective.overlap_ratio)
      || (plan.snapshot.strategy ?? "auto") !== (plan.effective.strategy ?? "auto"));

  const pageKeys = chunks.map((c) => c.chunk_id);
  const pickedChunks = chunks.filter((c) => picks.has(c.chunk_id));

  function moveSelection(delta: number): void {
    const idx = selected ? chunks.findIndex((c) => c.chunk_id === selected.chunk_id) : -1;
    const next = Math.min(chunks.length - 1, Math.max(0, idx + delta));
    const c = chunks[next];
    if (!c) return;
    setSelected(c);
    rowRefs.current[c.chunk_id]?.focus();
  }

  if (phase === "initial") {
    return (
      <Box sx={{ p: { xs: 2, md: 3 } }}>
        <Skeleton variant="rounded" height={90} sx={{ mb: 2 }} />
        <Skeleton variant="rounded" height={420} />
      </Box>
    );
  }

  if (phase === "failed" || !doc) {
    return (
      <Box sx={{ p: { xs: 2, md: 3 } }}>
        <Alert
          severity="error"
          action={<Button color="inherit" size="small" onClick={() => void load()}>重试</Button>}
        >
          加载文档失败：{error}
        </Alert>
      </Box>
    );
  }

  const sourceText = textBody || doc.text || "";
  const canHighlight = selected?.offset_valid && selected.char_start != null
    && selected.char_end != null && selected.char_end <= sourceText.length
    && selected.char_end > selected.char_start;

  return (
    <Box sx={{ p: { xs: 2, md: 3 }, maxWidth: CONTENT_MAX, mx: "auto" }}>
      <Stack direction="row" spacing={1.5} alignItems="center" sx={{ mb: 2, flexWrap: "wrap", gap: 1 }}>
        <DescriptionIcon color="primary" />
        <Typography variant="h6" sx={{ fontWeight: 500, minWidth: 0 }} noWrap title={doc.title}>
          {doc.title}
        </Typography>
        {plan && (
          <Chip
            size="small"
            variant="outlined"
            color={plan.own.custom ? "primary" : "default"}
            label={`${plan.effective.chunk_size} 字符 / ${plan.effective.overlap_ratio}% · ${strategyLabel(plan.effective.strategy)}`}
            onClick={() => void editDocPlanDialog(docId).then((ok) => { if (ok) void load(); })}
            title="点击编辑本文档的分块方案"
          />
        )}
        {planStale && (
          <Chip size="small" variant="outlined" color="warning" label="需重切"
            title="入库时的方案与当前方案不同；点「重新切分」后生效" />
        )}
        <Chip size="small" variant="outlined" label={`${total} 分块`} />
        {jobNote && (
          <Chip size="small" color="info" variant="outlined"
            icon={<CircularProgress size={12} />} label={jobNote} />
        )}
        <Box sx={{ flex: 1 }} />
        <Button
          variant="outlined" size="small" startIcon={<RefreshIcon />}
          disabled={!!jobNote}
          onClick={() => {
            void resplitDoc(docId, (r) => { if (r.job_id) void watchJob(r.job_id, "重新切分"); });
          }}
        >
          重新切分
        </Button>
        <Tooltip title={showSrc ? "收起原文（V）" : "展开原文对照（V）"}>
          <Button variant={showSrc ? "contained" : "outlined"} size="small" onClick={toggleSrc}>
            {showSrc ? "收起原文" : selected ? `对照原文（第 ${selected.ordinal ?? 0} 块）` : "显示原文"}
          </Button>
        </Tooltip>
        <IconButton size="small" aria-label="文档维护操作" onClick={(e) => setMenuEl(e.currentTarget)}>
          <MoreVertIcon />
        </IconButton>
        <Menu anchorEl={menuEl} open={!!menuEl} onClose={() => setMenuEl(null)}>
          <MenuItem onClick={() => {
            setMenuEl(null);
            void renameDocDialog(docId, doc.title).then((ok) => { if (ok) void load(); });
          }}>重命名</MenuItem>
          <MenuItem onClick={() => {
            setMenuEl(null);
            void moveDocDialog(docId, kbId).then((target) => {
              if (target) go(href("kb", target, "doc", docId));
              else if (target === "") go(href("kb", kbId, "docs"));
            });
          }}>移动到其他知识库</MenuItem>
          <MenuItem onClick={() => {
            setMenuEl(null);
            void editDocPlanDialog(docId).then((ok) => { if (ok) void load(); });
          }}>分块方案</MenuItem>
          <MenuItem onClick={() => {
            setMenuEl(null);
            void reparseDoc(docId, (r) => { if (r.job_id) void watchJob(r.job_id, "重新解析"); });
          }}>重读原文（换引擎）</MenuItem>
          <MenuItem onClick={() => {
            setMenuEl(null);
            void deleteDocDialog(doc).then((ok) => {
              if (ok) {
                invalidateKbs();
                void loadKbs();
                go(href("kb", kbId, "docs"));
              }
            });
          }}>
            <Typography color="error">删除文档</Typography>
          </MenuItem>
        </Menu>
      </Stack>

      <Stack direction="row" spacing={1.5} alignItems="center" sx={{ mb: 1.5, flexWrap: "wrap" }}>
        <TextField
          size="small" type="search" placeholder="过滤分块内容…"
          value={filter} onChange={(e) => setFilter(e.target.value)}
          sx={{ width: 280 }}
          slotProps={{ htmlInput: { "aria-label": "按内容过滤分块" } }}
        />
        <Button size="small" variant="outlined" startIcon={<AddIcon />}
          onClick={() => void addChunkDialog({ kbId, docId }).then((ok) => { if (ok) void load(); })}>
          新增分块
        </Button>
        {pickedChunks.length > 0 && (
          <>
            <Divider orientation="vertical" flexItem />
            <Typography variant="body2" color="text.secondary">已选 {pickedChunks.length} 块</Typography>
            <Button size="small" onClick={() => void batchSetEnabled(pickedChunks, true).then((ok) => {
              if (ok) { picks.clear(); void load(); }
            })}>
              批量启用
            </Button>
            <Button size="small" onClick={() => void batchSetEnabled(pickedChunks, false).then((ok) => {
              if (ok) { picks.clear(); void load(); }
            })}>
              批量停用
            </Button>
          </>
        )}
      </Stack>

      {phase === "ready" && error && (
        <Alert severity="error" sx={{ mb: 2 }}
          action={<Button color="inherit" size="small" onClick={() => void load()}>重试</Button>}>
          刷新失败，下面是上一次取到的结果：{error}
        </Alert>
      )}

      <Box
        sx={{
          display: "grid", gap: 2,
          gridTemplateColumns: showSrc ? { xs: "1fr", lg: "minmax(0,1.15fr) minmax(0,1fr)" } : "minmax(0,1fr)",
          alignItems: "start",
        }}
      >
        <GlassPanel radius={12} look={look} aria-label="分块列表">
          {chunks.length === 0 ? (
            <Alert
              severity={qRef.current ? "warning" : "info"}
              sx={{ m: 2 }}
              action={qRef.current ? (
                <Button color="inherit" size="small" onClick={() => setFilter("")}>清空过滤</Button>
              ) : undefined}
            >
              {qRef.current
                ? `没有包含「${qRef.current}」的分块。`
                : "这篇文档还没有分块。点「新增分块」手工补一块，或重新解析原文。"}
            </Alert>
          ) : (
            <>
              <TableContainer ref={sourceScroll}>
                <Table size="small" aria-label="分块列表">
                  <TableHead>
                    <TableRow>
                      <TableCell padding="checkbox">
                        <Checkbox
                          size="small"
                          checked={picks.all(pageKeys)}
                          indeterminate={picks.some(pageKeys)}
                          onChange={(e) => picks.setAll(pageKeys, e.target.checked)}
                          inputProps={{ "aria-label": "全选当前页" }}
                        />
                      </TableCell>
                      <TableCell align="right" sx={{ width: 56 }}>#</TableCell>
                      <TableCell>内容</TableCell>
                      <TableCell align="right" sx={{ width: 64 }}>页</TableCell>
                      <TableCell sx={{ width: 160 }}>状态</TableCell>
                      <TableCell align="right" sx={{ width: 130 }}>
                        <span className="sr-only">操作</span>
                      </TableCell>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {chunks.map((c, i) => (
                      <TableRow
                        key={c.chunk_id}
                        hover
                        selected={selected?.chunk_id === c.chunk_id}
                        tabIndex={0}
                        ref={(el) => { rowRefs.current[c.chunk_id] = el; }}
                        onClick={() => setSelected(c)}
                        onKeyDown={(e) => {
                          if (e.target !== e.currentTarget) return;
                          if (e.key === "ArrowDown") {
                            e.preventDefault();
                            moveSelection(1);
                          } else if (e.key === "ArrowUp") {
                            e.preventDefault();
                            moveSelection(-1);
                          } else if (e.key === "Enter" || e.key === " ") {
                            e.preventDefault();
                            setSelected(chunks[i] ?? c);
                          }
                        }}
                        sx={{ cursor: "pointer", "&:focus-visible": { outline: 2, outlineColor: "primary.main", outlineOffset: -2 } }}
                      >
                        <TableCell padding="checkbox">
                          <Checkbox
                            size="small"
                            checked={picks.has(c.chunk_id)}
                            onClick={(e) => e.stopPropagation()}
                            onChange={(e) => picks.toggle(c.chunk_id, e.target.checked)}
                            inputProps={{ "aria-label": `选择第 ${c.ordinal ?? 0} 块` }}
                          />
                        </TableCell>
                        <TableCell align="right">{c.ordinal ?? 0}</TableCell>
                        <TableCell sx={{ maxWidth: 640 }}>
                          <Typography
                            variant="body2" title={c.text}
                            sx={{
                              overflow: "hidden", display: "-webkit-box",
                              WebkitLineClamp: 2, WebkitBoxOrient: "vertical",
                              whiteSpace: "pre-wrap", lineHeight: 1.5,
                              color: c.enabled === false ? "text.disabled" : "text.primary",
                            }}
                          >
                            {c.text}
                          </Typography>
                          <Typography variant="caption" color="text.secondary" noWrap sx={{ display: "block" }}>
                            {c.heading_path || "无标题层级"} · {c.token_count ?? 0} tok
                            {c.offset_valid && c.char_start != null
                              ? ` · ${c.char_start}–${c.char_end}` : ""}
                          </Typography>
                        </TableCell>
                        <TableCell align="right">{c.page ?? "—"}</TableCell>
                        <TableCell>
                          <Stack direction="row" spacing={0.5}>
                            {c.origin === "manual" && <Chip size="small" variant="outlined" label="手工" />}
                            {c.edited && <Chip size="small" variant="outlined" color="warning" label="已编辑" />}
                            {c.enabled === false && <Chip size="small" variant="outlined" color="error" label="已停用" />}
                          </Stack>
                        </TableCell>
                        <TableCell align="right">
                          <Stack direction="row" spacing={0.5} justifyContent="flex-end"
                            onClick={(e) => e.stopPropagation()}>
                            <Tooltip title="编辑内容">
                              <IconButton size="small" aria-label={`编辑第 ${c.ordinal ?? 0} 块`}
                                onClick={() => void editChunkDialog(c).then((ok) => { if (ok) void load(); })}>
                                <EditIcon fontSize="small" />
                              </IconButton>
                            </Tooltip>
                            <Tooltip title={c.enabled === false ? "启用" : "停用（退出检索）"}>
                              <IconButton size="small"
                                aria-label={`切换第 ${c.ordinal ?? 0} 块启停`}
                                onClick={() => void setChunkEnabled(c, c.enabled === false).then((ok) => {
                                  if (ok) void load();
                                })}>
                                {c.enabled === false
                                  ? <VisibilityIcon fontSize="small" />
                                  : <VisibilityOffIcon fontSize="small" />}
                              </IconButton>
                            </Tooltip>
                            <Tooltip title="删除">
                              <IconButton size="small" color="error" aria-label={`删除第 ${c.ordinal ?? 0} 块`}
                                onClick={() => void deleteChunk(c).then((ok) => { if (ok) void load(); })}>
                                <DeleteIcon fontSize="small" />
                              </IconButton>
                            </Tooltip>
                          </Stack>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </TableContainer>
              <Divider />
              <TablePagination
                component="div"
                count={total}
                page={page}
                rowsPerPage={PAGE_SIZE}
                rowsPerPageOptions={[PAGE_SIZE]}
                labelRowsPerPage="每页"
                labelDisplayedRows={({ from, to, count }) => `${from}–${to} / 共 ${count} 块`}
                onPageChange={(_e, p) => {
                  pageRef.current = p;
                  setPage(p);
                  picks.clear();
                  void load(p, qRef.current);
                }}
              />
            </>
          )}
        </GlassPanel>

        {showSrc && (
          <GlassPanel
            radius={12} look={look}
            aria-label="原文"
            style={{
              position: "sticky", top: 0, maxHeight: "calc(100vh - 220px)",
              display: "flex", flexDirection: "column",
            }}
          >
            <Stack direction="row" spacing={1} alignItems="center" sx={{ px: 2, py: 1, borderBottom: 1, borderColor: "divider" }}>
              <Typography variant="body2" sx={{ flex: 1 }}>原文</Typography>
              {fileUrl && (
                <>
                  <Tooltip title="新窗口打开">
                    <IconButton size="small" aria-label="新窗口打开原文"
                      onClick={() => window.open(fileUrl, "_blank", "noopener")}>
                      <OpenInNewIcon fontSize="small" />
                    </IconButton>
                  </Tooltip>
                  <Tooltip title="下载原文">
                    <IconButton size="small" aria-label="下载原文" component="a"
                      href={downloadUrl(fileUrl)} download>
                      <DownloadIcon fontSize="small" />
                    </IconButton>
                  </Tooltip>
                </>
              )}
            </Stack>
            <Box ref={sourceBox} sx={{ flex: 1, minHeight: 0, overflow: "auto", p: 1.5 }}>
              {kind === "pdf" && fileUrl && (
                <iframe
                  title={`${doc.title} 原文`}
                  src={pageUrl(fileUrl, pageNum)}
                  style={{
                    width: frameSize ? `${frameSize.w}px` : "100%",
                    height: frameSize ? `${frameSize.h}px` : "66vh",
                    border: 0, borderRadius: 4, background: "#333",
                    display: "block", margin: "0 auto",
                  }}
                />
              )}
              {kind === "image" && fileUrl && (
                <Box component="img" src={fileUrl} alt={`${doc.title} 原文`}
                  sx={{ maxWidth: "100%", borderRadius: 1 }} />
              )}
              {kind === "text" && (
                textError ? (
                  <Alert severity="warning">原文抓取失败：{textError}（下面是解析全文）</Alert>
                ) : !sourceText ? (
                  <CircularProgress size={18} />
                ) : null
              )}
              {(kind === "text" || (kind === "download" && !fileUrl)) && sourceText && (
                <Typography
                  variant="body2" component="pre"
                  sx={{
                    whiteSpace: "pre-wrap", wordBreak: "break-word",
                    fontFamily: "inherit", lineHeight: 1.8, m: 0,
                  }}
                >
                  {canHighlight ? (
                    <>
                      {sourceText.slice(0, selected!.char_start)}
                      <mark ref={hitRef}>{sourceText.slice(selected!.char_start, selected!.char_end)}</mark>
                      {sourceText.slice(selected!.char_end)}
                    </>
                  ) : (
                    sourceText
                  )}
                </Typography>
              )}
              {kind === "download" && fileUrl && (
                <Alert severity="info">
                  这个格式无法内嵌预览（{doc.mime || "未知类型"}）。可下载后用本地应用打开。
                </Alert>
              )}
            </Box>
          </GlassPanel>
        )}
      </Box>
    </Box>
  );
}
