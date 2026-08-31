(() => {
  const STORAGE_KEY = "stock-card-expansion-v1";
  const validKey = (key) => typeof key === "string" && /^\d{4}-\d{2}-\d{2}\|[a-z-]+\|\d{6}$/.test(key);
  const keyFor = (date, scope, code) => {
    const key = `${date}|${scope}|${code}`;
    return validKey(key) ? key : null;
  };
  const createStore = (storage) => {
    let saved = [];
    try { saved = JSON.parse(storage?.getItem(STORAGE_KEY) || "[]"); } catch (_) { /* 损坏的显示偏好不影响行情。 */ }
    const expanded = new Set(Array.isArray(saved) ? saved.filter(validKey).slice(-500) : []);
    return {
      isOpen: (key) => expanded.has(key),
      remember: (key, open) => {
        if (!validKey(key)) return;
        if (open) expanded.add(key); else expanded.delete(key);
        while (expanded.size > 500) expanded.delete(expanded.values().next().value);
        try { storage?.setItem(STORAGE_KEY, JSON.stringify([...expanded])); } catch (_) { /* 存储禁用时，本页仍可独立折叠。 */ }
      },
    };
  };
  const riskLabel = (item) => {
    if (item.corporate_event_risk?.level === "high") return item.corporate_event_risk.label || "重大事项风险";
    if (item.regulatory_risk?.level === "high") return item.regulatory_risk.label || "异动高风险";
    if (item.failed_board || item.near_limit_failure) return item.failed_board ? "炸板风险" : "冲板未封";
    if (item.risk_veto) return "风险否决 · 不属于推荐";
    if (item.corporate_event_risk?.available === false) return "公告数据待核验";
    return "";
  };
  const api = { STORAGE_KEY, keyFor, createStore, riskLabel };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (typeof window === "undefined" || typeof document === "undefined") return;
  let storage;
  try { storage = window.localStorage; } catch (_) { /* 隐私模式使用内存偏好。 */ }
  const store = createStore(storage);
  const text = (tag, className, value) => {
    const element = document.createElement(tag);
    element.className = className;
    element.textContent = String(value ?? "");
    return element;
  };

  const mount = (content, { item, date, scope, index, metrics = "", decision, tone }) => {
    if (!content || !item) return;
    let details = content.parentElement?.matches("details.stock-disclosure") ? content.parentElement : null;
    if (!details) {
      details = document.createElement("details");
      details.className = "stock-disclosure";
      if (content.classList.contains("auction-only-card")) details.classList.add("auction-only-card");
      details.append(document.createElement("summary"));
      content.before(details);
      content.classList.add("stock-disclosure-content");
      details.append(content);
      details.addEventListener("toggle", () => {
        if (details.isConnected) store.remember(details.dataset.stockKey, details.open);
      });
    }
    const key = keyFor(date, scope, item.code);
    if (details.dataset.stockKey !== key) {
      store.remember(details.dataset.stockKey, details.open);
      details.dataset.stockKey = key || "";
      details.open = store.isOpen(key);
    }
    details.dataset.stockCode = String(item.code || "");
    const summary = details.querySelector(":scope > summary");
    summary.className = "stock-disclosure-summary";
    const identity = document.createElement("span");
    identity.className = "stock-disclosure-identity";
    identity.append(text("strong", "", `${index == null ? "" : `${index + 1}. `}${item.name || item.code}`),
      text("small", "", `${item.code || ""}${item.industry ? ` · ${item.industry}` : ""}`));
    const values = document.createElement("span");
    values.className = "stock-disclosure-values";
    const risk = riskLabel(item);
    const color = risk ? "negative" : ["positive", "negative", "neutral"].includes(tone) ? tone : "neutral";
    values.append(text("b", color, decision || item.decision || item.action || "仅观察"), text("small", "", metrics));
    if (risk) values.append(text("small", "stock-disclosure-risk", risk));
    const toggle = document.createElement("span");
    toggle.className = "stock-disclosure-toggle";
    toggle.append(text("span", "stock-disclosure-expand", "展开详情 ▾"), text("span", "stock-disclosure-collapse", "收起详情 ▴"));
    summary.replaceChildren(identity, values, toggle);
    return details;
  };
  const enhance = (container, items, options) => {
    if (!container) return;
    const cards = [...container.querySelectorAll(":scope > article")];
    cards.forEach((card, index) => {
      if (items[index]) mount(card, { ...options(items[index], index), item: items[index], index });
    });
  };
  const unwrap = (content) => {
    const details = content?.parentElement;
    if (!details?.matches("details.stock-disclosure")) return;
    store.remember(details.dataset.stockKey, details.open);
    details.before(content);
    content.classList.remove("stock-disclosure-content");
    details.remove();
  };
  window.StockCardDetails = { ...api, mount, enhance, unwrap };
})();
