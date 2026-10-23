import { StrictMode, useEffect, useMemo } from "react";
import { createRoot } from "react-dom/client";
import { CacheProvider } from "@emotion/react";
import createCache from "@emotion/cache";
import CssBaseline from "@mui/material/CssBaseline";
import { ThemeProvider } from "@mui/material/styles";

import App from "./App";
import { DialogHost } from "./lib/dialog";
import { buildTheme } from "./theme";
import { useStoreSelect } from "./lib/store";
import { GlassProvider } from "./ui/glass";

/**
 * 入口：挂载 React 根，主题随 `data-theme` 切换。
 *
 * 主题状态在 store 里（模块级），这里只是把它接进 MUI 的 ThemeProvider——
 * 换主题不重建应用，只重建 theme 对象（`useMemo` 按 mode 缓存，
 * 否则每次渲染都会生成新 theme 并让整棵树重样式化）。
 */
/** Emotion 缓存：用服务端注入的 CSP nonce（生产）；开发态没有 meta 则不带。 */
const emotionCache = createCache({
  key: "css",
  nonce: document
    .querySelector('meta[name="csp-nonce"]')
    ?.getAttribute("content") || undefined,
});

function Root(): React.ReactElement {
  const theme = useStoreSelect((s) => s.theme);
  const muiTheme = useMemo(() => buildTheme(theme), [theme]);

  // MUI 的 prefers-color-scheme 与我们的 data-theme 可能不一致：
  // 以 data-theme 为准（它才是用户选的那个），这里只同步浏览器 UI 色。
  useEffect(() => {
    document.documentElement.style.colorScheme = theme;
  }, [theme]);

  return (
    <CacheProvider value={emotionCache}>
      <ThemeProvider theme={muiTheme}>
        <CssBaseline />
        {/* 玻璃系统的主题边界：明暗两套材质 token 跟着应用主题走 */}
        <GlassProvider>
          <App />
          <DialogHost />
        </GlassProvider>
      </ThemeProvider>
    </CacheProvider>
  );
}

const el = document.getElementById("root");
if (!el) throw new Error("缺少 #root 挂载点（web/index.html）");

createRoot(el).render(
  <StrictMode>
    <Root />
  </StrictMode>,
);
