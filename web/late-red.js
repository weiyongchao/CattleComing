(() => {
  const byId = id => document.getElementById(id);
  const esc = value => String(value ?? "").replace(/[&<>"']/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
  const validNumber = value => value != null && value !== "" && Number.isFinite(Number(value));
  const fixed = (value, digits = 2) => validNumber(value) ? Number(value).toFixed(digits) : "--";
  const signed = (value, digits = 2) => validNumber(value) ? `${Number(value) >= 0 ? "+" : ""}${Number(value).toFixed(digits)}%` : "--";
  const money = value => validNumber(value) ? `${(Number(value) / 100000000).toFixed(2)}亿` : "--";
  let loading = false;
  let pollTimer = null;

  const renderRules = rule => {
    const entries = [
      ["执行时间", rule.time], ["股票范围", rule.universe], ["日线翻红", rule.first_red],
      ["均线拐点", rule.ma5], ["流动性", rule.liquidity], ["综合排名", rule.ranking],
      ["确定窗口", rule.execution], ["事件风控", rule.risk],
    ];
    byId("lateRedRules").innerHTML = entries.map(([name, value]) => `<div class="late-red-rule"><span>${esc(name)}</span><strong>${esc(value || "--")}</strong></div>`).join("");
  };

  const renderCandidate = (item, index) => {
    const reasons = (item.potential_reasons || []).map(esc).join(" · ");
    const mainFlow = item.main_flow_available ? `${money(item.main_net)} · ${signed(item.main_ratio)}` : "数据暂不可用";
    const factor = item.ranking_factor_scores || {};
    const factorScore = value => `${Number(value || 0) >= 0 ? "+" : ""}${Number(value || 0).toFixed(0)}`;
    const facts = [
      ["昨日涨跌", signed(item.previous_day_change_percent)],
      ["14:40 K线收盘", `${fixed(item.target_close)} · ${signed(item.target_change_percent)}`],
      ["10分钟MA5", `${fixed(item.ma5, 3)} · ${fixed(item.ma5_slope_bp)}bp`],
      ["翻红后保持（百分点）", fixed(item.signal_retention_percent)],
      ["当日累计主力净流入/占比", mainFlow],
      ["流通/总市值（仅参考）", `${money(item.float_market_cap)} / ${money(item.market_cap)}`],
      ["排名附加分", `资金${factorScore(factor.main_flow)} · 换手${factorScore(factor.turnover)} · 市值${factorScore(factor.market_cap)}`],
      ["本根/前5根均额", `${fixed(item.ten_minute_volume_ratio)}×`],
      ["当日流动性", `${money(item.amount)} · 换手${fixed(item.turnover_rate)}%`],
    ];
    return `<article class="late-red-candidate ${index === 0 ? "primary" : ""}" data-late-red-code="${esc(item.code)}">
      <div class="late-red-candidate-head">
        <div><h3>${index + 1}. ${esc(item.name)} <small>${esc(item.code)}</small>${index === 0 ? '<span class="late-red-badge">唯一首选观察</span>' : ""}</h3>
          <p class="late-red-warning">${reasons || "严格形态与风险过滤合格"}。潜力分仅用于同批候选排序，不改变入选逻辑。</p></div>
        <div style="text-align:right"><b class="positive">潜力分 ${fixed(item.potential_score, 1)}</b>
          <small style="display:block;color:var(--muted);margin-top:5px">排名快照价 ${fixed(item.current_price)} · ${signed(item.current_change_percent)}</small></div>
      </div>
      <div class="late-red-facts">${facts.map(([label, value]) => `<div><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`).join("")}</div>
    </article>`;
  };

  const schedulePoll = delay => {
    window.clearTimeout(pollTimer);
    pollTimer = window.setTimeout(() => {
      if (!byId("lateRedTab")?.hidden) loadLateRedScreen();
    }, delay);
  };

  async function loadLateRedScreen() {
    if (loading) return;
    loading = true;
    const loadingBox = byId("lateRedLoading"), errorBox = byId("lateRedError"), results = byId("lateRedResults");
    errorBox.hidden = true;
    try {
      const response = await fetch("/api/late-red-screen");
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "尾盘翻红筛选失败");
      renderRules(data.rule || {});
      byId("lateRedStage").textContent = data.stage || data.status || "等待数据";
      byId("lateRedStage").className = data.status === "ready" ? (data.qualified_count ? "positive" : "neutral") : data.status === "error" ? "negative" : "neutral";
      const rankedAt = data.refreshed_at || data.generated_at;
      byId("lateRedTime").textContent = rankedAt ? `${data.refreshed_at ? "排名快照" : "状态时间"} ${new Date(rankedAt).toLocaleString("zh-CN", {hour12:false})}${data.frozen ? " · 排名不再更新" : ""}` : "";
      if (data.status === "scanning") {
        loadingBox.hidden = false;
        results.hidden = true;
        schedulePoll(3000);
        return;
      }
      loadingBox.hidden = true;
      results.hidden = false;
      const metrics = [
        ["主板样本", data.scanned == null ? "--" : `${data.scanned}只`],
        ["翻红附近预筛", data.prefiltered == null ? "--" : `${data.prefiltered}只`],
        ["成功计算", data.evaluated == null ? "--" : `${data.evaluated}只`],
        ["严格形态", data.matched_count == null ? "0只" : `${data.matched_count}只`],
        ["风控合格", `${data.eligible_count ?? data.qualified_count ?? 0}只`],
      ];
      byId("lateRedMetrics").innerHTML = metrics.map(([name, value]) => `<article class="card late-red-metric"><span>${esc(name)}</span><strong>${esc(value)}</strong><small>${name === "成功计算" && data.coverage_percent != null ? `覆盖率 ${Number(data.coverage_percent).toFixed(1)}%` : ""}</small></article>`).join("");
      byId("lateRedCount").textContent = data.qualified_count ? `潜力前${data.qualified_count} · 唯一首选${esc(data.primary_code || "--")}` : "0只 · 宁可不选";
      byId("lateRedCount").className = data.qualified_count ? "positive" : "negative";
      const candidates = data.candidates || [];
      byId("lateRedCandidates").innerHTML = candidates.length ? candidates.map(renderCandidate).join("") : `<div class="late-red-empty">${data.status === "waiting" || data.status === "closed" ? esc(data.note || "等待14:40") : "当前没有同时满足昨日收绿、今日翻红、MA5刚拐头和风险过滤的股票。"}</div>`;
      const warnings = [data.note, data.timing_warning,
        data.data_degraded ? `行情覆盖率仅${data.coverage_percent || 0}%，结果不完整。` : "",
        data.main_flow_missing_count ? `${data.main_flow_missing_count}只合格票缺少主力资金数据，对应排名项不计分。` : "",
        data.current_data_warning].filter(Boolean);
      byId("lateRedNote").textContent = warnings.join(" ");
      schedulePoll(data.status === "ready" ? (data.frozen ? 60000 : 10000) : 30000);
    } catch (error) {
      loadingBox.hidden = true;
      errorBox.textContent = error.message;
      errorBox.hidden = false;
      schedulePoll(10000);
    } finally {
      loading = false;
    }
  }

  byId("lateRedRefresh")?.addEventListener("click", loadLateRedScreen);
  window.loadLateRedScreen = loadLateRedScreen;
})();
