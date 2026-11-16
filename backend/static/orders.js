"use strict";

const byId = (id) => document.getElementById(id);
const unknown = (value) => value ?? "未知";
const format = new Intl.NumberFormat("zh-CN");
const pageSize = 25;
const detail = byId("detail");
let shops = [];
let session = null;
let shopView = null;
let listRequest = 0;
let detailRequest = 0;
let salesRequest = 0;
let salesChart = null;

function element(tag, value, className) {
  const node = document.createElement(tag);
  if (value !== undefined) node.textContent = value;
  if (className) node.className = className;
  return node;
}

function query() {
  return new URLSearchParams(location.search);
}

function selectedShop(params) {
  return shops.find(({ source, shop_id }) => source === params.get("source") && shop_id === params.get("shop")) || shops[0];
}

function shopPath(shop) {
  return "/api/orders/shops/" + encodeURIComponent(shop.source) + "/" + encodeURIComponent(shop.shop_id);
}

function go(params, replace = false) {
  const suffix = params.size ? "?" + params.toString() : "";
  history[replace ? "replaceState" : "pushState"](null, "", "/orders" + suffix);
  render();
}

function clearDetail() {
  if (detail.open) detail.close();
  byId("detail-title").textContent = "订单详情";
  byId("detail-source").textContent = "";
  byId("detail-content").replaceChildren();
}

function clearShop() {
  shopView = null;
  byId("workspace").hidden = true;
  clearDetail();
  byId("orders").replaceChildren();
  byId("runs").replaceChildren();
  byId("sales-panel").hidden = true;
  byId("sales-product").replaceChildren();
  byId("sales-filters").reset();
  byId("sales-submit").disabled = true;
  byId("sales-notice").textContent = "";
  clearSalesResult();
  for (const id of ["notice", "sync-notice", "total-orders", "total-items", "source-label", "shop-id", "source-note", "range"]) {
    byId(id).textContent = "";
  }
  byId("sync-notice").hidden = true;
  byId("search").value = "";
  byId("status").replaceChildren(new Option("全部状态", ""));
  byId("preflight").disabled = byId("sync").disabled = false;
}

function clearSession() {
  session = null;
  shops = [];
  clearShop();
  byId("orders-users").hidden = true;
  byId("shop-field").hidden = true;
  byId("shop").replaceChildren();
  byId("login").hidden = true;
}

function loginRequired() {
  clearSession();
  byId("notice").textContent = "请登录后查看已授权店铺。";
  byId("login").hidden = false;
}

function savedLogin() {
  try {
    return JSON.parse(sessionStorage.getItem("ad.login"));
  } catch {
    return undefined;
  }
}

function sessionToken() {
  return savedLogin()?.token;
}

function currentSession(requestSession) {
  if (!requestSession || requestSession !== session) return false;
  if (requestSession.token === sessionToken()) return true;
  start();
  return false;
}

function currentShop(view) {
  return view === shopView && currentSession(view?.session);
}

async function api(token, path, options = {}) {
  if (!token) throw Object.assign(new Error("请登录后继续"), { status: 401 });
  const response = await fetch(path, {
    ...options,
    cache: "no-store",
    headers: { Authorization: "Bearer " + token, ...options.headers },
  });
  const payload = await response.json();
  if (!response.ok) {
    throw Object.assign(new Error(typeof payload.detail === "string" ? payload.detail : "请求失败"), {
      status: response.status, receipt: payload.data, retryAfter: response.headers.get("Retry-After"),
    });
  }
  return payload.data;
}

function appendRow(parent, values) {
  const row = parent.insertRow();
  for (const value of values) {
    const cell = row.insertCell();
    if (value instanceof Node) cell.append(value);
    else cell.textContent = value;
  }
}

function emptyRow(parent, columns, message) {
  const cell = parent.insertRow().insertCell();
  cell.colSpan = columns;
  cell.textContent = message;
}

