// Real Chromium, ordinary-chat SSE, real DeepSeek. Failures stay outside the baseline.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const {chromium} = require('playwright');
const {browserOptions} = require('./browser_options.cjs');
const {exercise} = require('./home_delegation_browser.cjs');

const prefix = '先检查 #101 的投放快照，只有出现明显异常才进一步查原因。需要深入查时，分别核实两条线索：';
const suffix = '从知识库核对 v1 点击率下降复核规则和样本量要求。最后给出证据、引用和仍无法确认的原因。';
const implicit = prefix + '从 2026-10-17 至 2026-10-30 的计划历史找 CTR 变化并核对覆盖，' + suffix;
const explicit = prefix +
  '分别核对 2026-10-17 至 2026-10-23 和 2026-10-24 至 2026-10-30 两窗的计划曝光、点击和覆盖，计算 CTR 相对变化，' + suffix;
const shifted = implicit.replace('2026-10-17 至 2026-10-30', '2026-10-16 至 2026-10-29') +
  '如触发深入核查，第一批的历史 Worker 只查询一次整窗覆盖并交回。收到该回执后，请你自行选窗，决定直查或追加委派来完成比较。';
const single = '查 #101 在 2026-10-17 至 2026-10-30 整个窗口的合计 CTR，再查知识库如何解释日期覆盖。' +
  '只需要这个窗口的合计。请先把合计交给指标 Worker，收到回执后再检索 ctr-review#windows@v1 并引用原文。';
const metric = 'get_plan_metrics_by_date';
const delegate = 'delegate_tasks';

