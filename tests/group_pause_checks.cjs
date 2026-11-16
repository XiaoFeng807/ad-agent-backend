const assert = require("node:assert/strict");
const { DatabaseSync } = require("node:sqlite");
const { expect } = require("playwright/test");
const { displayedEvidence } = require("./task30_checks.cjs");

const groups = "/api/chat/conversations";
const actionPath = (group, run, action) => `${groups}/${group}/runs/${run}/pause-actions/${action}`;
const actionPanel = (page, action) => page.locator(`.group-pause[data-action-id="${action}"]`);
const waitFor = (page, method, pathname) =>
      page.waitForResponse(r => new URL(r.url()).pathname === pathname && r.request().method() === method);

function query(reader, statement, ...args) {
  return reader.prepare(statement).all(...args).map(row => ({ ...row }));
}

function change(sqlite, statement, ...args) {
  const db = new DatabaseSync(sqlite, { timeout: 5000 });
  try { db.prepare(statement).run(...args); }
  finally { db.close(); }
}

function snapshot(reader) {
  reader.exec("BEGIN");
  try {
    return {
      plans: query(reader, "SELECT * FROM ad_plans ORDER BY id"),
      receipts: query(reader, "SELECT * FROM chat_group_pause_receipts ORDER BY rowid"),
      diagnosis_receipts: query(reader, "SELECT * FROM diagnosis_pause_receipts ORDER BY rowid"),
      proposals: query(reader, "SELECT * FROM chat_group_pause_proposals ORDER BY rowid"),
      updates: query(reader, "SELECT * FROM task34_status_updates ORDER BY rowid"),
    };
  } finally { reader.exec("ROLLBACK"); }
}

async function record(response) {
  return {
    path: new URL(response.url()).pathname, status: response.status(), body: await response.json(),
    ...(response.request ? {
      method: response.request().method(), request_body: response.request().postData(),
      cache_control: (await response.allHeaders())["cache-control"],
    } : {}),
  };
}

function trackStatusUpdates(sqlite) {
  change(sqlite, "CREATE TABLE task34_status_updates (plan_id INTEGER, old_status TEXT, new_status TEXT)");
  change(sqlite, `CREATE TRIGGER task34_count_update AFTER UPDATE OF status ON ad_plans
          WHEN old.status != new.status BEGIN
            INSERT INTO task34_status_updates VALUES (old.id, old.status, new.status);
          END`);
}

