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

test("检索：结果、参数草稿与应用后的 URL 一致", async ({ page }) => {
  await page.goto("/#/search?q=苹果");
  await expect(page.getByText(/条 ·/).first()).toBeVisible();
  await expect(page.getByRole("button", { name: /苹果/ }).first()).toBeVisible();

  await page.getByRole("button", { name: /^参数/ }).click();
  await page.getByLabel("返回条数").fill("3");
  // 改动只是草稿：URL 仍描述已生效的参数
  await expect(page).not.toHaveURL(/top_k=3/);
  await page.getByRole("button", { name: "应用并检索" }).click();
  await expect(page).toHaveURL(/top_k=3/);
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
  const bg = await page.evaluate(() => {
    const el = document.querySelector(".MuiCard-root");
    return el ? getComputedStyle(el).backgroundColor : "";
  });
  expect(bg).not.toBe("");
  expect(bg).not.toBe("rgba(0, 0, 0, 0)");
  expect(errors).toEqual([]);
});
