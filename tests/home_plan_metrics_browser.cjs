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
    const page = await browser.newPage({ viewport: { width: 1360, height: 1050 } });
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
    await page.getByRole("button", { name: "投放诊断", exact: true }).click();
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
      assert.match(await round.locator(".diagnosis-review-evidence").textContent(), /CTR 基准/);
      const citation = round.locator(".diagnosis-citations summary").first();
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
