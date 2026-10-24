import { useEffect, useRef, useState } from "react";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import Divider from "@mui/material/Divider";
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
        sx={{
          mb: 3, borderBottom: 1, borderColor: "divider",
          // 360px 下四个分区要全部可见：默认 90px 最小宽度会把「存储」
          // 挤出容器，而这里没有滚动提示。
          "& .MuiTab-root": {
            minWidth: { xs: 0, sm: 90 },
            px: { xs: 1.5, sm: 3 },
          },
        }}
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
type FieldKind = "select" | "text" | "number" | "secret" | "switch";

interface FieldSpec {
  key: string;
  label: string;
  kind: FieldKind;
  hint?: string;
  /** 危险项：提示走警示色，不跟普通说明混成一色 */
  danger?: boolean;
  /** 两列网格里占满整行：总开关这类单独成行的项 */
  full?: boolean;
}

interface ModelCardSpec {
  kind: ModelKind;
  title: string;
  desc: string;
  test: TestKind;
  fields: FieldSpec[];
  /** 这一段的连通性测试到底会做什么：后端真发一次请求，不是空跑。 */
  testNote: string;
  /** 补充说明：什么时候才需要这一段。 */
  tip?: string;
}

const SECRET_HINT = "显示 *** 表示已保存；保持原样即不修改。";

/**
 * 三段配置的字段不同（embed 有 dim、rerank 有 enabled），但**排布必须一样**：
 * 字段写成一张表，由同一个渲染函数逐条竖着铺下去。
 * 每段各写一遍 JSX 的老写法必然漂移——Rerank 那行曾经就是横排的。
 */
const MODEL_CARDS: ModelCardSpec[] = [
  {
    kind: "embed", title: "Embedding", desc: "决定向量维度与语义召回质量。", test: "embedding",
    testNote: "会真的向当前提供方发一次嵌入请求，并回传向量维度与吞吐。",
    fields: [
      { key: "provider", label: "提供方", kind: "select" },
      { key: "model", label: "模型", kind: "text" },
      { key: "base_url", label: "base_url", kind: "text" },
      { key: "api_key", label: "API Key", kind: "secret", hint: SECRET_HINT },
      {
        key: "dim", label: "向量维度", kind: "number", danger: true,
        hint: "危险：决定索引宽度。改动会让现有数据写入失败，且重建索引无法修复。",
      },
    ],
  },
  {
    kind: "llm", title: "LLM", desc: "用于问答生成，可热切换。", test: "llm",
    testNote: "会真的向当前提供方发一次生成请求，并回显回答内容与延迟。",
    fields: [
      { key: "provider", label: "提供方", kind: "select" },
      { key: "model", label: "模型", kind: "text" },
      { key: "base_url", label: "base_url", kind: "text" },
      { key: "api_key", label: "API Key", kind: "secret", hint: SECRET_HINT },
    ],
  },
  {
    kind: "rerank", title: "Rerank", desc: "对候选分块重排序；未启用时按融合分数排序。", test: "rerank",
    testNote: "会真的发一次重排请求，用一组正反例检查相关文档是否排到了前面。",
    tip: "Rerank 只重排召回后的候选，不扩大召回范围——召回本身就偏时，先调嵌入模型。",
    fields: [
      { key: "enabled", label: "启用", kind: "switch", full: true },
      { key: "provider", label: "提供方", kind: "select" },
      { key: "model", label: "模型", kind: "text" },
      { key: "base_url", label: "base_url", kind: "text" },
      { key: "api_key", label: "API Key", kind: "secret", hint: SECRET_HINT },
    ],
  },
];

const PROVIDER_OPTIONS: Record<ModelKind, Array<[string, string]>> = {
  embed: [
    ["custom", "custom（OpenAI 兼容）"],
    ["openai", "openai"], ["ollama", "ollama"], ["huggingface", "huggingface"],
  ],
  llm: [
    ["custom", "custom（OpenAI 兼容）"],
    ["openai", "openai"], ["ollama", "ollama"],
  ],
  rerank: [
    ["none", "none（不精排）"],
    ["api", "api（HTTP）"],
    ["cross-encoder", "cross-encoder（本地）"],
  ],
};

