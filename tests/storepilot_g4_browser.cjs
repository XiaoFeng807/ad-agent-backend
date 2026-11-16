const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const { chromium, expect } = require("playwright/test");
const { browserOptions } = require("./browser_options.cjs");

async function run(folder) {
  const plan = JSON.parse(await fs.readFile(path.join(folder, "plan.json"), "utf8"));
  const { answer_id: answerId } = JSON.parse(await fs.readFile(path.join(folder, "api-answer.json"), "utf8"));
  const browser = await chromium.launch(browserOptions());
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  try {
    await page.goto(plan.origins[0]);
    await page.locator("#loginUser").fill("task45-operator");
    await page.locator("#loginPass").fill(process.env.STOREPILOT_TEST_PASSWORD);
    await page.waitForFunction(() => Boolean(window._captchaId));
    await page.locator("#loginCaptcha").fill("0726");
    await page.locator("#loginPass").press("Enter");
    await page.locator("#appLayout").waitFor({ state: "visible" });
    await page.locator('[data-page="chat"]').click();
    await page.locator('button[data-conversation-id="' + plan.group + '"]').click();
    const assistant = page.locator('#chatMessages .msg.assistant[data-run-id="' + plan.first_run_id + '"]');
    await expect(assistant).toHaveCount(1);
    const beforeReload = await assistant.getAttribute("data-entry-id");
    assert.equal(beforeReload, answerId);
    await page.reload();
    await expect(assistant).toHaveCount(1);
    const afterReload = await assistant.getAttribute("data-entry-id");
    assert.equal(afterReload, answerId);
    await fs.writeFile(path.join(folder, "browser.json"), JSON.stringify({
      origin: plan.origins[0], run_id: plan.first_run_id, before_reload: beforeReload, after_reload: afterReload,
    }, null, 2));
  } finally {
    await browser.close();
  }
}

run(process.argv[2]).catch(error => { console.error(error); process.exitCode = 1; });
