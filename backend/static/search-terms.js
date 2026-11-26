const searchTermActions = {title: "建议改标题", review: "复核投放", observe: "继续观察", incomplete: "诊断未完成"};
let searchTermSetup = null;
let searchTermAttachment = null;

async function searchTermRequest(conversationId, path, options) {
  const response = await fetch("/api/chat/conversations/" + encodeURIComponent(conversationId) + "/search-terms" + path, {
    ...options, headers: {Authorization: "Bearer " + TOKEN, ...options?.headers},
  });
  const data = await response.json().catch(error => {
    if (response.ok) throw error;
    return {};
  });
  if (!response.ok) {
    const detail = data.detail;
    const message = Array.isArray(detail)
      ? detail.map(error => typeof error === "string" ? error : error.msg).join("\n")
      : typeof detail === "string" ? detail : detail?.message;
    const error = new Error(message || "搜索词请求失败");
    error.detail = detail;
    error.submissionRejected = response.status < 500;
    throw error;
  }
  return data;
}

function searchTermButton(parent, label, primary = false) {
  const button = groupText(parent, "button", label);
  button.type = "button";
  button.className = "btn btn-sm " + (primary ? "btn-primary" : "btn-outline");
  return button;
}

function closeSearchTermSetup(restoreFocus = false) {
  const state = searchTermSetup;
  if (!state) return;
  state.controller.abort();
  searchTermSetup = null;
  byId("searchTermSetup").close();
  byId("searchTermSetupBody").replaceChildren();
  state.trigger.setAttribute("aria-expanded", "false");
  if (restoreFocus) state.trigger.focus({preventScroll: true});
}

function searchTermSelectionFor(conversationId) {
  return searchTermAttachment?.conversationId === conversationId ? searchTermAttachment.selection : null;
}

function attachSearchTermReport(attachment) {
  searchTermAttachment = attachment;
  const label = byId("searchTermAttachmentLabel");
  label.textContent = attachment ? "搜索词报表 · " + groupCompactWindow(attachment.report) : "";
  label.title = attachment?.report.filename || "";
  byId("searchTermAttachment").hidden = !attachment;
  byId("searchTermImport").hidden = Boolean(attachment);
  const input = byId("chatInput");
  if (!input.dataset.defaultPlaceholder) input.dataset.defaultPlaceholder = input.placeholder;
  input.placeholder = attachment ? "输入你想诊断的搜索词问题…" : input.dataset.defaultPlaceholder;
}

function resetSearchTermSetup() {
  closeSearchTermSetup();
  attachSearchTermReport(null);
}

document.getElementById("searchTermSetupClose").onclick = () => closeSearchTermSetup(true);
document.getElementById("searchTermSetup").addEventListener("cancel", event => {
  event.preventDefault();
  closeSearchTermSetup(true);
});
document.getElementById("searchTermDetach").onclick = () => {
  resetSearchTermSetup();
  byId("chatInput").focus();
};

function reportDescription(report) {
  const provenance = report.format
    ? `${report.granularity === "daily" ? "逐日" : "汇总"} · ${report.currency} · ${report.attribution_days} 天点击归因 · ${report.source_note}`
    : "测试数据";
  return `${report.source} · ${report.marketplace} · ${report.start_date} 至 ${report.end_date} · ${provenance} · 导入 v${report.version} · ${report.row_count} 行`;
}

