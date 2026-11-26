const groupRunStatus = {
  running: "进行中", completed: "已完成", stopped: "已停止", superseded: "已被替换",
  error: "出错", interrupted: "已中断",
};
const groupActionStatus = { pending: "待审批", approved: "已执行", expired: "已失效" };
const groupProgressNotices = new Set(["read_reused", "read_duplicate", "duplicate_dispatch", "react_stopped", "listing_stage"]);
const delegationTools = {
  get_plan_metrics_by_date: "投放指标取证", get_plan_detail: "查询计划详情",
  get_daily_report_by_date: "查询日报", search_knowledge: "检索规则",
  get_listing_sales: "查询商品销量", get_listing_current: "读取 Listing",
  get_listing_facts: "核对商品资料", get_olist_product_sales: "查询历史销量",
  get_inspection_report: "核验搜索词报表", get_inspection_sources: "核验商品原文",
};
const delegationOutputs = {
  get_plan_metrics_by_date: "曝光、点击和 CTR", get_plan_detail: "计划详情",
  get_daily_report_by_date: "日报回执", search_knowledge: "规则条文",
  get_listing_sales: "商品销量", get_listing_current: "当前 Listing", get_listing_facts: "商品事实",
  get_olist_product_sales: "历史销量",
  get_inspection_report: "报表原行与归属", get_inspection_sources: "版本化原文",
};
const compactWorkbench = matchMedia("(max-width: 1050px)");
let openGroupResult = null;

const workbenchIcons = {
  sales: "M4 4v16h16M9 15v-3m4 3V8m4 7v-5",
  progress: "M4 7h16M4 12h16M4 17h10",
  listing: "M6 4h8l4 4v12H6Zm8 0v4h4M9 12h6m-6 4h6",
  pause: "M9 5v14m6-14v14",
  evidence: "M6 4h8l4 4v12H6Zm8 0v4h4M9 12h6m-6 4h6",
  search_term: "M10 5a5 5 0 1 0 0 10 5 5 0 0 0 0-10Zm4 9 5 5",
};

function workbenchCardIcon(heading, kind) {
  const wrapper = groupText(heading, "span", "");
  wrapper.className = "workbench-card-icon";
  wrapper.setAttribute("aria-hidden", "true");
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  const path = document.createElementNS(svg.namespaceURI, "path");
  path.setAttribute("d", workbenchIcons[kind]);
  svg.append(path);
  wrapper.append(svg);
}

function toggleGroupList(open = byId("chatGroupToggle").getAttribute("aria-expanded") === "false") {
  byId("chatGroupToggle").setAttribute("aria-expanded", String(open));
  byId("chatGroupControls").classList.toggle("is-open", open);
  if (open) byId("chatGroupList").querySelector('[aria-current="true"]')?.focus({ preventScroll: true });
}

function addGroupCard(container, model, title, summary, label, state, sales) {
  const card = groupText(container, "button", "");
  card.type = "button";
  card.className = "workbench-card";
  card.dataset.resultKind = model.kind;
  if (state) card.dataset.state = state;
  if (model.action) card.dataset.actionId = model.action.action_id;
  card.setAttribute("aria-controls", "workbenchDetail");
  card.setAttribute("aria-expanded", "false");
  const heading = groupText(card, "span", "");
  heading.className = "workbench-card-heading";
  workbenchCardIcon(heading, model.kind);
  groupText(heading, "strong", title);
  if (state) {
    const badge = groupText(heading, "span", state);
    badge.className = "workbench-card-state";
  }
  const description = groupText(card, "span", summary);
  description.className = "workbench-card-summary";
  const footer = sales ? appendWorkbenchSales(card, sales) : card;
  const cta = groupText(footer, "span", label + " →");
  cta.className = "workbench-card-link";
  model.title = title;
  model.card = card;
  card.workbenchResult = model;
  card.addEventListener("click", () => openGroupDetail(model));
}

