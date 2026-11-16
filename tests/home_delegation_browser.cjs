// Exercise the real homepage UI, then verify actual SDK calls and the matching server Trace.
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const {once} = require('node:events');
const fs = require('node:fs/promises');
const path = require('node:path');
const readline = require('node:readline');
const {chromium} = require('playwright');
const {browserOptions} = require('./browser_options.cjs');

const message = '检查 #101 最近的投放表现，有明显异常再查原因';

async function exercise(browser, directory, scenario, options = {}) {
  const question = options.message || message;
  await fs.mkdir(directory);
  const server = spawn(process.env.PYTHON || 'python',
    ['-B', '-X', 'utf8', '-m', options.module || 'scripts.run_home_delegation', directory, scenario],
    {windowsHide: true, stdio: ['pipe', 'pipe', 'ignore']});
  const result = {scenario, message: question, browser: browser.version()};
  let page;
  try {
    const origin = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(Error('server startup timeout')), 60000);
      server.once('error', error => { clearTimeout(timer); reject(error); });
      server.once('exit', code => { clearTimeout(timer); reject(Error('server exit ' + code)); });
      readline.createInterface({input: server.stdout}).on('line', line => {
        if (line.startsWith('DEMO_URL=')) { clearTimeout(timer); resolve(line.slice(9)); }
      });
    });
    page = await browser.newPage();
    page.setDefaultTimeout(110000);
    await page.goto(origin);
    await page.waitForFunction(() => window._captchaId);
    await page.locator('#loginUser').fill('delegation-demo');
    await page.locator('#loginPass').fill('demo-password');
    await page.locator('#loginCaptcha').fill('0726');
    await page.locator('#loginCaptcha').press('Enter');
    await page.locator('#navUsername').getByText('delegation-demo', {exact: true}).waitFor();
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
    await page.locator('#chatInput').fill(question);
    await page.locator('#chatInput').press('Enter');
    const stream = await response;
    assert.equal(stream.status(), 200);
    const body = stream.request().postDataJSON();
    assert.equal(body.message, question);
    assert.equal(body.diagnosis, undefined);
    await page.waitForFunction(() => window.acceptanceEvents.some(event => ['done', 'error'].includes(event.type)));
    result.events = await page.evaluate(() => window.acceptanceEvents);
    result.elapsed_seconds = Number(((performance.now() - started) / 1000).toFixed(3));
    assert.deepEqual(result.events.map(event => event.type), ['thinking', 'text', 'done']);
    const reply = result.events[1].content;
    assert.ok(reply.trim());
    await page.locator('#chatMessages .msg.assistant').last().getByText(/101/).first().waitFor();
  } catch (error) {
    result.failure = error.message;
  } finally {
    await page?.close();
    if (server.exitCode === null && server.signalCode === null) {
      const exited = once(server, 'exit');
      server.stdin.end();
      const timer = setTimeout(() => server.kill(), 35000);
      try { await exited; } finally { clearTimeout(timer); }
    }
  }
  try {
    const serverReport = JSON.parse(await fs.readFile(path.join(directory, 'server.json'), 'utf8'));
    if (options.verify) options.verify(result, serverReport);
    else if (!result.failure) verify(result, serverReport);
  } catch (error) {
    result.failure ||= error.message;
  }
  await fs.writeFile(path.join(directory, 'browser.json'), JSON.stringify(result, null, 2) + '\n');
  return result;
}

