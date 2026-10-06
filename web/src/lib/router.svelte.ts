/**
 * hash 路由。
 *
 * 路径是 $state，视图切换由 Svelte 的条件渲染完成。
 *
 * 阶段 3 起改成**模式表 + 命名参数**。之前是 `parts[0]/parts[2]` 这样的
 * 位置索引，视图里也跟着 `parts[1]`（kbId）、`parts[3]`（docId）——加一级
 * 路径就要同时改路由和一堆视图，漏一处就拿到 undefined。现在路由与视图
 * 都只认名字，段序只在模式表里出现一次。
 */
export interface Route {
  name: string;
  /** 命中的命名参数，如 { kbId, docId, tab } */
  params: Record<string, string>;
  /** 工作区标签（docs|chunks|settings）；非工作区路由为空串 */
  tab: string;
  /** 原始分段，仅供调试与兼容旧代码读取 */
  parts: string[];
  query: URLSearchParams;
}

interface Pattern {
  /** 路径段模式；以 ':' 开头的是参数占位 */
  seg: string[];
  name: string;
  tab?: string;
}

/**
 * 工作区路由保留 `kb` 这一段，没有按方案里写的 `#/{kbId}/{tab}` 缩短。
 *
 * 理由：短版会让 `#/search/docs` 这类地址与顶层 section 路由同形，
 * 路由必须靠"这段是不是 UUID"来消歧——而 kb_id 是字符串，用户完全可以
 * 建一个名字参与寻址的场景。少一个斜杠不值得换来一层隐式判断。
 */
const PATTERNS: Pattern[] = [
  { seg: [], name: "kbs" },
  { seg: ["search"], name: "search" },
  { seg: ["jobs"], name: "jobs" },
  { seg: ["apidoc"], name: "apidoc" },
  { seg: ["settings"], name: "settings", tab: "models" },
  { seg: ["settings", ":tab"], name: "settings" },
  { seg: ["kb", ":kbId", "docs"], name: "kb", tab: "docs" },
  { seg: ["kb", ":kbId", "chunks"], name: "kb", tab: "chunks" },
  { seg: ["kb", ":kbId", "settings"], name: "kb", tab: "settings" },
  { seg: ["kb", ":kbId", "doc", ":docId"], name: "doc", tab: "docs" },
  // 旧结构：#/{kbId} 裸列表
  { seg: ["kb", ":kbId"], name: "kb", tab: "docs" },
  { seg: ["kb", ":kbId", "standalone"], name: "kb", tab: "chunks" },
];

/** 旧地址 → 新地址。书签与外部链接不能 404。 */
function legacyRedirect(parts: string[]): string | null {
  if (parts[0] !== "kb") return null;
  const id = parts[1];
  if (!id) return null;
  if (parts.length === 2) return `${href("kb", id, "docs")}`;
  if (parts[2] === "standalone") {
    // 独立分块不再是单独一页，而是"分块"标签的一个范围筛选
    return `${href("kb", id, "chunks")}?scope=standalone`;
  }
  return null;
}

function match(parts: string[]): { name: string; params: Record<string, string>; tab: string } | null {
  for (const p of PATTERNS) {
    if (p.seg.length !== parts.length) continue;
    const params: Record<string, string> = {};
    let ok = true;
    for (let i = 0; i < p.seg.length; i++) {
      const s = p.seg[i]!;
      if (s.startsWith(":")) params[s.slice(1)] = parts[i] ?? "";
      else if (s !== parts[i]) { ok = false; break; }
    }
    if (ok) return { name: p.name, params, tab: p.tab ?? (params["tab"] ?? "") };
  }
  return null;
}

function parse(hash: string): Route {
  const raw = hash.replace(/^#\/?/, "");
  const [pathPart, queryPart = ""] = raw.split("?");
  const parts = pathPart.split("/").filter(Boolean).map(decodeURIComponent);
  const hit = match(parts) ?? { name: "kbs", params: {}, tab: "" };
  return { ...hit, parts, query: new URLSearchParams(queryPart) };
}

export const route = $state<Route>({
  name: "kbs", params: {}, tab: "", parts: [], query: new URLSearchParams(),
});

export function syncRoute(): void {
  // 裸 #/search 且上次有检索 → 先换成那次检索再解析。
  // 这件事必须放在 syncRoute 而不是 go()：dock 里的「检索」是 <a href>
  //（导航项只能是 a，否则中键新开、复制链接、状态栏预览全丢），
  // 它不经过 go()——恢复逻辑对真实点击路径一直是死的，
  // 实测点一次 dock：地址栏停在 #/search，检索框是空的。
  const bare = location.hash.replace(/^#/, "");
  if (bare === "/search" || bare === "/search?") {
    const prev = lastSearch();
    // replace 而不是赋值：不留一条"裸 #/search"的历史，否则后退一格又被
    // 恢复成同一次检索，用户退不出去
    if (prev && prev !== location.hash) { location.replace(prev); return; }
  }
  // 旧地址随时改写，不只限于首次加载：内部链接虽然都已升级成完整路径，
  // 但书签、手敲、外部贴进来的链接都会走到这里，地址栏留着旧形态的话，
  // 用户复制到的就是一个和当前界面不完全对应的 URL。
  const raw = location.hash.replace(/^#\/?/, "");
  const redirect = legacyRedirect(raw.split("?")[0]!.split("/").filter(Boolean));
  if (redirect) {
    // replace：不留一条"点进去又立刻被改写"的历史记录
    location.replace(redirect + (raw.includes("?") ? "?" + raw.split("?")[1] : ""));
    return;
  }
  const next = parse(location.hash);
  route.name = next.name;
  route.params = next.params;
  route.tab = next.tab;
  route.parts = next.parts;
  route.query = next.query;
}

/**
 * 记住最后一次「有内容」的检索。
 *
 * 顶栏或面包屑点「检索」会走到裸 #/search，若不留记忆，用户刚调好的
 * 关键词与过滤条件会被清空——他以为只是切了个页面。
 */
const LAST_SEARCH = "raggi.lastSearch";

export function rememberSearch(hash: string): void {
  try { sessionStorage.setItem(LAST_SEARCH, hash); } catch { /* 隐私模式 */ }
}

export function lastSearch(): string | null {
  try { return sessionStorage.getItem(LAST_SEARCH); } catch { return null; }
}

/** 编程式跳转。裸 "#/search" 的恢复不在这里做，见 syncRoute 的注释。 */
export function go(target: string): void {
  // 归一化：`href()` 给出的是 "#/…"，而手写调用常写成 "/…"。浏览器会自动
  // 给 location.hash 补 "#"，所以两种写法都能跳转，但读 hash 的地方必须
  // 只有一种形态。
  location.hash = target.startsWith("#")
    ? target
    : "#" + (target.startsWith("/") ? target : "/" + target);
}

/** 构造导航目标；分段自动编码，避免 ID 里的特殊字符破坏路由。 */
export function href(...parts: (string | number)[]): string {
  return "#/" + parts.map((p) => encodeURIComponent(String(p))).join("/");
}

export function queryString(params: Record<string, string | number | boolean | undefined | null>): string {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "" || v === false) continue;
    sp.set(k, String(v));
  }
  const s = sp.toString();
  return s ? "?" + s : "";
}

export function startRouter(): void {
  window.addEventListener("hashchange", syncRoute);
  // 改写逻辑现在在 syncRoute 里，启动时只需保证有个可解析的 hash
  if (!location.hash) location.replace("#/");
  else syncRoute();
}
