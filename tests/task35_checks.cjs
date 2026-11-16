const assert = require("node:assert/strict");
const path = require("node:path");
const { DatabaseSync } = require("node:sqlite");
const { expect } = require("playwright/test");
const { auth, openProgress, panel, progress } = require("./task30_checks.cjs");
const { approvePause, query, readRefreshedPause, record, snapshot } = require("./group_pause_checks.cjs");

function open(fixture) {
  const db = new DatabaseSync(fixture.sqlite, { readOnly: true, timeout: 5000 });
  const ledger = fixture.ledger && new DatabaseSync(fixture.ledger, { readOnly: true, timeout: 5000 });
  const read = () => snapshot(db);
  const attempts = () => ledger ? query(ledger, "SELECT count(*) AS n FROM sdk")[0].n : 0;
  const result = { http: {}, sql: { before_review: read() }, sdk_attempts: {} };
  result.sql.registrations = query(db, "SELECT * FROM diagnosis_sandbox_targets ORDER BY plan_id");
  assert.deepEqual(result.sql.registrations.map(row => [row.plan_id, row.user_id]), [[103, 17]]);
  assert.equal(result.sql.before_review.plans.find(row => row.id === 103).status, "active");
  assert.deepEqual(result.sql.before_review.proposals, []);
  assert.deepEqual(result.sql.before_review.receipts, []);
  assert.equal(attempts(), 0);
  return {
    result, read, attempts, live: Boolean(ledger), directory: fixture.directory,
    close() { db.close(); ledger?.close(); },
  };
}

async function approve(page, state, sample, terminal, oldRun, renderedAnswer, save) {
  const group = sample.conversation_id;
  const answers = sample.history.entries.filter(entry => entry.kind === "assistant");
  assert.equal(answers.length, 1);
  const [answer] = answers;
  assert.equal(answer.run_id, terminal.run_id);
  const result = state.result;
  const headers = await auth(page);
  const request = async (method, url, data) =>
    record(await page.request.fetch(url, { method, headers, data }));
  state.shot = async name => {
    await page.locator("#workbenchDetail").screenshot({ path: path.join(state.directory, "pause-" + name + ".png") });
  };
  state.approval = await approvePause({
    page, group, initial: { terminal, answer, actions: answer.pause_actions || [] },
    result, snapshot: state.read, shot: state.shot, request,
    beforeApprove: async () => {
      assert.deepEqual(result.basis, sample.basis_before_release);
      assert.equal(result.sql.before_approval.proposals[0].registration_id, result.sql.registrations[0].registration_id);
      const verdict = {
        ...sample,
        refreshed: {
          answer: renderedAnswer,
          basis: result.basis,
          old_progress: await progress(page, oldRun),
          new_progress: await progress(page, terminal.run_id),
        },
      };
      const checked = await request("POST", "/__task35/preapproval", verdict);
      result.preapproval_audit = checked.body;
      save();
      assert.equal(checked.status, 200, JSON.stringify(checked.body));
      result.sdk_attempts.before_approval = state.attempts();
      assert.equal(result.sdk_attempts.before_approval, checked.body.sdk_attempts);
      if (state.live) assert.ok(result.sdk_attempts.before_approval > 0);
      save();
    },
  });
  result.sdk_attempts.after_first_get = state.attempts();
  assert.equal(result.sdk_attempts.after_first_get, result.sdk_attempts.before_approval);
}

async function refresh(page, state, basis) {
  const result = state.result;
  const { action, receipt, runId, url } = state.approval;
  assert.equal(basis.entry_id, result.basis.entry_id);
  const answer = page.locator(`#chatMessages > .assistant[data-run-id="${runId}"]`).last();
  await expect(answer).toHaveAttribute("data-entry-id", basis.entry_id);
  await expect(answer.locator('[data-result-kind="pause"]')).toHaveAttribute("data-action-id", action);
  await openProgress(page, runId);
  await expect(panel(page, runId).locator(":scope > summary")).toHaveText("协作进度 · 已完成");
  await readRefreshedPause({ page, action, url, receipt, result, shot: state.shot });
  result.refreshed_evidence_ids = [...basis.metrics, basis.rule, ...basis.details].map(row => row.artifact_id);
  result.sql.after_refresh = state.read();
  assert.deepEqual(result.sql.after_refresh, result.sql.after_approval);
  result.sdk_attempts.after_refresh = state.attempts();
  assert.equal(result.sdk_attempts.after_refresh, result.sdk_attempts.before_approval);
}

module.exports = { open, approve, refresh };
