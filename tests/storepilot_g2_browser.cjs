const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const { DatabaseSync } = require("node:sqlite");
const { setTimeout: sleep } = require("node:timers/promises");
const { chromium, expect } = require("playwright/test");
const { submitGroupCreation } = require("./chat_group_create.cjs");

const folder = process.argv[2];
const file = name => path.join(folder, name);
const json = async name => JSON.parse(await fs.readFile(file(name), "utf8"));
const exists = name => fs.access(file(name)).then(() => true, () => false);
const signal = name => fs.writeFile(file(name), "");
const groups = "/api/chat/conversations";
const stream = "/api/chat/stream";
const auth = token => ({ Authorization: `Bearer ${token}` });
const report = { agents: [], samples: [], events: [], page_errors: [] };
const record = (event, fields = {}) => report.events.push({ event, epoch_ms: Date.now(), ...fields });
const until = (callback, message, timeout = 18000) => expect.poll(callback, { message, timeout }).toBeTruthy();

async function packet(response) {
  const headers = await response.allHeaders?.() ?? response.headers();
  return { status: response.status(), request_id: headers["x-request-id"], body: await response.json() };
}

async function fetchPacket(page, target, token, options = {}) {
  return page.evaluate(async ({ target, options }) => {
    const started_epoch_ms = Date.now();
    const started = performance.now();
    const response = await fetch(target, options);
    const headers_ms = performance.now() - started;
    const raw = await response.text();
    const body = raw ? JSON.parse(raw) : null;
    return { started_epoch_ms, headers_ms, ms: performance.now() - started, status: response.status,
      request_id: response.headers.get("x-request-id"), body };
  }, { target, options: { ...options, headers: { ...auth(token), ...options.headers } } });
}

async function login(page, username) {
  const { captcha_id } = await (await page.request.get("/api/captcha")).json();
  const response = await page.request.post("/api/login", { data: {
    username, password: process.env.STOREPILOT_TEST_PASSWORD, captcha_id, captcha_text: "0726",
  } });
  const body = await response.json();
  assert.equal(body.code, 200);
  return body.data.token;
}

function monitorResponse(page, method, pathname) {
  const response = page.waitForResponse(response => response.request().method() === method &&
    new URL(response.url()).pathname === pathname);
  // The fixture can fail before this response is consumed. Preserve that original error.
  response.catch(() => {});
  return response;
}

async function startAgent(page, agent, message) {
  await page.evaluate(({ agent, message, stream }) => {
    const controller = new AbortController();
    const state = { started_epoch_ms: Date.now(), frames: [] };
    window.g2Streams ??= {};
    window.g2Streams[agent.name] = { controller, state };
    const started = performance.now();
    void (async () => {
      try {
        const response = await fetch(stream, { method: "POST", signal: controller.signal,
          headers: { Authorization: `Bearer ${agent.token}`, "Content-Type": "application/json" },
          body: JSON.stringify({ conversation_id: agent.conversation_id, message, listing_operations: true }) });
        state.status = response.status;
        state.request_id = response.headers.get("x-request-id");
        state.headers_ms = performance.now() - started;
        if (!response.ok) state.body = await response.json();
        else {
          let pending = "";
          for await (const chunk of response.body.pipeThrough(new TextDecoderStream())) {
            pending += chunk;
            const frames = pending.split(/\r?\n\r?\n/);
            pending = frames.pop();
            for (const frame of frames) {
              if (frame.startsWith("data: ")) state.frames.push({ epoch_ms: Date.now(), ms: performance.now() - started,
                ...JSON.parse(frame.slice(6)) });
            }
          }
        }
      } catch (error) {
        state.error = error.name + ": " + error.message;
      } finally {
        state.ms = performance.now() - started;
        state.finished = true;
      }
    })();
  }, { agent, message, stream });
}