function inspect(result, server) {
  const requests = server.sdk_requests || [];
  const traceId = result.events?.at(-1)?.trace_id;
  const trace = server.traces?.find(row => row.trace_id === traceId);
  const final = result.events?.find(row => row.type === 'text')?.content || '';
  Object.assign(result, {trace_id: traceId, sdk_count: requests.length, final_reply: final,
    sdk_seconds: Number(requests.reduce((sum, row) => sum + (row.elapsed_seconds || 0), 0).toFixed(3)),
    http_statuses: requests.map(row => row.status ?? null), response_models: [...new Set(requests.map(row => row.response_model))]});
  if (server.failure || !requests.length || requests.some(row => row.status !== 200 || row.response_model !== 'deepseek-flash')) {
    result.status = 'blocked';
    result.blocker = server.failure || 'real_deepseek_response_unavailable';
    return;
  }
  if (!trace || result.failure) {
    result.status = 'failed';
    return;
  }

  const main = requests.filter(row => row.tools?.some(tool => tool.function.name === delegate) ||
    row.assistant_calls.some(call => call.function.name === delegate));
  const calls = requests.flatMap(row => row.response_calls || []);
  const receipt = (row, id) => {
    const item = row.tool_receipts.find(entry => entry.tool_call_id === id);
    return item && JSON.parse(item.content);
  };
  const continuation = id => requests.find(row => row.tool_receipts.some(item => item.tool_call_id === id));
  const first = main[0]?.response_calls?.[0];
  const firstReceipt = first && main.map(row => receipt(row, first.id)).find(Boolean);
  const sheets = calls.filter(call => call.function.name === delegate).map(call => {
    const row = main.find(item => receipt(item, call.id));
    return {id: call.id, sheet: JSON.parse(call.function.arguments), receipt: row && receipt(row, call.id),
      next_main_sdk_index: row && requests.indexOf(row) + 1, next_main_calls: row?.response_calls};
  });
  const reads = trace.steps.flatMap((step, index) => {
    if (step.stage !== 'tool_call') return [];
    const call = calls.find(item => item.id === step.tool_call_id);
    if (!call || step.action === delegate) return [];
    const row = continuation(call.id);
    const outcome = trace.steps.find(item => item.stage === 'tool_result' && item.runtime_call_id === step.runtime_call_id);
    return [{name: step.action, role: step.role, tool_call_id: call.id, runtime_call_id: step.runtime_call_id,
      parent_call_id: step.parent_call_id, step: index + 1, arguments: JSON.parse(call.function.arguments),
      receipt: row && receipt(row, call.id), outcome}];
  });
  const created = trace.steps.filter(step => step.stage === 'worker' && step.action === 'created');
  const reclaimed = trace.steps.filter(step => step.stage === 'worker' && step.action === 'reclaimed');
  const ids = steps => steps.map(step => step.runtime_call_id).sort();
  const windows = server.sql.windows;
  const matches = (args, window) => args?.plan_id === 101 &&
    args.start_date === window.start_date && args.end_date === window.end_date;
  const metricReads = reads.filter(read => read.name === metric);
  const split = windows.map(window => metricReads.find(read => matches(read.arguments, window)));
  const total = metricReads.find(read => matches(read.arguments,
    {start_date: windows[0].start_date, end_date: windows[1].end_date}));
  const rules = reads.filter(read => read.name === 'search_knowledge')
    .flatMap(read => read.receipt?.result?.evidence || []);
  const exactRules = Object.entries(server.rule_originals).filter(([id, original]) =>
    rules.some(row => row.chunk_id === id && row.excerpt === original)).map(([id]) => id);
  const checks = {
    first_snapshot: main[0]?.response_calls.length === 1 && first?.function.name === 'get_plan_detail' &&
      JSON.parse(first.function.arguments).plan_id === 101 && firstReceipt?.result?.clicks === (result.scenario === 'normal' ? 120 : 1),
    worker_lifecycle: JSON.stringify(ids(created)) === JSON.stringify(ids(reclaimed)),
    single_terminal_and_commit: trace.status === 'completed' &&
      JSON.stringify(server.committed_replies) === JSON.stringify([final]),
  };
  if (result.scenario === 'single') {
    delete checks.first_snapshot;
    const windowRule = 'ctr-review-v1-windows-01';
    const ruleRead = reads.find(read => read.name === 'search_knowledge' && read.outcome?.status === 'ok' &&
      read.receipt?.result?.evidence?.some(row => row.chunk_id === windowRule));
    const impressions = windows.reduce((sum, row) => sum + row.impressions, 0);
    const clicks = windows.reduce((sum, row) => sum + row.clicks, 0);
    checks.only_requested_total = metricReads.length === 1 && total?.receipt?.result?.coverage?.complete &&
      total.receipt.result.impressions === impressions && total.receipt.result.clicks === clicks;
    checks.no_comparison_followup = trace.steps.every(step => step.stage !== 'evidence_followup') &&
      sheets.every(sheet => !sheet.sheet.comparison_goal);
    checks.delegated_total_then_real_rule_read = total?.role === 'team_worker' && ruleRead?.step > total.step &&
      exactRules.includes(windowRule);
    checks.final_matches_total_and_rule = [impressions, clicks].every(value => final.replaceAll(',', '').includes(String(value))) &&
      final.includes((100 * clicks / impressions).toFixed(2) + '%') && final.includes(windowRule);
  } else if (result.scenario === 'normal') {
    checks.zero_worker_and_reads_only_snapshot = created.length === 0 && sheets.length === 0 && reads.length === 1;
  } else {
    checks.delegated_after_snapshot = created.length > 0 && sheets.length > 0 &&
      Boolean(main.find(row => row.response_calls.some(call => call.id === sheets[0].id) && receipt(row, first?.id)));
    if (result.scenario === 'missing') {
      const missingDate = windows[1].missing_dates[0];
      checks.real_missing_date_in_tool_receipt = metricReads.some(read =>
        read.receipt?.result?.coverage?.missing_dates?.includes(missingDate) ||
        read.receipt?.coverage?.missing_dates?.includes(missingDate));
      checks.specific_gap_in_final = /(?:2026[-年])?10[-月]27/.test(final);
      checks.final_withholds_comparison = /(?:无法|不能|不足以|暂不|不宜).{0,22}(?:判断|确认|得出|计算|比较|判定|适用|执行)/.test(final);
      const missingReads = metricReads.filter(read => read.outcome?.error_code === 'plan_history_missing_dates');
      checks.no_duplicate_missing_reads = missingReads.every(read =>
        metricReads.filter(other => matches(other.arguments, read.arguments)).length === 1);
    } else {
      checks.two_exact_windows = split.every((read, index) => {
        const data = read?.receipt?.result;
        return data?.scope === 'plan' && data.coverage.complete && data.impressions === windows[index].impressions &&
          data.clicks === windows[index].clicks;
      });
      checks.v1_rules_retrieved = ['ctr-review-v1-low-ctr-01', 'ctr-review-v1-low-volume-01'].every(id => exactRules.includes(id));
      checks.final_cites_v1 = final.includes('ctr-review-v1-low-ctr-01') && final.includes('ctr-review-v1-low-volume-01');
      checks.final_matches_sql_and_v1 = [7627, 2906, 11807, 2980].every(value => final.replaceAll(',', '').includes(String(value))) &&
        /38\.1\d?\s*[%％]/.test(final) && /25\.2[34]\s*[%％]/.test(final) && /33\.(?:7[4-9]|8\d?)\s*[%％]/.test(final) &&
        /待复核|需复核|触发.{0,12}复核/.test(final);
      if (result.scenario === 'shifted') {
        const percentages = [...final.matchAll(/(\d+(?:\.\d+)?)\s*[%％]/g)].map(match => Number(match[1]));
        checks.final_matches_sql_and_v1 = windows.flatMap(w => [w.impressions, w.clicks])
          .every(value => final.replaceAll(',', '').includes(String(value))) &&
          [...windows.map(w => w.ctr), server.sql.relative_drop]
            .every(value => percentages.some(percent => Math.abs(100 * value - percent) < 0.06)) &&
          /待复核|需复核|触发.{0,12}复核/.test(final);
        checks.whole_read_first = total && metricReads[0] === total;
        checks.main_replans_after_handoff = trace.steps.some(
          step => step.stage === 'evidence_followup' && step.action === 'opened');
      }
      const comparisons = [...sheets.flatMap(sheet => sheet.receipt?.ctr_comparisons || []),
        ...metricReads.flatMap(read => read.receipt?.ctr_comparisons || [])];
      checks.comparison_matches_sql = comparisons.some(row => [
        [row.baseline_ctr_pct, windows[0].ctr],
        [row.current_ctr_pct, windows[1].ctr],
        [row.relative_decline_pct, server.sql.relative_drop],
      ].every(([actual, expected]) => Math.abs(actual - 100 * expected) < 1e-9));
      checks.displayed_drop_matches_rounded_sql = [...final.matchAll(/(\d+(?:\.\d+)?)\s*[%％]/g)]
        .some(match => Math.abs(Number(match[1]) - 100 * server.sql.relative_drop) <= 0.005);
      if (total && metricReads[0] === total) {
        const handoff = sheets.find(sheet => sheet.receipt?.evidence?.some(
          row => row.runtime_call_id === total.runtime_call_id));
        const nextMain = handoff?.next_main_calls || [];
        checks.split_after_actual_whole_receipt = split.every(read => {
          if (!read || read.step <= total.step) return false;
          const worker = created.find(step => step.runtime_call_id === read.parent_call_id);
          return requests.some(row => receipt(row, total.tool_call_id) &&
            row.response_calls.some(call => call.id === read.tool_call_id)) ||
            nextMain.some(call => call.id === read.tool_call_id || trace.steps.some(step =>
              step.action === delegate && step.tool_call_id === call.id && worker &&
              step.runtime_call_id === worker.parent_call_id));
        });
      }
    }
  }
  Object.assign(result, {first_receipt: firstReceipt, task_sheets_and_handoffs: sheets, reads, exact_rule_chunks: exactRules,
    workers: [created.length, reclaimed.length], windows: split.map(read => read?.tool_call_id ?? null),
    whole_window: total?.tool_call_id, checks, status: Object.values(checks).every(Boolean) ? 'passed' : 'failed'});
}

