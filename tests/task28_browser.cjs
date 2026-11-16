const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");
const { browserOptions } = require("./browser_options.cjs");
const { submitGroupCreation } = require("./chat_group_create.cjs");
const { auth, delegation, displayedEvidence, evidencePath, expand, panel, progress, uniqueEntries, verifyProgress } = require("./task30_checks.cjs");

const [base, input, output] = process.argv.slice(2);
const fixture = JSON.parse(fs.readFileSync(input, "utf8"));
const groups = "/api/chat/conversations";
const stream = "/api/chat/stream";
const result = {};
const timeout = 100000;
const save = () => fs.writeFileSync(output, JSON.stringify(result, null, 2));
const shot = (locator, name) => locator.screenshot({ path: path.join(path.dirname(output), name + ".png") });
const post = (page, path) => page.waitForResponse(response =>
  new URL(response.url()).pathname === path && response.request().method() === "POST", { timeout });

async function sse(page, index) {
  await page.waitForFunction(index => window.task28Streams[index]?.text !== undefined, index, { timeout });
  const capture = await page.evaluate(index => window.task28Streams[index], index);
  assert.equal(capture.error, undefined);
  return capture.text.split(/\r?\n/).filter(line => line.startsWith("data: "))
    .map(line => JSON.parse(line.slice(6)));
}

