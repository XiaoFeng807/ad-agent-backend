// Public aggregate + real homepage/ASGI; model and embedding remain fixed.
const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const { once } = require("node:events");
const { randomBytes } = require("node:crypto");
const fs = require("node:fs/promises");
const path = require("node:path");
const readline = require("node:readline");
const { chromium } = require("playwright");
const { browserOptions } = require("./browser_options.cjs");

async function run() {
  const output = path.resolve(process.argv[2] || "logs/task12-browser");
  await fs.mkdir(output, { recursive: true });
  const directory = await fs.mkdtemp(path.join(output, "demo-"));
  const child = spawn(process.env.PYTHON || "python", ["-B", "-X", "utf8", "-m", "tests.home_diagnosis_server", directory], {
    windowsHide: true, stdio: ["ignore", "pipe", "inherit"],
    env: { ...process.env, JWT_SECRET: randomBytes(32).toString("hex"),
      PLAN_METRICS_DEMO_ARTIFACT: path.resolve(process.env.PLAN_METRICS_DEMO_ARTIFACT || "data/plan_metrics/criteo.sqlite3") },
  });
  let browser;
  const report = { kind: "public data; fixed model and embedding", cases: [], rounds: [] };
  try {
    const origin = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("server startup timeout")), 20000);
      readline.createInterface({ input: child.stdout }).on("line", line => {
        if (line.startsWith("DEMO_URL=")) { clearTimeout(timer); resolve(line.slice(9)); }
      });
      child.once("exit", code => { clearTimeout(timer); reject(new Error(`server exit ${code}`)); });
    });
    browser = await chromium.launch(browserOptions());
    console.log("AD_BROWSER " + browser.version());
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 },
      permissions: ["clipboard-read", "clipboard-write"] });
    page.setDefaultTimeout(10000);
    report.current_case = "homepage shows public numbers, source, time mapping and exact rule citation in both windows";
    await page.goto(origin);
    await page.waitForFunction(() => window._captchaId);
    await page.locator("#loginUser").fill("diagnosis-demo");
    await page.locator("#loginPass").fill("demo-password");
    await page.locator("#loginCaptcha").fill("0726");
    await page.locator("#loginCaptcha").press("Enter");
    await page.locator("#navUsername").getByText("diagnosis-demo", { exact: true }).waitFor();
    await page.locator('[data-page="chat"]').click();
    await page.getByRole("button", { name: "广告计划复核", exact: true }).click();
    await page.getByRole("button", { name: "使用示例" }).click();
    assert.equal(await page.locator('#diagnosisForm input[name="message"]').evaluate(input =>
      input.value === input.placeholder && Boolean(input.value)), true);
    await page.screenshot({ path: path.join(output, "advertising-review-setup.png") });
    await page.locator("#diagnosisMode").click();
    const field = name => page.locator(`#diagnosisForm input[name="${name}"]`);
    await field("plan_id").fill("101");
    await field("rule_id").fill("ctr-review-v1#low-ctr");
    const windows = [
      { baseline_start: "2026-10-15", baseline_end: "2026-10-21", current_start: "2026-10-22", current_end: "2026-10-28" },
      { baseline_start: "2026-10-19", baseline_end: "2026-10-21", current_start: "2026-10-26", current_end: "2026-10-28" },
    ];
    for (let index = 0; index < 2; index++) {
      for (const [name, value] of Object.entries(windows[index])) await field(name).fill(value);
      let events;
      await page.route("**/api/chat/stream", async route => {
        const response = await route.fetch();
        events = (await response.text()).split(/\r?\n/).filter(line => line.startsWith("data: ")).map(line => JSON.parse(line.slice(6)));
        await route.fulfill({ response });
      }, { times: 1 });
      await page.locator("#diagnosisSubmit").click();
      const round = page.locator(".diagnosis-round").nth(index);
      await round.locator('.diagnosis-status[data-state="done"]').waitFor();
      const proposal = events.at(-1).review_proposal;
      const text = await round.locator(".diagnosis-reply").textContent();
      assert.match(text, /criteo-attribution-2017/);
      assert.match(text, /相对降幅/);
      assert.match(text, /非真实日历日期/);
      assert.match(text, /公开数据/);
      assert.deepEqual(proposal.evidence_gaps, []);
      const expected = index === 0 ? [7544, 2835, 9723, 2784] : [3452, 1323, 4821, 1214];
      assert.deepEqual([proposal.metrics.baseline.impressions, proposal.metrics.baseline.clicks,
        proposal.metrics.current.impressions, proposal.metrics.current.clicks], expected);
      const comparison = round.locator(".diagnosis-comparison");
      const values = [expected[1] / expected[0] * 100, expected[3] / expected[2] * 100];
      assert.deepEqual(await comparison.locator(".diagnosis-ctr-pair strong").allTextContents(),
        values.map(value => value.toFixed(2) + "%"));
      const normalizedWidths = await comparison.locator(".diagnosis-ctr-track").evaluateAll(tracks =>
        tracks.map(track => track.firstElementChild.getBoundingClientRect().width / track.getBoundingClientRect().width));
      const max = Math.max(1, Math.ceil(Math.max(...values)));
      assert.ok(normalizedWidths.every((width, i) => Math.abs(width - values[i] / max) < 0.01));
      if (index === 0) {
        const trigger = round.getByRole("button", { name: "执行记录" });
        await trigger.click();
        const execution = page.locator("#diagnosisExecution");
        assert.deepEqual(await execution.locator("ol li > strong").allTextContents(),
          ["读取两窗指标", "核对点击率规则", "保存复核结果"]);
        assert.equal(await execution.locator("#diagnosisExecutionSummary strong").textContent(), "第 1 轮 · 已提交");
        assert.equal(await execution.locator("#diagnosisExecutionSummary small").textContent(), "计划 #101");
        assert.deepEqual(await execution.locator("ol li").first().locator("small").allTextContents(),
          ["基准窗 · 10 月 15–21 日 · 37.58%", "当前窗 · 10 月 22–28 日 · 28.63%"]);
        assert.equal(await execution.locator("ol li").nth(1).locator("small").textContent(), proposal.rule_id);
        await page.screenshot({ path: path.join(output, "advertising-review-trace.png") });
        await execution.getByRole("button", { name: "复制 Trace ID" }).click();
        await execution.getByText("已复制", { exact: true }).waitFor();
        assert.equal(await page.evaluate(() => navigator.clipboard.readText()), proposal.source_trace_id);
        await page.setViewportSize({ width: 390, height: 844 });
        await page.locator("#diagnosisExecution:modal").waitFor({state: "visible"});
        await page.keyboard.press("Escape");
        await execution.waitFor({state: "hidden"});
        assert.equal(await execution.isVisible(), false);
        assert.equal(await trigger.evaluate(element => document.activeElement === element), true);
        await page.setViewportSize({ width: 1440, height: 1000 });
        report.copied_trace_id = proposal.source_trace_id;
      }
      assert.match(await round.locator(".diagnosis-review-evidence").textContent(), /CTR 基准/);
      const citation = round.locator(".diagnosis-citations summary").first();
      await round.getByText("查看依据", { exact: true }).click();
      await citation.click();
      await page.getByText("两个可比窗口各达到 1,000 次计划曝光", { exact: false }).last().waitFor();
      // The original is shown in the inline citation panel; preserve its evidence.
      report.rounds.push(proposal);
      await round.screenshot({ path: path.join(output, `round-${index + 1}.png`) });
    }
    assert.equal(report.rounds[0].task_id, report.rounds[1].task_id);
    assert.notEqual(report.rounds[0].source_trace_id, report.rounds[1].source_trace_id);
    report.cases.push("homepage shows public numbers, source, time mapping and exact rule citation in both windows");
    report.current_case = "after round two, confirmation and GET preserve round one numbers, windows and citations";
    const panel = page.locator(".diagnosis-round").first().locator(".diagnosis-review");
    const readPromise = page.waitForResponse(response => /\/api\/diagnosis\/reviews\//.test(new URL(response.url()).pathname));
    await panel.getByRole("button", { name: "确认创建本地复核单" }).click();
    const receipt = (await (await readPromise).json()).data;
    for (const [key, value] of Object.entries(report.rounds[0])) assert.deepEqual(receipt[key], value);
    await panel.getByText("已保存的人工复核单", { exact: true }).waitFor();
    assert.match(await panel.textContent(), /7544/);
    assert.match(await panel.textContent(), /9723/);
    report.review = receipt;
    report.cases.push("after round two, confirmation and GET preserve round one numbers, windows and citations");
    report.current_case = "homepage displays missing date coverage explicitly";
    // Exercise missing coverage through the same page without changing the task.
    for (const name of ["current_start", "current_end"]) await field(name).fill("2026-11-01");
    await page.locator("#diagnosisSubmit").click();
    const missing = page.locator(".diagnosis-round").nth(2);
    await missing.locator('.diagnosis-status[data-state="done"]').waitFor();
    assert.match(await missing.locator(".diagnosis-gaps").textContent(), /缺窗\/覆盖不全/);
    assert.equal(await missing.locator(".diagnosis-comparison").isVisible(), false);
    await missing.getByRole("button", { name: "执行记录" }).click();
    assert.deepEqual(await page.locator("#diagnosisExecution ol li > strong").allTextContents(), ["保存复核结果"]);
    await page.keyboard.press("Escape");
    await missing.screenshot({ path: path.join(output, "missing-window.png") });
    report.cases.push("homepage displays missing date coverage explicitly");
    report.current_case = null;
    report.passed = true;
    console.log("3/3 public plan metrics browser cases passed");
  } finally {
    report.executed = report.cases.length + Number(Boolean(report.current_case));
    report.failed = Number(Boolean(report.current_case));
    await browser?.close();
    if (child.exitCode === null) { const exited = once(child, "exit"); child.kill(); await exited; }
    await fs.writeFile(path.join(output, "report.json"), JSON.stringify(report, null, 2) + "\n");
  }
}
run().catch(error => { console.error(error); process.exitCode = 1; });
