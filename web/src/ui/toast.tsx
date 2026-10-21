import { useEffect, useState, useSyncExternalStore } from "react";
import type { ReactElement } from "react";
import Alert from "@mui/material/Alert";
import Snackbar from "@mui/material/Snackbar";

/**
 * 轻提示（MUI Snackbar + Alert）。
 *
 * 模块级队列 + 订阅：调 `toast("已复制")` 的地方遍布 lib 与视图，
 * 它们没有组件的上下文，所以 API 必须是函数式的；渲染交给挂在壳层
 * 最外层的 `<ToastHost />`。
 *
 * 同时只显示一条（新的顶掉旧的）：Snackbar 叠起来会互相遮挡，
 * 而"三条提示同时看"本来也不是用户要的场景。
 */
export type ToastKind = "ok" | "err";

interface Item {
  id: number;
  text: string;
  kind: ToastKind;
}

let current: Item | null = null;
let nextId = 1;
const listeners = new Set<() => void>();

function emit(): void {
  for (const l of listeners) l();
}

export function toast(text: string, kind: ToastKind = "ok"): void {
  current = { id: nextId++, text, kind };
  emit();
}

/** 关闭当前提示（Snackbar 的 autoHide 到点也会走这里） */
function dismiss(): void {
  current = null;
  emit();
}

export function ToastHost(): ReactElement {
  const item = useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    () => current,
  );

  // Snackbar 自己管理进出场动画：key 换 id，新的来了就换成它
  const [open, setOpen] = useState(false);
  useEffect(() => {
    setOpen(!!item);
  }, [item]);

  return (
    <Snackbar
      key={item?.id ?? 0}
      open={open}
      autoHideDuration={item?.kind === "err" ? 6000 : 3000}
      onClose={(_e, reason) => {
        // clickaway 会误关：用户点别处并不是"读完了"
        if (reason === "clickaway") return;
        setOpen(false);
        dismiss();
      }}
      anchorOrigin={{ vertical: "bottom", horizontal: "center" }}
    >
      <Alert
        severity={item?.kind === "err" ? "error" : "success"}
        variant="filled"
        onClose={dismiss}
        sx={{ minWidth: 240 }}
      >
        {item?.text ?? ""}
      </Alert>
    </Snackbar>
  );
}
