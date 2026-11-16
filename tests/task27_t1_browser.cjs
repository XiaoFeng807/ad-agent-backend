const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { setTimeout: delay } = require("node:timers/promises");
const { chromium } = require("playwright");
const { browserOptions } = require("./browser_options.cjs");
const { submitGroupCreation } = require("./chat_group_create.cjs");
const { progressButton } = require("./task30_checks.cjs");

const [base, directory] = process.argv.slice(2);
const groups = "/api/chat/conversations";
const stream = "/api/chat/stream";
const control = "/__task27_t1";
const cases = [
  ["seed", "T1_SEED_OTHER_GROUP"], ["first", "T1_COMPARE"], ["second", "T1_COMPARE"],
  ["foreign", "T1_CROSS_GROUP"], ["retry", "T1_RETRY"], ["missing", "T1_MISSING"],
];

async function run() {
  const evidence = { groups: {}, cases: {}, replay: {} };
  const browser = await chromium.launch(browserOptions());
  const page = await browser.newPage();
  evidence.chromium = browser.version();
  const picker = page.locator("#chatGroupList");
  const bubbles = page.locator("#chatMessages .msg");
  await page.addInitScript(() => {
    const fetchPage = window.fetch.bind(window);
    window.task27T1Streams = [];
    window.fetch = async (url, options) => {
      const response = await fetchPage(url, options);
      if (url === "/api/chat/stream") {
        window.task27T1Streams.push(response.clone().text());
      }
      return response;
    };
  });

  async function post(endpoint) {
    const response = await page.request.post(base + endpoint);
    assert.equal(response.status(), 200);
  }

  async function create(title) {
    const [response] = await Promise.all([
      page.waitForResponse(item => new URL(item.url()).pathname === groups && item.request().method() === "POST"),
      submitGroupCreation(page, title),
    ]);
    assert.equal(response.status(), 201);
    const id = (await response.json()).data.conversation_id;
    await page.waitForFunction(value => document.querySelector("#chatGroupList [aria-current]")?.dataset.conversationId === value, id);
    return id;
  }

  async function history(id) {
    const token = await page.evaluate(() => JSON.parse(sessionStorage.getItem("ad.login")).token);
    const response = await page.request.get(base + groups + "/" + id + "/history", {
      headers: { Authorization: "Bearer " + token },
    });
    assert.equal(response.status(), 200);
    const result = (await response.json()).data;
    assert.equal(result.conversation.conversation_id, id);
    return result;
  }

  async function domMatches(snapshot) {
    const ids = snapshot.entries.filter(row => row.kind !== "worker").map(row => row.entry_id);
    await page.waitForFunction(expected => {
      const actual = [...document.querySelectorAll("#chatMessages .msg")].map(row => row.dataset.entryId);
      return JSON.stringify(actual) === JSON.stringify(expected);
    }, ids);
    const collect = locator => locator.evaluateAll(rows => rows.map(row => ({
      entry_id: row.dataset.entryId, run_id: row.dataset.runId, event: row.dataset.event ?? null,
      text: (row.querySelector(".workbench-reply") || row).textContent,
    })));
    const rows = await collect(bubbles);
    for (const run of new Set(snapshot.entries.filter(row => row.kind === "worker").map(row => row.run_id))) {
      await progressButton(page, run).click();
      for (const entry of snapshot.entries.filter(row => row.kind === "worker" && row.run_id === run)) {
        await page.locator(`#workbenchDetail [data-entry-id="${entry.entry_id}"]`).waitFor({ state: "attached" });
      }
      rows.push(...await collect(page.locator("#workbenchDetail [data-entry-id].msg")));
    }
    const byId = new Map(rows.map(row => [row.entry_id, row]));
    const actual = snapshot.entries.map(row => byId.get(row.entry_id));
    assert.deepEqual(actual.map(({ text, ...row }) => row), snapshot.entries.map(row => ({
      entry_id: row.entry_id, run_id: row.run_id, event: row.event?.kind ?? null,
    })));
    for (const row of snapshot.entries.filter(row => row.kind === "worker")) {
      assert.ok(actual.find(item => item.entry_id === row.entry_id).text.length > 0);
    }
    return actual;
  }

  try {
    await page.goto(base);
    await page.locator("#loginUser").fill("task27-owner");
    await page.locator("#loginPass").fill(process.env.AD_TASK27_FIXTURE_PASSWORD);
    await page.waitForFunction(() => Boolean(window._captchaId));
    await page.locator("#loginCaptcha").fill("0726");
    await page.locator("#loginPass").press("Enter");
    await page.locator('[data-page="chat"]').click();
    await picker.waitFor();
    evidence.groups.other = await create("27-T1 来源群");

    for (const [label, message] of cases) {
      if (label === "first") {
        evidence.groups.target = await create("27-T1 验证群");
        assert.notEqual(evidence.groups.target, evidence.groups.other);
      }
      const id = label === "seed" ? evidence.groups.other : evidence.groups.target;
      const sample = evidence.cases[label] = {};
      await post(control + "/case/" + label);
      const index = await page.evaluate(() => window.task27T1Streams.length);
      const pending = page.waitForResponse(response => new URL(response.url()).pathname === stream
        && response.request().method() === "POST");
      await page.locator("#chatInput").fill(message);
      await page.locator("#chatSend").click();
      const response = await pending;

      if (label === "first" || label === "second") {
        try {
          const deadline = Date.now() + 12000;
          do {
            sample.barrier = await (await page.request.get(base + control + "/barrier")).json();
            if (sample.barrier.waiting) break;
            await delay(50);
          } while (Date.now() < deadline);
          sample.beforeRelease = await history(id);
          assert.equal(sample.barrier.waiting, true);
          assert.deepEqual(sample.barrier.workers, ["metrics-A"]);
          assert.deepEqual(sample.barrier.reads.map(row => [row.tool, row.arguments.plan_id]),
            [["get_plan_metrics_by_date", 101]]);
          assert.ok(sample.barrier.reads.every(row => !Object.hasOwn(row, "result")));
          const active = sample.beforeRelease.runs.filter(row => row.status === "running");
          assert.equal(active.length, 1);
          const events = sample.beforeRelease.entries.filter(row => row.run_id === active[0].run_id && row.event);
          assert.ok(events.some(row => row.event.kind === "dispatched"));
          assert.ok(events.some(row => row.event.kind === "tool_started"));
          assert.ok(events.every(row => !["receipt", "succeeded", "reclaimed"].includes(row.event.kind)));
          sample.beforeReleaseDom = await domMatches(sample.beforeRelease);
        } finally {
          await post(control + "/release");
        }
      }

      sample.request = response.request().postDataJSON();
      sample.httpStatus = response.status();
      assert.equal(sample.httpStatus, 200);
      assert.equal(sample.request.conversation_id, id);
      assert.equal(sample.request.message, message);
      await page.waitForFunction(i => window.task27T1Streams.length > i, index);
      sample.sseText = await page.evaluate(i => window.task27T1Streams[i], index);
      sample.sse = sample.sseText.split(/\r?\n/).filter(line => line.startsWith("data: "))
        .map(line => JSON.parse(line.slice(6)));
      assert.deepEqual(sample.sse.map(row => row.type), ["thinking", "text", "done"]);
      assert.deepEqual([...new Set(sample.sse.map(row => row.conversation_id))], [id]);
      assert.equal(new Set(sample.sse.map(row => row.run_id)).size, 1);
      assert.equal(new Set(sample.sse.map(row => row.trace_id)).size, 1);
      sample.history = await history(id);
      sample.dom = await domMatches(sample.history);
      assert.ok(sample.dom.some(row => row.run_id === sample.sse[0].run_id && row.event === "receipt"));
      if (label === "retry" || label === "missing") {
        assert.ok(sample.dom.some(row => row.run_id === sample.sse[0].run_id && row.event === "failed"));
      }
    }

    await page.reload();
    await page.waitForFunction(id => document.querySelector("#chatGroupList [aria-current]")?.dataset.conversationId === id, evidence.groups.target);
    for (const [key, last] of [["target", "missing"], ["other", "seed"]]) {
      const id = evidence.groups[key];
      await picker.locator('[data-conversation-id="' + id + '"]').click();
      const snapshot = await history(id);
      const dom = await domMatches(snapshot);
      evidence.replay[key] = { snapshot, dom };
      assert.deepEqual(snapshot.entries, evidence.cases[last].history.entries);
      assert.deepEqual(dom, evidence.cases[last].dom);
      assert.equal(await picker.locator("[aria-current]").getAttribute("data-conversation-id"), id);
    }
  } finally {
    fs.writeFileSync(path.join(directory, "browser.json"), JSON.stringify(evidence, null, 2));
    await browser.close();
  }
}

run().catch(error => { console.error(error); process.exitCode = 1; });