async function run() {
  const browser = await chromium.launch(browserOptions());
  result.chromium = browser.version();
  let page;
  let pause;
  let pauseState;
  try {
    page = await browser.newPage({ baseURL: base, viewport: { width: 1440, height: 1200 }, locale: "en-US" });
    await page.addInitScript(() => {
      const fetch = window.fetch.bind(window);
      window.task28Streams = [];
      window.fetch = async (url, options) => {
        const capture = url === "/api/chat/stream" ? {} : null;
        if (capture) window.task28Streams.push(capture);
        const response = await fetch(url, options);
        if (capture) response.clone().text().then(text => { capture.text = text; }, error => {
          capture.text = "";
          capture.error = error.message;
        });
        return response;
      };
    });
    await page.goto(base);
    await page.locator("#loginUser").fill("task27-owner");
    await page.locator("#loginPass").fill(process.env.AD_TASK28_PASSWORD);
    await page.waitForFunction(() => Boolean(window._captchaId));
    await page.locator("#loginCaptcha").fill("0726");
    await page.locator("#loginPass").press("Enter");
    await page.locator('[data-page="chat"]').click();
    const creation = post(page, groups);
    await submitGroupCreation(page, fixture.pause ? "TASK-035 · 同群复核与沙箱暂停" :
      fixture.capture_wait ? "TASK-032" : fixture.old_plan_ids ? "TASK-029" : "TASK-028");
    const created = await creation;
    assert.equal(created.status(), 201);
    const id = (await created.json()).data.conversation_id;
    result.conversation_id = id;
    if (fixture.pause) {
      pause = require("./task35_checks.cjs");
      pauseState = pause.open(fixture.pause);
      result.pause = pauseState.result;
      save();
    }
    await page.locator("#chatInput").fill(fixture.message);
    const initial = post(page, stream);
    await page.locator("#chatSend").click();
    const old = await initial;
    result.old_request = old.request().postDataJSON();
    assert.deepEqual(result.old_request, { message: fixture.message, conversation_id: id });
    save();

    const waiting = await page.request.get(base + "/__task28/wait", { timeout });
    assert.equal(waiting.status(), 200);
    if (fixture.capture_wait) {
      result.held = await waiting.json();
      await progress(page, result.held.run_id || await delegation(page).getAttribute("data-run-id"));
      const started = page.locator(`[data-event="tool_started"][data-runtime-call-id="${result.held.runtime_call_id}"]`);
      await started.waitFor({ state: "attached" });
      const run = await started.getAttribute("data-run-id");
      const progressPanel = panel(page, run);
      result.initial_progress = await progress(page, run);
      await shot(progressPanel, "01-dispatched");
      await expand(progressPanel.locator(`.group-task:has([data-runtime-call-id="${result.held.runtime_call_id}"])`));
      const reviewer = progressPanel.locator('.group-task:has([data-event="waiting"])');
      if (await reviewer.count() === 1) await expand(reviewer);
      result.waiting = await progress(page, run);
      await shot(progressPanel, "02-waiting");
      save();
    }
    const previewResponse = post(page, groups + "/" + id + "/changes");
    await page.locator("#chatGroupChangeDates").click();
    await page.locator("#chatGroupDateRequest").fill(fixture.request);
    await page.locator("#chatGroupDatePreview").click();
    const preview = await previewResponse;
    result.preview_request = preview.request().postDataJSON();
    result.preview_status = preview.status();
    result.preview_response = await preview.json();
    save();
    assert.equal(preview.status(), 200);
    const proposal = result.preview_response.data;
    result.preview_text = await page.locator("#chatGroupDateFeedback").textContent();
    const kind = fixture.old_plan_ids ? "plan_change" : "date_change";
    assert.equal(proposal.kind, kind);
    if (fixture.old_plan_ids) {
      for (const key of ["old_plan_ids", "new_plan_ids", "remove_plan_id", "add_plan_id"]) {
        assert.deepEqual(proposal[key], fixture[key]);
      }
      const plans = ids => ids.map(id => "#" + id).join("、");
      assert.equal(result.preview_text, "替换前 · " + plans(fixture.old_plan_ids) +
        "\n替换后 · " + plans(fixture.new_plan_ids));
    } else {
      assert.deepEqual(proposal.old_dates, fixture.old_dates);
      assert.deepEqual(proposal.new_dates, fixture.new_dates);
      for (const window of [...Object.values(fixture.old_dates), ...Object.values(fixture.new_dates)]) {
        assert.ok(result.preview_text.includes(window.start_date));
        assert.ok(result.preview_text.includes(window.end_date));
      }
    }
    const confirmation = post(page, stream);
    if (fixture.capture_wait) await shot(page.locator("#chatGroupDateFeedback"), "03-preview");
    await page.locator("#chatGroupDateConfirm").click();
    const replacement = await confirmation;
    result.confirmation_request = replacement.request().postDataJSON();
    assert.deepEqual(result.confirmation_request, { conversation_id: id, change: {
      run_id: proposal.run_id, proposal_id: proposal.proposal_id,
    } });
    result.new_sse = await sse(page, 1);
    save();
    const terminal = result.new_sse.at(-1);
    assert.equal(terminal.type, "done");
    assert.notEqual(terminal.run_id, proposal.run_id);
    await page.waitForFunction(() => !document.querySelector("#chatSend").disabled, null, { timeout: 3000 });
    result.basis_before_release = await displayedEvidence(page, id, terminal.run_id);
    result.old_progress_before_release = await progress(page, proposal.run_id);
    assert.match(result.old_progress_before_release.summary, /已被替换/);
    if (fixture.capture_wait) {
      await progress(page, terminal.run_id);
      const newPanel = panel(page, terminal.run_id);
      await expand(newPanel.locator('.group-task:has([data-event="review_inputs"])'));
      await shot(newPanel, "04-handoff");
      await shot(page.locator(`#chatMessages > [data-run-id="${terminal.run_id}"].assistant`).last(), "04-answer");
      save();
    }
    assert.equal((await page.request.post(base + "/__task28/release")).status(), 200);
    result.old_sse = await sse(page, 0);
    assert.equal(result.old_sse.at(-1).type, "superseded");

    const history = await page.request.get(base + groups + "/" + id + "/history", {
      headers: await auth(page),
    });
    result.history = (await history.json()).data;
    const change = page.locator(`#chatMessages [data-event="${kind}"]`);
    const answer = page.locator(`#chatMessages > [data-run-id="${terminal.run_id}"].assistant`).last();
    await change.waitFor();
    const before = (await answer.textContent()).split("查看依据")[0];
    const changeBefore = await change.textContent();
    if (pause) {
      await pause.approve(page, pauseState, result, terminal, proposal.run_id, before, save);
      save();
    }
    await page.reload();
    await change.waitFor();
    result.refreshed = { change: await change.textContent(), old_run_id: await change.getAttribute("data-run-id"),
      new_run_id: await change.getAttribute("data-new-run-id"), answer: (await answer.textContent()).split("查看依据")[0] };
    assert.equal(result.refreshed.answer, before);
    assert.equal(result.refreshed.change, changeBefore);
    assert.match(result.refreshed.change, /已替换旧轮/);
    assert.equal(result.refreshed.old_run_id, proposal.run_id);
    assert.equal(result.refreshed.new_run_id, terminal.run_id);
    result.refreshed.old_progress = await progress(page, proposal.run_id);
    result.refreshed.new_progress = await progress(page, terminal.run_id);
    verifyProgress(result.refreshed.old_progress, result.history, proposal.run_id);
    verifyProgress(result.refreshed.new_progress, result.history, terminal.run_id);
    assert.match(result.refreshed.old_progress.summary, /已被替换/);
    const oldReclaims = result.refreshed.old_progress.entries.filter(entry => entry.kind === "reclaimed");
    assert.ok(oldReclaims.length >= 1);
    assert.ok(result.refreshed.old_progress.tasks.some(task => task.state === "inactive"));
    assert.ok(result.refreshed.new_progress.tasks.every(task => task.state === "succeeded"));
    const oldTaskText = result.refreshed.old_progress.tasks.map(task => task.text).join("\n");
    const newTaskText = result.refreshed.new_progress.tasks.map(task => task.text).join("\n");
    if (fixture.new_plan_ids) {
      assert.match(oldTaskText, /#102/);
      assert.doesNotMatch(newTaskText, /#102/);
      assert.match(newTaskText, /#103/);
    } else {
      assert.match(oldTaskText, /17–23 日/);
      assert.doesNotMatch(newTaskText, /17–23 日/);
      assert.match(newTaskText, /21–23 日/);
    }
    assert.ok(result.refreshed.new_progress.entries.some(entry => entry.kind === "dispatched"));
    const scope = result.refreshed.new_progress.scope.join("");
    if (fixture.new_plan_ids) assert.equal(scope, "确认计划 #101、#103");
    else for (const window of Object.values(fixture.new_dates)) {
      assert.ok(scope.includes(window.start_date + " 至 " + window.end_date));
    }
    result.refreshed.basis = await displayedEvidence(page, id, terminal.run_id);
    assert.deepEqual(result.refreshed.basis, result.basis_before_release);
    result.refreshed.entry_ids = await uniqueEntries(page);
    const oldEvidence = await page.request.get(evidencePath(id, proposal.run_id), { headers: await auth(page) });
    assert.equal(oldEvidence.status(), 404);
    result.refreshed.old_evidence_status = oldEvidence.status();
    if (pause) await pause.refresh(page, pauseState, result.refreshed.basis);
    await progress(page, proposal.run_id);
    const oldPanel = panel(page, proposal.run_id);
    await expand(oldPanel.locator('.group-task:has([data-event="tool_started"])').first());
    await oldPanel.screenshot({ path: path.join(path.dirname(output), "replaced-progress.png") });
    await progress(page, terminal.run_id);
    await panel(page, terminal.run_id).screenshot({ path: path.join(path.dirname(output), "replacement-tasks.png") });
    await displayedEvidence(page, id, terminal.run_id);
    await page.locator("#workbenchDetail .group-evidence").screenshot({
      path: path.join(path.dirname(output), "refreshed-basis.png"),
    });
  } catch (error) {
    if (fixture.capture_wait && page) {
      await page.screenshot({ path: path.join(path.dirname(output), "failure.png"), timeout: 5000 }).catch(() => {});
    }
    throw error;
  } finally {
    save();
    if (pauseState) pauseState.close();
    await browser.close();
  }
}

run().catch(error => { console.error(error); process.exitCode = 1; });