async function main() {
  const mode = process.argv[2] || 'matrix';
  const valid = ['smoke', 'matrix', 'normal', 'available', 'missing', 'control', 'shifted', 'single'];
  assert.ok(valid.includes(mode), `expected one of ${valid.join(', ')}`);
  const scenarios = mode === 'matrix' ? [...Array(3)].flatMap(() => ['normal', 'available', 'missing']).concat('control')
    : [mode === 'smoke' ? 'normal' : mode];
  const root = path.resolve(process.argv[3] || 'logs/task26-t1/real');
  await fs.mkdir(root, {recursive: true});
  const run = await fs.mkdtemp(path.join(root, mode + '-'));
  const browser = await chromium.launch(browserOptions());
  const rows = [];
  console.log(run);
  try {
    for (const [index, scenario] of scenarios.entries()) {
      const name = String(index + 1).padStart(2, '0') + '-' + scenario;
      const result = await exercise(browser, path.join(run, name), scenario, {
        module: 'scripts.run_task26_t1', message: ({control: explicit, shifted, single})[scenario] || implicit, verify: inspect,
      });
      const {status, failure, blocker, trace_id, sdk_count, elapsed_seconds, sdk_seconds, workers, windows, whole_window, checks} = result;
      const row = {name, scenario, status: status || 'blocked', failure, blocker, trace_id, sdk_count, elapsed_seconds,
        sdk_seconds, workers, windows, whole_window, checks};
      rows.push(row);
      await fs.writeFile(path.join(run, 'report.json'), JSON.stringify(rows, null, 2) + '\n');
      console.log(JSON.stringify(row));
    }
    process.exitCode = rows.every(row => row.status === 'passed') ? 0 : 1;
  } finally {
    await browser.close();
  }
}

module.exports = {inspect};
if (require.main === module) {
  main().catch(error => { console.error(error.message); process.exitCode = 1; });
}
