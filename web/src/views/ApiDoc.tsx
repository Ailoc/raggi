import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import IconButton from "@mui/material/IconButton";
import InputAdornment from "@mui/material/InputAdornment";
import List from "@mui/material/List";
import ListItemButton from "@mui/material/ListItemButton";
import ListItemText from "@mui/material/ListItemText";
import ListSubheader from "@mui/material/ListSubheader";
import { GlassPanel, useGlassLook } from "../ui/glass";
import Stack from "@mui/material/Stack";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";
import CheckIcon from "@mui/icons-material/Check";
import ContentCopyIcon from "@mui/icons-material/ContentCopy";
import SearchIcon from "@mui/icons-material/Search";

import { AUTH_NOTE, GROUPS, curlFor } from "../data/apidoc";
import type { Endpoint, Param } from "../data/apidoc";
import { selectText } from "../lib/dom";
import { CONTENT_MAX } from "../lib/layout";
import { go } from "../lib/router";
import { setCrumbs } from "../lib/store";

const METHOD_COLOR: Record<string, "success" | "info" | "warning" | "secondary" | "error"> = {
  GET: "success", POST: "info", PUT: "warning", PATCH: "secondary", DELETE: "error",
};

const METHOD_TITLE: Record<string, string> = {
  GET: "读取数据", POST: "创建或触发动作", PUT: "整体更新",
  PATCH: "局部更新", DELETE: "删除数据",
};

const IN_ORDER: Array<[Param["in"], string]> = [
  ["path", "路径参数"], ["query", "查询参数"],
  ["form", "表单字段"], ["body", "请求体"],
];

const epKey = (e: Endpoint): string => `${e.method} ${e.path}`;
const titleOf = (e: Endpoint): string => e.summary.replace(/。\s*$/, "");

/**
 * 行内标记：**粗体**、`代码` 与换行。
 *
 * 这里**不做 HTML 注入**：数据虽是仓内静态清单，但用 React 节点解析
 * 既没有注入面，也不需要"先整体转义再插标签"的自证负担。
 */
