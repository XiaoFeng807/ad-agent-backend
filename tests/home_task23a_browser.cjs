// Real Chromium and DeepSeek; the server archives allowlisted SDK wire data only.
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const { once } = require('node:events');
const fs = require('node:fs/promises');
const path = require('node:path');
const readline = require('node:readline');
const { chromium } = require('playwright');
const { browserOptions } = require('./browser_options.cjs');
const cases = {
  default: '帮我找出最近点击率下滑最明显、值得暂停复核的一条投放计划。',
  three: '帮我找出最近三天和之前三天点击率下滑最明显、值得暂停复核的一条投放计划。',
  unrelated: '帮我写一封请假邮件',
};
async function run(browser, root, artifact, name, message) {
  const dir = await fs.mkdtemp(path.join(root, name + '-'));
  const child = spawn(process.env.PYTHON || 'python', ['-B', '-X', 'utf8', '-m', 'scripts.run_natural_diagnosis',
    '--serve', 'anomaly', '--output-dir', dir, '--plan-metrics-db', artifact],
    { windowsHide: true, stdio: ['pipe', 'pipe', 'ignore'] });
  const report = { name, dir, ok: false };
  let page;
  try {
    const origin = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(Error('server timeout')), 90000);
      child.once('exit', code => { clearTimeout(timer); reject(Error('server exit ' + code)); });
      readline.createInterface({ input: child.stdout }).on('line', line => {
        if (line.startsWith('DEMO_URL=')) { clearTimeout(timer); resolve(line.slice(9)); }
      });
    });
    page = await browser.newPage();
    page.setDefaultTimeout(30000);
    await page.goto(origin);
    await page.waitForFunction(() => window._captchaId);
    await page.locator('#loginUser').fill('diagnosis-demo');
    await page.locator('#loginPass').fill('demo-password');
    await page.locator('#loginCaptcha').fill('0726');
    await page.locator('#loginCaptcha').press('Enter');
    await page.locator('#navUsername').getByText('diagnosis-demo', { exact: true }).waitFor();
    await page.locator('[data-page="chat"]').click();
    await page.getByRole('button', { name: '广告计划复核', exact: true }).click();
    await page.getByRole('textbox', { name: '诊断需求' }).fill(message);
    await page.evaluate(() => {
      const original = window.fetch.bind(window);
      window.captured = new Promise(resolve => { window.fetch = async (...args) => {
        const response = await original(...args);
        if (new URL(response.url).pathname === '/api/chat/stream') resolve(response.clone().text());
        return response;
      }; });
    });
    await page.locator('#diagnosisSubmit').click();
    report.events = (await page.evaluate(() => window.captured)).split('\n\n').filter(x => x.startsWith('data: ')).map(x => JSON.parse(x.slice(6)));
    assert.deepEqual(report.events.map(x => x.type), ['thinking', 'text', 'done']);
    report.trace_id = report.events[0].trace_id;
    if (name === 'unrelated') {
      assert.ok(report.events[1].evidence_gaps.length);
      assert.equal(report.events[2].review_proposal, undefined);
    } else {
      assert.ok(report.events[1].comparison);
      if (name === 'default') {
        assert.ok(report.events[2].review_proposal);
        const wait = (method, regex) => page.waitForResponse(r => r.request().method() === method && regex.test(new URL(r.url()).pathname));
        const post = wait('POST', /\/review-proposals\/[^/]+\/confirm$/);
        const get = wait('GET', /\/diagnosis\/reviews\/[^/]+$/);
        await page.getByRole('button', { name: '确认创建本地复核单' }).click();
        const p = await post, g = await get;
        assert.equal(p.status(), 200); assert.equal(g.status(), 200);
        report.review_post = (await p.json()).data;
        report.review_get_path = new URL(g.url()).pathname;
        report.review_get = (await g.json()).data;
        assert.deepEqual(report.review_get, report.review_post);
      }
    }
  } catch (error) { report.failure = error.message; }
  finally {
    await page?.close();
    if (child.exitCode === null && child.signalCode === null) {
      const exited = once(child, 'exit'); child.stdin.end();
      const timer = setTimeout(() => child.kill(), 35000);
      try { await exited; } finally { clearTimeout(timer); }
    }
  }
  try {
    const server = JSON.parse(await fs.readFile(path.join(dir, 'server.json'), 'utf8'));
    const traces = (await fs.readFile(path.join(dir, 'traces.jsonl'), 'utf8')).trim().split(/\r?\n/).map(JSON.parse);
    const trace = traces.find(x => x.trace_id === report.trace_id);
    assert.ok(!report.failure && !server.failure && trace);
    const calls = trace.steps.filter(x => x.stage === 'tool_call');
    report.sdk_requests = server.sdk_requests.length;
    report.business_calls = calls.length;
    assert.ok(server.sdk_requests.every(x => x.status === 200 && x.tool_choice !== 'none'));
    if (name === 'unrelated') { assert.equal(calls.length, 0); assert.equal(report.sdk_requests, 0); }
    else {
      assert.ok(report.sdk_requests >= 2 && report.sdk_requests <= 6);
      const tasks = server.sdk_requests.filter(x => x.tools[0].function.name === 'get_plan_metrics_by_date')
        .flatMap(x => JSON.parse(x.input_messages[1].content).required_queries);
      const expected = name === 'three' ? [['2026-10-25','2026-10-27'], ['2026-10-28','2026-10-30']]
        : [['2026-10-17','2026-10-23'], ['2026-10-24','2026-10-30']];
      assert.deepEqual([...new Set(tasks.map(x => x.start_date + '/' + x.end_date))].sort(), expected.map(x => x.join('/')).sort());
      const issued = server.sdk_requests.flatMap(x => x.response_calls).filter(x => x.function.name === 'get_plan_metrics_by_date')
        .map(x => JSON.parse(x.function.arguments));
      const canonical = rows => rows.map(x => `${x.plan_id}/${x.start_date}/${x.end_date}`).sort();
      assert.deepEqual(canonical(issued), canonical(tasks.slice(0, 6)));
      report.queries = tasks;
    }
    report.ok = true;
  } catch (error) { report.failure ||= error.message; }
  await fs.writeFile(path.join(dir, 'task23a.json'), JSON.stringify(report, null, 2) + '\n');
  console.log(`${name}: ${report.ok ? 'PASS' : 'FAIL'} ${report.trace_id || ''} SDK=${report.sdk_requests ?? '?'} ${report.failure || ''}`);
  return report;
}
async function main() {
  const [, , root, artifact] = process.argv;
  await fs.mkdir(root, { recursive: true });
  const browser = await chromium.launch(browserOptions());
  const reports = [];
  try { for (const [name, message] of Object.entries(cases)) reports.push(await run(browser, root, artifact, name, message)); }
  finally { await browser.close(); }
  await fs.writeFile(path.join(root, 'report.json'), JSON.stringify({ browser: browser.version(), reports }, null, 2) + '\n');
  process.exitCode = reports.every(x => x.ok) ? 0 : 1;
}
main().catch(error => { console.error(error.message); process.exitCode = 1; });
