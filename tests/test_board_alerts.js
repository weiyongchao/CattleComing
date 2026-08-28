const assert = require("node:assert/strict");
const {
  DEFAULT_SETTINGS,
  classifyAuctionCandidate,
  classifyLiveCandidate,
  eventKey,
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
}, DEFAULT_SETTINGS);
assert.equal(highTurnover.rule, "high-turnover-auction");
assert.equal(highTurnover.tone, "confirm");

const eventRisk = classifyAuctionCandidate({
  code: "600984",
  strategy_mode: "高换手强竞价连板",
  high_turnover_chain_matched: true,
  corporate_event_risk: { level: "high", label: "重大资产重组" },
}, DEFAULT_SETTINGS);
assert.equal(eventRisk.rule, "event-risk");
assert.equal(eventRisk.tone, "risk");

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
assert.equal(discovered.rule, "live-discovered");

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

console.log("board-alerts rules: OK");