function appendWorkbenchSales(card, sales) {
  const quantity = value => value == null ? "未知" : groupListingNumber(value) + " 件";
  const delta = sales.units_change_pct;
  const relative = delta == null ? "未知" : (delta > 0 ? "+" : "") + groupListingNumber(delta) + "%";
  const metrics = groupText(card, "span", "");
  metrics.className = "workbench-card-metrics";
  if (!sales.baseline) {
    const item = groupText(metrics, "span", "");
    groupText(item, "small", "销量");
    groupText(item, "b", quantity(sales.current.units));
    return card;
  }
  for (const [label, value, direction] of [["基准窗", quantity(sales.baseline.units)],
    ["当前窗", quantity(sales.current.units)], ["相对变化", relative, delta < 0 ? "down" : delta > 0 ? "up" : "flat"]]) {
    const item = groupText(metrics, "span", "");
    if (direction) item.dataset.direction = direction;
    groupText(item, "small", label);
    groupText(item, "b", value);
  }
  const footer = groupText(card, "span", "");
  footer.className = "workbench-card-footer";
  const total = sales.baseline.units == null || sales.current.units == null ? "两窗合计未知" :
    "两窗合计 " + quantity(sales.baseline.units + sales.current.units);
  const change = sales.units_delta == null ? "变化未知" : "变化 " +
    (sales.units_delta > 0 ? "+" : "") + quantity(sales.units_delta) + (delta == null ? "" : "（" + relative + "）");
  const meta = total + " · " + change;
  groupText(footer, "span", meta).className = "workbench-card-meta";
  return footer;
}

function appendGroupCards(bubble, conversationId, entry, run) {
  if (entry.evidence_kind === "search_term_diagnosis_v1") return;
  const cards = document.createElement("div");
  cards.className = "workbench-cards";
  const base = { conversationId, entry, run };
  const sales = entry.sales_summary;
  const olist = entry.evidence_kind === "olist_sales_v1";
  if (sales) {
    const summary = [sales.baseline, sales.current].filter(Boolean).map(groupWindow).join(" / ");
    addGroupCard(cards, { ...base, kind: "sales" }, "销量分析", summary,
      "查看依据", olist ? "Olist · 公开历史" : "本地测试", sales);
  }
  if (entry.has_evidence && (!sales || entry.evidence_gap_count ||
      (entry.has_product_details && !entry.listing_actions?.length))) {
    addGroupCard(cards, { ...base, kind: "evidence" }, entry.evidence_gap_count ? "证据缺项" : "分析依据",
      entry.evidence_gap_count ? entry.evidence_gap_count + " 项待核验" : "",
      "查看依据");
  }
  for (const action of entry.listing_actions || []) {
    addGroupCard(cards, { ...base, kind: "listing", action }, "英文卖点修改",
      "5 条文案 · 商品编号 " + action.target.sku + " · 本地测试", action.state === "approved" ? "查看回执" : "查看草案",
      groupActionStatus[action.state]);
  }
  for (const action of entry.pause_actions || []) {
    addGroupCard(cards, { ...base, kind: "pause", action }, "暂停投放",
      "#" + action.target.plan_id + " " + action.target.plan_name + " · 沙箱",
      action.state === "approved" ? "查看回执" : "查看变更", groupActionStatus[action.state]);
  }
  if (cards.childElementCount) bubble.appendChild(cards);
}

function delegationAgentName(label, task) {
  const tools = [...new Set(task.capabilities.map(tool => delegationTools[tool]).filter(Boolean))];
  const scopes = [...(task.plannedWindows || []), ...Array.from(task.reads.values(), event => event.arguments)];
  const targets = [...new Set(scopes.map(args => args?.sku || (args?.plan_id ? "#" + args.plan_id : "")).filter(Boolean))];
  const subject = targets.length === 1 && !task.review ? targets[0] : label.replace("任务", "Agent");
  return [subject, task.review ? "独立复核" : tools.join("与")].filter(Boolean).join(" · ");
}

