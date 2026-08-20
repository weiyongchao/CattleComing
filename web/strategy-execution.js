(() => {
  const page = document.getElementById("strategyTab");
  if (!page) return;

  const byId = (id) => document.getElementById(id);
  const JOURNAL_KEY = "stock-strategy-execution-journal-v1";
  const timeline = [
    [915, "T-1候选池", "主线、低位、公告风险"],
    [920, "09:15–09:20", "只观察，可撤单"],
    [925, "09:20–09:25", "不可撤单，判断真实竞价"],
    [930, "09:25–09:30", "排序1–2只，不下单"],
    [1500, "09:30以后", "承接、板块、封板确认"],
    [2400, "次日退出", "按预案处理，不临时改规则"],
  ];
  const state = { board: null, intraday: null, selectedCode: "", manual: {} };
  let loaded = false;

  const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  })[char]);
  const formatMoney = (value) => `${(Number(value || 0) / 1e8).toFixed(2)}亿`;
  const signed = (value, digits = 2) => `${Number(value) >= 0 ? "+" : ""}${Number(value || 0).toFixed(digits)}%`;
  const currentHHMM = () => {
    const now = new Date();
    return now.getHours() * 100 + now.getMinutes();
  };
  const currentPhase = () => {
    const hhmm = currentHHMM();
    if (hhmm < 915) return { index: 0, title: "盘前准备", note: "先建立3–5只主板候选，不根据隔夜消息直接下单。" };
    if (hhmm < 920) return { index: 1, title: "可撤单竞价观察", note: "该阶段虚假大单较多，只记录板块方向和异常高开。" };
    if (hhmm < 925) return { index: 2, title: "不可撤单竞价确认", note: "重点观察匹配价格、匹配量和未匹配卖单变化。" };
    if (hhmm < 930) return { index: 3, title: "竞价候选排序", note: "将候选缩小至1–2只；此时仍不构成打板买点。" };
    if (hhmm < 1500) return { index: 4, title: "开盘承接验证", note: "只有板块共振、价格承接和冲板主动性同时成立，才保留封板观察资格。" };
    return { index: 5, title: "收盘复盘 / 次日预案", note: "盘后数据只用于复盘，不回写成盘中买入信号。" };
  };

  const renderClock = () => {
    const now = new Date();
    byId("strategyClock").textContent = now.toLocaleTimeString("zh-CN", { hour12: false });
    const phase = currentPhase();
    byId("strategyStageTitle").textContent = phase.title;
    byId("strategyStageNote").textContent = phase.note;
    byId("strategyTimeline").innerHTML = timeline.map((item, index) => `
      <div class="strategy-step ${index < phase.index ? "done" : index === phase.index ? "active" : ""}">
        <b>${item[1]}</b><span>${item[2]}</span>
      </div>`).join("");
    if (state.selectedCode) renderDecision();
  };

  const getIntradayCandidate = (code) => state.intraday?.candidates?.find((item) => item.code === code);
  const getSelected = () => state.board?.candidates?.find((item) => item.code === state.selectedCode);

  const renderMarket = () => {
    const market = state.board.market;
    const gate = byId("strategyMarketGate");
    gate.textContent = `${market.score}分 · ${market.state}`;
    gate.className = market.state === "可观察" ? "positive" : market.state === "空仓" ? "negative" : "neutral";
    byId("strategyMarketMetrics").innerHTML = [
      ["竞价合格", `${state.board.screening.qualified_count || 0}只`, "09:25最终结果"],
      ["A级候选", `${state.board.candidates.filter((item) => item.actionable).length}只`, "只保留观察资格"],
      ["竞价均值", signed(market.average_gap), "候选平均高开"],
      ["主力确认", `${market.fund_confirmed_count || 0}只`, "最近可用资金数据"],
      ["盘中合格", `${state.intraday.qualified_count || 0}只`, "实时承接筛选"],
    ].map(([label, value, note]) => `<div><span>${label}</span><strong>${value}</strong><small>${note}</small></div>`).join("");
  };

  const renderCandidates = () => {
    const candidates = state.board.candidates || [];
    if (!candidates.length) {
      byId("strategyCandidates").innerHTML = '<div class="state">今日没有竞价候选，保持空仓。</div>';
      return;
    }
    byId("strategyCandidates").innerHTML = candidates.map((item, index) => {
      const live = getIntradayCandidate(item.code);
      return `<button class="strategy-candidate ${item.code === state.selectedCode ? "selected" : ""}" data-code="${escapeHtml(item.code)}" type="button">
        <b class="strategy-candidate-rank">${index + 1}</b>
        <span class="strategy-candidate-main"><strong>${escapeHtml(item.name)} <small>${escapeHtml(item.code)} · ${escapeHtml(item.industry)}</small></strong><small>竞价 ${signed(item.auction_gap_percent)} · 竞价额 ${formatMoney(item.auction_amount)} · ${live ? `盘中${signed(live.change_percent)}` : "未进入盘中前排"}</small></span>
        <span class="strategy-candidate-score"><b class="${item.actionable ? "positive" : item.action === "取消候选" ? "negative" : "neutral"}">${escapeHtml(item.action)}</b><small>${item.score}分 · ${item.guard_passed}/${item.guard_total}项</small></span>
      </button>`;
    }).join("");
    byId("strategyCandidates").querySelectorAll("button[data-code]").forEach((button) => {
      button.addEventListener("click", () => selectCandidate(button.dataset.code));
    });
  };

  const checksFor = (candidate) => {
    const live = getIntradayCandidate(candidate.code);
    const manual = state.manual[candidate.code] || {};
    return [
      { key: "mainboard", label: "沪深主板且非ST", passed: !candidate.name.toUpperCase().includes("ST"), auto: true },
      { key: "auction", label: "09:25竞价A级候选", passed: Boolean(candidate.actionable), auto: true },
      { key: "sector", label: "板块同步走强", passed: Boolean(live && live.sector_change_percent > 0), auto: true },
      { key: "support", label: "开盘价与竞价价承接有效", passed: Boolean(manual.support), auto: false },
      { key: "initiative", label: "冲板主动、卖压被消化", passed: Boolean(manual.initiative), auto: false },
      { key: "announcement", label: "已核验公告、减持与解禁风险", passed: Boolean(manual.announcement), auto: false },
    ];
  };

  const renderFocus = () => {
    const candidate = getSelected();
    if (!candidate) return;
    const live = getIntradayCandidate(candidate.code);
    byId("strategyFocus").className = "";
    byId("strategyFocus").innerHTML = `<div class="strategy-focus-head"><div><h3>${escapeHtml(candidate.name)} <small>${escapeHtml(candidate.code)}</small></h3><p>${escapeHtml(candidate.category)} / ${escapeHtml(candidate.industry)} · 09:25排名候选</p></div><b class="${live ? "positive" : "neutral"}">${live ? escapeHtml(live.tier || "盘中观察") : "等待盘中共振"}</b></div>
      <div class="strategy-focus-stats">
        <div><span>竞价高开</span><strong>${signed(candidate.auction_gap_percent)}</strong></div>
        <div><span>竞价成交额</span><strong>${formatMoney(candidate.auction_amount)}</strong></div>
        <div><span>竞价 / MA5</span><strong>${signed(candidate.price_vs_ma5_percent)}</strong></div>
        <div><span>盘中验证</span><strong>${live ? `${live.passed}/5项` : "未进入前排"}</strong></div>
      </div>`;
    const checks = checksFor(candidate);
    byId("strategyChecklist").innerHTML = checks.map((check) => `<button type="button" class="strategy-check ${check.passed ? "active" : ""} ${check.auto ? "auto" : ""}" data-key="${check.key}" ${check.auto ? "disabled" : ""}><span>${check.label}</span><i>${check.passed ? "✓" : "○"}</i></button>`).join("");
    byId("strategyChecklist").querySelectorAll("button:not([disabled])").forEach((button) => {
      button.addEventListener("click", () => {
        const current = state.manual[candidate.code] || {};
        state.manual[candidate.code] = { ...current, [button.dataset.key]: !current[button.dataset.key] };
        renderFocus();
        renderDecision();
      });
    });
    byId("strategyPrice").value = Number(live?.price || candidate.auction_price || 0).toFixed(2);
    renderSizing();
    renderDecision();
  };

  const renderDecision = () => {
    const candidate = getSelected();
    if (!candidate) return;
    const checks = checksFor(candidate);
    const passed = checks.filter((check) => check.passed).length;
    const phase = currentPhase();
    const marketAllowed = state.board.market.state !== "空仓";
    let label = "取消交易";
    let tone = "negative";
    let reason = `仅通过${passed}/${checks.length}项，未满足完整守卫条件。`;
    if (phase.index < 4) {
      label = "只观察，不下单";
      tone = "neutral";
      reason = "当前仍处于竞价筛选阶段；9:25候选必须等待9:30后的真实成交验证。";
    } else if (phase.index > 4) {
      label = "盘后仅复盘";
      tone = "neutral";
      reason = "已过盘中执行时段，当前结果不得追溯为买入信号。";
    } else if (marketAllowed && passed === checks.length) {
      label = "保留封板观察资格";
      tone = "positive";
      reason = "全部守卫条件已确认；仍需等待涨停附近真实卖盘被消化，不代表一定成交或盈利。";
    } else if (!marketAllowed) {
      reason = "市场总开关为“空仓”，个股条件不能覆盖系统性风险。";
    }
    const decision = byId("strategyDecision");
    decision.textContent = label;
    decision.className = tone;
    byId("strategyDecisionReason").textContent = reason;
  };

  const renderSizing = () => {
    const capital = Math.max(0, Number(byId("strategyCapital").value || 0));
    const percent = Math.min(20, Math.max(1, Number(byId("strategyPercent").value || 20)));
    const price = Math.max(0, Number(byId("strategyPrice").value || 0));
    byId("strategyPercent").value = percent;
    if (!price) {
      byId("strategySizing").textContent = "输入计划价格后计算。";
      return;
    }
    const budget = capital * percent / 100;
    const shares = Math.floor(budget / price / 100) * 100;
    const exposure = shares * price;
    const limitRisk = exposure * 0.10;
    byId("strategySizing").innerHTML = shares > 0
      ? `最多 <b>${shares.toLocaleString("zh-CN")}股</b> · 约¥${exposure.toLocaleString("zh-CN", { maximumFractionDigits: 0 })}<br>若次日跌停，账面风险约¥${limitRisk.toLocaleString("zh-CN", { maximumFractionDigits: 0 })}`
      : "仓位预算不足买入一手；不提高20%上限。";
  };

  const selectCandidate = (code) => {
    state.selectedCode = code;
    renderCandidates();
    renderFocus();
  };

  const getJournal = () => {
    try { return JSON.parse(localStorage.getItem(JOURNAL_KEY) || "[]"); } catch { return []; }
  };
  const renderJournal = () => {
    const entries = getJournal();
    byId("strategyJournal").innerHTML = entries.length ? entries.map((entry) => `<div class="strategy-journal-entry"><time>${escapeHtml(entry.time)}</time><div><b>${escapeHtml(entry.name || "未选候选")} ${escapeHtml(entry.code || "")}</b><p>${escapeHtml(entry.note)}</p></div><small>${escapeHtml(entry.decision)}</small></div>`).join("") : '<small style="color:var(--muted)">暂无执行记录。</small>';
  };

  const load = async () => {
    const loading = byId("strategyLoading");
    const error = byId("strategyError");
    const results = byId("strategyResults");
    loading.hidden = false;
    error.hidden = true;
    results.hidden = true;
    byId("strategyRefresh").disabled = true;
    try {
      const [boardResponse, intradayResponse] = await Promise.all([fetch("/api/board-plan"), fetch("/api/intraday-plan")]);
      const [board, intraday] = await Promise.all([boardResponse.json(), intradayResponse.json()]);
      if (!boardResponse.ok) throw new Error(board.error || "竞价策略读取失败");
      if (!intradayResponse.ok) throw new Error(intraday.error || "盘中策略读取失败");
      state.board = board;
      state.intraday = intraday;
      state.selectedCode = board.candidates.find((item) => item.actionable)?.code || board.candidates[0]?.code || "";
      byId("strategyGeneratedAt").textContent = new Date(board.generated_at).toLocaleString("zh-CN", { hour12: false });
      renderMarket();
      renderCandidates();
      if (state.selectedCode) renderFocus();
      renderJournal();
      results.hidden = false;
      loaded = true;
    } catch (err) {
      error.textContent = err.message;
      error.hidden = false;
    } finally {
      loading.hidden = true;
      byId("strategyRefresh").disabled = false;
    }
  };

  byId("strategyRefresh").addEventListener("click", load);
  ["strategyCapital", "strategyPercent", "strategyPrice"].forEach((id) => byId(id).addEventListener("input", renderSizing));
  byId("strategyJournalForm").addEventListener("submit", (event) => {
    event.preventDefault();
    const note = byId("strategyJournalNote").value.trim();
    if (!note) return;
    const selected = getSelected();
    const entries = getJournal();
    entries.unshift({
      time: new Date().toLocaleString("zh-CN", { hour12: false }),
      code: selected?.code || "", name: selected?.name || "",
      decision: byId("strategyDecision").textContent, note,
    });
    localStorage.setItem(JOURNAL_KEY, JSON.stringify(entries.slice(0, 30)));
    byId("strategyJournalNote").value = "";
    renderJournal();
  });
  byId("strategyClearJournal").addEventListener("click", () => {
    if (!window.confirm("确定清空全部本地执行记录吗？此操作无法撤销。")) return;
    localStorage.removeItem(JOURNAL_KEY);
    renderJournal();
  });
  document.querySelector('[data-tab="strategyTab"]')?.addEventListener("click", () => { if (!loaded) load(); });
  renderClock();
  renderJournal();
  load();
  window.setInterval(renderClock, 1000);
})();
