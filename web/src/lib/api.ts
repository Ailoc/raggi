// API 封装：Bearer 认证（token 存于 localStorage）+ JSON 请求。
// 泛型参数即响应类型，调用点由 TS 校验返回形状。
import type { Document } from "./types";

/**
 * 认证凭据存储键。
 *
 * 两类凭据并存：
 * - `raggi.token`：旧版静态 token（部署时用 RAG_TOKEN 设置）
 * - `raggi.apikey`：API 密钥（设置页签发，形如 rg_xxxxxxxx_...）
 *
 * 都存 localStorage，与既有行为一致（单机工具，无服务端会话）。
 */
const TOKEN_KEY = "token";
const APIKEY_KEY = "raggi.apikey";

export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) || "";
}

export function setToken(v: string): void {
  if (v) localStorage.setItem(TOKEN_KEY, v);
  else localStorage.removeItem(TOKEN_KEY);
}

/** 已配置的 API 密钥（无则空串）。密钥头优先于旧版 token。 */
export function getApiKey(): string {
  return localStorage.getItem(APIKEY_KEY) || "";
}

export function setApiKey(v: string): void {
  if (v) localStorage.setItem(APIKEY_KEY, v);
  else localStorage.removeItem(APIKEY_KEY);
}

export function authHeaders(): Record<string, string> {
  const key = getApiKey();
  if (key) return { Authorization: "Bearer " + key };
  const t = getToken();
  return t ? { Authorization: "Bearer " + t } : {};
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** 401 时的附加说明：把「没配凭据」和「密钥被拒」区分开，
 *  否则用户只看到「缺少凭据」，会以为自己漏填了设置里的 token。 */
function authHint(status: number, detail: string): string {
  if (status !== 401 && status !== 403) return detail;
  const hasKey = !!getApiKey();
  const hasToken = !!getToken();
  if (status === 403) return detail;
  if (!hasKey && !hasToken) {
    return detail + "（前端尚未配置凭据：可在「设置 → 访问密钥」填写密钥）";
  }
  if (hasKey) return detail + "（当前使用的密钥可能已吊销或过期）";
  return detail;
}

interface Options extends Omit<RequestInit, "body"> {
  /** 对象会自动 JSON 序列化；FormData 请改用 apiUpload */
  body?: unknown;
}

/**
 * 从错误响应里提取人话：后端 detail 可能是字符串、
 * FastAPI 校验错误数组，或干脆整段 JSON——直接展示原文很难读。
 */
async function errorMessage(r: Response): Promise<string> {
  const raw = await r.text();
  // 500 的响应体是 FastAPI 默认的纯文本 "Internal Server Error"，
  // 对使用者毫无信息量——翻译成一句能指引行动的话。
  if (!raw.trim() || /^internal server error$/i.test(raw.trim())) {
    return `服务器内部错误（HTTP ${r.status}），请查看服务端日志`;
  }
  try {
    const j: unknown = JSON.parse(raw);
    if (j && typeof j === "object" && "detail" in j) {
      const d = (j as { detail: unknown }).detail;
      if (typeof d === "string") return d;
      if (Array.isArray(d)) {
        return d.map((x) =>
          typeof x === "object" && x !== null && "msg" in x
            ? String((x as { msg: unknown }).msg)
            : String(x)).join("；");
      }
    }
  } catch {
    // 纯文本响应，原样返回
  }
  return raw;
}

/**
 * 发起 JSON 请求并解析响应。
 * 非 2xx 抛出 ApiError，消息取响应体（后端 detail 直接是字符串或 JSON）。
 */
export async function api<T>(path: string, opts: Options = {}): Promise<T> {
  const headers: Record<string, string> = Object.assign(
    { "Content-Type": "application/json" },
    authHeaders(),
    opts.headers as Record<string, string> | undefined,
  );
  const { body, ...rest } = opts;
  const init: RequestInit = { ...rest, headers };
  if (body !== undefined) init.body = JSON.stringify(body);
  const r = await fetch(path, init);
  if (!r.ok) throw new ApiError(r.status, authHint(r.status, await errorMessage(r)));
  return (await r.json()) as T;
}

/**
 * 同一个请求管线，但响应是纯文本（Markdown 报告、原文下载等）。
 *
 * 之前导出报告是绕过 `api()` 直接 `fetch` 的，于是拿不到鉴权头与统一的
 * 错误解析：后端一旦启用密钥，导出就变成裸 401，而用户看到的只是
 * 一段没人读的响应体文本。
 */
export async function apiText(path: string, opts: Options = {}): Promise<string> {
  const headers: Record<string, string> = Object.assign(
    { "Content-Type": "application/json" },
    authHeaders(),
    opts.headers as Record<string, string> | undefined,
  );
  const { body, ...rest } = opts;
  const init: RequestInit = { ...rest, headers };
  if (body !== undefined) init.body = JSON.stringify(body);
  const r = await fetch(path, init);
  if (!r.ok) throw new ApiError(r.status, authHint(r.status, await errorMessage(r)));
  return r.text();
}

/** 文件上传走 multipart，不能带 Content-Type（需由浏览器自行填 boundary）。 */
export async function apiUpload<T>(path: string, form: FormData,
                                  idempotencyKey?: string): Promise<T> {
  const r = await fetch(path, {
    method: "POST",
    body: form,
    headers: {
      ...authHeaders(),
      // 同一个键的重试返回首次结果，不会产生第二份文档——
      // 上传失败后重试（网络抖动、大文件超时）才安全
      ...(idempotencyKey ? { "Idempotency-Key": idempotencyKey } : {}),
    },
  });
  if (!r.ok) throw new ApiError(r.status, authHint(r.status, await errorMessage(r)));
  return (await r.json()) as T;
}

// ---- 端点封装：把 URL 拼装与响应类型收敛到一处 ----

export const getDoc = (docId: string) =>
  api<Document>(`/api/documents/${encodeURIComponent(docId)}`);