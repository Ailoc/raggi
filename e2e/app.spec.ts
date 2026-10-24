import { expect, test } from "@playwright/test";

/**
 * 浏览器 E2E：覆盖静态守卫测不到的“真实交互 + URL + CSP”组合。
 *
 * 对端是 tools/e2e_server.py 起的临时实例（桩 embedding、种子库），
 * 用例尽量只读；写路径由后端行为测试覆盖，这里只验证界面接线。
 */
const KB = "示例知识库";
const DOC = "示例文档 A";

test("首页 → 库 → 文档 → 原文按需展开并同步 URL", async ({ page }) => {
  const errors: string[] = [];
  page.on("console", (m) => {
    if (m.type() === "error" && !m.text().includes("favicon")) {
      errors.push(m.text());
    }
  });
  page.on("pageerror", (e) => errors.push(String(e)));

  page.on("response", (r) => {
    if (r.status() >= 400 && !r.url().includes("favicon")) {
      errors.push(`${r.status()} ${r.url()}`);
    }
  });

  const resp = await page.goto("/");
  expect(resp?.headers()["content-security-policy"]).toContain(
    "script-src 'self'",
  );
  await expect(page.getByRole("heading", { name: "知识库", exact: true })).toBeVisible();

  await page
    .getByRole("button", { name: new RegExp(`^${KB} \\d`) })
    .click();
  await expect(page.getByRole("tab", { name: "文档" })).toBeVisible();
  await page.getByRole("link", { name: DOC }).click();

  await expect(page.getByRole("heading", { name: DOC })).toBeVisible();
  await expect(page).not.toHaveURL(/src=1/);
  await expect(page.locator('[aria-label="原文"]')).toHaveCount(0);

  await page.getByRole("button", { name: /展开原文/ }).click();
  await expect(page).toHaveURL(/src=1/);
  await expect(page.locator('[aria-label="原文"]')).toBeVisible();
  expect(errors).toEqual([]);
});

test("检索：空态居中输入，发送后结果在上、条件草稿本地生效", async ({ page }) => {
  await page.goto("/#/search");
  const box = page.getByRole("textbox", { name: "检索或对话输入" });
  await expect(box).toBeVisible();
  await expect(page.getByText("检索结果")).toHaveCount(0);

  await box.fill("苹果");
  await box.press("Enter");
  // 检索词不写 URL：地址保持裸 #/search。
  await expect(page).not.toHaveURL(/q=/);
  await expect(page.getByText(/条 · 候选/).first()).toBeVisible();
  await expect(page.getByText("检索结果")).toBeVisible();
  // 发送即清空：查询词保留在结果标题里，输入台不应继续显示旧文本。
  await expect(box).toHaveValue("");

  // 结果列必须与输入台同宽同左边界：1440/1280 下都是 900px 工作列，
  // 窄屏下是同一个扣除内边距后的宽度（滚动条宽度也要一起计入）。
  const list = page.locator('ol[aria-label="检索证据"]');
  const composer = page.locator(".lg-composer");
  const listBox = await list.boundingBox();
  const composerBox = await composer.boundingBox();
  if (!listBox || !composerBox) throw new Error("结果列或输入台未渲染");
  expect(Math.abs(listBox.width - composerBox.width)).toBeLessThan(1.5);
  expect(Math.abs(listBox.x - composerBox.x)).toBeLessThan(1.5);

  // 滚动区要伸进输入台顶部的两个圆角缺口（约一个圆角半径），
  // 这样卡片是绕开玻璃圆角消失的；玻璃下方由同色衬底遮住内容。
  const scrollerBottom = await list.evaluate((ol) => {
    let el = ol.parentElement;
    while (el
      && !["auto", "scroll"].includes(getComputedStyle(el).overflowY)) {
      el = el.parentElement;
    }
    return el ? el.getBoundingClientRect().bottom : null;
  });
  if (scrollerBottom == null) throw new Error("滚动容器未渲染");
  // 滚动区必须越过输入台上沿、进入圆角缺口区域（不能提前裁断），
  // 同时又不能整段铺到输入台背后（玻璃下方有衬底遮住内容）。
  expect(scrollerBottom).toBeGreaterThan(composerBox.y + 12);

  // 滚到底：最后一张卡必须完整落在输入台上方，不能被输入台盖住。
  await list.evaluate((ol) => {
    let el = ol.parentElement;
    while (el
      && !["auto", "scroll"].includes(getComputedStyle(el).overflowY)) {
      el = el.parentElement;
    }
    if (el) el.scrollTop = el.scrollHeight;
  });
  const lastBox = await list.locator("li").last().boundingBox();
  if (!lastBox) throw new Error("最后一张结果卡未渲染");
  expect(lastBox.y + lastBox.height).toBeLessThan(composerBox.y - 8);

  const first = page.getByRole("button", { name: /苹果/ }).first();
  await expect(first).toBeVisible();
  await first.click();
  await expect(page.getByText("命中片段")).toBeVisible();
  await expect(first).toHaveAttribute("aria-expanded", "true");

  await page.getByRole("button", { name: /^条件/ }).click();
  await page.getByLabel("返回条数").fill("3");
  await page.getByRole("button", { name: "应用筛选" }).click();
  // 应用后条件以 chip 呈现，参数留在本地状态而不是 URL。
  await expect(page.getByText("返回条数：3")).toBeVisible();
  await expect(page).not.toHaveURL(/top_k=3/);

  // 顶栏「检索」是空态入口：清空上一次查询与结果。
  await page.getByRole("tab", { name: "检索" }).click();
  await expect(page.getByText("检索结果")).toHaveCount(0);
  await expect(page.getByRole("textbox", { name: "检索或对话输入" })).toBeVisible();

  // 结果页的「新检索」同样清空查询回到空态。
  await box.fill("苹果");
  await box.press("Enter");
  await expect(page.getByText(/条 · 候选/).first()).toBeVisible();
  await page.getByRole("button", { name: "新检索" }).click();
  await expect(page.getByText("检索结果")).toHaveCount(0);
  await expect(box).toHaveValue("");
});

