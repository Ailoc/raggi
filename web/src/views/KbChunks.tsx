import { useCallback, useEffect, useRef, useState } from "react";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Checkbox from "@mui/material/Checkbox";
import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
import Divider from "@mui/material/Divider";
import IconButton from "@mui/material/IconButton";
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
import ToggleButton from "@mui/material/ToggleButton";
import ToggleButtonGroup from "@mui/material/ToggleButtonGroup";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import AddIcon from "@mui/icons-material/Add";
import DeleteIcon from "@mui/icons-material/Delete";
import EditIcon from "@mui/icons-material/Edit";
import VisibilityIcon from "@mui/icons-material/Visibility";
import VisibilityOffIcon from "@mui/icons-material/VisibilityOff";

import { api } from "../lib/api";
import { CONTENT_MAX } from "../lib/layout";
import { go, href } from "../lib/router";
import { usePicks } from "../lib/selection";
import { kbName, setCrumbs, track, useStore } from "../lib/store";
import {
  addChunkDialog, batchSetEnabled, deleteChunk, editChunkDialog, setChunkEnabled,
} from "../lib/ops";
import type { Chunk, ChunkList } from "../lib/types";

const PAGE_SIZE = 20;

/**
 * 全库分块：范围切换（全部 / 仅独立块）+ 服务端过滤 + 分页 + 行内编辑/启停/删除。
 *
 * 范围过滤下推到服务端（`only_standalone`）：先取全库再本地筛会让"文档里的
 * 分块"占满上限、独立分块整个不可见——这正是当初单独开这一页的原因。
 */
