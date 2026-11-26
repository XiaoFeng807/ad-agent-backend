const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { DatabaseSync } = require("node:sqlite");
const { chromium } = require("playwright");
const { browserOptions } = require("./browser_options.cjs");
const { submitGroupCreation } = require("./chat_group_create.cjs");
const { auth, displayedEvidence, progress, progressButton, verifyProgress } = require("./task30_checks.cjs");
const { query, snapshot } = require("./group_pause_checks.cjs");

const [baseURL, input, output] = process.argv.slice(2);
const fixture = JSON.parse(fs.readFileSync(input, "utf8"));
const groups = "/api/chat/conversations";
const stream = "/api/chat/stream";
const result = {};
const timeout = 120000;
const save = () => fs.writeFileSync(output, JSON.stringify(result, null, 2));

async function run() {
  const browser = await chromium.launch(browserOptions());
  result.chromium = browser.version();
  const db = new DatabaseSync(fixture.sqlite, { readOnly: true, timeout: 5000 });
  let page;
  try {
    result.before = snapshot(db);
    page = await browser.newPage({ baseURL, viewport: { width: 1440, height: 1200 }, locale: "en-US" });
    await page.addInitScript(() => {
      const fetch = window.fetch.bind(window);
      window.task36Streams = [];
      window.fetch = async (url, options) => {
        const response = await fetch(url, options);
        if (url === "/api/chat/stream") {
          response.clone().text().then(text => window.task36Streams.push(text));
        }
        return response;
      };
    });
    const pauseRequests = [];
    page.on("request", request => {
      if (request.url().includes("/pause-actions/")) {
        pauseRequests.push({ method: request.method(), path: new URL(request.url()).pathname });
      }
    });
    await page.goto(baseURL);
    await page.locator("#loginUser").fill("task27-owner");
    await page.locator("#loginPass").fill(process.env.AD_TASK36_PASSWORD);
    await page.waitForFunction(() => Boolean(window._captchaId));
    await page.locator("#loginCaptcha").fill("0726");
    await page.locator("#loginPass").press("Enter");
    const post = url => page.waitForResponse(response =>
      new URL(response.url()).pathname === url && response.request().method() === "POST", { timeout });
    const alertSql = "SELECT * FROM alerts ORDER BY id";
    if (fixture.ctr_plan_id != null) {
      const pathName = `/api/plans/${fixture.ctr_plan_id}/ctr-alert-check`;
      assert.deepEqual(query(db, alertSql), []);
      await page.locator('[data-page="plans"]').evaluate(link => link.click());
      const check = page.locator(`button[data-ctr-plan="${fixture.ctr_plan_id}"]`);
      const pending = post(pathName);
      await check.click();
      const reply = await pending;
      result.ctr_check = {request: {method: reply.request().method(), path: pathName, body: reply.request().postData()},
        http: reply.status(), response: await reply.json(), alert_sql: alertSql, rows: query(db, alertSql)};
      save();
      assert.equal(reply.status(), 200);
      const data = result.ctr_check.response.data;
      assert.equal(data.status, "created");
      const [row] = result.ctr_check.rows;
      assert.equal(result.ctr_check.rows.length, 1);
      assert.equal(row.id, data.alert_id);
      const status = check.locator("xpath=../..").getByRole("status");
      await status.getByText(`已生成严重告警 #${data.alert_id}`).waitFor();
      result.ctr_check.rendered = await status.innerText();
      assert.ok(result.ctr_check.rendered.includes("检测时间"));
      for (const window of [data.baseline, data.current]) {
        for (const day of [window.start_date, window.end_date]) assert.ok(result.ctr_check.rendered.includes(day));
      }
      await status.screenshot({ path: path.join(path.dirname(output), "plan-ctr-check.png") });
    }
    await page.locator('[data-page="chat"]').click();
    const creation = post(groups);
    await submitGroupCreation(page, (fixture.suite || "TASK-036") + " · " + fixture.name);
    const created = await creation;
    assert.equal(created.status(), 201);
    const group = (await created.json()).data.conversation_id;
    result.conversation_id = group;

    await page.locator("#chatInput").fill(fixture.message);
    const submission = post(stream);
    const started = performance.now();
    await page.locator("#chatSend").click();
    const response = await submission;
    result.request = response.request().postDataJSON();
    assert.deepEqual(result.request, { message: fixture.message, conversation_id: group });
    save();
    await page.waitForFunction(() => window.task36Streams.length === 1, null, { timeout });
    result.turn_ms = Math.round(performance.now() - started);
    result.sse = (await page.evaluate(() => window.task36Streams[0])).split(/\r?\n/)
      .filter(line => line.startsWith("data: ")).map(line => JSON.parse(line.slice(6)));
    const terminal = result.sse.at(-1);
    save();
    assert.equal(terminal.type, "done");
    assert.deepEqual(result.sse.filter(row => ["done", "error", "stopped", "superseded"].includes(row.type)), [terminal]);
    const run = terminal.run_id;
    const answer = page.locator(`#chatMessages > .assistant[data-run-id="${run}"]`).last();
    const history = async () => {
      const response = await page.request.get(`${groups}/${group}/history`, { headers: await auth(page) });
      assert.equal(response.status(), 200);
      return (await response.json()).data;
    };
    const read = async () => {
      await answer.waitFor({ state: "attached" });
      const evidence = answer.locator('[data-result-kind="evidence"]');
      const basis = !fixture.suite || await evidence.count() ? await displayedEvidence(page, group, run) : null;
      const rendered = (await answer.textContent()).split("查看依据")[0];
      return {
        answer: rendered,
        basis,
        history: await history(),
        progress: !fixture.suite || await progressButton(page, run).count()
          ? await progress(page, run) : null,
        pause: await answer.locator('[data-result-kind="pause"]').allTextContents(),
        approvable: await answer.locator('[data-result-kind="pause"]').count(),
      };
    };
    const shot = (locator, name) => locator.screenshot({ path: path.join(path.dirname(output), name + ".png") });
    result.initial = await read();
    save();
    await shot(answer, "answer");
    await page.reload();
    result.refreshed = await read();
    if (result.ctr_check) result.ctr_check.refreshed_rows = query(db, alertSql);
    save();
    await shot(answer, "refreshed-answer");
    if (result.refreshed.basis) {
      await displayedEvidence(page, group, run);
      await shot(page.locator("#workbenchDetail .group-evidence"), "refreshed-basis");
    }
    assert.deepEqual(result.refreshed, result.initial);
    if (result.refreshed.progress) verifyProgress(result.refreshed.progress, result.refreshed.history, run);
    const answerEntry = result.refreshed.history.entries.find(row => row.kind === "assistant" && row.run_id === run);
    const entry = Number(answerEntry.entry_id.split(":")[1]);
    const where = "WHERE p.run_id=? AND p.entry_id=?";
    result.scoped = {
      proposals: query(db, "SELECT p.* FROM chat_group_pause_proposals p " + where, run, entry),
      receipts: query(db, "SELECT r.* FROM chat_group_pause_receipts r " +
        "JOIN chat_group_pause_proposals p USING(action_id) " + where, run, entry),
    };
    result.after = snapshot(db);
    result.pause_requests = pauseRequests;
  } catch (error) {
    await page?.screenshot({ path: path.join(path.dirname(output), "failure.png"), timeout: 5000 }).catch(() => {});
    throw error;
  } finally {
    save();
    db.close();
    await browser.close();
  }
}

run().catch(error => { console.error(error); process.exitCode = 1; });