test("对话：单列工作台、每次进入是新会话、引用在右侧同层展开原文", async ({ page }) => {
  const firstQuestion = "苹果是什么？";
  const search = (await (
    await page.request.post("/api/search", {
      data: { q: firstQuestion, top_k: 8 },
    })
  ).json()) as {
    results: Array<{
      chunk_id: string;
      doc_id: string;
      kb_id: string;
      title: string;
      heading_path: string;
      ordinal: number;
      page: number | null;
      score: number;
      snippet: { text: string; marks: number[][] };
    }>;
  };
  const hit = search.results[0];
  expect(hit, "检索没有结果，无法验证引用跳转").toBeTruthy();

  // 右侧阅读区展示的是这个分块在原始文件里的正文，不是检索片段。
  const chunk = (await (
    await page.request.get(`/api/chunks/${encodeURIComponent(hit!.chunk_id)}`)
  ).json()) as { text: string };
  expect(chunk.text.length).toBeGreaterThan(0);

  await page.route("**/api/models", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ features: { answer: true }, retrieve: {} }),
    }),
  );

  const answerRequests: Array<{
    q: string;
    history?: Array<{ role: string; content: string }>;
  }> = [];
  await page.route("**/api/answer/stream", (route) => {
    const body = route.request().postDataJSON() as {
      q: string;
      history?: Array<{ role: string; content: string }>;
    };
    answerRequests.push(body);
    const text = answerRequests.length === 1
      ? "根据证据，苹果出现在水果列表中。[1]"
      : "它还有苹果、香蕉和葡萄。[1]";
    route.fulfill({
      status: 200,
      headers: { "content-type": "text/event-stream; charset=utf-8" },
      body: [
        "event: sources",
        `data: ${JSON.stringify({
          citations: [{
            index: 1,
            chunk_id: hit!.chunk_id,
            doc_id: hit!.doc_id,
            kb_id: hit!.kb_id,
            title: hit!.title,
            heading_path: hit!.heading_path,
            page: hit!.page,
            ordinal: hit!.ordinal,
            score: hit!.score,
            snippet: hit!.snippet,
          }],
        })}`,
        "",
        "event: delta",
        `data: ${JSON.stringify({ text })}`,
        "",
        "event: done",
        `data: ${JSON.stringify({ took_ms: 12, mode: "hybrid", degraded_reason: null })}`,
        "",
        "",
      ].join("\n"),
    });
  });

  await page.goto("/#/search");
  const box = page.getByRole("textbox", { name: "检索或对话输入" });
  await box.fill("苹果");
  await box.press("Enter");
  await page.getByText(/条 · 候选/).first().waitFor();

  // 切到对话：回到空态（只有居中输入台），不直接落进左右分栏。
  await page.getByRole("button", { name: "对话模式" }).click();
  await expect(page.locator('[aria-label="对话记录"]')).toHaveCount(0);
  await expect(page.locator("aside[aria-label='证据阅读区']")).toHaveCount(0);

  await box.fill(firstQuestion);
  await page.getByRole("button", { name: "发送" }).click();
  await expect(page.getByText(/根据证据，苹果出现在水果列表中/)).toBeVisible();

  // 发送后的对话列与检索页同款：与输入台同宽同左边界。
  const thread = page.locator('[aria-label="对话记录"]');
  const composer = page.locator(".lg-composer");
  const threadBox = await thread.boundingBox();
  const composerBox = await composer.boundingBox();
  if (!threadBox || !composerBox) throw new Error("对话列或输入台未渲染");
  expect(Math.abs(threadBox.width - composerBox.width)).toBeLessThan(1.5);
  expect(Math.abs(threadBox.x - composerBox.x)).toBeLessThan(1.5);

  // 右侧阅读区默认不出现；点引用后它在**同一页面层级**展开：
  // 对话列整体左移，而不是被一个浮层盖住。
  const panel = page.locator("aside[aria-label='证据阅读区']");
  await expect(panel).toHaveCount(0);
  await page.getByRole("button", { name: /\[1\]/ }).first().click();
  await expect(panel).toBeVisible();

  // 区域里是那个分块的**原文**，不是检索片段。
  const reader = panel.getByLabel("分块原文");
  await expect(reader).toBeVisible();
  await expect(reader).toContainText(chunk.text.slice(0, 24));

  const shifted = await thread.boundingBox();
  const panelBox = await panel.boundingBox();
  if (!shifted || !panelBox) throw new Error("证据阅读区未渲染");
  expect(shifted.x).toBeLessThan(threadBox.x);
  expect(panelBox.x).toBeGreaterThanOrEqual(shifted.x + shifted.width - 2);

  // 编号带可以在本轮证据之间快速切换。
  await expect(page.getByRole("button", { name: "阅读证据 1" })).toBeVisible();

  // 「证据 N」同样打开右侧阅读区。
  await page.getByRole("button", { name: "关闭证据面板" }).click();
  await expect(panel).toHaveCount(0);
  await page.getByRole("button", { name: /证据 \d+/ }).first().click();
  await expect(panel).toBeVisible();
  // exact：按钮的悬浮提示文字里也含「本轮证据」四个字，不加精确匹配
  // 会同时命中提示气泡与区域标题。
  await expect(page.getByText("本轮证据", { exact: true })).toBeVisible();

  // 会话内可以连续追问，历史照常随请求带给模型。
  await box.fill("它还有哪些水果？");
  await page.getByRole("button", { name: "发送" }).click();
  await expect(page.getByText(/它还有苹果、香蕉和葡萄/)).toBeVisible();
  expect(answerRequests).toHaveLength(2);
  expect(answerRequests[1]?.history).toEqual([
    { role: "user", content: firstQuestion },
    { role: "assistant", content: "根据证据，苹果出现在水果列表中。[1]" },
  ]);

  // 切回检索再切回对话：每次进入都是新会话，上一段对话被丢弃。
  await page.getByRole("button", { name: "检索模式" }).click();
  await page.getByRole("button", { name: "对话模式" }).click();
  await expect(page.getByText(/它还有苹果、香蕉和葡萄/)).toHaveCount(0);
  await expect(page.locator('[aria-label="对话记录"]')).toHaveCount(0);
});

