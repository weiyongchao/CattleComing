(() => {
  const page = document.getElementById("strategyTab");
  if (!page) return;

  const byId = (id) => document.getElementById(id);
  const JOURNAL_KEY = "stock-strategy-execution-journal-v1";
  const timeline = [
    [915, "T-1候选池", "主线、低位、公告风险"],
    [920, "09:15–09:20", "只观察，可撤单"],
    [925, "09:20–09:25", "不可撤单，判断真实竞价"],
    [930, "09:25–09:30", "只建观察池，不下单"],
    [1500, "09:30以后", "承接、板块、封板确认"],
    [2400, "次日退出", "按预案处理，不临时改规则"],
  ];
  const state = { board: null, intraday: null, selectedCode: "", manual: {} };
  let loaded = false;
  let refreshing = false;

  const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  })[char]);
  const formatMoney = (value) => `${(Number(value || 0) / 1e8).toFixed(2)}亿`;
  const signed = (value, digits = 2) => `${Number(value) >= 0 ? "+" : ""}${Number(value || 0).toFixed(digits)}%`;
  const currentHHMM = () => {
    const now = new Date();
    return now.getHours() * 100 + now.getMinutes();
  };
  const liveSnapshotFresh = () => {
    const stamp = new Date(state.intraday?.generated_at).getTime();
    const now = new Date();
    const today = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
    return state.intraday?.selected_date === today && !state.intraday?.historical
      && Number.isFinite(stamp) && Date.now() - stamp <= 60000 && stamp <= Date.now() + 5000;
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
    if (state.selectedCode && state.intraday?.selected_date && !liveSnapshotFresh()) {
      state.selectedCode = "";
      renderMarket();
      renderCandidates();
    } else if (state.selectedCode) renderDecision();
  };

  const getIntradayCandidate = (code) => state.intraday?.candidates?.find((item) => item.code === code);
  const displayCandidates = () => {
    if (!liveSnapshotFresh() || !state.intraday?.daily_focus?.available) return [];
    const pool = [...(state.board?.candidates || []), ...(state.board?.watch_candidates || [])];
    return (state.intraday.candidates || []).filter((item) => item.primary_pick === true && item.recommended === true && item.code === state.intraday.daily_focus.primary_code).slice(0, 1).map((item) => ({ ...pool.find((row) => row.code === item.code), ...item, action: item.decision }));
  };
  const getSelected = () => displayCandidates().find((item) => item.code === state.selectedCode);

  const renderMarket = () => {
    const market = state.board.market;
    const gate = byId("strategyMarketGate");
    gate.textContent = `${market.score}分 · ${market.state}`;
    gate.className = market.state === "可观察" ? "positive" : market.state === "空仓" ? "negative" : "neutral";
    byId("strategyMarketMetrics").innerHTML = [
      ["竞价合格", `${state.board.screening.qualified_count || 0}只`, "09:25最终结果"],
      ["当前首选", `${displayCandidates().length}/1只`, "有效时不频繁换股"],
      ["今日已提示", `${state.intraday.daily_focus?.issued_count ?? "--"}/5只`, "全天不同代码累计"],
      ["盘中监控", `${state.intraday.monitored_count || 0}只`, "观察池不是推荐名单"],
      ["主力确认", `${market.fund_confirmed_count || 0}只`, "最近可用资金数据"],
      ["有效采样", "至少2次", "间隔≥20秒，非逐笔确认"],
    ].map(([label, value, note]) => `<div><span>${label}</span><strong>${value}</strong><small>${note}</small></div>`).join("");
  };

  const renderCandidates = () => {
    const candidates = displayCandidates();
    const focus = state.intraday?.daily_focus;
    byId("strategyDailyFocus").textContent = focus?.message || "竞价仅建观察池，等待盘中唯一首选；未达标可以一只不选。";
    byId("strategyFocusLock").hidden = Boolean(focus?.locked_code);
    byId("strategyFocusLock").disabled = !candidates.length || !liveSnapshotFresh();
    byId("strategyFocusUnlock").hidden = !focus?.locked_code;
    if (!candidates.length) {
      window.StockCardDetails.unwrap(byId("strategyFocusBody"));
      byId("strategyCandidates").innerHTML = '<div class="state">当前没有可提示的首选。等待质量门槛与两次有效采样，不凑数。</div>';
      byId("strategyFocus").textContent = "等待盘中精选，其他候选仅在打板页观察池监控。";
      byId("strategyChecklist").innerHTML = "";
      byId("strategyDecision").textContent = "等待封板确认";
      byId("strategyDecision").className = "neutral";
      byId("strategyDecisionReason").textContent = "当前无可执行精选。";
      return;
    }
    byId("strategyCandidates").innerHTML = candidates.map((item, index) => {
      const live = getIntradayCandidate(item.code);
      return `<button class="strategy-candidate ${item.code === state.selectedCode ? "selected" : ""}" data-code="${escapeHtml(item.code)}" type="button">
        <b class="strategy-candidate-rank">${index + 1}</b>
        <span class="strategy-candidate-main"><strong>${escapeHtml(item.name)} <small>${escapeHtml(item.code)} · ${escapeHtml(item.industry)}</small></strong><small>竞价 ${signed(item.auction_gap_percent)} · 竞价额 ${formatMoney(item.auction_amount)} · ${live ? `盘中${signed(live.change_percent)}` : "未进入盘中前排"}</small></span>
        <span class="strategy-candidate-score"><b class="${item.actionable || item.board_entry_allowed ? "positive" : item.action === "取消候选" ? "negative" : "neutral"}">${escapeHtml(item.action)}</b><small class="${item.entry_plan?.tone || "neutral"}">未持有：${escapeHtml(item.entry_plan?.action || "观望")}</small><small>潜力评分 ${item.potential_score ?? "--"} · ${live?.confirmation_samples || 0}/2次采样</small></span>
      </button>`;
    }).join("");
    byId("strategyCandidates").querySelectorAll("button[data-code]").forEach((button) => {
      button.addEventListener("click", () => selectCandidate(button.dataset.code));
    });
  };

  const checksFor = (candidate) => {
    const live = getIntradayCandidate(candidate.code);
    const manual = state.manual[candidate.code] || {};
    const eventRisk = candidate.corporate_event_risk || {};
    const reorganizationHigh = eventRisk.level === "high";
    return [
      { key: "mainboard", label: "沪深主板且非ST", passed: !candidate.name.toUpperCase().includes("ST"), auto: true },
      { key: "auction", label: "通过唯一首选门槛且未锁定提示", passed: live?.primary_pick === true && live?.recommended === true && !live?.focus_locked, auto: true },
      { key: "sector", label: "盘中量价承接未转弱", passed: Boolean(live && live.tone !== "reject"), auto: true },
      { key: "support", label: "开盘价与竞价价承接有效", passed: Boolean(manual.support), auto: false },
      { key: "initiative", label: "冲板主动、卖压被消化", passed: Boolean(manual.initiative), auto: false },
      { key: "announcement", label: reorganizationHigh ? `重大事项：${eventRisk.label}` : "已核验公告、减持与解禁风险", passed: reorganizationHigh ? false : Boolean(manual.announcement), auto: reorganizationHigh },
    ];
  };

  const renderFocus = () => {
    const candidate = getSelected();
    if (!candidate) return;
    const live = getIntradayCandidate(candidate.code);
    const entryPlan = live?.entry_plan || candidate.entry_plan;
    const eventRisk = live?.corporate_event_risk || candidate.corporate_event_risk;
    byId("strategyFocus").className = "";
    byId("strategyFocus").innerHTML = `<div class="strategy-focus-head"><div><h3>${escapeHtml(candidate.name)} <small>${escapeHtml(candidate.code)}</small></h3><p>${escapeHtml(candidate.industry || "行业待核验")} · ${escapeHtml(live?.discovery_source || "09:25竞价观察")}</p></div><b class="${live?.tone === "confirm" ? "positive" : live?.tone === "reject" ? "negative" : "neutral"}">${live ? escapeHtml(live.decision || "盘中观察") : "等待盘中确认"}</b></div>
      <div class="strategy-focus-stats">
        <div><span>竞价高开</span><strong>${signed(candidate.auction_gap_percent)}</strong></div>
        <div><span>竞价成交额</span><strong>${formatMoney(candidate.auction_amount)}</strong></div>
        <div><span>竞价 / MA5</span><strong>${signed(candidate.price_vs_ma5_percent)}</strong></div>
        <div><span>盘中验证</span><strong>${live ? `${live.passed}/${live.known_total}项` : "尚未读取"}</strong></div>
      </div>
      <div class="strategy-decision-reason"><b>潜力评分 ${candidate.potential_score ?? "--"}/100</b><p>${escapeHtml((candidate.potential_basis || []).join("；"))}</p><small>只是相对筛选分数，不是收益概率；只关注一只也不代表满仓。</small></div>
      ${eventRisk ? `<div class="strategy-decision-reason" style="border-color:${eventRisk.level === "high" ? "#7a3338" : "#2b3c36"}"><b class="${eventRisk.level === "high" ? "negative" : eventRisk.level === "normal" ? "positive" : "neutral"}">重大事项风险：${escapeHtml(eventRisk.label)}</b><small style="display:block;margin-top:5px">${escapeHtml(eventRisk.summary || "")}</small></div>` : ""}
      ${entryPlan ? `<div class="strategy-decision-reason"><b class="${entryPlan.tone || "neutral"}">未持有：${escapeHtml(entryPlan.action)}</b><p>${escapeHtml(entryPlan.timing)}</p><small>${escapeHtml(entryPlan.reference_text || "")}</small></div>` : ""}`;
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
    window.StockCardDetails.mount(byId("strategyFocusBody"), { item: candidate,
      date: state.intraday.selected_date, scope: "strategy",
      metrics: `现价 ${Number(live?.price || 0).toFixed(2)} · 涨幅 ${signed(live?.change_percent)} · 潜力 ${candidate.potential_score ?? "--"}分`,
      decision: entryPlan?.action || live?.decision || "等待确认", tone: entryPlan?.tone || "neutral" });
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
    if (state.intraday?.daily_focus?.locked_code) {
      label = "已选定，只看风险";
      tone = "neutral";
      reason = "今天不再提示新的买入对象；锁定只管理提醒，不代表已成交。";
    } else if (phase.index < 4) {
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

  const load = async (automatic = false) => {
    if (refreshing) return;
    refreshing = true;
    const loading = byId("strategyLoading");
    const error = byId("strategyError");
    const results = byId("strategyResults");
    loading.hidden = Boolean(automatic && loaded);
    error.hidden = true;
    if (!automatic || !loaded) results.hidden = true;
    byId("strategyRefresh").disabled = true;
    try {
      const boardResponse = await fetch("/api/board-plan");
      const board = await boardResponse.json();
      if (!boardResponse.ok) throw new Error(board.error || "竞价策略读取失败");
      let intraday = { candidates: [], confirmed_count: 0 };
      if (currentHHMM() >= 930 && currentHHMM() < 1505) {
        intraday.selected_date = board.selected_date;
        try {
          const liveResponse = await fetch("/api/board-open-guard");
          const liveData = await liveResponse.json();
          if (liveResponse.ok && liveData.daily_focus) {
            intraday = liveData;
            window.dispatchEvent(new CustomEvent("board:live-snapshot", { detail: liveData }));
          } else throw new Error(liveData.error || "唯一首选数据未就绪，请重启服务加载新策略。");
        } catch (err) {
          error.textContent = `${err.message} 暂停首选展示，不沿用旧买点。`;
          error.hidden = false;
          window.dispatchEvent(new CustomEvent("board:live-unavailable"));
        }
      }
      state.board = board;
      state.intraday = intraday;
      const displayed = displayCandidates();
      state.selectedCode = displayed.some((item) => item.code === state.selectedCode) ? state.selectedCode : displayed[0]?.code || "";
      byId("strategyGeneratedAt").textContent = new Date(intraday.generated_at || board.generated_at).toLocaleString("zh-CN", { hour12: false });
      renderMarket();
      renderCandidates();
      if (state.selectedCode) renderFocus();
      renderJournal();
      results.hidden = false;
      loaded = true;
    } catch (err) {
      results.hidden = true;
      error.textContent = err.message;
      error.hidden = false;
      window.dispatchEvent(new CustomEvent("board:live-unavailable"));
    } finally {
      refreshing = false;
      loading.hidden = true;
      byId("strategyRefresh").disabled = false;
    }
  };

  byId("strategyRefresh").addEventListener("click", () => load(false));
  const setFocusLock = async (code) => {
    byId("strategyFocusLock").disabled = true;
    byId("strategyFocusUnlock").disabled = true;
    try {
      const response = await fetch("/api/board-focus/lock", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ date: state.intraday?.selected_date, code }),
      });
      const focus = await response.json();
      if (!response.ok) throw new Error(focus.error || "提示锁定设置失败");
      state.intraday.daily_focus = focus;
      if (code) (state.intraday.candidates || []).forEach((item) => { item.focus_locked = true; item.actionable = false; item.board_entry_allowed = false; });
      window.dispatchEvent(new CustomEvent("board:focus-policy", { detail: focus }));
      renderCandidates();
      renderDecision();
      await load(true);
    } catch (err) {
      byId("strategyError").textContent = err.message;
      byId("strategyError").hidden = false;
    } finally {
      byId("strategyFocusUnlock").disabled = false;
      byId("strategyFocusLock").disabled = !displayCandidates().length;
    }
  };
  byId("strategyFocusLock").addEventListener("click", () => { const candidate = getSelected(); if (candidate) setFocusLock(candidate.code); });
  byId("strategyFocusUnlock").addEventListener("click", () => setFocusLock(null));
  window.setInterval(() => {
    const hhmm = currentHHMM();
    if (!page.hidden && ((hhmm >= 930 && hhmm < 1130) || (hhmm >= 1300 && hhmm < 1457))) load(true);
  }, 20000);
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
  document.querySelector('[data-tab="strategyTab"]')?.addEventListener("click", () => { if (!loaded || !liveSnapshotFresh()) load(); });
  renderClock();
  renderJournal();
  load();
  window.setInterval(renderClock, 1000);
})();