async function openSearchTermSetup(trigger) {
  if (searchTermSetup) return closeSearchTermSetup(true);
  const conversationId = activeChatGroup;
  if (!conversationId) return;
  closeGroupDetail(false);
  const controller = new AbortController();
  const signal = controller.signal;
  const selected = searchTermSelectionFor(conversationId);
  searchTermSetup = {controller, trigger};
  const dialog = byId("searchTermSetup");
  const panel = byId("searchTermSetupBody");
  trigger.setAttribute("aria-expanded", "true");
  const fileLabel = groupText(panel, "label", "");
  fileLabel.className = "search-term-file";
  workbenchCardIcon(fileLabel, "evidence");
  const fileText = groupText(fileLabel, "span", "");
  fileText.className = "search-term-file-text";
  const filename = groupText(fileText, "strong", "选择报表");
  const fileType = groupText(fileText, "small", "Sponsored Products · Excel / CSV");
  const replaceFile = groupText(fileLabel, "span", "更换");
  replaceFile.className = "search-term-replace-file";
  replaceFile.hidden = true;
  const input = groupText(fileLabel, "input", "");
  input.className = "search-term-file-input";
  input.type = "file";
  input.setAttribute("aria-label", "选择 Sponsored Products 报表");
  input.accept = ".xlsx,.csv";
  const feedback = groupText(panel, "p", "");
  feedback.className = "search-term-setup-feedback";
  feedback.setAttribute("role", "status");
  const metrics = groupText(panel, "dl", "");
  metrics.className = "search-term-overview";
  metrics.hidden = true;
  const scope = groupText(panel, "section", "");
  scope.className = "search-term-scope";
  groupText(scope, "h3", "报表范围");
  const fields = groupText(scope, "div", "");
  fields.className = "search-term-scope-fields";
  const metadata = groupText(fields, "dl", "");
  metadata.className = "search-term-scope-summary";
  metadata.hidden = true;
  const shopLabel = groupText(fields, "label", "店铺");
  const shop = groupText(shopLabel, "select", "");
  shop.setAttribute("aria-label", "美国站授权店铺");
  groupText(shop, "option", "选择店铺").value = "";
  shopLabel.hidden = true;
  const historyLabel = groupText(fields, "label", "版本");
  const history = groupText(historyLabel, "select", "");
  history.setAttribute("aria-label", "已导入版本");
  groupText(history, "option", "选择报表").value = "";
  historyLabel.hidden = true;
  const preview = groupText(panel, "div", "");
  let requestNumber = 0;
  let currentReport = null;

  function addReport(report) {
    if (Array.from(history.options).some(option => option.value === report.import_id)) return;
    const grain = report.granularity === "daily" ? "逐日 · " : report.granularity === "summary" ? "汇总 · " : "";
    groupText(history, "option", `v${report.version} · ${grain}${report.filename}`).value = report.import_id;
    historyLabel.hidden = false;
  }

  function syncShop() {
    const hasShop = currentReport && [...shop.options].some(option => option.value && option.value === currentReport.shop_id);
    shopLabel.hidden = shop.options.length <= 1 || Boolean(currentReport && !hasShop);
    if (hasShop) shop.value = currentReport.shop_id;
  }

  function clearPreview() {
    currentReport = null;
    filename.textContent = "选择报表";
    fileType.textContent = "Sponsored Products · Excel / CSV";
    replaceFile.hidden = true;
    fileLabel.classList.remove("has-report");
    metrics.replaceChildren();
    metrics.hidden = true;
    metadata.replaceChildren();
    metadata.hidden = true;
    preview.replaceChildren();
    syncShop();
  }

  function showReport({report, products}) {
    currentReport = report;
    addReport(report);
    history.value = report.import_id;
    filename.textContent = report.filename;
    fileType.textContent = report.granularity === "daily" ? "Sponsored Products · 逐日报表" :
      report.granularity === "summary" ? "Sponsored Products · 汇总报表" : "Sponsored Products · 测试报表";
    replaceFile.hidden = false;
    fileLabel.classList.add("has-report");
    const totals = [["曝光", report.totals?.impressions], ["点击", report.totals?.clicks], ["成交", report.totals?.orders]];
    if (totals.every(([, value]) => Number.isFinite(value))) {
      for (const [label, value] of totals) {
        const metric = groupText(metrics, "div", "");
        groupText(metric, "dt", label);
        groupText(metric, "dd", value.toLocaleString("zh-CN"));
      }
      metrics.hidden = false;
    }
    metadata.hidden = false;
    syncShop();
    for (const [key, value] of [
      ["站点", report.marketplace === "US" ? "美国站" : report.marketplace],
      ["日期", groupCompactWindow(report)],
      ...(shopLabel.hidden && report.shop_id ? [["报表店铺", report.shop_id]] : []),
      ...(report.groups.some(scope => scope.has_unmapped_rows && (report.format || scope.advertised_skus.length > 1))
        ? [["归属", "广告组 · 归属待补"]] : []),
    ]) {
      const row = groupText(metadata, "div", "");
      groupText(row, "dt", key);
      groupText(row, "dd", value);
    }
    const actions = groupText(preview, "div", "");
    actions.className = "search-term-setup-fields";
    const productLabel = groupText(actions, "label", report.format ? "参考商品" : "确认要核对的测试商品");
    const product = groupText(productLabel, "select", "");
    product.id = "searchTermProduct";
    groupText(product, "option", report.format ? "仅查看广告组指标" : "请选择商品").value = "";
    for (const item of products) groupText(product, "option", `${item.sku} · ${item.title}`).value = item.sku;
    if (report.verified_skus?.length === 1) product.value = report.verified_skus[0];
    let batch;
    if (report.batch_count > 1) {
      batch = groupText(groupText(actions, "label", "搜索词批次"), "select", "");
      for (let index = 1; index <= report.batch_count; index++) {
        groupText(batch, "option", `${index} / ${report.batch_count}`).value = index;
      }
    }
    if (selected?.import_id === report.import_id) {
      const sku = selected.sku || "";
      if ([...product.options].some(option => option.value === sku)) product.value = sku;
      if (batch && selected.batch <= batch.options.length) batch.value = String(selected.batch);
    }
    const footer = groupText(actions, "div", "");
    footer.className = "search-term-setup-actions";
    const info = groupText(footer, "details", "");
    info.className = "search-term-report-info";
    groupText(info, "summary", "报表明细");
    groupText(info, "p", reportDescription(report));
    if (report.totals) {
      const {impressions, clicks, orders, spend_cents, sales_cents} = report.totals;
      groupText(info, "p", `${report.shop_id} · 曝光 ${impressions} · 点击 ${clicks} · 成交 ${orders} · 花费 $${(spend_cents / 100).toFixed(2)} · 销售额 $${(sales_cents / 100).toFixed(2)}`);
    }
    const scopes = groupText(info, "ul", "");
    for (const item of report.groups) {
      const gap = item.has_unmapped_rows && (report.format || item.advertised_skus.length > 1) ? " · 归属待补" : "";
      groupText(scopes, "li", searchTermScope(item) + (item.advertised_skus.length ? " · " + item.advertised_skus.join("、") : "") + gap);
    }
    const confirm = searchTermButton(footer, "确认范围", true);
    function updateConfirmation() {
      confirm.disabled = !report.format && !product.value;
    }
    updateConfirmation();
    product.addEventListener("change", updateConfirmation);
    confirm.addEventListener("click", () => {
      const selection = {import_id: report.import_id, sku: product.value || null, batch: Number(batch?.value || 1)};
      closeSearchTermSetup();
      attachSearchTermReport({conversationId, report, selection});
      byId("chatInput").focus();
    });
  }

  async function loadPreview(path, options) {
    const number = ++requestNumber;
    clearPreview();
    feedback.textContent = "正在读取报表…";
    try {
      const result = await searchTermRequest(conversationId, path, {...options, signal});
      if (signal.aborted || number !== requestNumber) return;
      feedback.textContent = "";
      showReport(result);
    } catch (error) {
      if (!signal.aborted && number === requestNumber) feedback.textContent = error.message;
    }
  }

  input.addEventListener("change", () => {
    const file = input.files[0];
    if (file) loadPreview("/reports?" + new URLSearchParams({filename: file.name, shop_id: shop.value}), {
      method: "POST", body: file, headers: {"Content-Type": "application/octet-stream"},
    });
    input.value = "";
  });
  history.addEventListener("change", () => {
    if (history.value) return loadPreview("/reports/" + encodeURIComponent(history.value));
    requestNumber++;
    clearPreview();
    feedback.textContent = "";
  });
  shop.addEventListener("change", () => {
    requestNumber++;
    history.value = "";
    clearPreview();
    feedback.textContent = "";
  });
  if (compactWorkbench.matches) dialog.showModal();
  else dialog.show();
  input.focus();
  try {
    const [previous, shops] = await Promise.all([
      searchTermRequest(conversationId, "/reports", {signal}),
      searchTermRequest(conversationId, "/shops", {signal}),
    ]);
    if (!signal.aborted) {
      previous.forEach(addReport);
      for (const entry of shops) groupText(shop, "option", entry.shop_id).value = entry.shop_id;
      if (shops.length === 1 && !currentReport) shop.value = shops[0].shop_id;
      syncShop();
      if (selected && requestNumber === 0) await loadPreview("/reports/" + encodeURIComponent(selected.import_id));
    }
  } catch (error) {
    if (!signal.aborted && requestNumber === 0) feedback.textContent = error.message;
  }
}

