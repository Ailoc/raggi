import { useEffect, useState } from "react";

/**
 * hash 路由：模式表 + 命名参数。
 *
 * 与 Svelte 版的 URL 结构**逐字一致**（`#/kb/{id}/doc/{id}?chunk=…`）——
 * 迁移只换渲染层，书签、深链、后端日志里的路径都不该变化。
 *
 * 为什么不用 react-router：路由规则只有十来条、没有嵌套布局、没有 loader，
 * 而"URL 是唯一事实来源"这件事本项目已经用一张模式表表达得很清楚。
 * 引入一个路由器框架换来的抽象，在这里全是成本。
 */

export interface Route {
  name: string;
  params: Record<string, string>;
  tab: string;
  parts: string[];
  query: URLSearchParams;
}

/** 路径模式 → 路由名。顺序即优先级（`kb/:kbId` 必须排在 `kb/:kbId/docs` 之后） */
const PATTERNS: Array<[string, string]> = [
  ["", "kbs"],
  ["search", "search"],
  ["jobs", "jobs"],
  ["apidoc", "apidoc"],
  ["settings", "settings"],
  ["settings/:tab", "settings"],
  ["kb/:kbId/docs", "kb"],
  ["kb/:kbId/chunks", "kb"],
  ["kb/:kbId/settings", "kb"],
  ["kb/:kbId/doc/:docId", "doc"],
  // 旧地址：不带 tab 的库路径与 standalone，保留并重定向（见 legacyRedirect）
  ["kb/:kbId", "kb"],
  ["kb/:kbId/standalone", "kb"],
];

function parseHash(raw: string): Route {
  const hash = raw.startsWith("#") ? raw.slice(1) : raw;
  const qIndex = hash.indexOf("?");
  const path = qIndex >= 0 ? hash.slice(0, qIndex) : hash;
  const query = new URLSearchParams(qIndex >= 0 ? hash.slice(qIndex + 1) : "");
  const parts = path.split("/").filter(Boolean);

  for (const [pattern, name] of PATTERNS) {
    const segs = pattern.split("/").filter(Boolean);
    if (segs.length !== parts.length) continue;
    const params: Record<string, string> = {};
    let ok = true;
    for (let i = 0; i < segs.length; i++) {
      const seg = segs[i]!;
      if (seg.startsWith(":")) {
        params[seg.slice(1)] = decodeURIComponent(parts[i]!);
      } else if (seg !== parts[i]) {
        ok = false;
        break;
      }
    }
    if (!ok) continue;
    // 库工作区的 tab 是**第三段**（`kb/{kbId}/docs` → "docs"）。
    // 取 parts[1] 会拿到 kbId 本身——实测的表现是"文档页渲染出了库设置"。
    // 设置页的 tab 才是第二段（`settings/keys`）。
    const tab = name === "kb"
      ? (parts[2] ?? "docs")
      : name === "settings"
        ? (params.tab ?? "models")
        : "docs";
    return { name, params, tab, parts, query };
  }
  return { name: "kbs", params: {}, tab: "docs", parts, query };
}

/** 旧地址重写：`#/kb/{id}` → docs；`#/kb/{id}/standalone` → chunks?scope=standalone */
function legacyRedirect(route: Route): string | null {
  if (route.name !== "kb") return null;
  const kbId = route.params.kbId ?? "";
  if (route.parts.length === 2) return href("kb", kbId, "docs");
  if (route.parts[2] === "standalone") {
    return `${href("kb", kbId, "chunks")}?scope=standalone`;
  }
  return null;
}

export function readRoute(): Route {
  const route = parseHash(location.hash);
  const redirect = legacyRedirect(route);
  if (redirect) {
    // replace 不留历史：用户按后退不该被弹回旧地址再转一次
    location.replace("#" + redirect.replace(/^#/, ""));
    return parseHash(redirect);
  }
  return route;
}

/** 订阅 hashchange 的路由 hook */
export function useRoute(): Route {
  const [route, setRoute] = useState<Route>(() => readRoute());
  useEffect(() => {
    const onHash = (): void => setRoute(readRoute());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  return route;
}

/** 构造导航目标；分段自动编码，避免 ID 里的特殊字符破坏路由 */
export function href(...parts: (string | number)[]): string {
  return "#/" + parts.map((p) => encodeURIComponent(String(p))).join("/");
}

export function queryString(
  params: Record<string, string | number | boolean | undefined | null>,
): string {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "" || v === false) continue;
    sp.set(k, String(v));
  }
  const s = sp.toString();
  return s ? "?" + s : "";
}

export function go(target: string): void {
  location.hash = target.startsWith("#")
    ? target
    : "#" + (target.startsWith("/") ? target : "/" + target);
}
