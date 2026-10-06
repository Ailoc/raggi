/**
 * 原文面板的类型分派。
 *
 * | 类型            | 呈现方式                                    | 定位           |
 * |-----------------|---------------------------------------------|----------------|
 * | PDF             | `<iframe>`（浏览器内置查看器）              | URL `#page=N`  |
 * | 图片            | `<img>`                                     | 不可定位        |
 * | 文本类          | 抓取内容后按 offset 渲染                    | char_start/end |
 * | 其它二进制      | 下载入口                                    | 不可定位        |
 *
 * 为什么 PDF 用内置查看器：项目要求运行时零依赖，而主流浏览器都自带
 * PDF 渲染并支持 `#page=N` 跳页——分块到页码的联动用这一条就够。
 * 引入体积庞大的第三方渲染库只为跳页，不划算，而且它同样要发一个请求。
 */

export type SourceKind = "pdf" | "image" | "text" | "download";

/** 文本类 MIME：能被 fetch 成字符串再按偏移高亮。 */
const TEXT_MIME = /^text\/|json|xml|csv|markdown|x-yaml|x-sh/i;
const TEXT_EXT = /\.(txt|md|markdown|json|csv|log|ya?ml|html?|ts|js|py|toml|ini)$/i;

export function sourceKind(mime: string, filename: string): SourceKind {
  const m = (mime || "").toLowerCase();
  const f = filename || "";
  if (m === "application/pdf" || /\.pdf$/i.test(f)) return "pdf";
  if (m.startsWith("image/")) return "image";
  if (TEXT_MIME.test(m) || TEXT_EXT.test(f)) return "text";
  return "download";
}

/** 原生查看器的跳页地址。页码非法时退回第 1 页，不拼出 `#page=NaN`。 */
export function pageUrl(url: string, page?: number | null): string {
  const p = page && page > 0 ? Math.floor(page) : 1;
  const base = url.split("#")[0];
  return `${base}#page=${p}&view=FitH`;
}

/** 下载链接：加参数让后端带 Content-Disposition。 */
export function downloadUrl(url: string): string {
  return url + (url.includes("?") ? "&" : "?") + "download=1";
}

export interface Segment {
  text: string;
  /** 该段是否落在分块的字符区间内（渲染时高亮） */
  hit: boolean;
}

/**
 * 按字符偏移把全文切成「命中 / 未命中」交替的片段。
 *
 * 前置条件：`text` 必须与偏移的来源一致（后端在解析时用同一份纯文本
 * 计算 char_start/char_end）。调用方需先确认 `offset_valid`，否则
 * 偏移可能来自改写前的版本，切出来的高亮会错位到别的段落。
 */
export function segment(text: string, start: number, end: number): Segment[] {
  const a = Math.max(0, Math.min(start, text.length));
  const b = Math.max(a, Math.min(end, text.length));
  const out: Segment[] = [];
  if (a > 0) out.push({ text: text.slice(0, a), hit: false });
  if (b > a) out.push({ text: text.slice(a, b), hit: true });
  if (b < text.length) out.push({ text: text.slice(b), hit: false });
  return out;
}
