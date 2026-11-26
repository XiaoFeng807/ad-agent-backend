const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");
const { expect } = require("playwright/test");
const { browserOptions } = require("./browser_options.cjs");
const { submitGroupCreation } = require("./chat_group_create.cjs");
const { auth, delegation, displayedEvidence, evidencePath, expand, openProgress, panel, progress, progressButton,
  taskRows, uniqueEntries, verifyProgress } = require("./task30_checks.cjs");

const [base, input, output] = process.argv.slice(2);
const fixture = JSON.parse(fs.readFileSync(input, "utf8"));
const groups = "/api/chat/conversations";
const result = {};
const screenshot = name => path.join(path.dirname(output), name+".png");

async function login(page, user) {
  await page.goto(base);
  await page.locator("#loginUser").fill(user);
  await page.locator("#loginPass").fill(process.env.AD_TASK28_PASSWORD);
  await page.waitForFunction(() => Boolean(window._captchaId));
  await page.locator("#loginCaptcha").fill("0726");
  await page.locator("#loginPass").press("Enter");
  await page.locator('[data-page="chat"]').click();
}

async function create(page, name) {
  const created = page.waitForResponse(response => new URL(response.url()).pathname === groups &&
    response.request().method() === "POST");
  await submitGroupCreation(page, name);
  const response = await created;
  assert.equal(response.status(), 201);
  const group = (await response.json()).data.conversation_id;
  await page.waitForFunction(id => document.querySelector("#chatGroupList [aria-current]")?.dataset.conversationId === id, group);
  return group;
}

async function send(page, message) {
  await page.locator("#chatInput").fill(message);
  await page.locator("#chatSend").click();
}

async function history(page, group) {
  const response = await page.request.get(`${groups}/${group}/history`, { headers: await auth(page) });
  assert.equal(response.status(), 200);
  return (await response.json()).data;
}

async function finished(page, group) {
  await page.waitForFunction(id => Boolean(document.querySelector("#chatGroupList [aria-current]")?.dataset.conversationId === id &&
    !document.querySelector("#chatSend").disabled && document.querySelector("#chatMessages .msg.assistant")), group);
  const data = await history(page, group);
  assert.ok(data.runs.every(run => run.status === "completed"));
  return data.runs.at(-1);
}

async function inlineProgress(page, run, finished = false) {
  const section = delegation(page, run);
  await expect(section).toHaveCount(1);
  await expect(section).toBeVisible();
  await expect(page.locator(`#chatMessages > .msg[data-run-id="${run}"] [data-result-kind="progress"]`)).toHaveCount(0);
  await expect(section.locator(".workbench-delegation-row[open]")).toHaveCount(0);
  await expect(section.locator("code:visible")).toHaveCount(0);
  await expect(section).not.toContainText(run);
  const relation = await section.evaluate((element, id) => {
    const siblings = Array.from(element.parentElement.children);
    const answer = siblings.find(row => row.matches(`.msg.assistant[data-run-id="${id}"]`));
    const question = siblings.find(row => row.matches(`.msg.user[data-run-id="${id}"]`));
    const position = siblings.indexOf(element);
    return { afterQuestion: Boolean(question) && siblings.indexOf(question) < position,
      beforeAnswer: answer ? position < siblings.indexOf(answer) : null };
  }, run);
  assert.deepEqual(relation, { afterQuestion: true, beforeAnswer: finished ? true : null });
  const rows = await section.locator("[data-task-label][data-state]").evaluateAll(elements => elements.map(element => ({
    label: element.dataset.taskLabel, state: element.dataset.state,
    dispatched: element.hasAttribute("data-dispatched"), text: element.querySelector("summary").innerText,
  })));
  assert.equal(new Set(rows.map(row => row.label)).size, rows.length);
  assert.ok(rows.every(row => !/[a-f0-9]{24,}/i.test(row.text)), "Default progress exposed a technical ID");
  return { relation, rows };
}