function verify(result, server) {
  const traceId = result.events.at(-1).trace_id;
  const trace = server.traces.find(row => row.trace_id === traceId);
  assert.equal(trace.status, 'completed');
  assert.deepEqual(server.committed_replies, [result.events[1].content]);
  assert.ok(server.sdk_requests.every(row => row.status === 200));
  const requests = server.sdk_requests;
  const main = requests.filter(row => row.tools?.some(tool => tool.function.name === 'delegate_tasks'));
  const firstCall = main[0].response_calls[0];
  assert.equal(firstCall.function.name, 'get_plan_detail');
  assert.deepEqual(JSON.parse(firstCall.function.arguments), {plan_id: 101});
  assert.equal(main[0].response_calls.length, 1);
  const receipt = main[1].tool_receipts.find(row => row.tool_call_id === firstCall.id);
  const firstReceipt = JSON.parse(receipt.content);
  assert.equal(firstReceipt.result.clicks, result.scenario === 'normal' ? 120 : 1);
  const delegated = main.flatMap(row => row.response_calls.filter(call => call.function.name === 'delegate_tasks'));
  const created = trace.steps.filter(step => step.stage === 'worker' && step.action === 'created');
  const reclaimed = trace.steps.filter(step => step.stage === 'worker' && step.action === 'reclaimed');
  assert.deepEqual(created.map(step => step.runtime_call_id).sort(), reclaimed.map(step => step.runtime_call_id).sort());
  if (result.scenario === 'normal') {
    assert.equal(delegated.length, 0);
    assert.equal(created.length, 0);
    assert.equal(main.flatMap(row => row.response_calls).length, 1);
  } else {
    assert.ok(delegated.length > 0 && created.length > 0);
    const firstIndex = trace.steps.findIndex(step => step.stage === 'tool_result' && step.tool_call_id === firstCall.id);
    const delegateIndex = trace.steps.findIndex(step => step.stage === 'tool_call' && step.tool_call_id === delegated[0].id);
    assert.ok(firstIndex >= 0 && firstIndex < delegateIndex);
    assert.ok(requests.some(row => row.tool_receipts.some(item => item.tool_call_id === delegated[0].id)));
    assert.ok(trace.steps.some(step => step.stage === 'tool_call' && step.role === 'team_worker'));
    assert.ok(trace.steps.some(step => step.stage === 'tool_result' && step.role === 'team_worker' && step.status === 'error'));
    assert.match(result.events[1].content, /(?:无法|不能).{0,15}(?:确认|判断|定位|确定|断定)/);
  }
  const mainContinuations = requests.filter(row => row.assistant_calls.some(call => call.id === firstCall.id));
  const delegationHandoffs = delegated.map(call => {
    const row = mainContinuations.find(request => request.tool_receipts.some(item => item.tool_call_id === call.id));
    const receipt = row.tool_receipts.find(item => item.tool_call_id === call.id);
    return {tool_call_id: call.id, ...JSON.parse(receipt.content)};
  });
  const toolCalls = trace.steps.filter(step => step.stage === 'tool_call');
  const actualCalls = requests.flatMap(row => row.response_calls);
  for (const step of toolCalls.filter(step => step.role === 'team_worker' && !step.retry_of)) {
    assert.ok(actualCalls.some(call => call.id === step.tool_call_id && call.function.name === step.action));
  }
  Object.assign(result, {trace_id: traceId, first_call: firstCall, first_receipt: firstReceipt,
    task_sheets: delegated.map(call => ({id: call.id, ...JSON.parse(call.function.arguments)})),
    delegation_handoffs: delegationHandoffs,
    workers_created: created, workers_reclaimed: reclaimed,
    sdk_count: requests.length, tool_calls: toolCalls,
    after_first_receipt: mainContinuations.map(row => row.response_calls),
    final_reply: result.events[1].content, ok: true});
}

async function main() {
  const root = path.resolve(process.argv[2] || 'logs/task26-r2/real');
  await fs.mkdir(root, {recursive: true});
  const directory = await fs.mkdtemp(path.join(root, 'run-'));
  const browser = await chromium.launch(browserOptions());
  try {
    const cases = [];
    for (const scenario of ['normal', 'anomaly']) {
      cases.push(await exercise(browser, path.join(directory, scenario), scenario));
    }
    if (cases.every(row => row.ok)) {
      const [normal, anomaly] = cases.map(row => row.first_receipt.result);
      const differences = Object.keys(normal).filter(key => normal[key] !== anomaly[key]);
      assert.deepEqual(differences.sort(), ['clicks', 'cpc', 'ctr', 'roas', 'sales']);
    }
    await fs.writeFile(path.join(directory, 'report.json'), JSON.stringify(cases, null, 2) + '\n');
    console.log(JSON.stringify({directory, cases: cases.map(({scenario, trace_id, ok, failure}) =>
      ({scenario, trace_id, ok, failure}))}));
    process.exitCode = cases.every(row => row.ok) ? 0 : 1;
  } finally {
    await browser.close();
  }
}
module.exports = {exercise};
if (require.main === module) {
  main().catch(error => { console.error(error.message); process.exitCode = 1; });
}
