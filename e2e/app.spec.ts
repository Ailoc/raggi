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

test("对话：两栏布局、多轮历史、引用跳回证据", async ({ page }) => {
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
  await page.getByRole("button", { name: "对话" }).click();
  await box.fill(firstQuestion);
  await page.getByRole("button", { name: "发送" }).click();
  await expect(page.getByText(/根据证据，苹果出现在水果列表中/)).toBeVisible();
  await expect(page.getByText("本轮证据")).toBeVisible();

  await page.getByRole("button", { name: /\[1\]/ }).first().click();
  await expect(page.getByText("命中片段")).toBeVisible();
  await expect(page.locator('[role="button"][aria-expanded="true"]')).toHaveCount(1);

  await box.fill("它还有哪些水果？");
  await page.getByRole("button", { name: "发送" }).click();
  await expect(page.getByText(/它还有苹果、香蕉和葡萄/)).toBeVisible();
  expect(answerRequests).toHaveLength(2);
  expect(answerRequests[1]?.history).toEqual([
    { role: "user", content: firstQuestion },
    { role: "assistant", content: "根据证据，苹果出现在水果列表中。[1]" },
  ]);

  // 刷新后历史仍可恢复（sessionStorage），页面默认回到空态检索模式。
  await page.waitForTimeout(400);
  await page.reload();
  await expect(box).toBeVisible();
  await expect(page.getByText("检索结果")).toHaveCount(0);
  await page.getByRole("button", { name: "对话" }).click();
  await expect(page.getByText(/它还有苹果、香蕉和葡萄/)).toBeVisible();
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