const operations = { import: "文件导入", preflight: "连接预检", sync: "手动同步" };
const failures = {
  olist_import_failed: "Olist 导入失败",
  amazon_credentials_missing: "服务端缺少 LWA 凭据",
  amazon_authorization_failed: "Amazon 授权失败",
  amazon_rate_limited: "Amazon 请求限流",
  amazon_invalid_response: "Amazon 回执格式异常",
  amazon_invalid_marketplace: "Amazon 回执缺少有效站点",
  amazon_invalid_fulfillment: "Amazon 回执缺少有效履约方",
  amazon_invalid_created_time: "Amazon 回执缺少有效下单时间",
  amazon_invalid_last_updated_time: "Amazon 回执缺少有效源更新时间",
  amazon_invalid_pagination: "Amazon 回执缺少有效分页令牌",
  amazon_upstream_error: "Amazon 请求失败",
  amazon_unavailable: "Amazon 连接失败",
  amazon_pagination_loop: "Amazon 分页令牌重复",
  amazon_page_limit: "已达同步页数上限，请缩短窗口",
};

function runResult(run) {
  return run.outcome === "succeeded" ? "成功" : failures[run.error_code] || run.error_code;
}

const salesDates = ["baseline_start", "baseline_end", "current_start", "current_end"];

function clearSalesResult() {
  salesRequest += 1;
  salesChart?.dispose();
  salesChart = null;
  byId("sales-result").hidden = true;
  for (const id of ["sales-source", "sales-totals", "sales-points"]) byId(id).replaceChildren();
}

function setSalesDates() {
  const product = byId("sales-product").selectedOptions[0];
  if (!product) return;
  const latest = new Date(product.dataset.lastDate + "T00:00:00Z");
  if (!Number.isFinite(latest.getTime())) return;
  for (const [index, delta] of [27, 14, 13, 0].entries()) {
    const date = new Date(latest);
    date.setUTCDate(date.getUTCDate() - delta);
    byId(salesDates[index]).value = date.toISOString().slice(0, 10);
  }
}

async function loadSalesProducts(view) {
  if (view.salesProductsRequested) return;
  view.salesProductsRequested = true;
  byId("sales-notice").textContent = "正在读取商品…";
  try {
    const products = await api(view.session.token, shopPath(view.shop) + "/sales/products");
    if (!currentShop(view)) return;
    byId("sales-product").replaceChildren(...products.map((product) => {
      const option = new Option(product.product_id, product.product_id);
      option.dataset.lastDate = product.last_date;
      return option;
    }));
    setSalesDates();
    byId("sales-submit").disabled = !products.length;
    byId("sales-notice").textContent = products.length ? "" : "暂无可对比的商品。";
  } catch (error) {
    if (!currentShop(view)) return;
    if (error.status === 401) loginRequired();
    else {
      view.salesProductsRequested = false;
      byId("sales-notice").textContent = "商品读取失败。" + error.message;
    }
  }
}

function renderSales(data) {
  const points = [...data.baseline.points, ...data.current.points];
  const change = (metric) => {
    const { absolute, relative_pct } = data.change[metric];
    return (absolute > 0 ? "+" : "") + format.format(absolute) +
      (relative_pct === null ? "（基准为 0）" : "（" + format.format(relative_pct) + "%）");
  };
  byId("sales-source").textContent = data.source_label + " · " + data.product_id + " · 原始下单日期 · " +
    data.included_statuses.join(" / ") + " · 仅本卖家行项";
  byId("sales-totals").replaceChildren(...[["基准窗", data.baseline], ["当前窗", data.current]].map(([label, window]) =>
    element("span", label + " " + window.start_date + "–" + window.end_date + " · " +
      format.format(window.totals.units) + " 件 / " + format.format(window.totals.orders) + " 单")),
  element("span", "变化 · " + change("units") + " 件 / " + change("orders") + " 单"));
  for (const [label, window] of [["基准", data.baseline], ["当前", data.current]]) {
    for (const point of window.points) appendRow(byId("sales-points"), [label, point.date, point.units, point.orders]);
  }
  byId("sales-result").hidden = false;
  if (!window.echarts) return;
  const gapLabel = "日期间隔";
  const daysApart = Date.parse(data.current.start_date) - Date.parse(data.baseline.end_date);
  if (daysApart > 86_400_000) {
    points.splice(data.baseline.points.length, 0, { date: gapLabel, units: null, orders: null });
  }
  salesChart = echarts.init(byId("sales-chart"));
  salesChart.setOption({
    animation: false,
    color: ["#14594d", "#9a573b"],
    legend: { top: 6 },
    grid: { left: 58, right: 24, top: 52, bottom: 44 },
    tooltip: { trigger: "axis" },
    xAxis: {
      type: "category", data: points.map((point) => point.date),
      axisLabel: { formatter: (day) => day === gapLabel ? gapLabel : day.slice(5) },
    },
    yAxis: { type: "value", minInterval: 1 },
    series: [
      { name: "销量（件）", type: "bar", data: points.map((point) => point.units), barMaxWidth: 28 },
      { name: "去重订单数", type: "line", data: points.map((point) => point.orders), connectNulls: false, symbolSize: 5 },
    ],
  });
}

