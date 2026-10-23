import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import Divider from "@mui/material/Divider";
import FormControlLabel from "@mui/material/FormControlLabel";
import IconButton from "@mui/material/IconButton";
import MenuItem from "@mui/material/MenuItem";
import Switch from "@mui/material/Switch";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";
import CloseIcon from "@mui/icons-material/Close";

import { MODE_OPTIONS, PARAMS } from "../lib/searchparams";
import type { Kb } from "../lib/types";
import { GlassFilterPaper } from "./glass";
import KbMultiSelect from "./KbMultiSelect";

export const HINTS: Record<string, string> = {
  kb_id: "可多选并按名称搜索；一个都不选表示在全部知识库中检索",
  mode: "混合模式通常最稳；向量与全文适合分别验证召回",
  top_k: "展示多少条证据，不是候选池大小",
  group_by_doc: "每篇文档只保留分数最高的一块",
  origin: "解析入库的块，或手工维护的块",
  parser_engine: "例如 docling、pymupdf4llm、native",
  mime: "例如 application/pdf、text/markdown",
  include_disabled: "已停用的块默认不参与检索",
  score_threshold: "低于此值的命中会被丢弃；口径见结果摘要",
  window: "每块命中前后各带回多少块上下文",
  snippet_chars: "证据片段的最大字符数",
  highlight: "关闭后仍会返回片段，只是没有命中高亮",
  candidate_k: "召回候选池大小，越大越全也越慢",
  nprobes: "向量索引探测分区数；当前索引下收益有限",
  refine_factor: "量化索引的重排倍数，直接影响召回质量",
  k_rrf: "混合检索的 RRF 融合常数，一般无需修改",
  rerank: "覆盖服务端精排开关；仅在需要对照时使用",
};

export const PARAM_GROUPS = [
  {
    title: "范围",
    description: "先把查找空间收窄到真正相关的语料。",
    keys: ["kb_id", "origin", "parser_engine", "mime", "include_disabled"],
  },
  {
    title: "结果形态",
    description: "决定返回多少条、如何折叠以及相关性下限。",
    keys: ["mode", "top_k", "group_by_doc", "score_threshold"],
  },
  {
    title: "召回与排序",
    description: "调优旋钮。不了解含义时保持默认即可。",
    keys: ["candidate_k", "nprobes", "refine_factor", "k_rrf", "rerank"],
  },
  {
    title: "证据呈现",
    description: "只影响片段与上下文，不改变命中排序。",
    keys: ["window", "snippet_chars", "highlight"],
  },
] as const;

export interface SearchFiltersProps {
  open: boolean;
  onClose: () => void;
  draft: Record<string, string>;
  kbs: Kb[];
  serverDefaults: Record<string, string>;
  dirty: number;
  mode: "search" | "chat";
  onChange: (key: string, value: string) => void;
  onReset: () => void;
  onRevert: () => void;
  onApply: () => void;
}

/**
 * 完整条件面板：所有 17 个参数仍可访问，但不再占据主界面、也不再从
 * 侧边推入。分类纵向排列、组内多列控件：窄屏一行一个，宽屏一行三个，
 * 比 2×2 卡片更紧凑，也不会出现“左边一大块、右边一小块”的空白。
 */
