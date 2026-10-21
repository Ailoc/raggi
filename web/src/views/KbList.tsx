import { useEffect, useRef, useState } from "react";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Card from "@mui/material/Card";
import CardActionArea from "@mui/material/CardActionArea";
import CardActions from "@mui/material/CardActions";
import CardContent from "@mui/material/CardContent";
import Chip from "@mui/material/Chip";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import IconButton from "@mui/material/IconButton";
import Skeleton from "@mui/material/Skeleton";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import AddIcon from "@mui/icons-material/Add";
import ChevronRightIcon from "@mui/icons-material/ChevronRight";
import FolderIcon from "@mui/icons-material/Folder";
import RefreshIcon from "@mui/icons-material/Refresh";
import SettingsIcon from "@mui/icons-material/Settings";

import { api } from "../lib/api";
import { href, go } from "../lib/router";
import { invalidateKbs, loadKbs, setCrumbs, useStore } from "../lib/store";
import { fmtTime } from "../lib/format";
import type { Kb } from "../lib/types";
import { toast } from "../ui/toast";

/**
 * 知识库总览：卡片网格 + 新建。
 *
 * 卡片是 outlined 变体（1px 分隔线、无阴影）而不是 elevation —— 一屏
 * 3~5 张卡全带阴影会"像贴了一墙便利贴"；MUI 自己的仪表盘模板同样这么做。
 *
 * 卡片上没有删除：删除需要展示影响面（N 篇文档 / M 个分块）并要求输入
 * 库名确认，属于设置页的职责。总览页只做"进入"。
 */
export default function KbList(): React.ReactElement {
  const { kbs, kbsLoaded } = useStore();
  const [open, setOpen] = useState(false);
  const [error, setError] = useState("");
  const reloadSeq = useRef(0);

  const reload = (): void => {
    const seq = ++reloadSeq.current;
    setError("");
    invalidateKbs();
    void loadKbs().catch((e) => {
      if (seq === reloadSeq.current) {
        setError(e instanceof Error ? e.message : String(e));
      }
    });
  };

  useEffect(() => {
    setCrumbs([{ label: "知识库" }]);
    reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <Box sx={{ p: { xs: 2, md: 3 }, maxWidth: 1440, mx: "auto" }}>
      <Stack direction="row" alignItems="flex-start" spacing={2} sx={{ mb: 3 }}>
        <Box sx={{ flex: 1, minWidth: 0 }}>
          <Typography variant="h5" sx={{ fontWeight: 500 }}>
            知识库
          </Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
            按主题组织文档。分块方案等进阶参数建库后在「知识库设置」里单独配置。
          </Typography>
        </Box>
        <Button
          variant="outlined"
          startIcon={<RefreshIcon />}
          onClick={() => {
            invalidateKbs();
            setError("");
            void loadKbs().then(
              () => toast("知识库列表已刷新"),
              (e) => {
                const msg = e instanceof Error ? e.message : String(e);
                setError(msg);
                toast("刷新失败", "err");
              },
            );
          }}
        >
          刷新
        </Button>
        <Button variant="contained" startIcon={<AddIcon />} onClick={() => setOpen(true)}>
          新建知识库
        </Button>
      </Stack>

      {!kbsLoaded && error && (
        <Alert
          severity="error"
          sx={{ mb: 2 }}
          action={<Button color="inherit" size="small" onClick={reload}>重试</Button>}
        >
          加载知识库失败：{error}
        </Alert>
      )}

      {!kbsLoaded && !error && (
        <Box
          sx={{
            display: "grid", gap: 2,
            gridTemplateColumns: { xs: "1fr", sm: "repeat(2, 1fr)", lg: "repeat(3, 1fr)" },
          }}
        >
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} variant="rounded" height={148} />
          ))}
        </Box>
      )}

      {kbsLoaded && kbs.length === 0 && (
        <Alert severity="info" action={
          <Button color="inherit" size="small" onClick={() => setOpen(true)}>新建</Button>
        }>
          还没有知识库。先建一个，再把文档入库。
        </Alert>
      )}

      <Box
        sx={{
          display: "grid", gap: 2,
          gridTemplateColumns: { xs: "1fr", sm: "repeat(2, 1fr)", lg: "repeat(3, 1fr)" },
        }}
      >
        {kbs.map((k) => (
          <KbCard key={k.kb_id} kb={k} />
        ))}
      </Box>

      <CreateKbDialog open={open} onClose={() => setOpen(false)} />
    </Box>
  );
}