test("API 文档：过滤进 URL，选中端点进 URL", async ({ page }) => {
  await page.goto("/#/apidoc");
  await page.getByLabel("过滤 API 端点").fill("kbs");
  await expect(page).toHaveURL(/q=kbs/);
  await expect(page.getByText(/\d+ \/ \d+/)).toBeVisible();

  await page.getByRole("button", { name: /知识库列表/ }).click();
  await expect(page).toHaveURL(/ep=/);
  await expect(page.getByText(/按更新时间倒序/)).toBeVisible();
});

test("深链 ?chunk= 自动展开原文并定位到该文档", async ({ page }) => {
  const kbs = (await (await page.request.get("/api/kbs")).json()) as Array<{
    kb_id: string;
    name: string;
  }>;
  const kb = kbs.find((k) => k.name === KB);
  expect(kb, "种子库不存在，E2E 服务没有正确 seed").toBeTruthy();
  const list = (await (
    await page.request.get(
      `/api/documents?kb_id=${kb!.kb_id}&q=${encodeURIComponent(DOC)}`,
    )
  ).json()) as { items: Array<{ doc_id: string; title: string }> };
  const doc = list.items.find((d) => d.title === DOC);
  expect(doc, "种子文档不存在").toBeTruthy();
  const chunks = (await (
    await page.request.get(`/api/chunks?doc_id=${doc!.doc_id}`)
  ).json()) as { items: Array<{ chunk_id: string }> };
  expect(chunks.items.length).toBeGreaterThan(0);

  await page.goto(
    `/#/kb/${kb!.kb_id}/doc/${doc!.doc_id}?chunk=${chunks.items[0]!.chunk_id}`,
  );
  await expect(page.getByRole("heading", { name: DOC })).toBeVisible();
  await expect(page.locator('[aria-label="原文"]')).toBeVisible();
});

