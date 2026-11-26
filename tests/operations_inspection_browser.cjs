const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const { chromium, expect } = require("playwright/test");
const { browserOptions } = require("./browser_options.cjs");
const { submitGroupCreation } = require("./chat_group_create.cjs");

const [origin, folder, fixtureDir] = process.argv.slice(2);
const question = "帮我看看这周有哪些商品值得优先处理，证据够的话整理成待办。";

async function run() {
  const browser = await chromium.launch(browserOptions());
  const result = { browser: browser.version(), errors: [] };
  try {
    const page = await browser.newPage({ baseURL: origin, viewport: { width: 1400, height: 1080 } });
    page.on("pageerror", error => result.errors.push(error.message));
    await page.addInitScript(() => {
      const fetchResponse = window.fetch.bind(window);
      window.fetch = async (url, options) => {
        const response = await fetchResponse(url, options);
        if (url === "/api/chat/stream") {
          response.clone().text().then(body => {
            window.inspectionEvents = body.split(/\r?\n/).filter(line => line.startsWith("data: "))
              .map(line => JSON.parse(line.slice(6)));
          });
        }
        return response;
      };
    });
    await page.goto(origin);
    await page.locator("#loginUser").fill("task45-operator");
    await page.locator("#loginPass").fill(process.env.INSPECTION_BROWSER_PASSWORD);
    await page.waitForFunction(() => Boolean(window._captchaId));
    await page.locator("#loginCaptcha").fill("0726");
    const loginResponse = page.waitForResponse(response => new URL(response.url()).pathname === "/api/login");
    await page.locator("#loginPass").press("Enter");
    const login = await (await loginResponse).json();
    assert.equal(login.code, 200);
    const headers = { Authorization: "Bearer " + login.data.token };
    await page.locator('[data-page="chat"]').click();
    const created = page.waitForResponse(response => response.request().method() === "POST" &&
      new URL(response.url()).pathname === "/api/chat/conversations");
    await submitGroupCreation(page, "商品优先处理巡检");
    const conversation = (await (await created).json()).data.conversation_id;
    const group = "/api/chat/conversations/" + conversation;
    const imported = await page.request.post(group + "/search-terms/reports?filename=amazon-daily.csv&shop_id=t45-ops", {
      headers, data: await fs.readFile(path.join(fixtureDir, "report.csv")),
    });
    assert.equal(imported.status(), 201);

    const response = page.waitForResponse(response => new URL(response.url()).pathname === "/api/chat/stream");
    await page.locator("#chatInput").fill(question);
    await page.locator("#chatSend").click();
    const streamed = await response;
    assert.deepEqual(streamed.request().postDataJSON(), { message: question, conversation_id: conversation });
    await page.waitForFunction(() => Array.isArray(window.inspectionEvents));
    const events = await page.evaluate(() => window.inspectionEvents);
    const terminal = events.at(-1);
    assert.equal(terminal.type, "done");
    result.terminal = terminal;
    const card = page.locator(`.msg[data-run-id="${terminal.run_id}"] [data-result-kind="evidence"]`);
    await expect(card).toHaveCount(1);
    await card.click();
    const detail = page.locator("#workbenchDetail");
    await expect(detail.getByText("商品优先处理依据")).toBeVisible();
    const item = detail.getByText("待审标题草案 · 12L lunch bag", { exact: true });
    await item.click();
    await expect(detail.locator("[data-inspection-draft]")).toHaveText(
      "Trailbox Reusable Soft-Sided Insulated Lunch Bag 12 L, Zippered Cooler Tote");
    await expect(detail.getByText("商品事实 v7", { exact: true })).toBeVisible();
    await expect(detail.getByText(/原标题 v3/)).toBeVisible();
    await expect(detail.getByText("原报表第 2 行", { exact: true })).toBeVisible();
    await expect(detail.getByText("原报表第 3 行", { exact: true })).toBeVisible();
    await expect(detail.getByText(/30 点击 · 4 单/)).toBeVisible();
    await expect(detail.locator("blockquote").first()).toContainText("Capacity: 12 L.");
    await expect(detail.locator("[data-artifact-id]")).toHaveCount(4);
    await expect(detail.getByRole("button", { name: /批准|应用|执行/ })).toHaveCount(0);
    result.detail = await detail.innerText();
    await item.evaluate(element => element.scrollIntoView({ block: "start" }));
    await page.screenshot({ path: path.join(folder, "result.png") });
    await page.reload();
    await page.locator('[data-page="chat"]').click();
    await expect(card).toBeVisible();
    await card.click();
    await expect(detail.getByText("待审标题草案 · 12L lunch bag", { exact: true })).toBeVisible();
    assert.deepEqual(result.errors, []);
    result.passed = true;
  } finally {
    await fs.writeFile(path.join(folder, "browser.json"), JSON.stringify(result, null, 2) + "\n");
    await browser.close();
  }
}

run().catch(error => { console.error(error.message); process.exitCode = 1; });