async function querySales(event) {
  event.preventDefault();
  const view = shopView;
  if (!currentShop(view)) return;
  const params = new URLSearchParams({ product_id: byId("sales-product").value });
  for (const key of salesDates) params.set(key, byId(key).value);
  if (byId("sales-status").value) params.set("status", byId("sales-status").value);
  clearSalesResult();
  const request = salesRequest;
  byId("sales-notice").textContent = "正在读取销量…";
  try {
    const data = await api(view.session.token, shopPath(view.shop) + "/sales?" + params);
    if (request !== salesRequest || !currentShop(view)) return;
    byId("sales-notice").textContent = "";
    renderSales(data);
  } catch (error) {
    if (request !== salesRequest || !currentShop(view)) return;
    if (error.status === 401) loginRequired();
    else byId("sales-notice").textContent = "销量读取失败。" + error.message;
  }
}

function renderList(data, params, offset) {
  const olist = data.source === "olist-v2";
  byId("total-orders").textContent = format.format(data.totals.orders);
  byId("total-items").textContent = format.format(data.totals.items);
  byId("source-label").textContent = data.source_label;
  byId("shop-id").textContent = data.shop_id;
  byId("source-note").textContent = olist
    ? "仅供非商业评测。时间沿用源文件，时区未知。原始状态与交运时间为整单口径，行项仅含本卖家。"
    : "仅含美国站卖家自发货（FBM）订单。时间以 UTC 显示，发货截止为整单口径。";
  byId("olist-links").hidden = !olist;
  byId("sales-panel").hidden = !olist;
  if (olist) loadSalesProducts(shopView);
  byId("time-column").textContent = olist ? "源更新时间" : "整单发货截止（UTC）";
  byId("amazon").hidden = olist;
  const status = params.get("status") || "";
  const statuses = new Set(data.statuses);
  if (status) statuses.add(status);
  byId("status").replaceChildren(new Option("全部状态", ""), ...Array.from(statuses, (value) => new Option(value, value)));
  byId("status").value = status;
  byId("search").value = params.get("search") || "";
  const rows = byId("orders");
  rows.replaceChildren();
  for (const order of data.orders) {
    const target = new URLSearchParams(params);
    target.set("order", order.order_id);
    const link = element("a", order.order_id, "id");
    link.href = "/orders?" + target.toString();
    link.addEventListener("click", (event) => {
      if (event.button || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      go(target);
    });
    appendRow(rows, [link, element("span", unknown(order.status), "status"),
      unknown(order.ordered_at), order.item_count, unknown(olist ? order.source_updated_at : order.ship_by)]);
  }
  if (!data.orders.length) emptyRow(rows, 5, "没有符合条件的订单");
  const count = data.orders.length;
  byId("range").textContent = count
    ? "第 " + format.format(offset + 1) + "–" + format.format(offset + count) + " 条，共 " + format.format(data.matching_orders) + " 单"
    : "共 " + format.format(data.matching_orders) + " 单";
  byId("previous").disabled = offset === 0;
  byId("next").disabled = offset + pageSize >= data.matching_orders;
  const runs = byId("runs");
  runs.replaceChildren();
  for (const run of data.runs) {
    const result = runResult(run) + (run.pages == null ? "" : " · 已读取 " + format.format(run.pages) + " 页");
    appendRow(runs, [run.finished_at, operations[run.operation], result,
      run.orders_written == null ? "—" : run.orders_written + " / " + run.items_written]);
  }
  if (!data.runs.length) emptyRow(runs, 4, "暂无接入记录");
  byId("workspace").hidden = false;
  byId("notice").textContent = "";
}

async function renderDetail(view, orderId) {
  const request = ++detailRequest;
  const { session: owner, shop } = view;
  if (!orderId) {
    clearDetail();
    return;
  }
  byId("detail-title").textContent = "订单详情";
  byId("detail-source").textContent = "";
  const content = byId("detail-content");
  content.textContent = "正在读取订单…";
  if (!detail.open) detail.showModal();
  try {
    const order = await api(owner.token, shopPath(shop) + "/orders/" + encodeURIComponent(orderId));
    if (request !== detailRequest || !currentShop(view)) return;
    byId("detail-title").textContent = order.order_id;
    byId("detail-source").textContent = shop.source_label;
    const olist = order.source === "olist-v2";
    const fields = [
      [olist ? "原始状态（整单）" : "原始状态", order.status],
      [olist ? "下单时间（时区未知）" : "下单时间（UTC）", order.ordered_at],
      ["源更新时间", order.source_updated_at],
      ["同步时间（UTC）", order.synced_at],
      ...(olist ? [["交运时间（整单）", order.carrier_at], ["混合卖家订单", order.mixed_seller ? "是，仅展示本店行项" : "否"]]
        : [["发货截止（整单）", order.ship_by]]),
    ];
    const facts = element("dl");
    for (const [label, value] of fields) {
      const field = element("div");
      field.append(element("dt", label), element("dd", unknown(value)));
      facts.append(field);
    }
    const wrap = element("div", undefined, "table-wrap");
    const table = element("table");
    const header = table.createTHead().insertRow();
    for (const label of ["行项 ID", olist ? "产品 ID" : "SKU", "数量", ...(olist ? ["发货截止（行项）"] : [])]) {
      const th = element("th", label);
      th.scope = "col";
      header.append(th);
    }
    const items = table.createTBody();
    for (const item of order.items) {
      appendRow(items, [item.item_id, unknown(olist ? item.product_id : item.sku), unknown(item.quantity),
        ...(olist ? [unknown(item.ship_by)] : [])]);
    }
    if (!order.items.length) emptyRow(items, header.cells.length, "暂无行项");
    wrap.append(table);
    content.replaceChildren(facts, element("h2", "店铺行项 · " + order.items.length), wrap);
  } catch (error) {
    if (request !== detailRequest || !currentShop(view)) return;
    if (error.status === 401) loginRequired();
    else content.textContent = error.message || "订单读取失败";
  }
}

async function render() {
  if (!currentSession(session)) return;
  const params = query();
  const shop = selectedShop(params);
  if (!shop) return;
  const request = ++listRequest;
  if (shopView?.shop !== shop) {
    clearShop();
    shopView = { session, shop, loadingSync: false };
  }
  const view = shopView;
  byId("shop").value = String(shops.indexOf(shop));
  if (params.get("source") !== shop.source || params.get("shop") !== shop.shop_id) {
    if (params.has("source") || params.has("shop")) {
      for (const key of ["order", "search", "status", "offset"]) params.delete(key);
    }
    params.set("source", shop.source);
    params.set("shop", shop.shop_id);
    go(params, true);
    return;
  }
  const inputOffset = Number(params.get("offset"));
  const offset = Number.isSafeInteger(inputOffset) && inputOffset > 0 ? inputOffset : 0;
  const filters = new URLSearchParams({ offset, limit: pageSize });
  for (const key of ["status", "search"]) {
    if (params.get(key)) filters.set(key, params.get(key));
  }
  renderDetail(view, params.get("order"));
  try {
    const data = await api(view.session.token, shopPath(shop) + "?" + filters);
    if (request === listRequest && currentShop(view)) renderList(data, params, offset);
  } catch (error) {
    if (request !== listRequest || !currentShop(view)) return;
    if (error.status === 401) loginRequired();
    else byId("notice").textContent = "订单列表读取失败。" + error.message;
  }
}

async function sync(operation) {
  const view = shopView;
  if (!currentShop(view) || view.loadingSync) return;
  const notice = byId("sync-notice");
  const window = { updated_after: byId("after").value.trim(), updated_before: byId("before").value.trim() };
  view.loadingSync = true;
  byId("preflight").disabled = byId("sync").disabled = true;
  notice.hidden = false;
  notice.textContent = operations[operation] + "进行中…";
  try {
    const options = { method: "POST" };
    if (operation === "sync") {
      options.headers = { "Content-Type": "application/json" };
      options.body = JSON.stringify(window);
    }
    const receipt = await api(view.session.token, shopPath(view.shop) + "/" + operation, options);
    if (!currentShop(view)) return;
    notice.textContent = operations[operation] + "成功，写入 " + receipt.orders_written + " 单、" + receipt.items_written + " 条行项。";
  } catch (error) {
    if (!currentShop(view)) return;
    if (error.status === 401) {
      loginRequired();
      return;
    }
    const message = error.receipt ? runResult(error.receipt) : error.message;
    notice.textContent = message + (error.retryAfter ? "，请在 " + error.retryAfter + " 秒后重试。" : "");
  } finally {
    if (currentShop(view)) {
      view.loadingSync = false;
      byId("preflight").disabled = byId("sync").disabled = false;
      render();
    }
  }
}

byId("shop").addEventListener("change", () => {
  if (!currentSession(session)) return;
  const { source, shop_id } = shops[Number(byId("shop").value)];
  go(new URLSearchParams({ source, shop: shop_id }));
});
byId("filters").addEventListener("submit", (event) => {
  event.preventDefault();
  const params = query();
  for (const key of ["status", "search"]) {
    const value = byId(key).value.trim();
    if (value) params.set(key, value);
    else params.delete(key);
  }
  params.delete("offset");
  go(params);
});
byId("sales-filters").addEventListener("submit", querySales);
byId("sales-product").addEventListener("change", setSalesDates);
byId("sales-filters").addEventListener("change", () => {
  clearSalesResult();
  byId("sales-notice").textContent = "";
});
addEventListener("resize", () => salesChart?.resize());
for (const [id, delta] of [["previous", -pageSize], ["next", pageSize]]) {
  byId(id).addEventListener("click", () => {
    const params = query();
    params.set("offset", Math.max(0, Number(params.get("offset") || 0) + delta));
    go(params);
  });
}
function closeDetail() {
  const params = query();
  params.delete("order");
  go(params, true);
}
byId("close-detail").addEventListener("click", closeDetail);
detail.addEventListener("cancel", (event) => {
  event.preventDefault();
  closeDetail();
});
byId("preflight").addEventListener("click", () => sync("preflight"));
byId("orders-menu").addEventListener("click", () => {
  const open = byId("orders-nav").classList.toggle("open");
  byId("orders-menu").setAttribute("aria-expanded", String(open));
  if (open) byId("orders-nav-close").focus();
});
byId("orders-nav-close").addEventListener("click", closeOrdersNavigation);
function closeOrdersNavigation() {
  byId("orders-nav").classList.remove("open");
  byId("orders-menu").setAttribute("aria-expanded", "false");
  byId("orders-menu").focus();
}
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && byId("orders-nav").classList.contains("open")) {
    closeOrdersNavigation();
  }
});
byId("amazon").addEventListener("submit", (event) => {
  event.preventDefault();
  sync("sync");
});
addEventListener("popstate", render);

async function start() {
  clearSession();
  const requestSession = { token: sessionToken() };
  session = requestSession;
  byId("notice").textContent = "正在读取店铺权限…";
  try {
    const allowed = await api(requestSession.token, "/api/orders/shops");
    if (!currentSession(requestSession)) return;
    byId("orders-users").hidden = !["boss", "admin"].includes(savedLogin()?.user?.role);
    shops = allowed;
    if (!shops.length) {
      byId("notice").textContent = "当前账号没有可访问的店铺。";
      return;
    }
    byId("shop").replaceChildren(...shops.map((shop, index) => new Option(shop.source_label + " / " + shop.shop_id, index)));
    byId("shop-field").hidden = shops.length < 2;
    render();
  } catch (error) {
    if (!currentSession(requestSession)) return;
    if (error.status === 401) loginRequired();
    else byId("notice").textContent = "店铺读取失败。" + error.message;
  }
}

addEventListener("pagehide", clearSession);
addEventListener("pageshow", (event) => {
  if (event.persisted) start();
});
addEventListener("storage", (event) => {
  if (session && event.storageArea === sessionStorage && (event.key === "ad.login" || event.key === null)) {
    currentSession(session);
  }
});
start();
