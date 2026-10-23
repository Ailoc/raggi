import { useEffect, useRef, useState } from "react";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import Divider from "@mui/material/Divider";
import FormControlLabel from "@mui/material/FormControlLabel";
import MenuItem from "@mui/material/MenuItem";
import { GlassPanel, useGlassLook } from "../ui/glass";
import Skeleton from "@mui/material/Skeleton";
import Stack from "@mui/material/Stack";
import Switch from "@mui/material/Switch";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";
import DeleteForeverIcon from "@mui/icons-material/DeleteForever";
import DescriptionIcon from "@mui/icons-material/Description";
import LayersIcon from "@mui/icons-material/Layers";
import SaveIcon from "@mui/icons-material/Save";

import { api } from "../lib/api";
import { CONTENT_MAX } from "../lib/layout";
import { fmtTime } from "../lib/format";
import { removeKbDialog } from "../lib/ops";
import { href, go } from "../lib/router";
import { invalidateKbs, kbName, loadKbs, setCrumbs, track, useStore } from "../lib/store";
import { CHUNK_STRATEGIES, strategyLabel } from "../lib/types";
import type { Kb, KbPlan } from "../lib/types";
import { toast } from "../ui/toast";

/** 场景预设：把"文档类型 → 合理起点"写成可点的映射，而不是让用户面对数字发呆。 */
const PRESETS = [
  { key: "manual", label: "技术手册 / 规范", size: 512, ratio: 12.5, strategy: "structure" },
  { key: "faq", label: "FAQ / 短问答", size: 256, ratio: 20, strategy: "recursive" },
  { key: "code", label: "代码 / 表格密集", size: 1024, ratio: 10, strategy: "structure" },
  { key: "prose", label: "长文叙述", size: 600, ratio: 12.5, strategy: "semantic" },
] as const;

/**
 * "这个库算不算自定义方案" = 与系统默认相比，而不是 `plan.own.custom`。
 *
 * 两级模型下每个知识库都持有具体数值，`own.custom` 永远为真。后端
 * `plan_custom` 的定义是与默认值比较（容差 0.05，策略也参与），前端
 * 必须用同一把尺子，否则同一块面板会同时出现「当前生效 512」和
 * 「本知识库自定义」。
 */
export function isCustomPlan(
  own: { chunk_size: number; overlap_ratio: number; strategy?: string },
  global: { chunk_size: number; overlap_ratio: number; strategy?: string },
): boolean {
  return Number(own.chunk_size || 0) !== Number(global.chunk_size || 0)
    || Math.abs(Number(own.overlap_ratio || 0) - Number(global.overlap_ratio || 0)) > 0.05
    || (own.strategy ?? "auto") !== (global.strategy ?? "auto");
}

const baseSnap = (name: string, desc: string): string =>
  JSON.stringify([name.trim(), desc.trim()]);
const planSnap = (inherit: boolean, size: number, ratio: number, strategy: string): string =>
  JSON.stringify([inherit, Number(size || 0), Number(ratio || 0), strategy]);

/**
 * 知识库设置：基本信息 + 分块方案 + 内容概览 + 危险区。
 *
 * 两个表单各自持有独立的已保存快照：保存基本信息**就地更新**，绝不整页
 * 重载——旧版重载会把方案面板里未提交的编辑静默抹掉。
 */
