// Run from the repo root with Python dependencies, Node.js, Playwright and Chrome.
const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const { randomBytes } = require("node:crypto");
const { once } = require("node:events");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const readline = require("node:readline");
const { chromium } = require("playwright");
const { browserOptions } = require("./browser_options.cjs");

const streamPath = "/api/chat/stream";
const chunkPath = "/api/knowledge/chunks/ctr-review-v1-low-ctr-01";
const original = "两个可比窗口各达到 1,000 次计划曝光，且当前 CTR 较基准窗口相对下降至少 20% 时，标记为待复核。"
  + "先核对同一计划的历史曝光和点击，确认后检查素材与流量构成。用户级汇总不得代替计划指标。";
const ruleId = "ctr-review-v1#low-ctr";
const gap = "缺少该计划在基准窗口和当前窗口的历史 CTR";
const windows = [
  { baseline: { start_date: "2026-10-19", end_date: "2026-10-25" },
    current: { start_date: "2026-10-26", end_date: "2026-11-01" } },
  { baseline: { start_date: "2026-10-29", end_date: "2026-10-31" },
    current: { start_date: "2026-11-02", end_date: "2026-11-04" } },
];
const firstPayload = { diagnosis: { plan_id: "101", rule_id: ruleId, ...windows[0] } };
const matchesPath = (item, pathname) => new URL(item.url()).pathname === pathname;
const sse = (route, ...events) => route.fulfill({
  contentType: "text/event-stream",
  body: events.map(event => `data: ${JSON.stringify(event)}\n\n`).join(""),
});

async function login(page, username = "diagnosis-demo") {
  await page.waitForFunction(() => window._captchaId);
  await page.locator("#loginUser").fill(username);
  await page.locator("#loginPass").fill("demo-password");
  await page.locator("#loginCaptcha").fill("0726");
  await page.locator("#loginCaptcha").press("Enter");
  await page.locator("#navUsername").getByText(username, { exact: true }).waitFor();
}

async function openDiagnosis(page) {
  await page.locator('[data-page="chat"]').click();
  await page.getByRole("button", { name: "投放诊断", exact: true }).click();
  await page.waitForFunction(() => !document.getElementById("diagnosisInputs").disabled);
  if (!(await page.locator("#diagnosisStructuredFields").isVisible())) await page.locator("#diagnosisMode").click();
}

function field(page, name) {
  return page.locator(`#diagnosisForm input[name="${name}"]`);
}

async function fillWindows(page, index) {
  for (const [label, dates] of Object.entries(windows[index])) {
    await field(page, label + "_start").fill(dates.start_date);
    await field(page, label + "_end").fill(dates.end_date);
  }
}

async function fillFirst(page) {
  await field(page, "plan_id").fill("101");
  await field(page, "rule_id").fill(ruleId);
  await fillWindows(page, 0);
}

async function submit(page, expected) {
  const rounds = page.locator(".diagnosis-round");
  const index = await rounds.count();
  const [request] = await Promise.all([
    page.waitForRequest(request => matchesPath(request, streamPath)),
    page.locator("#diagnosisSubmit").click(),
  ]);
  assert.deepEqual(request.postDataJSON(), expected);
  return rounds.nth(index);
}

async function submitted(round) {
  await round.locator(".diagnosis-status[data-state=done]").getByText("已提交", { exact: true }).waitFor();
}