test("模型设置：卡内设置项两列、卡片竖排、脏状态可见可放弃、维度变更二次确认", async ({ page }) => {
  await page.goto("/#/settings/models");
  await expect(page.getByRole("combobox", { name: "提供方" }).first()).toBeVisible();

  // 卡片竖着摞、同宽；卡内设置项排两列——同一行两项 y 相同、x 分开
  const layout = await page.evaluate(() => {
    const panelOf = (title: string) =>
      [...document.querySelectorAll(".MuiTypography-h6")]
        .find((t) => t.textContent?.trim() === title)
        ?.closest(".lg-glass-panel") as HTMLElement | null;
    const box = (title: string) => {
      const r = panelOf(title)!.getBoundingClientRect();
      return { title, x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width) };
    };
    const rows = (title: string) => {
      const p = panelOf(title)!;
      return [...p.querySelectorAll("label[for]")].map((l) => {
        const t = document.getElementById(l.getAttribute("for")!) as HTMLElement;
        const r = t.getBoundingClientRect();
        return {
          label: l.textContent?.trim(),
          x: Math.round(r.x),
          y: Math.round(r.y),
          checkbox: (t as HTMLInputElement).type === "checkbox",
        };
      });
    };
    return {
      boxes: ["Embedding", "LLM", "Rerank"].map(box),
      embed: rows("Embedding"),
      llm: rows("LLM"),
      rerank: rows("Rerank"),
    };
  });

  expect(layout.boxes.map((b) => b.title)).toEqual(["Embedding", "LLM", "Rerank"]);
  expect(new Set(layout.boxes.map((b) => b.x)).size).toBe(1);
  expect(new Set(layout.boxes.map((b) => b.w)).size).toBe(1);
  expect(layout.boxes[1]!.y).toBeGreaterThan(layout.boxes[0]!.y);
  expect(layout.boxes[2]!.y).toBeGreaterThan(layout.boxes[1]!.y);

  // 卡内两列：非复选框字段恰好两个左边缘，逐行成对往下
  for (const rows of [layout.embed, layout.llm, layout.rerank]) {
    const fields = rows.filter((r) => !r.checkbox);
    const xs = [...new Set(fields.map((f) => f.x))].sort((a, b) => a - b);
    expect(xs.length).toBe(2);
    expect(xs[1]! - xs[0]!).toBeGreaterThan(200);
    expect(fields[0]!.y).toBe(fields[1]!.y);
    expect(fields[0]!.x).toBe(xs[0]);
    expect(fields[1]!.x).toBe(xs[1]);
    for (let i = 2; i < fields.length; i++) {
      expect(fields[i]!.y).toBeGreaterThan(fields[i - 2]!.y);
    }
  }

  // 改一个字段 → 脏状态可见 → 放弃后回到原值
  const model = page.getByRole("textbox", { name: "模型", exact: true }).first();
  const origModel = await model.inputValue();
  await model.fill("changed-model");
  await expect(page.getByText("有未保存的改动")).toBeVisible();
  await page.getByRole("button", { name: "放弃改动" }).click();
  await expect(page.getByText("有未保存的改动")).toHaveCount(0);
  await expect(model).toHaveValue(origModel);

  // 改向量维度 → 保存时必须二次确认；取消则维度回退
  const dim = page.getByRole("spinbutton", { name: "向量维度" });
  const origDim = await dim.inputValue();
  await dim.fill("8");
  await page.getByRole("button", { name: "保存并热切换" }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await expect(page.getByText(/变更向量维度/)).toBeVisible();
  await page.getByRole("button", { name: "取消" }).click();
  await expect(dim).toHaveValue(origDim);
});

