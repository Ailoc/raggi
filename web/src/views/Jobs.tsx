import { useCallback, useEffect, useRef, useState } from "react";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
import Divider from "@mui/material/Divider";
import FormControlLabel from "@mui/material/FormControlLabel";
import IconButton from "@mui/material/IconButton";
import LinearProgress from "@mui/material/LinearProgress";
import { GlassPanel, useGlassLook } from "../ui/glass";
import Skeleton from "@mui/material/Skeleton";
import Stack from "@mui/material/Stack";
import Switch from "@mui/material/Switch";
import Tab from "@mui/material/Tab";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TablePagination from "@mui/material/TablePagination";
import TableRow from "@mui/material/TableRow";
import Tabs from "@mui/material/Tabs";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import RefreshIcon from "@mui/icons-material/Refresh";
import StopCircleIcon from "@mui/icons-material/StopCircle";

import { api } from "../lib/api";
import { CONTENT_MAX } from "../lib/layout";
import { fmtSpan, fmtTimeShort } from "../lib/format";
import { setCrumbs, track, useQueue } from "../lib/store";
import { cancelJob } from "../lib/jobcancel";
import { stageLabel } from "../lib/jobs";
import type { DocumentList, Job, JobList } from "../lib/types";

const PAGE_SIZE = 30;

type Filter = "all" | "active" | "failed" | "done";

const STAGE_COLOR: Record<string, "default" | "info" | "success" | "error" | "warning"> = {
  done: "success",
  failed: "error",
  cancelled: "warning",
  cancelling: "warning",
};

/**
 * 任务列表：状态 / 阶段与进度 / 耗时 / 停止 + 筛选 + 自动刷新。
 *
 * 自动刷新**只在有活跃任务时**开：任务都跑完了还每 2 秒打一次接口，
 * 是拿本机数据库的读放大换心理安慰（这条判断在 Svelte 版就写下了）。
 */