async function reviewReadback({ page, directory, output, restart }) {
  await fillFirst(page);
  let events;
  await page.route("**" + streamPath, async route => {
    const response = await route.fetch();
    events = (await response.text()).split(/\r?\n/).filter(line => line.startsWith("data: "))
      .map(line => JSON.parse(line.slice(6)));
    await route.fulfill({ response });
  }, { times: 1 });
  const first = await submit(page, firstPayload);
  await submitted(first);
  const proposal = events.at(-1).review_proposal;
  const panel = first.locator(".diagnosis-review");
  await panel.getByText("待确认 · 提交人工复核", { exact: true }).waitFor();
  assert.match(await panel.textContent(), /2026-10-19/);
  assert.ok((await panel.textContent()).includes(gap));
  const rounds = [await inspectRound(page, directory, first, 0, firstPayload)];
  if (output) await screenshot(page, first, output, "diagnosis-round-1");

  await fillWindows(page, 1);
  const continuation = { diagnosis: { task_id: proposal.task_id, ...windows[1] } };
  const second = await submit(page, continuation);
  await submitted(second);
  assert.match(await second.locator(".diagnosis-review").textContent(), /2026-10-29/);
  rounds.push(await inspectRound(page, directory, second, 1, continuation));
  if (output) await screenshot(page, second, output, "diagnosis-round-2");

  await page.route("**/api/diagnosis/reviews/*", route => route.fulfill({ status: 503 }), { times: 1 });
  const confirmPath = "/api/diagnosis/review-proposals/" + proposal.action_id + "/confirm";
  const [confirmation] = await Promise.all([
    page.waitForResponse(response => matchesPath(response, confirmPath)),
    panel.getByRole("button", { name: "确认创建本地复核单" }).click(),
  ]);
  assert.equal(confirmation.status(), 200);
  assert.equal(confirmation.request().postData(), null);
  const { data: confirmed } = await confirmation.json();
  const reviewPath = "/api/diagnosis/reviews/" + confirmed.review_id;
  await panel.getByText("读取失败，可重新读取。", { exact: false }).waitFor();
  assert.equal(await panel.getByText("已保存的人工复核单").count(), 0);

  const [readback] = await Promise.all([
    page.waitForResponse(response => matchesPath(response, reviewPath)),
    panel.getByRole("button", { name: "重新读取复核单" }).click(),
  ]);
  assert.equal(readback.status(), 200);
  const { data: receipt } = await readback.json();
  assert.deepEqual(receipt, { ...proposal, review_id: confirmed.review_id, created_at: confirmed.created_at });
  assert.deepEqual(receipt.baseline, windows[0].baseline);
  assert.deepEqual(receipt.current, windows[0].current);
  assert.equal(receipt.source_trace_id, await first.locator(".diagnosis-trace code").textContent());
  assert.notEqual(receipt.source_trace_id, await second.locator(".diagnosis-trace code").textContent());
  await panel.getByText("已保存的人工复核单", { exact: true }).waitFor();
  await panel.getByText("复核单 ID " + receipt.review_id, { exact: false }).waitFor();
  assert.ok((await panel.textContent()).includes(receipt.citations[0].chunk_id));
  if (output) await screenshot(page, panel, output, "diagnosis-review");

  const { authorization } = await confirmation.request().allHeaders();
  const restarted = await restart();
  const persisted = await fetch(restarted + reviewPath, { headers: { authorization } });
  assert.equal(persisted.status, 200);
  const { data: persistedReceipt } = await persisted.json();
  assert.deepEqual(persistedReceipt, receipt);

  // All IDs and content here come from the isolated demo seeds. Omit request headers.
  const review = {
    proposal,
    confirmation: { http: confirmation.status(), path: confirmPath, data: confirmed },
    readback: { http: readback.status(), path: reviewPath, data: receipt },
    readback_after_restart: { http: persisted.status, path: reviewPath, data: persistedReceipt },
  };
  if (output) {
    await fs.writeFile(path.join(output, "diagnosis-evidence.json"), JSON.stringify({ rounds, review }, null, 2) + "\n");
  }
  console.log(JSON.stringify(review));
}

async function screenshot(page, target, output, name) {
  await target.scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(output, `${name}.png`) });
}

async function readTraces(directory) {
  try {
    const content = await fs.readFile(path.join(directory, "traces.jsonl"), "utf8");
    return content.trim().split("\n").filter(Boolean).map(line => JSON.parse(line));
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
    return [];
  }
}

