/**
 * 展示格式化的唯一来源。
 *
 * 之前 `fmtTime` 在 6 个文件里各写一遍（KbList / KbDocs / KbSettings /
 * Jobs / Settings / KeysPanel），而且是两种互不相同的风格：
 * 有的 `slice(0,16).replace("T"," ")` 输出 `2026-10-04 12:30`，
 * 有的 `toLocaleString("zh-CN")` 输出 `2026/10/4 12:30:00`，
 * 空值占位分别是 `""` 和 `"—"`。同一站点的两张表并排看就像两个产品。
 *
 * 这里只保留两种**语义**而不是两种风格：完整时间（详情页/设置页）与
 * 紧凑时间（表格里同一天的多行，年份是噪声）。
 */

/** 完整时间：`2026-10-04 12:30`。无效或缺失一律 `"—"`，不显示 1970。 */
export function fmtTime(s?: string | null): string {
  if (!s) return "—";
  const d = new Date(s);
  if (Number.isNaN(d.getTime())) return "—";
  const p = (n: number): string => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
    + ` ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** 紧凑时间：`10-04 12:30`。同一天内的多行列表里年份只是噪声。 */
export function fmtTimeShort(s?: string | null): string {
  const full = fmtTime(s);
  return full === "—" ? full : full.slice(5);
}

/** 相对时间：刚刚 / N 分钟前 / N 小时前 / 超过一天走完整时间。 */
export function fmtAgo(s?: string | null, now: number = Date.now()): string {
  if (!s) return "—";
  const t = new Date(s).getTime();
  if (Number.isNaN(t)) return "—";
  const sec = Math.max(0, Math.round((now - t) / 1000));
  if (sec < 60) return "刚刚";
  if (sec < 3600) return `${Math.floor(sec / 60)} 分钟前`;
  if (sec < 86400) return `${Math.floor(sec / 3600)} 小时前`;
  return fmtTime(s);
}

/** 字节数：1.5 KB / 230 MB。表格里的磁盘占用必须是这个而不是裸数字。 */
export function fmtBytes(n?: number | null): string {
  if (n === undefined || n === null || !Number.isFinite(n)) return "—";
  if (n < 1024) return `${n} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
  return `${v >= 100 ? Math.round(v) : Number(v.toFixed(1))} ${units[i]}`;
}

/** 毫秒：850 ms / 1.2 s。模型连通性测试与检索耗时都用它。 */
export function fmtMs(ms?: number | null): string {
  if (ms === undefined || ms === null || !Number.isFinite(ms)) return "—";
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.round(ms)} ms`;
}

/** 秒数区间：45s / 2m5s。任务耗时用。 */
export function fmtSpan(sec: number): string {
  if (!Number.isFinite(sec) || sec < 0) return "—";
  return sec < 60 ? `${Math.round(sec)}s`
    : `${Math.floor(sec / 60)}m${Math.round(sec % 60)}s`;
}