function delegationPendingOutput(task) {
  const metricSource = "get_plan_metrics_by_date";
  const windows = new Map();
  for (const args of task.plannedWindows || []) {
    const key = groupProgressReadKey({ source: metricSource, arguments: args });
    if (task.reads.get(key)?.kind !== "receipt") windows.set(key, args);
  }
  for (const event of task.active) {
    const args = event.arguments;
    if (event.source === metricSource && args?.plan_id && args.start_date && args.end_date) {
      windows.set(groupProgressReadKey(event), args);
    }
  }
  const byPlan = new Map();
  for (const args of windows.values()) {
    if (!byPlan.has(args.plan_id)) byPlan.set(args.plan_id, []);
    const name = args.window === "baseline" ? "基准窗" : args.window === "current" ? "当前窗" : "";
    const dates = groupCompactWindow(args);
    byPlan.get(args.plan_id).push(name ? `${name}（${dates}）` : dates);
  }
  const outputs = [...byPlan].map(([plan, dates]) => `#${plan} ${dates.join("、")}的${delegationOutputs[metricSource]}`);
  const described = new Set(byPlan.size ? [metricSource] : []);
  for (const event of task.active) {
    if (event.source === metricSource || event.source === "search_knowledge") continue;
    outputs.push(groupReadScope(event, true));
    described.add(event.source);
  }
  for (const source of task.capabilities) {
    if (described.has(source) || task.receipts.some(event => event.source === source)) continue;
    if (source === "search_knowledge" && task.reviewRule) {
      const version = /^ctr-review-(v[\d.]+)#low-ctr$/.exec(task.reviewRule)?.[1];
      if (version) {
        outputs.push(`《点击率下降复核》${version} 条文`);
        continue;
      }
    }
    outputs.push(delegationOutputs[source] || "任务回执");
  }
  return [...new Set(outputs)].join("；") || (task.receipts.length ? "" : "任务回执");
}

function appendDelegationWait(text, task, tasks, labels) {
  const pending = (task.waiting?.dependencies || []).filter(label => tasks.get(label)?.terminal?.kind !== "succeeded");
  if (!pending.length) return false;
  const list = groupText(text, "ul", "");
  list.className = "workbench-delegation-wait";
  for (const label of pending) {
    const dependency = tasks.get(label);
    const name = dependency ? delegationAgentName(labels.get(label), dependency) : "上游 Agent";
    const output = dependency ? delegationPendingOutput(dependency) : "任务回执";
    const item = groupText(list, "li", "等待 ");
    groupText(item, "span", name).className = "workbench-delegation-wait-name";
    groupText(item, "span", output ? " 交回 " + output : " 完成任务（回执已收到）");
  }
  return true;
}