async function inspectRound(page, directory, round, index, request) {
  await round.locator(".diagnosis-reply").getByText("证据不足", { exact: false }).waitFor();
  const gaps = await round.locator(".diagnosis-gaps li").allTextContents();
  assert.deepEqual(gaps, index ? [gap, "当前窗口用户日报无数据"] : [gap]);
  const [chunkResponse] = await Promise.all([
    page.waitForResponse(response => matchesPath(response, chunkPath)),
    round.getByText("点击率下降复核 · v1", { exact: true }).click(),
  ]);
  assert.equal(chunkResponse.status(), 200);
  const chunk = await chunkResponse.json();
  assert.equal(chunk.content, original);
  await round.locator(".diagnosis-citation pre").getByText(original, { exact: true }).waitFor();

  await round.getByRole("button", { name: "复制 Trace ID" }).click();
  await round.getByText("已复制", { exact: true }).waitFor();
  const traceId = await page.evaluate(() => navigator.clipboard.readText());
  assert.match(traceId, /^[a-f0-9]{12}$/);
  assert.equal(traceId, await round.locator(".diagnosis-trace code").textContent());
  const trace = (await readTraces(directory)).find(row => row.trace_id === traceId);
  assert.equal(trace?.status, "completed");
  assert.ok(trace.steps.some(step => step.stage === "persistence" && step.action === "save_reply"));
  const tools = trace.steps.filter(step => step.stage === "evidence_received");
  assert.deepEqual(tools.map(step => step.action).sort(),
    ["get_daily_report_by_date", "get_daily_report_by_date", "get_plan_detail", "search_knowledge"]);
  for (const step of tools) {
    assert.ok(trace.steps.some(call => call.stage === "tool_call" && call.runtime_call_id === step.runtime_call_id));
    assert.ok(trace.steps.some(result => result.stage === "tool_result" && result.runtime_call_id === step.runtime_call_id));
  }
  return { request, copied_trace_id: traceId, gaps, citation: { http: chunkResponse.status(), path: chunkPath, chunk }, trace };
}

async function evidence({ page, directory }) {
  await fillFirst(page);
  const evidence = [];
  let taskId;
  for (const index of [0, 1]) {
    if (index) await fillWindows(page, index);
    const request = index ? { diagnosis: { task_id: taskId, ...windows[index] } } : firstPayload;
    const round = await submit(page, request);
    await submitted(round);
    const id = (await page.locator("#diagnosisTask").textContent()).split(" ")[2];
    assert.match(id, /^[a-f0-9]{32}$/);
    if (index) assert.equal(id, taskId);
    taskId = id;
    assert.equal(await field(page, "plan_id").inputValue(), "101");
    assert.equal(await field(page, "rule_id").inputValue(), ruleId);
    assert.equal(await field(page, "plan_id").evaluate(input => input.readOnly), true);
    assert.equal(await field(page, "rule_id").evaluate(input => input.readOnly), true);

    evidence.push(await inspectRound(page, directory, round, index, request));
  }
  assert.equal(await page.locator(".diagnosis-status[data-state=done]").count(), 2);
  assert.notEqual(evidence[0].copied_trace_id, evidence[1].copied_trace_id);
  console.log(JSON.stringify(evidence.map(({ request, copied_trace_id, gaps, citation, trace }) =>
    ({ request, copied_trace_id, gaps, chunk_id: citation.chunk.chunk_id,
      tools: trace.steps.filter(step => step.stage === "evidence_received").map(step => step.action) }))));
}

const failures = [
  ["HTTP failure", route => route.fulfill({ status: 503, json: { detail: "演示服务暂不可用" } }), "HTTP 503"],
  ["SSE error", route => sse(route, { type: "text", content: "未提交的诊断回复" },
    { type: "error", content: "回复保存失败" }, { type: "done", task_id: "ignore-late-done" }), "回复保存失败"],
  ["early EOF", route => sse(route, { type: "text", content: "未提交的诊断回复" }), "未收到提交确认"],
];

async function failureRecovery({ page, origin }, fail, message) {
  await fillFirst(page);
  await page.route(origin + streamPath, fail, { times: 1 });
  const failed = await submit(page, firstPayload);
  await failed.locator(".diagnosis-status[data-state=error]").getByText(message, { exact: false }).waitFor();
  assert.equal(await page.locator(".diagnosis-status[data-state=done]").count(), 0);
  assert.equal(await field(page, "rule_id").inputValue(), ruleId);
  assert.equal(await field(page, "current_end").inputValue(), windows[0].current.end_date);
  assert.equal(await field(page, "plan_id").isEditable(), true);
  await submitted(await submit(page, firstPayload));
}

