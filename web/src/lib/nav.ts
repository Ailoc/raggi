/**
 * 导航的单一事实来源：section 表。
 *
 * 原先导航定义散在三处——`App.svelte` 的 nav 数组、`activeNav` 的三元折叠
 * （kb / doc / standalone / kbsettings 都要折叠回 "kbs"）、以及
 * `router.svelte.ts` 的 name switch。三处各写一遍的直接后果是漂移：
 * 加一个视图就要改三个地方，漏一处就出现"进去了但顶栏没有高亮"。
 *
 * 现在一张表派生全部：dock 的 section 切换器、当前 section、
 * 以及快捷键 1–5。路由本身的解析仍在 router.svelte.ts。
 */
import type { Route } from "./router.svelte";
// 只 import 类型：构建时会被擦除，所以这条边不会变成 lib→ui 的运行时依赖
// （那 5 个 ops 模块的 lib→ui 反向依赖是另一件事，见方案 §4-1）
import type { IconName } from "../ui/icons";

export interface Section {
  key: string;
  label: string;
  /** section 的落地地址（点 section 时用它） */
  href: string;
  /** SVG 精灵里的图标名（见 web/index.html 的 #i-*） */
  icon: IconName;
  /** 哪些路由名属于这个 section——取代旧代码里手写的折叠三元 */
  routes: string[];
}

export const SECTIONS: Section[] = [
  { key: "kbs", label: "知识库", href: "#/", icon: "layers",
    routes: ["kbs", "kb", "doc"] },
  { key: "search", label: "检索", href: "#/search", icon: "search", routes: ["search"] },
  { key: "jobs", label: "任务", href: "#/jobs", icon: "pulse", routes: ["jobs"] },
  { key: "settings", label: "设置", href: "#/settings/models", icon: "gear",
    routes: ["settings"] },
  { key: "apidoc", label: "API", href: "#/apidoc", icon: "info", routes: ["apidoc"] },
];

const BY_ROUTE = new Map<string, string>(
  SECTIONS.flatMap((s) => s.routes.map((r) => [r, s.key] as const)),
);

/** 当前 section；未知路由归到知识库（与旧实现的 fallback 一致）。 */
export function sectionOf(route: Route): Section {
  return SECTIONS.find((s) => s.key === (BY_ROUTE.get(route.name) ?? "kbs"))
    ?? SECTIONS[0]!;
}
