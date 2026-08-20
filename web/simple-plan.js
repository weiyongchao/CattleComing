(() => {
  const $ = (id) => document.getElementById(id);
  const fixed = (value, digits = 2) => Number.isFinite(Number(value)) ? Number(value).toFixed(digits) : "--";
  const signed = (value, digits = 2) => Number.isFinite(Number(value)) ? `${Number(value) >= 0 ? "+" : ""}${Number(value).toFixed(digits)}` : "--";
  const amount = (value) => `${Number(value) >= 0 ? "+" : ""}${(Number(value) / 1e8).toFixed(2)}亿`;
  const price = (value) => value == null ? "--" : `¥${Number(value).toFixed(2)}`;
  const tone = (action) => ["可建仓", "可加仓"].includes(action) ? "positive" : ["减仓", "控制仓位"].includes(action) ? "negative" : "neutral";

  const renderAuction = (data) => {
    $("simpleAuctionGate").textContent = `${data.gate.score}分 · ${data.gate.state}`;
    $("simpleAuctionGate").className = data.gate.state === "可观察" ? "positive" : data.gate.state === "空仓" ? "negative" : "neutral";
    $("simpleAuction").innerHTML = data.candidates.length ? data.candidates.map((item, index) => `
      <article class="action-box" style="display:grid;grid-template-columns:34px 1fr auto;gap:12px;align-items:center">
        <b style="color:var(--amber)">${index + 1}</b><div><strong>${item.name} <small style="color:var(--muted)">${item.code}</small></strong>
        <p style="color:var(--muted);font-size:12px;margin:6px 0 0">竞价 ${signed(item.auction_gap_percent)}% · 竞价额 ${amount(item.auction_amount)} · 近3/5/10日 ${signed(item.three_day_change_percent)}% / ${signed(item.five_day_change_percent)}% / ${signed(item.ten_day_change_percent)}% · 前序主力 ${item.decision_main_ratio == null ? "暂不可用" : `${signed(item.decision_main_ratio)}%`} · 近20日涨停 ${item.recent_limit_up_count ?? "--"}次</p></div>
        <div style="text-align:right"><b class="${item.actionable ? "positive" : "neutral"}">${item.action}</b><small style="display:block">${item.score}分</small></div></article>`).join("") : `<div class="state">今天没有达到竞价门槛的候选，允许空仓。</div>`;
  };

  const renderIntraday = (data) => {
    $("simpleIntradayGate").textContent = `${data.market.score}分 · ${data.market.state}`;
    $("simpleIntradayGate").className = data.market.state === "可观察" ? "positive" : data.market.state === "空仓" ? "negative" : "neutral";
    $("simpleIntraday").innerHTML = data.candidates.length ? data.candidates.map((item, index) => `
      <article class="action-box" style="display:grid;grid-template-columns:34px 1fr auto;gap:12px;align-items:center">
        <b style="color:var(--green)">${index + 1}</b><div><strong>${item.name} <small style="color:var(--muted)">${item.code} · ${item.leader_label}</small></strong>
        <p style="color:var(--muted);font-size:12px;margin:6px 0 0">涨跌 ${signed(item.change_percent)}% · 量比 ${fixed(item.volume_ratio)} · 换手 ${fixed(item.turnover_rate)}% · 买卖盘 ${item.order_signal} · 资金 ${item.funds.label}${item.funds.available ? `（${amount(item.funds.main_net)}）` : ""}</p>
        <small style="color:#bfd0ca">${item.simple_reason}</small></div><div style="text-align:right"><b class="${tone(item.simple_action)}">${item.simple_action}</b><small style="display:block">${item.score}分</small></div></article>`).join("") : `<div class="state">当前没有通过龙头和资金确认的盘中候选。</div>`;
  };

  const renderHolding = (data) => {
    const decision = data.decision, funds = data.funds, quote = data.quote;
    $("simpleHoldingAction").textContent = decision.action;
    $("simpleHoldingAction").className = tone(decision.action);
    const profit = decision.profit_percent == null ? "未填写成本" : `${signed(decision.profit_percent)}%`;
    $("simpleHolding").innerHTML = `<div class="operation-grid">
      <div class="action-box"><span>${quote.name} ${quote.code}</span><strong>${price(quote.price)} · ${data.score}分 · ${data.rating}</strong><small style="display:block;color:var(--muted)">持仓盈亏 ${profit}</small></div>
      <div class="action-box"><span>主力资金</span><strong class="${funds.available && funds.score >= 15 ? "positive" : funds.available && funds.score <= -20 ? "negative" : "neutral"}">${funds.label}${funds.available ? ` · ${amount(funds.main_net)}` : ""}</strong><small style="display:block;color:var(--muted)">${funds.available ? `${funds.date} · ${funds.source}` : funds.error || "资金待确认"}</small></div>
      <div class="action-box"><span>五档买卖盘</span><strong>${data.order_book.signal}</strong><small style="display:block;color:var(--muted)">买卖不平衡 ${fixed(Number(data.order_book.imbalance) * 100, 1)}%</small></div>
      <div class="action-box"><span>结论依据</span><strong>${decision.reason}</strong><small style="display:block;color:var(--muted)">现价相对MA5 ${signed(data.metrics.price_vs_ma5_percent)}%</small></div>
    </div><div class="operation-grid">
      <div class="action-box"><span>建仓区间</span><strong>${decision.build.enabled ? `${price(decision.build.price_low)}–${price(decision.build.price_high)}` : "暂不建仓"}</strong></div>
      <div class="action-box"><span>加仓触发</span><strong>${decision.add.enabled ? price(decision.add.price) : "暂不加仓"}</strong></div>
      <div class="action-box"><span>减仓参考</span><strong>${decision.reduce.enabled ? price(decision.reduce.price) : "未触发"}</strong></div>
      <div class="action-box"><span>退出参考</span><strong>${decision.exit.enabled ? price(decision.exit.price) : "未触发"}</strong></div>
    </div>${data.risk_points.length ? `<p class="disclaimer negative">风险：${data.risk_points.join("；")}</p>` : ""}`;
  };

  const load = async () => {
    $("simpleLoading").hidden = false; $("simpleError").hidden = true; $("simpleResults").hidden = true;
    const code = $("simpleCode").value.trim() || "600519";
    const cost = $("simpleCost").value || "0";
    const shares = $("simpleShares").value || "0";
    try {
      const response = await fetch(`/api/simple-plan?code=${encodeURIComponent(code)}&cost=${encodeURIComponent(cost)}&shares=${encodeURIComponent(shares)}`);
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "简明决策生成失败");
      $("simpleStage").textContent = data.stage;
      renderAuction(data.auction); renderIntraday(data.intraday); renderHolding(data.position);
      $("simpleTime").textContent = new Date(data.generated_at).toLocaleString("zh-CN");
      $("simpleRules").innerHTML = data.data_rules.map((rule) => `<p>${rule}</p>`).join("");
      $("simpleDisclaimer").textContent = data.disclaimer;
      $("simpleResults").hidden = false;
    } catch (error) {
      $("simpleError").textContent = error.message; $("simpleError").hidden = false;
    } finally {
      $("simpleLoading").hidden = true;
    }
  };

  $("simpleForm").addEventListener("submit", (event) => { event.preventDefault(); load(); });
  document.querySelector('[data-tab="simpleTab"]').addEventListener("click", load);
  load();
})();