function submitSearchTermRun(conversationId, path, options) {
  return submitGroupRequest(conversationId, key => postSearchTermRun(conversationId, path, options, key));
}

async function postSearchTermRun(conversationId, path, options, key) {
  const view = chatViewId;
  const pending = {};
  groupPending.set(conversationId, pending);
  updateChatGroupControls();
  try {
    const response = searchTermRequest(conversationId, path, {
      ...options, method: "POST", signal: groupSession.signal,
      headers: {...options?.headers, "Idempotency-Key": key},
    });
    readChatGroupHistory();
    const result = await response;
    if (!result?.run_id) throw new Error("未收到诊断运行回执");
    return result;
  } finally {
    if (groupPending.get(conversationId) === pending) groupPending.delete(conversationId);
    if (view === chatViewId && activeChatGroup === conversationId && TOKEN) await readChatGroupHistory();
  }
}

async function sendSearchTermDiagnosis(message) {
  const conversationId = activeChatGroup;
  const confirmed = searchTermSelectionFor(conversationId);
  if (!confirmed || groupPending.has(conversationId)) return;
  const view = chatViewId;
  const input = byId("chatInput");
  input.value = "";
  resetSearchTermSetup();
  try {
    await submitSearchTermRun(conversationId, "/runs", {
      headers: {"Content-Type": "application/json"}, body: JSON.stringify({...confirmed, message}),
    });
  } catch (error) {
    if (error.submissionRejected && view === chatViewId && activeChatGroup === conversationId && TOKEN) {
      input.value = message;
    }
  }
}

