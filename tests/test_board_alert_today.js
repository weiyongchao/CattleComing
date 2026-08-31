// 无浏览器、行情或业务文件写入：在最小DOM替身中回归实际事件入口和显示过滤。
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../web/board-alerts.js"), "utf8");
const TODAY = "2026-08-31";
const NOW = "2026-08-31T10:00:00+08:00";
const KEY = "board-condition-alerts-v2";
let passed = 0;
const check = (condition, message) => { assert.ok(condition, message); passed++; };

function harness(saved = {}) {
  let clock = new Date(NOW).getTime();
  class Clock extends Date {
    constructor(...args) { super(...(args.length ? args : [clock])); }
    static now() { return clock; }
  }
  const nodes = new Map();
  const listeners = new Map();
  const intervals = [];
  const storage = new Map([[KEY, JSON.stringify(saved)]]);
  const node = (id) => {
    if (!nodes.has(id)) nodes.set(id, {
      innerHTML: "", textContent: "", hidden: false, style: {}, handlers: {},
      classList: { toggle: () => false }, querySelector: () => null,
      addEventListener(name, fn) { this.handlers[name] = fn; }, removeAttribute() {},
      requestSubmit() { this.submitted = true; },
    });
    return nodes.get(id);
  };
  const window = { addEventListener: (name, fn) => listeners.set(name, fn),
    setInterval: (fn) => intervals.push(fn), requestAnimationFrame: (fn) => fn(), confirm: () => true };
  vm.runInNewContext(source, { window, document: { getElementById: node, querySelector: () => null },
    localStorage: { getItem: (key) => storage.get(key), setItem: (key, value) => storage.set(key, value) },
    Date: Clock, Intl, console });
  return { node, emit: (name, data) => listeners.get(name)?.({ detail: data }),
    saved: () => JSON.parse(storage.get(KEY)), html: () => node("boardAlertRows").innerHTML,
    tomorrow: () => { clock += 86400000; intervals.forEach((fn) => fn()); } };
}

const pick = (code = "600001") => ({ code, name: `测试${code}`, primary_pick: true, recommended: true,
  sealed: true, recommendation_kind: "sealed", price: 11, change_percent: 10 });
const snapshot = (code = "600001", changes = {}) => ({ selected_date: TODAY, generated_at: NOW,
  daily_focus: { available: true, primary_code: code, issued_count: 1,
    issued: [{ code, first_at: NOW }] }, candidates: [pick(code)], watch_candidates: [], ...changes });
const legacy = { date: TODAY, events: [{ id: `${TODAY}|600999|sealed`, code: "600999", name: "旧名单股票", tone: "confirm", unread: true }],
  triggered: { [`${TODAY}|600999|sealed`]: NOW }, notified_codes: ["600999"] };

const old = harness(legacy);
check(!old.html().includes("旧名单股票"), "刷新后未取得今日正式名单前不展示旧缓存股票");
old.node("boardAlertOpenStock").handlers.click();
check(!old.node("search").submitted, "打开按钮不能打开被过滤的旧股票");
old.emit("board:live-snapshot", snapshot("600001", { watch_candidates: [{ ...pick("600999"), failed_board: true }] }));
check(!old.html().includes("600999") && old.html().includes("600001"), "本地旧名额不能授权名单外股票显示或风险提示");
check(old.saved().events.length === 2 && old.saved().notified_codes.length === 2, "隐藏旧记录但不删除数据或返还名额");
check(old.node("boardAlertUnreadBadge").textContent === "1", "未读数只统计今日名单内记录");
old.emit("board:live-snapshot", snapshot("600002", { daily_focus: { available: true, primary_code: "600002", issued: [{ code: "600002", first_at: "2026-08-30T10:00:00+08:00" }] } }));
check(!old.html().includes('data-alert-id='), "当日响应混入昨日正式名单也不显示");

const app = harness();
app.emit("board:auction-snapshot", { selected_date: TODAY, auction_phase: "final", candidates: [pick()] });
check(!app.html().includes('data-alert-id='), "竞价快照不会触发条件预警");
app.emit("board:live-snapshot", snapshot("600001", { historical: true }));
app.emit("board:live-snapshot", snapshot("600001", { selected_date: "2026-08-30" }));
app.emit("board:live-snapshot", snapshot("600001", { generated_at: "2026-08-30T10:00:00+08:00" }));
check(!app.html().includes('data-alert-id='), "历史回放、昨日日期或昨日行情一律不触发");
app.emit("board:live-snapshot", snapshot());
check(app.saved().events.length === 1 && app.html().includes("600001"), "今日正式首选正常显示和提示");
app.emit("board:live-snapshot", snapshot());
check(app.saved().events.length === 1, "今日同票重复扫描不重发");
app.emit("board:live-snapshot", snapshot("600001", { candidates: [], watch_candidates: [
  { ...pick(), recommended: false, sealed: false, failed_board: true },
  { ...pick("600002"), recommended: false, failed_board: true },
] }));
check(app.saved().events.length === 2 && app.saved().events[0].tone === "risk", "今日正式名单股票失效后仍保留风险提醒");
check(!app.html().includes("600002"), "普通观察票即使炸板也不会混入预警");
app.emit("board:live-snapshot", snapshot("600002", { daily_focus: undefined }));
check(!app.html().includes('data-alert-id='), "接口缺少今日正式名单时停止显示与新增预警");
app.emit("board:live-snapshot", snapshot());
app.node("boardAlertClear").handlers.click();
check(app.saved().events.length === 0 && app.saved().notified_codes.length === 1, "清空展示不重置当日名额");
app.tomorrow();
check(!app.html().includes('data-alert-id=') && app.saved().date === "2026-09-01", "上海交易日期跨日后旧名单与显示归零");
app.emit("board:live-snapshot", snapshot());
check(!app.html().includes('data-alert-id='), "跨日后迟到的昨日快照不恢复旧预警");
console.log(`board-alerts today-only integration: ${passed} checks OK`);