export default function SearchFilters({
  open,
  onClose,
  draft,
  kbs,
  serverDefaults,
  dirty,
  mode,
  onChange,
  onReset,
  onRevert,
  onApply,
}: SearchFiltersProps): React.ReactElement {
  return (
    <Dialog
      open={open}
      onClose={onClose}
      fullWidth
      maxWidth={false}
      PaperComponent={GlassFilterPaper}
      aria-labelledby="filters-title"
      slotProps={{
        backdrop: {
          sx: (t) => ({
            backgroundColor: t.palette.mode === "dark"
              ? "rgba(2, 10, 12, 0.55)"
              : "rgba(15, 23, 42, 0.18)",
            backdropFilter: "blur(3px)",
          }),
        },
      }}
    >
      <DialogTitle
        id="filters-title"
        sx={{
          display: "flex",
          alignItems: "flex-start",
          gap: 2,
          px: { xs: 2, sm: 3 },
          pt: { xs: 1.75, sm: 2 },
          pb: 1,
        }}
      >
        <Box sx={{ flex: 1, minWidth: 0 }}>
          <Typography
            component="div"
            variant="h6"
            sx={{ fontWeight: 600, fontSize: 18, lineHeight: 1.3 }}
          >
            检索条件
          </Typography>
          <Typography
            variant="caption"
            color={dirty > 0 ? "primary.main" : "text.secondary"}
            sx={{ display: "block", mt: 0.5 }}
          >
            {dirty > 0
              ? `${dirty} 项修改尚未应用`
              : mode === "chat"
                ? "应用后从下一条消息开始生效。"
                : "应用后立即重新检索。"}
          </Typography>
        </Box>
        <IconButton aria-label="关闭检索条件" onClick={onClose} size="small">
          <CloseIcon fontSize="small" />
        </IconButton>
      </DialogTitle>
      <Divider />

      <DialogContent sx={{ px: { xs: 2, sm: 3 }, py: 1.5 }}>
        {PARAM_GROUPS.map((group, gi) => (
          <Box key={group.title}>
            <Box
              sx={{
                display: "flex",
                alignItems: "baseline",
                gap: 1.5,
                flexWrap: "wrap",
                mb: 1,
              }}
            >
              <Typography variant="subtitle2" sx={{ fontWeight: 600, fontSize: 14 }}>
                {group.title}
              </Typography>
              <Typography variant="caption" color="text.secondary">
                {group.description}
              </Typography>
            </Box>
            <Box
              sx={{
                display: "grid",
                gridTemplateColumns: {
                  xs: "1fr",
                  sm: "repeat(2, minmax(0, 1fr))",
                  md: "repeat(3, minmax(0, 1fr))",
                },
                gap: 1.25,
                alignItems: "start",
              }}
            >
              {group.keys.map((key) => {
                const p = PARAMS.find((x) => x.key === key);
                if (!p) return null;
                return (
                  <Box
                    key={p.key}
                    title={HINTS[p.key]}
                    sx={{
                      gridColumn: p.key === "kb_id"
                        ? { xs: "1 / -1", sm: "1 / -1", md: "span 2" }
                        : p.key === "rerank"
                          ? { md: "span 2" }
                          : undefined,
                    }}
                  >
                    <ParamControl
                      p={p}
                      compact
                      value={draft[p.key] ?? ""}
                      defaultValue={serverDefaults[p.key]}
                      kbs={kbs}
                      onChange={(v) => onChange(p.key, v)}
                    />
                    <Typography component="span" className="sr-only">
                      {HINTS[p.key]}
                    </Typography>
                  </Box>
                );
              })}
            </Box>
            {gi < PARAM_GROUPS.length - 1 && <Divider sx={{ my: 1.5 }} />}
          </Box>
        ))}
      </DialogContent>

      <Divider />
      <DialogActions
        disableSpacing
        sx={{ px: { xs: 1.5, sm: 3 }, py: 1.5, gap: { xs: 0.5, sm: 1 } }}
      >
        <Button
          size="small"
          onClick={onReset}
          sx={{
            color: "text.secondary", whiteSpace: "nowrap",
            flexShrink: 0, px: { xs: 1.25, sm: 2 },
          }}
        >
          恢复默认
        </Button>
        <Box sx={{ flex: 1 }} />
        <Button
          size="small"
          onClick={onRevert}
          disabled={dirty === 0}
          sx={{ whiteSpace: "nowrap", flexShrink: 0, px: { xs: 1.25, sm: 2 } }}
        >
          撤销修改
        </Button>
        <Button
          variant="contained"
          size="small"
          onClick={onApply}
          disabled={dirty === 0}
          sx={{ whiteSpace: "nowrap", flexShrink: 0, px: { xs: 1.25, sm: 2 } }}
        >
          应用筛选
        </Button>
      </DialogActions>
    </Dialog>
  );
}

