import { useEffect, useRef, useState } from "react";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import Divider from "@mui/material/Divider";
import FormControlLabel from "@mui/material/FormControlLabel";
import IconButton from "@mui/material/IconButton";
import InputAdornment from "@mui/material/InputAdornment";
import MenuItem from "@mui/material/MenuItem";
import { GlassPanel, useGlassLook } from "../ui/glass";
import Skeleton from "@mui/material/Skeleton";
import Stack from "@mui/material/Stack";
import Switch from "@mui/material/Switch";
import Tab from "@mui/material/Tab";
import Tabs from "@mui/material/Tabs";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";
import VisibilityIcon from "@mui/icons-material/Visibility";
import VisibilityOffIcon from "@mui/icons-material/VisibilityOff";

import { api } from "../lib/api";
import { CONTENT_MAX } from "../lib/layout";
import { confirmDialog } from "../lib/dialog";
import { fmtMs, fmtTime } from "../lib/format";
import { setCrumbs, track } from "../lib/store";
import type { Health, ModelsConfig } from "../lib/types";
import DataPanel from "../ui/DataPanel";
import KeysPanel from "../ui/KeysPanel";
import { toast } from "../ui/toast";

const TABS = [
  { key: "models", label: "模型", href: "#/settings/models" },
  { key: "keys", label: "密钥", href: "#/settings/keys" },
  { key: "health", label: "健康", href: "#/settings/health" },
  { key: "data", label: "存储", href: "#/settings/data" },
] as const;

type TabKey = (typeof TABS)[number]["key"];

/** 子页清单只在这里出现一次：任何别处再抄一份都会漂。 */
export default function Settings({ tab }: { tab: string }): React.ReactElement {
  const active: TabKey = TABS.some((t) => t.key === tab) ? (tab as TabKey) : "models";

  useEffect(() => {
    setCrumbs([{ label: "设置" }]);
  }, []);

  return (
    <Box sx={{ p: { xs: 2, md: 3 }, maxWidth: CONTENT_MAX, mx: "auto" }}>
      <Typography variant="h5" sx={{ fontWeight: 500 }}>设置</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5, mb: 2 }}>
        模型热切换、访问密钥、运行状况与存储占用。
      </Typography>
      <Tabs
        value={active}
        aria-label="设置分区"
        sx={{ mb: 3, borderBottom: 1, borderColor: "divider" }}
      >
        {TABS.map((t) => (
          <Tab key={t.key} value={t.key} label={t.label} component="a" href={t.href} />
        ))}
      </Tabs>

      {active === "models" && <ModelsPanel />}
      {active === "keys" && <KeysPanel />}
      {active === "health" && <HealthPanel />}
      {active === "data" && <DataPanel />}
    </Box>
  );
}

// ---- 模型 -----------------------------------------------------------------

type ModelKind = "embed" | "llm" | "rerank";
type TestKind = "embedding" | "llm" | "rerank";