function buildGroupDelegation(conversationId, entry, run, events, change) {
  if (!run || (!events.length && !change && !run.can_stop && !run.stop && run.status !== "superseded")) return null;
  const tasks = groupProgressTasks(events.filter(item => !groupProgressNotices.has(item.event?.kind)));
  const dispatchOrder = events.filter(item => item.event?.kind === "dispatched").map(item => groupTaskLabel(item.event));
  const order = new Set([...dispatchOrder, ...tasks.keys()]);
  const summaries = Array.from(order, label => {
    const task = tasks.get(label);
    return [task, groupTaskSummary(task, tasks, run)];
  });
  const rows = summaries.filter(([task, [state]]) => task.dispatched || state !== "running");
  const section = document.createElement("section");
  section.className = "workbench-delegation";
  section.dataset.runId = run.run_id;
  section.setAttribute("aria-label", tasks.size ? "本轮协作任务" : "本轮任务");
  const header = groupText(section, "div", "");
  header.className = "workbench-delegation-heading";
  const stage = events.findLast(item => item.event?.kind === "listing_stage");
  if (stage && run.status === "running") groupText(header, "span", stage.content).dataset.listingStage = "";

  const labels = new Map();
  const usedLabels = new Set(summaries.map(([task]) => task.label).filter(label => /^任务 \d+$/.test(label)));
  let nextLabel = 1;
  for (const [task] of summaries) {
    if (usedLabels.has(task.label)) {
      labels.set(task.label, task.label);
    } else {
      while (usedLabels.has("任务 " + nextLabel)) nextLabel++;
      labels.set(task.label, "任务 " + nextLabel++);
    }
  }
  if (rows.length) {
    const prior = byId("chatMessages").querySelector(`.workbench-delegation[data-run-id="${CSS.escape(run.run_id)}"]`);
    const openTasks = new Set(Array.from(prior?.querySelectorAll(".workbench-delegation-row[open]") || [],
      row => row.dataset.taskLabel));
    const list = groupText(section, "div", "");
    list.className = "workbench-delegation-list";
    for (const [task, [state, summary]] of rows) {
      const row = groupText(list, "details", "");
      row.className = "workbench-delegation-row";
      row.dataset.taskLabel = task.label;
      row.dataset.state = state;
      row.toggleAttribute("data-dispatched", Boolean(task.dispatched));
      row.open = openTasks.has(task.label);
      const toggle = groupText(row, "summary", "");
      toggle.className = "workbench-delegation-toggle";
      const icon = groupText(toggle, "span", "");
      icon.className = "workbench-delegation-icon";
      icon.setAttribute("aria-hidden", "true");
      const name = delegationAgentName(labels.get(task.label), task);
      groupText(toggle, "span", name).className = "workbench-delegation-name";
      let detail = summary;
      for (const [label, safeLabel] of labels) if (label && label !== safeLabel) detail = detail.replaceAll(label, safeLabel);
      let status = detail;
      if (state === "succeeded") status = "已交回";
      if (state === "running") {
        if (task.handoff) status = "正在复核";
        else if (!task.active.length && !task.receipts.length) status = "执行中";
      }
      if (state === "waiting") {
        for (const dependencyLabel of task.waiting.dependencies || []) {
          const dependency = tasks.get(dependencyLabel);
          if (dependency) {
            const label = labels.get(dependencyLabel);
            status = status.replaceAll(label, delegationAgentName(label, dependency));
          }
        }
      }
      groupText(toggle, "span", status).className = "workbench-delegation-state";
      toggle.title = [name, status].join(" · ");

      function renderDetails() {
        if (!row.open || row.children.length > 1) return;
        const body = groupText(row, "div", "");
        body.className = "workbench-delegation-body";
        if ((state !== "waiting" || !appendDelegationWait(body, task, tasks, labels)) && detail !== status) {
          groupText(body, "p", detail);
        }
        const history = groupText(body, "ol", "");
        history.className = "workbench-delegation-events";
        for (const event of task.entries) history.appendChild(groupProgressEntry(event, true));
      }
      row.addEventListener("toggle", renderDetails);
      renderDetails();
    }
  } else if (!stage || run.status !== "running") {
    groupText(header, "span", run.status === "running" ? "主 Agent 正在处理" :
      "本轮任务 · " + (groupRunStatus[run.status] || "已结束"));
  }
  const details = groupText(section, "button", "查看执行记录");
  details.type = "button";
  details.className = "workbench-delegation-details";
  details.dataset.resultKind = "progress";
  details.setAttribute("aria-controls", "workbenchDetail");
  details.setAttribute("aria-expanded", "false");
  const model = { conversationId, entry, run, kind: "progress", events, change, card: details, title: "执行记录" };
  details.workbenchResult = model;
  details.addEventListener("click", () => openGroupDetail(model));
  return section;
}

function isGroupDetailCurrent(detail) {
  return openGroupResult === detail && !detail.controller.signal.aborted &&
    detail.conversationId === activeChatGroup && detail.view === chatViewId && Boolean(TOKEN);
}

function closeGroupDetail(restoreFocus = true) {
  const detail = openGroupResult;
  if (!detail) return;
  openGroupResult = null;
  detail.controller.abort();
  detail.card.setAttribute("aria-expanded", "false");
  byId("workbenchDetail").close();
  byId("workbenchDetailBody").replaceChildren();
  if (restoreFocus && detail.card.isConnected) {
    byId("chatMessages").scrollTop = detail.threadScroll;
    detail.card.focus({ preventScroll: true });
    detail.card.scrollIntoView({ block: "nearest" });
  }
}

function refreshOpenGroupDetail() {
  const detail = openGroupResult;
  if (!detail) return;
  const cards = byId("chatMessages").querySelectorAll("[data-result-kind]");
  const card = Array.from(cards).find(button => {
    const model = button.workbenchResult;
    return model.entry.run_id === detail.entry.run_id && model.kind === detail.kind &&
      model.action?.action_id === detail.action?.action_id &&
      model.termItemId === detail.termItemId &&
      (model.entry.entry_id === detail.entry.entry_id || model.kind === "progress");
  });
  if (!card) {
    if (!detail.actionUnavailable) closeGroupDetail(false);
    return;
  }
  detail.card = card;
  card.setAttribute("aria-expanded", "true");
  if (detail.actionState) setWorkbenchActionState(detail, detail.actionState);
  if (detail.kind === "progress") {
    Object.assign(detail, card.workbenchResult);
    renderWorkbenchProgress(detail);
  }
}

