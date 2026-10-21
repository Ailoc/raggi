import { useCallback, useEffect, useRef, useState } from "react";
import Alert from "@mui/material/Alert";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import Divider from "@mui/material/Divider";
import Paper from "@mui/material/Paper";
import Skeleton from "@mui/material/Skeleton";
import Stack from "@mui/material/Stack";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";

import {
  api, ApiError, getApiKey, getToken, setApiKey, setToken,
} from "../lib/api";
import { confirmDialog, formDialog } from "../lib/dialog";
import { fmtTime } from "../lib/format";
import type { ApiKey, ApiKeyCreated, ApiKeyList } from "../lib/types";
import { toast } from "./toast";

/**
 * API 密钥：列表 / 签发 / 吊销 + 本机凭据。
 *
 * 明文密钥只在签发响应里出现一次（服务端只存哈希）；首次签发且本机
 * 没有任何凭据时自动收下，否则随后的列表刷新会 401，用户反而看不到
 * 自己刚签发的密钥。
 */
export default function KeysPanel(): React.ReactElement {
  const [list, setList] = useState<ApiKeyList | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [localKey, setLocalKey] = useState(getApiKey());
  const [localToken, setLocalToken] = useState(getToken());
  const loadSeq = useRef(0);

  const load = useCallback(async (): Promise<void> => {
    const seq = ++loadSeq.current;
    setLoading(true);
    setError("");
    try {
      const data = await api<ApiKeyList>("/api/keys");
      if (seq !== loadSeq.current) return;
      setList(data);
    } catch (e) {
      if (seq !== loadSeq.current) return;
      setError(e instanceof ApiError && (e.status === 401 || e.status === 403)
        ? "需要 write 权限才能管理密钥"
        : e instanceof Error ? e.message : String(e));
    } finally {
      if (seq === loadSeq.current) setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const stateLabel: Record<string, string> = {
    active: "有效", revoked: "已吊销", expired: "已过期",
  };

  async function createKey(): Promise<void> {
    let created: ApiKeyCreated | null = null;
    const v = await formDialog({
      title: "签发 API 密钥",
      note: "密钥明文只在签发后显示一次，服务端仅保存哈希，丢失后无法找回。",
      submitLabel: "签发",
      fields: [
        {
          key: "name", label: "名称", required: true,
          placeholder: "例如：CI 流水线 / 内部集成",
          note: "用于区分调用方，日后按名字吊销。",
        },
        {
          key: "scope", label: "权限", type: "select", value: "read",
          options: [
            { value: "read", label: "read（只读：检索与查看）" },
            { value: "write", label: "write（读写：可入库、编辑、删除）" },
          ],
          note: "只读够用就别给 write：只读密钥无法入库、编辑、删除，也不能签发密钥。",
        },
        {
          key: "expires_in_days", label: "有效期（天）", type: "text",
          placeholder: "留空 = 永不过期",
          note: "建议给外部集成设定期限，减少永久凭据的暴露面。",
        },
        {
          key: "note", label: "备注", placeholder: "用途说明（可选）",
        },
      ],
      onSubmit: async (values) => {
        const raw = String(values.expires_in_days ?? "").trim();
        const days = raw ? Number(raw) : null;
        if (raw && (!Number.isInteger(days) || (days as number) <= 0)) {
          throw new Error("有效期必须是正整数天");
        }
        created = await api<ApiKeyCreated>("/api/keys", {
          method: "POST",
          body: {
            name: String(values.name).trim(),
            scope: String(values.scope) === "write" ? "write" : "read",
            expires_in_days: days,
            note: String(values.note ?? "").trim(),
          },
        });
        if (!getApiKey() && !getToken() && created?.key) setApiKey(created.key);
      },
    });
    if (!v || !created) return;
    const c: ApiKeyCreated = created;
    setLocalKey(getApiKey());
    await load();
    await formDialog({
      title: "密钥已签发",
      note: c.warning || "密钥明文只显示这一次，请立即保存。",
      submitLabel: "我已保存",
      fields: [{
        key: "key", label: "完整密钥", type: "textarea", value: c.key, required: true,
        note: "关闭后无法再次查看。若丢失，请在列表中吊销并重新签发。",
      }],
      onSubmit: async () => { /* 仅展示 */ },
    });
  }

  async function revoke(k: ApiKey): Promise<void> {
    const isLocal = !!getApiKey() && !!k.prefix && getApiKey().startsWith(k.prefix);
    const ok = await confirmDialog({
      title: `吊销「${k.name}」`,
      body: "使用该密钥的客户端会立即失效并收到 401。"
        + (isLocal ? "当前浏览器正在使用这把密钥，吊销后需要重新配置凭据。" : ""),
      confirmLabel: "吊销",
      destructive: true,
    });
    if (!ok) return;
    try {
      await api(`/api/keys/${encodeURIComponent(k.key_id)}`, { method: "DELETE" });
      toast("已吊销");
      await load();
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), "err");
    }
  }

  function saveLocal(): void {
    setApiKey(localKey.trim());
    setToken(localToken.trim());
    toast("本机凭据已保存");
    void load();
  }

  function clearLocal(): void {
    setApiKey("");
    setToken("");
    setLocalKey("");
    setLocalToken("");
    toast("本机凭据已清除");
    void load();
  }

  return (
    <Stack spacing={2}>
      <Paper variant="outlined" sx={{ p: 3 }}>
        <Typography variant="h6" sx={{ fontSize: 16 }}>API 密钥</Typography>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          对外调用本服务时使用的凭据，形如 <code>rg_xxxxxxxx…</code>。
          请求时放在 <code>Authorization: Bearer &lt;key&gt;</code> 或{" "}
          <code>X-API-Key</code> 头里。
        </Typography>

        {loading && <Skeleton variant="rounded" height={120} />}
        {!loading && error && (
          <Alert
            severity="error"
            action={<Button color="inherit" size="small" onClick={() => void load()}>重试</Button>}
          >
            {error}。若本机没有 write 凭据，可在下方「本机凭据」里填入后重试。
          </Alert>
        )}
        {!loading && !error && list && (
          <>
            <Stack direction="row" spacing={3} sx={{ mb: 2 }} flexWrap="wrap">
              <Typography variant="body2">密钥总数 <strong>{list.total}</strong></Typography>
              <Typography variant="body2">
                有效 <strong>{list.items.filter((k) => k.state === "active").length}</strong>
              </Typography>
              <Typography variant="body2" color="text.secondary">
                静态 token {list.legacy_token_enabled ? "已启用" : "未启用"}
              </Typography>
            </Stack>

            {list.items.length === 0 ? (
              <Alert severity="info" action={
                <Button color="inherit" size="small" onClick={() => void createKey()}>签发第一把</Button>
              }>
                还没有 API 密钥。当前未启用鉴权，任何人都能直接调用本服务的接口。
                若要把 Raggi 暴露给其他人或公网，请先签发一把密钥。
              </Alert>
            ) : (
              <TableContainer>
                <Table size="small" aria-label="API 密钥列表">
                  <TableHead>
                    <TableRow>
                      <TableCell>名称</TableCell>
                      <TableCell>密钥</TableCell>
                      <TableCell>权限</TableCell>
                      <TableCell>状态</TableCell>
                      <TableCell>最近使用</TableCell>
                      <TableCell>过期</TableCell>
                      <TableCell align="right"><span className="sr-only">操作</span></TableCell>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {list.items.map((k) => (
                      <TableRow key={k.key_id} hover>
                        <TableCell>{k.name}</TableCell>
                        <TableCell sx={{ fontFamily: "monospace" }}>
                          {k.prefix ? `${k.prefix}…` : "—"}
                        </TableCell>
                        <TableCell>
                          <Chip size="small" variant="outlined"
                            color={k.scope === "write" ? "warning" : "default"} label={k.scope} />
                        </TableCell>
                        <TableCell>
                          <Chip
                            size="small" variant="outlined"
                            color={k.state === "active" ? "success" : k.state === "expired" ? "warning" : "error"}
                            label={stateLabel[k.state] ?? k.state}
                          />
                        </TableCell>
                        <TableCell>{fmtTime(k.last_used_at)}</TableCell>
                        <TableCell>{k.expires_at ? fmtTime(k.expires_at) : "永不过期"}</TableCell>
                        <TableCell align="right">
                          {k.state === "active" && (
                            <Button size="small" color="error" onClick={() => void revoke(k)}>
                              吊销
                            </Button>
                          )}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </TableContainer>
            )}
          </>
        )}

        <Divider sx={{ my: 2 }} />
        <Button variant="contained" onClick={() => void createKey()}>签发新密钥</Button>
      </Paper>

      <Paper variant="outlined" sx={{ p: 3 }}>
        <Typography variant="h6" sx={{ fontSize: 16 }}>本机凭据</Typography>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          浏览器调用本服务时携带的凭据，保存在本机 localStorage。
          API 密钥优先于静态 token；两者都为空时按「单机自用」直连。
        </Typography>
        <Stack direction={{ xs: "column", sm: "row" }} spacing={2} sx={{ mb: 2 }}>
          <TextField
            label="API 密钥" type="password" autoComplete="off" fullWidth
            value={localKey} onChange={(e) => setLocalKey(e.target.value)}
            placeholder="rg_…（留空则不使用）"
          />
          <TextField
            label="静态 token" type="password" autoComplete="off" fullWidth
            value={localToken} onChange={(e) => setLocalToken(e.target.value)}
            placeholder="部署时 RAG_TOKEN 的值（可选）"
          />
        </Stack>
        <Stack direction="row" spacing={1} justifyContent="flex-end">
          <Button onClick={clearLocal}>清除</Button>
          <Button variant="contained" onClick={saveLocal}>保存并重试</Button>
        </Stack>
      </Paper>
    </Stack>
  );
}
