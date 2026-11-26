const assert = require("node:assert/strict");
const {createHash} = require("node:crypto");
const fs = require("node:fs/promises");
const path = require("node:path");
const { DatabaseSync } = require("node:sqlite");
const { chromium, expect } = require("playwright/test");
const { browserOptions } = require("./browser_options.cjs");
const { submitGroupCreation } = require("./chat_group_create.cjs");

const [origin, folder, mode] = process.argv.slice(2);
const file = name => path.join(folder, name);
const groupsPath = "/api/chat/conversations";
const matches = (response, method, pathname) =>
  new URL(response.url()).pathname === pathname && response.request().method() === method;
const actionLabel = {title: "建议改标题", review: "复核投放", observe: "继续观察"};
const metrics = ["impressions", "clicks", "orders", "spend_cents", "sales_cents", "ctr_pct", "cvr_pct", "cpc_usd", "acos_pct"];

function businessSnapshot(db) {
  return Object.fromEntries(["listing_test_shops", "listing_test_products", "listing_test_facts", "listing_test_sales",
    "listing_change_proposals", "listing_change_receipts", "search_term_ad_groups",
    "search_term_amazon_membership", "search_term_amazon_row_links", "ad_plans"].map(
    table => [table, db.prepare("SELECT * FROM " + table + " ORDER BY rowid").all()]));
}

