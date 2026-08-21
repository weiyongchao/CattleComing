(() => {
  const results = document.getElementById("boardPlanResults");
  const candidateList = document.getElementById("boardCandidates");
  if (!results || !candidateList) return;

  const loading = document.getElementById("boardPlanLoading");
  const errorBox = document.getElementById("boardPlanError");
  const dateButtons = document.getElementById("boardDateButtons");
  const refreshButton = document.getElementById("boardPlanButton");
  const autoStatus = document.getElementById("boardAutoStatus");
  const preselectBox = document.getElementById("boardPreselect");
  const preselectList = document.getElementById("boardPreselectList");
  const openGuardBox = document.getElementById("boardOpenGuard");
  const openGuardButton = document.getElementById("boardOpenGuardButton");
  const openGuardList = document.getElementById("boardOpenGuardList");
  const openGuardError = document.getElementById("boardOpenGuardError");
  const openGuardTime = document.getElementById("boardOpenGuardTime");
  const openGuardNote = document.getElementById("boardOpenGuardNote");
  const nextDayBox = document.getElementById("nextDayStrategy");
  const nextDayButton = document.getElementById("nextDayStrategyButton");
  const nextDayList = document.getElementById("nextDayStrategyList");
  const nextDayError = document.getElementById("nextDayStrategyError");
  const nextDayTime = document.getElementById("nextDayStrategyTime");
  const nextDayNote = document.getElementById("nextDayStrategyNote");
  if (loading) loading.textContent = "正在读取前序走势与09:20不可撤单阶段竞价数据…";

  let rendering = false;
  let openGuardLoading = false;
  let nextDayLoading = false;
  let selectedDate = "";
  const yuan = (value) => Number.isFinite(Number(value)) ? `¥${Number(value).toLocaleString("zh-CN", { maximumFractionDigits: 0 })}` : "--";
  const amount = (value) => Number.isFinite(Number(value)) ? `${(Number(value) / 1e8).toFixed(2)}亿` : "--";
  const fixed = (value, digits = 2) => Number.isFinite(Number(value)) ? Number(value).toFixed(digits) : "--";
  const signed = (value, digits = 2) => Number.isFinite(Number(value)) ? `${Number(value) >= 0 ? "+" : ""}${Number(value).toFixed(digits)}` : "--";
  const localDate = () => {
    const now = new Date();
    return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
  };
  const secondsOfDay = (now = new Date()) => now.getHours() * 3600 + now.getMinutes() * 60 + now.getSeconds();
  const scheduleAt = (hour, minute, second, callback) => {
    const now = new Date();
    const target = new Date(now);
    target.setHours(hour, minute, second, 0);
    const delay = target.getTime() - now.getTime();
    if (delay > 0) window.setTimeout(callback, delay);
    return delay;
  };

  const captureAuctionTrajectory = async () => {
    const current = secondsOfDay();
    if (current < 9 * 3600 + 20 * 60 || current >= 9 * 3600 + 25 * 60) return;
    try {
      const response = await fetch("/api/auction-trajectory");
      const data = await response.json();
      if (response.ok && autoStatus) autoStatus.textContent = `全主板20秒重扫 · 已记录${data.count}只候选五档挂单`;
    } catch (_) {
      // 轨迹采样失败不阻断下一轮全市场重扫。
    }
  };

  const loadNextDayStrategy = async () => {
    if (!nextDayBox || !nextDayList || nextDayLoading || selectedDate !== localDate() || secondsOfDay() < 14 * 3600 + 50 * 60) return;
    nextDayLoading = true;
    nextDayBox.hidden = false;
    if (nextDayButton) nextDayButton.disabled = true;
    if (nextDayError) nextDayError.hidden = true;
    nextDayList.innerHTML = `<div class="state">正在根据当天封板、炸板、收盘位置、量价和主力资金生成次日预案…</div>`;
    try {
      const response = await fetch("/api/next-day-strategy");
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "次日策略生成失败");
      const toneClass = (tone) => tone === "hold" ? "positive" : tone === "sell" ? "negative" : "neutral";
      nextDayList.innerHTML = data.candidates.length ? data.candidates.map((item, index) => `
        <article class="action-box">
          <div style="display:flex;justify-content:space-between;gap:12px;align-items:flex-start">
            <div><strong>${index + 1}. ${item.name} <small style="color:var(--muted)">${item.code} · ${item.board_stage_label || item.priority_tier || "持仓"}</small></strong>
              <p style="color:#bfd0ca;font-size:12px;line-height:1.8;margin:7px 0">收盘 ${fixed(item.price)} · 参考成本 ${fixed(item.entry_price)} · 持仓浮动 ${signed(item.holding_return_percent)}% · 当日 ${signed(item.change_percent)}% · 较开盘 ${signed(item.price_vs_open_percent)}%</p>
              <p style="color:#bfd0ca;font-size:12px;line-height:1.8;margin:0">收盘位置 ${fixed(item.close_position_percent, 1)}% · 量比 ${fixed(item.volume_ratio)} · 换手 ${fixed(item.turnover_rate)}% · ${item.sealed ? "封板" : item.failed_board ? "炸板" : "未封板"} · 当日主力 ${item.funds.available ? `${signed(item.funds.main_ratio)}% / ${amount(item.funds.main_net)}` : "暂不可用"}</p>
              <small style="display:block;color:var(--muted);margin-top:7px">${item.reason}${item.risk_reasons.length ? ` 风险：${item.risk_reasons.join("；")}` : ""}</small>
            </div>
            <div style="text-align:right;min-width:150px"><b class="${toneClass(item.tone)}">${item.decision}</b><small style="display:block;margin-top:5px">${data.finalized ? "收盘最终结论" : "14:50预案"}</small></div>
          </div>
          <div class="operation-grid" style="margin-top:12px"><div class="action-box"><span>低开≥3%</span><strong>${item.next_day_plan.weak_open}</strong></div><div class="action-box"><span>平开附近</span><strong>${item.next_day_plan.flat_open}</strong></div><div class="action-box"><span>高开1%–7%</span><strong>${item.next_day_plan.strong_open}</strong></div><div class="action-box"><span>高开≥8.5%</span><strong>${item.next_day_plan.extreme_open}</strong></div></div>
        </article>`).join("") : `<div class="state">09:25冻结名单为空，今日无需生成次日持仓策略。</div>`;
      if (nextDayTime) nextDayTime.textContent = `${data.stage} · 持有${data.hold_count} / 减仓${data.reduce_count} / 卖出${data.sell_count}`;
      if (nextDayNote) nextDayNote.textContent = `${data.method} ${data.disclaimer}`;
    } catch (error) {
      if (nextDayError) { nextDayError.textContent = error.message; nextDayError.hidden = false; }
      nextDayList.innerHTML = "";
    } finally {
      nextDayLoading = false;
      if (nextDayButton) nextDayButton.disabled = false;
    }
  };

  const loadOpenGuard = async () => {
    if (!openGuardBox || !openGuardList || openGuardLoading || selectedDate !== localDate() || secondsOfDay() < 9 * 3600 + 30 * 60) return;
    openGuardLoading = true;
    openGuardBox.hidden = false;
    if (openGuardButton) openGuardButton.disabled = true;
    if (openGuardError) openGuardError.hidden = true;
    openGuardList.innerHTML = `<div class="state">正在用真实开盘价、成交量、五档盘口和当日资金复核冻结候选…</div>`;
    try {
      const response = await fetch("/api/board-open-guard");
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "开盘确认失败");
      const toneClass = (tone) => tone === "confirm" ? "positive" : tone === "reject" ? "negative" : "neutral";
      openGuardList.innerHTML = data.candidates.length ? data.candidates.map((item, index) => `
        <article class="action-box">
          <div style="display:flex;justify-content:space-between;gap:12px;align-items:flex-start">
            <div><strong>${index + 1}. ${item.name} <small style="color:var(--muted)">${item.code} · ${item.board_stage_label || item.priority_tier || "候选"}</small></strong>
              <p style="color:#bfd0ca;font-size:12px;line-height:1.8;margin:7px 0">现价 ${fixed(item.price)} · 开盘 ${fixed(item.open_price)} · 竞价 ${fixed(item.auction_price)} · 涨跌 ${signed(item.change_percent)}% · 较开盘 ${signed(item.price_vs_open_percent)}% · 较竞价 ${signed(item.price_vs_auction_percent)}%</p>
              <p style="color:#bfd0ca;font-size:12px;line-height:1.8;margin:0">成交额 ${amount(item.amount)} · 量比 ${fixed(item.volume_ratio)} · 换手 ${fixed(item.turnover_rate)}% · ${item.order_signal} ${signed(Number(item.order_imbalance) * 100, 1)}% · ${item.funds.label}${item.funds.available ? ` ${signed(item.funds.main_ratio)}% / ${amount(item.funds.main_net)}` : ""}</p>
              <small style="display:block;color:var(--muted);margin-top:7px">${item.summary}</small>
            </div>
            <div style="text-align:right;min-width:145px"><b class="${toneClass(item.tone)}">${item.decision}</b><small style="display:block;margin-top:5px">开盘确认 ${item.open_score}分</small><small style="display:block">通过 ${item.passed}/${item.known_total}项</small></div>
          </div>
          <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:10px">${item.checks.map((check) => `<small title="${check.value}" style="padding:6px 9px;border-radius:14px;background:${check.passed == null ? "#242b29" : check.passed ? "#183126" : "#321d20"};color:${check.passed == null ? "var(--muted)" : check.passed ? "var(--green)" : "var(--red)"}">${check.passed == null ? "○" : check.passed ? "✓" : "✗"} ${check.name}</small>`).join("")}</div>
        </article>`).join("") : `<div class="state">09:25冻结名单为空，今日不生成开盘买入确认。</div>`;
      if (data.errors.length) openGuardList.insertAdjacentHTML("beforeend", `<small class="negative">${data.errors.length}只实时行情读取失败，未据此给出结论。</small>`);
      if (openGuardTime) openGuardTime.textContent = `名单固定 · 动态数据 ${new Date(data.generated_at).toLocaleString("zh-CN", {hour12: false})}`;
      if (openGuardNote) openGuardNote.textContent = `${data.method} ${data.disclaimer}`;
    } catch (error) {
      if (openGuardError) { openGuardError.textContent = error.message; openGuardError.hidden = false; }
      openGuardList.innerHTML = "";
    } finally {
      openGuardLoading = false;
      if (openGuardButton) openGuardButton.disabled = false;
    }
  };

  const loadPreselect = async () => {
    if (!preselectBox || !preselectList) return;
    preselectBox.hidden = false;
    preselectList.innerHTML = `<div class="state">正在扫描全部主板的前序交易日走势，首次约需1分钟…</div>`;
    try {
      const response = await fetch("/api/board-preselect");
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "盘前预选失败");
      document.getElementById("boardPreselectStage").textContent = `${data.date} · ${data.candidates.length}只`;
      preselectList.innerHTML = data.candidates.length ? data.candidates.map((item, index) => `
        <article class="action-box" style="display:grid;grid-template-columns:36px 1fr auto;gap:12px;align-items:center">
          <b style="font-size:22px;color:var(--amber)">${index + 1}</b>
          <div><strong>${item.name} <small style="color:var(--muted)">${item.code} · ${item.industry}</small></strong><p style="color:var(--muted);font-size:12px;line-height:1.8;margin:6px 0 0">近3/5/10日 ${signed(item.three_day_change_percent)}% / ${signed(item.five_day_change_percent)}% / ${signed(item.ten_day_change_percent)}% · 前日量比 ${fixed(item.previous_volume_ratio)} · 近20日涨停 ${item.recent_limit_up_count}次</p><small style="color:#bfd0ca">${item.reasons.join(" · ") || "等待竞价确认"}</small></div>
          <b class="neutral">${fixed(item.score, 1)}分</b>
        </article>`).join("") : `<div class="state">盘前没有达到趋势门槛的标的，09:20仍会按不可撤单阶段数据重新筛选。</div>`;
      document.getElementById("boardPreselectNote").textContent = `扫描${data.scanned}只，失败${data.failed}只。${data.method} ${data.disclaimer}`;
    } catch (error) {
      preselectList.innerHTML = `<div class="state error">${error.message}</div>`;
    }
  };

  const render = async (force = false) => {
    const cards = Array.from(candidateList.children);
    if (rendering || (!force && (!cards.length || cards.every((card) => card.classList.contains("auction-only-card"))))) return;
    rendering = true;
    if (refreshButton) refreshButton.disabled = true;
    if (loading) loading.hidden = false;
    if (errorBox) errorBox.hidden = true;
    try {
      const response = await fetch(`/api/board-plan${selectedDate ? `?date=${encodeURIComponent(selectedDate)}` : ""}`);
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "打板历史回放失败");
      selectedDate = data.selected_date || selectedDate;

      const gateCard = document.getElementById("marketGate")?.closest("article");
      const gateTitle = gateCard?.querySelector("h2");
      const gateEyebrow = gateCard?.querySelector(".section-title span");
      if (gateTitle) gateTitle.textContent = "竞价质量总开关";
      if (gateEyebrow) gateEyebrow.textContent = "HISTORY + AUCTION GATE";
      document.getElementById("marketGate").textContent = `${data.market.score}分 · ${data.market.state}`;
      const screening = data.screening || {};
      document.getElementById("emotionMetrics").innerHTML = [
        ["全量主板", `${screening.scanned || 0}只`],
        ["多因子初筛", `${screening.prefiltered || 0}只`],
        ["深度评分", `${screening.deep_scanned || 0}只`],
        ["合格", `${screening.qualified_count || 0}只`],
        ["接力匹配", `${screening.relay_qualified_count || 0}只`],
        ["连板核心", `${screening.core_qualified_count || 0}只`],
        ["分歧转强", `${screening.reversal_qualified_count || 0}只`],
        ["连板优先", `${screening.continuation_primary_count || 0}只`],
        ["一进二观察", `${screening.one_to_two_count || 0}只`],
        ["首板观察", `${screening.first_board_watch_count || 0}只`],
        ["不可成交", `${screening.untradable_count || 0}只`],
        ["风险剔除", `${screening.risk_veto_count || 0}只`],
        ["主力确认", `${data.market.fund_confirmed_count || 0}只`],
      ].map(([key, value]) => `<article class="card"><span>${key}</span><strong>${value}</strong></article>`).join("") +
        `<small style="grid-column:1/-1;color:var(--muted)">数据源：${screening.source || data.market.source}${screening.snapshot_time ? `（快照 ${new Date(screening.snapshot_time).toLocaleString("zh-CN", { hour12: false })}）` : ""}。${screening.method || ""}${screening.replay_warning ? `<br><b style="color:var(--amber)">${screening.replay_warning}</b>` : ""}</small>`;

      document.getElementById("boardStage").textContent = data.stage;
      const plan = data.position_plan;
      document.getElementById("capitalPlan").innerHTML = [
        ["单只上限", yuan(plan.per_position)], ["最多新开", `${plan.max_positions}只`],
        ["新增暴露上限", yuan(plan.max_new_exposure)], ["保留现金", yuan(plan.cash_reserve)],
      ].map(([key, value]) => `<div class="action-box"><span>${key}</span><strong>${value}</strong></div>`).join("");
      document.getElementById("capitalRule").textContent = plan.rule;

      const candidateCard = candidateList.closest("article");
      const candidateTitle = candidateCard?.querySelector("h2");
      const candidateEyebrow = candidateCard?.querySelector(".section-title span");
      if (candidateTitle) candidateTitle.textContent = "T+1涨停预选";
      if (candidateEyebrow) candidateEyebrow.textContent = "NEXT-DAY LIMIT-UP CANDIDATES";
      const aGrade = data.candidates.filter((candidate) => candidate.actionable).length;
      document.getElementById("actionableCount").textContent = data.auction_phase === "indicative"
        ? `${data.candidates.length}只09:20观察 · 09:25复核`
        : `${screening.continuation_primary_count || 0}只连板优先 · ${screening.one_to_two_count || 0}只一进二 · ${screening.first_board_watch_count || 0}只首板`;
      candidateList.innerHTML = data.candidates.length ? data.candidates.map((candidate, index) => `
        <article class="action-box auction-only-card">
          <div style="display:flex;justify-content:space-between;gap:12px">
            <div><strong>${index + 1}. ${candidate.name} <small style="color:var(--muted)">${candidate.code} · ${[candidate.category, candidate.industry].filter(Boolean).join("/") || "未分类"} · ${candidate.strategy_mode || "观察"}</small> <small style="padding:3px 7px;border-radius:10px;background:${candidate.priority_tier === "连板优先" ? "#183126" : "#2b2b21"};color:${candidate.priority_tier === "连板优先" ? "var(--green)" : "var(--amber)"}">${candidate.priority_tier || "观察"}</small> <small style="padding:3px 7px;border-radius:10px;background:#1d2730;color:#8fc7ff">${candidate.board_stage_label || (candidate.consecutive_limit_up_days == null ? "板数未留存" : candidate.consecutive_limit_up_days > 0 ? `${candidate.consecutive_limit_up_days}进${candidate.consecutive_limit_up_days + 1} · 目标${candidate.consecutive_limit_up_days + 1}连板` : "首板候选")}</small></strong>
              <p style="color:var(--muted);font-size:12px;line-height:1.8">昨日收盘 ${fixed(candidate.previous_close)} · 竞价价 ${fixed(candidate.auction_price)} · 高开 ${signed(candidate.auction_gap_percent)}% · 竞价额 ${amount(candidate.auction_amount)}</p>
              <p style="color:#bfd0ca;font-size:12px;line-height:1.8">竞价量/近5日均量 ${fixed(candidate.auction_volume_percent)}% · MA5偏离 ${signed(candidate.price_vs_ma5_percent)}% · 近3/5/10日 ${signed(candidate.three_day_change_percent)}% / ${signed(candidate.five_day_change_percent)}% / ${signed(candidate.ten_day_change_percent)}%</p>
              <p style="color:#bfd0ca;font-size:12px;line-height:1.8">连续涨停 ${candidate.consecutive_limit_up_days ?? 0}天 · 近5/10日涨停 ${candidate.recent_5_limit_up_count ?? "--"}/${candidate.recent_10_limit_up_count ?? "--"}次 · 流通市值 ${candidate.float_market_cap ? amount(candidate.float_market_cap) : "--"} · 上市历史 ${candidate.listed_sessions ?? "--"}日</p>
              <p style="color:#bfd0ca;font-size:12px;line-height:1.8">前序主力 ${candidate.decision_main_ratio == null ? "暂不可用" : `${signed(candidate.decision_main_ratio)}% / ${amount(candidate.decision_main_net)}`} ${candidate.fund_data_date ? `（${candidate.fund_data_date}）` : ""} · 前日收盘位置 ${fixed(candidate.previous_close_position_percent, 1)}%</p>
              <p style="color:${candidate.big_order_support?.status === "weak" ? "var(--red)" : candidate.big_order_support?.status === "confirmed" ? "var(--green)" : "var(--muted)"};font-size:12px;line-height:1.8">大单确认：${candidate.big_order_support?.label || "待确认"} · 题材：${candidate.theme_context?.label || "待确认"}（${candidate.theme_context?.bucket || candidate.industry}）</p>
              <p style="color:${candidate.regulatory_risk?.level === "high" ? "var(--red)" : candidate.regulatory_risk?.level === "watch" ? "var(--amber)" : "var(--muted)"};font-size:12px;line-height:1.8">异动监管：${candidate.regulatory_risk?.label || "未计算"} · ${candidate.regulatory_risk?.summary || "--"}</p>
              <p style="color:${candidate.entry_confirmation?.status === "pending" ? "var(--amber)" : "var(--muted)"};font-size:12px;line-height:1.8">执行门槛：${candidate.entry_confirmation?.label || "待确认"} · ${candidate.entry_confirmation?.note || ""}</p>
              <p style="color:${candidate.risk_veto || candidate.tradable === false ? "var(--red)" : "var(--muted)"};font-size:12px;line-height:1.8">可成交性：${candidate.tradability_label || "等待开盘确认"} · T+1下跌风险 ${candidate.t1_downside_risk_score ?? "--"}分${candidate.auction_trajectory ? ` · ${candidate.auction_trajectory.label}（${candidate.auction_trajectory.sample_count}次，撮合涨幅漂移${signed(candidate.auction_trajectory.gap_drift_percent)}个百分点）` : " · 竞价轨迹待积累"}</p>
            </div>
            <div style="text-align:right"><b class="${candidate.actionable ? "positive" : candidate.regulatory_risk?.level === "high" ? "negative" : "neutral"}">${candidate.action || candidate.decision || "观察"}</b><small style="display:block;margin-top:5px;color:var(--green)">T+1预期 ${candidate.continuation_score ?? "--"}分</small><small style="display:block">竞价动态${candidate.selection_score ?? candidate.score ?? "--"}分 · 原始${candidate.score ?? "--"}分</small><small style="display:block">核心${candidate.core_chain_score || 0} · 接力${candidate.relay_score || 0} · 弱转强${candidate.reversal_score || 0} · 隔日${candidate.first_board_score || 0}</small><small style="display:block">${candidate.guard_passed ?? "--"}/${candidate.guard_total ?? "--"}项</small></div>
          </div>
          <div style="display:flex;gap:8px;flex-wrap:wrap">${(candidate.checks || []).map((check) => {
            const unknown = check.passed == null;
            const background = unknown ? "#242b29" : check.passed ? "#183126" : "#321d20";
            const color = unknown ? "var(--muted)" : check.passed ? "var(--green)" : "var(--red)";
            return `<small title="${check.note || ""}" style="padding:6px 9px;border-radius:14px;background:${background};color:${color}">${unknown ? "○" : check.passed ? "✓" : "✗"} ${check.name}${unknown ? "（数据缺失）" : ""}</small>`;
          }).join("")}</div>
        </article>`).join("") : `<div class="state auction-only-card">${data.auction_phase === "preauction" ? "09:20不可撤单观察窗口尚未开始。" : data.auction_phase === "indicative" ? "09:20暂时没有达到门槛的观察候选，09:25继续复核。" : "今日没有达到门槛的09:25最终竞价候选，保持空仓。"}</div>`;

      const workflow = results.querySelector(":scope > article:last-child");
      const workflowTitle = workflow?.querySelector("h2");
      if (workflowTitle) workflowTitle.textContent = "数据口径与决策步骤";
      const workflowGrid = workflow?.querySelector(".operation-grid");
      if (workflowGrid) workflowGrid.innerHTML = [
        ["连板核心", data.strategy_profile?.core_rule || "昨日连续涨停、竞价量价、流通市值与上市时长共同确认"],
        ["一进二/首板", data.strategy_profile?.first_board_rule || "量价亮眼时进入低优先级观察，并明确标注目标板数"],
        ["连板/加速", data.strategy_profile?.relay_rule || "强收盘、短上影、涨停活性与竞价放量联合确认"],
        ["分歧转强", data.strategy_profile?.reversal_rule || "前日高换手分歧，次日竞价超预期并获得量额确认"],
        ["动态加减分", "硬门槛通过后，大单/五档支撑与题材共振才参与排序；大市值、资金偏弱和异动风险降级"],
        ["风险降级", data.strategy_profile?.risk_rule || "高位爆量和涨停附近开盘只作观察"],
        ["09:20–09:25", "不可撤单阶段生成动态观察池；仍可新增委托，09:25必须最终复核"],
        ["盘中执行", "连板优先必须等实际封板、封单和回封确认；一进二与首板只允许较低优先级观察"],
      ].map(([key, value]) => `<div class="action-box"><span>${key}</span><strong>${value}</strong></div>`).join("");
      document.getElementById("boardDisclaimer").textContent = data.disclaimer;
      results.hidden = false;
      const generatedTime = data.generated_at ? new Date(data.generated_at).toLocaleString("zh-CN", { hour12: false }) : "时间未记录";
      document.getElementById("boardPlanTime").textContent = `${data.snapshot_label || (data.historical ? "历史回放" : "当日模式")} · ${data.selected_date || ""} · ${generatedTime}`;
      paintDateButtons();
      if (selectedDate === localDate() && secondsOfDay() >= 9 * 3600 + 30 * 60) loadOpenGuard();
      else if (openGuardBox) openGuardBox.hidden = true;
      if (selectedDate === localDate() && secondsOfDay() >= 14 * 3600 + 50 * 60) loadNextDayStrategy();
      else if (nextDayBox) nextDayBox.hidden = true;
    } catch (error) {
      if (errorBox) {
        errorBox.textContent = error.message;
        errorBox.hidden = false;
      }
    } finally {
      if (loading) loading.hidden = true;
      if (refreshButton) refreshButton.disabled = false;
      rendering = false;
    }
  };

  const paintDateButtons = () => {
    if (!dateButtons) return;
    dateButtons.querySelectorAll("button[data-date]").forEach((button) => {
      const active = button.dataset.date === selectedDate;
      button.style.background = active ? "#183126" : "transparent";
      button.style.borderColor = active ? "var(--green)" : "var(--line)";
      button.style.color = active ? "var(--green)" : "var(--muted)";
    });
  };

  const requestDate = (tradeDate) => {
    selectedDate = tradeDate;
    if (openGuardBox) openGuardBox.hidden = tradeDate !== localDate();
    if (nextDayBox) nextDayBox.hidden = tradeDate !== localDate();
    candidateList.innerHTML = `<div class="state">正在回放 ${tradeDate}，首次计算可能需要1–3分钟…</div>`;
    paintDateButtons();
    render(true);
  };

  const loadTodayAuction = (label = "竞价数据") => {
    selectedDate = "";
    candidateList.innerHTML = `<div class="state">正在读取今日竞价数据…</div>`;
    if (autoStatus) autoStatus.textContent = `${label}正在刷新`;
    render(true).finally(() => {
      const phase = secondsOfDay() < 9 * 3600 + 25 * 60 ? "09:20观察数据已刷新 · 09:25将最终复核" : "09:25最终竞价已刷新 · 可手动再次刷新";
      if (autoStatus) autoStatus.textContent = phase;
      captureAuctionTrajectory();
    });
  };

  let auctionRescanTimer = null;
  const startAuctionRescan = () => {
    if (auctionRescanTimer) return;
    loadTodayAuction("09:20–09:25每20秒全主板重扫");
    auctionRescanTimer = window.setInterval(() => {
      const seconds = secondsOfDay();
      if (seconds >= 9 * 3600 + 25 * 60) {
        window.clearInterval(auctionRescanTimer);
        auctionRescanTimer = null;
        return;
      }
      if (seconds >= 9 * 3600 + 20 * 60 && !document.getElementById("boardTab")?.hidden) {
        loadTodayAuction("09:20–09:25每20秒全主板重扫");
      }
    }, 20000);
  };

  const setupAutoRefresh = () => {
    const now = new Date();
    const weekday = now.getDay();
    if (weekday === 0 || weekday === 6) {
      if (autoStatus) autoStatus.textContent = "非交易日 · 不执行自动刷新";
      render();
      return;
    }
    const current = secondsOfDay(now);
    const premarketAt = 9 * 3600;
    const observeAt = 9 * 3600 + 20 * 60 + 5;
    const finalAt = 9 * 3600 + 25 * 60 + 10;
    const retryAt = 9 * 3600 + 26 * 60;
    const openGuardAt = 9 * 3600 + 30 * 60 + 5;
    const nextDayPreviewAt = 14 * 3600 + 50 * 60 + 5;
    const nextDayFinalAt = 15 * 3600 + 5 * 60 + 10;
    if (current < premarketAt) {
      if (autoStatus) autoStatus.textContent = "下次自动刷新 09:00 盘前预选";
      scheduleAt(9, 0, 0, () => {
        if (autoStatus) autoStatus.textContent = "09:00盘前预选正在刷新";
        loadPreselect();
      });
    } else if (current < observeAt) {
      if (autoStatus) autoStatus.textContent = "盘前阶段 · 09:20:05刷新不可撤单观察池";
      loadPreselect();
    }
    if (current < observeAt) {
      scheduleAt(9, 20, 5, startAuctionRescan);
    } else if (current < finalAt) {
      startAuctionRescan();
    }
    if (current < finalAt) {
      scheduleAt(9, 25, 10, () => loadTodayAuction("09:25:10最终竞价"));
    } else if (current >= finalAt) {
      loadTodayAuction("打开页面后刷新最终竞价");
    }
    if (current < retryAt) {
      scheduleAt(9, 26, 0, () => loadTodayAuction("09:26最终竞价重试"));
    }
    if (current < openGuardAt) scheduleAt(9, 30, 5, loadOpenGuard);
    else loadOpenGuard();
    if (current < 10 * 3600) {
      window.setInterval(() => {
        const seconds = secondsOfDay();
        if (seconds >= 9 * 3600 + 30 * 60 && seconds <= 10 * 3600 && !document.getElementById("boardTab")?.hidden) loadOpenGuard();
      }, 30000);
    }
    if (current < nextDayPreviewAt) scheduleAt(14, 50, 5, loadNextDayStrategy);
    else loadNextDayStrategy();
    if (current < nextDayFinalAt) scheduleAt(15, 5, 10, loadNextDayStrategy);
  };

  const loadDateButtons = async () => {
    if (!dateButtons) return;
    try {
      const response = await fetch("/api/trading-dates");
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "交易日读取失败");
      selectedDate = data.latest || data.dates[data.dates.length - 1];
      dateButtons.innerHTML = [...data.dates].reverse().map((tradeDate, index) =>
        `<button type="button" data-date="${tradeDate}" style="padding:9px 12px;border:1px solid ${index ? "var(--line)" : "var(--green)"};background:${index ? "transparent" : "#183126"};color:${index ? "var(--muted)" : "var(--green)"};border-radius:8px;cursor:pointer;font-weight:700">${tradeDate.slice(5)}</button>`
      ).join("");
      dateButtons.querySelectorAll("button[data-date]").forEach((button) =>
        button.addEventListener("click", () => requestDate(button.dataset.date))
      );
    } catch (error) {
      dateButtons.innerHTML = `<small class="negative">${error.message}</small>`;
    }
  };

  refreshButton?.addEventListener("click", (event) => {
    event.stopImmediatePropagation();
    if (selectedDate === localDate() || (!selectedDate && secondsOfDay() >= 9 * 3600 + 20 * 60)) loadTodayAuction("手动刷新竞价数据");
    else requestDate(selectedDate);
  }, true);
  openGuardButton?.addEventListener("click", loadOpenGuard);
  nextDayButton?.addEventListener("click", loadNextDayStrategy);

  document.querySelector('.top-tab[data-tab="boardTab"]')?.addEventListener("click", (event) => {
    event.stopImmediatePropagation();
    document.querySelectorAll(".tab-page").forEach((page) => { page.hidden = page.id !== "boardTab"; });
    document.querySelectorAll(".top-tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.tab === "boardTab"));
  }, true);

  new MutationObserver(() => render(false)).observe(candidateList, { childList: true });
  loadDateButtons().finally(setupAutoRefresh);
})();
