import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import IconButton from "@mui/material/IconButton";
import Skeleton from "@mui/material/Skeleton";
import Stack from "@mui/material/Stack";
import Tab from "@mui/material/Tab";
import Tabs from "@mui/material/Tabs";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import { alpha } from "@mui/material/styles";
import DownloadIcon from "@mui/icons-material/Download";
import RestartAltIcon from "@mui/icons-material/RestartAlt";

import { api, apiText } from "../lib/api";
import { CONTENT_MAX } from "../lib/layout";
import { GlassPanel, useGlassLook } from "../ui/glass";
import { setCrumbs, useStoreSelect } from "../lib/store";
import { streamPost } from "../lib/sse";
import type { StreamHandle } from "../lib/sse";
import {
  DEFAULTS, MODE_OPTIONS, PARAMS, defaultParams, paramsToBody,
} from "../lib/searchparams";
import { takePendingSearch } from "../lib/searchintent";
import type {
  AnswerHistoryTurn, ChatTurn, Citation, ModelsConfig, SearchResponse,
} from "../lib/types";
import ChatThread from "../ui/ChatThread";
import EvidenceList from "../ui/EvidenceList";
import SearchComposer from "../ui/SearchComposer";
import type { ComposerChip, SearchMode } from "../ui/SearchComposer";
import SearchFilters, { HINTS } from "../ui/SearchFilters";
import { toast } from "../ui/toast";

type MobilePane = "chat" | "evidence";

const CHAT_STATE_KEY = "raggi.chatState.v1";

/**
 * 顶栏之下的剩余视口。
 *
 * 壳层在 `--shell-header-h` 里给出顶栏实测高度（面包屑/工作区标签/滚动
 * 收起都会改变它）。这里若用 100% 或 100vh，工作台会多出一个顶栏的
 * 高度，底部输入台正好被挤出屏幕。
 */
const FILL_HEIGHT = "calc(100% - var(--shell-header-h, 84px))";

interface StoredChat {
  turns: ChatTurn[];
  params: Record<string, string>;
  activeTurnId: string | null;
}

function isStoredTurn(value: unknown): value is ChatTurn {
  if (!value || typeof value !== "object") return false;
  const t = value as Partial<ChatTurn>;
  const statuses = new Set(["searching", "streaming", "done", "error", "stopped"]);
  return typeof t.id === "string"
    && typeof t.question === "string"
    && typeof t.answer === "string"
    && typeof t.status === "string"
    && statuses.has(t.status)
    && Array.isArray(t.citations)
    && Array.isArray(t.hits);
}

