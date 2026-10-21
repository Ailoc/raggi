import { createTheme } from "@mui/material/styles";
import type { Theme } from "@mui/material/styles";

/**
 * MUI 主题。
 *
 * 上一代（Svelte）里我们手写过一套"MUI 默认主题的 token 移植"——那是
 * 在不能引入 MUI 时的替代品。现在迁移到 React 之后，**用真的 createTheme**
 * 表达同一套值：`palette.mode` 一行就拿到 #1976d2 / #90caf9 两套官方调色板，
 * shape.borderRadius 默认就是 4，字体栈默认就是 Roboto 优先。
 *
 * 只覆盖三件事，其余全部吃 MUI 默认（**默认值就是设计规范**，
 * 覆盖得越少，"像不像 Material"越有保证）：
 *
 * 1. **中文字体回退**：MUI 默认栈到 Roboto/Helvetica/Arial 为止，中文会落到
 *    浏览器默认的宋体类字体，观感立刻垮掉——补上 PingFang/雅黑。
 * 2. **禁用态的字色**：中文笔画密，38% 黑/50% 白（MUI disabled 缺省）
 *    在 13px 下偏虚，提到 45% / 58%。
 * 3. **AppBar 与 Paper 的分层**：深色下 MUI 的 Paper 默认与背景同色
 *    （#121212），卡片"浮不起来"——把它定成 elevation 覆层色 #1e1e1e。
 */
export function buildTheme(mode: "dark" | "light"): Theme {
  return createTheme({
    palette: {
      mode,
      background:
        mode === "dark"
          ? { default: "#121212", paper: "#1e1e1e" }
          : { default: "#fafafa", paper: "#ffffff" },
    },
    typography: {
      fontFamily: [
        "Roboto",
        '"Helvetica Neue"',
        "Arial",
        "-apple-system",
        "system-ui",
        '"PingFang SC"',
        '"Hiragino Sans GB"',
        '"Microsoft YaHei"',
        "sans-serif",
      ].join(","),
      // 13px 的正文基准比 MUI 默认（16px）小一档：这是数据密集型工具，
      // 表格与列表是主角（Material 的 dense 密度就写着这一点）。
      body1: { fontSize: 14 },
      body2: { fontSize: 13 },
    },
    components: {
      MuiCssBaseline: {
        styleOverrides: {
          html: { height: "100%" },
          body: {
            height: "100%",
            // 应用是"顶栏 + 单列滚动区"，body 自己不滚（滚动归主区）
            overflow: "hidden",
          },
          "#root": { height: "100%" },
          // 视觉隐藏、但读屏仍可读（表头「操作」等）
          ".sr-only": {
            position: "absolute",
            width: 1,
            height: 1,
            padding: 0,
            margin: -1,
            overflow: "hidden",
            clip: "rect(0,0,0,0)",
            whiteSpace: "nowrap",
            border: 0,
          },
          // 跳转链接（index.html 的第一个元素）：平时移出视口，键盘聚焦时
          // 才出现在左上角。没有它键盘用户要按几十次 Tab 才能越过顶栏。
          ".skip-link": {
            position: "absolute",
            left: -9999,
            top: 0,
            zIndex: 9999,
            padding: "8px 16px",
            borderRadius: 4,
            backgroundColor: mode === "dark" ? "#90caf9" : "#1976d2",
            color: mode === "dark" ? "rgba(0,0,0,0.87)" : "#fff",
          },
          ".skip-link:focus": { left: 8, top: 8 },
          "::-webkit-scrollbar": { width: 10, height: 10 },
          "::-webkit-scrollbar-thumb": {
            background: mode === "dark"
              ? "rgba(255,255,255,0.24)"
              : "rgba(0,0,0,0.24)",
            borderRadius: 999,
            border: "3px solid transparent",
            backgroundClip: "padding-box",
          },
          "::-webkit-scrollbar-track": { background: "transparent" },
        },
      },
      MuiButton: {
        defaultProps: { disableElevation: false },
        styleOverrides: {
          // MUI 默认 uppercase 对中文无副作用（无大小写），但字距会留出
          // 半个字宽的尾隙——中文按钮不需要它，收紧到 0.02em。
          root: { letterSpacing: "0.02em" },
        },
      },
      MuiPaper: {
        styleOverrides: {
          // 卡片是 outlined 变体（1px 分隔线、无阴影）：一屏 3~5 张卡
          // 全带阴影会"像贴了一墙便利贴"。这需要在调用处传 variant，
          // 这里只保证圆角一致。
          rounded: { borderRadius: 4 },
        },
      },
      MuiTablePagination: {
        defaultProps: {
          getItemAriaLabel: (type) => ({
            first: "第一页",
            last: "最后一页",
            next: "下一页",
            previous: "上一页",
          }[type] ?? type),
        },
      },
      MuiTableCell: {
        styleOverrides: {
          root: { paddingTop: 8, paddingBottom: 8 },
          head: { fontWeight: 500 },
        },
      },
    },
  });
}
