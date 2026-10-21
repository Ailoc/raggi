import { useCallback, useEffect, useRef, useState } from "react";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import Paper from "@mui/material/Paper";
import Skeleton from "@mui/material/Skeleton";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";

import { api } from "../lib/api";
import { fmtBytes } from "../lib/format";
import type { Stats } from "../lib/types";

/**
 * 存储：容量、目录占用、磁盘剩余与已建索引。
 *
 * 版本管理（/api/versions、/api/rollback）已整体退役——单机场景的
 * 误操作修复路径是重建索引 / 重放，而不是回退表版本。这个面板因此
 * 只剩只读统计。
 */
export default function DataPanel(): React.ReactElement {
  const [stats, setStats] = useState<Stats | null>(null);
  const [phase, setPhase] = useState<"initial" | "ready" | "failed">("initial");
  const [error, setError] = useState("");
  const loadSeq = useRef(0);

  const load = useCallback(async (fresh = false): Promise<void> => {
    const seq = ++loadSeq.current;
    setPhase((p) => (p === "ready" ? "ready" : "initial"));
    try {
      const s = await api<Stats>(`/api/stats${fresh ? "?fresh=true" : ""}`);
      if (seq !== loadSeq.current) return;
      setStats(s);
      setPhase("ready");
      setError("");
    } catch (e) {
      if (seq !== loadSeq.current) return;
      setError(e instanceof Error ? e.message : String(e));
      setPhase((p) => (p === "ready" ? "ready" : "failed"));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => { void load(); }, [load]);

  return (
    <Paper variant="outlined" sx={{ p: 3 }}>
      <Stack direction="row" spacing={2} alignItems="center" sx={{ mb: 2 }}>
        <Box sx={{ flex: 1 }}>
          <Typography variant="h6" sx={{ fontSize: 16 }}>容量</Typography>
          <Typography variant="body2" color="text.secondary">
            后端 {stats?.backend ?? "—"}；数据目录占用与索引规模。
          </Typography>
        </Box>
        <Button size="small" variant="outlined" onClick={() => void load(true)}>刷新</Button>
      </Stack>

      {phase === "failed" && (
        <Alert severity="error" action={
          <Button color="inherit" size="small" onClick={() => void load(true)}>重试</Button>
        }>
          读取统计失败：{error}
        </Alert>
      )}

      {phase === "initial" && <Skeleton variant="rounded" height={180} />}

      {phase === "ready" && stats && (
        <>
          <Box
            sx={{
              display: "grid", gap: 1.5,
              gridTemplateColumns: { xs: "1fr 1fr", sm: "repeat(3, 1fr)" },
            }}
          >
            {[
              ["文档", String(stats.doc_count)],
              ["分块", String(stats.chunk_count)],
              ["任务记录", String(stats.job_count)],
              ["数据目录", fmtBytes(stats.data_dir_bytes)],
              ["索引目录", fmtBytes(stats.lancedb_dir_bytes)],
              ["磁盘剩余", fmtBytes(stats.disk_free_bytes)],
            ].map(([label, value]) => (
              <Box key={label}>
                <Typography variant="caption" color="text.secondary">{label}</Typography>
                <Typography variant="body2" sx={{ fontVariantNumeric: "tabular-nums" }}>{value}</Typography>
              </Box>
            ))}
          </Box>

          {stats.indexes && (
            <>
              <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 3, mb: 1 }}>
                已建索引
              </Typography>
              <Stack spacing={0.75}>
                {Object.entries(stats.indexes).map(([table, list]) => (
                  <Stack key={table} direction="row" spacing={1} alignItems="center" flexWrap="wrap">
                    <Typography variant="body2" sx={{ fontWeight: 500 }}>{table}</Typography>
                    {list.length === 0
                      ? <Typography variant="caption" color="text.secondary">无索引</Typography>
                      : list.map((ix) => (
                        <Chip
                          key={`${ix.type}-${ix.columns.join(",")}`}
                          size="small" variant="outlined"
                          label={`${ix.type} · ${ix.columns.join(", ")}`}
                        />
                      ))}
                  </Stack>
                ))}
              </Stack>
            </>
          )}
        </>
      )}
    </Paper>
  );
}
