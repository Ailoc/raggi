/*
 * 首帧前的主题引导。
 *
 * 必须是独立文件而不是 index.html 里的内联脚本：页面响应带
 * `Content-Security-Policy: script-src 'self'`，内联脚本会被浏览器拦掉，
 * 结果就是浅色用户先看到一帧深色。放在 public/ 下由 Vite 原样复制。
 */
(function () {
  try {
    var t = localStorage.getItem("raggi.theme");
    if (!t) t = matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
    document.documentElement.dataset.theme = t;
    document.documentElement.style.colorScheme = t;
    document.documentElement.style.background =
      t === "light" ? "#f4faf8" : "#0b1416";
  } catch (e) {
    /* 隐私模式下 localStorage 不可用，保持默认深色 */
  }
})();
