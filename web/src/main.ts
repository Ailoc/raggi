import { mount } from "svelte";
import App from "./App.svelte";

/**
 * 样式加载顺序是有意义的，别按字母排序它：
 *
 * - `app.css`：遗留层（9 个未迁移视图仍依赖其中的组件样式与 --amber*）
 * - `tokens.css`：语义令牌层。它与 app.css 同为 `:root` 选择器，
 *   靠"后定义者胜"覆盖旧的暖中性与琥珀别名——顺序错了整个配色就退回去了
 * - `shell.css`：双栏外壳（dock / section 切换器 / 对象树 / 把手）
 *
 * 视图逐个迁进 primitives 后 app.css 会 shrinking；等它空了，
 * 前两层合并成一个文件。
 */
import "./styles/app.css";
import "./styles/tokens.css";
import "./styles/shell.css";

const target = document.getElementById("app");
if (!target) throw new Error("#app 不存在，index.html 被改坏了");

export default mount(App, { target });
