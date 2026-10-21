import { useEffect, useState, useSyncExternalStore } from "react";
import type { ReactElement } from "react";
import Button from "@mui/material/Button";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import MenuItem from "@mui/material/MenuItem";
import Stack from "@mui/material/Stack";
import Switch from "@mui/material/Switch";
import TextField from "@mui/material/TextField";
import FormControlLabel from "@mui/material/FormControlLabel";
import Alert from "@mui/material/Alert";
import Typography from "@mui/material/Typography";

/**
 * Promise 化的确认框与表单框（对应 Svelte 版的 confirmDialog / formDialog）。
 *
 * 为什么是 Promise：调用点都是"await 用户做完决定，再继续"。写成受控组件
 * 意味着每个调用点都要维护 open/状态/回调三样东西，而这里有三四十个调用点
 * （删除文档、移动、重命名、编辑分块、覆盖方案……）——Promise 让它们全都
 * 是一行 `if (await confirmDialog({...}))`。
 *
 * 同时只允许一个对话框（模块级 current）：嵌套开两个模态框是最容易把
 * 用户绕晕的界面错误，队列里后来的直接顶掉前面的（并 resolve 前一个的
 * 取消），而不是叠两层。
 */

export interface ConfirmOptions {
  title: string;
  /** 正文：说清**影响面**（删几篇、几块），不要写"确定吗？" */
  body: string;
  confirmLabel?: string;
  cancelLabel?: string;
  /** 破坏性操作：确认按钮用 error 色、焦点落在取消上 */
  destructive?: boolean;
  /** 要求输入指定文本才能确认（删知识库这类） */
  requireTyping?: string;
}

export type FieldType = "text" | "textarea" | "number" | "select" | "switch";

export interface FieldSpec {
  key: string;
  label: string;
  /** 默认 text——多数表单字段就是文本 */
  type?: FieldType;
  value?: string | number | boolean;
  placeholder?: string;
  note?: string;
  required?: boolean;
  options?: Array<{ value: string; label: string }>;
  min?: number;
  max?: number;
}

export interface FormOptions {
  title: string;
  note?: string;
  fields: FieldSpec[];
  submitLabel?: string;
  /** 提交逻辑放这里：抛错则**就地显示**且不关框（用户不必重新填一遍） */
  onSubmit: (values: Record<string, string | number | boolean>) => Promise<void>;
}

type Current =
  | { kind: "confirm"; opts: ConfirmOptions; resolve: (v: boolean) => void }
  | { kind: "form"; opts: FormOptions; resolve: (v: Record<string, string | number | boolean> | null) => void }
  | null;

let current: Current = null;
const listeners = new Set<() => void>();

function emit(): void {
  for (const l of listeners) l();
}

function useCurrent(): Current {
  return useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    () => current,
  );
}

export function confirmDialog(opts: ConfirmOptions): Promise<boolean> {
  // 顶掉未决的那个：resolve 为"取消"，调用点的逻辑继续走"用户没确认"。
  // 分开写而不是三元：confirm 的 resolve 收 boolean、form 的收对象，
  // 合并成一次调用 TS 无法收窄联合类型。
  const prev = current;
  if (prev?.kind === "confirm") prev.resolve(false);
  else if (prev?.kind === "form") prev.resolve(null);
  return new Promise<boolean>((resolve) => {
    current = { kind: "confirm", opts, resolve };
    emit();
  });
}

export function formDialog(
  opts: FormOptions,
): Promise<Record<string, string | number | boolean> | null> {
  const prev = current;
  if (prev?.kind === "confirm") prev.resolve(false);
  else if (prev?.kind === "form") prev.resolve(null);
  return new Promise((resolve) => {
    current = { kind: "form", opts, resolve };
    emit();
  });
}

/** 挂在壳层最外层的渲染器 */
export function DialogHost(): ReactElement {
  const cur = useCurrent();
  return cur?.kind === "confirm" ? (
    <ConfirmView key={cur.opts.title} opts={cur.opts}
      onDone={(v) => { const r = cur.resolve; current = null; emit(); r(v); }} />
  ) : cur?.kind === "form" ? (
    <FormView key={cur.opts.title} opts={cur.opts}
      onDone={(v) => { const r = cur.resolve; current = null; emit(); r(v); }} />
  ) : (
    <></>
  );
}

