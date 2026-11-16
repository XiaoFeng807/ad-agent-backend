// Exercise frozen cases through the actual Chromium homepage, login and ordinary-chat SSE.
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const {once} = require('node:events');
const fs = require('node:fs/promises');
const path = require('node:path');
const readline = require('node:readline');
const {chromium} = require('playwright');
const {browserOptions} = require('./browser_options.cjs');
const samplePath = process.env.AD_TASK26_SAMPLE && path.resolve(process.env.AD_TASK26_SAMPLE);
const sample = require(samplePath || '../docs/task26-t2-samples.json');

async function exercise(browser, origin, caseRow, directory) {
  const result = {id: caseRow.id, browser: browser.version()};
  const page = await browser.newPage();
  page.setDefaultTimeout(110000);
  try {
    await page.goto(origin);
    await page.waitForFunction(() => window._captchaId);
    await page.locator('#loginUser').fill(caseRow.id.toLowerCase());
    await page.locator('#loginPass').fill('demo-password');
    await page.locator('#loginCaptcha').fill('0726');
    await page.locator('#loginCaptcha').press('Enter');
    await page.locator('#navUsername').getByText(caseRow.id.toLowerCase(), {exact: true}).waitFor();
    await page.locator('[data-page="chat"]').click();
    await page.evaluate(() => {
      const read = window.readChatStream;
      window.acceptanceEvents = [];
      window.readChatStream = (payload, token, onEvent, signal) => read(payload, token, event => {
        window.acceptanceEvents.push(event);
        onEvent(event);
      }, signal);
    });
    const response = page.waitForResponse(r => r.request().method() === 'POST' &&
      new URL(r.url()).pathname === '/api/chat/stream');
    const started = performance.now();
    await page.locator('#chatInput').fill(caseRow.message);
    await page.locator('#chatInput').press('Enter');
    const stream = await response;
    result.http_status = stream.status();
    const body = stream.request().postDataJSON();
    assert.equal(body.message, caseRow.message);
    assert.equal(body.diagnosis, undefined);
    await page.waitForFunction(() => window.acceptanceEvents.some(event => ['done', 'error'].includes(event.type)));
    result.events = await page.evaluate(() => window.acceptanceEvents);
    result.elapsed_seconds = Number(((performance.now() - started) / 1000).toFixed(3));
    assert.equal(result.http_status, 200);
    assert.equal(result.events.at(-1).type, 'done');
    const text = result.events.filter(event => event.type === 'text').map(event => event.content).join('');
    assert.ok(text.trim());
    const bubble = page.locator('#chatMessages .msg.assistant').last();
    await bubble.waitFor();
    result.rendered = await bubble.innerText();
    assert.ok(result.rendered.trim());
  } catch (error) {
    result.failure = String(error.message).slice(0, 1000);
  } finally {
    await page.close();
    const folder = path.join(directory, caseRow.id);
    await fs.mkdir(folder, {recursive: true});
    await fs.writeFile(path.join(folder, 'browser.json'), JSON.stringify(result, null, 2) + '\n');
    console.log(JSON.stringify({case: result.id, browser: result.browser, failure: result.failure}));
  }
}

async function main() {
  const [ids, target, ledger] = process.argv.slice(2);
  assert.ok(ids && target, 'usage: node tests/task26_t2_browser.cjs formal-browser|smoke-browser|T2-001[,T2-002] output [ledger]');
  const directory = path.resolve(target);
  const requested = ids === 'smoke-browser' ? ['T2-001'] : ids.split(',');
  const cases = sample.cases.filter(row => ids === 'formal-browser' ? row.transport === 'chromium' : requested.includes(row.id));
  assert.ok(cases.length);
  const args = ['-B', '-X', 'utf8', '-m', 'scripts.run_task26_t2', 'chromium', ids, directory];
  if (ledger) args.push('--ledger', ledger);
  if (samplePath) args.push('--sample', samplePath);
  const server = spawn(process.env.PYTHON || 'python', args, {windowsHide: true, stdio: ['pipe', 'pipe', 'inherit']});
  let browser;
  try {
    const origin = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(Error('server startup timeout')), 90000);
      server.once('error', error => { clearTimeout(timer); reject(error); });
      server.once('exit', code => { clearTimeout(timer); reject(Error('server exit ' + code)); });
      readline.createInterface({input: server.stdout}).on('line', line => {
        if (line.startsWith('DEMO_URL=')) {
          clearTimeout(timer);
          resolve(line.slice(9));
        } else {
          console.log(line);
        }
      });
    });
    browser = await chromium.launch(browserOptions());
    for (const row of cases) await exercise(browser, origin, row, directory);
  } finally {
    await browser?.close();
    if (server.exitCode === null && server.signalCode === null) {
      const exited = once(server, 'exit');
      server.stdin.end();
      const timer = setTimeout(() => server.kill(), 35000);
      try { await exited; } finally { clearTimeout(timer); }
    }
  }
}

main().catch(error => { console.error(error.message); process.exitCode = 1; });
