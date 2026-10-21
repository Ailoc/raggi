import { defineConfig } from "@playwright/test";

/**
 * 浏览器 E2E：对着 tools/e2e_server.py 起的临时实例跑。
 *
 * 用系统 Chrome（channel: chrome）而不是下载 Playwright 自带 Chromium：
 * 本机与 CI 镜像里已有 Chrome，省掉 ~150MB 下载；没有 Chrome 的环境
 * 可以用 PW_CHANNEL= 让 Playwright 回退到自带浏览器（需先 install）。
 */
export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: [["list"]],
  outputDir: "test-results",
  use: {
    baseURL: "http://127.0.0.1:8177",
    channel: process.env.PW_CHANNEL || "chrome",
    headless: true,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  webServer: {
    // 先构建：E2E 必须对着当前源码的产物跑，而不是上一次的 dist
    command: "npm run build && python3 tools/e2e_server.py --port 8177",
    url: "http://127.0.0.1:8177/api/kbs",
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
    stdout: "pipe",
    stderr: "pipe",
  },
});