function ConfirmView({
  opts, onDone,
}: {
  opts: ConfirmOptions;
  onDone: (v: boolean) => void;
}): ReactElement {
  const [typed, setTyped] = useState("");
  const ready = !opts.requireTyping || typed === opts.requireTyping;

  return (
    <Dialog open onClose={() => onDone(false)} fullWidth maxWidth="xs">
      <DialogTitle>{opts.title}</DialogTitle>
      <DialogContent>
        <Typography variant="body2" sx={{ whiteSpace: "pre-line", color: "text.secondary" }}>
          {opts.body}
        </Typography>
        {opts.requireTyping && (
          <TextField
            fullWidth
            size="small"
            sx={{ mt: 2 }}
            label={`输入「${opts.requireTyping}」以确认`}
            value={typed}
            autoComplete="off"
            onChange={(e) => setTyped(e.target.value)}
          />
        )}
      </DialogContent>
      <DialogActions>
        {/* 破坏性操作把焦点放在"取消"上：回车不该触发删除 */}
        <Button autoFocus={opts.destructive} onClick={() => onDone(false)}>
          {opts.cancelLabel ?? "取消"}
        </Button>
        <Button
          variant="contained"
          color={opts.destructive ? "error" : "primary"}
          disabled={!ready}
          onClick={() => onDone(true)}
        >
          {opts.confirmLabel ?? "确定"}
        </Button>
      </DialogActions>
    </Dialog>
  );
}

function FormView({
  opts, onDone,
}: {
  opts: FormOptions;
  onDone: (v: Record<string, string | number | boolean> | null) => void;
}): ReactElement {
  const [values, setValues] = useState<Record<string, string | number | boolean>>(() => {
    const init: Record<string, string | number | boolean> = {};
    for (const f of opts.fields) {
      init[f.key] = f.value ?? (f.type === "switch" ? false : f.type === "number" ? 0 : "");
    }
    return init;
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const missing = opts.fields.some(
    (f) => f.required && String(values[f.key] ?? "").trim() === "",
  );

  async function submit(): Promise<void> {
    if (busy || missing) return;
    setBusy(true);
    setError("");
    try {
      await opts.onSubmit(values);
      onDone(values);
    } catch (e) {
      // 就地报错、不关框：用户填的东西还在
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open onClose={() => onDone(null)} fullWidth maxWidth="sm">
      <DialogTitle>{opts.title}</DialogTitle>
      <DialogContent>
        {opts.note && (
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
            {opts.note}
          </Typography>
        )}
        {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}
        <Stack spacing={2}>
          {opts.fields.map((f) =>
            f.type === "switch" ? (
              <FormControlLabel
                key={f.key}
                control={
                  <Switch
                    checked={Boolean(values[f.key])}
                    onChange={(e) => setValues((v) => ({ ...v, [f.key]: e.target.checked }))}
                  />
                }
                label={f.label}
              />
            ) : f.type === "select" ? (
              <TextField
                key={f.key} select fullWidth label={f.label}
                value={String(values[f.key] ?? "")}
                onChange={(e) => setValues((v) => ({ ...v, [f.key]: e.target.value }))}
                helperText={f.note}
              >
                {(f.options ?? []).map((o) => (
                  <MenuItem key={o.value} value={o.value}>{o.label}</MenuItem>
                ))}
              </TextField>
            ) : (
              <TextField
                key={f.key}
                fullWidth
                label={f.label}
                type={f.type === "number" ? "number" : "text"}
                multiline={f.type === "textarea"}
                minRows={f.type === "textarea" ? 4 : undefined}
                placeholder={f.placeholder}
                helperText={f.note}
                required={f.required}
                value={String(values[f.key] ?? "")}
                onChange={(e) =>
                  setValues((v) => ({
                    ...v,
                    [f.key]: f.type === "number" ? Number(e.target.value) : e.target.value,
                  }))
                }
                slotProps={{ htmlInput: { min: f.min, max: f.max } }}
              />
            ),
          )}
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={() => onDone(null)} disabled={busy}>取消</Button>
        <Button variant="contained" disabled={busy || missing} onClick={() => void submit()}>
          {busy ? "处理中…" : (opts.submitLabel ?? "保存")}
        </Button>
      </DialogActions>
    </Dialog>
  );
}

/** 输入框自动聚焦（表单框打开时） */
export function useAutoFocus(): (el: HTMLElement | null) => void {
  const [el, setEl] = useState<HTMLElement | null>(null);
  useEffect(() => {
    el?.focus();
  }, [el]);
  return setEl;
}