const acceptedTaskId = "0123456789abcdef0123456789abcdef";
const accepted = { type: "thinking", content: "任务已接受", task_id: acceptedTaskId };
const uncommitted = { type: "text", content: "未提交的诊断回复" };
const saveError = { type: "error", content: "回复保存失败", task_id: acceptedTaskId };
const confirmation = { type: "done", task_id: acceptedTaskId };
const acceptedFailures = [
  ["SSE save error after acceptance", [accepted, uncommitted, saveError, confirmation], saveError.content],
  ["early EOF after acceptance", [accepted, uncommitted], "未收到提交确认"],
  ["ID first delivered in SSE error", [saveError], saveError.content],
  ["mismatched done after acceptance", [accepted, { type: "done", task_id: "other-task" }], "任务标识异常"],
];

async function acceptedFailureRecovery({ page, origin }, name, events, message) {
  const requests = [];
  page.on("request", request => {
    if (matchesPath(request, streamPath)) requests.push(request.postDataJSON());
  });
  await fillFirst(page);
  await page.route(origin + streamPath, route => sse(route, ...events), { times: 1 });
  const failed = await submit(page, firstPayload);
  const error = failed.locator(".diagnosis-status[data-state=error]").getByText(message, { exact: false });
  await error.waitFor();
  assert.equal(await page.locator(".diagnosis-status[data-state=done]").count(), 0);
  assert.equal(await page.locator("#diagnosisTask").textContent(), `任务 ID ${acceptedTaskId}`);
  assert.equal(await field(page, "plan_id").isEditable(), false);
  assert.equal(await field(page, "rule_id").isEditable(), false);
  assert.equal(await field(page, "current_end").inputValue(), windows[0].current.end_date);
  assert.equal(await field(page, "current_end").isEditable(), true);
  assert.equal(await page.locator("#diagnosisSubmit").textContent(), "重试诊断");
  assert.equal(requests.length, 1);

  await page.route(origin + streamPath, route => sse(route, accepted, confirmation), { times: 1 });
  const retry = await submit(page, { diagnosis: { task_id: acceptedTaskId, ...windows[0] } });
  await submitted(retry);
  await error.waitFor();
  assert.equal(await page.locator(".diagnosis-status[data-state=done]").count(), 1);
  assert.equal(await page.locator("#diagnosisTask").textContent(), `任务 ID ${acceptedTaskId}`);
  console.log(JSON.stringify({ scenario: name, failed_request: requests[0], retry_request: requests[1] }));
}

async function concurrency({ page, origin }) {
  const ordinary = Promise.withResolvers();
  const diagnosis = Promise.withResolvers();
  await page.route(origin + streamPath, async route => {
    if (route.request().postDataJSON().diagnosis) {
      await diagnosis.promise;
      return route.continue();
    }
    await ordinary.promise;
    return sse(route, { type: "text", content: "普通聊天独立回复" }, { type: "done" });
  });
  await fillFirst(page);
  try {
    await submit(page, firstPayload);
    assert.equal(await page.locator("#diagnosisSubmit").isDisabled(), true);
    await page.getByRole("button", { name: "返回工作台", exact: true }).click();
    await page.locator("#chatInput").fill("普通聊天并发测试");
    await Promise.all([
      page.waitForRequest(req => matchesPath(req, streamPath) && req.postDataJSON().message),
      page.locator("#chatInput").press("Enter"),
    ]);
    await page.locator('[data-page="dashboard"]').click();
    await page.locator("#dashStats .stat-card").first().waitFor();
    diagnosis.resolve();
    await page.waitForFunction(() => document.querySelector(".diagnosis-status")?.dataset.state === "done");
    assert.equal(await page.locator("#chatMessages .assistant").textContent(), "");
    ordinary.resolve();
    await page.locator("#chatMessages").getByText("普通聊天独立回复").waitFor({ state: "attached" });
    await openDiagnosis(page);
    await submitted(page.locator(".diagnosis-round"));
    assert.equal(await page.locator("#diagnosisRounds").getByText("普通聊天独立回复").count(), 0);
    assert.equal(await page.locator("#chatMessages").getByText("证据不足", { exact: false }).count(), 0);
  } finally {
    diagnosis.resolve();
    ordinary.resolve();
  }
}

