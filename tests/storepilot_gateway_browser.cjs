const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const { DatabaseSync } = require("node:sqlite");
const { chromium, expect } = require("playwright/test");
const { submitGroupCreation } = require("./chat_group_create.cjs");

const [origin, folder, scenario] = process.argv.slice(2);
const streamPath = "/api/chat/stream";
const groups = "/api/chat/conversations";
const report = { scenario, browser: {}, points: [], pageErrors: [] };
const file = name => path.join(folder, name);
const record = (event, fields = {}) => report.points.push({ event, epoch_ms: Date.now(), ...fields });
const signal = async name => { record(name); await fs.writeFile(file(name), ""); };
const exists = name => fs.access(file(name)).then(() => true, () => false);
const waitFor = (callback, message, timeout = 20000) => expect.poll(callback, { message, timeout }).toBeTruthy();

function snapshot(db) {
  return Object.fromEntries(Object.entries({
    products: "listing_test_products ORDER BY sku",
    proposals: "listing_change_proposals ORDER BY action_id",
    receipts: "listing_change_receipts ORDER BY receipt_id",
    answers: "chat_group_entries WHERE kind='assistant' ORDER BY id",
    runs: "chat_group_runs ORDER BY run_id",
    adProposals: "chat_group_pause_proposals ORDER BY action_id",
    adReceipts: "chat_group_pause_receipts ORDER BY receipt_id",
  }).map(([key, sql]) => [key, db.prepare("SELECT * FROM " + sql).all()]));
}

async function packet(response) {
  const headers = await response.allHeaders?.() ?? response.headers();
  return { status: response.status(), request_id: headers["x-request-id"],
    body: await response.json().catch(() => response.text()) };
}

