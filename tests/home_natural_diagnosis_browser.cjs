// Real Provider acceptance only. Keep login responses and authorization headers out of reports.
const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const { once } = require("node:events");
const fs = require("node:fs/promises");
const path = require("node:path");
const readline = require("node:readline");
const { chromium } = require("playwright");
const { browserOptions } = require("./browser_options.cjs");

const message = "帮我找出最近点击率下滑最明显、值得暂停复核的一条投放计划。";
const scenarioStates = { normal: "normal", anomaly: "matched", missing: "insufficient" };
const requestPath = response => new URL(response.url()).pathname;
const evidenceFields = ["task_id", "turn_id", "source_trace_id", "plan_id", "rule_id", "baseline", "current",
  "citations", "metrics", "comparison", "calculation", "rule_evaluation", "evidence_gaps", "candidate_scope"];
const candidateIds = [101, 102, 103];

function originOf(child) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("sandbox_start_timeout")), 90000);
    child.once("error", error => { clearTimeout(timer); reject(error); });
    child.once("exit", code => { clearTimeout(timer); reject(new Error("sandbox_exit_" + code)); });
    readline.createInterface({ input: child.stdout }).on("line", line => {
      if (line.startsWith("DEMO_URL=")) {
        clearTimeout(timer);
        resolve(line.slice("DEMO_URL=".length));
      }
    });
  });
}

async function readData(response) {
  assert.equal(response.status(), 200);
  const result = await response.json();
  assert.equal(result.code, 200);
  return result.data;
}

async function runHomepage(page, origin, output, scenario, report) {
  page.setDefaultTimeout(15000);
  const responseFor = (method, pattern) => page.waitForResponse(response =>
    response.request().method() === method && pattern.test(requestPath(response)));
  await page.goto(origin);
  await page.waitForFunction(() => window._captchaId);
  await page.locator("#loginUser").fill("diagnosis-demo");
  await page.locator("#loginPass").fill("demo-password");
  await page.locator("#loginCaptcha").fill("0726");
  const login = responseFor("POST", /^\/api\/login$/);
  await page.locator("#loginCaptcha").press("Enter");
  const auth = { authorization: "Bearer " + (await readData(await login)).token };
  await page.locator("#navUsername").getByText("diagnosis-demo", { exact: true }).waitFor();
  await page.locator('[data-page="chat"]').click();
  await page.getByRole("button", { name: "投放诊断", exact: true }).click();
  await page.locator("#diagnosisHistoryStatus").getByText("暂无历史诊断", { exact: true }).waitFor();
  assert.equal(await page.locator("#diagnosisStructuredFields").isVisible(), false);
  await page.getByRole("textbox", { name: "诊断需求" }).fill(message);
  // Read the same response in the page while its SSE consumer is active.
  await page.evaluate(() => {
    const fetch = window.fetch.bind(window);
    window.task23SseBody = new Promise(resolve => {
      window.fetch = async (...args) => {
        const response = await fetch(...args);
        if (new URL(response.url).pathname === "/api/chat/stream") resolve(response.clone().text());
        return response;
      };
    });
  });
  const sse = page.waitForResponse(response =>
    response.request().method() === "POST" && requestPath(response) === "/api/chat/stream", { timeout: 300000 });
  const started = performance.now();
  await page.locator("#diagnosisSubmit").click();
  const stream = await sse;
  report.request = stream.request().postDataJSON();
  assert.deepEqual(report.request, { message, diagnosis: { mode: "natural" } });
  report.events = (await page.evaluate(() => window.task23SseBody)).split("\n\n")
    .filter(line => line.startsWith("data: ")).map(line => JSON.parse(line.slice(6)));
  report.sse_seconds = (performance.now() - started) / 1000;
  report.trace_id = report.events[0]?.trace_id;
  assert.deepEqual(report.events.map(event => event.type), ["thinking", "text", "done"]);
  const [, evidence, done] = report.events;
  assert.equal(evidence.candidate_revision, undefined);
  assert.deepEqual(evidence.candidate_scope.plan_ids, candidateIds);
  const round = page.locator(".diagnosis-round");
  await round.getByText("已提交", { exact: true }).waitFor();
  assert.equal(done.pause_proposal, undefined);
  assert.equal(await round.locator(".diagnosis-pause").isVisible(), false);
  if (scenario !== "anomaly") {
    assert.equal(done.review_proposal, undefined);
    assert.equal(await round.locator(".diagnosis-review").isVisible(), false);
    if (scenario === "missing") {
      assert.ok(evidence.evidence_gaps.length > 0);
      await round.locator(".diagnosis-gaps").getByText(/缺报/).first().waitFor();
    } else {
      assert.equal(evidence.rule_evaluation.matched, false);
    }
    await round.screenshot({ path: path.join(output, "result.png") });
    return;
  }
  assert.equal(evidence.comparison.candidates[0].plan_id, 103);
  assert.equal(evidence.rule_evaluation.matched, true);
  const evidenceText = await round.locator(".diagnosis-evidence").textContent();
  for (const id of candidateIds) assert.ok(evidenceText.includes("计划 #" + id));
  const ruleResponse = await page.request.get(origin + evidence.citations[0].url, { headers: auth });
  assert.equal(ruleResponse.status(), 200);
  const citation = await ruleResponse.json();
  assert.equal(citation.content, evidence.rule_evaluation.original);
  report.rule_get = citation;

  const review = round.locator(".diagnosis-review");
  const approveReview = review.getByRole("button", { name: "确认创建本地复核单" });
  await approveReview.waitFor();
  await round.screenshot({ path: path.join(output, "pending.png") });
  const reviewPost = responseFor("POST", /^\/api\/diagnosis\/review-proposals\/[^/]+\/confirm$/);
  const reviewGet = responseFor("GET", /^\/api\/diagnosis\/reviews\/[^/]+$/);
  await approveReview.click();
  report.review_post = await readData(await reviewPost);
  const getResponse = await reviewGet;
  report.review_get_path = requestPath(getResponse);
  report.review_get = await readData(getResponse);
  assert.deepEqual(report.review_get, report.review_post);
  for (const key of evidenceFields) assert.deepEqual(report.review_get[key], done.review_proposal[key]);
  await review.getByText("已保存的人工复核单", { exact: true }).waitFor();

  await round.screenshot({ path: path.join(output, "confirmed.png") });
}

