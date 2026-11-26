const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const { DatabaseSync } = require("node:sqlite");
const { chromium, expect } = require("playwright/test");
const { browserOptions } = require("./browser_options.cjs");
const { openGroupCreation, submitGroupCreation } = require("./chat_group_create.cjs");

const [origin, output, sqlite] = process.argv.slice(2);
const groups = "/api/chat/conversations";
const sku = "TASK42-BOTTLE";
const dates = "比较 2026-10-30 至 2026-11-05 与 2026-11-06 至 2026-11-12";
const sales = "查 SKU " + sku + " 销量，" + dates + "。";
const draft = sales + "结合商品资料重写英文五点，我确认后再应用。";
const tables = ["listing_test_shops", "listing_test_products", "listing_test_facts",
  "listing_test_sales", "listing_change_proposals", "listing_change_receipts"];

function sqlSnapshot() {
  const db = new DatabaseSync(sqlite, { readOnly: true, timeout: 5000 });
  try {
    db.exec("BEGIN");
    return Object.fromEntries(tables.map(table => [table,
      db.prepare("SELECT * FROM " + table + " ORDER BY rowid").all().map(row => ({ ...row }))]));
  } finally {
    db.close();
  }
}

async function browserChecks(page, report) {
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  const detail = page.locator("#workbenchDetail");
  const body = page.locator("#workbenchDetailBody");
  const input = page.locator("#chatInput");
  const messages = page.locator("#chatMessages");
  const action = detail.locator(".group-listing-action");
  const chart = detail.locator(".group-listing-chart");
  const approve = detail.getByRole("button", { name: "批准应用英文五点（本地测试）" });
  const card = (run, type) => messages.locator(`[data-run-id="${run}"] [data-result-kind="${type}"]`);
  const groupButton = id => page.locator(`#chatGroupList [data-conversation-id="${id}"]`);
  const waitFor = (pathname, method = "GET") => page.waitForResponse(response =>
    new URL(response.url()).pathname === pathname && response.request().method() === method);
  const data = async response => {
    assert.ok(response.ok(), await response.text());
    return (await response.json()).data;
  };
  async function screenshot(phase, width) {
    await page.screenshot({ path: path.join(output, width + "-" + phase + ".png"), animations: "disabled" });
  }
  async function viewport(width) {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
    await expect(input).toBeInViewport();
  }
  async function switchGroup(id) {
    if (await page.locator("#chatGroupToggle").isVisible()) await page.locator("#chatGroupToggle").click();
    await groupButton(id).click();
  }
  async function create(title) {
    if (await page.locator("#chatGroupToggle").isVisible()) await page.locator("#chatGroupToggle").click();
    const created = waitFor(groups, "POST");
    await submitGroupCreation(page, title);
    const response = await created;
    assert.equal(response.status(), 201);
    assert.deepEqual(response.request().postDataJSON(), { title: title.trim() });
    const group = await data(response);
    assert.equal(group.title, title.trim());
    await expect(groupButton(group.conversation_id)).toHaveAttribute("aria-current", "true");
    await expect(input).toBeFocused();
    const detailsButton = page.locator("#chatGroupDetailsButton");
    const details = page.locator("#chatGroupDetailsDialog");
    await detailsButton.click();
    await expect(details.locator("#chatGroupDetailsName")).toHaveText(group.title);
    await expect(details.locator("#chatGroupDetailsId")).toHaveText(group.conversation_id);
    await page.keyboard.press("Escape");
    await expect(details).not.toBeVisible();
    await expect(detailsButton).toBeFocused();
    return group.conversation_id;
  }
  async function send(message, conversationId) {
    await input.fill(message);
    const response = waitFor("/api/chat/stream", "POST");
    await input.press("Enter");
    const stream = await response;
    assert.equal(stream.status(), 200);
    assert.equal(stream.request().postDataJSON().conversation_id, conversationId);
    await page.waitForFunction(() => window.workbenchStreams?.length > 0);
    const events = await page.evaluate(() => window.workbenchStreams.shift());
    assert.equal(events.at(-1).type, "done");
    assert.ok(events.every(event => event.conversation_id === conversationId));
    await expect(messages.locator(`.assistant[data-run-id="${events.at(-1).run_id}"]`)).toBeVisible();
    return events.at(-1);
  }
  async function history(conversationId) {
    const token = await page.evaluate(() => JSON.parse(sessionStorage.getItem("ad.login")).token);
    return data(await page.request.get(origin + groups + "/" + conversationId + "/history", {
      headers: { Authorization: "Bearer " + token },
    }));
  }
  async function close(expected) {
    await page.keyboard.press("Escape");
    await expect(detail).not.toBeVisible();
    await expect(expected).toBeFocused();
    await expect(expected).toBeInViewport();
    await expect(input).toBeInViewport();
  }
  async function delayedEvidence(url, start, move, verify) {
    let release, arrived, finished;
    const held = new Promise(resolve => { release = resolve; });
    const fetched = new Promise(resolve => { arrived = resolve; });
    const delivered = new Promise(resolve => { finished = resolve; });
    // Intentionally ignore cancellation in this request to exercise the stale-response guard.
    await page.evaluate(requestPath => {
      const fetch = window.fetch;
      window.fetch = (url, options) => fetch(url, url === requestPath ? { ...options, signal: undefined } : options);
    }, url);
    await page.route("**" + url, async route => {
      const response = await route.fetch();
      arrived();
      await held;
      try { await route.fulfill({ response }); } finally { finished(); }
    }, { times: 1 });
    await start();
    await fetched;
    try { await move(); } finally { release(); }
    await delivered;
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    await verify();
  }

  await page.goto(origin);
  await page.locator("#loginUser").fill("workbench-qa");
  await page.locator("#loginPass").fill(process.env.AD_WORKBENCH_PASSWORD);
  await page.waitForFunction(() => Boolean(window._captchaId));
  await page.locator("#loginCaptcha").fill("0726");
  await page.locator("#loginPass").press("Enter");
  await expect(page.locator("#workbenchHome")).toHaveAttribute("aria-current", "page");
  await expect(page.locator("#sidebarNav")).not.toBeVisible();
  await expect(input).toBeVisible();
  await expect(messages).toContainText("旧聊天原文");
  await expect(messages.locator('.assistant[data-entry-id^="legacy:"] .workbench-card')).toHaveCount(0);
  const legacy = await page.locator("#chatGroupList [aria-current=true]").getAttribute("data-conversation-id");
  report.legacy = { id: legacy, originalReply: "旧聊天原文" };

  const groupToggle = page.locator("#chatGroupToggle");
  const groupRail = page.locator("#chatGroupControls");
  for (const width of [830, 914]) {
    await viewport(width);
    await expect(groupRail).not.toBeVisible();
    await groupToggle.click();
    await expect(groupRail).toBeVisible();
    await expect(groupToggle).toHaveAttribute("aria-expanded", "true");
    const railBounds = await groupRail.boundingBox();
    const threadBounds = await messages.boundingBox();
    assert.ok(railBounds.x + railBounds.width <= threadBounds.x,
      `At ${width}px, the session rail must leave the full chat thread visible`);
    assert.ok(threadBounds.x + threadBounds.width <= width);
    await input.click({ trial: true });
    await page.keyboard.press("Escape");
    await expect(groupRail).not.toBeVisible();
    await expect(groupToggle).toBeFocused();
  }
  await viewport(1440);
  await expect(groupRail).toBeVisible();
  await expect(groupToggle).not.toBeVisible();
  report.sessionRail = { inlineAt: [830, 914], pinnedAt: 1440 };

  const failedTitle = "首次未成功的工作群";
  const { dialog: creationDialog, name: creationName, submit: creationSubmit } = await openGroupCreation(page, "   ");
  const creationRequests = [];
  page.on("request", request => {
    if (new URL(request.url()).pathname === groups && request.method() === "POST") {
      creationRequests.push(request.postDataJSON());
    }
  });
  await expect(creationName).toBeFocused();
  await creationName.press("Enter");
  await expect(creationDialog).toBeVisible();
  await expect(creationName).toHaveAttribute("aria-invalid", "true");
  await expect(creationDialog.locator("#chatGroupCreateFeedback")).toContainText("1–80");
  await creationName.fill("长".repeat(81));
  await creationName.press("Enter");
  await expect(creationName).toHaveAttribute("aria-invalid", "true");
  assert.deepEqual(creationRequests, []);
  await creationName.fill(failedTitle);
  const creationBounds = await creationDialog.boundingBox();
  assert.ok(Math.abs(creationBounds.x + creationBounds.width / 2 - page.viewportSize().width / 2) < 2);
  assert.ok(Math.abs(creationBounds.y + creationBounds.height / 2 - page.viewportSize().height / 2) < 2);
  await page.screenshot({ path: path.join(output, "1440-create-group.png") });
  const createPost = url => url.pathname === groups;
  const postHandler = async action => {
    let intercepted = false;
    const handler = async route => {
      if (route.request().method() !== "POST" || intercepted) return route.fallback();
      intercepted = true;
      try { await action(route); } finally { await page.unroute(createPost, handler); }
    };
    await page.route(createPost, handler);
  };
  await postHandler(route => route.fulfill({ status: 503, json: { detail: "暂时无法创建工作群" } }));
  const failedCreation = waitFor(groups, "POST");
  await creationSubmit.click();
  assert.equal((await failedCreation).status(), 503);
  await expect(creationDialog.locator("#chatGroupCreateFeedback")).toContainText("暂时无法创建工作群");
  await expect(creationName).toHaveValue(failedTitle);
  await expect(creationSubmit).toBeEnabled();
  await expect(groupButton(legacy)).toHaveAttribute("aria-current", "true");
  await creationDialog.locator("#chatGroupCreateCancel").click();
  await expect(creationDialog).not.toBeVisible();
  await expect(page.locator("#chatGroupNew")).toBeFocused();

  let postArrived, releasePost;
  const accepted = new Promise(resolve => { postArrived = resolve; });
  const holdPost = new Promise(resolve => { releasePost = resolve; });
  await postHandler(async route => {
    postArrived();
    await holdPost;
    await route.continue();
  });

  const sameTitle = "北美商品运营";
  const creating = create("  " + sameTitle + "  ");
  let group;
  try {
    await accepted;
    await expect(creationSubmit).toBeDisabled();
    await expect(creationDialog.locator("#chatGroupCreateCancel")).toBeDisabled();
    await page.keyboard.press("Enter");
    await page.keyboard.press("Escape");
    await expect(creationDialog).toBeVisible();
    assert.deepEqual(creationRequests, [{ title: failedTitle }, { title: sameTitle }]);
  } finally {
    releasePost();
    group = await creating;
  }
  report.creationGuards = { emptyRejected: true, tooLongRejected: true, failureStatus: 503, titlePreserved: true,
    cancelReturnedFocus: true, duplicatePostsWhilePending: 0, attempts: creationRequests.slice() };
  const beforeRounds = sqlSnapshot();
  const firstMessage = "查 SKU " + sku + " 销量，比较 2026-11-09 至 2026-11-10 与 2026-11-11 至 2026-11-12。";
  const first = await send(firstMessage, group);
  const firstHistory = await history(group);
  assert.equal(firstHistory.conversation.conversation_id, group);
  assert.deepEqual(firstHistory.entries.filter(entry => entry.kind === "user").map(entry => entry.content), [firstMessage]);
  assert.ok(firstHistory.entries.some(entry => entry.kind === "assistant" && entry.run_id === first.run_id));
  const second = await send(draft, group);
  const firstCard = card(first.run_id, "sales");
  const secondCard = card(second.run_id, "sales");
  const changeCard = card(second.run_id, "listing");
  const firstUrl = groups + "/" + group + "/runs/" + first.run_id + "/evidence";
  const secondUrl = groups + "/" + group + "/runs/" + second.run_id + "/evidence";
  await expect(firstCard).toContainText("两窗合计 29 件 · 变化 +1 件（+7.14%）");
  await expect(secondCard).toContainText("两窗合计 119 件 · 变化 -21 件（-30%）");
  await expect(changeCard).toContainText("待审批");
  assert.notEqual(first.run_id, second.run_id);
  await viewport(390);
  const other = await create(sameTitle);
  assert.notEqual(other, group);
  const otherMessage = sales + "仅查销量，第二群对照。";
  const otherRun = await send(otherMessage, other);
  const otherHistory = await history(other);
  assert.equal(otherHistory.conversation.conversation_id, other);
  assert.equal(otherHistory.conversation.title, firstHistory.conversation.title);
  assert.deepEqual(otherHistory.entries.filter(entry => entry.kind === "user").map(entry => entry.content), [otherMessage]);
  assert.ok(otherHistory.entries.some(entry => entry.kind === "assistant" && entry.run_id === otherRun.run_id));
  await page.reload();
  await expect(groupButton(other)).toHaveAttribute("aria-current", "true");
  await expect(messages).toContainText(otherMessage);
  await expect(messages).not.toContainText(firstMessage);
  assert.deepEqual(await history(other), otherHistory);
  report.creation = { first: group, sameTitleSecond: other, title: sameTitle, firstRun: first.run_id,
    secondGroupFirstRun: otherRun.run_id, secondGroupViewport: 390, historyIsolatedAfterRefresh: true };
  await viewport(1440);
  await page.locator("#chatGroupArchive").click();
  await expect(groupButton(other)).toContainText("已归档");
  await expect(input).toBeDisabled();
  await page.locator("#chatGroupArchive").click();
  await expect(input).toBeEnabled();
  await switchGroup(group);
  await expect(input).toBeEnabled();
  report.archive = { group: other, archivedAndRestored: true };

  await delayedEvidence(firstUrl, () => firstCard.click(), async () => {
    const read = waitFor(secondUrl);
    await secondCard.click();
    report.secondEvidence = await data(await read);
    await expect(chart).toBeVisible();
  }, async () => {
    await expect(secondCard).toHaveAttribute("aria-expanded", "true");
    await expect(firstCard).toHaveAttribute("aria-expanded", "false");
    await expect(chart).toHaveCount(1);
    await expect(chart.locator("[data-date]")).toHaveCount(14);
    await expect(chart.locator('[data-date="2026-10-30"]')).toBeVisible();
  });
  await delayedEvidence(firstUrl, () => firstCard.click(), () => switchGroup(legacy), async () => {
    await expect(detail).not.toBeVisible();
    await expect(messages).toContainText("旧聊天原文");
  });
  await switchGroup(group);
  const firstRead = waitFor(firstUrl);
  await firstCard.click();
  report.firstEvidence = await data(await firstRead);
  await expect(chart).toBeVisible();
  await expect(chart.locator("[data-date]")).toHaveCount(4);
  assert.equal(report.firstEvidence.run_id, first.run_id);
  assert.equal(report.secondEvidence.run_id, second.run_id);
  assert.notEqual(report.firstEvidence.entry_id, report.secondEvidence.entry_id);
  await close(firstCard);
  await delayedEvidence(firstUrl, () => firstCard.click(), () => close(firstCard),
    () => expect(detail).not.toBeVisible());
  report.races = { nextCard: true, nextGroup: true, close: true };

  const evidence = report.secondEvidence;
  const proposed = evidence.proposal.after_bullets;
  const actionId = await changeCard.getAttribute("data-action-id");
  const actionUrl = secondUrl.replace(/evidence$/, "listing-actions/" + actionId);
  const before = sqlSnapshot();
  for (const table of tables.filter(table => table !== "listing_change_proposals")) {
    assert.deepEqual(before[table], beforeRounds[table]);
  }
  assert.equal(before.listing_change_proposals.length, 1);
  report.sql = { beforeRounds, before };

  for (const width of [1440, 390]) {
    await viewport(width);
    await messages.evaluate(element => { element.scrollTop = element.scrollHeight; });
    await screenshot("default", width);
    await secondCard.click();
    await expect(chart).toBeVisible();
    const geometry = await detail.evaluate(element => {
      const box = element.getBoundingClientRect();
      return { modal: element.matches(":modal"), x: box.x, y: box.y, width: box.width, height: box.height };
    });
    assert.equal(geometry.modal, width === 390);
    if (width === 390) assert.deepEqual(geometry, { modal: true, x: 0, y: 0, width: 390, height: 844 });
    else {
      assert.ok(geometry.x > 700 && geometry.width < 510);
      await input.focus();
      await expect(input).toBeFocused();
    }
    await screenshot("detail", width);
    await close(secondCard);
    const checked = waitFor(actionUrl);
    await changeCard.click();
    assert.match((await checked).url(), /\?review=true$/);
    await expect(approve).toBeEnabled();
    await expect(detail.locator(".workbench-listing-product")).toHaveText(evidence.listing.title);
    await expect(detail).toContainText("店铺 task42-test · 站点 US · 商品编号 " + sku);
    await expect(detail.locator('[data-bullet-version="before"] li')).toHaveText(evidence.listing.bullets);
    assert.deepEqual(await detail.locator('[data-bullet-version="proposed"] li').evaluateAll(rows =>
      rows.map(row => row.firstChild.textContent)), proposed);
    await expect(detail.locator('[data-bullet-version="proposed"] [data-fact-id]')).toHaveCount(5);
    const source = detail.locator(".workbench-listing-source").first();
    await source.locator("summary").click();
    await expect(source).toHaveAttribute("open", "");
    await expect(source.locator("p")).toContainText(evidence.facts.facts[0].statement);
    assert.ok(evidence.listing.bullets[0].length > 120 && proposed[0].length > 120);
    assert.equal(await body.evaluate(element => element.scrollWidth > element.clientWidth), false);
    await screenshot("pending", width);
    if (width === 390) {
      await detail.getByText("商品资料 · 版本 1", { exact: true }).click();
      await approve.scrollIntoViewIfNeeded();
      await expect.poll(() => body.evaluate(element => element.scrollTop)).toBeGreaterThan(0);
      await screenshot("pending-scrolled", width);
    }
    await close(changeCard);
    report[String(width)] = { geometry, keyboardReturn: true, longEnglish: true, composerReachable: true };
  }

  await viewport(1440);
  await changeCard.click();
  await expect(approve).toBeEnabled();
  assert.deepEqual(sqlSnapshot(), before);
  const order = [];
  page.on("response", response => {
    if (new URL(response.url()).pathname === actionUrl + "/confirm") order.push("POST response");
  });
  page.on("request", request => {
    if (request.url() === origin + actionUrl) order.push("GET request");
  });
  const post = waitFor(actionUrl + "/confirm", "POST");
  const get = waitFor(actionUrl);
  await approve.click();
  report.post = await data(await post);
  report.readback = await data(await get);
  assert.deepEqual(order, ["POST response", "GET request"]);
  assert.deepEqual(report.post, report.readback);
  const receipt = report.readback.receipt;
  assert.equal(receipt.source_trace_id, second.trace_id);
  assert.ok(receipt.execution_trace_id);
  assert.deepEqual(report.readback.listing.bullets, proposed);
  await expect(action).toContainText(receipt.execution_trace_id);
  await expect(page.locator("#workbenchDetailClose")).toBeInViewport();
  await expect(page.locator("#chatGroupTitle")).toBeInViewport();
  await expect(input).toBeInViewport();
  await expect(detail.locator('[data-bullet-version="actual"]')).toBeInViewport();
  const after = sqlSnapshot();
  report.sql.after = after;
  const expected = before.listing_test_products.map(row => row.sku === sku ? {
    ...row, bullets: JSON.stringify(proposed), version: row.version + 1,
  } : row);
  assert.deepEqual(after.listing_test_products, expected);
  for (const table of tables.filter(table => !["listing_test_products", "listing_change_receipts"].includes(table))) {
    assert.deepEqual(after[table], before[table]);
  }
  assert.equal(after.listing_change_receipts.length, 1);
  assert.equal(after.listing_change_receipts[0].receipt_id, receipt.receipt_id);
  await screenshot("executed", 1440);
  await page.reload();
  await expect(detail).not.toBeVisible();
  await expect(changeCard).toContainText("已执行");
  await viewport(390);
  const restored = waitFor(actionUrl);
  await changeCard.click();
  report.refresh = await data(await restored);
  assert.deepEqual(report.refresh, report.readback);
  await expect(action).toContainText("当前实际版本 2");
  await expect(detail.locator('[data-bullet-version="actual"]')).toBeInViewport();
  await screenshot("executed", 390);
  await close(changeCard);
  const reloadedEvidence = waitFor(firstUrl);
  await firstCard.click();
  assert.deepEqual(await data(await reloadedEvidence), report.firstEvidence);
  await close(firstCard);

  await viewport(1440);
  const stale = await send(draft + "这次采用备选措辞。", group);
  const staleCard = card(stale.run_id, "listing");
  const writer = new DatabaseSync(sqlite, { timeout: 5000 });
  try {
    writer.prepare("UPDATE listing_test_facts SET version=version+1 WHERE sku=?").run(sku);
    const staleBefore = sqlSnapshot();
    await staleCard.click();
    await expect(action).toContainText("提案已失效");
    await expect(approve).toHaveCount(0);
    assert.deepEqual(sqlSnapshot(), staleBefore);
    await close(staleCard);
    writer.prepare("UPDATE listing_test_sales SET series=json_remove(series, '$.2026-11-12') WHERE sku=?").run(sku);
  } finally { writer.close(); }
  const gapBefore = sqlSnapshot();
  const gap = await send(draft, group);
  await expect(card(gap.run_id, "listing")).toHaveCount(0);
  await card(gap.run_id, "evidence").click();
  await expect(detail).toContainText("销量记录不完整");
  await expect(detail).toContainText("有数据 6 天");
  await expect(approve).toHaveCount(0);
  assert.deepEqual(sqlSnapshot(), gapBefore);
  report.blocked = { stale: stale.run_id, gap: gap.run_id, approvalsAbsent: true };

  await delayedEvidence(firstUrl, () => firstCard.click(), async () => {
    await page.getByRole("button", { name: /退出登录/ }).click();
    await expect(page.locator("#loginPage")).toBeVisible();
  }, () => expect(detail).not.toBeVisible());
  report.races.logout = true;
  await page.reload();
  // Sign in again through the normal entry to confirm the navigation routes.
  await page.locator("#loginUser").fill("workbench-qa");
  await page.locator("#loginPass").fill(process.env.AD_WORKBENCH_PASSWORD);
  await page.waitForFunction(() => Boolean(window._captchaId));
  await page.locator("#loginCaptcha").fill("0726");
  await page.locator("#loginPass").press("Enter");
  await page.getByRole("link", { name: "AI 工作台", exact: true }).click();
  await page.getByRole("button", { name: "广告计划复核", exact: true }).click();
  await expect(page.getByRole("heading", { name: "广告计划复核" })).toBeVisible();
  await page.getByRole("button", { name: "返回工作台" }).click();
  await expect(input).toBeVisible();
  await expect(page.getByRole("link", { name: "店铺订单", exact: true })).not.toBeVisible();
  const amazonRequests = [];
  page.on("request", request => {
    if (/\/api\/orders\/shops\/amazon-us-fbm\/.*\/(preflight|sync)$/.test(new URL(request.url()).pathname)) {
      amazonRequests.push(request.url());
    }
  });
  const sources = page.locator("#workbenchSourcesTrigger");
  await sources.click();
  const sourceDialog = page.getByRole("dialog", { name: "数据来源" });
  await expect(sourceDialog).toContainText("本地测试数据");
  await expect(sourceDialog).toContainText("已登记 · 待验证");
  await page.screenshot({ path: path.join(output, "1440-data-sources.png") });
  await sourceDialog.getByRole("link", { name: "管理订单连接 · workbench-qa-amazon" }).click();
  await page.waitForURL(url => url.pathname === "/orders" && url.searchParams.get("source") === "amazon-us-fbm");
  await expect(page).toHaveTitle(/订单/);
  await page.getByRole("link", { name: "返回工作台", exact: true }).click();
  await page.waitForURL(url => url.origin === origin && url.pathname === "/");
  await expect(page.locator("#workbenchHome")).toHaveAttribute("aria-current", "page");
  await expect(groupButton(group)).toHaveAttribute("aria-current", "true");
  await expect(page.locator("#sidebarNav a[data-page=dashboard]")).not.toBeVisible();
  await page.getByRole("button", { name: "查销量", exact: true }).click();
  await expect(input).toHaveValue(/商品名.*上周卖了多少件/);
  await input.fill("");
  assert.deepEqual(amazonRequests, []);
  report.routes = { diagnosis: true, ordersViaSources: true, legacyNavHidden: true, returnToSelectedGroup: true };
  const delayedTitle = "甲创建但响应迟到";
  await page.evaluate(pathname => {
    const fetch = window.fetch;
    window.fetch = (url, options) => fetch(url, url === pathname && options?.method === "POST" ?
      { ...options, signal: undefined } : options);
  }, groups);
  let createFetched, releaseCreate, createDelivered;
  const fetchedCreate = new Promise(resolve => { createFetched = resolve; });
  const heldCreate = new Promise(resolve => { releaseCreate = resolve; });
  const deliveredCreate = new Promise(resolve => { createDelivered = resolve; });
  await postHandler(async route => {
    const response = await route.fetch();
    const { data: committed } = await response.json();
    createFetched(committed);
    try {
      await heldCreate;
      await route.fulfill({ response });
    } finally { createDelivered(); }
  });
  await submitGroupCreation(page, delayedTitle);
  const lateGroup = await fetchedCreate;
  try {
    // Simulate the application's session transition while a modal owns page focus.
    await page.evaluate(() => doLogout());
    await expect(creationDialog).not.toBeVisible();
    await page.locator("#loginUser").fill("workbench-qa-empty");
    await page.locator("#loginPass").fill(process.env.AD_WORKBENCH_PASSWORD);
    await page.waitForFunction(() => Boolean(window._captchaId));
    await page.locator("#loginCaptcha").fill("0726");
    await page.locator("#loginPass").press("Enter");
    await expect(page.locator("#chatGroupNew")).toBeVisible();
    await expect(page.locator("#chatGroupList .workbench-group-empty")).toHaveText("还没有会话");
    await expect(input).toBeDisabled();
    await expect(page.locator("#chatGroupList [data-conversation-id]")).toHaveCount(0);
  } finally { releaseCreate(); }
  await deliveredCreate;
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  await expect(creationDialog).not.toBeVisible();
  await expect(groupButton(lateGroup.conversation_id)).toHaveCount(0);
  await expect(messages).not.toContainText("旧聊天原文");
  await expect(input).toBeDisabled();
  await viewport(390);
  await page.locator("#chatGroupToggle").click();
  await page.locator("#workbenchSourcesTrigger").click();
  await expect(sourceDialog).toContainText("未登记");
  await expect(sourceDialog.getByRole("link", { name: /管理订单连接/ })).toHaveCount(0);
  await page.screenshot({ path: path.join(output, "390-data-sources.png") });
  await page.keyboard.press("Escape");
  await expect(sourceDialog).not.toBeVisible();
  await expect(page.locator("#workbenchSourcesTrigger")).toBeFocused();
  report.sources = { unregistered: true, noOutboundSync: amazonRequests.length === 0 };
  report.creationGuards.staleResponseAfterAccountChange = { ignored: true, committedFirstAccount: lateGroup.conversation_id };
  assert.deepEqual(errors, []);
}

async function main() {
  const report = { passed: false };
  const browser = await chromium.launch(browserOptions());
  try {
    report.browser = browser.version();
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, timezoneId: "Asia/Shanghai" });
    page.setDefaultTimeout(20000);
    await page.addInitScript(() => {
      const fetch = window.fetch.bind(window);
      window.workbenchStreams = [];
      window.fetch = async (url, options) => {
        const response = await fetch(url, options);
        if (url === "/api/chat/stream") {
          response.clone().text().then(text => window.workbenchStreams.push(text.split(/\r?\n/)
            .filter(line => line.startsWith("data: ")).map(line => JSON.parse(line.slice(6)))));
        }
        return response;
      };
    });
    await browserChecks(page, report);
    report.passed = true;
  } catch (error) {
    report.failure = error.stack;
    throw error;
  } finally {
    await browser.close();
    await fs.writeFile(path.join(output, "browser.json"), JSON.stringify(report, null, 2) + "\n");
  }
  console.log(JSON.stringify({ passed: report.passed, browser: report.browser, races: report.races }));
}

main().catch(error => { console.error(error); process.exitCode = 1; });