function renderWorkbenchProgress(detail) {
  const body = byId("workbenchDetailBody");
  const previous = body.querySelector(".group-progress");
  const scroll = body.scrollTop;
  const focusedTask = document.activeElement?.closest(".group-task")?.dataset.taskLabel;
  const panel = groupProgress(detail.run, detail.events, detail.change, previous);
  body.replaceChildren(panel);
  body.scrollTop = scroll;
  if (focusedTask) Array.from(panel.querySelectorAll(".group-task")).find(
    row => row.dataset.taskLabel === focusedTask)?.querySelector("summary")?.focus({ preventScroll: true });
}

async function openGroupDetail(model) {
  if (model.conversationId !== activeChatGroup) return;
  closeSearchTermSetup();
  const dialog = byId("workbenchDetail");
  if (openGroupResult) {
    openGroupResult.controller.abort();
    openGroupResult.card.setAttribute("aria-expanded", "false");
  }
  const detail = { ...model, view: chatViewId, controller: new AbortController(),
    threadScroll: openGroupResult?.threadScroll ?? byId("chatMessages").scrollTop };
  openGroupResult = detail;
  model.card.setAttribute("aria-expanded", "true");
  byId("workbenchDetailContext").textContent = chatGroups.get(model.conversationId)?.title || "工作群";
  byId("workbenchDetailTitle").textContent = model.title;
  const body = byId("workbenchDetailBody");
  body.replaceChildren();
  dialog.dataset.kind = detail.kind;
  dialog.dataset.entryId = detail.entry.entry_id;
  dialog.dataset.runId = detail.entry.run_id;
  body.scrollTop = 0;
  if (!dialog.open) {
    if (compactWorkbench.matches) dialog.showModal();
    else dialog.show();
  }
  byId("workbenchDetailClose").focus({ preventScroll: true });
  if (detail.kind === "progress") return renderWorkbenchProgress(detail);
  if (detail.kind === "search_term") return renderSearchTermDetail(body, detail);

  const loading = groupText(body, "p", "正在读取本轮详情…");
  loading.setAttribute("role", "status");
  try {
    const evidence = await readGroupEntryEvidence(detail.conversationId, detail.entry, detail.controller.signal);
    if (!isGroupDetailCurrent(detail)) return;
    loading.remove();
    const panel = groupText(body, "section", "");
    panel.className = "group-evidence";
    panel.dataset.answerId = detail.entry.entry_id;
    if (detail.kind === "listing") {
      renderGroupListingEvidence(panel, evidence, false);
      refineWorkbenchListingEvidence(panel, evidence);
      await renderWorkbenchListing(body, detail, evidence);
    } else if (detail.kind === "sales" && evidence.kind === "listing_operations_v1") {
      renderGroupListingEvidence(panel, evidence);
    } else {
      renderGroupEvidence(panel, evidence);
      if (detail.kind === "pause") {
        const action = groupPausePanel(detail.conversationId, detail.entry, detail.action, detail);
        body.appendChild(action);
        if (detail.action.state === "approved") action.querySelector("button").click();
      }
    }
  } catch (error) {
    if (!isGroupDetailCurrent(detail)) return;
    loading.remove();
    if (error.status === 404 && detail.kind === "listing") {
      return renderWorkbenchListing(body, detail, {});
    }
    groupText(body, "p", error.message).setAttribute("role", "status");
  }
}

