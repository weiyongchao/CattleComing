(() => {
  const button = document.getElementById("screenButton");
  const auctionButton = document.getElementById("auctionButton");
  if (!button) return;
  if (auctionButton) auctionButton.hidden = true;

  let loading = false;
  const amount = (value) => Number.isFinite(Number(value)) ? `${(Number(value) / 1e8).toFixed(2)}亿` : "--";
  const signed = (value, digits = 2) => Number.isFinite(Number(value)) ? `${Number(value) >= 0 ? "+" : ""}${Number(value).toFixed(digits)}` : "--";
  const fixedValue = (value, digits = 2) => Number.isFinite(Number(value)) ? Number(value).toFixed(digits) : "--";

  const contextCard = (context = {}) => {
    const global = context.global_market || {};
    const policy = context.policy || {};
    const indexes = (global.indexes || []).map(item => `<span style="padding:5px 8px;border-radius:12px;background:#18231f;color:${item.change_percent >= 0 ? "var(--green)" : "var(--red)"}">${item.name} ${signed(item.change_percent)}%</span>`).join("");
    const themes = (policy.signals || []).slice(0, 6).map(item => `<span title="${(item.policies || []).map(policyItem => policyItem.title).join("；")}" style="padding:5px 8px;border-radius:12px;background:#2b2b21;color:var(--amber)">${item.theme} +${item.score}</span>`).join("");
    return `<div style="display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap"><strong>政策与外围环境</strong><b class="${global.adjustment >= 0 ? "positive" : "negative"}">${global.state || "外围数据暂不可用"}</b></div>
      <div style="display:flex;gap:7px;flex-wrap:wrap;margin-top:10px">${indexes || '<span class="neutral">外围指数暂不可用</span>'}</div>
      <div style="display:flex;gap:7px;flex-wrap:wrap;margin-top:8px">${themes || '<span class="neutral">近期未识别到行业政策主题</span>'}</div>
      <small style="display:block;color:var(--muted);margin-top:9px">${context.note || "宏观信息仅作辅助修正。"}</small>`;
  };

  const card = (item, index) => {
    const rebound = item.strategy_type === "超跌修复";
    const auction = item.auction_reference || {};
    return `<article class="action-box" style="display:grid;grid-template-columns:42px 1fr auto;align-items:start;gap:14px;margin-bottom:10px">
      <b style="font-size:22px;color:${rebound ? "var(--amber)" : "var(--green)"}">${index + 1}</b>
      <div>
        <strong>${item.name} <small style="color:var(--muted)">${item.code} · ${item.industry}</small> <small style="padding:3px 7px;border-radius:10px;background:${rebound ? "#332b18" : "#183126"};color:${rebound ? "var(--amber)" : "var(--green)"}">${item.strategy_type}</small> <small style="padding:3px 7px;border-radius:10px;background:#1d2730;color:#8fc7ff">${item.opportunity_level || "精选"}</small></strong>
        <p style="color:#bfd0ca;margin:7px 0 0;font-size:12px;line-height:1.8">现价 ${fixedValue(item.price)} · 当日 ${signed(item.change_percent)}% · 近5/20/60日 ${signed(item.return_5_percent)}% / ${signed(item.return_20_percent)}% / ${signed(item.return_60_percent)}%</p>
        <p style="color:var(--muted);margin:3px 0 0;font-size:12px;line-height:1.8">MA5/10/20/60 ${fixedValue(item.ma5)} / ${fixedValue(item.ma10)} / ${fixedValue(item.ma20)} / ${fixedValue(item.ma60)} · 20日波动 ${fixedValue(item.volatility_20_percent)}% · 最大回撤 ${fixedValue(item.max_drawdown_20_percent)}% · 60日高点回撤 ${signed(item.drawdown_from_high_60_percent)}%</p>
        <p style="color:var(--muted);margin:3px 0 0;font-size:12px;line-height:1.8">实时量比 ${fixedValue(item.current_volume_ratio)} · 实时主力 ${signed(item.current_main_ratio)}% · 换手 ${fixedValue(item.turnover_rate)}% · 观察区 ${fixedValue(item.entry_price_low)}–${fixedValue(item.entry_price_high)} · 趋势失效参考 ${fixedValue(item.invalidation_price)}</p>
        <p style="color:${auction.available ? "#bfd0ca" : "var(--muted)"};margin:3px 0 0;font-size:12px;line-height:1.8">09:25竞价参考：${auction.available ? `${auction.quality} · 高开${signed(auction.gap_percent)}% · 竞价额${amount(auction.amount)}` : auction.quality || "暂不可用"}</p>
        <p style="color:var(--muted);margin:3px 0 0;font-size:12px;line-height:1.8">环境修正 ${signed(item.macro_adjustment, 1)}分 · ${item.global_state || "外围数据暂不可用"}${(item.policy_themes || []).length ? ` · 政策匹配 ${(item.policy_themes || []).join("/")}` : ""}</p>
        <small class="positive">依据：${(item.reasons || []).join(" · ") || (rebound ? "超跌后止跌转强" : "多因子趋势通过")}</small>
        <small style="display:block;color:var(--amber);margin-top:5px">${item.entry_note}</small>
        ${(item.risks || []).length ? `<small class="negative" style="display:block;margin-top:5px">风险：${item.risks.join("；")}</small>` : ""}
      </div>
      <div style="text-align:right;min-width:105px"><b class="${rebound ? "neutral" : "positive"}">${fixedValue(item.display_score, 1)}分</b><small style="display:block;margin-top:5px">基础${fixedValue(item.base_opportunity_score, 1)} · 环境${signed(item.macro_adjustment, 1)}</small></div>
    </article>`;
  };

  async function loadDailyRecommendations() {
    if (loading) return;
    loading = true;
    const loadingBox = document.getElementById("screening");
    const results = document.getElementById("screenResults");
    const error = document.getElementById("screenError");
    const activeButton = document.getElementById("screenButton");
    loadingBox.hidden = false;
    results.hidden = true;
    error.hidden = true;
    activeButton.disabled = true;
    try {
      const response = await fetch("/api/screen");
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "每日推荐筛选失败");
      document.getElementById("screenTime").textContent = `${new Date(data.generated_at).toLocaleString("zh-CN", {hour12: false})} · 30秒动态刷新`;
      document.getElementById("externalMarketContext").innerHTML = contextCard(data.external_context);
      document.getElementById("candidateList").innerHTML = data.candidates.length
        ? data.candidates.map(card).join("")
        : `<div class="state">今天没有同时通过趋势、资金、量比和买入位置精选门槛的股票，不为凑数强行推荐。</div>`;
      document.getElementById("screenNote").textContent = `全主板${data.scanned}只 · 深度扫描${data.deep_scanned}只 · 趋势精选${data.trend_qualified_count}只 · 修复精选${data.rebound_qualified_count}只 · 最终展示${data.candidates.length}只 · 失败${data.failed}只。${data.method} ${data.disclaimer}`;
      results.hidden = false;
    } catch (exception) {
      error.textContent = exception.message;
      error.hidden = false;
    } finally {
      loadingBox.hidden = true;
      activeButton.disabled = false;
      loading = false;
    }
  }

  const replacement = button.cloneNode(true);
  button.replaceWith(replacement);
  replacement.addEventListener("click", loadDailyRecommendations);
  screenLeaders = loadDailyRecommendations;
  window.setInterval(() => {
    if (!document.getElementById("screenTab")?.hidden) loadDailyRecommendations();
  }, 30000);
})();