async function run() {
  const manifest = JSON.parse(await fs.readFile(path.resolve(folder, "../../manifest.json"), "utf8"));
  const source = manifest.sources.find(product => product.target.sku === manifest.scenario.sku);
  const ad = scenario === "ad-worker-abort";
  const { ad_message } = JSON.parse(await fs.readFile(file("case.json"), "utf8"));
  report.message = ad ? ad_message : manifest.scenario.message;
  let browser, page, db;
  try {
    db = new DatabaseSync(file("run.sqlite3"), { readOnly: true });
    report.before = snapshot(db);
    browser = await chromium.launch({ channel: "msedge" });
    report.browser.version = browser.version();
    page = await browser.newPage({ baseURL: origin, viewport: { width: 1360, height: 1050 }, timezoneId: "Asia/Shanghai" });
    page.setDefaultTimeout(15000);
    page.on("pageerror", error => report.pageErrors.push(error.message));
    await page.addInitScript(({ streamPath, scenario }) => {
      const nativeFetch = window.fetch.bind(window);
      window.transport = null;
      window.fetch = async (url, options) => {
        const isStream = url === streamPath && options?.method === "POST";
        const confirm = typeof url === "string" && url.endsWith("/confirm") && url.includes("/listing-actions/");
        if (!isStream && !confirm) return nativeFetch(url, options);
        const headers = new Headers(options.headers);
        if (["before-headers", "upstream-abort", "lost-confirm", "rollback", "idle-timeout", "max-duration"].includes(scenario)) {
          headers.set("x-envoy-internal", "true");
          headers.set("x-envoy-retry-on", "5xx,reset,connect-failure");
          headers.set("x-envoy-max-retries", "2");
          headers.set("x-envoy-hedge-on-per-try-timeout", "true");
          headers.set("x-envoy-upstream-rq-timeout-ms", "40000");
          headers.set("x-envoy-upstream-rq-per-try-timeout-ms", "20000");
          headers.set("x-envoy-upstream-stream-duration-ms", "30");
        }
        if (!isStream) return nativeFetch(url, { ...options, headers });
        const controller = new AbortController();
        window.abortTransport = () => controller.abort();
        const signal = options.signal ? AbortSignal.any([controller.signal, options.signal]) : controller.signal;
        const started = performance.now();
        const data = window.transport = { started_epoch_ms: Date.now(), frames: [] };
        const response = await nativeFetch(url, { ...options, headers, signal }).catch(error => {
          data.error = error.message;
          throw error;
        });
        data.headers_ms = performance.now() - started;
        data.status = response.status;
        data.request_id = response.headers.get("x-request-id");
        if (!response.ok) return response;
        const decoder = new TextDecoder();
        let pending = "";
        const tap = new TransformStream({
          transform(chunk, stream) {
            pending += decoder.decode(chunk, { stream: true });
            const frames = pending.split(/\r?\n\r?\n/);
            pending = frames.pop();
            for (const raw of frames) {
              if (raw.startsWith("data: ")) data.frames.push({ ms: performance.now() - started, raw,
                event: JSON.parse(raw.slice(6)) });
            }
            stream.enqueue(chunk);
          },
        });
        return new Response(response.body.pipeThrough(tap), { status: response.status, headers: response.headers });
      };
    }, { streamPath, scenario });
    await page.goto(origin);
    await page.locator("#loginUser").fill("task45-operator");
    await page.locator("#loginPass").fill(process.env.STOREPILOT_TEST_PASSWORD);
    await page.waitForFunction(() => Boolean(window._captchaId));
    await page.locator("#loginCaptcha").fill("0726");
    const onResponse = (method, target) => page.waitForResponse(response => response.request().method() === method &&
      new URL(response.url()).pathname === target);
    const loginResponse = onResponse("POST", "/api/login");
    await page.locator("#loginPass").press("Enter");
    const login = await (await loginResponse).json();
    assert.equal(login.code, 200);
    const headers = { Authorization: "Bearer " + login.data.token };
    const get = async url => packet(await page.request.get(url, { headers }));
    await page.locator('[data-page="chat"]').click();
    const creation = onResponse("POST", groups);
    await submitGroupCreation(page, "StorePilot " + scenario);
    const { conversation_id: conversation } = (await (await creation).json()).data;
    const group = `${groups}/${conversation}`;
    report.conversation_id = conversation;
    await page.locator("#chatInput").fill(report.message);
    await page.locator("#chatSend").click();
    const status = page.locator("#chatGroupStatus");

    if (scenario === "before-headers") {
      await expect(status).toContainText("HTTP 503");
      report.ui_error = await status.innerText();
      report.history = await get(group + "/history");
      assert.equal(report.history.body.data.entries.length, 0);
      assert.equal(report.history.body.data.runs.length, 0);
      assert.deepEqual(snapshot(db), report.before);
      return;
    }

    const transport = () => page.evaluate(() => window.transport);
    const progress = await page.waitForFunction(() => window.transport?.frames.find(row => row.event.type === "thinking"));
    const { trace_id, run_id } = (await progress.jsonValue()).event;
    report.trace_id = trace_id;
    report.run_id = run_id;
    if (["normal", "client-abort", "upstream-abort", "idle-timeout", "max-duration", "ad-worker-abort"].includes(scenario)) {
      await waitFor(() => exists("model-held"), "fixed Worker is at the model barrier");
      const board = page.locator(`[data-run-id="${run_id}"].workbench-delegation, .group-progress[data-run-id="${run_id}"]`);
      await expect(board).toBeVisible();
      report.visible_progress = await board.innerText();
      assert.ok(!(await transport()).frames.some(row => row.event.type === "done"));
      record("progress-visible");
      await board.screenshot({ path: file("in-progress.png") });
      if (scenario === "normal") await signal("release-model");
      if (["client-abort", "ad-worker-abort"].includes(scenario)) {
        record("client-abort");
        await page.evaluate(() => window.abortTransport());
      }
      if (scenario === "upstream-abort") await signal("cut-upstream");
      if (scenario !== "normal") {
        await waitFor(() => exists("app-disconnected"), "disconnect reached FastAPI", scenario === "max-duration" ? 115000 : 30000);
        await signal("release-model");
        const expectedStatus = scenario === "max-duration" ? "error" : "interrupted";
        await waitFor(() => snapshot(db).runs.some(run => run.status === expectedStatus), "terminal SQL status");
        await expect(status).not.toHaveText("");
        report.ui_error = await status.innerText();
        report.history = await get(group + "/history");
        assert.ok(!report.history.body.data.entries.some(entry => entry.kind === "assistant"));
        const after = snapshot(db);
        for (const key of ["products", "proposals", "receipts", "answers", "adProposals", "adReceipts"]) {
          assert.deepEqual(after[key], report.before[key]);
        }
        assert.ok(!(await transport()).frames.some(row => row.event.type === "done"));
        return;
      }
    }

    if (scenario === "lost-done") {
      await waitFor(() => exists("done-held"), "terminal frame held at the upstream socket");
      report.sqlAtCut = snapshot(db);
      assert.equal(report.sqlAtCut.answers.length, 1);
      assert.equal(report.sqlAtCut.proposals.length, 1);
      assert.deepEqual(report.sqlAtCut.products, report.before.products);
      await page.waitForFunction(() => window.transport?.frames.some(row => row.event.type === "text"));
      record("independent-sql-commit");
      await signal("cut-upstream");
      await expect(status).not.toHaveText("");
      report.ui_error = await status.innerText();
    }
    const card = page.locator(`.msg[data-run-id="${run_id}"] [data-result-kind="listing"]`);
    await expect(card).toBeVisible();
    if (scenario !== "lost-done") {
      await page.waitForFunction(() => window.transport?.frames.some(row => row.event.type === "done"));
    }
    const frames = (await transport()).frames;
    const terminal = frames.find(row => row.event.type === "done");
    assert.equal(Boolean(terminal), scenario !== "lost-done");
    if (terminal) assert.equal(terminal.event.trace_id, trace_id);
    report.history = await get(group + "/history");
    const [entry] = report.history.body.data.entries.filter(row => row.kind === "assistant");
    assert.equal(report.history.body.data.entries.filter(row => row.kind === "assistant").length, 1);
    assert.equal(entry.run_id, run_id);
    assert.equal(entry.content, frames.filter(row => row.event.type === "text").map(row => row.event.content).join(""));
    const runPath = `${group}/runs/${run_id}`;
    report.evidence = await get(runPath + "/evidence");
    const evidence = report.evidence.body.data;
    const expected = manifest.sql_oracle.find(row => row.sku === source.target.sku);
    assert.equal(evidence.metrics.baseline.units, expected.baseline_units);
    assert.equal(evidence.metrics.current.units, expected.current_units);
    const [{ action_id }] = entry.listing_actions;
    report.action_id = action_id;
    const actionPath = `${runPath}/listing-actions/${action_id}`;
    const pending = onResponse("GET", actionPath);
    await card.click();
    report.pending = await packet(await pending);
    const proposal = report.pending.body.data.proposal;
    assert.equal(report.pending.body.data.state, "pending");
    assert.deepEqual(proposal.target, source.target);
    assert.equal(proposal.original_version, source.listing_version);
    assert.equal(proposal.facts_version, source.facts_version);
    assert.equal(proposal.bullets.length, 5);
    assert.equal(new Set(proposal.bullets.map(row => row.fact_id)).size, 5);
    for (const bullet of proposal.bullets) {
      const fact = source.facts.find(row => row.fact_id === bullet.fact_id);
      assert.equal(bullet.statement, fact.statement);
      assert.ok(fact.options.includes(bullet.text));
    }
    const section = page.locator(".group-listing-action");
    const button = section.getByRole("button", { name: "批准应用英文五点（本地测试）", exact: true });
    await expect(button).toBeVisible();
    await section.screenshot({ path: file("pending.png") });
    report.sqlPending = snapshot(db);
    assert.equal(report.sqlPending.proposals.length, 1);
    assert.equal(report.sqlPending.answers[0].content, entry.content);
    assert.deepEqual(report.sqlPending.products, report.before.products);
    assert.equal(report.sqlPending.receipts.length, 0);
    if (scenario === "lost-done") return;

    const confirmPath = actionPath + "/confirm";
    const post = onResponse("POST", confirmPath);
    await button.click();
    if (scenario === "lost-confirm") {
      await waitFor(() => exists("confirm-held"), "approval response is held before upstream headers");
      report.sqlAtCut = snapshot(db);
      assert.equal(report.sqlAtCut.receipts.length, 1);
      assert.equal(report.sqlAtCut.products.find(row => row.sku === source.target.sku).version, source.listing_version + 1);
      record("independent-sql-commit");
      await signal("cut-upstream");
    }
    report.post = await packet(await post);
    report.independentGet = await get(actionPath);
    if (scenario === "rollback") {
      assert.equal(report.post.status, 503);
      assert.equal(report.independentGet.body.data.state, "pending");
      assert.deepEqual(snapshot(db), report.sqlPending);
      return;
    }

    const receipt = report.independentGet.body.data.receipt;
    assert.ok(receipt.receipt_id);
    if (scenario === "lost-confirm") {
      assert.equal(report.post.status, 503);
      const entries = (await fs.readFile(file("transport.jsonl"), "utf8")).trim().split("\n").map(JSON.parse);
      report.confirmBeforeManual = entries.filter(row => row.event === "fastapi-entry" && row.route === "listing-confirm");
      assert.equal(report.confirmBeforeManual.length, 1);
      assert.equal(report.confirmBeforeManual[0].request_id, report.post.request_id);
      record("manual-confirm");
      report.manualPost = await packet(await page.request.post(confirmPath, { headers }));
      assert.equal(report.manualPost.status, 200);
      assert.deepEqual(report.manualPost.body.data.receipt, receipt);
    } else {
      assert.equal(report.post.status, 200);
      assert.deepEqual(report.post.body.data.receipt, receipt);
    }
    report.stream = await transport();
    await page.reload();
    const refreshed = onResponse("GET", actionPath);
    await card.click();
    report.refreshGet = await packet(await refreshed);
    await expect(section).toHaveAttribute("data-state", "approved");
    await section.screenshot({ path: file("approved.png") });
    assert.deepEqual(report.refreshGet.body.data.receipt, receipt);
    const after = snapshot(db);
    assert.equal(after.receipts.length, 1);
    assert.equal(after.receipts[0].receipt_id, receipt.receipt_id);
    for (const product of after.products) {
      const old = report.before.products.find(row => row.sku === product.sku);
      if (product.sku === source.target.sku) {
        assert.equal(product.version, old.version + 1);
        assert.deepEqual(JSON.parse(product.bullets), proposal.bullets.map(row => row.text));
      } else assert.deepEqual(product, old);
    }
  } catch (error) {
    report.failure = error.stack;
    process.exitCode = 1;
    await page?.screenshot({ path: file("failure.png"), timeout: 3000 }).catch(() => {});
  } finally {
    report.stream ??= await page?.evaluate(() => window.transport).catch(() => null);
    if (report.pageErrors.length) process.exitCode = 1;
    if (db) { report.after = snapshot(db); db.close(); }
    await browser?.close();
    await fs.writeFile(file("browser.json"), JSON.stringify(report, null, 2) + "\n");
  }
}

run().catch(error => { console.error(error); process.exitCode = 1; });
