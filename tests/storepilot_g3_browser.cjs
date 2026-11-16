const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const { DatabaseSync } = require("node:sqlite");
const { chromium, expect } = require("playwright/test");
const { browserOptions } = require("./browser_options.cjs");
const { submitGroupCreation } = require("./chat_group_create.cjs");

const folder = process.argv[2];
const file = name => path.join(folder, name);
const report = { watches: [], pageErrors: [] };
const exists = name => fs.access(file(name)).then(() => true, () => false);
const until = (callback, message) => expect.poll(callback, { message, timeout: 25000 }).toBeTruthy();

async function run() {
  const { origins, message } = JSON.parse(await fs.readFile(file("plan.json"), "utf8"));
  const browser = await chromium.launch(browserOptions());
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 },
    recordVideo: { dir: file("video"), size: { width: 1280, height: 900 } } });
  const db = new DatabaseSync(file("run.sqlite3"), { readOnly: true });
  const page = await context.newPage();
  const video = page.video();
  const watchPath = url => /\/background-inspections\/[^/]+\/events$/.test(new URL(url, origins[0]).pathname);
  page.on("pageerror", error => report.pageErrors.push(error.message));
  page.on("response", async response => {
    if (watchPath(response.url()) && response.request().method() === "GET") {
      report.watches.push({ origin: new URL(response.url()).origin, status: response.status(),
        request_id: (await response.allHeaders())["x-request-id"],
        cursor: Number((await response.request().allHeaders())["last-event-id"]) });
    }
  });
  try {
    await page.addInitScript(() => {
      const fetch = window.fetch.bind(window);
      window.fetch = (url, options) => {
        if (typeof url !== "string" || !url.includes("/background-inspections/") || !url.endsWith("/events")) {
          return fetch(url, options);
        }
        if (window.g3Offline) return Promise.reject(new TypeError("Fixture link disconnected"));
        window.g3Wire = new AbortController();
        const signal = AbortSignal.any([options.signal, window.g3Wire.signal]);
        return fetch(new URL(url, window.g3Gateway || location.origin), { ...options, signal });
      };
    });
    await page.goto(origins[0]);
    await page.locator("#loginUser").fill("task45-operator");
    await page.locator("#loginPass").fill(process.env.STOREPILOT_TEST_PASSWORD);
    await page.waitForFunction(() => Boolean(window._captchaId));
    await page.locator("#loginCaptcha").fill("0726");
    const loggedIn = page.waitForResponse(response => new URL(response.url()).pathname === "/api/login");
    await page.locator("#loginPass").press("Enter");
    const login = await (await loggedIn).json();
    assert.equal(login.code, 200);
    try {
      await page.locator("#appLayout").waitFor({ state: "visible", timeout: 6000 });
    } catch (error) {
      report.loginError = await page.locator("#loginErr").textContent();
      await page.evaluate(data => openSession(data), login.data);
      throw error;
    }
    await page.locator('[data-page="chat"]').click();
    const created = page.waitForResponse(response => response.request().method() === "POST" &&
      new URL(response.url()).pathname === "/api/chat/conversations");
    await submitGroupCreation(page, "G3 三款商品后台巡检");
    const { conversation_id: group } = (await (await created).json()).data;
    await page.locator("#chatGroupListing").click();
    await page.locator("#chatInput").fill(message);
    const started = page.waitForResponse(response => response.request().method() === "POST" &&
      new URL(response.url()).pathname.endsWith("/background-inspections"));
    await page.locator("#chatGroupBackground").click();
    const response = await started;
    const startBody = await response.json();
    assert.equal(response.status(), 202, JSON.stringify(startBody));
    const { run_id: run } = startBody.data;
    Object.assign(report, { group, run_id: run, start_request_id: (await response.allHeaders())["x-request-id"] });
    await until(() => exists("held-first"), "first bottom-level evidence read entered");
    const panel = page.locator("#backgroundInspectionProgress");
    const item = panel.locator("li");
    await expect(item.last()).toContainText("正在取证 · T45-THERMAL-TOTE · 销量");
    const cursor = Number(await item.last().getAttribute("data-seq"));
    report.disconnect_cursor = cursor;
    await page.evaluate(() => {
      window.g3Offline = true;
      window.g3Wire.abort();
    });
    await expect(page.locator("#backgroundInspectionState")).toContainText("连接中断");
    await until(() => exists("watch-disconnected"), "backend saw the first browser disconnect");
    await fs.writeFile(file("release-first"), "");
    await until(() => exists("held-second"), "next read blocked after offline checkpoint");
    const during = db.prepare("SELECT e.seq, s.released_ns FROM background_inspection_events e " +
      "JOIN chat_group_run_slots s USING (run_id) WHERE e.run_id=? AND e.kind='step_completed'").all(run);
    assert.equal(during.length, 1);
    assert.ok(during[0].seq > cursor);
    assert.equal(during[0].released_ns, null);
    assert.equal(Number(await item.last().getAttribute("data-seq")), cursor);
    report.offline_checkpoint_seq = during[0].seq;
    await page.evaluate(origin => {
      window.g3Gateway = origin;
      window.g3Offline = false;
    }, origins[1]);
    await expect(panel.locator('li[data-seq="' + during[0].seq + '"]')).toContainText("已提交检查点");
    await until(() => report.watches.some(watch => watch.origin === origins[1] && watch.cursor === cursor),
      "second Envoy received original cursor");
    await fs.writeFile(file("release-second"), "");
    await expect(page.locator("#backgroundInspectionState")).toHaveText("结果已提交", { timeout: 25000 });
    const sequences = await item.evaluateAll(rows => rows.map(row => Number(row.dataset.seq)));
    assert.deepEqual(sequences, Array.from({ length: sequences.length }, (_, i) => i + 1));
    report.dom_sequences = sequences;
    const assistant = page.locator('#chatMessages .msg.assistant[data-run-id="' + run + '"]').filter({ hasText: "上新前商品巡检" });
    await expect(assistant).toHaveCount(1);
    report.answer_entry_id = await assistant.getAttribute("data-entry-id");
    await page.reload();
    await expect(assistant).toHaveCount(1);
    assert.equal(await assistant.getAttribute("data-entry-id"), report.answer_entry_id);
    const result = await page.request.get(origins[1] + "/api/chat/conversations/" + group + "/background-inspections/" + run, {
      headers: { Authorization: "Bearer " + login.data.token },
    });
    assert.equal((await result.json()).data.answer.entry_id, report.answer_entry_id);
    assert.deepEqual(report.pageErrors, []);
  } finally {
    db.close();
    await context.close();
    await video.saveAs(file("browser-reconnect.webm"));
    await video.delete();
    await browser.close();
    await fs.writeFile(file("browser.json"), JSON.stringify(report, null, 2));
  }
}

run().catch(error => { console.error(error); process.exitCode = 1; });