async function run() {
  const {contract, independent_sql: oracle, independent_scores: scores, files, membership_sha256: membershipSha} =
    JSON.parse(await fs.readFile(file("frozen.json"), "utf8"));
  const report = {mode, cases: {}, checks: {}, pageErrors: []};
  const db = new DatabaseSync(file("sandbox.sqlite3"));
  db.exec("PRAGMA busy_timeout=5000");
  let browser, context, page;
  try {
    browser = await chromium.launch(browserOptions());
    report.browser = {channel: "msedge", version: browser.version()};
    context = await browser.newContext({baseURL: origin, viewport: {width: 1440, height: 1000},
      timezoneId: "Asia/Shanghai", permissions: ["clipboard-read", "clipboard-write"],
      ...(process.env.SEARCH_TERMS_VIDEO === "1"
        ? {recordVideo: {dir: folder, size: {width: 1440, height: 1000}}} : {})});
    const session = await context.request.post("/api/login", {data: {
      username: "sp-test-operator", password: process.env.SEARCH_TERMS_PASSWORD,
    }});
    const login = await session.json();
    assert.equal(login.code, 200);
    await context.addInitScript(data => {
      sessionStorage.setItem("ad.login", JSON.stringify(data));
      sessionStorage.setItem("ad.chatPage", "groups");
    }, login.data);
    const headers = {Authorization: "Bearer " + login.data.token};
    page = await context.newPage();
    page.setDefaultTimeout(15000);
    page.on("pageerror", error => report.pageErrors.push(error.message));
    await page.goto("/");
    const created = page.waitForResponse(response => matches(response, "POST", groupsPath));
    await submitGroupCreation(page, "广告搜索词诊断 · US");
    const conversation = (await (await created).json()).data.conversation_id;
    const api = `${groupsPath}/${conversation}/search-terms`;
    const cardLocator = page.locator('[data-result-kind="search_term"]');
    const productsBefore = businessSnapshot(db);
    const originalFacts = productsBefore.listing_test_facts[0].facts;
    const setFacts = change => {
      const next = JSON.parse(originalFacts);
      if (change) {
        const capacity = next.find(fact => fact.fact_id === "capacity");
        capacity.statement = contract.capacity_8;
        capacity.options = ["8 L"];
      }
      const originalVersion = productsBefore.listing_test_facts[0].version;
      db.prepare("UPDATE listing_test_facts SET facts=?, version=? WHERE sku='SP-LUNCH'")
        .run(change ? JSON.stringify(next) : originalFacts, originalVersion + Number(change));
    };
    let originalImport, supported;

    async function recordStep(name) {
      if (mode === "live") {
        await page.screenshot({path: file(name + ".png")});
        // Hold each recorded view long enough to read it. Assertions wait on the UI independently.
        await new Promise(resolve => setTimeout(resolve, 1700));
      }
    }

    async function checkCase(name, index, rerunButton) {
      const filename = files[name];
      const expected = contract.expected[name];
      const source = oracle[filename];
      const outcome = report.cases[name] = {expected};
      const before = businessSnapshot(db);
      if (!rerunButton) {
        await page.locator("#searchTermImport").click();
        const setup = page.locator("#searchTermSetup");
        const upload = page.waitForResponse(response => matches(response, "POST", api + "/reports"));
        await setup.locator('input[type="file"]').setInputFiles(path.join("tests", "fixtures", "search_terms", filename));
        const response = await upload;
        assert.equal(response.status(), 201);
        const imports = await (await context.request.get(api + "/reports", {headers})).json();
        const manifest = imports.find(report => report.filename === filename);
        assert.ok(manifest);
        assert.equal(manifest.sha256, source.sha256);
        if (name === "supported") originalImport = manifest.import_id;
        await expect(setup).toContainText("Amazon Sponsored Products Search Term Report");
        await expect(setup).toContainText("US · 2026-11-09 至 2026-11-10 · 逐日 · USD · 7 天点击归因 · 官方字段夹具 · 真实导出待联调");
        await expect(setup).toContainText(source.campaign);
        await expect(setup).toContainText(source.ad_group);
        if (name === "mixed") await expect(setup).toContainText("SP-CONTROL、SP-LUNCH · 归属待补");
        await expect(setup.locator(".search-term-overview dd")).toHaveText(
          ["impressions", "clicks", "orders"].map(key => source[key].toLocaleString("zh-CN")));
        await expect(setup.getByLabel("美国站授权店铺")).toHaveValue(manifest.shop_id);
        await setup.locator("#searchTermProduct").selectOption("SP-LUNCH");
        if (name === "supported") {
          const drawerBounds = await setup.boundingBox();
          const chatBounds = await page.locator(".workbench-conversation").boundingBox();
          assert.ok(drawerBounds.x >= chatBounds.x + chatBounds.width - 1);
          await page.screenshot({path: file("report-drawer.png")});
        }
        await setup.getByRole("button", {name: "确认范围"}).click();
        await expect(setup).toBeHidden();
        await expect(page.locator("#searchTermAttachmentLabel")).toHaveText("搜索词报表 · 11 月 9–10 日");
        await expect(page.locator("#searchTermImport")).toBeHidden();
        await expect(page.locator("#chatInput")).toBeFocused();
        if (name === "supported") {
          await page.screenshot({path: file("report-attached.png")});
          await page.locator("#searchTermAttachmentLabel").click();
          await expect(setup.locator("#searchTermProduct")).toHaveValue("SP-LUNCH");
          await page.setViewportSize({width: 390, height: 844});
          await expect(page.locator("#searchTermSetup:modal")).toBeVisible();
          await page.keyboard.press("Escape");
          await expect(setup).toBeHidden();
          await expect(page.locator("#searchTermAttachmentLabel")).toBeVisible();
          await page.setViewportSize({width: 1440, height: 1000});
          await page.getByRole("button", {name: "移除搜索词报表"}).click();
          await expect(page.locator("#searchTermAttachment")).toBeHidden();
          await page.locator("#searchTermImport").click();
          await setup.getByLabel("已导入版本").selectOption(manifest.import_id);
          await setup.locator("#searchTermProduct").selectOption("SP-LUNCH");
          await setup.getByRole("button", {name: "确认范围"}).click();
          await expect(setup).toBeHidden();
        }
        await page.locator("#chatInput").fill(contract.question);
        await recordStep("01-upload-confirm");
      }
      const start = performance.now();
      const rerunPath = `/runs/${report.cases.supported?.run_id}/rerun`;
      const completed = page.waitForResponse(
        response => matches(response, "POST", api + (rerunButton ? rerunPath : "/runs")), {timeout: 60000},
      );
      await (rerunButton || page.locator("#chatSend")).click();
      await expect(page.locator("#searchTermAttachment")).toBeHidden();
      const liveProgress = page.locator(".search-term-progress").last().locator('[data-stage="agent_started"]');
      await expect(liveProgress).toBeVisible();
      await recordStep("02-progress");
      const response = await completed;
      if (response.status() !== 200) {
        const error = await response.json().catch(() => null);
        assert.fail(`Diagnosis returned HTTP ${response.status()}: ${JSON.stringify(error)}`);
      }
      const receipt = await response.json();
      const savedQuestion = db.prepare("SELECT content FROM chat_group_entries WHERE run_id=? AND kind='user'")
        .get(receipt.run_id).content;
      assert.equal(savedQuestion, contract.question);
      const submitted = response.request().postDataJSON();
      if (rerunButton) {
        assert.equal(submitted, null);
        outcome.rerun = {status: response.status(), original_run_id: report.cases.supported.run_id,
          question: savedQuestion, import_id: originalImport, receipt};
      } else {
        assert.equal(submitted.message, contract.question);
      }
      const importId = rerunButton ? originalImport : submitted.import_id;
      outcome.duration_ms = Math.round(performance.now() - start);
      outcome.trace_id = receipt.trace_id;
      outcome.run_id = receipt.run_id;
      const stages = page.locator(`.search-term-progress[data-run-id="${receipt.run_id}"]`);
      await expect(stages).toContainText("结果已保存");
      outcome.stages = await stages.locator("li").allTextContents();
      await expect(cardLocator).toHaveCount(index + 1);
      const card = cardLocator.nth(index);
      await expect(page.locator(`[data-run-id="${receipt.run_id}"].msg.assistant .workbench-card`)).toHaveCount(1);
      await expect(card).toContainText("12L lunch bag");
      await expect(card).toContainText(actionLabel[expected.action]);
      await card.click();
      const side = page.locator("#workbenchDetail");
      const reportSection = side.getByText("报表原行与指标 · 2 行");
      const factsSection = side.getByText(/标题与商品原文 · 资料/);
      await reportSection.click();
      const itemPath = `${api}/runs/${receipt.run_id}/items/1`;
      const detail = await (await context.request.get(itemPath, {headers})).json();
      assert.equal(detail.report.sha256, source.sha256);
      assert.equal(detail.report.import_id, importId);
      assert.equal(detail.report.start_date, source.start_date);
      assert.equal(detail.report.end_date, source.end_date);
      assert.deepEqual(detail.rows.map(row => row.raw), source.raw_lines);
      assert.deepEqual(detail.rows.map(row => row.values), source.values);
      assert.deepEqual(detail.rows.map(row => [row.sheet, row.line]), [[source.sheet, 2], [source.sheet, 3]]);
      assert.equal(detail.report.attribution_days, 7);
      assert.equal(detail.report.granularity, "daily");
      for (const metric of metrics) assert.equal(detail.item.metrics[metric], source[metric], metric);
      assert.equal(detail.product.title, contract.title);
      assert.equal(detail.product.capacity_quote, name === "changed" ? contract.capacity_8 : contract.capacity_12);
      assert.equal(detail.item.action, scores[name].action);
      assert.equal(detail.item.code, scores[name].code);
      assert.equal(Number(Boolean(detail.draft)), expected.drafts);
      assert.equal(Number(Boolean(detail.item.review)), expected.reviews);
      assert.deepEqual(detail.rules, contract.rules);
      await expect(side.locator("pre")).toHaveCount(2);
      await expect(side).toContainText(source.sheet ? `${source.sheet} · 第 2 行` : source.raw_lines[0].trim());
      if (name !== "mixed") {
        assert.deepEqual(detail.item.ownership.map(row => row.membership_sha256), [membershipSha, membershipSha]);
        assert.equal(detail.item.ownership[0].matched_by, name === "low" ? "period_names" : "ids");
        await expect(side).toContainText(membershipSha);
      }
      await recordStep("03-original-report-rows");
      await factsSection.click();
      await expect(side).toContainText(detail.product.capacity_quote);
      await recordStep("04-original-product-facts");
      if (name === "changed") {
        assert.equal(detail.report.import_id, originalImport);
        assert.equal(detail.report.batch, supported.report.batch);
        assert.equal(detail.product.facts_version, supported.product.facts_version + 1);
        assert.equal(detail.product.title_version, supported.product.title_version);
        assert.deepEqual(detail.item.metrics, supported.item.metrics);
        await expect(card).toContainText("搜索需求为 12 L，商品原文核验容量为 8 L");
        await expect(side).toContainText("待人工复核 · 12L lunch bag · SP-101 / AG-101");
        outcome.rerun.capacity_original = detail.product.capacity_quote;
        outcome.rerun.batch = detail.report.batch;
      }
      if (name === "low") {
        await expect(card).toContainText("点击 15 / 门槛 20");
        assert.equal(detail.item.metrics.cvr_pct, supported.item.metrics.cvr_pct);
      }
      if (name === "mixed") {
        assert.equal(detail.item.attribution, "ad_group");
        await expect(side).toContainText("归属待补 · 广告组指标");
        await expect(side).toContainText("用户所选参考商品 · 搜索词归属待补");
      }
      const original = db.prepare("SELECT raw_csv FROM search_term_report_imports WHERE import_id=?").get(importId);
      assert.deepEqual(Buffer.from(original.raw_csv), await fs.readFile(path.join("tests", "fixtures", "search_terms", filename)));
      assert.deepEqual(businessSnapshot(db), before);
      outcome.receipt = {report_version: detail.report.version, report_sha256: detail.report.sha256,
        facts_version: detail.product.facts_version, title_version: detail.product.title_version};
      outcome.metrics = detail.item.metrics;
      outcome.rows = detail.rows.map(row => ({sheet: row.sheet, line: row.line}));
      outcome.ownership = detail.item.ownership;
      outcome.result = {action: detail.item.action, code: detail.item.code, attribution: detail.item.attribution,
        reason: detail.item.reason, drafts: Number(Boolean(detail.draft)), reviews: Number(Boolean(detail.item.review))};
      outcome.passed = true;
      return detail;
    }

    supported = await checkCase("supported", 0);
    const side = page.locator("#workbenchDetail");
    const title = side.locator("#searchTermTitleEdit");
    const copy = side.getByRole("button", {name: "复制已保存版本"});
    const verify = side.getByRole("button", {name: "校验并保存"});
    const approvePath = `${api}/runs/${report.cases.supported.run_id}/items/1`;
    assert.equal(supported.draft.title, contract.title + ", 12 L");
    await expect(side.locator("del")).toHaveText(contract.title);
    await title.fill(contract.valid_edit);
    await expect(side.locator("#searchTermTitleCount")).toContainText("75 / 75 字符（含空格）");
    await expect(copy).toBeDisabled();
    const accepted75 = page.waitForResponse(response => matches(response, "PUT", approvePath + "/draft"));
    await verify.click();
    const save75 = await receipt(await accepted75);
    assert.deepEqual(save75, {status: 200, body: {title: contract.valid_edit, revision: 2}});
    await expect(side.locator("ins")).toHaveText(contract.valid_edit);
    await copy.click();
    await expect(side.getByRole("status")).toContainText("已复制 v2");
    assert.equal(await page.evaluate(() => navigator.clipboard.readText()), contract.valid_edit);
    await recordStep("05-edited-copied");
    const validation = [];
    for (const [invalid, message] of [[contract.over_limit, "标题 76 字符，超过 75 字符上限"],
      ["Trailbox Insulated Lunch Bag, 18 L", "已核验容量 12 L"]]) {
      await title.fill(invalid);
      if (invalid === contract.over_limit) {
        await expect(side.locator("#searchTermTitleCount")).toContainText("76 / 75 字符");
        await expect(side.locator("#searchTermTitleCount")).toContainText("超限");
      }
      const rejected = page.waitForResponse(response => matches(response, "PUT", approvePath + "/draft"));
      await verify.click();
      const rejection = await receipt(await rejected);
      assert.equal(rejection.status, 422);
      await expect(side.getByRole("alert")).toContainText(message);
      const copied = page.waitForResponse(response => matches(response, "GET", approvePath + "/draft/copy"));
      await copy.click();
      const checkedCopy = await receipt(await copied);
      assert.equal(checkedCopy.status, 200);
      await expect(side.getByRole("status")).toContainText("已复制 v2");
      assert.equal(await page.evaluate(() => navigator.clipboard.readText()), contract.valid_edit);
      validation.push({length: invalid.length, rejection, checkedCopy, message: await side.getByRole("alert").innerText()});
    }
    await recordStep("06-invalid-edits");
    assert.deepEqual(businessSnapshot(db), productsBefore);
    await page.reload();
    await cardLocator.first().click();
    await expect(title).toHaveValue(contract.valid_edit);
    await side.getByText("报表原行与指标 · 2 行").click();
    await side.getByText(/标题与商品原文 · 资料/).click();
    await expect(side).toContainText(contract.capacity_12);
    await expect(side).toContainText("Daily · 第 2 行");
    await expect(side).toContainText(contract.rules.version);
    await side.locator("ins").scrollIntoViewIfNeeded();
    const restored = await (await context.request.get(approvePath, {headers})).json();
    assert.deepEqual(restored.draft, {title: contract.valid_edit, revision: 2});
    assert.deepEqual(restored.rows, supported.rows);
    assert.deepEqual(restored.product, supported.product);
    assert.equal(restored.entry_id, supported.entry_id);
    await page.evaluate(() => navigator.clipboard.writeText("clipboard before valid copy"));
    const reopenedCopy = page.waitForResponse(response => matches(response, "GET", approvePath + "/draft/copy"));
    await copy.click();
    const copyReceipt = await receipt(await reopenedCopy);
    await expect(side.getByRole("status")).toContainText("已复制 v2");
    const clipboardAfterRefresh = await page.evaluate(() => navigator.clipboard.readText());
    assert.deepEqual(copyReceipt, {status: 200, body: restored.draft});
    assert.equal(clipboardAfterRefresh, restored.draft.title);
    report.checks.edit_copy_refresh = {passed: true, save75, validation, copyReceipt, clipboardAfterRefresh, saved_version: restored.draft,
      raw_rows_identical: true, original_facts_identical: true, business_state_identical: true};
    await recordStep("07-refresh-reopen");

    async function receipt(response) {
      return {status: response.status(), body: await response.json()};
    }

    async function checkStaleness(name, expectedReasons, baseline = false) {
      const before = businessSnapshot(db);
      const historicalBefore = await receipt(await context.request.get(approvePath, {headers}));
      assert.equal(historicalBefore.status, 200);
      assert.deepEqual(historicalBefore.body, restored);
      const draftRow = () => ({...db.prepare(
        "SELECT revision, title FROM search_term_title_drafts WHERE run_id=? AND item_id=1",
      ).get(report.cases.supported.run_id)});
      const draftBefore = draftRow();
      const copyPath = approvePath + "/draft/copy?revision=" + draftBefore.revision;
      const editPath = approvePath + "/draft";
      const clipboard = () => page.evaluate(() => navigator.clipboard.readText());
      const marker = "clipboard before " + name;
      await page.reload();
      await cardLocator.first().click();
      await expect(title).toHaveValue(contract.valid_edit);
      await side.getByText("报表原行与指标 · 2 行").click();
      await side.getByText(/标题与商品原文 · 资料/).click();
      await expect(side).toContainText(contract.capacity_12);
      await expect(side).toContainText(contract.rules.version);
      await expect(side.locator("del")).toHaveText(contract.title);
      await expect(side.locator("pre")).toHaveCount(2);
      await page.evaluate(value => navigator.clipboard.writeText(value), marker);
      const directCopy = await receipt(await context.request.get(copyPath, {headers}));
      const directEdit = await receipt(await context.request.put(editPath, {headers,
        data: {title: draftBefore.title, revision: draftBefore.revision}}));
      const uiCopyResponse = page.waitForResponse(response => matches(response, "GET", approvePath + "/draft/copy"));
      await copy.click();
      const uiCopy = await receipt(await uiCopyResponse);
      if (baseline) await expect(side.getByRole("status")).toContainText("已复制 v2");
      else {
        await expect(side.getByRole("alert")).toContainText("商品资料或标题已更新，请重新诊断");
        await expect(side.getByRole("button", {name: "重新诊断"})).toBeEnabled();
      }
      const afterCopy = await clipboard();
      // Reopen so the save action also exercises the server response in Edge.
      await side.getByRole("button", {name: "关闭"}).click();
      await cardLocator.first().click();
      await title.fill(supported.draft.title);
      const uiEditResponse = page.waitForResponse(response => matches(response, "PUT", editPath));
      await verify.click();
      const uiEdit = await receipt(await uiEditResponse);
      const snapshot = name + "-edge.png";
      if (baseline) await expect(side.locator("ins")).toHaveText(supported.draft.title);
      else {
        await expect(side.getByRole("alert")).toContainText("商品资料或标题已更新，请重新诊断");
        const reasonLabels = {facts_changed: "商品资料已更新", title_changed: "原标题已更新",
          product_unavailable: "当前 SKU 的商品资料不可用"};
        for (const reason of expectedReasons) await expect(side.getByRole("alert")).toContainText(reasonLabels[reason]);
        await expect(side.getByRole("button", {name: "重新诊断"})).toBeEnabled();
        await expect(side.locator("ins")).toHaveText(draftBefore.title);
      }
      await page.screenshot({path: file(snapshot)});
      const draftAfter = draftRow();
      const afterEdit = await clipboard();
      const historicalAfter = await receipt(await context.request.get(approvePath, {headers}));
      const businessUnchanged = JSON.stringify(businessSnapshot(db)) === JSON.stringify(before);
      report.checks[name] = {directEdit, directCopy, uiEdit, uiCopy, draftBefore, draftAfter, historicalBefore,
        historicalAfter, businessUnchanged, clipboard: {before: marker, afterCopy, afterEdit}, snapshot};
      for (const http of [directEdit, directCopy, uiEdit, uiCopy]) {
        assert.equal(http.status, baseline ? 200 : 409);
        if (!baseline) {
          assert.equal(http.body.detail.code, "stale_title_draft");
          assert.deepEqual(http.body.detail.reasons, expectedReasons);
        }
      }
      assert.equal(afterCopy, baseline ? draftBefore.title : marker);
      assert.equal(afterEdit, afterCopy);
      assert.deepEqual(draftAfter, baseline ? {title: supported.draft.title, revision: 3} : draftBefore);
      assert.equal(businessUnchanged, true);
      assert.deepEqual(historicalAfter.body, {...restored, draft: draftAfter});
    }

    if (mode === "baseline") {
      setFacts(true);
      await checkStaleness("capacity-change", ["facts_changed"], true);
    }

    if (mode === "fixed") {
      const stale = await context.request.put(approvePath + "/draft", {headers, data: {title: supported.draft.title, revision: 1}});
      assert.equal(stale.status(), 409);
      assert.equal((await context.request.get(approvePath)).status(), 401);
      const otherLogin = await (await context.request.post("/api/login", {data: {
        username: "sp-test-other", password: process.env.SEARCH_TERMS_PASSWORD,
      }})).json();
      assert.equal(otherLogin.code, 200);
      const otherHeaders = {Authorization: "Bearer " + otherLogin.data.token};
      for (const resource of [approvePath, api + "/reports/" + originalImport]) {
        assert.equal((await context.request.get(resource, {headers: otherHeaders})).status(), 404);
      }
      const forged = await context.request.post(api + "/runs", {headers, data: {
        import_id: originalImport, sku: "different-shop-sku", message: contract.question,
      }});
      assert.equal(forged.status(), 422);
      report.checks.ownership_and_scope = true;
      await side.getByRole("button", {name: "关闭"}).click();
      setFacts(true);
      await checkStaleness("capacity-change", ["facts_changed"]);
      const changed = await checkCase("changed", 1, side.getByRole("button", {name: "重新诊断"}));
      const rerunPath = `${api}/runs/${report.cases.supported.run_id}/rerun`;
      assert.equal((await context.request.post(rerunPath, {headers: otherHeaders})).status(), 404);
      const historical = await (await context.request.get(approvePath, {headers})).json();
      assert.deepEqual(historical, restored);
      assert.equal(changed.item.review.status, "pending");
      await page.screenshot({path: file("rerun-8l-edge.png")});
      await side.getByRole("button", {name: "关闭"}).click();
      setFacts(false);
      const updateTitle = db.prepare("UPDATE listing_test_products SET title=? WHERE sku='SP-LUNCH'");
      const changedTitle = contract.title + ".";
      updateTitle.run(changedTitle);
      await checkStaleness("title-text-only", ["title_changed"]);
      report.checks["title-text-only"].currentTitle = changedTitle;
      report.checks["title-text-only"].currentTitleVersion = db.prepare(
        "SELECT version FROM listing_test_products WHERE sku='SP-LUNCH'",
      ).get().version;
      assert.equal(report.checks["title-text-only"].currentTitleVersion, supported.product.title_version);
      assert.deepEqual(businessSnapshot(db).listing_test_facts, productsBefore.listing_test_facts);
      updateTitle.run(contract.title);
      await side.getByRole("button", {name: "关闭"}).click();
      const shopOwner = db.prepare("UPDATE listing_test_shops SET user_id=? WHERE shop_id='search-terms-test' AND marketplace_id='US'");
      shopOwner.run(18);
      try {
        const deniedCopy = await receipt(await context.request.get(approvePath + "/draft/copy?revision=2", {headers}));
        const deniedEdit = await receipt(await context.request.put(approvePath + "/draft", {headers,
          data: {title: supported.draft.title, revision: 2}}));
        for (const denied of [deniedEdit, deniedCopy]) {
          assert.equal(denied.status, 404);
        }
        assert.equal((await context.request.post(rerunPath, {headers})).status(), 404);
        report.checks.shop_reauthorization = {deniedEdit, deniedCopy};
      } finally {
        shopOwner.run(17);
      }
      const listingTables = ["search_term_ad_groups", "listing_test_facts", "listing_test_products"];
      db.exec("BEGIN");
      const removed = listingTables.map(table => [table, db.prepare(
        `DELETE FROM ${table} WHERE shop_id='search-terms-test' AND marketplace_id='US' AND sku='SP-LUNCH' RETURNING rowid, *`,
      ).all()]);
      db.exec("COMMIT");
      try {
        await checkStaleness("product-unavailable", ["product_unavailable"]);
      } finally {
        db.exec("BEGIN");
        for (const [table, rows] of removed.reverse()) {
          for (const row of rows) {
            const keys = Object.keys(row);
            db.prepare(`INSERT INTO ${table} (${keys.join(", ")}) VALUES (${keys.map(() => "?").join(", ")})`)
              .run(...Object.values(row));
          }
        }
        db.exec("COMMIT");
      }
      await side.getByRole("button", {name: "关闭"}).click();
      await checkCase("low", 2);
      await side.getByRole("button", {name: "关闭"}).click();
      const mixed = await checkCase("mixed", 3);
      await page.reload();
      await cardLocator.nth(3).click();
      await expect(side).toContainText("归属待补 · 广告组指标");
      await side.getByText("报表原行与指标 · 2 行").click();
      await expect(side.locator("pre")).toHaveCount(2);
      const mixedPath = `${api}/runs/${report.cases.mixed.run_id}/items/1`;
      const mixedReloaded = await receipt(await context.request.get(mixedPath, {headers}));
      assert.deepEqual(mixedReloaded, {status: 200, body: mixed});
      assert.equal(mixedReloaded.body.draft, null);
      assert.deepEqual(mixedReloaded.body.item.ownership, []);
      assert.deepEqual((await receipt(await context.request.get(approvePath, {headers}))).body, restored);
      report.checks.mixed_refresh = {groupMetrics: mixed.item.metrics, noRowOwnership: true, noDraft: true,
        historicalCardAndMixedSnapshotsIdentical: true};
      await side.getByRole("button", {name: "关闭"}).click();
      await page.locator("#searchTermImport").click();
      const setup = page.locator("#searchTermSetup");
      const previews = {};
      for (const name of ["amazon-daily.csv", "amazon-summary.xlsx", "amazon-summary.csv", "report.csv"]) {
        const upload = page.waitForResponse(response => matches(response, "POST", api + "/reports"));
        await setup.locator('input[type="file"]').setInputFiles(path.join("tests", "fixtures", "search_terms", name));
        const response = await upload;
        assert.equal(response.status(), 201);
        const imports = await (await context.request.get(api + "/reports", {headers})).json();
        const manifest = imports.find(report => report.filename === name);
        assert.ok(manifest);
        assert.equal(manifest.sha256, oracle[name].sha256);
        if (name === "report.csv") {
          await expect(setup).toContainText("Sponsored Products Search Term Test");
          continue;
        }
        await expect(setup).toContainText(manifest.granularity === "daily" ? "逐日 · USD" : "汇总 · USD");
        await expect(setup).toContainText("曝光 320 · 点击 30 · 成交 4 · 花费 $21.00 · 销售额 $99.96");
        for (const metric of Object.keys(manifest.totals)) assert.equal(manifest.totals[metric], oracle[name][metric]);
        previews[name] = {granularity: manifest.granularity, totals: manifest.totals, rows: manifest.row_count};
      }
      const invalidUpload = page.waitForResponse(response => matches(response, "POST", api + "/reports"));
      await setup.locator('input[type="file"]').setInputFiles({
        name: "missing.csv", mimeType: "text/csv",
        buffer: Buffer.from("Date,Campaign Name,Ad Group Name,Currency,Impressions,Clicks,Spend,Purchases,Sales\n"),
      });
      assert.equal((await invalidUpload).status(), 422);
      await expect(setup).toContainText("第 1 行缺必填列 Customer Search Term");
      report.checks.import_previews = previews;
      assert.equal(db.prepare("SELECT count(*) AS n FROM search_term_report_imports").get().n, 7);
      assert.equal(db.prepare("SELECT count(*) AS n FROM search_term_title_drafts").get().n, 1);
      const productsAfter = businessSnapshot(db);
      assert.deepEqual(productsAfter, productsBefore);
      const hashSnapshot = snapshot => createHash("sha256").update(JSON.stringify(snapshot)).digest("hex");
      report.checks.independent_sql = {tables: Object.keys(productsBefore),
        before_sha256: hashSnapshot(productsBefore), after_sha256: hashSnapshot(productsAfter)};
      report.checks.stale_edit_and_historical_versions = true;
    }
    assert.deepEqual(report.pageErrors, []);
  } catch (error) {
    report.failure = String(error.stack || error);
    if (page) await page.screenshot({path: file("initial-deviation.png")}).catch(() => {});
    throw error;
  } finally {
    await fs.writeFile(file("browser-result.json"), JSON.stringify(report, null, 2));
    const video = page?.video();
    try {
      await context?.close();
      if (video) {
        const recorded = await video.path();
        await fs.rename(recorded, file("edge-replay.webm"));
      }
    } finally {
      db.close();
      await browser?.close();
    }
  }
}

run().catch(error => {
  process.stderr.write(String(error.stack || error));
  process.exitCode = 1;
});
