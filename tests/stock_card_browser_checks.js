// 仅在8001隔离假数据页面执行，检查真实DOM折叠与刷新行为。
(async () => {
  if (location.origin !== "http://127.0.0.1:8001") throw new Error("仅限隔离假数据服务");
  const checks = [];
  const check = (condition, message) => { if (!condition) throw new Error(message); checks.push(message); };
  const waitFor = async (test) => {
    const deadline = Date.now() + 10000;
    while (!test()) {
      if (Date.now() > deadline) throw new Error("等待界面更新超时");
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
  };
  const tick = () => new Promise((resolve) => setTimeout(resolve, 30));
  // 只重置本次隔离页面的展开偏好，使脚本可重复执行。
  document.querySelectorAll("details.stock-disclosure[open]").forEach((item) => { item.open = false; });
  await tick();
  const cards = () => [...document.querySelectorAll("#boardCandidates > details.stock-disclosure")];
  const first = cards()[0];
  const second = cards()[1];
  check(cards().length === 5 && cards().every((item) => !item.open), "五只竞价卡片可以分别收起");
  first.querySelector("summary").click();
  await tick();
  check(first.open && !second.open, "展开第一只不影响第二只");
  second.querySelector("summary").click();
  await tick();
  check(first.open && second.open, "允许同时展开两只，不是互斥手风琴");
  first.querySelector("summary").click();
  await tick();
  check(!first.open && second.open, "单独收起第一只不影响第二只");
  document.getElementById("boardPlanButton").click();
  await waitFor(() => !second.isConnected && cards().length === 5);
  check(!cards()[0].open && cards()[1].open, "重新获取竞价数据后保持逐只展开状态");
  check(document.querySelectorAll("#boardCandidates details.stock-disclosure details.stock-disclosure").length === 0, "刷新不会嵌套重复的折叠包装");
  const saved = JSON.parse(localStorage.getItem("stock-card-expansion-v1"));
  check(saved.includes(cards()[1].dataset.stockKey) && !saved.includes(cards()[0].dataset.stockKey), "展开与收起偏好已写入独立本地存储");
  await waitFor(() => document.querySelector("#boardOpenGuardList > details.stock-disclosure"));
  const live = document.querySelector("#boardOpenGuardList > details.stock-disclosure");
  check(!live.open, "盘中首选默认精简展示");
  live.querySelector("summary").click();
  await tick();
  document.getElementById("boardOpenGuardButton").click();
  await waitFor(() => !live.isConnected && document.querySelector("#boardOpenGuardList > details.stock-disclosure"));
  check(document.querySelector("#boardOpenGuardList > details.stock-disclosure").open, "盘中刷新保留首选展开状态");
  document.querySelector('[data-tab="strategyTab"]').click();
  await waitFor(() => !document.getElementById("strategyTab").hidden && document.querySelector(".strategy-focus-card > details.stock-disclosure"));
  const strategy = document.querySelector(".strategy-focus-card > details.stock-disclosure");
  check(strategy && !strategy.open, "策略详情与盘中卡片折叠状态相互独立");
  strategy.querySelector("summary").click();
  await tick();
  const support = document.querySelector('#strategyChecklist [data-key="support"]');
  support.click();
  check(strategy.open && document.querySelector('#strategyChecklist [data-key="support"]').classList.contains("active"), "展开后手工核验仍能操作，重绘不会自动收起");
  strategy.querySelector("summary").click();
  await tick();
  check(!strategy.open && document.getElementById("strategyDecision").textContent.length > 0, "收起策略详情仍保留外部核心决策");
  const date = new Date().toLocaleDateString("en-CA");
  const host = document.createElement("div");
  document.body.append(host);
  const content = document.createElement("article");
  content.textContent = "测试风险详情";
  host.append(content);
  const risk = window.StockCardDetails.mount(content, { date, scope: "test", item: { code: "600999", name: "风险测试", failed_board: true }, decision: "停止追入" });
  check(!risk.open && risk.querySelector("summary").textContent.includes("炸板风险"), "折叠时高风险标记仍在摘要中");
  window.StockCardDetails.unwrap(content);
  check(content.parentElement === host && !host.querySelector("details"), "名单失效时可以撤掉旧股票摘要而保留提示容器");
  host.remove();
  return { passed: checks.length, checks };
})()
