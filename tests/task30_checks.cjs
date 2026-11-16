const assert = require("node:assert/strict");

const groups = "/api/chat/conversations";
const evidencePath = (group, run) => `${groups}/${group}/runs/${run}/evidence`;
const panel = (page, run) => page.locator(`.group-progress[data-run-id="${run}"]`);
const delegation = (page, run) => page.locator(run ?
  `#chatMessages > .workbench-delegation[data-run-id="${run}"]` : "#chatMessages > .workbench-delegation");
const progressButton = (page, run) => delegation(page, run).locator('[data-result-kind="progress"]');

async function auth(page) {
  const token = await page.evaluate(() => JSON.parse(sessionStorage.getItem("ad.login")).token);
  return { Authorization: "Bearer " + token };
}

async function expand(details) {
  if (await details.getAttribute("open") === null) await details.locator(":scope > summary").click();
}

async function displayedEvidence(page, group, run) {
  const card = page.locator(`.msg[data-run-id="${run}"] [data-result-kind="evidence"]`);
  const details = page.locator("#workbenchDetail .group-evidence");
  const response = await page.request.get(evidencePath(group, run), { headers: await auth(page) });
  assert.equal(response.status(), 200);
  const data = (await response.json()).data;
  assert.equal(data.conversation_id, group);
  assert.equal(data.run_id, run);
  assert.equal(await card.locator("xpath=ancestor::*[@data-entry-id][1]").getAttribute("data-entry-id"), data.entry_id);
  if (await card.getAttribute("aria-expanded") !== "true") await card.click();
  if (data.alert_review) {
    await details.locator('[data-evidence="alert-period"]').waitFor();
    const text = await details.textContent();
    const period = data.alert_review.alert_period;
    for (const value of [period.timezone, period.start, period.end]) assert.ok(text.includes(value));
    const links = await details.locator('[data-evidence="alert"]').allTextContents();
    assert.deepEqual(links, data.alert_review.associations.map(row => `告警 #${row.alert_id} → 计划 #${row.plan_id}`));
    for (const {ctr_check: check} of data.alert_review.associations) {
      if (!check) continue;
      assert.ok(text.includes("检测时间") && text.includes(check.rule));
      for (const [label, window] of [["基准窗", check.baseline], ["当前窗", check.current]]) {
        assert.ok(text.includes(`告警检测${label} ${window.start_date} 至 ${window.end_date}`));
      }
    }
    const gaps = await details.locator('[data-evidence="alert-gap"]').allTextContents();
    assert.equal(gaps.length, data.alert_review.association_gaps.length);
    for (const [index, row] of data.alert_review.association_gaps.entries()) {
      assert.ok(gaps[index].includes(`告警 #${row.alert_id}`));
    }
    if (data.alert_review.review_scope) assert.ok(text.includes("指标比较"));
  } else {
    await details.locator('[data-evidence="metric"]').first().waitFor();
  }
  const metrics = await details.locator('[data-evidence="metric"]').allTextContents();
  assert.equal(metrics.length, data.alert_review ? data.metrics.length : 4);
  for (const [index, metric] of data.metrics.entries()) {
    const text = metrics[index];
    assert.ok(text.includes(`#${metric.plan_id} · ${metric.start_date} 至 ${metric.end_date}`));
    if (metric.gap) assert.ok(text.includes(metric.gap_text || metric.gap));
    else {
      assert.ok(text.includes("曝光 " + metric.impressions.toLocaleString("en-US")));
      assert.ok(text.includes("点击 " + metric.clicks.toLocaleString("en-US")));
      assert.ok(text.includes(`CTR ${metric.ctr_pct.toFixed(2)}%`));
    }
  }
  const artifactIds = await details.locator('[data-artifact-id]').evaluateAll(rows => rows.map(row => row.dataset.artifactId));
  assert.deepEqual(artifactIds, [...data.metrics, data.rule, ...data.details].map(row => row?.artifact_id).filter(Boolean));
  if (data.rule) {
    const text = await details.locator('[data-evidence="rule"]').textContent();
    assert.ok(data.rule.source);
    for (const part of [data.rule.version, data.rule.source, data.rule.chunk_id]) assert.ok(text.includes(part));
  }
  for (const detail of data.details) {
    const text = await details.locator(`[data-evidence="detail"]:has([data-artifact-id="${detail.artifact_id}"])`).textContent();
    for (const part of [detail.id, detail.plan_name, detail.status, detail.daily_budget, detail.review_input.worker_id]) {
      assert.ok(text.includes(String(part)));
    }
  }
  return data;
}