/**
 * 三段配置的字段名不同，读写形状却一样（都是字符串键值）。
 * 统一成一张表，渲染层就不必为每段各写一遍类型体操。
 */
type Section = Record<string, string | number | boolean>;

function sectionOf(cfg: ModelsConfig, kind: ModelKind): Section {
  return cfg[kind] as unknown as Section;
}

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

  const section = (kind: ModelKind): Section => sectionOf(models, kind);

  /** 一个设置项：标签在上、控件在下、说明再下。 */
  const renderField = (kind: ModelKind, spec: FieldSpec): React.ReactElement => {
    const id = `models-${kind}-${spec.key}`;
    const labelId = `${id}-label`;
    const value = section(kind)[spec.key];

    if (spec.kind === "switch") {
      const on = value === true;
      return (
        <SettingField id={id} labelId={labelId} label={spec.label}>
          <Box sx={{ display: "flex", alignItems: "center", gap: 1.5 }}>
            <Switch
              id={id}
              checked={on}
              onChange={(e) => patch(kind, spec.key, e.target.checked)}
              slotProps={{ input: { "aria-label": spec.label } }}
            />
            <Typography variant="body2" color={on ? "success.main" : "text.secondary"}>
              {on ? "已开启：候选分块先精排再返回" : "已关闭：按融合分数排序"}
            </Typography>
          </Box>
        </SettingField>
      );
    }

    if (spec.kind === "select") {
      return (
        <SettingField id={id} labelId={labelId} label={spec.label} hint={spec.hint} danger={spec.danger}>
          <TextField
            id={id}
            select
            fullWidth
            value={String(value ?? "")}
            onChange={(e) => patch(kind, spec.key, e.target.value)}
            slotProps={{
              select: {
                labelId,
                // MUI 会把 select 的 id 拼进 aria-labelledby，屏幕阅读器会把
                // 当前值一起念出来（"提供方 custom（OpenAI 兼容）"）。
                // 这里点名只认可见标签，名字就干净了。
                SelectDisplayProps: {
                  "aria-label": spec.label,
                  "aria-labelledby": labelId,
                },
              },
            }}
          >
            {PROVIDER_OPTIONS[kind].map(([v, text]) => (
              <MenuItem key={v} value={v}>{text}</MenuItem>
            ))}
          </TextField>
        </SettingField>
      );
    }

    if (spec.kind === "secret") {
      return (
        <SettingField id={id} labelId={labelId} label={spec.label} hint={spec.hint} danger={spec.danger}>
          <TextField
            id={id}
            type={revealed[kind] ? "text" : "password"}
            fullWidth
            autoComplete="off"
            value={String(value ?? "")}
            onChange={(e) => patch(kind, spec.key, e.target.value)}
            slotProps={{
              htmlInput: { "aria-label": spec.label },
              input: {
                endAdornment: (
                  <InputAdornment position="end">
                    <IconButton
                      size="small"
                      aria-label={revealed[kind] ? "隐藏密钥" : "显示密钥"}
                      onClick={() => setRevealed((r) => ({ ...r, [kind]: !r[kind] }))}
                    >
                      {revealed[kind]
                        ? <VisibilityOffIcon fontSize="small" />
                        : <VisibilityIcon fontSize="small" />}
                    </IconButton>
                  </InputAdornment>
                ),
              },
            }}
          />
        </SettingField>
      );
    }

    return (
      <SettingField id={id} labelId={labelId} label={spec.label} hint={spec.hint} danger={spec.danger}>
        <TextField
          id={id}
          type={spec.kind === "number" ? "number" : "text"}
          fullWidth
          value={String(value ?? "")}
          onChange={(e) => patch(kind, spec.key, spec.kind === "number" ? Number(e.target.value) : e.target.value)}
          slotProps={{ htmlInput: { "aria-label": spec.label } }}
        />
      </SettingField>
    );
  };

  /**
   * 卡内的参数区：**两列网格**，每项仍是标签在上、控件在下。
   *
   * 一行一个字段会把宽屏拉成一条长表单：每行 1200px，眼睛来回折返。
   * 拆两列后每个字段 ~600px，`base_url` 能一行读完，卡片也不会太高。
   */
  const fieldGrid = (card: ModelCardSpec): React.ReactElement => (
    <Box sx={{
      display: "grid",
      gridTemplateColumns: { xs: "1fr", sm: "repeat(2, minmax(0, 1fr))" },
      columnGap: 3,
      rowGap: 2.5,
      alignItems: "start",
    }}>
      {card.fields.map((spec) => (
        <Box
          key={spec.key}
          sx={{ minWidth: 0, gridColumn: spec.full ? "1 / -1" : "auto" }}
        >
          {renderField(card.kind, spec)}
        </Box>
      ))}
    </Box>
  );

  /** 卡内的连通性测试区：按钮 + 结论。 */
  const testActions = (card: ModelCardSpec): React.ReactElement => (
    <>
      <Button size="small" variant="outlined" disabled={!!testing[card.test]}
        onClick={() => void test(card.test)}>
        {testing[card.test] ? "测试中…" : "测试连通性"}
      </Button>
      <TestResult result={results[card.test]} />
    </>
  );

  /** 测试到底会做什么：后端真发一次请求，别让人以为只是探活。 */
  const testNote = (card: ModelCardSpec): React.ReactElement => (
    <Typography variant="caption" color="text.secondary" sx={{ mt: 1.5 }}>
      {card.testNote}
    </Typography>
  );

  return (
    <Box component="form" onSubmit={(e) => { e.preventDefault(); void save(); }}>
      {/* 卡片竖着摞，列在卡内：每张卡里的设置项排成两列。
          表单列宽收在 1280 以内，两列各 ~600px，不会被拉到 1600px 宽。 */}
      <Box sx={{ maxWidth: 1280, mx: "auto" }}>
        <Stack spacing={2.5}>
          {MODEL_CARDS.map((card) => (
            <GlassPanel key={card.kind} radius={16} look={look} sx={{ p: 3 }}>
              <Typography variant="h6" sx={{ fontSize: 16 }}>{card.title}</Typography>
              <Typography variant="body2" color="text.secondary" sx={{ mb: 2.5 }}>
                {card.desc}
              </Typography>
              {fieldGrid(card)}
              <Divider sx={{ my: 2.5 }} />
              <Stack direction="row" spacing={2} alignItems="flex-start" sx={{ flexWrap: "wrap" }}>
                {testActions(card)}
              </Stack>
              {testNote(card)}
              {card.tip && (
                <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 0.5 }}>
                  {card.tip}
                </Typography>
              )}
            </GlassPanel>
          ))}
        </Stack>

        <GlassPanel
          radius={16} look={look}
          style={{ position: "sticky", bottom: 0, zIndex: 5 }}
          sx={{
            mt: 2.5, p: 2, display: "flex", gap: 2,
            alignItems: "center", flexWrap: "wrap",
          }}
        >
          <Typography variant="body2" color="text.secondary" sx={{ flex: 1, minWidth: 200 }}>
            保存后立即热切换并写入 data/config.toml。
            {/* 窄屏上这条提醒要占三行，把输入框都盖住了；维度字段旁边
                已经有一行红色的完整警告，这里只在宽屏重复一次。 */}
            <Box component="span" sx={{ display: { xs: "none", sm: "inline" } }}>
              <strong>向量维度变更会导致写入失效</strong>，需谨慎操作。
            </Box>
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
    </Box>
  );
}

/**
 * 设置项的骨架：标签在上、控件在下、说明再下。
 *
 * 标签是真正的 `<label for>`：点文字能聚焦控件，读屏也读得到名字——
 * 不能靠 placeholder，输入后就消失了。
 */
function SettingField({ id, labelId, label, hint, danger, children }: {
  id: string;
  labelId: string;
  label: string;
  hint?: string;
  danger?: boolean;
  children: React.ReactNode;
}): React.ReactElement {
  return (
    <Box sx={{ display: "grid", gap: 0.75 }}>
      <Typography
        component="label"
        htmlFor={id}
        id={labelId}
        variant="caption"
        sx={{ color: "text.secondary", fontWeight: 600, letterSpacing: "0.04em" }}
      >
        {label}
      </Typography>
      {children}
      {hint && (
        <Typography
          variant="caption"
          sx={{ color: danger ? "error.main" : "text.secondary" }}
        >
          {hint}
        </Typography>
      )}
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