function verifyWireAndDatabase(scenario, page, server, traces) {
  assert.equal(server.failure, undefined);
  const requests = server.sdk_requests;
  assert.ok(requests.length > 1);
  assert.ok(requests.every(row => row.status === 200 && row.model === server.model && row.response_model));
  const callName = call => call.function.name;
  const metricTool = "get_plan_metrics_by_date";
  const ruleTool = "search_knowledge";
  const calls = requests.flatMap(row => row.response_calls);
  assert.deepEqual([...new Set(requests.flatMap(row => row.tools.map(callName)))].sort(), [metricTool, ruleTool]);
  assert.ok(requests.length <= 6);
  const evidence = page.events[1];
  const state = evidence.rule_evaluation ? (evidence.rule_evaluation.matched ? "matched" : "normal") : "insufficient";
  assert.equal(state, scenarioStates[scenario]);
  assert.equal(server.fixed_rule.state, state);
  const trace = traces.find(item => item.trace_id === page.trace_id);
  assert.equal(trace.status, "completed");
  const dispatches = trace.steps.filter(step => step.stage === "dispatch");
  assert.deepEqual(dispatches.map(step => step.role), ["data_agent", "knowledge_agent"]);
  const executionCalls = trace.steps.filter(step => step.stage === "tool_call");
  const results = trace.steps.filter(step => step.stage === "tool_result");
  assert.deepEqual(executionCalls.map(step => step.runtime_call_id).sort(), results.map(step => step.runtime_call_id).sort());
  assert.equal(new Set(executionCalls.map(step => step.runtime_call_id)).size, executionCalls.length);
  assert.ok(executionCalls.length <= 12);
  const sdkEvidence = {};
  for (const dispatch of dispatches) {
    const tool = dispatch.action;
    const roleRequests = requests.filter(row => row.tools.length === 1 && callName(row.tools[0]) === tool);
    assert.ok(roleRequests.length >= 1 && roleRequests.length <= 3);
    assert.deepEqual(roleRequests[0].input_messages.map(row => row.role), ["system", "user"]);
    const { required_queries: tasks } = JSON.parse(roleRequests[0].input_messages[1].content);
    assert.ok(roleRequests.every(row => row.tool_choice === "auto"));
    const issued = roleRequests.flatMap(row => row.response_calls).filter(call => callName(call) === tool)
      .map(call => JSON.parse(call.function.arguments));
    assert.deepEqual(issued.map(JSON.stringify).sort(), tasks.map(JSON.stringify).sort());
    const received = trace.steps.filter(step => step.stage === "evidence_received" &&
      step.parent_call_id === dispatch.runtime_call_id);
    assert.equal(received.length, tasks.length);
    for (const receipt of received) {
      const execution = executionCalls.find(step => step.runtime_call_id === receipt.runtime_call_id);
      assert.equal(execution.parent_call_id, dispatch.runtime_call_id);
      assert.equal(execution.tool_call_id, receipt.tool_call_id);
      assert.equal(execution.action, tool);
    }
    sdkEvidence[tool] = issued;
  }
  const metricReceipts = sdkEvidence[metricTool];
  assert.equal(metricReceipts.length, 2 * candidateIds.length);
  assert.deepEqual([...new Set(metricReceipts.map(row => row.plan_id))].sort(), candidateIds);
  assert.deepEqual(sdkEvidence[ruleTool], [{ query: 'ctr-review-v1#low-ctr' }]);
  assert.deepEqual(server.db_start.pause_registrations.map(row => row.plan_id), [101, 102, 201]);
  const missingDates = scenario === "missing" ? [server.injected_omission.date] : [];
  const retries = trace.steps.filter(step => step.stage === "retry");
  if (server.injected_failure) {
    const { arguments: args, attempts } = server.injected_failure;
    assert.deepEqual(attempts, [args, args]);
    const failed = results.find(row => row.error_code === "temporary_tool_failure");
    assert.ok(failed);
    assert.equal(retries.length, 1);
    assert.equal(retries[0].retry_of, failed.runtime_call_id);
    const retried = results.find(row => row.retry_of === failed.runtime_call_id);
    assert.equal(retried.parent_call_id, failed.parent_call_id);
    assert.equal(retried.status, "ok");
    assert.equal(executionCalls.filter(row => row.action === metricTool).length, metricReceipts.length + 1);
  } else {
    assert.equal(retries.length, 0);
  }
  if (scenario === "missing") {
    const missing = server.injected_omission;
    assert.deepEqual(missingDates, [missing.date]);
    assert.ok(evidence.evidence_gaps.some(gap => gap.includes("#" + missing.plan_id) && gap.includes(missing.date)));
    assert.ok(!executionCalls.some(step => step.action === "submit_comparison"));
  } else {
    assert.ok(executionCalls.some(step => step.action === "submit_comparison" && step.role === "orchestrator"));
    assert.deepEqual(evidence.comparison.candidates.map(item => item.plan_id),
      server.fixed_rule.ranking.map(item => item.plan_id));
    for (const item of evidence.comparison.candidates) {
      const { baseline: base, current } = item.metrics;
      const baseCtr = base.clicks / base.impressions;
      const currentCtr = current.clicks / current.impressions;
      assert.ok(Math.abs(item.calculation.relative_decline_pct - (baseCtr - currentCtr) / baseCtr * 100) < 1e-10);
      const fixed = server.fixed_rule.ranking.find(entry => entry.plan_id === item.plan_id);
      if (base.start_date === fixed.baseline.start_date && current.end_date === fixed.current.end_date) {
        for (const key of ["clicks", "impressions"]) {
          assert.equal(base[key], fixed.baseline[key]);
          assert.equal(current[key], fixed.current[key]);
        }
      }
    }
  }
  if (scenario === "anomaly") {
    assert.equal(server.fixed_rule.eligible_plan_id, 103);
    assert.equal(page.review_get.plan_id, 103);
    const [review] = server.approval_snapshots;
    assert.equal(server.approval_snapshots.length, 1);
    assert.equal(review.status, 200);
    assert.deepEqual(review.before.plans, server.db_start.plans);
    assert.deepEqual(review.before.counts, { ...server.db_start.counts, diagnosis_review_proposals: 1 });
    assert.deepEqual(review.after, { ...review.before, counts: { ...review.before.counts, diagnosis_reviews: 1 } });
    assert.deepEqual(server.db_end, review.after);
  } else {
    assert.deepEqual(server.db_end, server.db_start);
    assert.deepEqual(server.approval_snapshots, []);
  }
  return {
    state, model: server.model, sdk_requests: requests.length, trace_id: trace.trace_id,
    tools: Object.fromEntries([...new Set(calls.map(callName))].map(name =>
      [name, calls.filter(call => callName(call) === name).length])),
    business_tools: executionCalls.length,
    sdk_seconds: requests.reduce((sum, row) => sum + row.elapsed_seconds, 0),
    trace_seconds: trace.total_duration,
    tokens: requests.every(row => row.usage?.total_tokens !== undefined)
      ? requests.reduce((sum, row) => sum + row.usage.total_tokens, 0) : undefined,
    unresolved_missing_dates: [...new Set(missingDates)],
  };
}

