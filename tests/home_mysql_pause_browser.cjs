// Drive the existing homepage against the isolated R4 FastAPI process.
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const { chromium } = require("playwright");
const { browserOptions } = require("./browser_options.cjs");

async function data(response) {
  assert.equal(response.status(), 200);
  const result = await response.json();
  assert.equal(result.code, 200);
  return result.data;
}

async function run() {
  const [origin, output] = process.argv.slice(2);
  const browser = await chromium.launch(browserOptions());
  try {
    const page = await browser.newPage({ viewport: { width: 1360, height: 1050 } });
    page.setDefaultTimeout(20000);
    await page.goto(origin);
    await page.waitForFunction(() => window._captchaId);
    await page.locator("#loginUser").fill("diagnosis-demo");
    await page.locator("#loginPass").fill(process.env.R4_BROWSER_PASSWORD);
    await page.locator("#loginCaptcha").fill("0726");
    await page.locator("#loginCaptcha").press("Enter");
    await page.locator("#navUsername").getByText("diagnosis-demo", { exact: true }).waitFor();
    await page.locator('[data-page="chat"]').click();
    await page.getByRole("button", { name: "投放诊断", exact: true }).click();
    // The migrated fixture already has history and the page opens its latest structured task.
    await page.locator("#diagnosisNew").click();
    const field = name => page.locator(`#diagnosisForm input[name="${name}"]`);
    await field("plan_id").fill("101");
    await field("rule_id").fill("ctr-review-v1#low-ctr");
    for (const suffix of ["start", "end"]) {
      await field("baseline_" + suffix).fill("2026-10-01");
      await field("current_" + suffix).fill("2026-10-02");
    }
    await page.locator("#diagnosisSubmit").click();
    const first = page.locator(".diagnosis-round").first();
    const panel = first.locator(".diagnosis-pause");
    const approve = panel.getByRole("button", { name: "批准暂停沙箱计划" });
    await approve.waitFor();
    const original = await panel.locator("[data-diagnosis-snapshot]").textContent();
    const evidence = await panel.locator("[data-diagnosis-evidence]").textContent();
    assert.match(await panel.locator(".diagnosis-pause-target").textContent(), /#101.*预期状态 active → paused/);
    assert.match(original, /2026-10-01.*2026-10-02/);
    assert.match(evidence, /曝光 1000 · 点击 100.*曝光 1000 · 点击 80.*相对降幅 20\.000000%.*ctr-review-v1-low-ctr-01/);
    const review = first.locator(".diagnosis-review");
    await review.getByRole("button", { name: "确认创建本地复核单" }).click();
    await review.getByText("已保存的人工复核单", { exact: true }).waitFor();
    await first.screenshot({ path: path.join(output, "pending-approval.png") });

    const confirmedResponse = page.waitForResponse(response =>
      /^\/api\/diagnosis\/pause-actions\/[^/]+\/confirm$/.test(new URL(response.url()).pathname));
    const readResponse = page.waitForResponse(response =>
      /^\/api\/diagnosis\/pause-actions\/[^/]+$/.test(new URL(response.url()).pathname));
    await approve.click();
    const receipt = await data(await confirmedResponse);
    const read = await data(await readResponse);
    assert.deepEqual(read, { receipt, actual_status: "paused" });
    await panel.getByText("实际状态 paused", { exact: true }).waitFor();
    assert.equal(await panel.locator("[data-diagnosis-evidence]").textContent(), evidence);
    assert.ok(original.includes(receipt.source_trace_id));
    assert.ok((await panel.locator(".diagnosis-pause-receipt").textContent()).includes(receipt.receipt_id));
    await first.screenshot({ path: path.join(output, "confirmed-readback.png") });
    await fs.writeFile(path.join(output, "browser.json"), JSON.stringify({
      browser: browser.version(), original, evidence, read,
    }, null, 2) + "\n");
  } finally {
    await browser.close();
  }
}

run().catch(error => { console.error(error); process.exitCode = 1; });
