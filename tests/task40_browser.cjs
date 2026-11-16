const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");
const { browserOptions } = require("./browser_options.cjs");
const { submitGroupCreation } = require("./chat_group_create.cjs");
const { progressButton } = require("./task30_checks.cjs");

const [baseURL, input, output] = process.argv.slice(2);
const scenario = JSON.parse(fs.readFileSync(input, "utf8"));
const result = {};
const timeout = 105000;

async function run() {
  const browser = await chromium.launch(browserOptions());
  result.chromium = browser.version();
  try {
    const page = await browser.newPage({ baseURL, viewport: { width: 1440, height: 1000 } });
    await page.addInitScript(() => {
      const original = window.fetch.bind(window);
      window.task40Streams = [];
      window.fetch = async (url, options) => {
        const response = await original(url, options);
        if (url === "/api/chat/stream") response.clone().text().then(body => window.task40Streams.push(body));
        return response;
      };
    });
    await page.goto(baseURL);
    await page.locator("#loginUser").fill("task40-owner");
    await page.locator("#loginPass").fill(process.env.AD_TASK40_PASSWORD);
    await page.waitForFunction(() => Boolean(window._captchaId));
    await page.locator("#loginCaptcha").fill("0726");
    await page.locator("#loginPass").press("Enter");
    await page.locator('[data-page="chat"]').click();
    const post = route => page.waitForResponse(response => new URL(response.url()).pathname === route
      && response.request().method() === "POST", { timeout });
    const creation = post("/api/chat/conversations");
    await submitGroupCreation(page, "TASK-040 · " + scenario.name);
    const created = await creation;
    assert.equal(created.status(), 201);
    result.conversation_id = (await created.json()).data.conversation_id;

    const stream = post("/api/chat/stream");
    await page.locator("#chatInput").fill(scenario.message);
    await page.locator("#chatSend").click();
    const response = await stream;
    result.request = response.request().postDataJSON();
    result.http = response.status();
    await page.waitForFunction(() => window.task40Streams.length === 1, null, { timeout });
    result.sse = (await page.evaluate(() => window.task40Streams[0])).split(/\r?\n/)
      .filter(line => line.startsWith("data: ")).map(line => JSON.parse(line.slice(6)));
    const terminal = result.sse.at(-1);
    assert.equal(result.http, 200);
    assert.equal(terminal.type, "done");
    assert.deepEqual(result.sse.filter(row => ["done", "error", "stopped", "superseded"].includes(row.type)), [terminal]);
    const answer = page.locator(`#chatMessages > .assistant[data-run-id="${terminal.run_id}"]`);
    const readAnswer = async () => {
      await answer.waitFor();
      if (scenario.expect_loop) await progressButton(page, terminal.run_id).click();
      const snapshot = await page.waitForFunction(({ runId, expectLoop }) => {
        const messages = document.querySelector("#chatMessages");
        const entry = messages?.querySelector(`:scope > .assistant[data-run-id="${runId}"]`);
        if (!entry) return false;
        const progress = document.querySelector(`#workbenchDetail .group-progress[data-run-id="${runId}"]`);
        const notices = Array.from(progress?.querySelectorAll(":scope > p[data-event]") || [], row => row.textContent);
        if (expectLoop && notices.length !== 3) return false;
        return { text: entry.querySelector(".workbench-reply").innerText, notices, status: entry.dataset.replyStatus };
      }, { runId: terminal.run_id, expectLoop: scenario.expect_loop });
      return snapshot.jsonValue();
    };
    result.initial = await readAnswer();
    await page.reload();
    await page.locator('[data-page="chat"]').click();
    result.refreshed = await readAnswer();
    assert.deepEqual(result.initial, result.refreshed);
    assert.equal(result.refreshed.status, "回复已保存");
    // The history renderer can replace the answer element after reload.
    await page.locator("#chatMessages").evaluate(element => { element.scrollTop = element.scrollHeight; });
    await page.screenshot({ path: path.join(path.dirname(output), "refreshed.png") });
    if (scenario.expect_loop) {
      const panel = page.locator(`[data-run-id="${terminal.run_id}"].group-progress`);
      const notice = scenario.expect_loop === "dispatch" ? "重复分派已拦截" : "已复用本轮结果";
      assert.equal(result.refreshed.notices.filter(line => line.startsWith(notice)).length, 2);
      await panel.evaluate(element => element.scrollIntoView({ block: "nearest" }));
      await page.screenshot({ path: path.join(path.dirname(output), "refreshed-progress.png") });
    }
  } finally {
    await browser.close();
  }
}

run().catch(error => {
  result.failure = error.message;
  process.exitCode = 1;
}).finally(() => fs.writeFileSync(output, JSON.stringify(result, null, 2), "utf8"));
