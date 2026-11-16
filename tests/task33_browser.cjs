const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium, expect } = require("playwright/test");
const { browserOptions } = require("./browser_options.cjs");
const { submitGroupCreation } = require("./chat_group_create.cjs");
const { auth, displayedEvidence, evidencePath, openProgress, panel, progress } = require("./task30_checks.cjs");

const [base, input, output] = process.argv.slice(2);
const fixture = JSON.parse(fs.readFileSync(input, "utf8"));
const result = {};
const groups = "/api/chat/conversations";
const stream = "/api/chat/stream";
const terminal = new Set(["done", "stopped", "superseded", "error"]);
const groupPath = id => groups + "/" + id;

async function run() {
  const browser = await chromium.launch(browserOptions());
  result.chromium = browser.version();
  const page = await browser.newPage({ baseURL: base, viewport: { width: 1280, height: 980 }, timezoneId: "Asia/Shanghai" });
  const stopButton = page.locator(".chat-input-area #chatGroupStop");
  page.setDefaultTimeout(20000);
  try {
    await page.addInitScript(() => {
      const fetch = window.fetch.bind(window);
      window.task33Streams = [];
      window.fetch = async (url, options) => {
        const response = await fetch(url, options);
        if (url === "/api/chat/stream") {
          const record = { request: JSON.parse(options.body), status: response.status };
          window.task33Streams.push(record);
          response.clone().text().then(text => {
            record.events = text.split(/\r?\n/).filter(line => line.startsWith("data: "))
              .map(line => JSON.parse(line.slice(6)));
          }).catch(error => { record.error = error.message; });
        }
        return response;
      };
    });
    await page.goto(base);
    await page.locator("#loginUser").fill("task27-owner");
    await page.locator("#loginPass").fill(process.env.AD_TASK33_PASSWORD);
    await page.waitForFunction(() => Boolean(window._captchaId));
    await page.locator("#loginCaptcha").fill("0726");
    await page.locator("#loginPass").press("Enter");
    await page.locator('[data-page="chat"]').click();
    const headers = await auth(page);

    const request = async (method, url, data, requestHeaders = headers) => {
      const response = await page.request.fetch(url, { method, headers: requestHeaders, data, timeout: 30000 });
      return { status: response.status(), body: await response.json() };
    };
    const stage = async (name, method = "GET") => {
      assert.equal((await request(method, "/__task28/stage/" + name)).status, 200);
    };
    const release = name => stage(name, "POST");
    const history = async id => {
      const response = await request("GET", groupPath(id) + "/history");
      assert.equal(response.status, 200);
      return response.body.data;
    };
    const postStop = (group, run, credentials = headers) =>
      request("POST", groupPath(group) + "/stop", { run_id: run }, credentials);
    const create = async title => {
      const created = page.waitForResponse(res => new URL(res.url()).pathname === groups && res.request().method() === "POST");
      await submitGroupCreation(page, title);
      const response = await created;
      assert.equal(response.status(), 201);
      await expect(page.locator("#chatInput")).toBeEnabled();
      return (await response.json()).data.conversation_id;
    };
    const send = async message => {
      await page.locator("#chatInput").fill(message);
      await page.locator("#chatSend").click();
    };
    const running = async id => {
      const run = (await history(id)).runs.find(run => run.status === "running");
      assert.ok(run);
      return run.run_id;
    };
    const uiStop = async (id, run) => {
      const clicked = page.waitForResponse(res => new URL(res.url()).pathname === groupPath(id) + "/stop");
      page.once("dialog", async dialog => {
        result.confirmation = dialog.message();
        await dialog.accept();
      });
      await expect(stopButton).toHaveAccessibleName("停止本轮");
      await stopButton.click();
      const response = await clicked;
      assert.match(result.confirmation, new RegExp(run));
      assert.deepEqual(response.request().postDataJSON(), { run_id: run });
      return { status: response.status(), body: await response.json() };
    };
    const finished = async (run, type) => {
      await page.waitForFunction(run => task33Streams.some(row => row.events?.some(event => event.run_id === run)), run);
      const record = await page.evaluate(run => task33Streams.find(row => row.events?.some(event => event.run_id === run)), run);
      assert.equal(record.status, 200);
      assert.deepEqual(record.events.filter(event => terminal.has(event.type)).map(event => event.type), [type]);
      assert.equal(record.events.at(-1).type, type);
      assert.equal(record.events.filter(event => event.type === "text").length, type === "done" ? 1 : 0);
      result.streams ??= {};
      result.streams[run] = record;
    };
    const stopped = async run => {
      await openProgress(page, run);
      await expect(panel(page, run).locator(":scope > summary")).toHaveText("协作进度 · 已停止");
      await expect(page.locator("#chatInput")).toBeEnabled();
      await expect(stopButton).toBeHidden();
    };
    const finishedDirect = async run => {
      await finished(run, "done");
      assert.equal(result.streams[run].events.find(event => event.type === "text").content, "#101 当前状态 active。");
    };
    const shot = name => page.screenshot({ path: path.join(path.dirname(output), name + ".png") });

    if (fixture.mode === "late") {
      // Start a second group first and hold its save through the stop in the first group.
      result.sideGroup = await create("TASK-033 · 并行工作群");
      await send(fixture.direct);
      await stage("side-save");
      result.sideRun = await running(result.sideGroup);
    }
    const id = result.group = await create("TASK-033 · " + fixture.mode);
    await send(fixture.message);
    const wait = fixture.mode === "late" ? "metric" : fixture.mode === "pre-register" ? "register" : "save";
    await stage(wait);
    const run = result.run = await running(id);
    if (fixture.mode === "late") {
      result.before = await history(id);
      const pausePath = "/api/diagnosis/pause-actions/" + fixture.pauseReceipt;
      result.pauseBefore = await request("GET", pausePath);
      assert.equal(result.pauseBefore.status, 200);
      await expect(stopButton).toBeVisible();
      await openProgress(page, run);
      await expect(panel(page, run).locator('[data-event="receipt"]')).toHaveCount(1);
      await expect(panel(page, run).locator('[data-event="tool_started"]')).toHaveCount(2);
      result.progressBefore = await progress(page, run);
      assert.equal(result.progressBefore.entries.filter(row => row.kind === "waiting").length, 1);
      await shot("01-before");
      const captcha = await request("GET", "/api/captcha");
      const login = await request("POST", "/api/login", { username: "task27-other",
        password: process.env.AD_TASK33_PASSWORD, captcha_id: captcha.body.captcha_id, captcha_text: "0726" });
      assert.equal(login.status, 200);
      result.rejected = await Promise.all([
        postStop(id, run, { Authorization: "Bearer " + login.body.data.token }),
        postStop(result.sideGroup, run), postStop(id, "a".repeat(32)),
      ]);
      assert.deepEqual(result.rejected.map(row => row.status), [404, 404, 404]);
      assert.equal(await running(id), run);
      await page.route(groupPath(id) + "/stop", async route => {
        const responses = await Promise.all([route.fetch(), route.fetch()]);
        result.stops = await Promise.all(responses.map(async res => ({ status: res.status(), body: await res.json() })));
        await route.fulfill({ response: responses[0] });
      }, { times: 1 });
    }
    const stopping = uiStop(id, run);
    if (fixture.mode === "answer-first") {
      await stage("stop");
      await release("save");
      await finished(run, "done");
      await release("stop");
    }
    result.stop = await stopping;
    if (fixture.mode === "answer-first") {
      assert.equal(result.stop.status, 409);
      assert.match(result.stop.body.detail, /答案已提交/);
      result.after = await history(id);
      assert.equal(result.after.runs.find(row => row.run_id === run).status, "completed");
      result.basis = await displayedEvidence(page, id, run);
      await shot("answer-first");
      return;
    }

    assert.equal(result.stop.status, 200);
    await stopped(run);
    result.after = await history(id);
    assert.deepEqual(result.after.runs.find(row => row.run_id === run).stop, result.stop.body.data);
    if (fixture.mode === "late") {
      assert.deepEqual(result.stops, [result.stop, result.stop]);
      const previouslyCommitted = result.before.entries.filter(row => row.run_id === run);
      assert.deepEqual(result.after.entries.filter(row => row.run_id === run), previouslyCommitted);
      await shot("02-stopped");
      assert.equal(await running(result.sideGroup), result.sideRun);
      await release("side-save");
      await finishedDirect(result.sideRun);

      await send(fixture.direct);
      await stage("new-save");
      result.newRun = await running(id);
      result.replay = await postStop(id, run);
      assert.deepEqual(result.replay, result.stop);
      assert.equal(await running(id), result.newRun);
      await release("new-save");
      await finishedDirect(result.newRun);
      assert.ok(await page.evaluate(group => task33Streams.some(row =>
        row.request.conversation_id === group && !row.events && !row.error), id));
    }
    await release(wait);
    await finished(run, "stopped");
    result.final = await history(id);
    assert.equal((await request("GET", evidencePath(id, run))).status, 404);
    assert.ok(!result.final.entries.some(row => row.run_id === run && row.kind === "assistant"));
    if (fixture.mode === "late") {
      await page.reload();
      await page.locator('[data-page="chat"]').click();
      await stopped(run);
      result.refreshed = await history(id);
      assert.deepEqual(result.refreshed, result.final);
      const record = panel(page, run).locator(".group-stop-record");
      const metadata = result.stop.body.data;
      await expect(record).toContainText(metadata.stopped_by_name);
      await expect(record.locator("time")).toHaveAttribute("datetime", metadata.stopped_at);
      await expect(record.locator("code")).toHaveText(run);
      const persistedProgress = result.refreshed.entries.filter(row => row.run_id === run && row.kind === "worker");
      await expect(panel(page, run).locator("[data-entry-id]")).toHaveCount(persistedProgress.length);
      result.progressRefresh = await progress(page, run);
      assert.deepEqual(result.progressRefresh.entries.filter(row => row.kind !== "reclaimed"), result.progressBefore.entries);
      await shot("03-refresh");
      result.pauseReceipt = await request("GET", "/api/diagnosis/pause-actions/" + fixture.pauseReceipt);
      assert.deepEqual(result.pauseReceipt, result.pauseBefore);
    }
  } finally {
    fs.writeFileSync(output, JSON.stringify(result, null, 2));
    await browser.close();
  }
}

run().catch(error => { console.error(error); process.exitCode = 1; });