/** 只恢复终态轮次；保存时没写完的轮次在加载时标记为已停止。 */
function loadChatState(): StoredChat | null {
  try {
    const raw = sessionStorage.getItem(CHAT_STATE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<StoredChat>;
    if (!parsed || !Array.isArray(parsed.turns)) return null;
    const turns = parsed.turns.filter(isStoredTurn).map((turn) => ({
      ...turn,
      evidenceLoading: false,
      status: turn.status === "searching" || turn.status === "streaming"
        ? "stopped" as const
        : turn.status,
    })).slice(-10);
    if (turns.length === 0) return null;
    const params = {
      ...defaultParams(),
      ...(parsed.params && typeof parsed.params === "object" ? parsed.params : {}),
    };
    const active = typeof parsed.activeTurnId === "string"
      && turns.some((t) => t.id === parsed.activeTurnId)
      ? parsed.activeTurnId
      : turns[turns.length - 1]!.id;
    return { turns, params, activeTurnId: active };
  } catch {
    return null;
  }
}

function saveChatState(
  turns: ChatTurn[], params: Record<string, string>, activeTurnId: string | null,
): void {
  try {
    sessionStorage.setItem(CHAT_STATE_KEY, JSON.stringify({
      turns: turns.slice(-10),
      params,
      activeTurnId,
    }));
  } catch {
    // 配额/隐私模式：聊天仍在内存里，不因持久化失败打断交互
  }
}

function chipLabel(key: string, value: string): string {
  if (key === "origin") return `来源：${value === "manual" ? "手工块" : "解析入库"}`;
  if (key === "group_by_doc") return "按文档去重";
  if (key === "include_disabled") return "含已停用";
  if (key === "highlight") return value === "true" ? "高亮" : "关闭高亮";
  if (key === "rerank") return `精排：${value === "true" ? "强制开" : "强制关"}`;
  const p = PARAMS.find((x) => x.key === key);
  return `${p?.label ?? key}：${value}`;
}

function historyFrom(turns: ChatTurn[]): AnswerHistoryTurn[] {
  const out: AnswerHistoryTurn[] = [];
  for (const turn of turns) {
    if (!turn.question.trim()) continue;
    out.push({ role: "user", content: turn.question.slice(0, 2000) });
    if (turn.answer.trim()) {
      // 服务端单条历史上限 4000；长回答必须截断，否则下一轮会被 422 拒绝。
      out.push({ role: "assistant", content: turn.answer.slice(0, 3900) });
    }
  }
  return out.slice(-8);
}

function modeLabel(mode: string | undefined): string {
  if (!mode) return "";
  return MODE_OPTIONS.find((m) => m.value === mode)?.label ?? mode;
}

function makeTurnId(): string {
  try {
    return crypto.randomUUID();
  } catch {
    return `turn-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  }
}

/**
 * 智能体工作台。
 *
 * 空态只有一个居中的输入台；发送后输入台下移：
 * - 检索：结果占据上方滚动区，输入台固定在底部；
 * - 对话：左侧是带多轮历史的消息流，右侧是本轮证据，输入台在消息流底部。
 *
 * 检索模式下 URL 仍是唯一事实来源；对话模式不共享 URL 状态，避免每轮
 * 追问都改写历史。两种模式共用同一份参数解释与同一套证据渲染。
 */
export default function Search(): React.ReactElement {
  const look = useGlassLook("card");
  const kbs = useStoreSelect((s) => s.kbs);
  // 命令面板可能在本页挂载前就发起了检索，初始化时先取走它的意图。
  const initialSearch = useMemo(takePendingSearch, []);
  const restoredChat = useMemo(loadChatState, []);

  const [viewMode, setViewMode] = useState<SearchMode>("search");
  const [query, setQuery] = useState(initialSearch);
  const [searchDraft, setSearchDraft] = useState(initialSearch);
  const [applied, setApplied] = useState<Record<string, string>>(defaultParams);
  const [chatDraft, setChatDraft] = useState("");
  const [chatParams, setChatParams] = useState<Record<string, string>>(
    () => restoredChat?.params ?? defaultParams(),
  );

  const [searchResp, setSearchResp] = useState<SearchResponse | null>(null);
  const [searchBusy, setSearchBusy] = useState(false);
  const [searchError, setSearchError] = useState("");
  const [searchExpanded, setSearchExpanded] = useState<string | null>(null);

  const [chatTurns, setChatTurns] = useState<ChatTurn[]>(() => restoredChat?.turns ?? []);
  const [activeTurnId, setActiveTurnId] = useState<string | null>(
    () => restoredChat?.activeTurnId ?? null,
  );
  const [chatExpanded, setChatExpanded] = useState<string | null>(null);
  const [chatRunningId, setChatRunningId] = useState<string | null>(null);
  const [focusChunk, setFocusChunk] = useState<string | null>(null);
  const [mobilePane, setMobilePane] = useState<MobilePane>("chat");

  const [answerEnabled, setAnswerEnabled] = useState(false);
  const [serverDefaults, setServerDefaults] = useState<Record<string, string>>({});
  const [filterOpen, setFilterOpen] = useState(false);
  const [draft, setDraft] = useState<Record<string, string>>(defaultParams);

  const inputRef = useRef<HTMLInputElement | null>(null);
  const searchSeq = useRef(0);
  const searchScrollRef = useRef<HTMLDivElement | null>(null);

  const chatStream = useRef<StreamHandle | null>(null);
  const chatSearchCtrl = useRef<AbortController | null>(null);
  const chatBuffer = useRef("");
  const chatRaf = useRef<number | null>(null);
  const chatTurnRef = useRef<string | null>(null);
  // 表单动作在每次渲染时更新，但传给 memo 化输入台的引用保持稳定：
  // 否则流式回答每帧都会把整个输入台（含两个浮层选择器）重渲染一遍。
  const sendRef = useRef<() => void>(() => {});
  const modeRef = useRef<(mode: SearchMode) => void>(() => {});
  const stopRef = useRef<() => void>(() => {});
  const newChatRef = useRef<() => void>(() => {});

  const currentParams = viewMode === "search" ? applied : chatParams;

  const dirty = useMemo(
    () => PARAMS.filter((p) => (draft[p.key] ?? "") !== (currentParams[p.key] ?? "")).length,
    [draft, currentParams],
  );

  const extraChips = useMemo<ComposerChip[]>(() => (
    PARAMS
      .filter((p) => p.key !== "kb_id" && p.key !== "mode")
      .map((p) => [p.key, currentParams[p.key] ?? ""] as const)
      .filter(([key, value]) => value !== "" && value !== (DEFAULTS[key] ?? ""))
      .map(([key, value]) => ({
        key,
        label: chipLabel(key, value),
        title: HINTS[key],
      }))
  ), [currentParams]);

  const activeTurn = useMemo(() => {
    if (chatTurns.length === 0) return null;
    return chatTurns.find((t) => t.id === activeTurnId) ?? chatTurns[chatTurns.length - 1];
  }, [chatTurns, activeTurnId]);

  const searchActive = viewMode === "search" && query.trim() !== "";
  const chatActive = viewMode === "chat" && chatTurns.length > 0;
  const idle = !searchActive && !chatActive;

  useEffect(() => {
    setCrumbs([{ label: "检索" }]);
    void api<ModelsConfig>("/api/models")
      .then((m) => {
        setAnswerEnabled(m.features?.answer === true);
        const next: Record<string, string> = {};
        for (const [key, value] of Object.entries(m.retrieve ?? {})) {
          if (value !== null && value !== undefined && typeof value !== "object") {
            next[key] = String(value);
          }
        }
        setServerDefaults(next);
      })
      .catch(() => setAnswerEnabled(false));
  }, []);

  useEffect(() => {
    if (!answerEnabled && viewMode === "chat") setViewMode("search");
  }, [answerEnabled, viewMode]);

  // 顶栏点「检索」/命令面板打开检索：清空上一次查询，回到空态。
  useEffect(() => {
    const onNew = (): void => {
      takePendingSearch();   // 丢弃未消费的旧意图，避免下次挂载被它触发
      newSearch();
    };
    const onIntent = (e: Event): void => {
      const q = (e as CustomEvent<{ q?: string }>).detail?.q ?? "";
      takePendingSearch();   // 事件已送达，清掉兜底的 pending
      if (!q.trim()) return;
      setViewMode("search");
      setSearchDraft(q);
      setQuery(q);
    };
    window.addEventListener("raggi:newSearch", onNew);
    window.addEventListener("raggi:search", onIntent);
    return () => {
      window.removeEventListener("raggi:newSearch", onNew);
      window.removeEventListener("raggi:search", onIntent);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // `/` 聚焦输入台；在移动端的证据页先切回对话页。
  useEffect(() => {
    const onKey = (e: KeyboardEvent): void => {
      const t = e.target as HTMLElement | null;
      if (t && (/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || t.isContentEditable)) return;
      if (t && t.closest('[role="dialog"]')) return;
      if (e.key === "/" && !e.ctrlKey && !e.metaKey) {
        e.preventDefault();
        setMobilePane("chat");
        requestAnimationFrame(() => inputRef.current?.focus());
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const runSearch = useCallback(async (signal?: AbortSignal): Promise<void> => {
    const seq = ++searchSeq.current;
    if (!query.trim()) {
      setSearchResp(null);
      setSearchExpanded(null);
      setSearchError("");
      setSearchBusy(false);
      return;
    }
    setSearchBusy(true);
    setSearchError("");
    setSearchResp(null);
    setSearchExpanded(null);
    try {
      const r = await api<SearchResponse>("/api/search", {
        method: "POST",
        body: paramsToBody(query, applied),
        signal,
      });
      if (seq !== searchSeq.current) return;
      setSearchResp(r);
      requestAnimationFrame(() => searchScrollRef.current?.scrollTo({ top: 0 }));
    } catch (e) {
      if (seq !== searchSeq.current) return;
      if (signal?.aborted || (e instanceof DOMException && e.name === "AbortError")) return;
      setSearchError(e instanceof Error ? e.message : String(e));
    } finally {
      if (seq === searchSeq.current) setSearchBusy(false);
    }
  }, [query, applied]);

  useEffect(() => {
    const ctrl = new AbortController();
    void runSearch(ctrl.signal);
    return () => ctrl.abort();
  }, [runSearch]);

  // 组件离开时断开正在进行的证据检索与生成流。
  useEffect(() => () => {
    chatStream.current?.stop();
    chatSearchCtrl.current?.abort();
    if (chatRaf.current != null) cancelAnimationFrame(chatRaf.current);
  }, []);

  // 对话历史只写本机 sessionStorage：刷新/切换页面后可恢复，但不改 URL、
  // 不跨浏览器会话；生成中的轮次不落盘，避免恢复到半截状态。
  useEffect(() => {
    if (chatTurns.length === 0) {
      try { sessionStorage.removeItem(CHAT_STATE_KEY); } catch { /* 隐私模式 */ }
      return;
    }
    if (chatTurns.some((t) => t.status === "searching" || t.status === "streaming")) return;
    const timer = setTimeout(
      () => saveChatState(chatTurns, chatParams, activeTurnId),
      250,
    );
    return () => clearTimeout(timer);
  }, [chatTurns, chatParams, activeTurnId]);

  function newSearch(): void {
    setViewMode("search");
    setQuery("");
    setSearchDraft("");
    setSearchResp(null);
    setSearchError("");
    setSearchExpanded(null);
    setFilterOpen(false);
  }

  function submitSearch(): void {
    const text = searchDraft.trim();
    if (!text) return;
    if (text === query) {
      // 同一个问题再按一次：原地重查，而不是被状态相等吃掉。
      void runSearch();
      return;
    }
    setQuery(text);
  }

  function changeMode(next: SearchMode): void {
    if (next === viewMode) return;
    if (next === "chat" && !answerEnabled) return;
    if (chatRunningId) stopChat();
    if (next === "chat") {
      if (chatTurns.length === 0) {
        setChatParams({ ...applied });
        if (!chatDraft && query) setChatDraft(query);
      }
      setMobilePane("chat");
    }
    setViewMode(next);
    requestAnimationFrame(() => inputRef.current?.focus());
  }

  const updateParam = useCallback((key: string, value: string): void => {
    if (viewMode === "search") {
      setApplied((prev) => ({ ...prev, [key]: value }));
      return;
    }
    setChatParams((prev) => ({ ...prev, [key]: value }));
  }, [viewMode]);

  const removeChip = useCallback((key: string): void => {
    updateParam(key, DEFAULTS[key] ?? "");
  }, [updateParam]);

  const handleKbChange = useCallback((value: string): void => {
    updateParam("kb_id", value);
  }, [updateParam]);

  const handleRetrievalModeChange = useCallback((mode: string): void => {
    updateParam("mode", mode);
  }, [updateParam]);

  const openFilters = useCallback((): void => {
    setDraft({ ...currentParams });
    setFilterOpen(true);
  }, [currentParams]);

  function applyFilters(): void {
    if (viewMode === "search") {
      setApplied({ ...draft });
    } else {
      setChatParams({ ...draft });
      if (chatTurns.length > 0) toast("检索条件已更新，下一条消息生效");
    }
    setFilterOpen(false);
  }

  async function exportReport(): Promise<void> {
    try {
      const md = await apiText("/api/search/report", {
        method: "POST",
        body: paramsToBody(query, applied),
      });
      const blob = new Blob([md], { type: "text/markdown;charset=utf-8" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `检索报告-${new Date().toISOString().slice(0, 10)}.md`;
      a.click();
      URL.revokeObjectURL(a.href);
      toast("报告已导出");
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), "err");
    }
  }

  const patchTurn = useCallback((id: string, patch: Partial<ChatTurn>): void => {
    setChatTurns((prev) => prev.map((t) => (t.id === id ? { ...t, ...patch } : t)));
  }, []);

  function flushChatAnswer(): void {
    if (chatRaf.current != null) return;
    chatRaf.current = requestAnimationFrame(() => {
      chatRaf.current = null;
      const id = chatTurnRef.current;
      if (!id) return;
      patchTurn(id, { answer: chatBuffer.current });
    });
  }

  function runTurn(
    turnId: string,
    question: string,
    history: AnswerHistoryTurn[],
  ): void {
    chatSearchCtrl.current?.abort();
    const searchCtrl = new AbortController();
    chatSearchCtrl.current = searchCtrl;

    chatBuffer.current = "";
    chatTurnRef.current = turnId;
    setChatRunningId(turnId);

    const params = { ...chatParams };
    const baseBody = paramsToBody(question, params);
    const searchBody = { ...baseBody };
    const answerBody = {
      ...baseBody,
      // 问答接口的 top_k 上限是 50（会把命中全文拼进提示词），
      // 检索接口允许更大；对话里同时受两者约束。
      top_k: Math.min(50, Number(params.top_k) || 8),
      history,
    };

    void api<SearchResponse>("/api/search", {
      method: "POST",
      body: searchBody,
      signal: searchCtrl.signal,
    })
      .then((r) => {
        if (searchCtrl.signal.aborted) return;
        patchTurn(turnId, {
          hits: r.results,
          searchTookMs: r.took_ms,
          mode: r.mode,
          scoreKind: r.score_kind,
          degradedReason: r.degraded_reason,
          searchError: undefined,
          evidenceLoading: false,
        });
      })
      .catch((e) => {
        if (searchCtrl.signal.aborted) return;
        patchTurn(turnId, {
          searchError: e instanceof Error ? e.message : String(e),
          evidenceLoading: false,
        });
      })
      .finally(() => {
        if (chatSearchCtrl.current === searchCtrl) chatSearchCtrl.current = null;
        patchTurn(turnId, { evidenceLoading: false });
      });

    chatStream.current = streamPost("/api/answer/stream", answerBody, {
      onSources: (items) => {
        patchTurn(turnId, { citations: items as Citation[] });
      },
      onDelta: (text) => {
        chatBuffer.current += text;
        // 状态只在第一段增量时翻转一次。改前每个 delta 都 map 整个
        // 轮次数组（返回新数组 → 整页重渲染），而正文的实际刷新已经
        // 由下面的 rAF 合并到每帧一次。
        setChatTurns((prev) => {
          const current = prev.find((t) => t.id === turnId);
          if (!current || current.status !== "searching") return prev;
          return prev.map((t) => (
            t.id === turnId ? { ...t, status: "streaming" } : t
          ));
        });
        flushChatAnswer();
      },
      onDone: (done) => {
        if (chatRaf.current != null) {
          cancelAnimationFrame(chatRaf.current);
          chatRaf.current = null;
        }
        patchTurn(turnId, {
          answer: chatBuffer.current,
          status: "done",
          tookMs: done.took_ms,
          mode: done.mode || undefined,
          degradedReason: done.degraded_reason,
        });
        chatTurnRef.current = null;
        chatStream.current = null;
        setChatRunningId(null);
      },
      onError: (message, hint) => {
        if (chatRaf.current != null) {
          cancelAnimationFrame(chatRaf.current);
          chatRaf.current = null;
        }
        patchTurn(turnId, {
          answer: chatBuffer.current,
          status: "error",
          error: hint ? `${message}（${hint}）` : message,
        });
        chatTurnRef.current = null;
        chatStream.current = null;
        setChatRunningId(null);
      },
    });
  }

  function sendChat(): void {
    const text = chatDraft.trim();
    if (!text || !answerEnabled || chatRunningId) return;
    const id = makeTurnId();
    const history = historyFrom(chatTurns);
    const turn: ChatTurn = {
      id,
      question: text,
      answer: "",
      citations: [],
      hits: [],
      status: "searching",
      evidenceLoading: true,
    };
    setChatTurns((prev) => [...prev, turn]);
    setActiveTurnId(id);
    setChatExpanded(null);
    setFocusChunk(null);
    setChatDraft("");
    setMobilePane("chat");
    runTurn(id, text, history);
  }

  function retryTurn(turnId: string): void {
    if (chatRunningId) return;
    const index = chatTurns.findIndex((t) => t.id === turnId);
    if (index < 0) return;
    const turn = chatTurns[index]!;
    const history = historyFrom(chatTurns.slice(0, index));
    patchTurn(turnId, {
      answer: "",
      citations: [],
      hits: [],
      status: "searching",
      evidenceLoading: true,
      error: undefined,
      searchError: undefined,
      searchTookMs: undefined,
      tookMs: undefined,
      scoreKind: undefined,
      degradedReason: undefined,
    });
    setActiveTurnId(turnId);
    setChatExpanded(null);
    setFocusChunk(null);
    runTurn(turnId, turn.question, history);
  }

  function stopChat(): void {
    chatStream.current?.stop();
    chatStream.current = null;
    chatSearchCtrl.current?.abort();
    chatSearchCtrl.current = null;
    if (chatRaf.current != null) {
      cancelAnimationFrame(chatRaf.current);
      chatRaf.current = null;
    }
    const id = chatTurnRef.current ?? chatRunningId;
    if (id) {
      patchTurn(id, {
        answer: chatBuffer.current,
        status: "stopped",
        evidenceLoading: false,
      });
    }
    chatTurnRef.current = null;
    setChatRunningId(null);
  }

  function newChat(): void {
    if (chatRunningId) stopChat();
    setChatTurns([]);
    setActiveTurnId(null);
    setChatExpanded(null);
    setChatDraft("");
    setFocusChunk(null);
    setMobilePane("chat");
    try { sessionStorage.removeItem(CHAT_STATE_KEY); } catch { /* 隐私模式 */ }
  }

  function selectTurn(turnId: string): void {
    setActiveTurnId(turnId);
    if (turnId !== activeTurnId) setChatExpanded(null);
    setFocusChunk(null);
    setMobilePane("evidence");
  }

  function revealCitation(turnId: string, citation: Citation): void {
    const turn = chatTurns.find((t) => t.id === turnId);
    if (turn && !turn.evidenceLoading && !turn.hits.some((h) => h.chunk_id === citation.chunk_id)) {
      toast("该引用不在本轮证据列表中", "err");
    }
    setViewMode("chat");
    setActiveTurnId(turnId);
    setChatExpanded(citation.chunk_id);
    setFocusChunk(citation.chunk_id);
    setMobilePane("evidence");
  }

  const handleFocusHandled = useCallback(() => setFocusChunk(null), []);
  const handleSend = useCallback(() => sendRef.current(), []);
  const handleModeChange = useCallback((next: SearchMode) => modeRef.current(next), []);
  const handleStop = useCallback(() => stopRef.current(), []);
  const handleNewChat = useCallback(() => newChatRef.current(), []);
  const toggleSearchExpanded = useCallback(
    (id: string) => setSearchExpanded((cur) => (cur === id ? null : id)), []);
  const toggleChatExpanded = useCallback(
    (id: string) => setChatExpanded((cur) => (cur === id ? null : id)), []);

  // 每次提交后把最新实现交给稳定入口；引用赋值放 effect 里，
  // 避免在渲染阶段写入 ref（并发渲染下可能指向未提交的状态）。
  useEffect(() => {
    modeRef.current = changeMode;
    stopRef.current = stopChat;
    newChatRef.current = newChat;
    sendRef.current = viewMode === "search" ? submitSearch : sendChat;
  });

  const filtersPanel = (
    <SearchFilters
      open={filterOpen}
      onClose={() => setFilterOpen(false)}
      draft={draft}
      kbs={kbs}
      serverDefaults={serverDefaults}
      dirty={dirty}
      mode={viewMode}
      onChange={(key, value) => setDraft((prev) => ({ ...prev, [key]: value }))}
      onReset={() => setDraft({ ...DEFAULTS })}
      onRevert={() => setDraft({ ...currentParams })}
      onApply={applyFilters}
    />
  );

  const composer = (compact: boolean): React.ReactElement => (
    <SearchComposer
      mode={viewMode}
      onModeChange={handleModeChange}
      value={viewMode === "search" ? searchDraft : chatDraft}
      onValueChange={viewMode === "search" ? setSearchDraft : setChatDraft}
      onSend={handleSend}
      onStop={viewMode === "chat" && chatRunningId ? handleStop : undefined}
      running={viewMode === "search" ? searchBusy : chatRunningId != null}
      compact={compact}
      answerEnabled={answerEnabled}
      kbValue={currentParams.kb_id ?? ""}
      retrievalMode={currentParams.mode || "hybrid"}
      onKbChange={handleKbChange}
      onRetrievalModeChange={handleRetrievalModeChange}
      onOpenFilters={openFilters}
      onNewChat={viewMode === "chat" && chatTurns.length > 0 ? handleNewChat : undefined}
      chips={extraChips}
      onRemoveChip={removeChip}
      kbs={kbs}
      inputRef={inputRef}
    />
  );

  // ---- 空态：页面中央只有输入台 ----
  if (idle) {
    return (
      <>
        <Box
          sx={{
            height: FILL_HEIGHT,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            px: { xs: 2, md: 4 },
            py: 4,
            position: "relative",
            overflow: "hidden",
          }}
        >
          {/*
            底色由壳层统一提供（`ui/glass` 的 shell 形态），这里不再
            自己铺一层——两片光叠在一起，顶栏和页面就接不上了。
          */}
          <Box sx={{ width: "100%", maxWidth: 780, position: "relative" }}>
            {composer(false)}
          </Box>
        </Box>
        {filtersPanel}
      </>
    );
  }

  // ---- 检索：结果在上，输入台固定在底部 ----
  if (searchActive) {
    return (
      <>
      <Box
        sx={{
          height: FILL_HEIGHT,
          display: "flex",
          flexDirection: "column",
          minHeight: 0,
          backgroundImage: (t) => `radial-gradient(1000px 360px at 50% -120px, `
            + `${alpha(t.palette.primary.main, 0.08)}, transparent 70%)`,
          backgroundRepeat: "no-repeat",
        }}
      >
        <Box ref={searchScrollRef} sx={{ flex: 1, minHeight: 0, overflowY: "auto" }}>
          <Box sx={{ maxWidth: CONTENT_MAX, mx: "auto", px: { xs: 2, md: 3 }, py: { xs: 2, md: 3 } }}>
            <Stack
              direction="row"
              spacing={2}
              alignItems="flex-start"
              sx={{ mb: 1.5 }}
            >
              <Box sx={{ flex: 1, minWidth: 0 }}>
                <Typography variant="subtitle2" color="text.secondary">
                  检索结果
                </Typography>
                <Typography variant="h5" sx={{ fontWeight: 650, mt: 0.25, wordBreak: "break-word" }}>
                  “{query}”
                </Typography>
                {searchResp && (
                  <Typography variant="body2" color="text.secondary" aria-live="polite">
                    显示 {searchResp.results.length} 条 · 候选 {searchResp.total_candidates} 条
                    {" · "}{searchResp.took_ms}ms · {modeLabel(searchResp.mode)} · {searchResp.score_kind}
                  </Typography>
                )}
              </Box>
              <Button
                size="small"
                startIcon={<RestartAltIcon fontSize="small" />}
                onClick={newSearch}
                sx={{ borderRadius: 999, color: "text.secondary" }}
              >
                新检索
              </Button>
              <Tooltip title="导出 Markdown 检索报告">
                <span>
                  <IconButton
                    aria-label="导出检索报告"
                    onClick={() => void exportReport()}
                    disabled={!searchResp || searchResp.results.length === 0}
                  >
                    <DownloadIcon />
                  </IconButton>
                </span>
              </Tooltip>
            </Stack>

            {searchError && (
              <Alert
                severity="error"
                action={<Button color="inherit" size="small" onClick={() => void runSearch()}>重试</Button>}
              >
                {searchError}
              </Alert>
            )}

            {!searchResp && !searchError && (
              <Stack spacing={1.5} aria-label="正在检索">
                <Typography variant="body2" color="text.secondary">正在检索…</Typography>
                {[0, 1, 2, 3].map((i) => (
                  <Skeleton key={i} variant="rounded" height={i === 0 ? 92 : 78} sx={{ borderRadius: 2 }} />
                ))}
              </Stack>
            )}

            {searchResp?.degraded_reason && (
              <Alert severity="warning" sx={{ mb: 1.5 }}>
                检索已降级：{searchResp.degraded_reason}
              </Alert>
            )}

            {searchResp?.results.length === 0 && (
              <GlassPanel
                radius={16} look={look}
                sx={{
                  px: { xs: 2.5, md: 4 },
                  py: { xs: 3, md: 5 },
                }}
              >
                <Typography variant="h6" sx={{ fontWeight: 550 }}>
                  没有找到直接匹配的证据
                </Typography>
                <Typography variant="body2" color="text.secondary" sx={{ mt: 1, maxWidth: 620 }}>
                  先确认范围是否过窄，再尝试更短的词组、同义表述，或调低分数阈值。
                </Typography>
                <Button variant="outlined" size="small" onClick={openFilters} sx={{ mt: 2.5 }}>
                  调整条件
                </Button>
              </GlassPanel>
            )}
 
            {searchResp && searchResp.results.length > 0 && (
              <>
                <Stack
                  direction="row"
                  justifyContent="space-between"
                  alignItems="center"
                  sx={{ mb: 0.5, gap: 2, flexWrap: "wrap" }}
                >
                  <Typography variant="caption" color="text.secondary">
                    点击任意一条展开证据；↑ ↓ 切换，Enter 展开，Esc 收起
                  </Typography>
                  {searchResp.has_more && (
                    <Button size="small" onClick={openFilters} sx={{ minWidth: 0 }}>
                      还有更多候选 · 调整返回条数
                    </Button>
                  )}
                </Stack>
                <EvidenceList
                  hits={searchResp.results}
                  expanded={searchExpanded}
                  onToggle={toggleSearchExpanded}
                />
              </>
            )}
          </Box>
        </Box>

        <Box
          sx={{
            flex: "none",
            px: { xs: 2, md: 4 },
            pt: 1,
            pb: { xs: "calc(10px + env(safe-area-inset-bottom))", md: 1.5 },
            backgroundImage: (t) => `linear-gradient(180deg, `
              + `${alpha(t.palette.background.default, 0)}, `
              + `${t.palette.background.default} 55%)`,
          }}
        >
          <Box sx={{ maxWidth: 900, mx: "auto" }}>{composer(true)}</Box>
        </Box>
      </Box>
      {filtersPanel}
      </>
    );
  }

  // ---- 对话：左侧消息流，右侧本轮证据，输入台在左侧底部 ----
  const activeHits = activeTurn?.hits ?? [];
  return (
    <>
    <Box sx={{ height: FILL_HEIGHT, display: "flex", flexDirection: "column", minHeight: 0 }}>
      <Tabs
        value={mobilePane}
        onChange={(_e, value: MobilePane) => setMobilePane(value)}
        variant="fullWidth"
        sx={{
          display: { xs: "flex", lg: "none" },
          borderBottom: 1,
          borderColor: "divider",
          minHeight: 44,
        }}
      >
        <Tab value="chat" label="对话" sx={{ minHeight: 44 }} />
        <Tab
          value="evidence"
          label={`证据${activeHits.length > 0 ? ` (${activeHits.length})` : ""}`}
          sx={{ minHeight: 44 }}
        />
      </Tabs>

      <Box
        sx={{
          flex: 1,
          minHeight: 0,
          width: "100%",
          maxWidth: CONTENT_MAX,
          mx: "auto",
          display: "grid",
          gridTemplateColumns: { xs: "minmax(0,1fr)", lg: "minmax(0,1.08fr) minmax(0,.92fr)" },
        }}
      >
        <Box
          sx={{
            minHeight: 0,
            display: { xs: mobilePane === "chat" ? "flex" : "none", lg: "flex" },
            flexDirection: "column",
            borderRight: { lg: 1 },
            borderColor: "divider",
          }}
        >
          <Box sx={{ flex: 1, minHeight: 0 }}>
            <ChatThread
              turns={chatTurns}
              activeTurnId={activeTurn?.id ?? null}
              runningTurnId={chatRunningId}
              onSelectTurn={selectTurn}
              onStop={stopChat}
              onRetry={retryTurn}
              onCitation={revealCitation}
            />
          </Box>
          <Box
            sx={{
              flex: "none",
              px: { xs: 1.5, sm: 2.5 },
              pt: 1,
              pb: { xs: "calc(10px + env(safe-area-inset-bottom))", md: 1.5 },
              backgroundImage: (t) => `linear-gradient(180deg, `
                + `${alpha(t.palette.background.default, 0)}, `
                + `${t.palette.background.default} 60%)`,
            }}
          >
            {composer(true)}
          </Box>
        </Box>

        <Box
          sx={{
            minHeight: 0,
            overflowY: "auto",
            display: { xs: mobilePane === "evidence" ? "block" : "none", lg: "block" },
            bgcolor: (t) => alpha(t.palette.primary.main, 0.02),
            borderLeft: { lg: 1 },
            borderColor: "divider",
          }}
        >
          <Box sx={{ px: { xs: 2, md: 2.5 }, py: 2.5 }}>
            <Typography variant="subtitle2" color="text.secondary">
              本轮证据
            </Typography>
            <Typography variant="h6" sx={{ fontWeight: 600, mt: 0.25, wordBreak: "break-word" }}>
              {activeTurn?.question ?? ""}
            </Typography>
            <Typography variant="caption" color="text.secondary" aria-live="polite">
              {activeTurn?.evidenceLoading && activeHits.length === 0
                ? "正在检索…"
                : `${activeHits.length} 条证据`
                  + (activeTurn?.searchTookMs != null ? ` · ${activeTurn.searchTookMs}ms` : "")
                  + (activeTurn?.mode ? ` · ${modeLabel(activeTurn.mode)}` : "")
                  + (activeTurn?.scoreKind ? ` · ${activeTurn.scoreKind}` : "")}
            </Typography>

            {activeTurn?.searchError && (
              <Alert severity="warning" sx={{ mt: 1.5 }}>
                证据检索失败：{activeTurn.searchError}
              </Alert>
            )}

            {activeTurn?.evidenceLoading && activeHits.length === 0 && (
              <Stack spacing={1.25} sx={{ mt: 2 }}>
                {[0, 1, 2].map((i) => (
                  <Skeleton key={i} variant="rounded" height={84} sx={{ borderRadius: 2 }} />
                ))}
              </Stack>
            )}

            {activeTurn && activeHits.length === 0
              && !activeTurn.evidenceLoading
              && !activeTurn.searchError && (
                <Box sx={{ mt: 2, py: 3 }}>
                  <Typography variant="body2" color="text.secondary">
                    {activeTurn.status === "stopped"
                      ? "已停止生成，证据检索也已被取消。"
                      : "这一轮没有检索到证据，回答可能缺少依据。"}
                  </Typography>
                </Box>
              )}

            {activeHits.length > 0 && (
              <Box sx={{ mt: 1.5 }}>
                <EvidenceList
                  hits={activeHits}
                  compact
                  expanded={chatExpanded}
                  onToggle={toggleChatExpanded}
                  focusChunk={focusChunk}
                  onFocusHandled={handleFocusHandled}
                  ariaLabel="本轮证据"
                />
              </Box>
            )}

            {!activeTurn && (
              <GlassPanel
                radius={12} look={look}
                sx={{
                  mt: 2,
                  px: 2.5,
                  py: 4,
                  textAlign: "center",
                }}
              >
                <Typography variant="body2" color="text.disabled">
                  发送消息后，本轮检索到的证据会出现在这里。
                </Typography>
              </GlassPanel>
            )}
          </Box>
        </Box>
      </Box>
    </Box>
    {filtersPanel}
    </>
  );
}
