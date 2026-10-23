import { useCallback, useEffect, useRef, useState } from "react";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Checkbox from "@mui/material/Checkbox";
import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import Divider from "@mui/material/Divider";
import IconButton from "@mui/material/IconButton";
import Link from "@mui/material/Link";
import Menu from "@mui/material/Menu";
import MenuItem from "@mui/material/MenuItem";
import { GlassPanel, useGlassLook } from "../ui/glass";
import Skeleton from "@mui/material/Skeleton";
import Stack from "@mui/material/Stack";
import Tab from "@mui/material/Tab";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TablePagination from "@mui/material/TablePagination";
import TableRow from "@mui/material/TableRow";
import Tabs from "@mui/material/Tabs";
import TextField from "@mui/material/TextField";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import AddIcon from "@mui/icons-material/Add";
import DeleteIcon from "@mui/icons-material/Delete";
import DriveFolderUploadIcon from "@mui/icons-material/DriveFolderUpload";
import EditIcon from "@mui/icons-material/Edit";
import InsertDriveFileIcon from "@mui/icons-material/InsertDriveFile";
import MoreVertIcon from "@mui/icons-material/MoreVert";
import RefreshIcon from "@mui/icons-material/Refresh";
import StopCircleIcon from "@mui/icons-material/StopCircle";

import { api, apiUpload, ApiError } from "../lib/api";
import { CONTENT_MAX } from "../lib/layout";
import { confirmDialog } from "../lib/dialog";
import { fmtTime } from "../lib/format";
import { cancelJob } from "../lib/jobcancel";
import { stageLabel, waitForJob } from "../lib/jobs";
import type { Job as WaitJob } from "../lib/jobs";
import {
  deleteDocsBatch, deleteDocDialog, editDocPlanDialog, renameDocDialog,
  reparseDoc, resplitDoc,
} from "../lib/ops";
import { href } from "../lib/router";
import { usePicks } from "../lib/selection";
import { invalidateKbs, kbName, loadKbs, pokeQueue, setCrumbs, track, useStoreSelect } from "../lib/store";
import type { DocPlan, Document, DocumentList, IngestResult, Kb } from "../lib/types";
import { toast } from "../ui/toast";

const PAGE_SIZE = 20;

interface ActiveJob {
  stage: string;
  job_id: string;
}

interface PlanState {
  stale: boolean;
  custom: boolean;
  text: string;
  title: string;
}

/**
 * 知识库文档列表：服务端过滤 + 分页 + 批量删除 + 行内维护。
 *
 * 几个不能丢的约束（都是旧版实测换来的）：
 * - 过滤与分页都交给服务端（`q` 在分页前过滤，`total` 是命中数）。
 * - 加载分三态：有数据时的轮询只表示 refreshing，不把表格换成骨架屏，
 *   否则正在敲的过滤词会丢焦点。
 * - `wait=false` 提交的重解析 / 重切行内可见阶段，并提供停止入口。
 * - 方案列区分「需重切」：入库快照与当前方案不同时，只有重新切分才生效。
 */