async function accountSwitch({ page, origin }) {
  await fillFirst(page);
  await submitted(await submit(page, firstPayload));
  const taskId = (await page.locator("#diagnosisTask").textContent()).split(" ")[2];
  const late = Promise.withResolvers();
  await page.route(origin + streamPath, async route => {
    await late.promise;
    await sse(route, { type: "text", content: "上一账号的延迟回复" }, { type: "done", task_id: taskId })
      .catch(() => {}); // The old session aborts its request on logout.
  }, { times: 1 });
  try {
    await fillWindows(page, 1);
    await submit(page, { diagnosis: { task_id: taskId, ...windows[1] } });
    const oldCaptcha = await page.evaluate(() => window._captchaId);
    await Promise.all([
      page.waitForEvent("requestfailed", request => matchesPath(request, streamPath)),
      page.getByRole("button", { name: "退出登录" }).click(),
    ]);
    assert.equal(await page.locator(".diagnosis-round").count(), 0);
    await page.waitForFunction(id => window._captchaId !== id, oldCaptcha);
    await login(page, "second-demo");
    await openDiagnosis(page);
    late.resolve();
    assert.equal(await page.locator(".diagnosis-round").count(), 0);
    assert.equal(await page.locator("#diagnosisTask").textContent(), "");
    assert.equal(await field(page, "plan_id").inputValue(), "");
    assert.equal(await field(page, "current_end").inputValue(), "");
    assert.equal(await field(page, "rule_id").isEditable(), true);
  } finally {
    late.resolve();
  }
}

async function staleAcceptance({ page, origin }) {
  const late = Promise.withResolvers();
  await page.route(origin + streamPath, async route => {
    await late.promise;
    await sse(route, accepted, saveError);
  }, { times: 1 });
  await page.evaluate(() => {
    const read = window.readChatStream;
    window.readChatStream = async (payload, token, onEvent) => {
      window.readChatStream = read;
      try {
        // Simulate a response already queued for delivery when the session aborts.
        await read(payload, token, onEvent);
      } catch (error) {
        window.staleDiagnosisError = error.message;
        throw error;
      }
    };
  });
  try {
    await fillFirst(page);
    await submit(page, firstPayload);
    const oldCaptcha = await page.evaluate(() => window._captchaId);
    await page.getByRole("button", { name: "退出登录" }).click();
    assert.equal(await page.locator("#diagnosisTask").textContent(), "");
    await page.waitForFunction(id => window._captchaId !== id, oldCaptcha);
    await login(page, "second-demo");
    await openDiagnosis(page);
    late.resolve();
    await page.waitForFunction(message => window.staleDiagnosisError === message, saveError.content);
    assert.equal(await page.locator(".diagnosis-round").count(), 0);
    assert.equal(await page.locator("#diagnosisTask").textContent(), "");
    assert.equal(await field(page, "plan_id").inputValue(), "");
    assert.equal(await field(page, "plan_id").isEditable(), true);
    await fillFirst(page);
    await page.route(origin + streamPath, route => sse(route, { type: "done", task_id: "second-account-task" }),
      { times: 1 });
    await submitted(await submit(page, firstPayload));
  } finally {
    late.resolve();
  }
}

