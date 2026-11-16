const assert = require("node:assert/strict");
const fs = require("node:fs");
const { chromium } = require("playwright");
const { browserOptions } = require("./browser_options.cjs");
const { submitGroupCreation } = require("./chat_group_create.cjs");
const { progressButton } = require("./task30_checks.cjs");

const [base, input, output] = process.argv.slice(2);
const captureOnly = process.argv.includes("--capture-only");
const { title, message, username } = JSON.parse(fs.readFileSync(input, "utf8"));
const groups = "/api/chat/conversations";
const stream = "/api/chat/stream";
const result = { sse: [] };
const save = () => fs.writeFileSync(output, JSON.stringify(result, null, 2));

async function run() {
  const browser = await chromium.launch(browserOptions());
  try {
    const page = await browser.newPage();
    const picker = page.locator("#chatGroupList");
    result.chromium = browser.version();
    await page.addInitScript(() => {
      const original = window.fetch.bind(window);
      window.t3 = { stream: null, entries: {}, streamEnded: null };
      window.fetch = async (url, options) => {
        const response = await original(url, options);
        if (url === "/api/chat/stream") {
          response.clone().text().then(text => {
            window.t3.stream = text;
            window.t3.streamEnded = performance.now();
          });
        }
        return response;
      };
    });
    await page.goto(base);
    await page.locator("#loginUser").fill(username);
    await page.locator("#loginPass").fill(process.env.AD_TASK27_T3_PASSWORD);
    await page.waitForFunction(() => Boolean(window._captchaId));
    await page.locator("#loginCaptcha").fill("0726");
    await page.locator("#loginPass").press("Enter");
    await page.locator('[data-page="chat"]').click();
    await picker.waitFor();

    const matches = (response, pathname) => new URL(response.url()).pathname === pathname
      && response.request().method() === "POST";
    const [created] = await Promise.all([
      page.waitForResponse(response => matches(response, groups)),
      submitGroupCreation(page, title),
    ]);
    assert.equal(created.status(), 201);
    const groupId = (await created.json()).data.conversation_id;
    await page.waitForFunction(id => document.querySelector("#chatGroupList [aria-current]")?.dataset.conversationId === id, groupId);
    await page.evaluate(() => {
      const chat = document.querySelector("#chatMessages");
      new MutationObserver(() => {
        const card = chat.querySelector(':scope > .workbench-delegation [data-result-kind="progress"]');
        if (card && card.getAttribute("aria-expanded") !== "true" && !document.querySelector("#workbenchDetail[open]")) {
          card.click();
        }
        for (const row of document.querySelectorAll("#chatMessages [data-entry-id], #workbenchDetail .msg[data-entry-id]")) {
          window.t3.entries[row.dataset.entryId] ??= {
            at_ms: performance.now(), event: row.dataset.event ?? null, text: row.textContent,
            before_stream_end: window.t3.streamEnded === null,
          };
        }
      }).observe(document.querySelector("#ordinaryChatView"), { childList: true, subtree: true });
    });

    await page.locator("#chatInput").fill(message);
    const started = performance.now();
    result.stream_posts = 0;
    page.on("request", request => {
      if (new URL(request.url()).pathname === stream && request.method() === "POST") result.stream_posts++;
    });
    const posted = page.waitForRequest(request => new URL(request.url()).pathname === stream
      && request.method() === "POST");
    const accepted = page.waitForResponse(response => matches(response, stream), { timeout: 105000 });
    if (captureOnly) {
      await page.route("**/api/chat/stream", route => route.fulfill({ status: 204 }));
    }
    await page.locator("#chatSend").click();
    result.request = (await posted).postDataJSON();
    save();
    assert.deepEqual(result.request, { message, conversation_id: groupId });
    const response = await accepted;
    result.http_status = response.status();
    if (captureOnly) {
      assert.equal(result.http_status, 204);
      assert.equal(result.stream_posts, 1);
      result.captured_without_stream = true;
      return;
    }
    assert.equal(result.http_status, 200);
    await page.waitForFunction(() => window.t3.stream !== null, null, { timeout: 105000 });
    result.browser_round_seconds = Math.round(performance.now() - started) / 1000;
    result.sse = (await page.evaluate(() => window.t3.stream)).split(/\r?\n/)
      .filter(line => line.startsWith("data: ")).map(line => JSON.parse(line.slice(6)));
    save();

    async function history() {
      const token = await page.evaluate(() => JSON.parse(sessionStorage.getItem("ad.login")).token);
      const response = await page.request.get(base + groups + "/" + groupId + "/history", {
        headers: { Authorization: "Bearer " + token },
      });
      assert.equal(response.status(), 200);
      const snapshot = (await response.json()).data;
      assert.equal(snapshot.conversation.conversation_id, groupId);
      return snapshot;
    }

    async function dom(snapshot) {
      await page.waitForFunction(expected => {
        const ids = [...document.querySelectorAll("#chatMessages .msg")].map(row => row.dataset.entryId);
        return JSON.stringify(ids) === JSON.stringify(expected);
      }, snapshot.entries.filter(row => row.kind !== "worker").map(row => row.entry_id));
      const progress = progressButton(page, terminal.run_id);
      if (await progress.count() && await progress.getAttribute("aria-expanded") !== "true") await progress.click();
      const rendered = await page.locator("#chatMessages .msg, #workbenchDetail .msg").evaluateAll(items => items.map(row => ({
        entry_id: row.dataset.entryId, run_id: row.dataset.runId, event: row.dataset.event ?? null,
        text: (row.querySelector(".workbench-reply") || row).textContent,
      })));
      const byId = new Map(rendered.map(row => [row.entry_id, row]));
      const rows = snapshot.entries.map(row => byId.get(row.entry_id));
      assert.deepEqual(rows.map(({ text, ...row }) => row), snapshot.entries.map(row => ({
        entry_id: row.entry_id, run_id: row.run_id, event: row.event?.kind ?? null,
      })));
      for (const row of snapshot.entries.filter(item => item.kind === "worker")) {
        assert.ok(rows.find(item => item.entry_id === row.entry_id).text.length > 0);
      }
      return rows;
    }

    result.history = await history();
    result.dom = await dom(result.history);
    result.live = await page.evaluate(() => window.t3.entries);
    save();
    await page.reload();
    await page.locator('[data-page="chat"]').click();
    await page.waitForFunction(id => document.querySelector("#chatGroupList [aria-current]")?.dataset.conversationId === id, groupId);
    result.replay_history = await history();
    result.replay_dom = await dom(result.replay_history);
    assert.deepEqual(result.replay_history, result.history);
    assert.deepEqual(result.replay_dom, result.dom);
    result.dom_replay_pass = true;
  } finally {
    save();
    await browser.close();
  }
}

run().catch(error => {
  result.browser_failure = error.name;
  save();
  console.error(error);
  process.exitCode = 1;
});
