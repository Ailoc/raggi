<!--
  结构化高亮渲染：把后端返回的 {text, marks} 渲染成带 <mark> 的纯文本。

  **为什么不用 {@html}**：早前 snippet 是后端拼好的 HTML 字符串，前端
  必须用 {@html} 注入，安全完全依赖后端每次都记得转义。入库文本来自
  URL 抓取（任意网页正文），漏一次转义就是存储型 XSS。

  这里用 Svelte 原生插值渲染文本（自动转义），高亮区间由后端给出，
  因此安全由语言本身保证，而不是由每个人记住调用 escape。
-->
<script lang="ts">
  /** 片段：纯文本 + 高亮区间。marks 为相对 text 的 [start, end) 列表。 */
  interface Snippet {
    text: string;
    marks: [number, number][] | number[][];
  }

  let {
    snippet,
    class: className = "",
  }: { snippet?: Snippet | null; class?: string } = $props();

  type Part = { text: string; hit: boolean };

  /**
   * 把 text 按 marks 切成「普通 / 高亮」交替的片段序列。
   *
   * 越界或乱序的区间直接丢弃，不抛错：后端契约是纯数据，界面不该
   * 因为一个异常区间就白屏或把整段文字吞掉。
   */
  const parts = $derived.by((): Part[] => {
    const text = snippet?.text ?? "";
    const marks = (snippet?.marks ?? [])
      .filter((m): m is number[] => Array.isArray(m) && m.length >= 2)
      .map((m) => [Number(m[0]), Number(m[1])] as [number, number])
      .filter(([s, e]) =>
        Number.isFinite(s) && Number.isFinite(e) && s >= 0 && e > s && e <= text.length)
      .sort((a, b) => a[0] - b[0]);

    if (!marks.length) return text ? [{ text, hit: false }] : [];

    const out: Part[] = [];
    let cursor = 0;
    for (const [s, e] of marks) {
      // 重叠区间：跳过已消费部分，避免文字重复或 <mark> 嵌套
      if (s < cursor) continue;
      if (s > cursor) out.push({ text: text.slice(cursor, s), hit: false });
      out.push({ text: text.slice(s, e), hit: true });
      cursor = e;
    }
    if (cursor < text.length) out.push({ text: text.slice(cursor), hit: false });
    return out;
  });
</script>

{#if parts.length}
  <span class={className}
    >{#each parts as p, i (i)}{#if p.hit}<mark>{p.text}</mark>{:else}{p.text}{/if}{/each}</span
  >
{/if}

<style>
  /* 高亮只改颜色/背景，不改布局 —— 与原 {@html} 版的观感一致 */
  mark {
    background: var(--mark-bg, rgba(255, 214, 102, 0.28));
    color: inherit;
    padding: 0 1px;
    border-radius: 2px;
  }
</style>