/** 参数控件：规格表是唯一来源，id 供键盘/测试定位。 */
function ParamControl({
  p, compact = false, value, defaultValue, kbs, onChange,
}: {
  p: (typeof PARAMS)[number];
  compact?: boolean;
  value: string;
  defaultValue?: string;
  kbs: Kb[];
  onChange: (v: string) => void;
}): React.ReactElement {
  const hint = HINTS[p.key];
  const helper = compact ? undefined : hint;
  const placeholder = value === "" && defaultValue ? `默认 ${defaultValue}` : undefined;

  if (p.key === "kb_id") {
    return (
      <Box>
        <KbMultiSelect
          id={p.id}
          value={value}
          onChange={onChange}
          kbs={kbs}
          variant="field"
        />
        {!compact && (
          <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 0.5 }}>
            {hint}
          </Typography>
        )}
      </Box>
    );
  }
  if (p.key === "mode") {
    return (
      <TextField id={p.id} select size="small" fullWidth label={p.label}
        value={value || "hybrid"} onChange={(e) => onChange(e.target.value)} helperText={helper}>
        {MODE_OPTIONS.map((m) => <MenuItem key={m.value} value={m.value}>{m.label}</MenuItem>)}
      </TextField>
    );
  }
  if (p.key === "group_by_doc") {
    return (
      <TextField id={p.id} select size="small" fullWidth label={p.label}
        value={value || "false"} onChange={(e) => onChange(e.target.value)} helperText={helper}>
        <MenuItem value="false">否，保留每篇文档的多块</MenuItem>
        <MenuItem value="true">是，每篇只留最高分</MenuItem>
      </TextField>
    );
  }
  if (p.key === "origin") {
    return (
      <TextField
        id={p.id}
        select
        size="small"
        fullWidth
        label={p.label}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        helperText={helper}
        slotProps={{
          inputLabel: { shrink: true },
          select: {
            displayEmpty: true,
            renderValue: (current: unknown) => (
              current === "parsed" ? "解析入库"
                : current === "manual" ? "手工块"
                : "全部来源"
            ),
          },
        }}
      >
        <MenuItem value="">全部来源</MenuItem>
        <MenuItem value="parsed">解析入库</MenuItem>
        <MenuItem value="manual">手工块</MenuItem>
      </TextField>
    );
  }
  if (p.key === "rerank") {
    return (
      <TextField
        id={p.id}
        select
        size="small"
        fullWidth
        label={p.label}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        helperText={helper}
        slotProps={{
          inputLabel: { shrink: true },
          select: {
            displayEmpty: true,
            renderValue: (current: unknown) => (
              current === "true" ? "强制开启"
                : current === "false" ? "强制关闭"
                : "跟随服务端配置"
            ),
          },
        }}
      >
        <MenuItem value="">跟随服务端配置</MenuItem>
        <MenuItem value="true">强制开启</MenuItem>
        <MenuItem value="false">强制关闭</MenuItem>
      </TextField>
    );
  }
  if (p.kind === "bool") {
    return (
      <Box>
        <FormControlLabel
          control={
            <Switch
              id={p.id}
              checked={value === "true"}
              onChange={(e) => onChange(e.target.checked ? "true" : "false")}
            />
          }
          label={p.label}
        />
        {!compact && (
          <Typography variant="caption" color="text.secondary" sx={{ display: "block", ml: 0.25 }}>
            {hint}
          </Typography>
        )}
      </Box>
    );
  }
  if (p.kind === "num") {
    return (
      <TextField
        id={p.id}
        size="small"
        fullWidth
        type="number"
        label={p.label}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        helperText={helper}
        placeholder={placeholder}
        slotProps={{ htmlInput: { min: 0 } }}
      />
    );
  }
  return (
    <TextField
      id={p.id}
      size="small"
      fullWidth
      label={p.label}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      helperText={helper}
      placeholder={placeholder}
    />
  );
}