export default function KbDocs({ kbId }: { kbId: string }): React.ReactElement {
  const look = useGlassLook("card");
  const kbsLoaded = useStoreSelect((s) => s.kbsLoaded);
  const [kb, setKb] = useState<Kb | null>(null);
  const [items, setItems] = useState<Document[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(0);
  const [filter, setFilter] = useState("");
  const [phase, setPhase] = useState<"initial" | "ready" | "failed">("initial");
  const [error, setError] = useState("");
  const [refreshing, setRefreshing] = useState(false);
  const [composeOpen, setComposeOpen] = useState(false);
  const [plans, setPlans] = useState<Record<string, DocPlan>>({});
  const [jobs, setJobs] = useState<Record<string, ActiveJob>>({});
  const [orphanJobs, setOrphanJobs] = useState<ActiveJob[]>([]);
  const [menuDoc, setMenuDoc] = useState<Document | null>(null);
  const [menuEl, setMenuEl] = useState<HTMLElement | null>(null);

  const picks = usePicks();
  const reqSeq = useRef(0);
  const pageRef = useRef(0);
  const qRef = useRef("");
  const pollTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const hadActiveJobs = useRef(false);

  const load = useCallback(
    async (targetPage = pageRef.current, query = qRef.current): Promise<void> => {
      const seq = ++reqSeq.current;
      setRefreshing(true);
      try {
        const sp = new URLSearchParams({
          kb_id: kbId,
          limit: String(PAGE_SIZE),
          offset: String(targetPage * PAGE_SIZE),
        });
        if (query) sp.set("q", query);
        const [k, r] = await Promise.all([
          track(api<Kb>(`/api/kbs/${encodeURIComponent(kbId)}`)),
          track(api<DocumentList>(`/api/documents?${sp.toString()}`)),
        ]);
        if (seq !== reqSeq.current) return;
        setKb(k);
        setItems(r.items);
        setTotal(r.total ?? 0);
        setPhase("ready");
        setError("");
        // 过滤态下不重取方案：/documents/plans 不接受 q，按 q 过滤后的行
        // 未必落在它的窗口里；方案徽标保留已取到的那份即可
        if (!query) void loadPlans();
        const maxPage = Math.max(0, Math.ceil((r.total ?? 0) / PAGE_SIZE) - 1);
        if (targetPage > maxPage) {
          pageRef.current = maxPage;
          setPage(maxPage);
          void load(maxPage, query);
        }
      } catch (e) {
        if (seq !== reqSeq.current) return;
        setPhase((p) => (p === "ready" ? "ready" : "failed"));
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        if (seq === reqSeq.current) setRefreshing(false);
      }
    },
    [kbId],
  );

  /** 方案摘要一次取一窗（服务端上限 500）；超出部分是已知覆盖边界，标 "—"。 */
  const loadPlans = useCallback(async (): Promise<void> => {
    try {
      const r = await api<{ items: Record<string, DocPlan> }>(
        `/api/documents/plans?kb_id=${encodeURIComponent(kbId)}&limit=500`);
      setPlans(r.items ?? {});
    } catch {
      // 辅助信息：取不到就整列降级，不把列表页打成错误态
      setPlans({});
    }
  }, [kbId]);

  /**
   * 活跃任务：驱动行内「处理中」与列表轮询。
   *
   * 未产出 doc_id 的入库任务单独成列：新文档要等流水线跑到切分前才回写
   * doc_id，最初几百毫秒里按文档挂在列表上会找不到。
   */
  const refreshJobs = useCallback(async (): Promise<void> => {
    clearTimeout(pollTimer.current);
    try {
      const r = await api<{ items: Array<{ doc_id: string; stage: string; job_id: string }> }>(
        "/api/jobs?active_only=true&limit=50");
      const m: Record<string, ActiveJob> = {};
      const loose: ActiveJob[] = [];
      for (const j of r.items) {
        const item = { stage: j.stage, job_id: j.job_id };
        if (j.doc_id) m[j.doc_id] = item;
        else loose.push(item);
      }
      setJobs(m);
      setOrphanJobs(loose);
      if (r.items.length > 0) {
        hadActiveJobs.current = true;
        pollTimer.current = setTimeout(() => {
          void load();
          void refreshJobs();
        }, 2500);
      } else if (hadActiveJobs.current) {
        // 最后一个活跃任务跑到终态：再取一次列表，否则页面上停着
        // 任务结束前那一帧的状态，只能手动刷新才看得到结果
        hadActiveJobs.current = false;
        void load();
        void loadPlans();
      }
    } catch {
      setJobs({});
      setOrphanJobs([]);
    }
  }, [load]);

  useEffect(() => {
    setCrumbs([{ label: "知识库", href: "#/" }, { label: kbName(kbId) }]);
  }, [kbId, kbsLoaded]);

  useEffect(() => {
    clearTimeout(pollTimer.current);
    pageRef.current = 0;
    qRef.current = "";
    setPage(0);
    setFilter("");
    setItems([]);
    setTotal(0);
    setPlans({});
    picks.clear();
    setPhase("initial");
    void load(0, "");
    void refreshJobs();
    return () => clearTimeout(pollTimer.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kbId]);

  // 过滤 debounce：每个字符打一次接口是拿数据库读放大换心理安慰
  useEffect(() => {
    const t = setTimeout(() => {
      const q = filter.trim();
      if (q === qRef.current) return;
      qRef.current = q;
      pageRef.current = 0;
      setPage(0);
      picks.clear();
      void load(0, q);
    }, 300);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filter]);

  function turnPage(p: number): void {
    pageRef.current = p;
    setPage(p);
    picks.clear();
    void load(p, qRef.current);
  }

  async function cancelDocJob(docId: string): Promise<void> {
    const j = jobs[docId];
    if (!j) return;
    await cancelJob(j.job_id, () => {
      void refreshJobs();
      void load();
    });
  }

  function planState(docId: string): PlanState {
    const p = plans[docId];
    if (!p) return { stale: false, custom: false, text: "—", title: "" };
    const s = p.snapshot;
    const e = p.effective;
    const stale = !!s && (s.chunk_size !== e.chunk_size
      || Number(s.overlap_ratio) !== Number(e.overlap_ratio)
      || (s.strategy ?? "auto") !== (e.strategy ?? "auto"));
    return {
      stale,
      custom: p.own.custom,
      text: stale ? "需重切" : `${e.chunk_size} · ${e.overlap_ratio}%`,
      title: stale
        ? `入库时用的是 ${s!.chunk_size} 字符 · ${s!.overlap_ratio}%，当前方案是 `
          + `${e.chunk_size} 字符 · ${e.overlap_ratio}%，点「重新切分」应用`
        : p.own.custom
          ? "本文档单独覆盖了分块方案"
          : `来自${e.source === "kb" ? "知识库默认" : "系统默认"}`,
    };
  }

  async function refreshAll(): Promise<void> {
    invalidateKbs();
    void loadKbs();
    await load();
    void refreshJobs();
  }

  async function removePicked(): Promise<void> {
    const docs = items.filter((d) => picks.has(d.doc_id));
    if (docs.length === 0) return;
    const chunks = docs.reduce((n, d) => n + (d.chunk_count ?? 0), 0);
    const ok = await confirmDialog({
      title: `删除 ${docs.length} 篇文档`,
      body: docs.length <= 5
        ? `将删除「${docs.map((d) => d.title).join("」「")}」及其 ${chunks} 个分块与留档原文。此操作不可撤销。`
        : `将删除这 ${docs.length} 篇文档及其 ${chunks} 个分块与留档原文。此操作不可撤销。`,
      confirmLabel: "删除",
      destructive: true,
      requireTyping: docs.length > 5 ? String(docs.length) : undefined,
    });
    if (!ok) return;
    if (await deleteDocsBatch(docs)) {
      picks.clear();
      invalidateKbs();
      void loadKbs();
      await load();
    }
  }

  async function openDocMenu(e: React.MouseEvent<HTMLElement>, d: Document): Promise<void> {
    setMenuEl(e.currentTarget);
    setMenuDoc(d);
  }

  const pickedCount = picks.count(items.map((d) => d.doc_id));
  const allPicked = picks.all(items.map((d) => d.doc_id));
  const somePicked = picks.some(items.map((d) => d.doc_id));

  return (
    <Box sx={{ p: { xs: 2, md: 3 }, maxWidth: CONTENT_MAX, mx: "auto" }}>
      <Stack
        direction={{ xs: "column", sm: "row" }}
        alignItems="flex-start"
        spacing={2}
        sx={{ mb: 2 }}
      >
        <Box sx={{ flex: 1, minWidth: 0, width: "100%" }}>
          <Typography variant="h5" sx={{ fontWeight: 500 }}>
            {kb?.name ?? kbName(kbId)}
          </Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
            {kb
              ? `${kb.doc_count} 文档 · ${kb.chunk_count} 分块 · 更新于 ${fmtTime(kb.updated_at)}`
              : "正在读取知识库…"}
          </Typography>
        </Box>
        <Stack
          direction="row"
          spacing={1}
          sx={{ flexShrink: 0, alignSelf: { xs: "flex-end", sm: "flex-start" } }}
        >
          <Button
            variant="contained"
            startIcon={<DriveFolderUploadIcon />}
            aria-expanded={composeOpen}
            onClick={() => setComposeOpen((v) => !v)}
          >
            入库
          </Button>
          <Button variant="outlined" startIcon={<RefreshIcon />} onClick={() => void refreshAll()}>
            刷新
          </Button>
        </Stack>
      </Stack>

      {(kb?.doc_count ?? 0) > 0 || filter ? (
        <Stack direction="row" spacing={2} alignItems="center" useFlexGap flexWrap="wrap" sx={{ mb: 2 }}>
          <TextField
            size="small"
            type="search"
            placeholder="过滤标题…"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            sx={{ width: { xs: "100%", sm: 320 } }}
            slotProps={{ htmlInput: { "aria-label": "按标题过滤文档" } }}
          />
          {refreshing && <CircularProgress size={14} aria-label="正在刷新" />}
          {qRef.current && (
            <Typography variant="body2" color="text.secondary">
              匹配 {total} 篇
            </Typography>
          )}
        </Stack>
      ) : null}

      {orphanJobs.length > 0 && (
        <Alert
          severity="info"
          sx={{ mb: 2 }}
          action={
            <Stack direction="row" spacing={0.5}>
              {orphanJobs.map((j, i) => (
                <Button
                  key={j.job_id}
                  color="inherit"
                  size="small"
                  onClick={() => void cancelJob(j.job_id, () => void refreshJobs())}
                >
                  停止 {i + 1}
                </Button>
              ))}
            </Stack>
          }
        >
          {orphanJobs.length} 个文件正在入库（{stageLabel(orphanJobs[0]!.stage)}…）
        </Alert>
      )}

      {phase === "failed" && (
        <Alert
          severity="error"
          sx={{ mb: 2 }}
          action={<Button color="inherit" size="small" onClick={() => void load()}>重试</Button>}
        >
          加载文档失败：{error}
        </Alert>
      )}

      {phase === "initial" && <Skeleton variant="rounded" height={360} />}

      {phase === "ready" && error && (
        <Alert
          severity="error"
          sx={{ mb: 2 }}
          action={<Button color="inherit" size="small" onClick={() => void load()}>重试</Button>}
        >
          刷新失败，下面是上一次取到的结果：{error}
        </Alert>
      )}

      {phase === "ready" && items.length === 0 && (
        <Alert
          severity={qRef.current ? "warning" : "info"}
          action={qRef.current ? (
            <Button color="inherit" size="small" onClick={() => setFilter("")}>清空过滤</Button>
          ) : undefined}
        >
          {qRef.current
            ? `没有标题包含「${qRef.current}」的文档。换个关键词，或清空过滤条件。`
            : "这个库还没有文档。点右上角「入库」上传文件、粘贴文本或抓取网页。"}
        </Alert>
      )}

      {phase === "ready" && items.length > 0 && (
        <>
          {pickedCount > 0 && (
            <Stack
              direction="row"
              spacing={1}
              alignItems="center"
              role="region"
              aria-label="批量操作"
              sx={{
                mb: 1, px: 2, py: 1,
                border: 1, borderColor: "primary.main", borderRadius: 1,
                bgcolor: "action.hover",
              }}
            >
              <Typography variant="body2" sx={{ flex: 1 }}>已选 {pickedCount} 篇</Typography>
              <Button size="small" onClick={picks.clear}>取消选择</Button>
              <Button size="small" color="error" startIcon={<DeleteIcon />}
                onClick={() => void removePicked()}>
                删除所选
              </Button>
            </Stack>
          )}

          <GlassPanel radius={12} look={look} className="lg-clip">
            <TableContainer>
              <Table size="small" aria-label="文档列表" sx={{ tableLayout: "fixed" }}>
                <TableHead>
                  <TableRow>
                    <TableCell padding="checkbox">
                      <Checkbox
                        size="small"
                        checked={allPicked}
                        indeterminate={somePicked}
                        onChange={(e) => picks.setAll(items.map((d) => d.doc_id), e.target.checked)}
                        inputProps={{ "aria-label": "全选当前页" }}
                      />
                    </TableCell>
                    <TableCell>标题</TableCell>
                    <TableCell sx={{ display: { xs: "none", sm: "table-cell" }, width: 120 }}>
                      分块方案
                    </TableCell>
                    <TableCell sx={{ display: { xs: "none", lg: "table-cell" }, width: 120 }}>
                      引擎
                    </TableCell>
                    <TableCell
                      align="right"
                      sx={{ display: { xs: "none", sm: "table-cell" }, width: 64 }}
                    >
                      块数
                    </TableCell>
                    <TableCell sx={{ display: { xs: "none", sm: "table-cell" }, width: 96 }}>
                      状态
                    </TableCell>
                    <TableCell sx={{ display: { xs: "none", md: "table-cell" }, width: 140 }}>
                      入库时间
                    </TableCell>
                    <TableCell align="right" sx={{ width: { xs: 80, sm: 96 } }}>
                      <span className="sr-only">操作</span>
                    </TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {items.map((d) => {
                    const ps = planState(d.doc_id);
                    const job = jobs[d.doc_id];
                    const statusNode = job ? (
                      <Stack direction="row" spacing={0.5} alignItems="center">
                        <Chip size="small" color="warning" variant="outlined"
                          label={stageLabel(job.stage)} />
                        <Tooltip title="停止这个任务">
                          <IconButton
                            size="small"
                            aria-label={`停止 ${d.title} 的处理`}
                            onClick={() => void cancelDocJob(d.doc_id)}
                          >
                            <StopCircleIcon fontSize="small" />
                          </IconButton>
                        </Tooltip>
                      </Stack>
                    ) : (
                      <Chip
                        size="small"
                        variant="outlined"
                        color={d.status === "ready" ? "success" : d.status === "failed" ? "error" : "warning"}
                        label={d.status}
                      />
                    );
                    return (
                      <TableRow key={d.doc_id} hover selected={picks.has(d.doc_id)}>
                        <TableCell padding="checkbox">
                          <Checkbox
                            size="small"
                            checked={picks.has(d.doc_id)}
                            onChange={(e) => picks.toggle(d.doc_id, e.target.checked)}
                            inputProps={{ "aria-label": `选择 ${d.title}` }}
                          />
                        </TableCell>
                        <TableCell sx={{ maxWidth: 360 }}>
                          <Link
                            href={href("kb", kbId, "doc", d.doc_id)}
                            underline="hover"
                            title={d.title}
                            sx={{
                              display: "block", overflow: "hidden",
                              textOverflow: "ellipsis", whiteSpace: "nowrap",
                            }}
                          >
                            {d.title}
                          </Link>
                          <Box sx={{ mt: 0.5, display: { xs: "block", sm: "none" } }}>
                            {statusNode}
                          </Box>
                        </TableCell>
                        <TableCell sx={{ display: { xs: "none", sm: "table-cell" } }}>
                          <Chip
                            size="small"
                            variant="outlined"
                            color={ps.stale ? "warning" : ps.custom ? "primary" : "default"}
                            label={ps.text}
                            title={ps.title}
                          />
                        </TableCell>
                        <TableCell sx={{ display: { xs: "none", lg: "table-cell" } }}>
                          <Typography variant="body2" color="text.secondary">
                            {d.parser_engine || "—"}
                          </Typography>
                        </TableCell>
                        <TableCell
                          align="right"
                          sx={{ display: { xs: "none", sm: "table-cell" } }}
                        >
                          {d.chunk_count ?? 0}
                        </TableCell>
                        <TableCell sx={{ display: { xs: "none", sm: "table-cell" } }}>
                          {statusNode}
                        </TableCell>
                        <TableCell sx={{ display: { xs: "none", md: "table-cell" } }}>
                          <Typography variant="body2" color="text.secondary">
                            {d.created_at ? fmtTime(d.created_at) : "—"}
                          </Typography>
                        </TableCell>
                        <TableCell align="right">
                          <Stack direction="row" spacing={0.5} justifyContent="flex-end">
                            <Tooltip title="删除文档">
                              <IconButton
                                size="small" color="error" aria-label={`删除 ${d.title}`}
                                onClick={() => {
                                  void deleteDocDialog(d).then((ok) => {
                                    if (!ok) return;
                                    picks.toggle(d.doc_id, false);
                                    invalidateKbs();
                                    void loadKbs();
                                    void load();
                                  });
                                }}
                              >
                                <DeleteIcon fontSize="small" />
                              </IconButton>
                            </Tooltip>
                            <Tooltip title="更多操作">
                              <IconButton
                                size="small" aria-label={`${d.title} 的更多操作`}
                                onClick={(e) => void openDocMenu(e, d)}
                              >
                                <MoreVertIcon fontSize="small" />
                              </IconButton>
                            </Tooltip>
                          </Stack>
                        </TableCell>
                      </TableRow>
                    );
                  })}
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
              labelDisplayedRows={({ from, to, count }) => `${from}–${to} / 共 ${count} 篇`}
              onPageChange={(_e, p) => turnPage(p)}
            />
          </GlassPanel>
        </>
      )}

      <Menu anchorEl={menuEl} open={!!menuEl} onClose={() => setMenuEl(null)}>
        <MenuItem onClick={() => {
          const d = menuDoc;
          setMenuEl(null);
          if (!d) return;
          void editDocPlanDialog(d.doc_id).then((ok) => {
            if (ok) { void load(); void loadPlans(); }
          });
        }}>
          <EditIcon fontSize="small" sx={{ mr: 1 }} /> 覆盖分块方案
        </MenuItem>
        <MenuItem onClick={() => {
          const d = menuDoc;
          setMenuEl(null);
          if (!d) return;
          void renameDocDialog(d.doc_id, d.title).then((ok) => { if (ok) void load(); });
        }}>
          重命名
        </MenuItem>
        <MenuItem onClick={() => {
          const d = menuDoc;
          setMenuEl(null);
          if (!d) return;
          void reparseDoc(d.doc_id).then((ok) => {
            if (ok) { void load(); void refreshJobs(); }
          });
        }}>
          重读原文（换引擎）
        </MenuItem>
        <MenuItem onClick={() => {
          const d = menuDoc;
          setMenuEl(null);
          if (!d) return;
          void resplitDoc(d.doc_id).then((ok) => {
            if (ok) { void load(); void loadPlans(); void refreshJobs(); }
          });
        }}>
          重新切分
        </MenuItem>
      </Menu>

      <ComposeDialog
        key={kbId}
        open={composeOpen}
        kbId={kbId}
        onClose={() => setComposeOpen(false)}
        onDone={() => {
          pageRef.current = 0;
          setPage(0);
          void load(0, qRef.current);
          void refreshJobs();
          invalidateKbs();
          void loadKbs();
        }}
      />
    </Box>
  );
}

/**
 * 入库对话框：文件（可多选）/ 文本 / URL 三个页签。
 *
 * 文件走 `?wait=false` 异步流水线，并逐个盯到终态：
 * - 失败项留在队列里供重试（几十个文件时凭记忆重挑是不现实的）；
 * - 每个文件一个幂等键，网络抖动后重试不会产生第二份文档；
 * - 提交后 `pokeQueue()` 立刻刷新顶栏队列摘要。
 */
function ComposeDialog({
  open, kbId, onClose, onDone,
}: {
  open: boolean;
  kbId: string;
  onClose: () => void;
  onDone: () => void;
}): React.ReactElement {
  const look = useGlassLook("card");
  const [tab, setTab] = useState<"file" | "text" | "url">("file");
  const [busy, setBusy] = useState(false);
  const [stage, setStage] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [engine, setEngine] = useState("auto");
  const [text, setText] = useState("");
  const [title, setTitle] = useState("");
  const [url, setUrl] = useState("");
  const [currentJob, setCurrentJob] = useState("");
  const cancelAll = useRef(false);
  const abortRef = useRef<AbortController | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);

  // 对话框卸载时中止本地轮询：服务端任务继续，但不再往已卸载的组件写状态
  useEffect(() => () => abortRef.current?.abort(), []);

  useEffect(() => {
    if (!open) {
      cancelAll.current = false;
      setBusy(false);
      setStage("");
      setCurrentJob("");
    }
  }, [open]);

  function addFiles(list: File[]): void {
    if (list.length) setFiles((prev) => [...prev, ...list]);
  }

  async function uploadFiles(): Promise<void> {
    if (files.length === 0 || busy) return;
    const target = kbId;
    const batch = files.slice();
    const succeeded = new Set<File>();
    const cancelled = new Set<File>();
    let done = 0;
    let failed = 0;
    setBusy(true);
    cancelAll.current = false;
    const ac = new AbortController();
    abortRef.current = ac;

    for (let i = 0; i < batch.length; i++) {
      const file = batch[i]!;
      const fd = new FormData();
      fd.append("file", file);
      fd.append("kb_id", target);
      fd.append("engine", engine);
      setStage(`上传中 ${i + 1}/${batch.length} · 排队中`);
      const idem = `ui-${Date.now()}-${i}-${Math.random().toString(36).slice(2, 8)}`;
      try {
        const r = await apiUpload<IngestResult>("/api/documents?wait=false", fd, idem);
        if (r.status === "ready" || r.status === "duplicate") {
          done += 1;
          succeeded.add(file);
          continue;
        }
        if (!r.job_id) {
          done += 1;
          succeeded.add(file);
          continue;
        }
        pokeQueue();
        setCurrentJob(r.job_id);
        const job = await waitForJob(r.job_id, (j) => {
          setStage(`上传中 ${i + 1}/${batch.length} · ${stageLabel(j.stage)}`);
        }, ac.signal);
        setCurrentJob("");
        if (job?.stage === "done") {
          done += 1;
          succeeded.add(file);
        } else if (job?.stage === "cancelled") {
          cancelled.add(file);
          toast(`「${file.name}」已取消`);
        } else if (job?.stage === "failed") {
          failed += 1;
          toast(`「${file.name}」失败：${job.error ?? "未知错误"}`, "err");
        } else {
          toast(`「${file.name}」仍在后台处理，稍后刷新查看`);
        }
      } catch (e) {
        failed += 1;
        toast(`「${file.name}」失败：${e instanceof ApiError ? e.message : String(e)}`, "err");
      }
      if (cancelAll.current || ac.signal.aborted) break;
    }
    abortRef.current = null;

    setBusy(false);
    setStage("");
    setCurrentJob("");
    const remaining = files.filter((f) => !succeeded.has(f) && !cancelled.has(f));
    setFiles(remaining);
    if (done > 0) onDone();
    if (failed > 0) toast(`完成 ${done} 个，${failed} 个失败——失败项已保留，可重新提交`, "err");
    // 全部成功才自动收起：有失败项时留在面板里，用户可以直接重试
    if (failed === 0 && remaining.length === 0 && !cancelAll.current) onClose();
  }

  async function cancelCurrent(): Promise<void> {
    if (!currentJob) return;
    cancelAll.current = true;
    const r = await cancelJob(currentJob);
    if (r?.outcome === "running") setStage("正在停止（当前阶段结束后生效）…");
  }

  /** 文本 / URL 也走异步队列并显示阶段：同步等待会让对话框在解析/向量化期间假死。 */
  async function followIngest(jobId: string, label: string): Promise<WaitJob | null> {
    const ac = new AbortController();
    abortRef.current = ac;
    pokeQueue();
    setCurrentJob(jobId);
    const job = await waitForJob(jobId, (j) => {
      setStage(`${label} · ${stageLabel(j.stage)}`);
    }, ac.signal);
    setCurrentJob("");
    return ac.signal.aborted ? null : job;
  }

  async function submitText(): Promise<void> {
    const t = text.trim();
    if (!t || busy) return;
    setBusy(true);
    cancelAll.current = false;
    setStage("粘贴文本 · 排队中");
    try {
      const r = await api<IngestResult>("/api/documents/text?wait=false", {
        method: "POST",
        body: { text: t, title: title.trim() || undefined, kb_id: kbId },
      });
      const job = r.job_id ? await followIngest(r.job_id, "粘贴文本") : null;
      if (r.job_id && job?.stage !== "done") {
        if (job?.stage === "failed") {
          toast(`入库失败：${job.error || "未知原因"}`, "err");
        } else if (job?.stage === "cancelled") {
          toast("已取消");
        } else {
          toast("仍在后台处理，稍后刷新查看");
          onDone();
        }
        return;
      }
      toast("已入库");
      setText("");
      setTitle("");
      onClose();
      onDone();
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), "err");
    } finally {
      setBusy(false);
      setStage("");
    }
  }

  async function submitUrl(): Promise<void> {
    const u = url.trim();
    if (!u || busy) return;
    setBusy(true);
    cancelAll.current = false;
    setStage("抓取网页 · 排队中");
    try {
      const r = await api<IngestResult>("/api/documents/url?wait=false", {
        method: "POST", body: { url: u, kb_id: kbId },
      });
      const job = r.job_id ? await followIngest(r.job_id, "抓取网页") : null;
      if (r.job_id && job?.stage !== "done") {
        if (job?.stage === "failed") {
          toast(`抓取失败：${job.error || "未知原因"}`, "err");
        } else if (job?.stage === "cancelled") {
          toast("已取消");
        } else {
          toast("仍在后台处理，稍后刷新查看");
          onDone();
        }
        return;
      }
      toast("已抓取入库");
      setUrl("");
      onClose();
      onDone();
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), "err");
    } finally {
      setBusy(false);
      setStage("");
    }
  }

  return (
    <Dialog open={open} onClose={() => { if (!busy) onClose(); }} fullWidth maxWidth="sm">
      <DialogTitle>入库到「{kbName(kbId)}」</DialogTitle>
      <DialogContent>
        <Tabs
          value={tab}
          onChange={(_e, v: "file" | "text" | "url") => setTab(v)}
          sx={{
            mb: 2,
            "& .MuiTab-root": {
              minWidth: { xs: 0, sm: 90 },
              px: { xs: 0.75, sm: 3 },
            },
          }}
        >
          <Tab value="file" label="上传文件" />
          <Tab value="text" label="粘贴文本" />
          <Tab value="url" label="抓取网页" />
        </Tabs>

        {stage && (
          <Stack direction="row" spacing={1} alignItems="center" role="status" sx={{ mb: 2 }}>
            <CircularProgress size={14} />
            <Typography variant="body2" color="text.secondary">{stage}</Typography>
          </Stack>
        )}

        {tab === "file" && (
          <Stack spacing={2}>
            <Box
              sx={{
                border: 2, borderStyle: "dashed", borderColor: "divider",
                borderRadius: 1, p: { xs: 2, sm: 4 }, textAlign: "center",
              }}
              onDragOver={(e) => e.preventDefault()}
              onDrop={(e) => {
                e.preventDefault();
                if (!busy) addFiles([...(e.dataTransfer.files ?? [])]);
              }}
            >
              <InsertDriveFileIcon sx={{ fontSize: 40, color: "text.disabled", mb: 1 }} />
              <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
                把文件拖到这里，或选择文件（可多选）。支持 PDF / Markdown / 文本等常见格式。
              </Typography>
              <Stack direction="row" spacing={1} justifyContent="center" alignItems="center" useFlexGap flexWrap="wrap">
                <TextField
                  select size="small" label="解析引擎" value={engine}
                  onChange={(e) => setEngine(e.target.value)}
                  sx={{ flex: 1, minWidth: 130, maxWidth: 200 }}
                >
                  <MenuItem value="auto">auto（自动）</MenuItem>
                  <MenuItem value="pymupdf4llm">pymupdf4llm</MenuItem>
                  <MenuItem value="native">native</MenuItem>
                  <MenuItem value="docling">docling</MenuItem>
                  <MenuItem value="unstructured">unstructured</MenuItem>
                </TextField>
                <Button
                  variant="outlined" disabled={busy}
                  onClick={() => fileRef.current?.click()}
                  sx={{ flexShrink: 0, whiteSpace: "nowrap" }}
                >
                  选择文件
                </Button>
              </Stack>
              <input
                ref={fileRef}
                type="file"
                multiple
                hidden
                onChange={(e) => {
                  if (!busy) addFiles([...(e.target.files ?? [])]);
                  e.target.value = "";
                }}
              />
            </Box>

            {files.length > 0 && (
              <GlassPanel radius={12} look={look} sx={{ p: 1 }}>
                <Typography variant="body2" color="text.secondary" sx={{ px: 1, py: 0.5 }}>
                  待上传 {files.length} 个文件
                </Typography>
                <Stack spacing={0.5}>
                  {files.map((f, i) => (
                    <Stack key={`${f.name}-${i}`} direction="row" spacing={1} alignItems="center" sx={{ px: 1 }}>
                      <Typography variant="body2" sx={{ flex: 1, minWidth: 0 }} noWrap>
                        {f.name}
                      </Typography>
                      <Typography variant="caption" color="text.secondary">
                        {Math.max(1, Math.round(f.size / 1024))} KB
                      </Typography>
                      <IconButton
                        size="small" aria-label={`移除 ${f.name}`} disabled={busy}
                        onClick={() => setFiles((prev) => prev.filter((x) => x !== f))}
                      >
                        <DeleteIcon fontSize="small" />
                      </IconButton>
                    </Stack>
                  ))}
                </Stack>
              </GlassPanel>
            )}

            <Typography variant="caption" color="text.secondary">
              停止后：排队中的任务会立即取消；运行中的任务在当前阶段结束后停止。
            </Typography>
          </Stack>
        )}

        {tab === "text" && (
          <Stack spacing={2}>
            <TextField
              label="标题（可选）" value={title} fullWidth
              onChange={(e) => setTitle(e.target.value)}
            />
            <TextField
              label="正文" value={text} fullWidth multiline minRows={8}
              placeholder="粘贴要入库的文本…"
              onChange={(e) => setText(e.target.value)}
            />
          </Stack>
        )}

        {tab === "url" && (
          <Stack spacing={2}>
            <TextField
              label="网页地址" value={url} fullWidth autoFocus
              placeholder="https://example.com/doc"
              helperText="抓取正文后入库；指向内网地址的 URL 会被拒绝。"
              onChange={(e) => setUrl(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") void submitUrl(); }}
              slotProps={{ htmlInput: { "aria-label": "网页地址" } }}
            />
          </Stack>
        )}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} disabled={busy}>取消</Button>
        {busy && currentJob && (
          <Button color="error" startIcon={<StopCircleIcon />} onClick={() => void cancelCurrent()}>
            停止入库
          </Button>
        )}
        {tab === "file" && (
          <Button variant="contained" disabled={files.length === 0 || busy}
            startIcon={<AddIcon />} onClick={() => void uploadFiles()}>
            {busy ? "上传中…" : `入库 ${files.length || ""}`}
          </Button>
        )}
        {tab === "text" && (
          <Button variant="contained" disabled={!text.trim() || busy} onClick={() => void submitText()}>
            {busy ? "入库中…" : "入库"}
          </Button>
        )}
        {tab === "url" && (
          <Button variant="contained" disabled={!url.trim() || busy} onClick={() => void submitUrl()}>
            {busy ? "抓取中…" : "抓取入库"}
          </Button>
        )}
      </DialogActions>
    </Dialog>
  );
}