async function main() {
  const plan = await json("plan.json");
  const manifest = await json("../manifest.json");
  const { message, sku } = manifest.scenario;
  let browser, db;
  const pages = [];
  try {
    db = new DatabaseSync(file("run.sqlite3"), { readOnly: true });
    db.exec("PRAGMA busy_timeout = 5000");
    report.sqlite = { version: db.prepare("SELECT sqlite_version() AS version").get().version };
    const rows = sql => db.prepare(sql).all().map(row => ({ ...row }));
    const slots = () => rows(`SELECT run_id, user_id, CAST(acquired_ns AS TEXT) AS acquired_ns,
      CAST(released_ns AS TEXT) AS released_ns FROM chat_group_run_slots ORDER BY chat_group_run_slots.acquired_ns`);
    const runs = () => rows("SELECT * FROM chat_group_runs ORDER BY run_id");
    const products = () => rows("SELECT * FROM listing_test_products ORDER BY shop_id, marketplace_id, sku");
    const receipts = () => rows("SELECT * FROM listing_change_receipts ORDER BY receipt_id");
    const journal = async () => (await fs.readFile(file("transport.jsonl"), "utf8")).trim().split("\n").map(JSON.parse);
    browser = await chromium.launch({ channel: "msedge" });
    report.browser = browser.version();
    for (const origin of plan.origins) {
      const page = await browser.newPage({ baseURL: origin });
      await page.goto(origin + "/api/health");
      pages.push(page);
    }
    const tokens = [];
    const lookup = agent => pages[agent.gateway].evaluate(name => window.g2Streams?.[name]?.state, agent.name);
    const close = async agent => {
      await signal(`release-${agent.run_id}`);
      await until(() => slots().some(slot => slot.run_id === agent.run_id && slot.released_ns !== null),
        `slot released for ${agent.name}`);
      await until(async () => (await lookup(agent))?.finished, `stream finished for ${agent.name}`);
      Object.assign(agent, await lookup(agent));
    };

    async function prepare(name, account, gateway, barrier = "model") {
      const result = await fetchPacket(pages[gateway], groups, tokens[account], { method: "POST",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title: `G2 ${name}` }) });
      assert.equal(result.status, 201);
      const conversation_id = result.body.data.conversation_id;
      await fs.writeFile(file(`mode-${conversation_id}`), barrier);
      const agent = { name, account: manifest.accounts[account], gateway, origin: plan.origins[gateway],
        conversation_id, barrier };
      report.agents.push(agent);
      return () => startAgent(pages[gateway], { ...agent, token: tokens[account] }, message).then(() => agent);
    }

    async function settle(agent) {
      await until(async () => {
        const state = await lookup(agent);
        return state?.frames.some(frame => frame.type === "thinking") || state?.finished;
      }, `progress or explicit rejection for ${agent.name}`);
      Object.assign(agent, await lookup(agent));
      assert.ok(agent.request_id, agent.error);
      if (agent.status === 200) {
        const progress = agent.frames.find(frame => frame.type === "thinking");
        assert.ok(progress?.run_id && progress.trace_id, JSON.stringify(agent));
        Object.assign(agent, { run_id: progress.run_id, trace_id: progress.trace_id, progress });
        if (agent.barrier !== "fail") await until(() => exists(`held-${agent.run_id}`), `barrier for ${agent.name}`);
        assert.ok(slots().some(slot => slot.run_id === agent.run_id));
      } else {
        assert.equal(agent.status, 429, JSON.stringify(agent));
        assert.ok(!runs().some(run => run.conversation_id === agent.conversation_id));
      }
      return agent;
    }

    async function submit(...args) {
      return settle(await (await prepare(...args))());
    }

    if (plan.case === "rate" || plan.case === "lifecycle") {
      for (const account of manifest.accounts) tokens.push(await login(pages[0], account));
    }

    if (plan.case === "rate") {
      const attempt = (gateway, token, spoof) => fetchPacket(pages[gateway], stream, token, { method: "POST",
        headers: { "Content-Type": "application/json", "x-user-id": spoof },
        body: JSON.stringify({ conversation_id: "absent", message }) });
      const anonymous = await attempt(0, "invalid", "1");
      assert.equal(anonymous.status, 401);
      report.rate = await Promise.all(Array.from({ length: 20 }, (_, index) => {
        const gateway = index % 2;
        const account = index === 19 ? 1 : 0;
        return attempt(gateway, tokens[account], String(100 + index)).then(result => ({ gateway, account, ...result }));
      }));
      assert.equal(report.rate.at(-1).status, 404, JSON.stringify(report.rate));
      assert.ok(report.rate.some(row => row.account === 0 && row.status === 404));
      for (const gateway of [0, 1]) {
        const denials = report.rate.filter(row => row.gateway === gateway && row.status === 429);
        assert.ok(denials.length);
        for (const denial of denials) assert.equal(denial.body.detail, "account_rate_exceeded");
      }
      const privatePath = await fetchPacket(pages[1], "/_gateway/rate/api/chat/stream", tokens[0], { method: "POST" });
      assert.equal(privatePath.status, 404);
      assert.deepEqual(runs(), []);
      return;
    }

    if (plan.case === "lifecycle") {
      const first = await submit("alpha-tool", 0, 0, "tool");
      const second = await submit("alpha-second", 0, 1);
      const over = await submit("alpha-over", 0, 0);
      assert.equal(over.body.detail, "account_inflight_exceeded");
      const beta = await submit("beta-progress", 1, 1);
      assert.equal(beta.status, 200);
      report.full = slots().filter(slot => slot.released_ns === null);
      assert.equal(report.full.length, 3);
      await close(beta);
      const contest = await Promise.all([prepare("race-beta", 1, 0), prepare("race-gamma", 2, 1)]);
      const competitors = await Promise.all((await Promise.all(contest.map(start => start()))).map(settle));
      assert.deepEqual(competitors.map(agent => agent.status).sort(), [200, 429]);
      assert.equal(competitors.find(agent => agent.status === 429).body.detail, "global_inflight_exceeded");
      await close(competitors.find(agent => agent.status === 200));

      record("browser-abort", { request_id: first.request_id, run_id: first.run_id });
      await pages[0].evaluate(name => window.g2Streams[name].controller.abort(), first.name);
      await until(async () => (await journal()).some(row => row.event === "app-disconnected" &&
        row.request_id === first.request_id), "Envoy disconnect reached FastAPI");
      const stillFull = await submit("alpha-while-tool-drains", 0, 1);
      assert.equal(stillFull.body.detail, "account_inflight_exceeded");
      report.slots_while_draining = slots();
      assert.equal(slots().find(slot => slot.run_id === first.run_id).released_ns, null);
      record("release-tool", { request_id: first.request_id, run_id: first.run_id });
      await close(first);
      assert.match(first.error, /AbortError/);
      assert.ok(!first.frames.some(frame => frame.type === "done"));
      assert.equal(runs().find(run => run.run_id === first.run_id).status, "interrupted");
      for (const table of ["chat_group_entries WHERE kind='assistant' AND", "listing_change_proposals WHERE"]) {
        assert.deepEqual(db.prepare(`SELECT * FROM ${table} run_id=?`).all(first.run_id), []);
      }
      const reentry = await submit("alpha-reentry-error", 0, 1, "fail");
      assert.equal(reentry.status, 200);
      await close(reentry);
      assert.ok(reentry.frames.some(frame => frame.type === "error"));
      assert.equal(runs().find(run => run.run_id === reentry.run_id).status, "error");
      await close(second);
      return;
    }

    const ui = await browser.newPage({ baseURL: plan.origins[0], viewport: { width: 1360, height: 1050 },
      timezoneId: "Asia/Shanghai" });
    ui.on("pageerror", error => report.page_errors.push(error.message));
    if (plan.case === "approval") await ui.addInitScript(() => {
      const fetchOriginal = window.fetch.bind(window);
      window.fetch = (url, options) => {
        if (options?.method !== "POST" || typeof url !== "string" || !url.endsWith("/confirm")) {
          return fetchOriginal(url, options);
        }
        const headers = new Headers(options.headers);
        headers.set("x-envoy-retry-on", "5xx,reset,connect-failure");
        headers.set("x-envoy-max-retries", "2");
        headers.set("x-envoy-hedge-on-per-try-timeout", "true");
        return fetchOriginal(url, { ...options, headers });
      };
    });
    await ui.goto(plan.origins[0]);
    await ui.locator("#loginUser").fill(manifest.accounts[0]);
    await ui.locator("#loginPass").fill(process.env.STOREPILOT_TEST_PASSWORD);
    await ui.waitForFunction(() => Boolean(window._captchaId));
    await ui.locator("#loginCaptcha").fill("0726");
    const loginResponse = monitorResponse(ui, "POST", "/api/login");
    await ui.locator("#loginPass").press("Enter");
    const loginResult = await (await loginResponse).json();
    assert.equal(loginResult.code, 200);
    tokens.push(loginResult.data.token);
    for (const account of manifest.accounts.slice(1)) tokens.push(await login(pages[1], account));
    await ui.locator('[data-page="chat"]').click();
    const creation = monitorResponse(ui, "POST", groups);
    await submitGroupCreation(ui, "G2 receipt source");
    const { conversation_id: conversation } = (await (await creation).json()).data;
    const historyPath = `${groups}/${conversation}/history`;
    const seedStream = monitorResponse(ui, "POST", stream);
    await ui.locator("#chatInput").fill(message);
    await ui.locator("#chatSend").click();
    const seedResponse = await seedStream;
    assert.equal(seedResponse.status(), 200);
    report.seed_request_id = (await seedResponse.allHeaders())["x-request-id"];
    const card = ui.locator('.msg [data-result-kind="listing"]');
    await expect(card).toBeVisible();
    const seed = await fetchPacket(pages[1], historyPath, tokens[0]);
    const [entry] = seed.body.data.entries.filter(row => row.kind === "assistant");
    const [{ action_id }] = entry.listing_actions;
    const actionPath = `${groups}/${conversation}/runs/${entry.run_id}/listing-actions/${action_id}`;
    const evidencePath = `${groups}/${conversation}/runs/${entry.run_id}/evidence`;
    const confirmPath = actionPath + "/confirm";
    report.action = { conversation_id: conversation, run_id: entry.run_id, trace_id: entry.trace_id, action_id };
    const open = monitorResponse(ui, "GET", actionPath);
    await card.click();
    report.pending = await packet(await open);
    const approve = ui.getByRole("button", { name: "批准应用英文五点（本地测试）", exact: true });
    await expect(approve).toBeVisible();
    const section = ui.locator(".group-listing-action");
    const assertReceipt = (result, expected) => {
      assert.equal(result.status, 200);
      assert.equal(result.body.data.conversation_id, conversation);
      assert.equal(result.body.data.run_id, entry.run_id);
      assert.equal(result.body.data.action_id, action_id);
      assert.deepEqual(result.body.data.receipt, expected);
    };
    let known;
    if (plan.case === "mix") {
      const approved = monitorResponse(ui, "POST", confirmPath);
      await approve.click();
      report.seed_post = await packet(await approved);
      known = report.seed_post.body.data.receipt;
      assert.ok(known?.receipt_id);
      await expect(section).toHaveAttribute("data-state", "approved");
    }
    await until(() => slots().find(slot => slot.run_id === entry.run_id)?.released_ns, "source run slot released");

    const profiles = plan.case === "mix" ? [[0, 0], [0, 1], [0, 0], [0, 1], [1, 1], [2, 0]] : [[0, 0], [0, 1], [1, 1]];
    if (plan.case === "mix") assert.equal(profiles.length, plan.agent_submissions);
    const starts = [];
    for (const [index, [account, gateway]] of profiles.entries()) {
      starts.push(await prepare(`load-${index}`, account, gateway));
    }
    // Fill the account first, then race the independent accounts for the remaining global capacity.
    const split = plan.case === "mix" ? 4 : 2;
    for (const batch of [starts.slice(0, split), starts.slice(split)]) {
      await Promise.all((await Promise.all(batch.map(start => start()))).map(settle));
    }
    const active = report.agents.filter(agent => agent.status === 200);
    assert.equal(active.length, plan.mode === "B" ? 3 : 6);
    assert.equal(active.filter(agent => agent.account === manifest.accounts[0]).length, plan.mode === "B" ? 2 : 4);
    report.held_slots = slots().filter(slot => slot.released_ns === null);
    assert.equal(report.held_slots.length, active.length);

    async function refreshReceipt(expected) {
      await ui.reload();
      await ui.locator('[data-page="chat"]').click();
      await until(() => card.isVisible(), "original conversation card after refresh");
      const readback = monitorResponse(ui, "GET", actionPath);
      await card.click();
      const result = await packet(await readback);
      assertReceipt(result, expected);
      await expect(section).toHaveAttribute("data-state", "approved");
      await expect(section).toContainText(expected.receipt_id);
      return result;
    }

    if (plan.case === "mix") {
      const lanes = [[0, historyPath, "history"], [1, actionPath, "receipt"],
        [1, historyPath, "history"], [0, actionPath, "receipt"]];
      assert.equal(lanes.length, plan.get_lanes);
      const load = async () => {
        const started = performance.now();
        record("get-load-start");
        await Promise.all(lanes.map(async ([gateway, target, kind]) => {
          for (let tick = 0; tick < plan.get_ticks; tick++) {
            const due = started + tick * plan.get_interval_ms;
            await sleep(Math.max(0, due - performance.now()));
            const lag_ms = Math.max(0, performance.now() - due);
            const result = await fetchPacket(pages[gateway], target, tokens[0]).catch(error => ({ status: 0, error: error.message }));
            const { body, ...timing } = result;
            report.samples.push({ tick, gateway, kind, lag_ms, ...timing });
            if (result.status === 200 && kind === "receipt") assertReceipt(result, known);
            if (result.status === 200 && kind === "history") {
              assert.equal(body.data.conversation.conversation_id, conversation);
              assert.ok(body.data.entries.some(row => row.run_id === entry.run_id && row.kind === "assistant"));
            }
          }
        }));
        await sleep(Math.max(0, started + plan.get_duration_ms - performance.now()));
        record("get-load-end");
      };
      const [uiReceipt] = await Promise.all([refreshReceipt(known), load()]);
      report.ui_readback = uiReceipt;
      await ui.screenshot({ path: file("receipt-under-load.png") });
      report.evidence = await fetchPacket(pages[1], evidencePath, tokens[0]);
      assert.equal(report.evidence.status, 200);
      const foreignReceipt = await fetchPacket(pages[1], actionPath, tokens[1]);
      assert.equal(foreignReceipt.status, 404);
      assert.equal(slots().filter(slot => slot.released_ns === null).length, active.length);
    } else {
      report.sqlBeforeApproval = { products: products(), receipts: receipts() };
      assert.deepEqual(report.sqlBeforeApproval.receipts, []);
      const post = monitorResponse(ui, "POST", confirmPath);
      await approve.click();
      await until(() => exists("confirm-held"), "approval response awaiting wire cut");
      report.sqlAtCut = { products: products(), receipts: receipts(), active: slots().filter(row => row.released_ns === null) };
      assert.equal(report.sqlAtCut.active.length, 3);
      const [row] = report.sqlAtCut.receipts;
      assert.equal(row.action_id, action_id);
      const target = report.pending.body.data.proposal.target;
      assert.equal(target.sku, sku);
      const projection = product => ["shop_id", "marketplace_id", "sku"].every(key => product[key] === target[key]);
      const beforeTarget = report.sqlBeforeApproval.products.find(projection);
      const afterTarget = report.sqlAtCut.products.find(projection);
      assert.deepEqual(afterTarget, { ...beforeTarget, version: beforeTarget.version + 1, bullets: afterTarget.bullets });
      assert.deepEqual(JSON.parse(afterTarget.bullets), report.pending.body.data.proposal.bullets.map(bullet => bullet.text));
      assert.deepEqual(report.sqlAtCut.products.filter(product => !projection(product)),
        report.sqlBeforeApproval.products.filter(product => !projection(product)));
      record("independent-sql-commit", { action_id, receipt_id: row.receipt_id });
      await signal("cut-upstream");
      const lost = await post;
      report.post = { status: lost.status(), request_id: (await lost.allHeaders())["x-request-id"] };
      assert.equal(report.post.status, 503);
      report.independent_get = await fetchPacket(pages[1], actionPath, tokens[0]);
      known = report.independent_get.body.data.receipt;
      assert.equal(known.receipt_id, row.receipt_id);
      assertReceipt(report.independent_get, known);
      report.entries_before_retry = (await journal()).filter(row => row.route === "listing-confirm" && row.event === "fastapi-entry");
      assert.deepEqual(report.entries_before_retry.map(row => row.request_id), [report.post.request_id]);
      const manual = monitorResponse(ui, "POST", confirmPath);
      await expect(approve).toBeEnabled();
      record("manual-confirm");
      await approve.click();
      report.manual_post = await packet(await manual);
      assertReceipt(report.manual_post, known);
      await expect(section).toHaveAttribute("data-state", "approved");
      report.refresh_get = await refreshReceipt(known);
      assert.deepEqual({ products: products(), receipts: receipts() },
        { products: report.sqlAtCut.products, receipts: report.sqlAtCut.receipts });
      await ui.screenshot({ path: file("lost-approval-readback.png") });
    }
    await signal("release-all");
    await Promise.all(active.map(close));
    for (const agent of active) assert.ok(agent.frames.some(frame => frame.type === "done"), JSON.stringify(agent));
  } catch (error) {
    report.failure = error.stack;
    process.exitCode = 1;
  } finally {
    db?.close();
    await browser?.close();
    if (report.page_errors.length) process.exitCode = 1;
    await fs.writeFile(file("browser.json"), JSON.stringify(report, null, 2) + "\n");
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
