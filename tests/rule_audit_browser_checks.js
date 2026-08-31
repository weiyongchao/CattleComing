window.__ruleAuditChecks = null;
(async () => {
  const checks = [];
  const assert = (value, name) => { if (!value) throw new Error(name); checks.push(name); };
  const wait = async (test) => {
    const until = Date.now() + 8000;
    while (!test()) { if (Date.now() > until) throw new Error("界面等待超时"); await new Promise(resolve => setTimeout(resolve, 40)); }
  };
  const byId = id => document.getElementById(id);
  document.querySelector('[data-tab="historyReviewTab"]').click();
  await wait(() => document.querySelector(".review-audit"));
  const audit = document.querySelector(".review-audit");
  assert(document.querySelectorAll("[data-history-date]").length === 5, "即使旧接口多返回日期也只展示五日");
  assert(!audit.open, "五日对照默认折叠");
  audit.querySelector("summary").click();
  assert(audit.textContent.includes("5条候选（5条回放）"), "明确回放样本数");
  assert(audit.querySelectorAll("table").length === 2, "结构与分数分开对照");
  assert(audit.textContent.includes("3/5") && audit.textContent.includes("4/4"), "T日与T+1分母独立");
  assert(audit.textContent.includes("不自动升级推荐"), "实验不得自动升级买点");
  assert(!byId("historyReviewSummary").textContent.includes("准确率"), "累计指标不再冒称准确率");
  assert(byId("historyReviewDays").textContent.includes("待T+1复盘"), "最新日期待次日数据");
  assert(!byId("historyReviewDays").textContent.includes("+0.00%"), "缺失次日数据不显示零收益");
  assert(!byId("historyReviewDays").querySelector("strong b"), "候选名称不能注入HTML");
  assert(byId("historyReviewDays").textContent.includes("<b>文本</b>"), "候选名称按文本展示");
  document.querySelectorAll("[data-history-date]")[1].click();
  assert(byId("historyReviewDays").textContent.includes("次日强势"), "未涨停但强势上涨不判规则失败");
  assert(audit.textContent.includes("次日≥5%"), "明确次日强势统计线");
  assert(byId("historyReviewDays").textContent.includes("非实盘买点"), "逐日来源标签可见");
  const next = document.querySelectorAll("[data-history-date]")[0];
  next.click();
  assert(byId("historyReviewDays").textContent.includes("待T+1复盘"), "切日期不残留上一日结果");
  return {passed:checks.length,checks};
})().then(value => { window.__ruleAuditChecks = value; }).catch(error => { window.__ruleAuditChecks = {error:error.message}; });