function buildSearchTermProgress(events, run) {
  const stages = events.filter(entry => entry.event?.kind === "search_term_stage");
  if (!stages.length) return null;
  const section = document.createElement("section");
  section.className = "workbench-delegation search-term-progress";
  section.dataset.runId = run.run_id;
  section.setAttribute("aria-label", "搜索词诊断进度");
  groupText(section, "strong", "搜索词诊断进度");
  const list = groupText(section, "ol", "");
  for (const stage of stages) groupText(list, "li", stage.content).dataset.stage = stage.event.stage;
  if (run.status === "completed") groupText(list, "li", "结果已保存");
  if (run.status === "stopped") groupText(list, "li", "本轮已停止");
  list.setAttribute("aria-live", "polite");
  return section;
}

function appendSearchTermCards(bubble, conversationId, entry, run) {
  if (!entry.search_terms?.length) return;
  const cards = groupText(bubble, "div", "");
  cards.className = "workbench-cards";
  for (const item of entry.search_terms) {
    addGroupCard(cards, {conversationId, entry, run, kind: "search_term", termItemId: item.id},
      item.term, item.reason, "查看依据", searchTermActions[item.action]);
  }
}

function searchTermScope(scope) {
  const named = (name, id) => id ? `${name} (${id})` : name;
  return `${named(scope.campaign, scope.campaign_id)} / ${named(scope.ad_group, scope.ad_group_id)}`;
}

