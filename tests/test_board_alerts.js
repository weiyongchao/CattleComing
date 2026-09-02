const assert = require("node:assert/strict");
const {
  DEFAULT_SETTINGS,
  classifyAuctionCandidate,
  classifyLiveCandidate,
  classifyPriorityWatchCandidate,
  eventKey,
  notifiedCodes,
  canPrompt,
  normalizeSettings,
  tradingDate,
  todayBoardCodes,
  todayPriorityWatchCodes,
  todayAlertEvents,
} = require("../web/board-alerts.js");

assert.equal(
  eventKey("2026-08-28", "600103", "sealed"),
  "2026-08-28|600103|sealed",
);

const highTurnover = classifyAuctionCandidate({
  code: "600103",
  strategy_mode: "高换手强竞价连板",
  high_turnover_chain_matched: true,
  eligible: true,
}, { rules: { highTurnover: true } });
assert.equal(highTurnover, null);
assert.equal(normalizeSettings({ rules: { auction: true, discovered: true } }).rules.auction, false);

const eventRisk = classifyAuctionCandidate({
  code: "600984",
  strategy_mode: "高换手强竞价连板",
  high_turnover_chain_matched: true,
  corporate_event_risk: { level: "high", label: "重大资产重组" },
}, DEFAULT_SETTINGS);
assert.equal(eventRisk, null); // 竞价快照（包括风险排除池）一律不预警

const failedBoard = classifyLiveCandidate({
  code: "002963",
  failed_board: true,
  sealed: true,
  tone: "confirm",
  decision: "炸板转弱 · 放弃追入",
}, DEFAULT_SETTINGS);
assert.equal(failedBoard.rule, "failed-board");
assert.equal(failedBoard.tone, "risk");

const discovered = classifyLiveCandidate({
  code: "003040",
  discovery_source: "09:30开盘补选",
  tone: "watch",
}, DEFAULT_SETTINGS);
assert.equal(discovered, null);

assert.equal(classifyLiveCandidate({ sealed: true, tone: "confirm" }), null);
assert.equal(classifyLiveCandidate({ tone: "confirm", rebound_confirmed: true }), null);
assert.equal(classifyLiveCandidate({ recommended: true, sealed: true, recommendation_kind: "sealed" }), null);
assert.equal(classifyLiveCandidate({ primary_pick: true, recommended: true, sealed: true, recommendation_kind: "sealed" }).rule, "sealed");
assert.equal(classifyLiveCandidate({ primary_pick: true, recommended: true, recommendation_kind: "strong_open" }).rule, "open-confirm");
assert.equal(classifyLiveCandidate({ primary_pick: true, recommended: true, focus_locked: true, sealed: true, recommendation_kind: "sealed" }), null);
assert.equal(classifyAuctionCandidate({ actionable: true, recommendation_badge: "旧版推荐" }, { rules: { auction: true } }), null);
assert.equal(classifyLiveCandidate({ recommended: true, sealed: true, recommendation_kind: "sealed", failed_board: true }).tone, "risk");
assert.equal(classifyLiveCandidate({ recommended: true, sealed: true, recommendation_kind: "sealed", corporate_event_risk: { level: "high" } }).tone, "risk");

assert.equal(classifyPriorityWatchCandidate({ status: "triggered", alert_level: "watch",
  formal_recommendation: false, recommended: false, actionable: false }).tone, "watch");
assert.equal(classifyPriorityWatchCandidate({ status: "approaching", alert_level: "watch",
  formal_recommendation: false, recommended: false, actionable: false }).rule, "watch-approaching");
assert.equal(classifyPriorityWatchCandidate({ status: "invalid", alert_level: "risk",
  formal_recommendation: false, recommended: false, actionable: false }).tone, "risk");
assert.equal(classifyPriorityWatchCandidate({ status: "triggered", alert_level: "watch",
  formal_recommendation: true, recommended: true, actionable: true }), null);

const disabledHighTurnover = classifyAuctionCandidate({
  code: "600103",
  strategy_mode: "高换手强竞价连板",
  high_turnover_chain_matched: true,
  eligible: true,
}, {
  ...DEFAULT_SETTINGS,
  rules: { ...DEFAULT_SETTINGS.rules, highTurnover: false },
});
assert.equal(disabledHighTurnover, null);

