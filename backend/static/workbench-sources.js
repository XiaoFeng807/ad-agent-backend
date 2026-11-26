(function () {
  "use strict";

  const amazonSource = "amazon-us-fbm";
  let active = null;

  function element(parent, tag, text, className) {
    const node = document.createElement(tag);
    if (text) node.textContent = text;
    if (className) node.className = className;
    parent.append(node);
    return node;
  }

  function sourceCard(parent, name) {
    const section = element(parent, "section", "", "source-card");
    const heading = element(section, "div", "", "source-card-heading");
    element(heading, "h3", name);
    const status = element(heading, "span", "", "source-card-status");
    return { section, status };
  }

  function close(restoreFocus = true) {
    if (!active) return;
    const { dialog, trigger } = active;
    active = null;
    dialog.close();
    dialog.remove();
    if (restoreFocus) trigger?.focus({ preventScroll: true });
  }

  async function open() {
    if (!TOKEN || active) return;
    const token = TOKEN;
    const sessionId = chatViewId;
    const trigger = document.activeElement;
    const dialog = element(document.body, "dialog", "", "workbench-sources-dialog");
    dialog.id = "workbenchSourcesDialog";
    dialog.setAttribute("aria-labelledby", "workbenchSourcesTitle");
    const header = element(dialog, "header", "", "sources-dialog-header");
    const title = element(header, "h2", "数据来源");
    title.id = "workbenchSourcesTitle";
    const closeButton = element(header, "button", "关闭", "btn btn-outline btn-sm");
    closeButton.type = "button";
    closeButton.autofocus = true;
    closeButton.addEventListener("click", () => close());
    dialog.addEventListener("cancel", event => {
      event.preventDefault();
      close();
    });
    dialog.addEventListener("click", event => {
      if (event.target !== dialog) return;
      const { left, top, right, bottom } = dialog.getBoundingClientRect();
      if (event.clientX < left || event.clientX > right || event.clientY < top || event.clientY > bottom) close();
    });

    const cards = element(dialog, "div", "", "sources-dialog-content");
    const local = sourceCard(cards, "本地测试数据");
    local.status.textContent = "演示来源";
    local.status.dataset.state = "demo";
    element(local.section, "p", "用于演示销量分析和商品卖点审批。");

    const amazon = sourceCard(cards, "Amazon 美国站");
    amazon.status.setAttribute("role", "status");
    amazon.status.textContent = "读取中";
    element(amazon.section, "p", "FBM · 卖家自行发货 · 只读订单");
    const amazonDetail = element(amazon.section, "div", "", "source-card-detail");
    active = { dialog, trigger };
    dialog.showModal();

    const sameSession = () => active?.dialog === dialog && token === TOKEN && sessionId === chatViewId;
    try {
      const result = await api("GET", "/api/orders/shops");
      if (!sameSession()) return;
      if (result.code !== 200 || !Array.isArray(result.data)) throw new Error("shop_list_unavailable");
      const registrations = result.data.filter(shop => shop.source === amazonSource);
      if (!registrations.length) {
        amazon.status.textContent = "未登记";
        element(amazonDetail, "p", "卖家授权并登记店铺后，可验证订单连接。");
        return;
      }

      amazon.status.textContent = "已登记 · 待验证";
      element(amazonDetail, "p", "可验证授权并手动同步订单。工作台销量演示仍使用本地数据。");
      for (const shop of registrations) {
        const row = element(amazonDetail, "div", "", "source-shop-row");
        element(row, "span", shop.shop_id);
        const link = element(row, "a", "管理订单连接");
        link.href = "/orders?" + new URLSearchParams({ source: amazonSource, shop: shop.shop_id });
        link.setAttribute("aria-label", "管理订单连接 · " + shop.shop_id);
      }
    } catch {
      if (!sameSession()) return;
      amazon.status.textContent = "状态不可用";
      element(amazonDetail, "p", "暂未读到登记状态，请稍后重试。");
    }
  }

  window.openWorkbenchSources = open;
  window.closeWorkbenchSources = close;
})();