function searchTermEvidence(body, result) {
  const {report, item, product, rows, historical_rows} = result;
  groupText(body, "p", reportDescription(report));
  groupText(body, "p", searchTermScope(item.scope));
  if (report.format) groupText(body, "p", `${report.filename} · SHA-256 ${report.sha256}`);
  groupText(body, "p", `标题规则 ${result.rules.version}`);
  const label = item.attribution === "product" ? "已核验商品归属 · " + product.target.sku :
    item.attribution === "ad_group" ? "归属待补 · 广告组指标" : "广告组指标无法核验";
  groupText(body, "p", label);
  const reason = item.reason.startsWith(searchTermActions[item.action]) ? item.reason :
    searchTermActions[item.action] + " · " + item.reason;
  groupText(body, "p", reason);
  if (item.metrics_unavailable_reason && !reason.includes(item.metrics_unavailable_reason)) {
    groupText(body, "p", item.metrics_unavailable_reason);
  }
  if (item.demand) {
    const {product_type, attribute, value, unit, conditions} = item.demand;
    groupText(body, "p", ["搜索需求", product_type, attribute, [value, unit].filter(Boolean).join(" "),
      ...Object.values(conditions).filter(Boolean)].join(" · "));
  }
  if (item.metrics) searchTermReportRows(body, "报表原行与指标", item.metrics, rows, item.ownership);
  if (item.historical && historical_rows?.length) {
    const history = item.historical;
    const title = history.attribution === "product" ? `历史商品指标 · SKU ${history.sku}` :
      "历史广告组指标";
    searchTermReportRows(body, title, history.metrics, historical_rows);
  }
  if (!product) return;
  searchTermProductEvidence(body, item, product);
}

function searchTermReportRows(body, label, metrics, rows, proofs = []) {
  const number = value => value == null ? "—" : value.toLocaleString("en-US");
  const dollars = cents => "$" + (cents / 100).toFixed(2);
  const percent = value => value == null ? "—" : number(value) + "%";
  const originals = groupText(body, "details", "");
  originals.className = "search-term-evidence";
  groupText(originals, "summary", `${label} · ${rows.length} 行`);
  groupText(originals, "p", `曝光 ${number(metrics.impressions)} · 点击 ${number(metrics.clicks)} · 成交 ${number(metrics.orders)} · 花费 ${dollars(metrics.spend_cents)} · 销售额 ${dollars(metrics.sales_cents)}`);
  groupText(originals, "p", `CTR ${percent(metrics.ctr_pct)} · 转化率 ${percent(metrics.cvr_pct)} · CPC ${metrics.cpc_usd == null ? "—" : dollars(Math.round(metrics.cpc_usd * 100))} · ACoS ${percent(metrics.acos_pct)}`);
  const ownership = new Map(proofs.map(entry => [JSON.stringify([entry.sheet, entry.line]), entry]));
  for (const row of rows) {
    if (row.normalized) {
      const {start_date, end_date} = row.normalized;
      const location = row.sheet ? `${row.sheet} · ` : "";
      groupText(originals, "strong", `${location}第 ${row.line} 行 · ${start_date}${start_date === end_date ? "" : " 至 " + end_date}`);
      groupText(originals, "pre", row.raw ?? Object.entries(row.values).map(([column, value]) => `${column}\t${value ?? ""}`).join("\n"));
      const proof = ownership.get(JSON.stringify([row.sheet, row.line]));
      if (proof) {
        const match = proof.matched_by === "ids" ? "活动与广告组 ID" : "报表周期内唯一名称";
        groupText(originals, "p", `${match} · ${proof.sku}${proof.asin ? " / " + proof.asin : ""} · ${proof.membership_location} · SHA-256 ${proof.membership_sha256}`);
        if (proof.row_link_sha256) groupText(originals, "p", `逐行映射 · ${proof.row_link_location} · SHA-256 ${proof.row_link_sha256}`);
      }
      continue;
    }
    groupText(originals, "strong", `第 ${row.line} 行 · ${row.values.Date}`);
    groupText(originals, "pre", row.raw);
    const values = row.values;
    groupText(originals, "p", `曝光 ${values.Impressions} · 点击 ${values.Clicks} · 成交 ${values.Orders} · 花费 $${values["Spend USD"]} · 销售额 $${values["Sales USD"]}`);
  }
}