function refineWorkbenchListingEvidence(panel, evidence) {
  const title = evidence.listing?.title;
  const heading = panel.firstElementChild;
  heading.className = "workbench-listing-product";
  heading.textContent = title || "商品变更";
  if (title) {
    const repeated = Array.from(panel.children).find(node => node.tagName === "P" && node.textContent === title);
    repeated?.remove();
  }
  const meta = document.createElement("p");
  meta.className = "workbench-listing-meta";
  const target = evidence.target;
  meta.textContent = target ? "店铺 " + target.shop_id + " · 站点 " + target.marketplace_id +
    " · 商品编号 " + target.sku + " · 本地测试商品" : "本地测试商品";
  heading.after(meta);
  if (evidence.publication) {
    const publication = evidence.publication;
    groupEventId(panel, "销量发布", publication.publication_id);
    groupText(panel, "p", "销量版本 " + publication.snapshot.version + " · 导入批次 " + publication.batch_id +
      " · 发布账号 " + publication.published_by_user_id + " · 取证账号 " + publication.read_by_user_id);
    const report = evidence.publication_report;
    if (report?.report) {
      groupText(panel, "p", "报表 " + report.report.filename + " · 版本 " + report.report.version);
      groupEventId(panel, "报表导入", report.report.import_id);
    }
  }
  if (evidence.proposal?.inspection) {
    const basis = evidence.proposal.inspection;
    const facts = new Map(evidence.facts.facts.map(fact => [fact.fact_id, fact.statement]));
    const reason = document.createElement("p");
    reason.textContent = "相对降幅最大 · 下滑 " + groupListingNumber(-evidence.metrics.units_change_pct) +
      "% · 巡检阈值 " + basis.threshold_pct + "%";
    meta.after(reason);
    const omissions = document.createElement("details");
    groupText(omissions, "summary", "查看现有五点遗漏的事实");
    const list = groupText(omissions, "ul", "");
    for (const id of basis.omitted_fact_ids) groupText(list, "li", facts.get(id));
    reason.after(omissions);
  }

  const current = panel.querySelector('[data-bullet-version="before"]');
  if (current) current.previousElementSibling.textContent = "当前文案 · 版本 " + evidence.listing.version;
  const proposed = panel.querySelector('[data-bullet-version="proposed"]');
  if (proposed) proposed.previousElementSibling.textContent = "拟更新文案";
  const facts = new Map((evidence.facts?.facts || []).map(fact => [fact.fact_id, fact.statement]));
  for (const source of proposed?.querySelectorAll("small[data-fact-id]") || []) {
    const factId = source.dataset.factId;
    if (!facts.has(factId)) continue;
    const disclosure = document.createElement("details");
    disclosure.className = "workbench-listing-source";
    disclosure.dataset.factId = factId;
    groupText(disclosure, "summary", "查看原文");
    groupText(disclosure, "p", facts.get(factId));
    source.replaceWith(disclosure);
  }
}

function setWorkbenchActionState(detail, state) {
  detail.actionState = state;
  detail.card.dataset.state = groupActionStatus[state];
  detail.card.querySelector(".workbench-card-state").textContent = groupActionStatus[state];
  if (state === "approved") detail.card.querySelector(".workbench-card-link").textContent = "查看回执 →";
}

