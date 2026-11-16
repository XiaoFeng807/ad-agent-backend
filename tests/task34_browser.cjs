const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { DatabaseSync } = require("node:sqlite");
const { chromium, expect } = require("playwright/test");
const { browserOptions } = require("./browser_options.cjs");
const { submitGroupCreation } = require("./chat_group_create.cjs");
const { auth, delegation, displayedEvidence, expand, openProgress, panel } = require("./task30_checks.cjs");

const [base, input, output] = process.argv.slice(2);
const { message, sqlite } = JSON.parse(fs.readFileSync(input, "utf8"));
const groups = "/api/chat/conversations";
const result = { http: {}, sql: {}, rejects: {} };
const reader = new DatabaseSync(sqlite, { readOnly: true, timeout: 5000 });

const {
  actionPanel, actionPath, approvePause, readRefreshedPause, record, trackStatusUpdates, waitFor,
} = require("./group_pause_checks.cjs");
const sql = require("./group_pause_checks.cjs");
const query = (statement, ...args) => sql.query(reader, statement, ...args);
const change = (statement, ...args) => sql.change(sqlite, statement, ...args);
const snapshot = () => sql.snapshot(reader);

async function run() {
  const browser = await chromium.launch(browserOptions());
  result.chromium = browser.version();
  const page = await browser.newPage({ baseURL: base, viewport: { width: 1280, height: 1000 }, timezoneId: "Asia/Shanghai" });
  page.setDefaultTimeout(20000);
  const shot = name => page.screenshot({ path: path.join(path.dirname(output), name + ".png") });
  try {
    await page.addInitScript(() => {
      const fetch = window.fetch.bind(window);
      window.task34Streams = [];
      window.fetch = async (url, options) => {
        const response = await fetch(url, options);
        if (url === "/api/chat/stream") {
          const stream = { status: response.status };
          window.task34Streams.push(stream);
          response.clone().text().then(text => {
            stream.events = text.split(/\r?\n/).filter(line => line.startsWith("data: "))
              .map(line => JSON.parse(line.slice(6)));
          }).catch(error => { stream.error = error.message; });
        }
        return response;
      };
    });
    await page.goto(base);
    await page.locator("#loginUser").fill("task27-owner");
    await page.locator("#loginPass").fill(process.env.AD_TASK34_PASSWORD);
    await page.waitForFunction(() => Boolean(window._captchaId));
    await page.locator("#loginCaptcha").fill("0726");
    await page.locator("#loginPass").press("Enter");
    await page.locator('[data-page="chat"]').click();
    const headers = await auth(page);
    const request = async (method, url, data, credentials = headers) =>
      record(await page.request.fetch(url, { method, headers: credentials, data, timeout: 30000 }));
    const history = async id => (await request("GET", groups + "/" + id + "/history")).body.data;
    async function create(title) {
      const created = page.waitForResponse(r => new URL(r.url()).pathname === groups && r.request().method() === "POST");
      await submitGroupCreation(page, title);
      await expect(page.locator("#chatInput")).toBeEnabled();
      return (await (await created).json()).data.conversation_id;
    }
    async function start() {
      const index = await page.evaluate(() => window.task34Streams.length);
      await page.locator("#chatInput").fill(message);
      await page.locator("#chatSend").click();
      return index;
    }
    async function finish(index, type) {
      await page.waitForFunction(i => window.task34Streams[i]?.events || window.task34Streams[i]?.error, index);
      const { status, events, error } = await page.evaluate(i => window.task34Streams[i], index);
      assert.equal(status, 200);
      assert.equal(error, undefined);
      const terminal = events.filter(event => ["done", "stopped", "error", "superseded"].includes(event.type));
      assert.deepEqual(terminal.map(event => event.type), [type]);
      return terminal[0];
    }
    async function review(group) {
      const terminal = await finish(await start(), "done");
      const answer = (await history(group)).entries.find(e => e.run_id === terminal.run_id && e.kind === "assistant");
      assert.ok(answer?.has_evidence);
      return { terminal, answer, actions: answer.pause_actions || [] };
    }
    trackStatusUpdates(sqlite);
    const group = await create("TASK-034 · 独立复核与沙箱暂停");
    result.sql.before_review = snapshot();
    const initial = await review(group);
    const { action, runId, url, receipt } = await approvePause({
      page, group, initial, result, snapshot, shot, request,
    });
    await page.reload();
    await page.locator('[data-page="chat"]').click();
    await readRefreshedPause({ page, action, url, receipt, result, shot });

    const other = await create("TASK-034 · 对照工作群");
    await page.locator(`#chatGroupList [data-conversation-id="${group}"]`).click();
    const captcha = (await request("GET", "/api/captcha", undefined, {})).body;
    const otherLogin = await request("POST", "/api/login", {
      username: "task27-other", password: process.env.AD_TASK34_PASSWORD,
      captcha_id: captcha.captcha_id, captcha_text: "0726",
    }, {});
    assert.equal(otherLogin.status, 200);
    const otherHeaders = { Authorization: "Bearer " + otherLogin.body.data.token };
    for (const [name, target, credentials, status] of [
      ["wrong_group", actionPath(other, runId, action), headers, 404],
      ["wrong_run", actionPath(group, "wrong-run", action), headers, 404],
      ["non_owner", url, otherHeaders, 404],
      ["no_session", url, {}, 401],
      ["diagnosis_entry", "/api/diagnosis/pause-actions/" + action, headers, 404],
    ]) {
      result.rejects[name] = await request("POST", target + "/confirm", undefined, credentials);
      assert.equal(result.rejects[name].status, status);
      assert.equal((await request("GET", target, undefined, credentials)).status, status);
    }
    assert.deepEqual(snapshot(), result.sql.after_approval);
    change("UPDATE ad_plans SET status='active' WHERE id=103");
    result.http.later_status = await request("GET", url);
    assert.deepEqual(result.http.later_status.body.data, { receipt, actual_status: "active" });

    const second = await review(group);
    assert.equal(second.actions.length, 1);
    const retryAction = second.actions[0].action_id;
    const retryUrl = actionPath(group, second.terminal.run_id, retryAction);
    change("UPDATE ad_plans SET status='paused' WHERE id=103");
    const changed = snapshot();
    result.rejects.status_changed = await request("POST", retryUrl + "/confirm");
    assert.equal(result.rejects.status_changed.status, 409);
    assert.deepEqual(snapshot(), changed);
    change("UPDATE ad_plans SET status='active' WHERE id=103");
    result.sql.before_fault = snapshot();
    change(`CREATE TRIGGER task34_receipt_failure BEFORE INSERT ON chat_group_pause_receipts
            WHEN (SELECT status FROM ad_plans WHERE id=103)='paused' BEGIN
              SELECT RAISE(ABORT, 'task34 injected after state update');
            END`);
    result.http.injected_failure = await request("POST", retryUrl + "/confirm");
    assert.equal(result.http.injected_failure.status, 503);
    result.sql.after_fault = snapshot();
    assert.deepEqual(result.sql.after_fault, result.sql.before_fault);
    assert.equal((await request("GET", retryUrl)).status, 404);
    change("DROP TRIGGER task34_receipt_failure");
    result.http.concurrent = await Promise.all(Array.from({ length: 6 }, () => request("POST", retryUrl + "/confirm")));
    for (const reply of result.http.concurrent) {
      assert.equal(reply.status, 200);
      assert.deepEqual(reply.body, result.http.concurrent[0].body);
      assert.equal(reply.body.data.action_id, retryAction);
    }
    result.sql.after_retry = snapshot();
    assert.equal(result.sql.after_retry.receipts.length, result.sql.after_fault.receipts.length + 1);
    assert.deepEqual(result.sql.after_retry.updates.slice(result.sql.after_fault.updates.length),
      [{ plan_id: 103, old_status: "active", new_status: "paused" }]);
    assert.deepEqual(result.sql.after_retry.plans,
      result.sql.after_fault.plans.map(row => row.id === 103 ? { ...row, status: "paused" } : row));

    change("UPDATE ad_plans SET status='active' WHERE id=103");
    const registration = query("SELECT * FROM diagnosis_sandbox_targets WHERE plan_id=103")[0];
    change("DELETE FROM diagnosis_sandbox_targets WHERE plan_id=103");
    const unregistered = snapshot();
    const withoutTarget = await review(group);
    assert.deepEqual(withoutTarget.actions, []);
    assert.deepEqual(snapshot(), unregistered);
    result.rejects.unregistered = { answer: withoutTarget.answer.entry_id, proposals: 0 };
    result.rejects.revoked_registration = await request("GET", retryUrl);
    assert.equal(result.rejects.revoked_registration.status, 404);
    change("INSERT INTO diagnosis_sandbox_targets VALUES (?, ?, ?)", registration.plan_id, registration.user_id, registration.registration_id);
    const binding = query("SELECT * FROM plan_metric_bindings WHERE plan_id=101")[0];
    change("DELETE FROM plan_metric_bindings WHERE plan_id=101");
    const withGap = snapshot();
    const incomplete = await review(group);
    assert.deepEqual(incomplete.actions, []);
    const gap = await displayedEvidence(page, group, incomplete.terminal.run_id);
    assert.equal(gap.gap, "plan_history_not_mapped");
    assert.deepEqual(snapshot(), withGap);
    result.rejects.insufficient_evidence = gap;
    change("INSERT INTO plan_metric_bindings VALUES (?, ?, ?, ?)",
      binding.plan_id, binding.source_id, binding.data_version, binding.source_plan_id);

    result.sql.before_stop = snapshot();
    assert.equal((await request("POST", "/__task34/hold", undefined, {})).status, 200);
    const stoppedStream = await start();
    const held = await request("GET", "/__task28/wait", undefined, {});
    assert.equal(held.status, 200);
    const current = (await history(group)).runs.find(r => r.status === "running");
    assert.ok(current);
    const stoppedResponse = waitFor(page, "POST", groups + "/" + group + "/stop");
    page.once("dialog", async dialog => {
      assert.match(dialog.message(), new RegExp(current.run_id));
      await dialog.accept();
    });
    await delegation(page, current.run_id).getByRole("button", { name: "停止本轮" }).click();
    result.http.stop = await record(await stoppedResponse);
    assert.equal(result.http.stop.status, 200);
    assert.equal((await request("POST", "/__task28/release", undefined, {})).status, 200);
    result.stop = { held: held.body, terminal: await finish(stoppedStream, "stopped") };
    const stopHistory = await history(group);
    const stopped = stopHistory.runs.find(r => r.run_id === current.run_id);
    assert.equal(stopped.status, "stopped");
    assert.deepEqual(stopped.stop, result.http.stop.body.data);
    assert.equal(stopped.stop.stopped_by_user_id, 17);
    assert.ok(!stopHistory.entries.some(e => e.run_id === current.run_id && e.kind === "assistant"));
    assert.ok(stopHistory.entries.some(e => e.run_id === current.run_id && e.event?.kind === "reclaimed"));
    result.stop.records = query("SELECT * FROM chat_group_entries WHERE run_id=? AND kind IN ('stop','assistant')",
      current.run_id);
    assert.deepEqual(result.stop.records.map(row => row.kind), ["stop"]);
    result.sql.after_stop = snapshot();
    assert.deepEqual(result.sql.after_stop, result.sql.before_stop);
    await page.reload();
    await page.locator('[data-page="chat"]').click();
    const stoppedPanel = panel(page, current.run_id);
    await openProgress(page, current.run_id);
    await expect(stoppedPanel.locator(":scope > summary")).toHaveText("协作进度 · 已停止");
    await expand(stoppedPanel);
    await expect(stoppedPanel.locator(".group-stop-record")).toContainText("task27-owner");
    await expect(stoppedPanel.locator(".group-stop-record")).toContainText(current.run_id);
    await stoppedPanel.locator(".group-stop-record").scrollIntoViewIfNeeded();
    await shot("04-stop-refresh");
    await page.locator(`[data-result-kind="pause"][data-action-id="${action}"]`).click();
    await expect(actionPanel(page, action)).toContainText("批准暂停 #103");
    result.stop.history = stopHistory;
  } finally {
    fs.writeFileSync(output, JSON.stringify(result, null, 2));
    await browser.close();
  }
}

run().catch(error => { console.error(error); process.exitCode = 1; }).finally(() => reader.close());