test("窄屏：模型设置不横向溢出，设置项收成一列", async ({ page }) => {
  await page.setViewportSize({ width: 360, height: 780 });
  await page.goto("/#/settings/models");
  await expect(page.getByRole("combobox", { name: "提供方" }).first()).toBeVisible();
  const overflow = await page.evaluate(() => {
    const main = document.querySelector("main");
    return {
      doc: document.documentElement.scrollWidth > document.documentElement.clientWidth,
      main: main ? main.scrollWidth > main.clientWidth : false,
    };
  });
  expect(overflow).toEqual({ doc: false, main: false });

  // 两列在窄屏必须收成一列：所有字段共用同一个左边缘
  const xs = await page.evaluate(() =>
    [...document.querySelectorAll<HTMLElement>('[id^="models-"]')]
      .filter((el) => (el as HTMLInputElement).type !== "checkbox")
      .map((el) => Math.round(el.getBoundingClientRect().x)));
  expect(new Set(xs).size).toBe(1);
});

test("主题切换跨刷新保留", async ({ page }) => {
  await page.goto("/");
  const before = await page.evaluate(
    () => document.documentElement.dataset.theme,
  );
  await page.getByRole("button", { name: /切换到.*主题/ }).click();
  const after = await page.evaluate(
    () => document.documentElement.dataset.theme,
  );
  expect(after).not.toBe(before);
  await page.reload();
  expect(
    await page.evaluate(() => document.documentElement.dataset.theme),
  ).toBe(after);
});

test("CSP 下 Emotion 样式必须带 nonce 且实际生效", async ({ page }) => {
  const errors: string[] = [];
  page.on("console", (m) => {
    if (m.type() === "error" && !m.text().includes("favicon")) {
      errors.push(m.text());
    }
  });
  page.on("pageerror", (e) => errors.push(String(e)));

  const resp = await page.goto("/");
  const csp = resp?.headers()["content-security-policy"] ?? "";
  expect(csp).toContain("style-src-elem 'self' 'nonce-");
  expect(csp).not.toContain("style-src 'unsafe-inline'");

  const nonce = await page.evaluate(() =>
    document.querySelector('meta[name="csp-nonce"]')?.getAttribute("content"),
  );
  expect(nonce).toBeTruthy();
  // 注意读 `.nonce` 属性而不是 getAttribute：浏览器隐藏了 nonce 内容属性
  // （防样式表外带），但 IDL 属性仍能拿到真实值。
  const styles = await page.evaluate(() =>
    [...document.querySelectorAll("style")].map((s) => ({
      nonce: s.nonce,
      rules: s.sheet?.cssRules.length ?? 0,
    })),
  );
  expect(styles.length).toBeGreaterThan(0);
  expect(styles.every((s) => s.nonce === nonce && s.rules > 0)).toBe(true);

  // 样式真的生效（不是被 CSP 拦掉后的无样式页面）
  // 探针用实心按钮而不是知识库卡片：卡片已经是玻璃面，真折射路径下
  // 它的背景是透明的（材质由 GPU 贴图画），只有实心按钮的主题色在
  // 两条路径下都是不透明色。
  const bg = await page.evaluate(() => {
    const el = document.querySelector(".MuiButton-contained");
    return el ? getComputedStyle(el).backgroundColor : "";
  });
  expect(bg).not.toBe("");
  expect(bg).not.toBe("rgba(0, 0, 0, 0)");
  expect(errors).toEqual([]);
});
