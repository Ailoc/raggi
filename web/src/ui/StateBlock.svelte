<script lang="ts">
  /**
   * 空态 / 错误态 / 加载态的统一呈现。
   *
   * 旧实现用字符串拼 HTML，于是「错误态忘了给重试按钮」这种遗漏
   * 只能靠人眼发现。组件化后，传了 onretry 就一定有按钮。
   */
  import type { Snippet } from "svelte";
  import Icon from "./Icon.svelte";
  import type { IconName } from "./icons";

  interface Props {
    kind?: "empty" | "error";
    title: string;
    text?: string;
    action?: string;
    onaction?: () => void;
    /** 缺省按状态选图标；特殊空态（如"还没有密钥"）可指定 */
    icon?: IconName;
    children?: Snippet;
  }
  let {
    kind = "empty", title, text = "", action, onaction, icon, children,
  }: Props = $props();
</script>

<div class="state state-{kind}">
  {#if children}
    {@render children()}
  {/if}
  <span class="state-ico"><Icon name={icon ?? (kind === "error" ? "alert" : "files")} size="ico-lg" /></span>
  <p class="state-title">{title}</p>
  {#if text}<p class="state-text">{text}</p>{/if}
  {#if action && onaction}
    <button class="btn" type="button" onclick={onaction}>{action}</button>
  {/if}
</div>