export default function Jobs(): React.ReactElement {
  const look = useGlassLook("card");
  const queue = useQueue();
  const [items, setItems] = useState<Job[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(0);
  const [filter, setFilter] = useState<Filter>("all");
  const [auto, setAuto] = useState(true);
  const [phase, setPhase] = useState<"initial" | "ready" | "failed">("initial");
  const [error, setError] = useState("");
  const [refreshing, setRefreshing] = useState(false);
  const [titles, setTitles] = useState<Record<string, string>>({});
  const reqSeq = useRef(0);
  const pageRef = useRef(0);
  const filterRef = useRef<Filter>("all");

  const load = useCallback(async (
    targetPage = pageRef.current,
    status = filterRef.current,
  ): Promise<void> => {
    const seq = ++reqSeq.current;
    setRefreshing(true);
    try {
      // 过滤与分页都由服务端完成：客户端先取一页再筛，会让 total 与列表
      // 各说各话，而且筛不到其它页上的任务。
      const r = await track(api<JobList>(
        `/api/jobs?limit=${PAGE_SIZE}&offset=${targetPage * PAGE_SIZE}`
        + `&status=${status}`));
      if (seq !== reqSeq.current) return;
      setItems(r.items);
      setTotal(r.total ?? 0);
      setPhase("ready");
      setError("");
    } catch (e) {
      if (seq !== reqSeq.current) return;
      setPhase((p) => (p === "ready" ? "ready" : "failed"));
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      if (seq === reqSeq.current) setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    setCrumbs([{ label: "任务" }]);
    void load(0, "all");
    // 文档标题映射：一次拉一窗建 map，比每行各查一次省 30 个请求
    void api<DocumentList>("/api/documents?limit=200")
      .then((r) => {
        const m: Record<string, string> = {};
        for (const d of r.items) m[d.doc_id] = d.title;
        setTitles(m);
      })
      .catch(() => { /* 标题是装饰：拉不到就显示短码 */ });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const activeCount = queue.active;

  // 自动刷新：仅活跃任务 + 页面可见。活跃数来自全局队列摘要，
  // 不能只看“当前页”的 items（在「失败」标签下会错误地停掉轮询）。
  useEffect(() => {
    if (!auto || activeCount === 0) return;
    const t = setInterval(() => {
      if (document.visibilityState === "visible") void load();
    }, 2000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [auto, activeCount, load]);

  function changeFilter(next: Filter): void {
    filterRef.current = next;
    setFilter(next);
    pageRef.current = 0;
    setPage(0);
    setItems([]);
    setPhase("initial");
    void load(0, next);
  }

  function turnPage(next: number): void {
    pageRef.current = next;
    setPage(next);
    void load(next, filterRef.current);
  }

  async function cancel(job: Job): Promise<void> {
    // 服务端按 outcome 组织文案（排队中=真取消 / 运行中=阶段结束后停），
    // 前端转述它而不是自己编一套。
    await cancelJob(job.job_id, () => { void load(); });
  }

  function duration(j: Job): string {
    if (!j.started_at) return "—";
    const end = j.ended_at ? new Date(j.ended_at).getTime() : Date.now();
    return fmtSpan(end - new Date(j.started_at).getTime());
  }

  return (
    <Box sx={{ p: { xs: 2, md: 3 }, maxWidth: CONTENT_MAX, mx: "auto" }}>
      <Stack direction="row" spacing={2} alignItems="center" useFlexGap flexWrap="wrap" sx={{ mb: 2 }}>
        <Typography
          variant="h5"
          sx={{ fontWeight: 500, flex: { xs: "1 1 100%", sm: 1 } }}
        >
          任务
        </Typography>
        <FormControlLabel
          control={<Switch checked={auto} onChange={(e) => setAuto(e.target.checked)} />}
          label="自动刷新（仅活跃任务）"
        />
        <Button variant="outlined" startIcon={<RefreshIcon />} onClick={() => void load()}>
          刷新
        </Button>
      </Stack>

      {phase === "initial" && <Skeleton variant="rounded" height={320} />}

      {phase === "failed" && (
        <Alert severity="error" sx={{ mb: 2 }}
          action={<Button color="inherit" size="small" onClick={() => void load()}>重试</Button>}>
          加载失败：{error}
        </Alert>
      )}

      {phase === "ready" && (
        <>
          <Tabs
            value={filter}
            onChange={(_e, v: Filter) => changeFilter(v)}
            sx={{
              mb: 2, minHeight: 40,
              "& .MuiTab-root": {
                minWidth: { xs: 0, sm: 90 },
                px: { xs: 1.25, sm: 3 },
              },
            }}
          >
            <Tab value="all" label="全部" />
            <Tab value="active" label={`进行中${activeCount ? ` (${activeCount})` : ""}`} />
            <Tab value="failed" label="失败" />
            <Tab value="done" label="已完成" />
          </Tabs>

          {refreshing && activeCount > 0 && (
            <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1 }}>
              <CircularProgress size={12} />
              <Typography variant="caption" color="text.secondary">更新中…</Typography>
            </Stack>
          )}

          {items.length === 0 ? (
            <Alert severity="info">
              {filter === "active" ? "当前没有进行中的任务。"
                : filter === "failed" ? "没有失败的任务。"
                : filter === "done" ? "没有已完成的任务。"
                : "还没有任务记录。"}
            </Alert>
          ) : (
            <GlassPanel radius={12} look={look} className="lg-clip">
              <TableContainer>
                <Table size="small" aria-label="任务列表" sx={{ tableLayout: "fixed" }}>
                  <TableHead>
                    <TableRow>
                      <TableCell sx={{ display: { xs: "none", sm: "table-cell" }, width: 90 }}>
                        状态
                      </TableCell>
                      <TableCell sx={{ width: { xs: 120, sm: 180 } }}>阶段</TableCell>
                      <TableCell>文档</TableCell>
                      <TableCell sx={{ display: { xs: "none", md: "table-cell" }, width: 100 }}>
                        引擎
                      </TableCell>
                      <TableCell
                        align="right"
                        sx={{ display: { xs: "none", md: "table-cell" }, width: 90 }}
                      >
                        耗时
                      </TableCell>
                      <TableCell sx={{ display: { xs: "none", md: "table-cell" }, width: 130 }}>
                        开始
                      </TableCell>
                      <TableCell align="right" sx={{ width: 40 }}>
                        <span className="sr-only">操作</span>
                      </TableCell>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {items.map((j) => (
                      <TableRow key={j.job_id} hover>
                        <TableCell sx={{ display: { xs: "none", sm: "table-cell" } }}>
                          <Chip
                            size="small"
                            variant="outlined"
                            color={STAGE_COLOR[j.stage] ?? "info"}
                            label={stageLabel(j.stage)}
                          />
                        </TableCell>
                        <TableCell>
                          <Typography variant="body2">{stageLabel(j.stage)}</Typography>
                          {!j.terminal && j.total > 0 && (
                            <LinearProgress
                              variant="determinate"
                              value={Math.min(100, Math.round((j.progress / j.total) * 100))}
                              sx={{ mt: 0.5 }}
                            />
                          )}
                          {j.error && (
                            <Tooltip title={j.error}>
                              <Typography
                                variant="caption"
                                color="error"
                                sx={{
                                  display: "block", maxWidth: 320,
                                  overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                                }}
                              >
                                {j.error}
                              </Typography>
                            </Tooltip>
                          )}
                        </TableCell>
                        <TableCell sx={{ maxWidth: 260 }}>
                          <Typography
                            variant="body2"
                            title={titles[j.doc_id] ?? j.doc_id}
                            sx={{
                              overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                            }}
                          >
                            {titles[j.doc_id] ?? (j.doc_id ? j.doc_id.slice(0, 8) : "—")}
                          </Typography>
                        </TableCell>
                        <TableCell sx={{ display: { xs: "none", md: "table-cell" } }}>
                          <Typography variant="body2" color="text.secondary">
                            {j.parser_engine || "—"}
                          </Typography>
                        </TableCell>
                        <TableCell
                          align="right"
                          sx={{ display: { xs: "none", md: "table-cell" } }}
                        >
                          {duration(j)}
                        </TableCell>
                        <TableCell sx={{ display: { xs: "none", md: "table-cell" } }}>
                          <Typography variant="body2" color="text.secondary">
                            {j.started_at ? fmtTimeShort(j.started_at) : "—"}
                          </Typography>
                        </TableCell>
                        <TableCell align="right">
                          {!j.terminal && (
                            <Tooltip title="停止（当前阶段结束后生效）">
                              <IconButton size="small" color="error"
                                aria-label="停止任务" onClick={() => void cancel(j)}>
                                <StopCircleIcon fontSize="small" />
                              </IconButton>
                            </Tooltip>
                          )}
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
                labelDisplayedRows={({ from, to, count }) => `${from}–${to} / 共 ${count} 个任务`}
                onPageChange={(_e, p) => turnPage(p)}
              />
            </GlassPanel>
          )}
        </>
      )}
    </Box>
  );
}