export default function KbChunks({
  kbId,
  scope = "",
}: {
  kbId: string;
  scope?: string;
}): React.ReactElement {
  const look = useGlassLook("card");
  const { kbsLoaded } = useStore();
  const standalone = scope === "standalone";
  const [items, setItems] = useState<Chunk[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(0);
  const [filter, setFilter] = useState("");
  const [q, setQ] = useState("");
  const [phase, setPhase] = useState<"initial" | "ready" | "failed">("initial");
  const [error, setError] = useState("");
  const [refreshing, setRefreshing] = useState(false);
  const picks = usePicks();
  const reqSeq = useRef(0);

  const load = useCallback(
    async (targetPage = page, query = q, only = standalone): Promise<void> => {
      const seq = ++reqSeq.current;
      setRefreshing(true);
      try {
        const sp = new URLSearchParams({
          kb_id: kbId,
          only_standalone: String(only),
          include_disabled: "true",
          limit: String(PAGE_SIZE),
          offset: String(targetPage * PAGE_SIZE),
        });
        if (query) sp.set("q", query);
        const r = await track(api<ChunkList>(`/api/chunks?${sp.toString()}`));
        if (seq !== reqSeq.current) return;
        setItems(r.items);
        setTotal(r.total ?? 0);
        setPhase("ready");
        setError("");
        const maxPage = Math.max(0, Math.ceil((r.total ?? 0) / PAGE_SIZE) - 1);
        if (targetPage > maxPage) {
          setPage(maxPage);
          void load(maxPage, query, only);
        }
      } catch (e) {
        if (seq !== reqSeq.current) return;
        setPhase((p) => (p === "ready" ? "ready" : "failed"));
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        if (seq === reqSeq.current) setRefreshing(false);
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [kbId, page, q, standalone],
  );

  useEffect(() => {
    setCrumbs([
      { label: "知识库", href: "#/" },
      { label: kbName(kbId), href: href("kb", kbId, "docs") },
      { label: standalone ? "独立分块" : "全部分块" },
    ]);
  }, [kbId, standalone, kbsLoaded]);

  // 换库/换范围：回到第一页
  useEffect(() => {
    setPage(0);
    picks.clear();
    void load(0, q, standalone);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kbId, standalone]);

  useEffect(() => {
    const t = setTimeout(() => {
      if (filter.trim() === q) return;
      setQ(filter.trim());
      setPage(0);
      picks.clear();
      void load(0, filter.trim(), standalone);
    }, 300);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filter]);

  function setScope(next: string): void {
    go(next === "standalone"
      ? `${href("kb", kbId, "chunks")}?scope=standalone`
      : href("kb", kbId, "chunks"));
  }

  const isOn = (c: Chunk): boolean => c.enabled !== false;

  async function afterChange(): Promise<void> {
    await load();
  }

  const pageKeys = items.map((c) => c.chunk_id);
  const pickedChunks = items.filter((c) => picks.has(c.chunk_id));

  return (
    <Box sx={{ p: { xs: 2, md: 3 }, maxWidth: CONTENT_MAX, mx: "auto" }}>
      <Stack direction="row" spacing={2} alignItems="flex-start" sx={{ mb: 2 }}>
        <Box sx={{ flex: 1, minWidth: 0 }}>
          <Typography variant="h5" sx={{ fontWeight: 500 }}>
            {standalone ? "独立分块" : "全部分块"}
          </Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
            {standalone
              ? `不属于任何文档、直接挂在「${kbName(kbId)}」下的分块`
              : `「${kbName(kbId)}」里的所有分块，含不属于任何文档的独立分块`}
            {q ? "" : ` · 共 ${total} 块`}
          </Typography>
        </Box>
        <ToggleButtonGroup
          size="small"
          exclusive
          value={standalone ? "standalone" : "all"}
          onChange={(_e, v: string | null) => { if (v) setScope(v === "standalone" ? "standalone" : ""); }}
          aria-label="分块范围"
        >
          <ToggleButton value="all">全部</ToggleButton>
          <ToggleButton value="standalone">仅独立块</ToggleButton>
        </ToggleButtonGroup>
        <Button
          variant="contained"
          startIcon={<AddIcon />}
          onClick={() => {
            void addChunkDialog({ kbId }).then((ok) => { if (ok) void afterChange(); });
          }}
        >
          新增独立分块
        </Button>
      </Stack>

      <Stack direction="row" spacing={2} alignItems="center" sx={{ mb: 2 }}>
        <TextField
          size="small"
          type="search"
          placeholder="过滤分块内容…"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          sx={{ width: 320 }}
          slotProps={{ htmlInput: { "aria-label": "按内容过滤分块" } }}
        />
        {refreshing && <CircularProgress size={14} aria-label="正在刷新" />}
        {q && (
          <Typography variant="body2" color="text.secondary">匹配 {total} 块</Typography>
        )}
        {pickedChunks.length > 0 && (
          <>
            <Divider orientation="vertical" flexItem />
            <Typography variant="body2" color="text.secondary">
              已选 {pickedChunks.length} 块
            </Typography>
            <Button size="small" startIcon={<VisibilityIcon />}
              onClick={() => void batchSetEnabled(pickedChunks, true).then((o) => { if (o) { picks.clear(); void afterChange(); } })}>
              批量启用
            </Button>
            <Button size="small" startIcon={<VisibilityOffIcon />}
              onClick={() => void batchSetEnabled(pickedChunks, false).then((o) => { if (o) { picks.clear(); void afterChange(); } })}>
              批量停用
            </Button>
          </>
        )}
      </Stack>

      {phase === "failed" && (
        <Alert severity="error" sx={{ mb: 2 }}
          action={<Button color="inherit" size="small" onClick={() => void load()}>重试</Button>}>
          加载失败：{error}
        </Alert>
      )}

      {phase === "initial" && <Skeleton variant="rounded" height={320} />}

      {phase === "ready" && error && (
        <Alert severity="error" sx={{ mb: 2 }}
          action={<Button color="inherit" size="small" onClick={() => void load()}>重试</Button>}>
          刷新失败，下面是上一次取到的结果：{error}
        </Alert>
      )}

      {phase === "ready" && items.length === 0 && (
        <Alert
          severity={q ? "warning" : "info"}
          action={q ? (
            <Button color="inherit" size="small" onClick={() => setFilter("")}>清空过滤</Button>
          ) : undefined}
        >
          {q
            ? `没有包含「${q}」的分块。`
            : standalone
              ? "还没有独立分块。用右上角「新增独立分块」补术语表、口径说明等内容。"
              : "这个库里还没有分块。文档入库并切分后会出现在这里。"}
        </Alert>
      )}

      {phase === "ready" && items.length > 0 && (
        <GlassPanel radius={12} look={look}>
          <TableContainer>
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
                  <TableCell sx={{ width: 140 }}>状态</TableCell>
                  <TableCell align="right" sx={{ width: 130 }}>
                    <span className="sr-only">操作</span>
                  </TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {items.map((c) => (
                  <TableRow key={c.chunk_id} hover>
                    <TableCell padding="checkbox">
                      <Checkbox
                        size="small"
                        checked={picks.has(c.chunk_id)}
                        onChange={(e) => picks.toggle(c.chunk_id, e.target.checked)}
                        inputProps={{ "aria-label": `选择第 ${c.ordinal ?? 0} 块` }}
                      />
                    </TableCell>
                    <TableCell align="right">{c.ordinal ?? 0}</TableCell>
                    <TableCell sx={{ maxWidth: 640 }}>
                      <Typography
                        variant="body2"
                        title={c.text}
                        sx={{
                          overflow: "hidden", display: "-webkit-box",
                          WebkitLineClamp: 2, WebkitBoxOrient: "vertical",
                          color: c.enabled === false ? "text.disabled" : "text.primary",
                        }}
                      >
                        {c.text}
                      </Typography>
                    </TableCell>
                    <TableCell align="right">{c.page ?? "—"}</TableCell>
                    <TableCell>
                      <Stack direction="row" spacing={0.5}>
                        {c.origin === "manual" && <Chip size="small" label="手工" variant="outlined" />}
                        {c.edited && <Chip size="small" color="warning" label="已编辑" variant="outlined" />}
                        {!isOn(c) && <Chip size="small" color="error" label="已停用" variant="outlined" />}
                      </Stack>
                    </TableCell>
                    <TableCell align="right">
                      <Stack direction="row" spacing={0.5} justifyContent="flex-end">
                        <Tooltip title="编辑内容">
                          <IconButton size="small" aria-label={`编辑第 ${c.ordinal ?? 0} 块`}
                            onClick={() => void editChunkDialog(c).then((ok) => { if (ok) void afterChange(); })}>
                            <EditIcon fontSize="small" />
                          </IconButton>
                        </Tooltip>
                        <Tooltip title={isOn(c) ? "停用（退出检索）" : "启用"}>
                          <IconButton size="small"
                            aria-label={isOn(c) ? `停用第 ${c.ordinal ?? 0} 块` : `启用第 ${c.ordinal ?? 0} 块`}
                            onClick={() => void setChunkEnabled(c, !isOn(c)).then((ok) => { if (ok) void afterChange(); })}>
                            {isOn(c) ? <VisibilityOffIcon fontSize="small" /> : <VisibilityIcon fontSize="small" />}
                          </IconButton>
                        </Tooltip>
                        <Tooltip title="删除">
                          <IconButton size="small" color="error" aria-label={`删除第 ${c.ordinal ?? 0} 块`}
                            onClick={() => void deleteChunk(c).then((ok) => { if (ok) void afterChange(); })}>
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
            onPageChange={(_e, p) => { setPage(p); picks.clear(); void load(p, q, standalone); }}
          />
        </GlassPanel>
      )}
    </Box>
  );
}