function KbCard({ kb }: { kb: Kb }): React.ReactElement {
  return (
    <Card variant="outlined" sx={{ display: "flex", flexDirection: "column" }}>
      <CardActionArea
        onClick={() => go(href("kb", kb.kb_id, "docs"))}
        sx={{ flex: 1, alignItems: "stretch" }}
      >
        <CardContent sx={{ pb: 1 }}>
          <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1 }}>
            <FolderIcon color="primary" fontSize="small" />
            <Typography
              variant="subtitle1"
              sx={{ fontWeight: 500, flex: 1, minWidth: 0, wordBreak: "break-word" }}
            >
              {kb.name}
            </Typography>
          </Stack>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
            {kb.doc_count} 文档 · {kb.chunk_count} 分块
            {" · "}
            {kb.updated_at ? fmtTime(kb.updated_at) : "—"}
          </Typography>
          <Typography
            variant="body2"
            color="text.secondary"
            sx={{
              minHeight: 40,
              overflow: "hidden",
              display: "-webkit-box",
              WebkitLineClamp: 2,
              WebkitBoxOrient: "vertical",
            }}
          >
            {kb.description || "（无描述）"}
          </Typography>
        </CardContent>
      </CardActionArea>

      <CardActions sx={{ justifyContent: "space-between", px: 2, pb: 1.5 }}>
        <Chip
          size="small"
          variant="outlined"
          color="primary"
          label={`${kb.chunk_size || 512} 字符 · 重叠 ${kb.overlap_ratio || 0}%`}
        />
        <Tooltip title="知识库设置">
          <IconButton
            size="small"
            aria-label={`${kb.name}的设置`}
            onClick={() => go(href("kb", kb.kb_id, "settings"))}
          >
            <SettingsIcon fontSize="small" />
          </IconButton>
        </Tooltip>
      </CardActions>
    </Card>
  );
}

/** 新建知识库对话框（MUI Dialog）。名称必填，创建后回到总览并刷新。 */
function CreateKbDialog({ open, onClose }: { open: boolean; onClose: () => void }): React.ReactElement {
  const [name, setName] = useState("");
  const [desc, setDesc] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (open) {
      setName("");
      setDesc("");
    }
  }, [open]);

  async function submit(): Promise<void> {
    if (!name.trim() || busy) return;
    setBusy(true);
    try {
      await api<Kb>("/api/kbs", {
        method: "POST",
        body: { name: name.trim(), description: desc.trim() },
      });
      invalidateKbs();
      await loadKbs();
      toast("知识库已创建");
      onClose();
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), "err");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onClose={onClose} fullWidth maxWidth="xs">
      <DialogTitle>新建知识库</DialogTitle>
      <DialogContent>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          先起个名字。描述、分块方案等进阶设置，建库后在「知识库设置」里随时可以改。
        </Typography>
        <Stack spacing={2}>
          <TextField
            label="名称"
            value={name}
            autoFocus
            fullWidth
            placeholder="例如：产品手册"
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") void submit();
            }}
          />
          <TextField
            label="描述（可选）"
            value={desc}
            fullWidth
            multiline
            minRows={2}
            onChange={(e) => setDesc(e.target.value)}
          />
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>取消</Button>
        <Button
          variant="contained"
          disabled={!name.trim() || busy}
          onClick={() => void submit()}
          endIcon={<ChevronRightIcon />}
        >
          {busy ? "创建中…" : "创建"}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
