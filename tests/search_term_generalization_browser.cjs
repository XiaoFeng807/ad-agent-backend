const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const { chromium, expect } = require("playwright/test");
const { browserOptions } = require("./browser_options.cjs");
const { submitGroupCreation } = require("./chat_group_create.cjs");

async function verify() {
  const [origin, folder] = process.argv.slice(2);
  const { question } = JSON.parse(await fs.readFile(path.join(folder, "frozen.json"), "utf8"));
  const browser = await chromium.launch(browserOptions());
  try {
    const context = await browser.newContext({ baseURL: origin, permissions: ["clipboard-read", "clipboard-write"] });
    const session = await context.request.post("/api/login", { data: {
      username: "sp-generalization", password: process.env.SEARCH_TERMS_PASSWORD,
    }});
    const login = await session.json();
    assert.equal(login.code, 200);
    await context.addInitScript(data => {
      sessionStorage.setItem("ad.login", JSON.stringify(data));
      sessionStorage.setItem("ad.chatPage", "groups");
    }, login.data);
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.goto("/");
    await submitGroupCreation(page, "Generalized cleaning requirement");
    await page.locator("#searchTermImport").click();
    const setup = page.locator("#searchTermSetup");
    await setup.locator('input[type="file"]').setInputFiles(path.join(folder, "report.csv"));
    await setup.locator("#searchTermProduct").selectOption("DEV-MUG");
    await setup.getByRole("button", { name: "确认范围" }).click();
    await expect(setup).toBeHidden();
    await expect(page.locator("#searchTermAttachmentLabel")).toContainText("搜索词报表");
    await page.locator("#chatInput").fill(question);
    await page.locator("#chatSend").click();
    const card = page.locator('[data-result-kind="search_term"]');
    await expect(card).toContainText("建议改标题");
    await page.reload();
    await card.click();
    await page.getByText("报表原行与指标 · 1 行").click();
    await expect(page.getByText("第 2 行 · 2026-11-09")).toBeVisible();
    await page.getByText(/本轮原标题与商品原文/).click();
    const cite = page.getByText("已核验引用 · specifications.tsv · row:3");
    await cite.click();
    const original = cite.locator("..");
    await expect(original.getByText(/Cleaning\s+Dishwasher safe\s+-\s+Body\s+Top rack only/)).toBeVisible();
    await expect(original.getByText(/DEV-MUG · support · chunk_id [a-f0-9]{64}/)).toBeVisible();
    await page.getByRole("button", { name: "复制已保存版本" }).click();
    await expect.poll(() => page.evaluate(() => navigator.clipboard.readText()))
      .toBe("Bramble Travel Mug, Body Top Rack Only Dishwasher Safe");
    assert.deepEqual(errors, []);
    await fs.writeFile(path.join(folder, "browser-result.json"), JSON.stringify({
      passed: true, version: browser.version(), refreshedReportRow: 2, refreshedSourceRow: 3,
      copiedTitle: await page.evaluate(() => navigator.clipboard.readText()),
    }, null, 2) + "\n");
  } finally {
    await browser.close();
  }
}

verify().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
