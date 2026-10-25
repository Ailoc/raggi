import { useEffect, useRef, useState } from "react";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
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
import { CONTENT_MAX } from "../lib/layout";
import { href, go } from "../lib/router";
import { invalidateKbs, loadKbs, setCrumbs, useStoreSelect } from "../lib/store";
import { fmtTime } from "../lib/format";
import type { Kb } from "../lib/types";
import { GlassPanel, useGlassLook } from "../ui/glass";
import { toast } from "../ui/toast";

/**
 * 知识库总览：玻璃卡片网格 + 新建。
 *
 * 卡片是**液态玻璃**：材质来自 ui/glass.tsx（open-glass-ui 的 CSS 材质）。
 * 玻璃要有东西可弯，所以整页铺了一层极光背景（GlassBackdrop）——它同时
 * 是卡片底下被模糊、被折射的那层内容。
 *
 * 卡片上没有删除：删除需要展示影响面（N 篇文档 / M 个分块）并要求输入
 * 库名确认，属于设置页的职责。总览页只做"进入"。
 */
export default function KbList(): React.ReactElement {
  const kbs = useStoreSelect((s) => s.kbs);
  const kbsLoaded = useStoreSelect((s) => s.kbsLoaded);
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
    <Box sx={{ p: { xs: 2, md: 3 }, maxWidth: CONTENT_MAX, mx: "auto" }}>
      {/* 小屏把操作按钮收到标题下方，避免描述被挤成一字一行；大屏四列，
          否则 1920 下三张卡各占半屏、右半边全是空白。 */}
      <Stack
        direction={{ xs: "column", sm: "row" }}
        alignItems="flex-start"
        spacing={2}
        sx={{ mb: 3 }}
      >
        <Box sx={{ flex: 1, minWidth: 0, width: "100%" }}>
          <Typography variant="h5" sx={{ fontWeight: 500 }}>
            知识库
          </Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
            按主题组织文档。分块方案等进阶参数建库后在「知识库设置」里单独配置。
          </Typography>
        </Box>
        <Stack
          direction="row"
          spacing={1}
          sx={{ flexShrink: 0, alignSelf: { xs: "flex-end", sm: "flex-start" } }}
        >
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

      {/* 骨架屏与卡片共用同一层背景，加载完成时极光不会"跳"一下 */}
      {!kbsLoaded && !error && (
        <Box
          sx={{
            display: "grid", gap: 2,
            gridTemplateColumns: {
              xs: "1fr", sm: "repeat(2, minmax(0, 1fr))",
              lg: "repeat(3, minmax(0, 1fr))", xl: "repeat(4, minmax(0, 1fr))",
            },
          }}
        >
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} variant="rounded" height={148} sx={{ borderRadius: 5 }} />
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
          gridTemplateColumns: {
            xs: "1fr", sm: "repeat(2, minmax(0, 1fr))",
            lg: "repeat(3, minmax(0, 1fr))", xl: "repeat(4, minmax(0, 1fr))",
          },
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

/**
 * 卡片的艺术指导：比默认更透一点、边缘更亮一点。
 *
 * 玻璃底下是极光背景，卡片要"浮"出来就得比背景更亮——`opacity` 压低
 * 让底下的颜色透上来，`rim`/`lensing` 拉高让边缘读出厚度。
 */
function KbCard({ kb }: { kb: Kb }): React.ReactElement {
  const look = useGlassLook("card");
  return (
    <GlassPanel radius={20} className="lg-kb-card" look={look} blur={false}>
      <CardActionArea
        onClick={() => go(href("kb", kb.kb_id, "docs"))}
        sx={{
          flex: 1,
          alignItems: "stretch",
          // MUI 的悬停遮罩只覆盖 CardActionArea（上半张卡），下方的
          // CardActions 不在里面——悬停时于是只有上半截变深。关掉
          // 这块局部遮罩，整卡的悬停提示交给 .lg-kb-card::before。
          "&:hover .MuiCardActionArea-focusHighlight": { opacity: 0 },
          // 键盘焦点仍要可见，不能被上面的悬停规则一起吃掉。
          "&.Mui-focusVisible .MuiCardActionArea-focusHighlight": {
            opacity: (t) => t.palette.action.focusOpacity,
          },
        }}
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
    </GlassPanel>
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