export default function KbSettings({ kbId }: { kbId: string }): React.ReactElement {
  const look = useGlassLook("card");
  const { kbsLoaded } = useStore();
  const [kb, setKb] = useState<Kb | null>(null);
  const [plan, setPlan] = useState<KbPlan | null>(null);
  const [phase, setPhase] = useState<"initial" | "ready" | "failed">("initial");
  const [error, setError] = useState("");

  const [name, setName] = useState("");
  const [desc, setDesc] = useState("");
  const [inherit, setInherit] = useState(true);
  const [size, setSize] = useState(512);
  const [ratio, setRatio] = useState(12.5);
  const [strategy, setStrategy] = useState("auto");

  const [savedBase, setSavedBase] = useState("");
  const [savedPlan, setSavedPlan] = useState("");
  const [savingInfo, setSavingInfo] = useState(false);
  const [savingPlan, setSavingPlan] = useState(false);
  const loadSeq = useRef(0);

  const baseDirty = !!kb && baseSnap(name, desc) !== savedBase;
  const planDirty = !!plan && planSnap(inherit, size, ratio, strategy) !== savedPlan;
  const customPlan = !!plan && isCustomPlan(plan.own, plan.global);

  async function load(): Promise<void> {
    const seq = ++loadSeq.current;
    try {
      const [k, p] = await Promise.all([
        track(api<Kb>(`/api/kbs/${encodeURIComponent(kbId)}`)),
        track(api<KbPlan>(`/api/kbs/${encodeURIComponent(kbId)}/plan`)),
      ]);
      if (seq !== loadSeq.current) return;
      const isDefault = !isCustomPlan(p.own, p.global);
      const nextSize = p.own.chunk_size || p.global.chunk_size;
      const nextRatio = p.own.overlap_ratio || p.global.overlap_ratio;
      const nextStrategy = p.own.strategy || p.global.strategy || "auto";
      setKb(k);
      setPlan(p);
      setName(k.name);
      setDesc(k.description ?? "");
      setInherit(isDefault);
      setSize(nextSize);
      setRatio(nextRatio);
      setStrategy(nextStrategy);
      setSavedBase(baseSnap(k.name, k.description ?? ""));
      setSavedPlan(planSnap(isDefault, nextSize, nextRatio, nextStrategy));
      setPhase("ready");
      setError("");
      setCrumbs([
        { label: "知识库", href: "#/" },
        { label: k.name, href: href("kb", kbId, "docs") },
        { label: "设置" },
      ]);
    } catch (e) {
      if (seq !== loadSeq.current) return;
      setError(e instanceof Error ? e.message : String(e));
      setPhase((p) => (p === "ready" ? "ready" : "failed"));
    }
  }

  useEffect(() => {
    setCrumbs([
      { label: "知识库", href: "#/" },
      { label: kbName(kbId), href: href("kb", kbId, "docs") },
      { label: "设置" },
    ]);
    setPhase("initial");
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kbId, kbsLoaded]);

  async function saveBase(): Promise<void> {
    if (!kb) return;
    const n = name.trim();
    if (!n) { toast("名称不能为空", "err"); return; }
    setSavingInfo(true);
    try {
      await api(`/api/kbs/${encodeURIComponent(kbId)}`, {
        method: "PUT",
        body: { name: n, description: desc.trim() },
      });
      toast("基本信息已保存");
      // 就地更新：整页 load() 会抹掉方案面板里未提交的编辑
      setKb({ ...kb, name: n, description: desc.trim() });
      setSavedBase(baseSnap(n, desc));
      setCrumbs([
        { label: "知识库", href: "#/" },
        { label: n, href: href("kb", kbId, "docs") },
        { label: "设置" },
      ]);
      invalidateKbs();
      void loadKbs();
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), "err");
    } finally {
      setSavingInfo(false);
    }
  }

  async function savePlan(): Promise<void> {
    if (!plan) return;
    const s = Number(size || 0);
    const r = Number(ratio || 0);
    // 后端把 chunk_size=0 解释为「恢复系统默认」。取消勾选后把大小留空
    // 绝不能发出去：用户想自定义，服务端却改回默认，界面还说保存成功。
    if (!inherit && !(s >= 64 && s <= 8192)) {
      toast("分块大小需在 64–8192 之间。留空等于「恢复系统默认」，不是自定义", "err");
      return;
    }
    if (!inherit && !(r >= 0 && r <= 50)) {
      toast("重叠比例需在 0–50% 之间（超过 50% 收益递减且块数翻倍）", "err");
      return;
    }
    setSavingPlan(true);
    try {
      await api(`/api/kbs/${encodeURIComponent(kbId)}/plan`, {
        method: "PUT",
        body: {
          chunk_size: inherit ? 0 : s,
          overlap_ratio: inherit ? 0 : r,
          strategy: inherit ? "" : strategy,
        },
      });
      toast(inherit ? "已恢复系统默认方案" : "分块方案已保存");
      // 只重取本面板的方案接口：整页 load() 会冲掉基本信息里的未提交编辑。
      // effective 由后端计算（含 overlap_chars / source），前端不能自己拼。
      let fresh: KbPlan | null = null;
      try {
        fresh = await api<KbPlan>(`/api/kbs/${encodeURIComponent(kbId)}/plan`);
      } catch {
        // 重取失败：至少让本面板的口径与刚提交的值一致
        const nextOwn = {
          ...plan.own,
          custom: !inherit,
          chunk_size: inherit ? plan.global.chunk_size : s,
          overlap_ratio: inherit ? plan.global.overlap_ratio : r,
          strategy: inherit ? (plan.global.strategy || "auto") : strategy,
        };
        fresh = {
          ...plan,
          own: nextOwn,
          effective: {
            ...plan.effective,
            chunk_size: nextOwn.chunk_size,
            overlap_ratio: nextOwn.overlap_ratio,
            strategy: nextOwn.strategy,
            source: inherit ? "global" : "kb",
          },
        };
      }
      const isDefault = !isCustomPlan(fresh.own, fresh.global);
      const nextSize = fresh.own.chunk_size || fresh.global.chunk_size;
      const nextRatio = fresh.own.overlap_ratio || fresh.global.overlap_ratio;
      const nextStrategy = fresh.own.strategy || fresh.global.strategy || "auto";
      setPlan(fresh);
      setInherit(isDefault);
      setSize(nextSize);
      setRatio(nextRatio);
      setStrategy(nextStrategy);
      setSavedPlan(planSnap(isDefault, nextSize, nextRatio, nextStrategy));
      setKb((k) => (k ? { ...k, plan_custom: !isDefault } : k));
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), "err");
    } finally {
      setSavingPlan(false);
    }
  }

  async function removeKb(): Promise<void> {
    if (!kb) return;
    const ok = await removeKbDialog(kbId, kb.name, kb.doc_count ?? 0, kb.chunk_count ?? 0);
    if (ok) go("#/");
  }

  if (phase === "initial") {
    return (
      <Box sx={{ p: { xs: 2, md: 3 }, maxWidth: CONTENT_MAX, mx: "auto" }}>
        <Skeleton variant="rounded" height={200} sx={{ mb: 2 }} />
        <Skeleton variant="rounded" height={260} />
      </Box>
    );
  }

  if (phase === "failed" || !kb || !plan) {
    return (
      <Box sx={{ p: { xs: 2, md: 3 }, maxWidth: CONTENT_MAX, mx: "auto" }}>
        <Alert
          severity="error"
          action={<Button color="inherit" size="small" onClick={() => void load()}>重试</Button>}
        >
          加载知识库设置失败{error ? `：${error}` : ""}
        </Alert>
      </Box>
    );
  }

  return (
    <Box sx={{ p: { xs: 2, md: 3 }, maxWidth: CONTENT_MAX, mx: "auto" }}>
      <Typography variant="h5" sx={{ fontWeight: 500, mb: 0.5 }}>{kb.name}</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>
        {kb.doc_count} 个文档 · {kb.chunk_count} 个分块 · 更新于 {fmtTime(kb.updated_at)}
      </Typography>

      <GlassPanel radius={16} look={look} sx={{ p: 3, mb: 2 }}>
        <Typography variant="h6" sx={{ fontSize: 16 }}>基本信息</Typography>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          名称与描述。描述只给人看，不参与检索。
        </Typography>
        <Stack spacing={2}>
          <TextField
            label="名称" value={name} fullWidth required
            onChange={(e) => setName(e.target.value)}
          />
          <TextField
            label="描述" value={desc} fullWidth multiline minRows={2}
            placeholder="这个库收录什么内容"
            onChange={(e) => setDesc(e.target.value)}
          />
        </Stack>
        <Stack direction="row" spacing={1} alignItems="center" justifyContent="flex-end" sx={{ mt: 2 }}>
          <Typography variant="body2" color="primary" role="status" sx={{ mr: "auto" }}>
            {baseDirty ? "有未保存的改动" : ""}
          </Typography>
          <Button
            disabled={!baseDirty || savingInfo}
            onClick={() => { setName(kb.name); setDesc(kb.description ?? ""); }}
          >
            放弃改动
          </Button>
          <Button
            variant="contained" startIcon={<SaveIcon />}
            disabled={!baseDirty || savingInfo}
            onClick={() => void saveBase()}
          >
            {savingInfo ? "保存中…" : "保存基本信息"}
          </Button>
        </Stack>
      </GlassPanel>

      <GlassPanel radius={16} look={look} sx={{ p: 3, mb: 2 }}>
        <Typography variant="h6" sx={{ fontSize: 16 }}>分块方案</Typography>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          决定文档入库时怎么切。单个文档还可再覆盖一次。
        </Typography>

        <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" sx={{ mb: 1 }}>
          <Typography variant="body2">
            当前生效 <strong>{plan.effective.chunk_size} 字符 · 重叠 {plan.effective.overlap_ratio}% ·{" "}
              {strategyLabel(plan.effective.strategy)}</strong>
          </Typography>
          <Chip
            size="small"
            variant="outlined"
            color={customPlan ? "primary" : "default"}
            label={customPlan ? "本知识库自定义" : "与系统默认一致"}
          />
        </Stack>

        <FormControlLabel
          control={<Switch checked={inherit} onChange={(e) => setInherit(e.target.checked)} />}
          label={`用系统默认值（${plan.global.chunk_size} 字符 / ${plan.global.overlap_ratio}% / ${strategyLabel(plan.global.strategy)}）`}
        />
        <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 2 }}>
          保存时把系统默认抄到本库；日后修改系统默认不会自动影响已建好的库。
        </Typography>

        {!inherit && (
          <>
            <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap sx={{ mb: 2, alignItems: "center" }}>
              <Typography variant="body2" color="text.secondary">按场景填入推荐值：</Typography>
              {PRESETS.map((p) => (
                <Chip
                  key={p.key}
                  size="small"
                  variant="outlined"
                  clickable
                  label={p.label}
                  title={`${p.size} 字符 / 重叠 ${p.ratio}% / ${strategyLabel(p.strategy)}`}
                  onClick={() => {
                    setSize(p.size);
                    setRatio(p.ratio);
                    setStrategy(p.strategy);
                  }}
                />
              ))}
            </Stack>

            <Stack direction={{ xs: "column", sm: "row" }} spacing={2}>
              <TextField
                select label="分块策略" value={strategy} sx={{ minWidth: 200 }}
                onChange={(e) => setStrategy(e.target.value)}
                helperText={CHUNK_STRATEGIES.find((c) => c.value === strategy)?.hint ?? ""}
              >
                {CHUNK_STRATEGIES.map((s) => (
                  <MenuItem key={s.value} value={s.value} title={s.hint}>{s.label}</MenuItem>
                ))}
              </TextField>
              <TextField
                label="分块大小（字符）" type="number" value={size}
                onChange={(e) => setSize(Number(e.target.value))}
                slotProps={{ htmlInput: { min: 64, max: 8192 } }}
                helperText="64–8192。中文建议 300–600。"
                sx={{ width: 200 }}
              />
              <TextField
                label="重叠比例（%）" type="number" value={ratio}
                onChange={(e) => setRatio(Number(e.target.value))}
                slotProps={{ htmlInput: { min: 0, max: 50 } }}
                helperText="0–50。业界常用 10–20%。"
                sx={{ width: 180 }}
              />
            </Stack>
          </>
        )}
        <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 1 }}>
          保存只影响之后入库的文档；已有文档要在文档行上点「重新切分」才会按新方案重切。
        </Typography>

        <Stack direction="row" spacing={1} alignItems="center" justifyContent="flex-end" sx={{ mt: 2 }}>
          <Typography variant="body2" color="primary" role="status" sx={{ mr: "auto" }}>
            {planDirty ? "方案有未保存的改动" : ""}
          </Typography>
          <Button
            disabled={!planDirty || savingPlan}
            onClick={() => {
              setInherit(!isCustomPlan(plan.own, plan.global));
              setSize(plan.own.chunk_size || plan.global.chunk_size);
              setRatio(plan.own.overlap_ratio || plan.global.overlap_ratio);
              setStrategy(plan.own.strategy || plan.global.strategy || "auto");
            }}
          >
            放弃改动
          </Button>
          <Button
            variant="contained" startIcon={<SaveIcon />}
            disabled={!planDirty || savingPlan}
            onClick={() => void savePlan()}
          >
            {savingPlan ? "保存中…" : "保存方案"}
          </Button>
        </Stack>
      </GlassPanel>

      <GlassPanel radius={16} look={look} sx={{ p: 3, mb: 2 }}>
        <Typography variant="h6" sx={{ fontSize: 16 }}>内容概览</Typography>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          只读统计，用于核对入库结果。
        </Typography>
        <Box
          sx={{
            display: "grid", gap: 1.5, mb: 2,
            gridTemplateColumns: { xs: "1fr 1fr", sm: "repeat(3, 1fr)" },
          }}
        >
          {[
            ["文档", String(kb.doc_count)],
            ["分块", String(kb.chunk_count)],
            ["创建时间", fmtTime(kb.created_at)],
            ["更新时间", fmtTime(kb.updated_at)],
            ["知识库 ID", kb.kb_id],
          ].map(([label, value]) => (
            <Box key={label}>
              <Typography variant="caption" color="text.secondary">{label}</Typography>
              <Typography variant="body2" sx={{ wordBreak: "break-all" }}>{value}</Typography>
            </Box>
          ))}
        </Box>
        <Stack direction="row" spacing={1}>
          <Button component="a" href={href("kb", kbId, "docs")} startIcon={<DescriptionIcon />}>
            查看文档
          </Button>
          <Button
            component="a"
            href={`${href("kb", kbId, "chunks")}?scope=standalone`}
            startIcon={<LayersIcon />}
          >
            独立分块
          </Button>
        </Stack>
      </GlassPanel>

      <GlassPanel radius={16} look={look} sx={{ p: 3, borderColor: "error.main" }}>
        <Typography variant="h6" sx={{ fontSize: 16, color: "error.main" }}>危险操作</Typography>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          删除知识库会连同它的 {kb.doc_count} 篇文档、{kb.chunk_count} 个分块与留档原文一起删除，不可撤销。
        </Typography>
        <Divider sx={{ mb: 2 }} />
        <Button
          variant="contained" color="error" startIcon={<DeleteForeverIcon />}
          onClick={() => void removeKb()}
        >
          删除本知识库
        </Button>
      </GlassPanel>
    </Box>
  );
}