const today = "2026-08-31";
const legacy = { date: today, events: [{ code: "600001", tone: "confirm" }, { code: "600002", tone: "risk" }],
  triggered: { [`${today}|600003|open-confirm`]: true, [`${today}|600001|sealed`]: true }, notified_codes: ["600004"] };
assert.deepEqual(notifiedCodes(legacy, today), ["600004", "600001", "600003"]);
assert.deepEqual(notifiedCodes(legacy, "2026-09-01"), []);
assert.deepEqual(notifiedCodes({ date: today, events: {} }, today), []);
const codes = [];
for (let index = 1; index <= 7; index++) {
  const code = `60000${index}`;
  if (canPrompt(codes, code)) codes.push(code);
}
assert.equal(codes.length, 5);
assert.equal(canPrompt(codes, "600006"), false);
assert.equal(canPrompt(["600001"], "600001"), false);
assert.equal(canPrompt([], "600001", true), false);
assert.equal(canPrompt([], ""), false);
assert.deepEqual(notifiedCodes({ date: today, events: [], notified_codes: codes }, today), codes);
assert.equal(notifiedCodes({ date: today, notified_codes: [...codes, "600006"] }, today).length, 6); // 不截断旧版名额记录

assert.equal(tradingDate("2026-08-30T16:00:00Z"), today);
assert.equal(tradingDate("2026-08-31T16:00:00Z"), "2026-09-01");
assert.equal(tradingDate("bad"), null);
assert.equal(tradingDate(null), null);
assert.equal(tradingDate(undefined), null);
const focusSnapshot = { selected_date: today, daily_focus: { available: true, issued: [
  { code: "600001", first_at: "2026-08-31T09:32:00+08:00" },
  { code: "600002", first_at: "2026-08-30T10:00:00+08:00" },
  { code: "600003" },
] } };
const allowed = todayBoardCodes(focusSnapshot, today);
assert.deepEqual([...allowed], ["600001"]);
assert.equal(todayBoardCodes({ ...focusSnapshot, historical: true }, today).size, 0);
assert.equal(todayBoardCodes({ ...focusSnapshot, selected_date: "2026-08-30" }, today).size, 0);
assert.equal(todayBoardCodes({ ...focusSnapshot, daily_focus: { available: false, issued: focusSnapshot.daily_focus.issued } }, today).size, 0);
assert.equal(todayBoardCodes({ selected_date: today }, today).size, 0);
assert.equal(todayBoardCodes({ ...focusSnapshot, daily_focus: { available: true, issued: Array(6).fill(focusSnapshot.daily_focus.issued[0]) } }, today).size, 0);
const prioritySnapshot = { selected_date: today, priority_watch_candidates: [
  { code: "600010", priority_watch: true }, { code: "600011", priority_watch: false },
] };
const priorityAllowed = todayPriorityWatchCodes(prioritySnapshot, today);
assert.deepEqual([...priorityAllowed], ["600010"]);
assert.equal(todayPriorityWatchCodes({ ...prioritySnapshot, historical: true }, today).size, 0);
assert.equal(todayPriorityWatchCodes({ ...prioritySnapshot, priority_watch_candidates: Array(6).fill(prioritySnapshot.priority_watch_candidates[0]) }, today).size, 0);
const alerts = [
  { id: `${today}|600001|sealed`, code: "600001", tone: "confirm" },
  { id: `${today}|600001|failed-board`, code: "600001", tone: "risk" },
  { id: "2026-08-30|600001|sealed", code: "600001", tone: "confirm" },
  { id: `${today}|600002|failed-board`, code: "600002", tone: "risk" },
  { id: `${today}|600001|auction`, code: "600001", tone: "watch" },
  { id: `${today}|600001|sealed`, trade_date: "2026-08-30", code: "600001", tone: "confirm" },
];
assert.deepEqual(todayAlertEvents(alerts, today, allowed), alerts.slice(0, 2));
assert.deepEqual(todayAlertEvents(alerts, today, new Set()), []);
const priorityAlert = { id: `${today}|600010|watch-triggered`, trade_date: today,
  code: "600010", tone: "watch", scope: "priority_watch" };
assert.deepEqual(todayAlertEvents([priorityAlert], today, new Set(), priorityAllowed), [priorityAlert]);
assert.deepEqual(todayAlertEvents([priorityAlert], today, new Set(), new Set()), []);

console.log("board-alerts rules: OK");