async function progress(page, run) {
  const details = panel(page, run);
  await openProgress(page, run);
  await details.waitFor({ state: "attached" });
  await expand(details);
  return {
    summary: await details.locator(":scope > summary").textContent(),
    scope: await details.locator(".group-scope").allTextContents(),
    tasks: await taskRows(details),
    entries: await details.locator("[data-entry-id]").evaluateAll(rows => rows.map(row => ({
      entry_id: row.dataset.entryId, run_id: row.dataset.runId, kind: row.dataset.event,
      task_label: row.dataset.taskLabel, worker_id: row.dataset.workerId, text: row.textContent,
      runtime_call_id: row.dataset.runtimeCallId, dispatch_id: row.dataset.dispatchId, model_call_id: row.dataset.modelCallId,
      artifact_ids: [...row.querySelectorAll("[data-artifact-id]")].map(code => code.dataset.artifactId),
      displayed_ids: [...row.querySelectorAll("code")].map(code => code.textContent),
    }))),
  };
}

async function openProgress(page, run) {
  const card = progressButton(page, run);
  if (await card.getAttribute("aria-expanded") !== "true") await card.click();
}

async function taskRows(panel) {
  return panel.locator(".group-task").evaluateAll(rows => rows.map(row => ({
    task_label: row.dataset.taskLabel, state: row.dataset.state, open: row.open,
    text: row.querySelector("summary").textContent,
  })));
}

function verifyProgress(sample, history, run) {
  const entries = history.entries.filter(row => row.run_id === run && row.kind === "worker");
  const tasks = [...new Set(entries.map(row => row.event.task_label))];
  assert.deepEqual(sample.tasks.map(row => row.task_label).sort(), tasks.sort());
  for (const label of tasks) {
    const expected = entries.filter(row => row.event.task_label === label);
    const displayed = sample.entries.filter(row => row.task_label === label);
    assert.deepEqual(displayed.map(row => row.entry_id), expected.map(row => row.entry_id));
    for (const [index, row] of displayed.entries()) {
      const event = expected[index].event;
      assert.equal(row.run_id, run);
      assert.equal(row.kind, event.kind);
      for (const key of ["worker_id", "runtime_call_id", "dispatch_id", "model_call_id"]) {
        assert.equal(row[key], event[key] ?? undefined);
        if (event[key]) assert.ok(row.displayed_ids.includes(event[key]));
      }
      assert.deepEqual(row.artifact_ids, [event.artifact_id, ...(event.producers || []).map(row => row.artifact_id)].filter(Boolean));
      for (const producer of event.producers || []) {
        assert.ok(row.displayed_ids.includes(producer.worker_id));
        if (producer.runtime_call_id) assert.ok(row.displayed_ids.includes(producer.runtime_call_id));
      }
    }
  }
}

async function uniqueEntries(page) {
  const ids = await page.locator("#chatMessages [data-entry-id]").evaluateAll(rows => rows.map(row => row.dataset.entryId));
  assert.equal(new Set(ids).size, ids.length);
  return ids;
}

module.exports = { auth, delegation, displayedEvidence, evidencePath, expand, openProgress, panel, progress,
  progressButton, taskRows, uniqueEntries, verifyProgress };