function searchTermProductEvidence(body, item, product) {
  const sources = groupText(body, "details", "");
  sources.className = "search-term-evidence";
  const version = product.document_version ?? product.facts_version;
  groupText(sources, "summary", "本轮原标题与商品原文 · " +
    (version == null ? "资料缺失" : `资料 ${product.document_version ?? "v" + version}`));
  if (item.attribution !== "product") groupText(sources, "p", "用户所选参考商品 · 搜索词归属待补");
  groupText(sources, "p", `${product.title} · 标题 v${product.title_version} · ${product.target.sku}`);
  const facts = groupText(sources, "ul", "");
  for (const fact of product.facts) groupText(facts, "li", fact.statement);
  for (const chunk of item.source_chunks || []) {
    const original = groupText(sources, "details", "");
    const cited = item.citations.some(citation => citation.chunk_id === chunk.chunk_id);
    const locations = chunk.locations.map(location => location.location).join(", ");
    groupText(original, "summary", `${cited ? "已核验引用" : "本轮检索"} · ${chunk.filename} · ${locations}`);
    groupText(original, "pre", chunk.content);
    groupText(original, "small", `${chunk.sku} · ${chunk.version} · chunk_id ${chunk.chunk_id}`);
  }
  if (item.review) {
    const review = groupText(body, "section", "");
    review.className = "search-term-evidence";
    groupText(review, "h3", "投放复核待办");
    groupText(review, "p", `待人工复核 · ${item.review.term} · ${item.scope.campaign_id} / ${item.scope.ad_group_id}`);
    groupText(review, "p", item.review.reason);
  }
}