async function approvePause({ page, group, initial, result, snapshot, shot, request, beforeApprove }) {
  const [proposal] = initial.actions;
  assert.equal(initial.actions.length, 1);
  assert.equal(proposal.state, "pending");
  assert.deepEqual(proposal.target, { plan_id: 103, plan_name: "东港搜索", expected_status: "active" });
  const runId = initial.terminal.run_id;
  const action = proposal.action_id;
  const url = actionPath(group, runId, action);
  result.source = initial.terminal;
  result.basis = await displayedEvidence(page, group, runId);
  assert.equal(result.basis.review_status, "completed");
  assert.equal(result.basis.entry_id, initial.answer.entry_id);
  assert.equal(result.basis.details[0].id, 103);
  assert.match(initial.answer.content, /#103/);
  await page.locator(`[data-result-kind="pause"][data-action-id="${action}"]`).click();
  const pending = actionPanel(page, action);
  await expect(pending).toContainText("待审批");
  await pending.scrollIntoViewIfNeeded();
  await shot("01-pending");
  const approve = pending.getByRole("button", { name: "批准暂停投放（沙箱）" });
  let approvalRequests = 0;
  page.on("request", r => {
    if (new URL(r.url()).pathname === url + "/confirm") approvalRequests++;
  });
  page.once("dialog", async dialog => {
    result.preview = dialog.message();
    await dialog.dismiss();
  });
  await approve.click();
  assert.match(result.preview, /#103 东港搜索[\s\S]*active → paused/);
  assert.equal(approvalRequests, 0);
  result.sql.before_approval = snapshot();
  assert.deepEqual(result.sql.before_approval.plans, result.sql.before_review.plans);
  assert.equal(result.sql.before_approval.plans.find(row => row.id === 103).status, "active");
  assert.deepEqual(result.sql.before_approval.receipts, []);
  assert.deepEqual(result.sql.before_approval.updates, []);
  assert.equal(result.sql.before_approval.proposals.length, 1);
  assert.equal((await request("GET", url)).status, 404);

  assert.deepEqual(result.sql.before_approval.diagnosis_receipts, []);
  const saved = result.sql.before_approval.proposals[0];
  assert.equal(saved.action_id, action);
  assert.equal(saved.conversation_id, group);
  assert.equal(saved.run_id, runId);
  assert.equal(`entry:${saved.entry_id}`, initial.answer.entry_id);
  assert.equal(saved.plan_id, 103);
  const { conversation_id, run_id, entry_id, ...evidence } = result.basis;
  assert.deepEqual(JSON.parse(saved.snapshot).evidence, evidence);
  assert.equal(JSON.parse(saved.snapshot).source_trace_id, initial.terminal.trace_id);
  if (beforeApprove) await beforeApprove();
  const card = page.locator(`[data-result-kind="pause"][data-action-id="${action}"]`);
  if (await card.getAttribute("aria-expanded") !== "true") await card.click();

  const order = [];
  const confirmed = response => {
    if (new URL(response.url()).pathname === url + "/confirm" && response.request().method() === "POST") {
      order.push("POST response");
    }
  };
  const readback = request => {
    if (new URL(request.url()).pathname === url && request.method() === "GET") order.push("GET request");
  };
  page.on("response", confirmed);
  page.on("request", readback);
  const post = waitFor(page, "POST", url + "/confirm");
  const get = waitFor(page, "GET", url);
  page.once("dialog", dialog => dialog.accept());
  await approve.click();
  result.http.post = await record(await post);
  result.http.get = await record(await get);
  page.off("response", confirmed);
  page.off("request", readback);
  result.http.order = order;
  assert.deepEqual(order, ["POST response", "GET request"]);
  assert.equal(approvalRequests, 1);
  assert.equal(result.http.post.status, 200);
  assert.equal(result.http.post.request_body, null);
  assert.equal(result.http.get.status, 200);
  assert.equal(result.http.get.cache_control, "no-store");
  const receipt = result.http.post.body.data;

  assert.equal(receipt.conversation_id, group);
  assert.equal(receipt.run_id, runId);
  assert.equal(receipt.entry_id, initial.answer.entry_id);
  assert.equal(receipt.source_trace_id, initial.terminal.trace_id);
  assert.equal(receipt.approved_by_user_id, 17);
  assert.equal(receipt.approved_by_name, "task27-owner");
  assert.equal(receipt.committed_status, "paused");
  assert.deepEqual(receipt.evidence, evidence);
  result.sql.after_approval = snapshot();
  assert.deepEqual(result.sql.after_approval.plans,
    result.sql.before_approval.plans.map(row => row.id === 103 ? { ...row, status: "paused" } : row));
  assert.deepEqual(result.sql.after_approval.updates, [{ plan_id: 103, old_status: "active", new_status: "paused" }]);
  assert.equal(result.sql.after_approval.receipts.length, 1);
  const row = result.sql.after_approval.receipts[0];
  for (const key of ["action_id", "receipt_id", "execution_trace_id", "approved_by_user_id",
                     "approved_by_name", "created_at", "committed_status"]) assert.equal(row[key], receipt[key]);
  assert.deepEqual(result.sql.after_approval.proposals, result.sql.before_approval.proposals);
  assert.deepEqual(result.sql.after_approval.diagnosis_receipts, []);
  const actualStatus = result.sql.after_approval.plans.find(row => row.id === 103).status;
  assert.deepEqual(result.http.get.body.data, { receipt, actual_status: actualStatus });
  await expect(pending).toContainText("执行时状态 paused · 当前实际状态 paused");
  await expect(pending).toContainText(receipt.execution_trace_id);
  await shot("02-approved");
  return { action, runId, url, receipt };
}

async function readRefreshedPause({ page, action, url, receipt, result, shot }) {
  const original = actionPanel(page, action);
  const refreshed = waitFor(page, "GET", url);
  await page.locator(`[data-result-kind="pause"][data-action-id="${action}"]`).click();
  result.http.refresh = await record(await refreshed);
  assert.deepEqual(result.http.refresh.body, result.http.get.body);
  await expect(original).toContainText(receipt.receipt_id);
  await expect(original).toContainText(receipt.approved_by_name);
  await expect(original).toContainText(receipt.execution_trace_id);
  await expect(original).toContainText("当前实际状态 paused");
  await shot("03-refresh");
}

module.exports = { actionPanel, actionPath, approvePause, change, query, readRefreshedPause, record, snapshot, trackStatusUpdates, waitFor };