async function runScenario(browser, root, artifact, scenario, fault) {
  const output = await fs.mkdtemp(path.join(root, scenario + "-"));
  const child = spawn(process.env.PYTHON || "python", [
    "-B", "-X", "utf8", "-m", "scripts.run_natural_diagnosis", "--serve", scenario,
    "--output-dir", output, "--plan-metrics-db", artifact, ...(fault ? [fault] : []),
  ], { windowsHide: true, stdio: ["pipe", "pipe", "ignore"] });
  const report = { scenario, output, ok: false };
  let page;
  try {
    const origin = await originOf(child);
    page = await browser.newPage({ viewport: { width: 1360, height: 1200 } });
    await runHomepage(page, origin, output, scenario, report);
  } catch (error) {
    report.homepage_failure = error.message;
  } finally {
    await page?.close();
    if (child.pid && child.exitCode === null && child.signalCode === null) {
      const exited = once(child, "exit");
      child.stdin.end();
      const timer = setTimeout(() => child.kill(), 35000);
      try { await exited; } finally { clearTimeout(timer); }
    }
  }
  try {
    const server = JSON.parse(await fs.readFile(path.join(output, "server.json"), "utf8"));
    const traces = await fs.readFile(path.join(output, "traces.jsonl"), "utf8").then(
      text => text.trim().split(/\r?\n/).map(line => JSON.parse(line)), () => []);
    for (const row of server.sdk_requests) row.trace_id = report.trace_id;
    await fs.writeFile(path.join(output, "server.json"), JSON.stringify(server, null, 2) + "\n");
    report.provider = server.provider;
    report.model = server.model;
    report.sdk_requests = server.sdk_requests.length;
    report.provider_failure = server.failure || server.sdk_requests.find(row => row.failure_reason)?.failure_reason;
    report.fixed_rule = server.fixed_rule;
    if (!report.homepage_failure && !report.provider_failure) {
      report.agent = verifyWireAndDatabase(scenario, report, server, traces);
      report.ok = true;
    }
  } catch (error) {
    report.verification_failure = error.message;
  }
  await fs.writeFile(path.join(output, "homepage.json"), JSON.stringify(report, null, 2) + "\n");
  console.log(`${scenario}: ${report.ok ? "PASS" : "FAIL"} · ${report.model || "unknown"} · ${report.sdk_requests ?? 0} SDK · ${report.trace_id || "no trace"}`);
  return report;
}

async function main() {
  const [, , output, artifact, only, fault] = process.argv;
  const browser = await chromium.launch(browserOptions());
  const reports = [];
  try {
    for (const scenario of only ? [only] : Object.keys(scenarioStates)) {
      reports.push(await runScenario(browser, output, artifact, scenario, fault));
    }
  } finally {
    await browser.close();
    const summary = { ok: reports.length === (only ? 1 : 3) && reports.every(report => report.ok),
      browser: browser.version(), sdk_requests: reports.reduce((sum, report) => sum + (report.sdk_requests ?? 0), 0),
      scenarios: reports.map(({ events, ...report }) => report) };
    await fs.writeFile(path.join(output, "report.json"), JSON.stringify(summary, null, 2) + "\n");
    process.exitCode = summary.ok ? 0 : 1;
  }
}

main().catch(error => { console.error(error.message); process.exitCode = 1; });
