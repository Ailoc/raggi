import { useEffect, useMemo, useRef, useState } from "react";
import Box from "@mui/material/Box";
import Chip from "@mui/material/Chip";
import Dialog from "@mui/material/Dialog";
import Divider from "@mui/material/Divider";
import List from "@mui/material/List";
import ListItemButton from "@mui/material/ListItemButton";
import ListItemIcon from "@mui/material/ListItemIcon";
import ListItemText from "@mui/material/ListItemText";
import ListSubheader from "@mui/material/ListSubheader";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";
import DescriptionIcon from "@mui/icons-material/Description";
import FolderIcon from "@mui/icons-material/Folder";
import SearchIcon from "@mui/icons-material/Search";

import { api } from "../lib/api";
import { SECTIONS } from "../lib/nav";
import { go, href } from "../lib/router";
import { setPendingSearch } from "../lib/searchintent";
import { useStoreSelect } from "../lib/store";
import type { DocumentList } from "../lib/types";

/**
 * 命令面板（⌘K / Ctrl+K）：去哪 / 检索 / 做什么 的统一入口。
 *
 * 它替代的**不是**导航，而是"穿过多层列表找一篇文档"：以前要到某篇文档
 * 要么从总览进库、再从库进列表（两次加载两次点击），要么先检索再点进结果。
 * 面板把所有跳转目标摊平成一张可搜索列表。
 *
 * 刻意**不常驻**（不是侧栏）：常驻导航栏在这个项目里试过三代都删了——
 * 它与主内容重复。面板是瞬态的，只有想跳转时存在。
 *
 * 文档列表懒加载（limit=200，够用就停）：面板的定位是跳转，不是浏览全部。
 */
interface Cmd {
  id: string;
  group: string;
  label: string;
  hint?: string;
  icon: React.ReactNode;
  run: () => void;
}

