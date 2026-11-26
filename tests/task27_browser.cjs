const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");
const { browserOptions } = require("./browser_options.cjs");
const { submitGroupCreation } = require("./chat_group_create.cjs");
const { progressButton } = require("./task30_checks.cjs");

const [base, directory] = process.argv.slice(2);
const route = "/api/chat/conversations";
const stream = "/api/chat/stream";
const attack = `<img src="data:image/png,invalid" onerror="this.dataset.securityProbe='executed'">`;

async function run() {
  const browser = await chromium.launch(browserOptions());
  const page = await browser.newPage();
  await page.addInitScript(() => {
    const original = window.fetch.bind(window);
    window.task27Streams = {};
    window.fetch = async (url, options) => {
      const response = await original(url, options);
      if (url === "/api/chat/stream" && response.ok) {
        window.task27Streams[JSON.parse(options.body).message] = response.clone().text();
      }
      return response;
    };
  });
  const result = { chromium: browser.version(), ids: {}, sse: {}, dom: {}, checks: [], access: [] };
  const picker = page.locator("#chatGroupList");
  const bubbles = page.locator("#chatMessages .msg");
  const progress = kind => page.locator('#workbenchDetail [data-event'+(kind ? '="'+kind+'"' : '')+']');
  async function openProgress(kind){
    const card = progressButton(page).last();
    if (await card.getAttribute("aria-expanded") !== "true") await card.click();
    const event = progress(kind).first();
    await event.waitFor({ state: "attached" });
    const panel = event.locator("xpath=ancestor::details[1]");
    if (await panel.getAttribute("open") === null) await panel.locator(":scope > summary").click();
    await event.waitFor();
  }
  const archive = page.locator("#chatGroupArchive");
  const chat = page.locator('[data-page="chat"]');

  async function auth() {
    const token = await page.evaluate(() => JSON.parse(sessionStorage.getItem("ad.login")).token);
    return { Authorization: "Bearer " + token };
  }

  async function login(name) {
    await page.locator("#loginUser").fill(name);
    await page.locator("#loginPass").fill(process.env.AD_TASK27_FIXTURE_PASSWORD);
    await page.waitForFunction(() => Boolean(window._captchaId));
    await page.locator("#loginCaptcha").fill("0726");
    await page.locator("#loginPass").press("Enter");
    await chat.waitFor();
    await chat.click();
    await picker.waitFor();
  }

  async function create(label) {
    const [response] = await Promise.all([
      page.waitForResponse(item => new URL(item.url()).pathname === route && item.request().method() === "POST"),
      submitGroupCreation(page, "TASK27 " + label),
    ]);
    assert.equal(response.status(), 201);
    const id = (await response.json()).data.conversation_id;
    await page.waitForFunction(value => document.querySelector("#chatGroupList [aria-current]")?.dataset.conversationId === value, id);
    result.ids[label] = id;
    return id;
  }

  async function select(id) {
    await picker.locator('[data-conversation-id="' + id + '"]').click();
    assert.equal(await picker.locator("[aria-current]").getAttribute("data-conversation-id"), id);
  }

  async function send(message) {
    const wait = page.waitForResponse(response => new URL(response.url()).pathname === stream &&
      response.request().postDataJSON().message === message);
    await page.locator("#chatInput").fill(message);
    await page.locator("#chatSend").click();
    return wait;
  }

  async function events(response) {
    assert.equal(response.status(), 200);
    const message = response.request().postDataJSON().message;
    await page.waitForFunction(key => Boolean(window.task27Streams[key]), message);
    const text = await page.evaluate(key => window.task27Streams[key], message);
    const rows = text.split(/\r?\n/).filter(line => line.startsWith("data: "))
      .map(line => JSON.parse(line.slice(6)));
    assert.deepEqual(rows.map(row => row.type), ["thinking", "text", "done"]);
    assert.equal(new Set(rows.map(row => row.run_id)).size, 1);
    assert.equal(new Set(rows.map(row => row.trace_id)).size, 1);
    return rows;
  }

  async function safe() {
    assert.equal(await page.locator("#chatMessages, #workbenchDetail").locator("img, [onerror], [data-security-probe], a[href^='javascript:']").count(), 0);
  }

  async function history(id) {
    const response = await page.request.get(base + route + "/" + id + "/history", { headers: await auth() });
    assert.equal(response.status(), 200);
    return (await response.json()).data;
  }

  async function ids() {
    return bubbles.evaluateAll(elements => elements.map(element => element.dataset.entryId));
  }

  try {
    await page.goto(base);
    await login("task27-owner");
    await select("legacy-17");
    await bubbles.getByText("LEGACY_QUESTION", { exact: true }).waitFor();
    await bubbles.locator("strong").getByText("正常回复", { exact: true }).waitFor();
    assert.deepEqual(await ids(), ["legacy:41", "legacy:42"]);
    await safe();
    result.checks.push("legacy stable IDs and malicious history");

    const a = await create("A");
    await events(await send("A_SEED"));
    await bubbles.getByText("A_SEED_ANSWER", { exact: true }).waitFor();
    assert.equal(await progress().count(), 0);
    const b = await create("B");
    assert.notEqual(a, b);
    await events(await send("B_SEED"));
    await bubbles.getByText("B_SEED_ANSWER", { exact: true }).waitFor();
    assert.equal(await progress().count(), 0);
    result.checks.push("distinct group IDs and zero Worker for simple replies");

    await select(a);
    const responseA = await send("A_WORK");
    await page.waitForFunction(async () => (await (await fetch("/__task27/state")).json()).a_waiting);
    await openProgress("dispatched");
    assert.match(await progress("dispatched").textContent(), /<img src=/);
    await safe();
    const headers = await auth();
    for (const [method, url, data] of [
      ["PATCH", route + "/" + a + "/archive", { archived: true }],
      ["POST", stream, { message: "REJECT_WHILE_RUNNING", conversation_id: a }],
    ]) {
      const response = await page.request.fetch(base + url, { method, headers, data });
      assert.equal(response.status(), 409);
    }
    result.checks.push("running group rejects archive and another turn");

    await select(b);
    result.sse.B = await events(await send("B_WORK"));
    await bubbles.getByText("B_WORK 完成", { exact: true }).waitFor();
    result.beforeRelease = { A: await history(a), B: await history(b) };
    assert.equal(result.beforeRelease.A.runs.find(run => run.status === "running").status, "running");
    assert.equal(result.beforeRelease.B.runs.find(run => run.run_id === result.sse.B[0].run_id).status, "completed");
    const beforeRelease = await ids();
    assert.equal((await page.request.post(base + "/__task27/release")).status(), 200);
    result.sse.A = await events(responseA);
    assert.equal(await picker.locator("[aria-current]").getAttribute("data-conversation-id"), b);
    assert.deepEqual(await ids(), beforeRelease);
    assert.equal(await bubbles.getByText(/A_WORK/).count(), 0);
    result.dom.B = await ids();

    await select(a);
    await openProgress("reclaimed");
    await bubbles.locator("strong").getByText("正常回复", { exact: true }).waitFor();
    assert.equal(await bubbles.getByText(/B_WORK/).count(), 0);
    assert.ok((await bubbles.allTextContents()).some(text => text.includes(attack)));
    await safe();
    assert.ok(result.sse.A.find(row => row.type === "text").content.includes(attack));
    result.checks.push("B commits first and late A events only render in A");
    result.checks.push("AD-038 malicious assistant and Worker text with normal Markdown");

    await page.reload();
    await page.waitForFunction(value => document.querySelector("#chatGroupList [aria-current]")?.dataset.conversationId === value, a);
    await openProgress("receipt");
    await safe();
    assert.equal(new Set(await ids()).size, (await ids()).length);
    await archive.click();
    await page.waitForFunction(() => document.querySelector("#chatGroupStatus").textContent === "已归档");
    assert.equal(await page.locator("#chatSend").isDisabled(), true);
    const denied = await page.request.post(base + stream, {
      headers: await auth(), data: { message: "REJECT_ARCHIVED", conversation_id: a },
    });
    assert.equal(denied.status(), 409);
    await select(b);
    await select(a);
    await openProgress("receipt");
    await archive.click();
    await page.waitForFunction(() => document.querySelector("#chatGroupArchive").textContent === "归档会话");
    await events(await send("A_RESUME"));
    await bubbles.getByText("A_RESUME_ANSWER", { exact: true }).waitFor();
    await events(await send("A_FAIL"));
    await openProgress("failed");
    await bubbles.getByText("A_FAIL 完成", { exact: true }).waitFor();
    result.dom.A = await ids();
    result.checks.push("refresh, archive, restore, resume and actual Worker failure");

    await page.evaluate(() => doLogout());
    await login("task27-owner");
    await page.waitForFunction(value => document.querySelector("#chatGroupList [aria-current]")?.dataset.conversationId === value, a);
    await bubbles.getByText("A_RESUME_ANSWER", { exact: true }).waitFor();
    assert.deepEqual(await ids(), result.dom.A);
    await page.evaluate(() => doLogout());
    await login("task27-other");
    assert.equal(await picker.locator("button").count(), 0);
    for (const [method, endpoint, body] of [
      ["GET", id => route + "/" + id + "/history"],
      ["PATCH", id => route + "/" + id + "/archive", () => ({ archived: true })],
      ["POST", () => stream, id => ({ message: "DENIED", conversation_id: id })],
    ]) {
      const outcomes = [];
      for (const id of [a, b, "f".repeat(32)]) {
        const response = await page.request.fetch(base + endpoint(id), {
          method, headers: await auth(), data: body?.(id),
        });
        assert.equal(response.status(), 404);
        outcomes.push(await response.json());
      }
      assert.deepEqual(outcomes, Array(3).fill({ detail: "工作群不存在" }));
      result.access.push({ method, responses: outcomes });
    }
    result.checks.push("relogin recovers owner state and other account gets uniform 404");

    await page.evaluate(() => doLogout());
    await login("task27-owner");
    const legacyResponse = await page.request.get(base + "/api/chat/history", { headers: await auth() });
    assert.equal(legacyResponse.status(), 200);
    const legacy = (await legacyResponse.json()).data;
    assert.deepEqual(legacy.map(row => row.content), ["LEGACY_QUESTION", result.sse.A[1].content.slice("A_WORK ".length)]);
    const legacyStream = await page.request.post(base + stream, {
      headers: await auth(), data: { message: "LEGACY_CONTINUE", chat_view_id: "task27-legacy" },
    });
    assert.equal(legacyStream.status(), 200);
    assert.match(await legacyStream.text(), /LEGACY_CONTINUE_ANSWER/);
    result.checks.push("legacy GET and POST access only the legacy store");
  } finally {
    fs.writeFileSync(path.join(directory, "browser.json"), JSON.stringify(result, null, 2));
    await browser.close();
  }
}

run().catch(error => { console.error(error); process.exitCode = 1; });