function startServer(directory, signingKey) {
  const child = spawn(process.env.PYTHON || "python", [
    "-B", "-X", "utf8", "-m", "tests.home_diagnosis_server", directory,
  ], {
    stdio: ["ignore", "pipe", "inherit"],
    windowsHide: true,
    env: { ...process.env, JWT_SECRET: signingKey },
  });
  const origin = new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error("FastAPI demo startup timed out")), 20000);
    const lines = readline.createInterface({ input: child.stdout });
    lines.on("line", line => {
      if (!line.startsWith("DEMO_URL=")) return;
      clearTimeout(timeout);
      resolve(line.slice("DEMO_URL=".length));
    });
    child.once("error", error => { clearTimeout(timeout); reject(error); });
    child.once("exit", code => { clearTimeout(timeout); reject(new Error(`FastAPI demo exited with ${code}`)); });
  });
  return { child, origin };
}

async function stopServer({ child }) {
  if (child.pid && child.exitCode === null && child.signalCode === null) {
    const exited = once(child, "exit");
    child.kill();
    await exited;
  }
}

async function run() {
  const output = process.argv[2] && path.resolve(process.argv[2]);
  if (output) await fs.mkdir(output, { recursive: true });
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "ad-diagnosis-"));
  const signingKey = randomBytes(32).toString("hex");
  let server = startServer(directory, signingKey);
  async function restart() {
    await stopServer(server);
    server = startServer(directory, signingKey);
    return server.origin;
  }
  let browser;
  try {
    const origin = await server.origin;
    browser = await chromium.launch(browserOptions());
    console.log("AD_BROWSER " + browser.version());
    const cases = [
      ["real FastAPI two-round demo with original and clipboard trace lookup", evidence],
      ...failures.map(([name, fail, message]) =>
        [`${name} restores input and allows retry`, options => failureRecovery(options, fail, message)]),
      ["ordinary chat and diagnosis stay paired through navigation", concurrency],
      ["logout and account switch clear active diagnosis", accountSwitch],
      ...acceptedFailures.map(([name, events, message]) =>
        [`${name} keeps the accepted task and the failed round`,
          options => acceptedFailureRecovery(options, name, events, message)]),
      ["late task acceptance cannot restore a diagnosis after account switch", staleAcceptance],
      ["confirm round A after round B and read its saved receipt after restart", reviewReadback],
    ];
    let passed = 0;
    let traceCount = 0;
    const evalTraces = [];
    for (const [name, test] of cases) {
      const context = await browser.newContext({ viewport: { width: 1280, height: 1200 } });
      let failure = null;
      try {
        await context.grantPermissions(["clipboard-read", "clipboard-write"], { origin });
        const page = await context.newPage();
        page.setDefaultTimeout(8000);
        if (test === concurrency) {
          // This scenario's mocked ordinary stream uses the legacy, ID-free SSE contract.
          await page.route(origin + "/api/chat/conversations", route => route.fulfill({ status: 404 }));
        }
        await page.goto(origin);
        await login(page);
        await openDiagnosis(page);
        // Each original scenario starts a fresh diagnosis in the shared fixture.
        const newDiagnosis = page.locator("#diagnosisNew");
        if (await newDiagnosis.isVisible()) await newDiagnosis.click();
        await test({ page, directory, origin, output, restart });
        passed++;
        console.log(`PASS ${name}`);
      } catch (error) {
        failure = error.message;
        console.error(`FAIL ${name}`, error);
      } finally {
        await context.close();
      }
      if (process.env.AD_EVAL_RESULTS) {
        const traces = await readTraces(directory);
        const current = traces.slice(traceCount);
        traceCount = traces.length;
        evalTraces.push(...current.map(trace => ({ case: name, ...trace })));
        console.log("AD_EVAL_CASE " + JSON.stringify({
          name, failure, trace_ids: current.map(trace => trace.trace_id),
        }));
      }
    }
    if (output && process.env.AD_EVAL_RESULTS) {
      await fs.writeFile(path.join(output, "browser-traces.jsonl"), evalTraces.map(trace => JSON.stringify(trace) + "\n").join(""));
    }
    console.log(`${passed}/${cases.length} diagnosis browser cases passed`);
    if (passed !== cases.length) process.exitCode = 1;
  } finally {
    await browser?.close();
    await stopServer(server);
    await fs.rm(directory, { recursive: true, force: true, maxRetries: 3 });
  }
}

run().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
