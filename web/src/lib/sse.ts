/**
 * SSE 客户端（基于 fetch + ReadableStream）。
 *
 * 为什么不用 `EventSource`：它只支持 GET，无法携带请求头与请求体——
 * 而问答必须 POST、且在启用鉴权时要带 Bearer。浏览器没有 polyfill 级的
 * 替代（`eventsource` npm 包同样是 GET-only），因此手写解析。
 *
 * 要处理的三个坑：
 * 1. **分块**：网络包边界与 SSE 事件边界无关，必须按 \n\n 切事件并留缓冲；
 * 2. **flush**：服务端每次 yield 都要立刻到客户端，中转的代理可能缓冲
 *    （服务端已发 X-Accel-Buffering: no，但本地链路仍应逐块消费）；
 * 3. **中断**：用户点「停止」时必须真的断开连接——否则后端仍在生成，
 *    界面上「停止」只是骗自己。用 AbortController，并区分
 *    「主动停止」与「真的出错了」。
 */
import { authHeaders } from "./api";

export interface StreamHandlers {
  /** 引用表（事件 sources）——先于正文到达 */
  onSources?: (citations: unknown[]) => void;
  /** 增量文本（事件 delta），多次触发 */
  onDelta?: (text: string) => void;
  /** 收尾（事件 done） */
  onDone?: (info: { took_ms: number; mode: string; degraded_reason: string | null }) => void;
  /** 服务端推来的错误（事件 error） */
  onError?: (message: string, hint: string) => void;
}

export interface StreamHandle {
  /** 主动停止：断开连接并 resolve。再次调用无副作用。 */
  stop(): void;
}

/**
 * 发起一次流式请求。
 *
 * 返回的 handle 带 stop()：调用后连接立即断开。
 *
 * **必须立即返回 handle，而不是等流读完**——早前把整段读取放进
 * `await`，导致调用方的 `await streamPost(...)` 一直阻塞到生成结束，
 * 于是「停止」按钮在整个生成期间都没渲染出来（running 还没被置位），
 * 流式最该有的「随时可中断」恰好失效。
 */
export function streamPost(
  path: string,
  body: unknown,
  handlers: StreamHandlers,
  signal?: AbortSignal,
): StreamHandle {
  const ctrl = new AbortController();
  // 调用方自己的 signal 也要生效（组件卸载时用它收尾）
  if (signal) {
    if (signal.aborted) ctrl.abort();
    else signal.addEventListener("abort", () => ctrl.abort(), { once: true });
  }
  let stopped = false;

  const done = (async () => {
    const resp = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify(body),
      signal: ctrl.signal,
    });
    if (!resp.ok || !resp.body) {
      // 非流式错误（503 未启用问答等）：响应体仍是 {"detail": ...}
      let detail = `HTTP ${resp.status}`;
      try {
        const j = await resp.json();
        if (j && typeof j === "object" && "detail" in j) {
          detail = String((j as { detail: unknown }).detail);
        }
      } catch { /* 非 JSON 响应，保留状态码文案 */ }
      handlers.onError?.(detail, "");
      return;
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";

    for (;;) {
      const { done: fin, value } = await reader.read();
      if (fin) break;
      buf += decoder.decode(value, { stream: true });
      // SSE 以空行分隔事件；最后一段可能不完整，留在缓冲区等下一批
      let idx: number;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const raw = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        handleEvent(raw, handlers);
      }
    }
    // 收尾时可能还有一段没跟空行
    if (buf.trim()) handleEvent(buf, handlers);
  })();

  // 无论正常结束、报错还是被中断，都不让调用方拿到 rejection：
  // 错误已由 onError 汇报，用户主动停止更不是错误。
  void done.catch((e: unknown) => {
    if (stopped || (e instanceof DOMException && e.name === "AbortError")) return;
    handlers.onError?.(e instanceof Error ? e.message : String(e), "");
  });

  return {
    stop() {
      stopped = true;
      void ctrl.abort();
    },
  };
}

function handleEvent(raw: string, h: StreamHandlers): void {
  let event = "message";
  const dataLines: string[] = [];
  for (const line of raw.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
  }
  if (!dataLines.length) return;
  let data: Record<string, unknown>;
  try {
    data = JSON.parse(dataLines.join("\n")) as Record<string, unknown>;
  } catch {
    return;   // 半截 JSON：忽略而不是抛错（下一批会补全）
  }

  switch (event) {
    case "sources":
      h.onSources?.((data.citations as unknown[]) ?? []);
      break;
    case "delta":
      h.onDelta?.(String(data.text ?? ""));
      break;
    case "done":
      h.onDone?.({
        took_ms: Number(data.took_ms ?? 0),
        mode: String(data.mode ?? ""),
        degraded_reason: (data.degraded_reason as string | null) ?? null,
      });
      break;
    case "error":
      h.onError?.(String(data.message ?? "未知错误"),
        String(data.hint ?? ""));
      break;
  }
}