async function run() {
  const browser = await chromium.launch(browserOptions());
  result.chromium = browser.version();
  try {
    const page = await browser.newPage({ baseURL: base, viewport: { width: 1440, height: 1000 }, locale: "en-US" });
    await page.addInitScript(() => {
      const fetch = window.fetch.bind(window);
      window.task30Streams = {};
      window.fetch = async (url, options) => {
        const response = await fetch(url, options);
        if (url === "/api/chat/stream") {
          const group = JSON.parse(options.body).conversation_id;
          response.clone().text().then(text => { window.task30Streams[group] = text; });
        }
        return response;
      };
    });
    await login(page, "task27-owner");
    const select = id => page.locator(`#chatGroupList [data-conversation-id="${id}"]`).click();
    if (fixture.mode === "gap") {
      const group = await create(page, "缺项复核");
      await send(page, fixture.message);
      const run = await finished(page, group);
      const gapInline = delegation(page, run.run_id);
      await expect(gapInline.locator('[data-task-label][data-state="failed"]').filter({ hasText: /#102.*未绑定历史来源/ })).toBeVisible();
      await expect(gapInline.locator('[data-task-label][data-state="blocked"]').filter({ hasText: "独立复核" })).toBeVisible();
      result.gap_inline = await inlineProgress(page, run.run_id, true);
      await expect(panel(page, run.run_id)).toHaveCount(0);
      await expect(progressButton(page, run.run_id)).toHaveAttribute("aria-expanded", "false");
      await gapInline.screenshot({ path: screenshot("blocked-inline") });
      await page.screenshot({ path: screenshot("blocked-thread") });
      result.progress = await progress(page, run.run_id);
      assert.equal(result.progress.summary, "协作进度 · 部分受阻");
      verifyProgress(result.progress, await history(page, group), run.run_id);
      const blocked = result.progress.entries.find(row => row.kind === "failed" && row.text.includes("复核未启动"));
      const missing = result.progress.entries.find(row => /#102.*2026-10-17/.test(row.text) && row.kind === "failed");
      assert.ok(missing);
      assert.ok(blocked.text.includes(missing.task_label));
      assert.ok(result.progress.entries.some(row => row.task_label === missing.task_label && row.kind === "reclaimed"));
      const gap = result.progress.tasks.find(row => row.task_label === missing.task_label);
      assert.match(gap.text, /缺项 · #102 基准窗、当前窗 · 未绑定历史来源/);
      const blockedSummary = /独立复核 · 受阻 · 任务 \d+ #102 基准窗、当前窗 · 未绑定历史来源/;
      assert.match(result.progress.tasks.find(row => row.state === "blocked").text, blockedSummary);
      assert.equal(result.progress.entries.some(row => row.kind === "review_inputs"), false);
      // The scheduler can commit the dependency failure before the producer's terminal progress event.
      await page.route(`**${groups}/${group}/history`, async route => {
        const response = await route.fetch();
        const payload = await response.json();
        payload.data.entries = payload.data.entries.filter(({ event }) => !(event?.task_label === missing.task_label &&
          (event.kind === "reclaimed" || (event.kind === "failed" && !event.source))));
        await route.fulfill({ response, json: payload });
      }, { times: 1 });
      await page.evaluate(() => readChatGroupHistory());
      result.gap_before_worker_terminal = await taskRows(panel(page, run.run_id));
      assert.match(result.gap_before_worker_terminal.find(row => row.state === "blocked").text, blockedSummary);
      await page.evaluate(() => readChatGroupHistory());
      const gapPanel = panel(page, run.run_id);
      await gapPanel.screenshot({ path: screenshot("blocked-progress") });
      await expand(gapPanel.locator(`.group-task[data-task-label="${missing.task_label}"]`));
      await gapPanel.screenshot({ path: screenshot("blocked-expanded") });
      result.basis = await displayedEvidence(page, group, run.run_id);
      assert.deepEqual(result.basis.metrics.filter(metric => metric.gap).map(metric => [metric.plan_id, metric.gap]),
        [[102, "plan_history_not_mapped"], [102, "plan_history_not_mapped"]]);
      assert.deepEqual(result.basis.details, []);
      await page.locator(".group-evidence").screenshot({ path: screenshot("missing-basis") });
      return;
    }

    await select("legacy-17");
    await page.getByText("LEGACY_QUESTION", { exact: true }).waitFor();
    assert.deepEqual(await uniqueEntries(page), ["legacy:41", "legacy:42"]);
    const a = await create(page, "例会复核 A");
    await send(page, fixture.message);
    assert.equal((await page.request.get("/__task28/stage/before-read")).status(), 200);
    const aRun = (await history(page, a)).runs[0];
    const aInline = delegation(page, aRun.run_id);
    await openProgress(page, aRun.run_id);
    const aPanel = panel(page, aRun.run_id);
    await page.waitForFunction(run => document.querySelectorAll(
      `.group-progress[data-run-id="${run}"] [data-event="succeeded"]`).length === 2, aRun.run_id);
    const pending = aPanel.locator('.group-task').filter({ hasText: "指标取证待开始" });
    await pending.locator(":scope > summary").waitFor();
    const label = await pending.getAttribute("data-task-label");
    const metricTask = aPanel.locator(`.group-task[data-task-label="${label}"]`);
    const reviewTask = aPanel.locator(".group-task").filter({ has: page.locator("summary", { hasText: "独立复核" }) });
    result.before_read = await progress(page, aRun.run_id);
    assert.ok(result.before_read.tasks.every(task => !task.open));
    assert.ok(!result.before_read.entries.some(row => row.task_label === label && row.kind === "tool_started"));
    const inlineMetric = aInline.locator(`.workbench-delegation-row[data-task-label="${label}"]`);
    const inlineReview = aInline.locator('[data-state="waiting"]');
    const waitRows = inlineReview.locator(".workbench-delegation-wait > li");
    const metricAgent = await inlineMetric.locator(".workbench-delegation-name").textContent();
    assert.equal(metricAgent, "#101 · 投放指标取证");
    const expectedWait = `等待 ${metricAgent} 交回 #101 基准窗（10 月 17–23 日）、当前窗（10 月 24–30 日）的曝光、点击和 CTR`;
    const beforeRead = await inlineProgress(page, aRun.run_id);
    assert.equal(beforeRead.rows.filter(row => row.dispatched).length, 3);
    assert.equal(beforeRead.rows.find(row => row.state === "waiting").dispatched, false);
    await aPanel.screenshot({ path: screenshot("before-read") });
    await expand(metricTask);
    await expect(waitRows).not.toBeVisible();
    await expand(inlineReview);
    await expect(waitRows).toBeVisible();
    await expect(waitRows).toHaveText([expectedWait]);
    result.waiting_before_read = await waitRows.allTextContents();
    await expect(inlineMetric).not.toHaveAttribute("open", "");
    const inlineReviewSummary = inlineReview.locator(":scope > summary");
    await expect(inlineReviewSummary).toBeFocused();
    assert.equal((await page.request.post("/__task28/stage/before-read")).status(), 200);
    assert.equal((await page.request.get("/__task28/wait", { timeout: 55000 })).status(), 200);
    await metricTask.locator(":scope > summary").filter({ hasText: "正在查 #101" }).waitFor();
    result.waiting = await progress(page, aRun.run_id);
    assert.ok(result.waiting.tasks.find(row => row.task_label === label).open);
    assert.match(await metricTask.locator(":scope > summary").textContent(), /正在查 #101 · 10 月 17–23 日 · 曝光与点击$/);
    assert.match(await reviewTask.locator(":scope > summary").textContent(), /独立复核 · 等待 #101 基准窗$/);
    assert.ok(result.waiting.entries.some(row => row.kind === "dispatched" && row.text.includes("指标")));
    assert.ok(!result.waiting.entries.some(row => row.kind === "review_inputs"));
    const historyPattern = `**${groups}/${a}/history`;
    const duplicate = async route => {
      const response = await route.fetch();
      const payload = await response.json();
      payload.data.entries.push(...payload.data.entries);
      await route.fulfill({ response, json: payload });
    };
    await page.route(historyPattern, duplicate);
    const waitingIds = await uniqueEntries(page);
    for (let index = 0; index < 2; index++) {
      await page.evaluate(() => readChatGroupHistory());
      assert.deepEqual(await uniqueEntries(page), waitingIds);
      assert.equal(await metricTask.getAttribute("open"), "");
      await expect(inlineReview).toHaveAttribute("open", "");
      await expect(inlineReviewSummary).toBeFocused();
    }
    await page.unroute(historyPattern, duplicate);
    verifyProgress(result.waiting, await history(page, a), aRun.run_id);
    await metricTask.locator(":scope > summary").click();
    await aPanel.screenshot({ path: screenshot("waiting") });
    await expand(metricTask);
    await aPanel.screenshot({ path: screenshot("waiting-expanded") });
    await page.keyboard.press("Escape");
    await expect(progressButton(page, aRun.run_id)).toHaveAttribute("aria-expanded", "false");
    await expect(waitRows).toBeVisible();
    await expect(waitRows).toHaveText([expectedWait]);
    await aInline.screenshot({ path: screenshot("waiting-inline-expanded") });
    await inlineReviewSummary.click();
    await expect(waitRows).not.toBeVisible();
    await expect(inlineMetric.locator(":scope > summary")).toContainText("正在查 #101");
    result.waiting_inline = await inlineProgress(page, aRun.run_id);
    const dispatched = (await history(page, a)).entries.filter(row => row.event?.kind === "dispatched")
      .map(row => row.event.task_label);
    assert.deepEqual(result.waiting_inline.rows.filter(row => row.dispatched).map(row => row.label), [...new Set(dispatched)]);
    await aInline.screenshot({ path: screenshot("waiting-inline") });
    await page.screenshot({ path: screenshot("waiting-thread") });

    const staleCaptured = Promise.withResolvers();
    const allowStale = Promise.withResolvers();
    await page.route(historyPattern, async route => {
      const response = await route.fetch();
      staleCaptured.resolve();
      await allowStale.promise;
      await route.fulfill({ response });
    }, { times: 1 });
    const staleRead = page.evaluate(() => readChatGroupHistory());
    await staleCaptured.promise;

    const b = await create(page, "例会复核 B");
    await send(page, fixture.message);
    const bRun = await finished(page, b);
    result.b = await displayedEvidence(page, b, bRun.run_id);
    result.b_progress = await progress(page, bRun.run_id);
    const bIds = await uniqueEntries(page);
    allowStale.resolve();
    await staleRead;
    assert.deepEqual(await progress(page, bRun.run_id), result.b_progress);
    assert.equal((await page.request.post("/__task28/release")).status(), 200);
    assert.equal((await page.request.get("/__task28/stage/review")).status(), 200);
    await select(a);
    await openProgress(page, aRun.run_id);
    await reviewTask.locator(":scope > summary").filter({ hasText: "已接收 4 份指标、1 份规则 · 正在复核" }).waitFor();
    result.live_handoff = await progress(page, aRun.run_id);
    assert.ok(result.live_handoff.tasks.every(row => row.state !== "waiting"));
    await expect(aInline.locator(".workbench-delegation-wait")).toHaveCount(0);
    verifyProgress(result.live_handoff, await history(page, a), aRun.run_id);
    await aPanel.screenshot({ path: screenshot("review-default") });
    await expand(reviewTask);
    await page.setViewportSize({ width: 1440, height: 1600 });
    await reviewTask.screenshot({ path: screenshot("handoff") });
    await page.setViewportSize({ width: 390, height: 844 });
    const narrowSummary = reviewTask.locator(":scope > summary");
    await narrowSummary.click();
    assert.equal(await reviewTask.getAttribute("open"), null);
    await aPanel.screenshot({ path: screenshot("narrow-default") });
    await narrowSummary.click();
    const handoff = reviewTask.locator('[data-event="review_inputs"]');
    await handoff.waitFor();
    const ids = await handoff.locator("code").all();
    assert.equal(ids.length, 17);
    assert.ok(await handoff.evaluate(row => row.scrollWidth <= row.clientWidth &&
      [...row.querySelectorAll("code")].every(code => code.getBoundingClientRect().right <= row.getBoundingClientRect().right + 1)));
    for (const id of ids) {
      await id.scrollIntoViewIfNeeded();
      assert.equal(await id.evaluate(code => {
        const rect = code.getBoundingClientRect();
        const chat = document.querySelector("#workbenchDetailBody").getBoundingClientRect();
        return rect.left >= chat.left && rect.right <= chat.right && rect.top >= chat.top && rect.bottom <= chat.bottom;
      }), true);
    }
    await page.setViewportSize({ width: 390, height: 1400 });
    await handoff.screenshot({ path: screenshot("narrow-expanded") });
    await page.setViewportSize({ width: 1440, height: 1000 });
    await select(b);
    await openProgress(page, bRun.run_id);
    assert.equal((await page.request.post("/__task28/stage/review")).status(), 200);
    await page.waitForFunction(id => Boolean(window.task30Streams[id]), a);
    const events = (await page.evaluate(id => window.task30Streams[id], a)).split(/\r?\n/)
      .filter(line => line.startsWith("data: ")).map(line => JSON.parse(line.slice(6)));
    assert.equal(events.at(-1).type, "done");
    assert.equal(events.at(-1).run_id, aRun.run_id);
    assert.equal(await page.locator("#chatGroupList [aria-current]").getAttribute("data-conversation-id"), b);
    assert.equal(await panel(page, aRun.run_id).count(), 0);
    assert.deepEqual(await uniqueEntries(page), bIds);
    assert.deepEqual(await progress(page, bRun.run_id), result.b_progress);
    assert.deepEqual(await displayedEvidence(page, b, bRun.run_id), result.b);

    await select(a);
    await expect(delegation(page, aRun.run_id).locator('[data-task-label][data-state="succeeded"]').filter({ hasText: "独立复核" })).toBeVisible();
    result.completed_inline = await inlineProgress(page, aRun.run_id, true);
    assert.ok(result.completed_inline.rows.every(row => row.state === "succeeded" && row.dispatched && row.text.includes("已交回")));
    const completedMetric = delegation(page, aRun.run_id).locator(`.workbench-delegation-row[data-task-label="${label}"]`);
    await expand(completedMetric);
    const receiptIds = (await history(page, a)).entries.filter(row => row.event?.task_label === label && row.event.kind === "receipt")
      .map(row => row.event.artifact_id);
    const shownReceipts = completedMetric.locator('[data-event="receipt"] [data-artifact-id]');
    await shownReceipts.first().waitFor();
    assert.deepEqual(await shownReceipts.evaluateAll(nodes => nodes.map(node => node.dataset.artifactId)),
      [...new Set(receiptIds)]);
    await completedMetric.locator(":scope > summary").click();
    await delegation(page, aRun.run_id).screenshot({ path: screenshot("completed-inline") });
    await page.screenshot({ path: screenshot("completed-thread") });
    result.a_progress = await progress(page, aRun.run_id);
    verifyProgress(result.a_progress, await history(page, a), aRun.run_id);
    result.a = await displayedEvidence(page, a, aRun.run_id);
    assert.notEqual(result.a.details[0].review_input.worker_id, result.b.details[0].review_input.worker_id);
    const acceptedIds = data => [...data.metrics, data.rule, ...data.details].map(row => row.artifact_id);
    assert.ok(acceptedIds(result.a).every(id => !acceptedIds(result.b).includes(id)));
    const aIds = await uniqueEntries(page);
    await page.setViewportSize({ width: 1440, height: 1800 });
    await page.reload();
    await page.waitForFunction(id => document.querySelector("#chatGroupList [aria-current]")?.dataset.conversationId === id, a);
    await expect(delegation(page, aRun.run_id).locator('[data-task-label][data-state="succeeded"]').filter({ hasText: "独立复核" })).toBeVisible();
    assert.deepEqual(await inlineProgress(page, aRun.run_id, true), result.completed_inline);
    assert.deepEqual(await displayedEvidence(page, a, aRun.run_id), result.a);
    assert.deepEqual(await uniqueEntries(page), aIds);
    await page.locator(".group-evidence").screenshot({ path: screenshot("basis") });

    result.access = [];
    const other = await browser.newPage({ baseURL: base });
    await login(other, "task27-other");
    for (const [client, group, run] of [[page, b, aRun.run_id], [page, a, bRun.run_id], [other, a, aRun.run_id]]) {
      const response = await client.request.get(evidencePath(group, run), { headers: await auth(client) });
      assert.equal(response.status(), 404);
      result.access.push({ group, run, status: response.status(), body: await response.json() });
    }
    await send(page, fixture.status_request);
    await page.getByText("#101 当前状态 active。", { exact: true }).waitFor();
    const simple = (await history(page, a)).runs.at(-1);
    assert.equal(await panel(page, simple.run_id).count(), 0);
    assert.equal(await delegation(page, simple.run_id).count(), 0);
    assert.equal(await page.locator(`#chatMessages [data-run-id="${simple.run_id}"] .workbench-card`).count(), 0);
    result.simple = simple;
  } finally {
    fs.writeFileSync(output, JSON.stringify(result, null, 2));
    await browser.close();
  }
}

run().catch(error => { console.error(error); process.exitCode = 1; });