function ModelsPanel(): React.ReactElement {
  const look = useGlassLook("card");
  const [models, setModels] = useState<ModelsConfig | null>(null);
  const [savedSnapshot, setSavedSnapshot] = useState("");
  const [origDim, setOrigDim] = useState(0);
  const [phase, setPhase] = useState<"initial" | "ready" | "failed">("initial");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState<Record<string, boolean>>({});
  const [results, setResults] = useState<Record<string, Record<string, unknown>>>({});
  const [revealed, setRevealed] = useState<Record<string, boolean>>({});
  const loadSeq = useRef(0);

  async function load(): Promise<void> {
    const seq = ++loadSeq.current;
    try {
      const m = await track(api<ModelsConfig>("/api/models"));
      if (seq !== loadSeq.current) return;
      setModels(m);
      setOrigDim(m.embed.dim);
      setSavedSnapshot(JSON.stringify(m));
      setPhase("ready");
      setError("");
    } catch (e) {
      if (seq !== loadSeq.current) return;
      setError(e instanceof Error ? e.message : String(e));
      setPhase((p) => (p === "ready" ? "ready" : "failed"));
    }
  }

  useEffect(() => { void load(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const dirty = !!models && JSON.stringify(models) !== savedSnapshot;

  function patch(kind: ModelKind, key: string, value: string | number | boolean): void {
    setModels((m) => (m ? { ...m, [kind]: { ...m[kind], [key]: value } } : m));
  }

  async function save(): Promise<void> {
    if (!models) return;
    if (models.embed.dim !== origDim) {
      const ok = await confirmDialog({
        title: "变更向量维度？",
        body: `向量维度 ${origDim} → ${models.embed.dim}。现有数据的向量宽度与新配置`
          + "不一致，之后所有入库与分块编辑都会失败；重建索引无法修复，"
          + "只能改回原维度或换新的数据目录重新入库。",
        confirmLabel: "仍然变更",
        destructive: true,
      });
      if (!ok) {
        patch("embed", "dim", origDim);
        return;
      }
    }
    setSaving(true);
    try {
      // 只提交可编辑的三段；retrieve/features 是只读回显，回传只会
      // 制造“这些值也能改”的错觉（后端本来也会忽略）。
      const r = await api<{ warnings?: string[]; persisted?: boolean }>(
        "/api/models", {
          method: "PUT",
          body: { embed: models.embed, llm: models.llm, rerank: models.rerank },
        });
      toast(r.persisted === false ? "已热切换（未落盘）" : "已保存并热切换");
      for (const w of r.warnings ?? []) toast(w, "err");
      await load();
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), "err");
    } finally {
      setSaving(false);
    }
  }

  async function test(kind: TestKind): Promise<void> {
    setTesting((t) => ({ ...t, [kind]: true }));
    try {
      const r = await api<Record<string, Record<string, unknown>>>(
        "/api/models/test", { method: "POST", body: { kind } });
      setResults((prev) => ({
        ...prev,
        [kind]: r[kind] ?? { ok: false, error: "后端未返回该类别的结果" },
      }));
    } catch (e) {
      setResults((prev) => ({
        ...prev,
        [kind]: { ok: false, error: e instanceof Error ? e.message : String(e) },
      }));
    } finally {
      setTesting((t) => ({ ...t, [kind]: false }));
    }
  }

  if (phase === "initial") {
    return (
      <Stack spacing={2}>
        <Skeleton variant="rounded" height={260} />
        <Skeleton variant="rounded" height={260} />
      </Stack>
    );
  }
  if (phase === "failed" || !models) {
    return (
      <Alert severity="error" action={
        <Button color="inherit" size="small" onClick={() => void load()}>重试</Button>
      }>
        加载模型配置失败：{error}
      </Alert>
    );
  }

  const keyField = (kind: ModelKind, label: string): React.ReactElement => (
    <TextField
      label={label}
      type={revealed[kind] ? "text" : "password"}
      fullWidth autoComplete="off"
      value={models[kind].api_key}
      helperText="显示 *** 表示已保存；保持原样即不修改。"
      onChange={(e) => patch(kind, "api_key", e.target.value)}
      slotProps={{
        input: {
          endAdornment: (
            <InputAdornment position="end">
              <IconButton
                size="small"
                aria-label={revealed[kind] ? "隐藏密钥" : "显示密钥"}
                onClick={() => setRevealed((r) => ({ ...r, [kind]: !r[kind] }))}
              >
                {revealed[kind] ? <VisibilityOffIcon fontSize="small" /> : <VisibilityIcon fontSize="small" />}
              </IconButton>
            </InputAdornment>
          ),
        },
      }}
    />
  );

  const providerSelect = (
    kind: ModelKind, options: Array<[string, string]>,
  ): React.ReactElement => (
    <TextField
      select label="提供方" fullWidth
      value={models[kind].provider}
      onChange={(e) => patch(kind, "provider", e.target.value)}
    >
      {options.map(([v, label]) => <MenuItem key={v} value={v}>{label}</MenuItem>)}
    </TextField>
  );

  return (
    <Box component="form" onSubmit={(e) => { e.preventDefault(); void save(); }}>
      <Box
        sx={{
          display: "grid", gap: 2,
          gridTemplateColumns: { xs: "1fr", lg: "repeat(2, minmax(0, 1fr))" },
        }}
      >
        <GlassPanel radius={16} look={look} sx={{ p: 3 }}>
          <Typography variant="h6" sx={{ fontSize: 16 }}>Embedding</Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
            决定向量维度与语义召回质量。
          </Typography>
          <Stack spacing={2}>
            {providerSelect("embed", [
              ["custom", "custom（OpenAI 兼容）"],
              ["openai", "openai"], ["ollama", "ollama"], ["huggingface", "huggingface"],
            ])}
            <TextField label="模型" fullWidth value={models.embed.model}
              onChange={(e) => patch("embed", "model", e.target.value)} />
            <TextField label="base_url" fullWidth value={models.embed.base_url}
              onChange={(e) => patch("embed", "base_url", e.target.value)} />
            {keyField("embed", "API Key")}
            <TextField
              label="向量维度" type="number" fullWidth value={models.embed.dim}
              onChange={(e) => patch("embed", "dim", Number(e.target.value))}
              helperText="危险：决定索引宽度。改动会让现有数据写入失败，且重建索引无法修复。"
            />
          </Stack>
          <Divider sx={{ my: 2 }} />
          <Stack direction="row" spacing={2} alignItems="flex-start">
            <Button size="small" variant="outlined" disabled={!!testing.embedding}
              onClick={() => void test("embedding")}>
              {testing.embedding ? "测试中…" : "测试连通性"}
            </Button>
            <TestResult result={results.embedding} />
          </Stack>
        </GlassPanel>

        <GlassPanel radius={16} look={look} sx={{ p: 3 }}>
          <Typography variant="h6" sx={{ fontSize: 16 }}>LLM</Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
            用于问答生成，可热切换。
          </Typography>
          <Stack spacing={2}>
            {providerSelect("llm", [
              ["custom", "custom（OpenAI 兼容）"],
              ["openai", "openai"], ["ollama", "ollama"],
            ])}
            <TextField label="模型" fullWidth value={models.llm.model}
              onChange={(e) => patch("llm", "model", e.target.value)} />
            <TextField label="base_url" fullWidth value={models.llm.base_url}
              onChange={(e) => patch("llm", "base_url", e.target.value)} />
            {keyField("llm", "API Key")}
          </Stack>
          <Divider sx={{ my: 2 }} />
          <Stack direction="row" spacing={2} alignItems="flex-start">
            <Button size="small" variant="outlined" disabled={!!testing.llm}
              onClick={() => void test("llm")}>
              {testing.llm ? "测试中…" : "测试连通性"}
            </Button>
            <TestResult result={results.llm} />
          </Stack>
        </GlassPanel>

        <GlassPanel radius={16} look={look} style={{ gridColumn: "1 / -1" }} sx={{ p: 3 }}>
          <Typography variant="h6" sx={{ fontSize: 16 }}>Rerank</Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
            对候选分块重排序；未启用时按融合分数排序。
          </Typography>
          <Stack direction={{ xs: "column", md: "row" }} spacing={2}>
            <FormControlLabel
              control={<Switch checked={models.rerank.enabled}
                onChange={(e) => patch("rerank", "enabled", e.target.checked)} />}
              label="启用"
              sx={{ minWidth: 110 }}
            />
            <TextField select label="提供方" value={models.rerank.provider}
              onChange={(e) => patch("rerank", "provider", e.target.value)}
              sx={{ minWidth: 220 }}>
              <MenuItem value="none">none（不精排）</MenuItem>
              <MenuItem value="api">api（HTTP）</MenuItem>
              <MenuItem value="cross-encoder">cross-encoder（本地）</MenuItem>
            </TextField>
            <TextField label="模型" value={models.rerank.model} sx={{ flex: 1, minWidth: 180 }}
              onChange={(e) => patch("rerank", "model", e.target.value)} />
            <TextField label="base_url" value={models.rerank.base_url} sx={{ flex: 1, minWidth: 180 }}
              onChange={(e) => patch("rerank", "base_url", e.target.value)} />
            <Box sx={{ minWidth: 220, flex: 1 }}>{keyField("rerank", "API Key")}</Box>
          </Stack>
          <Divider sx={{ my: 2 }} />
          <Stack direction="row" spacing={2} alignItems="flex-start">
            <Button size="small" variant="outlined" disabled={!!testing.rerank}
              onClick={() => void test("rerank")}>
              {testing.rerank ? "测试中…" : "测试连通性"}
            </Button>
            <TestResult result={results.rerank} />
          </Stack>
        </GlassPanel>
      </Box>

      <GlassPanel
        radius={16} look={look}
        style={{ position: "sticky", bottom: 0, zIndex: 5, marginTop: 16 }}
        sx={{
          p: 2, display: "flex", gap: 2,
          alignItems: "center", flexWrap: "wrap",
        }}
      >
        <Typography variant="body2" color="text.secondary" sx={{ flex: 1, minWidth: 200 }}>
          保存后立即热切换并写入 data/config.toml。<strong>向量维度变更会导致写入失效</strong>，需谨慎操作。
        </Typography>
        {dirty && (
          <Typography variant="body2" color="primary" role="status">有未保存的改动</Typography>
        )}
        {dirty && (
          <Button disabled={saving} onClick={() => void load()}>放弃改动</Button>
        )}
        <Button type="submit" variant="contained" disabled={!dirty || saving}>
          {saving ? "保存中…" : "保存并热切换"}
        </Button>
      </GlassPanel>
    </Box>
  );
}

/** 连通性结论：先给"通/不通"，再列延迟、维度、排序是否正确。 */
function TestResult({ result }: { result?: Record<string, unknown> }): React.ReactElement | null {
  if (!result) return null;
  const ok = result.ok === true;
  return (
    <Box sx={{ flex: 1, minWidth: 0 }} role="status">
      <Alert severity={ok ? "success" : "error"} variant="outlined" sx={{ py: 0.25 }}>
        <Typography variant="body2" sx={{ fontWeight: 500 }}>
          {ok ? "连通" : "不通"}
          {result.latency_ms !== undefined ? ` · 延迟 ${fmtMs(Number(result.latency_ms))}` : ""}
        </Typography>
        {!ok && (
          <Typography variant="caption" sx={{ display: "block", wordBreak: "break-word" }}>
            {String(result.error ?? "未知错误")}
          </Typography>
        )}
        {ok && result.skipped === true && (
          <Typography variant="caption">{String(result.reason ?? "已跳过")}</Typography>
        )}
        {ok && result.skipped !== true && (
          <Typography variant="caption" color="text.secondary" sx={{ display: "block" }}>
            {[
              result.model !== undefined ? `模型 ${String(result.model)}` : "",
              result.dim !== undefined ? `维度 ${String(result.dim)}` : "",
              result.throughput_texts_per_s !== undefined
                ? `吞吐 ${String(result.throughput_texts_per_s)} 条/秒` : "",
              result.sorted_correctly !== undefined
                ? `排序${result.sorted_correctly === true ? "正确" : "有误"}` : "",
            ].filter(Boolean).join(" · ")}
            {result.sample !== undefined && (
              <Box component="span" sx={{ display: "block", mt: 0.5, fontFamily: "monospace" }}>
                {String(result.sample)}
              </Box>
            )}
          </Typography>
        )}
      </Alert>
    </Box>
  );
}

// ---- 健康 -----------------------------------------------------------------

function HealthPanel(): React.ReactElement {
  const look = useGlassLook("card");
  const [health, setHealth] = useState<Health | null>(null);
  const [phase, setPhase] = useState<"initial" | "ready" | "failed">("initial");
  const [error, setError] = useState("");
  const [acting, setActing] = useState("");
  const loadSeq = useRef(0);

  async function load(fresh = false): Promise<void> {
    const seq = ++loadSeq.current;
    try {
      const h = await api<Health>(`/api/health${fresh ? "?fresh=true" : ""}`);
      if (seq !== loadSeq.current) return;
      setHealth(h);
      setPhase("ready");
      setError("");
    } catch (e) {
      if (seq !== loadSeq.current) return;
      setError(e instanceof Error ? e.message : String(e));
      setPhase((p) => (p === "ready" ? "ready" : "failed"));
    }
  }

  useEffect(() => { void load(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  async function sysAction(path: string, label: string): Promise<void> {
    setActing(path);
    try {
      const r = await api<{ ok: boolean; fixed?: number }>(path, { method: "POST" });
      toast(path.endsWith("/reconcile") ? `已修复 ${r.fixed ?? 0} 处计数` : `${label}完成`);
      await load(true);
    } catch (e) {
      toast(`${label}失败：` + (e instanceof Error ? e.message : String(e)), "err");
    } finally {
      setActing("");
    }
  }

  if (phase === "initial") return <Skeleton variant="rounded" height={320} />;
  if (phase === "failed" || !health) {
    return (
      <Alert severity="error" action={
        <Button color="inherit" size="small" onClick={() => void load(true)}>重试</Button>
      }>
        读取运行状况失败：{error}
      </Alert>
    );
  }

  const checksFailed = health.checks_failed ?? [];
  const ftsUnknown = checksFailed.includes("fts_stale");
  const modelUnknown = checksFailed.includes("embedding_model");

  return (
    <Stack spacing={2}>
      <GlassPanel radius={16} look={look} sx={{ p: 3 }}>
        <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1 }}>
          <Typography variant="h6" sx={{ fontSize: 16 }}>运行状况</Typography>
          <Chip
            size="small" variant="outlined"
            color={health.status === "ok" ? "success" : "warning"}
            label={health.status === "ok" ? "正常" : "需关注"}
          />
        </Stack>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          {health.status === "ok" ? "全部检查通过。" : "存在需要关注的问题。"}
          最近检测 {fmtTime(health.now)}
        </Typography>

        <Box
          sx={{
            display: "grid", gap: 1.5,
            gridTemplateColumns: { xs: "1fr 1fr", sm: "repeat(3, 1fr)" },
          }}
        >
          <Kv label="文档 / 分块" value={`${health.doc_count} / ${health.chunk_count}`} />
          <Kv label="独立分块" value={String(health.standalone_chunks)} />
          <Kv label="孤立分块" value={health.orphan_chunks ? String(health.orphan_chunks) : "无"}
            bad={health.orphan_chunks > 0} />
          <Kv label="计数不一致"
            value={health.count_mismatch.length ? `${health.count_mismatch.length} 个文档` : "无"}
            bad={health.count_mismatch.length > 0} />
          <Kv label="FTS 待重建"
            value={ftsUnknown ? "未知（检查没跑成）" : health.fts_stale_count < 0
              ? "未知" : health.fts_stale_count ? String(health.fts_stale_count) : "无"}
            bad={!ftsUnknown && health.fts_stale_count !== 0} />
          <Kv label="嵌入维度"
            value={`${health.embedding_dim}（表内 ${health.stored_embedding_dim ?? "—"}）`}
            bad={health.dim_mismatch} />
          <Kv label="嵌入模型"
            value={modelUnknown
              ? `${health.embedding_model}（未查到，不能说一致）`
              : health.embedding_model_mismatch
                ? `${health.embedding_model}（与表内不一致）`
                : `${health.embedding_model}（一致）`}
            bad={!modelUnknown && health.embedding_model_mismatch} />
        </Box>

        <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 2, mb: 1 }}>
          索引状态
        </Typography>
        <Stack direction="row" spacing={1} flexWrap="wrap">
          {Object.entries(health.index_state).map(([name, s]) => (
            <Chip
              key={name}
              size="small"
              variant="outlined"
              color={s.indexed ? "success" : "warning"}
              label={`${name}：${s.indexed ? "已建" : `未建（待索引 ${s.unindexed_rows}）`}`}
            />
          ))}
        </Stack>

        <Divider sx={{ my: 2 }} />
        <Stack direction="row" spacing={1} flexWrap="wrap">
          <Button variant="outlined" disabled={acting !== ""}
            onClick={() => void sysAction("/api/reindex", "重建索引")}>
            {acting === "/api/reindex" ? "重建中…" : "重建索引"}
          </Button>
          <Button variant="outlined" disabled={acting !== ""}
            onClick={() => void sysAction("/api/reconcile", "修复计数")}>
            {acting === "/api/reconcile" ? "修复中…" : "修复计数"}
          </Button>
          <Button variant="contained" onClick={() => void load(true)}>重新检测</Button>
        </Stack>
      </GlassPanel>

      <GlassPanel radius={16} look={look} sx={{ p: 2 }}>
        <Box component="details">
          <Box component="summary" sx={{ cursor: "pointer", fontSize: 13, color: "text.secondary" }}>
            原始响应（排障用）
          </Box>
          <Box
            component="pre"
            sx={{
              mt: 1, p: 1.5, bgcolor: "action.hover", borderRadius: 1,
              fontSize: 12, overflow: "auto", maxHeight: 420, m: 0,
            }}
          >
            {JSON.stringify(health, null, 2)}
          </Box>
        </Box>
      </GlassPanel>
    </Stack>
  );
}

function Kv({ label, value, bad }: { label: string; value: string; bad?: boolean }): React.ReactElement {
  return (
    <Box>
      <Typography variant="caption" color="text.secondary">{label}</Typography>
      <Typography variant="body2" color={bad ? "error.main" : "text.primary"}
        sx={{ fontWeight: bad ? 600 : 400, wordBreak: "break-word" }}>
        {value}
      </Typography>
    </Box>
  );
}
