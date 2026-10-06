<script lang="ts">
  import { api, apiUpload } from "../lib/api";
  import { kbName } from "../lib/stores.svelte";
  import { toast } from "../ui/toast.svelte";
  import Icon from "../ui/Icon.svelte";
  import { stageLabel, waitForJob } from "../lib/jobs";
  import { cancelJob } from "../lib/jobcancel";

  let {
    kbId, open = false, onclose, ondone,
  }: { kbId: string; open?: boolean; onclose: () => void; ondone: () => void } = $props();

  let tab = $state<"file" | "text" | "url">("file");
  let files = $state<File[]>([]);
  let text = $state("");
  let title = $state("");
  let url = $state("");
  let stage = $state("");
  /** 正在轮询的任务 id：有值时「停止入库」按钮才可用 */
  let currentJob = $state("");
  /** 用户点了停止：中止本批剩余文件 */
  let cancelAll = $state(false);
  let cancelled = $state<string[]>([]);
  let busy = $state(false);
  let dragOver = $state(false);
  let panel = $state<HTMLElement | null>(null);

  /**
   * 待入库内容绑定的目标库。
   *
   * 提交时才读当前 kbId 是错的：用户选好文件后切到别的知识库，
   * 内容会静默进错库，界面上没有任何异常。所以在**选择内容时**
   * 就锁定目标。
   */
  let composeKbId = $state("");

  // 打开时把焦点放进面板本身（不聚焦具体控件：常驻焦点框会被
  // 读成"某个按钮被选中"）
  $effect(() => {
    if (open) {
      panel?.focus({ preventScroll: true });
      if (!composeKbId) composeKbId = kbId;
    }
  });

  // 切换知识库时把队列清空：留着上一批文件会让人以为它们属于新库
  $effect(() => {
    const id = kbId;
    if (id !== composeKbId) {
      if (files.length > 0) toast("已切换知识库，待上传队列已清空");
      files = [];
      composeKbId = id;
    }
  });

  function pick(e: Event): void {
    const input = e.currentTarget as HTMLInputElement;
    if (input.files) addFiles([...input.files]);
    input.value = "";
  }

  function addFiles(list: File[]): void {
    composeKbId = kbId;   // 选定即锁定目标
    files = [...files, ...list];
  }

  function drop(e: DragEvent): void {
    e.preventDefault();
    dragOver = false;
    if (e.dataTransfer?.files) addFiles([...e.dataTransfer.files]);
  }

  /**
   * 解析引擎。
   *
   * 以前这个 `<select>` 没有绑定，提交时用 `document.getElementById("engine")?.value`
   * 去读——于是"元素没找到"和"值就是 auto"长得一模一样，静默退回 auto：
   * 用户选了 docling，实际入库用的却是 auto。改成状态后，值只有一个来源，
   * 界面显示的也就是要提交的那个。
   */
  let engine = $state("auto");

  async function uploadFiles(): Promise<void> {
    if (files.length === 0) return;
    const target = composeKbId;
    const batch = files.slice();
    busy = true;
    let done = 0;
    let failed = 0;
    // 只移除成功的；失败的留在队列里供重试——否则用户只能凭记忆
    // 重新挑文件，而这在有几十个文件时几乎做不好
    const succeeded = new Set<File>();

    for (let i = 0; i < batch.length; i++) {
      const file = batch[i];
      const fd = new FormData();
      fd.append("file", file);
      fd.append("kb_id", target);
      fd.append("engine", engine);
      stage = `上传中 ${i + 1}/${batch.length} · 排队中`;
      // 每个文件一个幂等键：网络抖动后重试这一项不会产生第二份文档
      const idem = `ui-${Date.now()}-${i}-${Math.random().toString(36).slice(2, 8)}`;
      try {
        const r = await apiUpload<{
          job_id?: string; doc_id?: string; status?: string;
          idempotent_replay?: boolean;
        }>("/api/documents?wait=false", fd, idem);

        if (r.status === "ready" || r.status === "duplicate") {
          done++; succeeded.add(file);
          continue;
        }
        if (!r.job_id) { done++; succeeded.add(file); continue; }

        currentJob = r.job_id;
        const job = await waitForJob(r.job_id, (j) => {
          stage = `上传中 ${i + 1}/${batch.length} · ${stageLabel(j.stage)}`;
        });
        currentJob = "";
        if (job?.stage === "done") {
          done++; succeeded.add(file);
        } else if (job?.stage === "cancelled") {
          // 用户主动取消：不是失败，也不该留在队列里等重传
          cancelled.push(file.name);
          toast(`「${file.name}」已取消`);
        } else if (job?.stage === "failed") {
          failed++;
          toast(`「${file.name}」失败：${job.error ?? "未知错误"}`, "err");
        } else {
          // 轮询超时：任务可能仍在后台跑，不报成失败，也不确定成功
          toast(`「${file.name}」仍在后台处理，稍后刷新查看`);
        }
      } catch (e) {
        failed++;
        toast(`「${file.name}」失败：${e instanceof Error ? e.message : String(e)}`, "err");
      }
      if (cancelAll) break;
    }

    busy = false;
    stage = "";
    currentJob = "";
    const wasCancelled = cancelAll;
    cancelAll = false;
    if (failed === 0 && !wasCancelled) files = [];
    else files = files.filter((f) => !succeeded.has(f));
    if (done > 0) ondone();
    if (failed > 0) toast(`完成 ${done} 个，${failed} 个失败——失败项已保留，可重新提交`, "err");
    if (cancelled.length > 0) {
      toast(`已取消 ${cancelled.length} 个文件`, "ok");
      cancelled = [];
    }
  }

  /** 取消当前正在入库的文件（以及本批剩余）。 */
  async function cancelCurrent(): Promise<void> {
    if (!currentJob) return;
    cancelAll = true;
    const r = await cancelJob(currentJob);
    // 服务端若回报 running，说明当前阶段还会跑完；界面据此说明
    if (r?.outcome === "running") {
      stage = "正在停止（当前阶段结束后生效）…";
    }
  }

  async function submitText(): Promise<void> {
    const t = text.trim();
    if (!t) return;
    busy = true;
    try {
      await api("/api/documents/text", {
        method: "POST",
        body: { text: t, title: title.trim() || undefined, kb_id: composeKbId || kbId },
      });
      text = ""; title = "";
      toast("已入库");
      ondone();
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), "err");
    } finally {
      busy = false;
    }
  }

  async function submitUrl(): Promise<void> {
    const u = url.trim();
    if (!u) return;
    busy = true;
    try {
      await api("/api/documents/url", {
        method: "POST", body: { url: u, kb_id: composeKbId || kbId },
      });
      url = "";
      toast("已抓取入库");
      ondone();
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), "err");
    } finally {
      busy = false;
    }
  }

  const tabs = [
    { key: "file" as const, label: "上传文件" },
    { key: "text" as const, label: "粘贴文本" },
    { key: "url" as const, label: "从网址抓取" },
  ];