function searchTermTitleEditor(body, detail, path, result) {
  let saved = result.draft;
  const editor = groupText(body, "section", "");
  editor.className = "search-term-evidence";
  groupText(editor, "h3", "标题草案");
  const diff = groupText(editor, "div", "");
  diff.className = "search-term-diff";
  const before = groupText(diff, "div", "");
  groupText(before, "strong", "原始标题");
  groupText(before, "del", result.product.title);
  const after = groupText(diff, "div", "");
  groupText(after, "strong", "已校验标题");
  const accepted = groupText(after, "ins", saved.title);
  const label = groupText(editor, "label", "编辑英文标题");
  label.htmlFor = "searchTermTitleEdit";
  const input = groupText(editor, "textarea", "");
  input.id = label.htmlFor;
  input.value = saved.title;
  input.rows = 3;
  input.maxLength = 2000;
  input.setAttribute("aria-describedby", "searchTermTitleFeedback");
  const buttons = groupText(editor, "div", "");
  buttons.className = "search-term-controls";
  const verify = searchTermButton(buttons, "校验并保存", true);
  const copy = searchTermButton(buttons, "复制已保存版本");
  const restore = searchTermButton(buttons, "恢复已保存版本");
  const rerun = searchTermButton(buttons, "重新诊断", true);
  rerun.hidden = true;
  const status = groupText(editor, "p", "");
  status.id = "searchTermTitleFeedback";
  status.setAttribute("role", "status");
  const errors = groupText(editor, "p", "");
  errors.setAttribute("role", "alert");
  const rule = result.edit_rules;
  const count = groupText(editor, "p", "");
  count.id = "searchTermTitleCount";
  input.setAttribute("aria-describedby", "searchTermTitleFeedback searchTermTitleCount");
  const archived = chatGroups.get(detail.conversationId)?.archived;
  let busy = false;
  let stale = false;

  function sync() {
    const dirty = input.value !== saved.title;
    verify.disabled = busy || archived || stale || !dirty;
    copy.disabled = busy || stale || (dirty && !errors.textContent);
    restore.disabled = busy || !dirty;
    input.disabled = busy || archived || stale;
    rerun.disabled = busy || archived;
    accepted.textContent = saved.title;
    const length = Array.from(input.value).length;
    count.textContent = `${length} / ${rule.title_max_length} 字符（含空格） · ${rule.version}${length > rule.title_max_length ? " · 超限" : ""}`;
  }
  function refreshStatus() {
    if (!stale) errors.textContent = "";
    status.textContent = input.value === saved.title ? `已保存 v${saved.revision}` : "编辑后须重新校验才能复制。";
    sync();
  }
  function showError(error) {
    status.textContent = "";
    errors.textContent = error.message;
    if (error.detail?.code !== "stale_title_draft") return;
    stale = true;
    rerun.hidden = false;
    const reasonLabels = {
      facts_changed: "商品资料已更新",
      title_changed: "原标题已更新",
      product_unavailable: "当前 SKU 的商品资料不可用",
      ownership_changed: "报表商品归属已更新",
    };
    const reasons = error.detail.reasons.map(reason => reasonLabels[reason]).filter(Boolean);
    if (reasons.length) errors.textContent += "。" + reasons.join("、") + "。";
  }
  input.addEventListener("input", refreshStatus);
  restore.addEventListener("click", () => { input.value = saved.title; refreshStatus(); });
  verify.addEventListener("click", async () => {
    busy = true;
    sync();
    try {
      saved = await searchTermRequest(detail.conversationId, path + "/draft", {
        method: "PUT", headers: {"Content-Type": "application/json"}, signal: detail.controller.signal,
        body: JSON.stringify({title: input.value, revision: saved.revision}),
      });
      if (isGroupDetailCurrent(detail)) refreshStatus();
    } catch (error) {
      if (isGroupDetailCurrent(detail)) showError(error);
    } finally {
      busy = false;
      sync();
    }
  });
  copy.addEventListener("click", async () => {
    busy = true;
    sync();
    try {
      const latest = await searchTermRequest(detail.conversationId, path + "/draft/copy?revision=" + saved.revision,
        {signal: detail.controller.signal, cache: "no-store"});
      if (!isGroupDetailCurrent(detail)) return;
      if (latest.revision !== saved.revision || latest.title !== saved.title) {
        throw new Error("已保存版本有变化，请重新打开卡片。");
      }
      await navigator.clipboard.writeText(latest.title);
      status.textContent = `已复制 v${saved.revision}`;
    } catch (error) {
      if (isGroupDetailCurrent(detail)) showError(error);
    } finally {
      busy = false;
      sync();
    }
  });
  rerun.addEventListener("click", async () => {
    if (groupPending.has(detail.conversationId)) return;
    busy = true;
    status.textContent = "正在重新诊断…";
    sync();
    try {
      const {run_id} = await submitSearchTermRun(detail.conversationId, `/runs/${encodeURIComponent(detail.run.run_id)}/rerun`);
      if (isGroupDetailCurrent(detail)) {
        closeGroupDetail(false);
        byId("chatMessages").querySelector(`[data-run-id="${CSS.escape(run_id)}"] [data-result-kind="search_term"]`)?.focus();
      }
    } catch (error) {
      if (isGroupDetailCurrent(detail)) status.textContent = error.message;
    } finally {
      busy = false;
      sync();
    }
  });
  refreshStatus();
}

async function renderSearchTermDetail(body, detail) {
  const path = `/runs/${encodeURIComponent(detail.run.run_id)}/items/${detail.termItemId}`;
  const loading = groupText(body, "p", "正在读取报表和商品依据…");
  loading.setAttribute("role", "status");
  try {
    const result = await searchTermRequest(detail.conversationId, path, {signal: detail.controller.signal});
    if (!isGroupDetailCurrent(detail)) return;
    if (result.entry_id !== detail.entry.entry_id) throw new Error("诊断卡标识异常");
    loading.remove();
    searchTermEvidence(body, result);
    if (result.draft) searchTermTitleEditor(body, detail, path, result);
  } catch (error) {
    if (isGroupDetailCurrent(detail)) loading.textContent = error.message;
  }
}
