/**
 * 导航的单一事实来源：section 表。
 *
 * 顶栏分段控件、当前 section 与路由归属都从这张表派生。原先导航定义
 * 散在三处（壳层数组、activeNav 三元折叠、router 的 name switch），
 * 加一个视图要改三个地方，漏一处就会出现"进去了但顶栏没高亮"。
 */
import type { Route } from "./router";

export interface Section {
  key: string;
  label: string;
  /** section 的落地地址（点 section 时用它） */
  href: string;
  /** 哪些路由名属于这个 section */
  routes: string[];
}

export const SECTIONS: Section[] = [
  { key: "kbs", label: "知识库", href: "#/", routes: ["kbs", "kb", "doc"] },
  { key: "search", label: "检索", href: "#/search", routes: ["search"] },
  { key: "jobs", label: "任务", href: "#/jobs", routes: ["jobs"] },
  { key: "settings", label: "设置", href: "#/settings/models", routes: ["settings"] },
  { key: "apidoc", label: "API", href: "#/apidoc", routes: ["apidoc"] },
];

const BY_ROUTE = new Map<string, string>(
  SECTIONS.flatMap((s) => s.routes.map((r) => [r, s.key] as const)),
);

/** 当前 section；未知路由归到知识库（与旧实现的 fallback 一致）。 */
export function sectionOf(route: Route): Section {
  return SECTIONS.find((s) => s.key === (BY_ROUTE.get(route.name) ?? "kbs"))
    ?? SECTIONS[0]!;
}