function inline(text: string): ReactNode[] {
  const out: ReactNode[] = [];
  const re = /\*\*([^*]+)\*\*|`([^`]+)`|\n/g;
  let cur = 0;
  let key = 0;
  for (let m = re.exec(text); m; m = re.exec(text)) {
    if (m.index > cur) out.push(text.slice(cur, m.index));
    if (m[1] !== undefined) out.push(<strong key={key++}>{m[1]}</strong>);
    else if (m[2] !== undefined) {
      out.push(
        <Box component="code" key={key++}
          sx={{ px: 0.5, py: 0.1, borderRadius: 0.5, bgcolor: "action.hover", fontFamily: "monospace", fontSize: "0.92em" }}>
          {m[2]}
        </Box>,
      );
    } else out.push(<br key={key++} />);
    cur = m.index + m[0].length;
  }
  if (cur < text.length) out.push(text.slice(cur));
  return out;
}

/**
 * API 文档：左导航 / 中正文 / 右调用示例。
 *
 * URL 是过滤词与选中端点的唯一事实来源（`?q=` / `?ep=`）：
 * 可分享、刷新不丢、后退可用，且左栏列表必须渲染**过滤后**的端点——
 * 否则会出现"列表只剩 3 个、正文还是被过滤掉的那一个"。
 */
export default function ApiDoc({ query }: { query: URLSearchParams }): React.ReactElement {
  const initialQ = query.get("q") ?? "";
  const [filter, setFilter] = useState(initialQ);
  const pushed = useRef(initialQ);
  const [copied, setCopied] = useState("");
  const [live, setLive] = useState("");
  const curlPre = useRef<HTMLPreElement | null>(null);

  // 与其他视图一样显式写入面包屑：否则从知识库页切过来时，
  // 上一页的面包屑会留在 API 页顶栏里，还顺带把顶栏撑高 36px。
  useEffect(() => {
    setCrumbs([{ label: "API 文档" }]);
  }, []);

  // URL（后退 / 手敲）→ 本地输入框
  useEffect(() => {
    const fromUrl = query.get("q") ?? "";
    if (fromUrl !== pushed.current) {
      pushed.current = fromUrl;
      setFilter(fromUrl);
    }
  }, [query]);

  // 本地输入框 → URL：debounce，避免每敲一个字就塞一条历史
  useEffect(() => {
    const want = filter.trim();
    const t = setTimeout(() => {
      if (want === pushed.current) return;
      pushed.current = want;
      go(want ? "/apidoc?q=" + encodeURIComponent(want) : "/apidoc");
    }, 300);
    return () => clearTimeout(t);
  }, [filter]);

  const base = typeof location !== "undefined" ? location.origin : "";
  const total = GROUPS.reduce((n, g) => n + g.endpoints.length, 0);
  const needle = filter.trim().toLowerCase();
  const shown = needle
    ? GROUPS.map((g) => ({
        ...g,
        endpoints: g.endpoints.filter((e) =>
          (e.path + e.summary).toLowerCase().includes(needle)),
      })).filter((g) => g.endpoints.length > 0)
    : GROUPS;
  const shownCount = shown.reduce((n, g) => n + g.endpoints.length, 0);

  const selectedKey = query.get("ep") ?? "";
  let selected: { group: (typeof GROUPS)[number]; ep: Endpoint } | null = null;
  for (const g of GROUPS) {
    for (const e of g.endpoints) {
      if (epKey(e) === selectedKey) { selected = { group: g, ep: e }; break; }
    }
    if (selected) break;
  }

  function openEp(e: Endpoint): void {
    const sp = new URLSearchParams();
    if (filter.trim()) sp.set("q", filter.trim());
    sp.set("ep", epKey(e));
    go("/apidoc?" + sp.toString());
  }

  function announce(msg: string): void { setLive(msg); }

  async function copy(text: string, key: string, el?: HTMLElement | null): Promise<void> {
    try {
      await navigator.clipboard.writeText(text);
      announce(`已复制 ${key}`);
    } catch {
      if (el) {
        selectText(el);
        announce("复制失败，已选中，按 Ctrl+C 复制");
      } else {
        announce("复制失败，请手动选择文本");
      }
    }
    setCopied(key);
    setTimeout(() => setCopied((c) => (c === key ? "" : c)), 2000);
  }

  const activeCurl = selected ? curlFor(selected.ep, base) : "";
  const quickEp = GROUPS.flatMap((g) => g.endpoints)
    .find((e) => e.method === "GET" && e.path.endsWith("/kbs"));
  const quickCurl = quickEp ? curlFor(quickEp, base) : "";

  return (
    <Box
      sx={{
        px: { xs: 2, md: 3 },
        py: 2,
        maxWidth: CONTENT_MAX,
        mx: "auto",
        // 桌面端三栏各自滚动：整页不再跟着滚动，高度锁在顶栏以下的
        // 剩余视口里。顶栏高度由壳层实测（面包屑/滚动状态会变）。
        height: { lg: "calc(100% - var(--shell-header-h, 84px))" },
        display: { lg: "flex" },
        flexDirection: "column",
        minHeight: 0,
      }}
    >
      <Box id="apiDocLive" className="sr-only" role="status" aria-live="polite">{live}</Box>

      <Box
        sx={{
          display: "grid",
          gap: 3,
          alignItems: { xs: "start", lg: "stretch" },
          flex: { lg: 1 },
          minHeight: { lg: 0 },
          gridTemplateColumns: {
            xs: "1fr",
            lg: "240px minmax(0, 1fr) 300px",
          },
        }}
      >
        <Box
          component="nav"
          aria-label="接口列表"
          sx={{
            minWidth: 0,
            minHeight: { lg: 0 },
            display: { lg: "flex" },
            flexDirection: { lg: "column" },
            maxHeight: { xs: "42vh", lg: "none" },
            overflowY: { xs: "auto", lg: "hidden" },
            pr: { xs: 1, lg: 0 },
          }}
        >
          {/* 搜索框固定在左栏顶部：它不在下面的滚动容器里。 */}
          <Box sx={{ flexShrink: 0 }}>
            <TextField
              size="small"
              type="search"
              placeholder="搜索接口…"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              fullWidth
              slotProps={{
                htmlInput: { "aria-label": "过滤 API 端点" },
                input: {
                  startAdornment: (
                    <InputAdornment position="start"><SearchIcon fontSize="small" /></InputAdornment>
                  ),
                },
              }}
              helperText={needle ? `${shownCount} / ${total}` : " "}
            />
          </Box>

          {/* 只有列表滚动，搜索框保持不动。 */}
          <Box
            sx={{
              flex: { lg: 1 },
              minHeight: { lg: 0 },
              overflowY: { lg: "auto" },
              pr: { lg: 1 },
            }}
          >
            {shown.map((g) => (
              <Box key={g.id}>
                <ListSubheader disableSticky sx={{ px: 0, bgcolor: "transparent", fontSize: 12 }}>
                  {g.title}
                </ListSubheader>
                <List dense disablePadding>
                  {g.endpoints.map((e) => (
                    <ListItemButton
                      key={epKey(e)}
                      selected={selectedKey === epKey(e)}
                      onClick={() => openEp(e)}
                      title={`${e.method} ${e.path}`}
                      sx={{ borderRadius: 1, py: 0.4 }}
                    >
                      <Chip
                        size="small"
                        variant="outlined"
                        color={METHOD_COLOR[e.method] ?? "default"}
                        label={e.method}
                        sx={{ mr: 1, height: 20, fontSize: 10, minWidth: 46 }}
                      />
                      <ListItemText
                        primary={titleOf(e)}
                        slotProps={{ primary: { noWrap: true, fontSize: 12.5 } }}
                      />
                    </ListItemButton>
                  ))}
                </List>
              </Box>
            ))}
            {shown.length === 0 && (
              <Typography variant="body2" color="text.secondary" sx={{ px: 1, py: 2 }}>
                没有匹配的端点。
              </Typography>
            )}
          </Box>
        </Box>

        <Box
          component="section"
          aria-label="接口正文"
          sx={{
            minWidth: 0,
            minHeight: { lg: 0 },
            overflowY: { lg: "auto" },
            pr: { lg: 2 },
            pb: 6,
          }}
        >
          {selected ? (
            <EndpointDetail
              groupTitle={selected.group.title}
              ep={selected.ep}
              curl={activeCurl}
              copied={copied}
              curlPre={curlPre}
              onCopy={copy}
            />
          ) : (
            <Overview total={total} />
          )}
        </Box>

        <Box
          component="aside"
          aria-label="调用示例"
          sx={{
            display: "flex",
            flexDirection: "column",
            gap: 2,
            minWidth: 0,
            minHeight: { lg: 0 },
            overflowY: { lg: "auto" },
            pr: { lg: 0.5 },
          }}
        >
          {selected ? (
            <>
              <CodeCard title="curl" code={activeCurl} copyKey={`${epKey(selected.ep)} curl`}
                copied={copied} onCopy={copy} preRef={curlPre} />
              {selected.ep.example !== undefined && (
                <CodeCard title="请求体" code={JSON.stringify(selected.ep.example, null, 2)}
                  copyKey={`${epKey(selected.ep)} 示例`} copied={copied} onCopy={copy} />
              )}
              {selected.ep.raw !== undefined && (
                <CodeCard title="响应示例" code={selected.ep.raw}
                  copyKey={`${epKey(selected.ep)} 响应`} copied={copied} onCopy={copy}
                  badge="200" />
              )}
            </>
          ) : (
            <>
              <CodeCard title="Base URL" code={base} />
              {quickEp && (
                <CodeCard title="快速开始" code={quickCurl} copyKey="快速开始"
                  copied={copied} onCopy={copy} />
              )}
            </>
          )}
        </Box>
      </Box>
    </Box>
  );
}

function Overview({ total }: { total: number }): React.ReactElement {
  return (
    <>
      <Typography variant="caption" color="text.secondary">API</Typography>
      <Typography variant="h4" sx={{ fontWeight: 600, mb: 1 }}>API 参考</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ maxWidth: "72ch", lineHeight: 1.8 }}>
        Raggi 对外暴露 {total} 个 HTTP 接口，全部位于 <code>/api</code> 前缀下。
        从左侧选择一个接口查看参数与调用示例；检索与入库可以直接用 curl 调用，无需经过前端。
      </Typography>

      <SectionTitle>鉴权</SectionTitle>
      <Typography variant="body2" color="text.secondary" sx={{ whiteSpace: "pre-wrap", lineHeight: 1.8 }}>
        {inline(AUTH_NOTE)}
      </Typography>

      <SectionTitle>错误约定</SectionTitle>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 1, lineHeight: 1.8 }}>
        错误响应统一为 <code>{'{"detail": "..."}'}</code>，但状态码区分了原因，排查时请以状态码为准：
      </Typography>
      <Table size="small">
        <TableHead>
          <TableRow>
            <TableCell>状态码</TableCell>
            <TableCell>含义</TableCell>
            <TableCell>典型场景</TableCell>
          </TableRow>
        </TableHead>
        <TableBody>
          {[
            ["400", "请求参数有问题", "缺必填字段、URL 协议不允许、指向内网地址"],
            ["401", "未认证", "配置了密钥或 token 但未携带 Authorization 头"],
            ["403", "权限不足", "编辑解析产生的分块但未带 force=true；密钥 scope 不够"],
            ["404", "对象不存在", "文档 / 分块 / 知识库 / 任务 ID 无效"],
            ["413", "上传超限", "文件超过 max_upload_mb"],
            ["500", "服务端错误", "向量维度失配等，detail 保留原始原因"],
            ["503", "依赖不可用", "embedding / LLM 服务未就绪、入库队列已满，或功能开关未启用"],
          ].map(([code, meaning, scene]) => (
            <TableRow key={code}>
              <TableCell sx={{ fontFamily: "monospace" }}>{code}</TableCell>
              <TableCell>{meaning}</TableCell>
              <TableCell sx={{ color: "text.secondary" }}>{scene}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </>
  );
}

function SectionTitle({ children }: { children: ReactNode }): React.ReactElement {
  return (
    <Typography variant="h6" sx={{ fontSize: 16, mt: 4, mb: 1, pb: 0.5, borderBottom: 1, borderColor: "divider" }}>
      {children}
    </Typography>
  );
}

function EndpointDetail({
  groupTitle, ep, copied, curl, curlPre, onCopy,
}: {
  groupTitle: string;
  ep: Endpoint;
  curl: string;
  copied: string;
  curlPre: React.RefObject<HTMLPreElement | null>;
  onCopy: (text: string, key: string, el?: HTMLElement | null) => Promise<void>;
}): React.ReactElement {
  const look = useGlassLook("card");
  const params = ep.params ?? [];
  const groups = IN_ORDER
    .map(([k, label]) => ({ label, params: params.filter((p) => p.in === k) }))
    .filter((g) => g.params.length > 0);

  return (
    <>
      <Typography variant="caption" color="text.secondary">{groupTitle}</Typography>
      <Stack direction="row" spacing={2} alignItems="center" sx={{ mt: 0.5, mb: 0.5, flexWrap: "wrap" }}>
        <Typography variant="h4" sx={{ fontWeight: 600, flex: 1, minWidth: 0 }}>
          {titleOf(ep)}
        </Typography>
        <Button
          size="small"
          variant="outlined"
          startIcon={copied === `${epKey(ep)} curl` ? <CheckIcon /> : <ContentCopyIcon />}
          onClick={() => void onCopy(curl, `${epKey(ep)} curl`, curlPre.current)}
        >
          {copied === `${epKey(ep)} curl` ? "已复制" : "复制 curl"}
        </Button>
      </Stack>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
        {METHOD_TITLE[ep.method] ?? ""}
      </Typography>

      {ep.notes?.length ? (
        <Box component="ul" sx={{ pl: 2.5, my: 1 }}>
          {ep.notes.map((n) => (
            <Typography component="li" variant="body2" key={n} sx={{ mb: 0.5, lineHeight: 1.8 }}>
              {inline(n)}
            </Typography>
          ))}
        </Box>
      ) : null}

      <GlassPanel flat radius={12} look={look} sx={{ p: 1.5, my: 2, display: "flex", alignItems: "center", gap: 1.5, flexWrap: "wrap" }}>
        <Chip size="small" variant="outlined" color={METHOD_COLOR[ep.method] ?? "default"} label={ep.method} />
        <Typography component="code" sx={{ flex: 1, minWidth: 160, fontFamily: "monospace", fontSize: 13.5, wordBreak: "break-all" }}>
          {ep.path}
        </Typography>
        <Button
          size="small"
          variant="contained"
          startIcon={copied === `${epKey(ep)} 路径` ? <CheckIcon /> : <ContentCopyIcon />}
          onClick={() => void onCopy(`${ep.method} ${ep.path}`, `${epKey(ep)} 路径`)}
        >
          {copied === `${epKey(ep)} 路径` ? "已复制" : "复制路径"}
        </Button>
      </GlassPanel>

      <SectionTitle>参数</SectionTitle>
      {groups.length === 0 ? (
        <Typography variant="body2" color="text.secondary">无参数。</Typography>
      ) : groups.map((g) => (
        <Box key={g.label} sx={{ mb: 2 }}>
          <Typography variant="subtitle2" sx={{ color: "text.secondary", mb: 0.5 }}>{g.label}</Typography>
          <Box sx={{ display: "flex", flexDirection: "column" }}>
            {g.params.map((p) => (
              <Box key={p.name} sx={{ py: 1.25, borderBottom: 1, borderColor: "divider" }}>
                <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap">
                  <Typography component="code" sx={{ fontFamily: "monospace", fontSize: 13, color: "primary.main", fontWeight: 500 }}>
                    {p.name}
                  </Typography>
                  <Typography component="span" sx={{ fontFamily: "monospace", fontSize: 12, color: "text.secondary" }}>
                    {p.type}
                  </Typography>
                  {p.required && <Chip size="small" color="error" variant="outlined" label="必填" sx={{ height: 20 }} />}
                </Stack>
                <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5, lineHeight: 1.7 }}>
                  {inline(p.desc)}
                </Typography>
              </Box>
            ))}
          </Box>
        </Box>
      ))}

      {ep.fields?.length ? (
        <>
          <SectionTitle>响应字段</SectionTitle>
          <Box sx={{ display: "flex", flexDirection: "column" }}>
            {ep.fields.map((f) => (
              <Box key={f.name} sx={{ py: 1.25, borderBottom: 1, borderColor: "divider" }}>
                <Stack direction="row" spacing={1} alignItems="center">
                  <Typography component="code" sx={{ fontFamily: "monospace", fontSize: 13, color: "primary.main", fontWeight: 500 }}>
                    {f.name}
                  </Typography>
                  <Typography component="span" sx={{ fontFamily: "monospace", fontSize: 12, color: "text.secondary" }}>
                    {f.type}
                  </Typography>
                </Stack>
                <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5, lineHeight: 1.7 }}>
                  {inline(f.desc)}
                </Typography>
              </Box>
            ))}
          </Box>
        </>
      ) : null}

      {ep.contentType && (
        <Typography variant="body2" color="text.secondary" sx={{ mt: 2 }}>
          响应类型：<code>{ep.contentType}</code>
        </Typography>
      )}
    </>
  );
}

function CodeCard({
  title, code, copyKey, copied, onCopy, preRef, badge,
}: {
  title: string;
  code: string;
  copyKey?: string;
  copied?: string;
  onCopy?: (text: string, key: string, el?: HTMLElement | null) => Promise<void>;
  preRef?: React.RefObject<HTMLPreElement | null>;
  badge?: string;
}): React.ReactElement {
  const look = useGlassLook("card");
  const localRef = useRef<HTMLPreElement | null>(null);
  const ref = preRef ?? localRef;
  const done = !!copyKey && copied === copyKey;
  return (
    <GlassPanel flat radius={12} look={look} className="lg-clip">
      <Stack direction="row" spacing={1} alignItems="center" sx={{ px: 1.5, py: 0.75, borderBottom: 1, borderColor: "divider" }}>
        {badge && <Chip size="small" color="success" variant="outlined" label={badge} sx={{ height: 20 }} />}
        <Typography variant="caption" sx={{ flex: 1, fontWeight: 600 }}>{title}</Typography>
        {copyKey && onCopy && (
          <IconButton
            size="small"
            aria-label={`复制${title}`}
            onClick={() => void onCopy(code, copyKey, ref.current)}
          >
            {done ? <CheckIcon fontSize="small" /> : <ContentCopyIcon fontSize="small" />}
          </IconButton>
        )}
      </Stack>
      <Box
        component="pre"
        ref={ref}
        sx={{
          m: 0, p: 1.5, maxHeight: 420, overflow: "auto",
          fontFamily: "monospace", fontSize: 12, lineHeight: 1.7,
          whiteSpace: "pre",
        }}
      >
        <Box component="code">{code}</Box>
      </Box>
    </GlassPanel>
  );
}