</script>

{#if open}
  <div class="compose" id="compose" bind:this={panel} tabindex="-1">
    <div class="compose-head">
      <h2 class="compose-title">入库</h2>
      <!-- 显式标出目标库：切库时内容会被清空，用户需要知道"入到哪" -->
      <span class="compose-target" id="composeTarget">入库到：{kbName(composeKbId || kbId)}</span>
      <button class="icon-btn" type="button" aria-label="收起入库面板" onclick={onclose}>
        <Icon name="close" />
      </button>
    </div>

    <div class="compose-tabs" role="tablist" aria-label="入库方式">
      {#each tabs as t (t.key)}
        <button class="ctab" role="tab" type="button" aria-selected={tab === t.key}
          onclick={() => (tab = t.key)}>{t.label}</button>
      {/each}
    </div>

    {#if tab === "file"}
      <div class="ctabpanel" role="tabpanel">
        <label class="dropzone" class:is-dragover={dragOver}
          ondragover={(e) => { e.preventDefault(); dragOver = true; }}
          ondragleave={() => (dragOver = false)}
          ondrop={drop}>
          <Icon name="upload" size="ico-lg" />
          <span class="dropzone-title">点击选择文件，或拖拽到此处</span>
          <span class="dropzone-hint">支持 PDF / DOCX / PPTX / HTML / MD / TXT，可多选</span>
          <input type="file" multiple onchange={pick} aria-label="选择要上传的文件" />
        </label>

        {#if files.length > 0}
          <ul class="filelist">
            {#each files as f, i (f.name + f.size + i)}
              <li>
                <Icon name="doc" />
                <span class="filelist-name" title={f.name}>{f.name}</span>
                <span class="muted">{Math.max(1, Math.round(f.size / 1024))} KB</span>
                <button class="icon-btn" type="button" disabled={busy}
                  aria-label="移除 {f.name}"
                  onclick={() => (files = files.filter((_, j) => j !== i))}>
                  <Icon name="close" />
                </button>
              </li>
            {/each}
          </ul>
        {/if}

        <div class="field field-inline">
          <label class="field-label" for="engine">解析引擎</label>
          <select id="engine" bind:value={engine}>
            <option value="auto">auto（自动选择）</option>
            <option value="docling">docling</option>
            <option value="pymupdf4llm">pymupdf4llm</option>
            <option value="unstructured">unstructured</option>
            <option value="native">native</option>
          </select>
          <span class="field-note">切分按本知识库的分块方案执行，可在「设置」里调整。</span>
        </div>
        <div class="form-actions">
          {#if busy}
            <button class="btn btn-primary" type="button" disabled>
              {stage || "正在入库…"}
            </button>
            <button class="btn btn-danger" type="button" disabled={!currentJob}
              onclick={cancelCurrent}>
              停止入库
            </button>
          {:else}
            <button class="btn btn-primary" type="button" disabled={files.length === 0}
              onclick={uploadFiles}>
              {files.length ? `开始入库（${files.length} 个文件）` : "开始入库"}
            </button>
          {/if}
          {#if busy && stage}
            <!-- 取消的能力边界要写在界面上：排队中立即停，运行中要等当前阶段 -->
            <span class="field-note">
              停止后：排队中的文件立即取消；正在解析/向量化的会跑完当前阶段再停。
            </span>
          {/if}
        </div>
      </div>
    {:else if tab === "text"}
      <div class="ctabpanel" role="tabpanel">
        <div class="field">
          <span class="field-label">标题（可选）</span>
          <input type="text" bind:value={title} placeholder="例如：会议纪要 2026-01" autocomplete="off" />
        </div>
        <div class="field">
          <span class="field-label">正文</span>
          <textarea rows="7" bind:value={text} placeholder="粘贴要入库的正文内容"></textarea>
          <span class="field-note">留空标题时自动取正文首行。</span>
        </div>
        <div class="form-actions">
          <button class="btn btn-primary" type="button" disabled={busy || !text.trim()}
            onclick={submitText}>入库为文档</button>
        </div>
      </div>
    {:else}
      <div class="ctabpanel" role="tabpanel">
        <div class="field">
          <span class="field-label">URL</span>
          <input type="url" bind:value={url} placeholder="https://…" autocomplete="off" />
          <span class="field-note">服务端抓取网页正文后入库，适合外部文档。</span>
        </div>
        <div class="form-actions">
          <button class="btn btn-primary" type="button" disabled={busy || !url.trim()}
            onclick={submitUrl}>抓取入库</button>
        </div>
      </div>
    {/if}
  </div>
{/if}

<style>
  /* 焦点容器例外（等效替代：面板获得焦点后内部控件经 Tab 可达，
     整块描框会被读成"面板处于错误状态"） */
  .compose:focus { outline: none; }
</style>
