import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Divider from "@mui/material/Divider";
import Drawer from "@mui/material/Drawer";
import FormControlLabel from "@mui/material/FormControlLabel";
import IconButton from "@mui/material/IconButton";
import MenuItem from "@mui/material/MenuItem";
import Stack from "@mui/material/Stack";
import Switch from "@mui/material/Switch";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";
import CloseIcon from "@mui/icons-material/Close";

import { MODE_OPTIONS, PARAMS } from "../lib/searchparams";

export const HINTS: Record<string, string> = {
  kb_id: "留空表示在全部知识库中检索",
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
  kbs: Array<{ kb_id: string; name: string }>;
  serverDefaults: Record<string, string>;
  dirty: number;
  mode: "search" | "chat";
  onChange: (key: string, value: string) => void;
  onReset: () => void;
  onRevert: () => void;
  onApply: () => void;
}

/** 完整条件抽屉：所有 17 个参数仍可访问，但不再占据主界面。 */
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
    <Drawer
      anchor="right"
      open={open}
      onClose={onClose}
      slotProps={{ paper: { sx: { width: { xs: "100%", sm: 470 } } } }}
    >
      <Stack sx={{ height: "100%" }}>
        <Stack direction="row" alignItems="center" spacing={1} sx={{ px: 2.5, py: 2 }}>
          <Box sx={{ flex: 1 }}>
            <Typography variant="subtitle1" sx={{ fontWeight: 600 }}>检索条件</Typography>
            <Typography variant="caption" color={dirty > 0 ? "primary.main" : "text.secondary"}>
              {dirty > 0
                ? `${dirty} 项修改尚未应用`
                : mode === "chat"
                  ? "应用后从下一条消息开始生效。"
                  : "应用后写入地址并重新检索。"}
            </Typography>
          </Box>
          <IconButton aria-label="关闭检索条件" onClick={onClose}>
            <CloseIcon fontSize="small" />
          </IconButton>
        </Stack>
        <Divider />

        <Box sx={{ flex: 1, overflowY: "auto", px: 2.5, py: 2 }}>
          {PARAM_GROUPS.map((group, gi) => (
            <Box key={group.title} sx={{ mb: 3 }}>
              <Typography variant="overline" color="text.secondary" sx={{ letterSpacing: ".12em" }}>
                {group.title}
              </Typography>
              <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 1.5 }}>
                {group.description}
              </Typography>
              <Stack spacing={1.75}>
                {group.keys.map((key) => {
                  const p = PARAMS.find((x) => x.key === key);
                  if (!p) return null;
                  return (
                    <ParamControl
                      key={p.key}
                      p={p}
                      value={draft[p.key] ?? ""}
                      defaultValue={serverDefaults[p.key]}
                      kbs={kbs}
                      onChange={(v) => onChange(p.key, v)}
                    />
                  );
                })}
              </Stack>
              {gi < PARAM_GROUPS.length - 1 && <Divider sx={{ mt: 3 }} />}
            </Box>
          ))}
        </Box>

        <Divider />
        <Stack direction="row" spacing={1} sx={{ px: 2.5, py: 2 }}>
          <Button size="small" onClick={onReset} sx={{ color: "text.secondary" }}>
            恢复默认
          </Button>
          <Box sx={{ flex: 1 }} />
          <Button size="small" onClick={onRevert} disabled={dirty === 0}>
            撤销修改
          </Button>
          <Button variant="contained" size="small" onClick={onApply} disabled={dirty === 0}>
            应用筛选
          </Button>
        </Stack>
      </Stack>
    </Drawer>
  );
}

/** 参数控件：规格表是唯一来源，id 供键盘/测试定位。 */
function ParamControl({
  p, value, defaultValue, kbs, onChange,
}: {
  p: (typeof PARAMS)[number];
  value: string;
  defaultValue?: string;
  kbs: Array<{ kb_id: string; name: string }>;
  onChange: (v: string) => void;
}): React.ReactElement {
  const hint = HINTS[p.key];
  const placeholder = value === "" && defaultValue ? `默认 ${defaultValue}` : undefined;

  if (p.key === "kb_id") {
    return (
      <TextField
        id={p.id}
        select
        size="small"
        fullWidth
        label={p.label}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        helperText={hint}
        slotProps={{
          inputLabel: { shrink: true },
          select: {
            displayEmpty: true,
            renderValue: (current: unknown) => {
              const id = String(current ?? "");
              return id ? (kbs.find((k) => k.kb_id === id)?.name ?? id) : "全部知识库";
            },
          },
        }}
      >
        <MenuItem value="">全部知识库</MenuItem>
        {kbs.map((k) => <MenuItem key={k.kb_id} value={k.kb_id}>{k.name}</MenuItem>)}
      </TextField>
    );
  }
  if (p.key === "mode") {
    return (
      <TextField id={p.id} select size="small" fullWidth label={p.label}
        value={value || "hybrid"} onChange={(e) => onChange(e.target.value)} helperText={hint}>
        {MODE_OPTIONS.map((m) => <MenuItem key={m.value} value={m.value}>{m.label}</MenuItem>)}
      </TextField>
    );
  }
  if (p.key === "group_by_doc") {
    return (
      <TextField id={p.id} select size="small" fullWidth label={p.label}
        value={value || "false"} onChange={(e) => onChange(e.target.value)} helperText={hint}>
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
        helperText={hint}
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
        helperText={hint}
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
        <Typography variant="caption" color="text.secondary" sx={{ display: "block", ml: 0.25 }}>
          {hint}
        </Typography>
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
        helperText={hint}
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
      helperText={hint}
      placeholder={placeholder}
    />
  );
}