async function renderWorkbenchListing(body, detail, evidence) {
  const { conversationId, entry, action, controller } = detail;
  const path = "/" + encodeURIComponent(conversationId) + "/runs/" + encodeURIComponent(entry.run_id) +
    "/listing-actions/" + encodeURIComponent(action.action_id);
  const section = groupText(body, "section", "");
  section.className = "group-listing-action";
  section.dataset.actionId = action.action_id;
  const status = groupText(section, "p", "正在检查提案…");
  status.setAttribute("role", "status");

  function verifyAction(data) {
    const { shop_id, marketplace_id, sku } = action.target;
    if (data.conversation_id !== conversationId || data.run_id !== entry.run_id ||
        data.entry_id !== entry.entry_id || data.action_id !== action.action_id ||
        data.target?.shop_id !== shop_id || data.target?.marketplace_id !== marketplace_id ||
        data.target?.sku !== sku || (data.receipt && data.receipt.action_id !== action.action_id)) {
      throw new Error("商品变更与本轮答案不匹配");
    }
  }

  function showReceipt(data, reviewMessage) {
    verifyAction(data);
    if (!data.receipt || !data.listing) throw new Error("本轮执行回执暂不可读取");
    if (!data.proposal) body.querySelector(".group-evidence")?.remove();
    section.replaceChildren(status);
    setWorkbenchActionState(detail, "approved");
    section.dataset.state = "approved";
    const receipt = data.receipt;
    status.textContent = "已执行 · " + receipt.approved_by_name + " · " +
      new Date(receipt.approved_at).toLocaleString("zh-CN");
    const result = groupText(section, "div", "");
    result.className = "group-listing-receipt";
    groupText(result, "h4", "当前实际版本 " + data.listing.version);
    groupListingBullets(result, data.listing.bullets, "actual");
    if (receipt.committed_version !== data.listing.version) {
      groupText(result, "h4", "本回执提交版本 " + receipt.committed_version);
      groupListingBullets(result, receipt.committed_bullets, "committed");
    }
    groupEventId(result, "回执", receipt.receipt_id);
    groupEventId(result, "执行 Trace", receipt.execution_trace_id);
    groupEventId(result, "来源 Trace", receipt.source_trace_id);
    if (reviewMessage) groupText(result, "p", "复盘不可查看 · " + reviewMessage).setAttribute("role", "status");
    if (data.review_available || data.proposal?.review_baseline) {
      const check = groupText(result, "button", "查看复盘");
      check.type = "button";
      check.className = "btn";
      check.setAttribute("aria-expanded", "false");
      const panel = groupText(result, "section", "");
      panel.hidden = true;
      check.addEventListener("click", async () => {
        if (!isGroupDetailCurrent(detail) || check.disabled) return;
        check.disabled = true;
        check.setAttribute("aria-expanded", "true");
        panel.hidden = false;
        panel.replaceChildren();
        const notice = groupText(panel, "p", "正在读取复盘销量…");
        notice.setAttribute("role", "status");
        try {
          const review = await chatGroupRequest(path + "/review", "GET", undefined, controller.signal);
          if (!isGroupDetailCurrent(detail)) return;
          if (review.receipt_id !== receipt.receipt_id || review.action_id !== action.action_id) {
            throw new Error("复盘与执行回执不匹配");
          }
          notice.remove();
          groupText(panel, "h4", "下一周期销量复盘");
          groupText(panel, "p", "北京时间 · 基准窗覆盖 " + review.baseline.covered_days +
            "/7 天 · 复盘窗覆盖 " + review.current.covered_days + "/7 天");
          groupEventId(panel, "关联回执", review.receipt_id);
          groupListingChart(panel, review, { currentLabel: "复盘窗", showChange: review.status === "complete" });
          if (review.status === "incomplete") {
            groupText(panel, "p", "复盘窗缺报 " + review.missing_dates.join("、") + " · 销量比较待补齐");
          }
          if (review.observation_snapshot) {
            groupText(panel, "p", "原商品版本 " + review.original_version + " · 本次提交版本 " + receipt.committed_version +
              " · 观察期已覆盖日期合计 " + review.known_units + " 件");
            groupEventId(panel, "基准销量快照", review.baseline_snapshot.snapshot_id);
            groupEventId(panel, "复盘销量快照", review.observation_snapshot.snapshot_id);
            if (review.observation_inspection) {
              groupEventId(panel, "复盘销量发布", review.observation_inspection.publication_id);
            }
            groupText(panel, "p", "五点修改效果待观察。" + review.recommendation);
          }
          panel.dataset.listingReview = review.status;
        } catch (error) {
          if (!isGroupDetailCurrent(detail)) return;
          if (error.status === 404) await refreshReceiptAfterReviewDenied(receipt.receipt_id, error.message);
          else notice.textContent = error.message;
        } finally {
          check.disabled = false;
        }
      });
    }
    body.prepend(section);
    body.scrollTop = 0;
  }

  async function refreshReceiptAfterReviewDenied(receiptId, message) {
    body.querySelector(".group-evidence")?.remove();
    section.replaceChildren(status);
    status.textContent = "正在重新读取执行回执…";
    try {
      const data = await chatGroupRequest(path, "GET", undefined, controller.signal);
      if (!isGroupDetailCurrent(detail)) return;
      if (data.receipt?.receipt_id !== receiptId) throw new Error("读回回执与本轮执行回执不匹配");
      showReceipt(data, message);
      if (!data.proposal) readChatGroupHistory();
    } catch (error) {
      if (!isGroupDetailCurrent(detail)) return;
      if (error.status === 404) showError(error);
      else status.textContent = "无法重新读取执行回执 · " + error.message;
    }
  }

  function showError(error) {
    if (error.status === 404) {
      detail.actionUnavailable = true;
      delete section.dataset.state;
      section.replaceChildren(status);
      body.replaceChildren(section);
      status.textContent = "商品提案不存在";
      readChatGroupHistory();
    } else if (error.status === 409) {
      setWorkbenchActionState(detail, "expired");
      section.dataset.state = "expired";
      status.textContent = "提案已失效 · " + (error.approvalBlockReason?.message || error.message);
    } else {
      status.textContent = error.message;
    }
  }

  try {
    const data = await chatGroupRequest(path + (action.state === "approved" ? "" : "?review=true"),
      "GET", undefined, controller.signal);
    if (!isGroupDetailCurrent(detail)) return;
    verifyAction(data);
    if (data.state === "approved") return showReceipt(data);
    if (data.approval_blocked || data.listing?.version !== action.expected_version) {
      const error = new Error(data.approval_blocked || "商品版本已变化，请重新生成提案");
      error.status = 409;
      return showError(error);
    }
    const facts = new Set(evidence.facts?.facts?.map(fact => fact.fact_id));
    const complete = !evidence.gaps?.length && evidence.listing?.bullets?.length === 5 &&
      evidence.proposal?.bullets?.length === 5 && evidence.proposal.bullets.every(bullet => facts.has(bullet.fact_id));
    if (!complete) {
      status.textContent = "证据不足，无法审批";
      return;
    }
    setWorkbenchActionState(detail, "pending");
    section.dataset.state = "pending";
    status.textContent = "待审批 · 仅更新这 5 条英文卖点";
    const approve = groupText(section, "button", "批准应用英文五点（本地测试）");
    approve.type = "button";
    approve.className = "btn btn-primary";
    approve.addEventListener("click", async () => {
      if (!isGroupDetailCurrent(detail) || approve.disabled) return;
      approve.disabled = true;
      status.textContent = "正在提交审批…";
      let committed = false;
      try {
        // The write can finish after the panel closes. Only the active panel can display its result.
        const response = await chatGroupRequest(path + "/confirm", "POST");
        committed = true;
        if (!isGroupDetailCurrent(detail)) return;
        verifyAction(response);
        approve.remove();
        setWorkbenchActionState(detail, "approved");
        status.textContent = "已提交，正在读取实际版本与回执…";
        const actual = await chatGroupRequest(path, "GET", undefined, controller.signal);
        if (isGroupDetailCurrent(detail)) {
          if (actual.receipt?.receipt_id !== response.receipt?.receipt_id) throw new Error("读回回执与审批结果不匹配");
          showReceipt(actual);
          body.focus({ preventScroll: true });
        }
      } catch (error) {
        if (!isGroupDetailCurrent(detail)) return;
        if (error.status === 404) {
          showError(error);
        } else if (committed) {
          approve.remove();
          status.textContent = "审批已提交，读回失败。请重新打开查看回执。";
        } else {
          showError(error);
          if (section.dataset.state === "expired") approve.remove();
          else approve.disabled = false;
        }
      } finally {
        if (committed && detail.view === chatViewId && conversationId === activeChatGroup && TOKEN) {
          readChatGroupHistory();
        }
      }
    });
  } catch (error) {
    if (isGroupDetailCurrent(detail)) showError(error);
  }
}

document.getElementById("workbenchDetail").addEventListener("cancel", event => {
  event.preventDefault();
  closeGroupDetail();
});
document.addEventListener("keydown", event => {
  if (event.key !== "Escape" || event.defaultPrevented) return;
  if (document.querySelector("dialog:modal")) return;
  if (openGroupResult && !compactWorkbench.matches) {
    event.preventDefault();
    closeGroupDetail();
  } else if (byId("searchTermSetup").open) {
    event.preventDefault();
    closeSearchTermSetup(true);
  } else if (byId("diagnosisExecution").open) {
    event.preventDefault();
    closeDiagnosisExecution();
  } else if (document.getElementById("chatGroupControls").classList.contains("is-open")) {
    toggleGroupList(false);
    byId("chatGroupToggle").focus();
  }
});
compactWorkbench.addEventListener("change", () => {
  const dialog = document.querySelector(".workbench-side-panel[open]");
  if (!dialog) return;
  dialog.close();
  if (compactWorkbench.matches) dialog.showModal();
  else dialog.show();
});