export default function CommandPalette(): React.ReactElement {
  const kbs = useStoreSelect((s) => s.kbs);
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const [active, setActive] = useState(0);
  const [docs, setDocs] = useState<DocumentList["items"]>([]);
  const docsLoaded = useRef(false);
  const inputRef = useRef<HTMLInputElement | null>(null);

  // 打开入口挂在 window 上（顶栏按钮与快捷键都派发同一个事件）
  useEffect(() => {
    const onOpen = (): void => {
      setOpen(true);
      setQ("");
      setActive(0);
    };
    window.addEventListener("raggi:palette", onOpen);
    return () => window.removeEventListener("raggi:palette", onOpen);
  }, []);

  useEffect(() => {
    if (!open) return;
    // 聚焦输入框：面板的全部价值在"立刻能打字"
    setTimeout(() => inputRef.current?.focus(), 0);
    if (docsLoaded.current) return;
    docsLoaded.current = true;
    void api<DocumentList>("/api/documents?limit=200")
      .then((r) => setDocs(r.items))
      .catch(() => { docsLoaded.current = false; /* 下次打开再试 */ });
  }, [open]);

  const ql = q.trim().toLowerCase();

  const results = useMemo((): Cmd[] => {
    const out: Cmd[] = [];
    const match = (c: Cmd): boolean =>
      !ql || (c.label + " " + (c.hint ?? "")).toLowerCase().includes(ql);
    let group = "";

    const push = (list: Cmd[], cap: number): void => {
      let n = 0;
      for (const c of list) {
        if (n >= cap) break;
        if (!match(c)) continue;
        if (c.group !== group) group = c.group;
        out.push(c);
        n += 1;
      }
    };

    // 检索入口置顶：最常见的用途就是"有词了，去找它"
    if (ql) {
      out.push({
        id: "search",
        group: "检索",
        label: `检索“${q.trim()}”`,
        hint: "全部知识库",
        icon: <SearchIcon fontSize="small" />,
        run: () => {
          const text = q.trim();
          setPendingSearch(text);
          window.dispatchEvent(new CustomEvent("raggi:search", { detail: { q: text } }));
          go(href("search"));
        },
      });
      group = "检索";
    }

    push(
      SECTIONS.map((s) => ({
        id: `nav:${s.key}`,
        group: "去往",
        label: s.label,
        icon: <SearchIcon fontSize="small" />,
        run: () => {
          if (s.key === "search") {
            window.dispatchEvent(new CustomEvent("raggi:newSearch"));
          }
          go(s.href);
        },
      })),
      6,
    );
    push(
      kbs.map((k) => ({
        id: `kb:${k.kb_id}`,
        group: "去往",
        label: k.name,
        hint: `${k.doc_count} 篇 · ${k.chunk_count} 块`,
        icon: <FolderIcon fontSize="small" color="primary" />,
        run: () => go(href("kb", k.kb_id, "docs")),
      })),
      8,
    );
    if (ql) {
      push(
        docs.map((d) => ({
          id: `doc:${d.doc_id}`,
          group: "文档",
          label: d.title,
          hint: `${d.chunk_count ?? 0} 块`,
          icon: <DescriptionIcon fontSize="small" />,
          run: () => go(href("kb", d.kb_id ?? "", "doc", d.doc_id)),
        })),
        12,
      );
    }
    return out;
  }, [q, ql, kbs, docs]);

  // 结果变短时把选中项拉回界内（敲字把列表从 20 条筛到 3 条）
  useEffect(() => {
    if (active >= results.length) setActive(Math.max(0, results.length - 1));
  }, [results.length, active]);

  function onKeyDown(e: React.KeyboardEvent): void {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive((a) => (results.length === 0 ? 0 : (a + 1) % results.length));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((a) => (results.length === 0 ? 0 : (a - 1 + results.length) % results.length));
    } else if (e.key === "Enter") {
      e.preventDefault();
      const c = results[active];
      if (c) {
        setOpen(false);
        c.run();
      }
    }
  }

  let lastGroup = "";

  return (
    <Dialog
      open={open}
      onClose={() => setOpen(false)}
      fullWidth
      maxWidth="sm"
      slotProps={{
        paper: { sx: { position: "fixed", top: "10vh", m: 0 } },
      }}
    >
      <Box sx={{ p: 2, pb: 1 }}>
        <TextField
          inputRef={inputRef}
          fullWidth
          variant="standard"
          placeholder="跳转到知识库、文档，或直接检索…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={onKeyDown}
          InputProps={{ disableUnderline: true, sx: { fontSize: 16 } }}
          inputProps={{ "aria-label": "命令面板搜索" }}
        />
      </Box>
      <Divider />

      <List dense sx={{ maxHeight: "52vh", overflowY: "auto", py: 1 }}>
        {results.map((c, i) => {
          const header = c.group !== lastGroup ? c.group : null;
          lastGroup = c.group;
          return (
            <Box key={c.id}>
              {header && (
                <ListSubheader
                  disableSticky
                  sx={{ lineHeight: "28px", fontSize: 11, letterSpacing: ".05em", textTransform: "uppercase" }}
                >
                  {header}
                </ListSubheader>
              )}
              <ListItemButton
                selected={i === active}
                onMouseEnter={() => setActive(i)}
                onClick={() => {
                  setOpen(false);
                  c.run();
                }}
                sx={{ borderRadius: 1, mx: 1 }}
              >
                <ListItemIcon sx={{ minWidth: 32 }}>{c.icon}</ListItemIcon>
                <ListItemText
                  primary={c.label}
                  slotProps={{
                    primary: { noWrap: true, fontSize: 14 },
                  }}
                />
                {c.hint && <Chip size="small" variant="outlined" label={c.hint} />}
              </ListItemButton>
            </Box>
          );
        })}
        {results.length === 0 && (
          <Typography variant="body2" color="text.secondary" sx={{ px: 2, py: 3, textAlign: "center" }}>
            没有匹配的跳转目标。
          </Typography>
        )}
      </List>

      <Divider />
      <Box sx={{ px: 2, py: 1, display: "flex", gap: 2 }}>
        <Typography variant="caption" color="text.secondary">
          ↑↓ 选择
        </Typography>
        <Typography variant="caption" color="text.secondary">
          Enter 执行
        </Typography>
        <Typography variant="caption" color="text.secondary">
          Esc 关闭
        </Typography>
      </Box>
    </Dialog>
  );
}
