(() => {
  let autoReviewRunning = false;
  let historyDays = [];
  let selectedReviewDate = "";
  const byId = (id) => document.getElementById(id);
  const known = (value) => value !== null && value !== undefined && value !== "" && Number.isFinite(Number(value));
  const number = (value, digits = 2) => known(value) ? Number(value).toFixed(digits) : "--";
  const signed = (value, digits = 2) => known(value) ? `${Number(value) >= 0 ? "+" : ""}${Number(value).toFixed(digits)}%` : "--";
  const escape = (value) => String(value ?? "--").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const label = () => "打板候选";
  const tone = (value) => ["次日涨停", "次日强势", "次日正溢价"].includes(value) ? "positive" : ["次日负溢价", "高开回落"].includes(value) ? "negative" : "neutral";

  function renderAudit(audit) {
    const box = byId("historyReviewRuleAudit");
    if (!box) return;
    if (!audit) { box.textContent = "五日规则复核尚未就绪，请重启后端加载新版。"; return; }
    const summary = audit.summary;
    const ratio = (count, total) => total ? `${count}/${total}` : "--";
    const table = (groups) => `<table class="review-table"><thead><tr><th>分组</th><th>候选</th><th>T日封板</th><th>次日正收盘溢价</th><th>T封板后次日≥5%</th><th>平均次日开盘溢价</th><th>平均次日收盘溢价</th><th>次日涨停</th></tr></thead><tbody>${groups.map(group => `<tr><td>${escape(group.name)}</td><td>${group.candidate_count}</td><td>${ratio(group.t0_sealed_count, group.t0_count)}</td><td>${ratio(group.t1_close_positive_count, group.t1_close_count)}</td><td>${ratio(group.sealed_t1_strong_count, group.sealed_t1_count)}</td><td>${signed(group.t1_open_mean)}</td><td>${signed(group.t1_close_mean)}</td><td>${ratio(group.t1_limit_up_count, group.t1_limit_count)}</td></tr>`).join("")}</tbody></table>`;
    box.innerHTML = `<details class="card review-audit"><summary>最近${audit.day_count}个已留存交易日规则复核 · ${summary.candidate_count}条候选（${summary.replay_count}条回放）</summary><p>${escape(audit.dates.join("、"))}<br>T日封板 ${ratio(summary.t0_sealed_count, summary.t0_count)} · 次日正收盘溢价 ${ratio(summary.t1_close_positive_count, summary.t1_close_count)} · 次日涨停 ${ratio(summary.t1_limit_up_count, summary.t1_limit_count)}（非胜率）</p><p>${escape(audit.decision)}</p><h4>结构分层（描述性对照，不是回测）</h4>${table(audit.structure_groups)}<h4>评分分层</h4>${table(audit.score_groups)}<p>${escape(audit.limitation)}</p><p>数据覆盖：${audit.coverage.map(day => `${escape(day.date)} ${day.candidate_count}条／${day.replay ? "回放" : "存档"}／${day.review_available ? "有收盘数据" : "复盘缺失或来源不匹配"}`).join("；")}</p></details>`;
    const objective = document.createElement("p");
    objective.textContent = audit.objective + " 当前T日封板且有T+1收盘数据：" + ratio(summary.sealed_t1_strong_count,summary.sealed_t1_count) + "达到次日≥5%观察线。";
    box.querySelector("summary").after(objective);
  }

  function candidateRows(snapshot, review) {
    const candidates = (review?.candidates || snapshot.candidates || []);
    return candidates.map((item, index) => `
      <tr>
        <td>${index + 1}</td>
        <td><strong>${escape(item.name)}</strong><br><small>${escape(item.code)}</small></td>
        <td>${number(item.score, 0)}分<br><small>${escape(item.decision || item.signal || "观察")}</small></td>
        <td>¥${number(item.reference_price)}<br><small>${snapshot.source === "board" ? `竞价 ${signed(item.auction_gap_percent)}` : `入选时 ${signed(item.change_percent_at_selection)}`}</small></td>
        <td>${item.outcome ? `¥${number(item.outcome.close)}<br><small>日涨跌 ${signed(item.outcome.daily_change_percent)}${snapshot.source === "board" ? ` · ${item.outcome.same_day_sealed === true ? "封板" : item.outcome.same_day_sealed === false ? "未封板" : "封板状态未知"}` : ""}</small>` : `<span class="neutral">${escape(item.error || "待收盘复盘")}</span>`}</td>
        <td class="${Number(item.return_percent) >= 0 ? "positive" : "negative"}">${snapshot.source === "board" && item.outcome?.next_day ? `开 ${signed(item.outcome.next_day.open_return_percent)}<br><small>高 ${signed(item.outcome.next_day.high_return_percent)} · 收 ${signed(item.outcome.next_day.close_return_percent)} · ${item.outcome.next_day.limit_up ? "T+1涨停" : "未涨停"}</small>` : item.outcome ? signed(item.return_percent) : "--"}</td>
        <td><span class="review-tag ${tone(item.attribution)}">${escape(item.attribution || (item.counted === false ? "未计入" : "待复盘"))}</span><br><small>${escape(item.cause || "")}</small></td>
      </tr>`).join("");
  }

  function sourceSection(name, snapshot, reviewed) {
    const reviewSource = reviewed?.sources?.[name];
    return `<section class="review-source"><h4>${label(name)} · ${snapshot.candidates.length}只 · ${snapshot.historical_proxy || String(snapshot.snapshot_kind).includes("replay") ? "盘后回放，非实盘买点" : "候选存档，非成交记录"}</h4><p class="disclaimer">采集时间 ${escape(snapshot.captured_at)} · 规则 ${escape(snapshot.rule_version)}</p>
      <table class="review-table"><thead><tr><th>#</th><th>股票</th><th>入选结论</th><th>记录价</th><th>T日收盘</th><th>${name === "board" ? "T+1表现（对比T收盘）" : "记录后表现"}</th><th>结果描述</th></tr></thead>
      <tbody>${candidateRows(snapshot, reviewSource)}</tbody></table></section>`;
  }

  function dayCard(day) {
    const review = day.review;
    const accuracy = review?.accuracy_percent == null ? "待复盘" : `${number(review.accuracy_percent, 1)}%`;
    const sources = Object.entries(day.sources || {}).map(([name, snapshot]) => sourceSection(name, snapshot, review)).join("");
    const suggestions = review?.rule_adjustment?.suggestions || [];
    return `<article class="card review-day">
      <div class="review-day-header"><div><span class="eyebrow">DAILY AUDIT</span><h3>${day.date}</h3></div>
        <div class="review-date-actions"><b class="${tone(review?.diagnosis)}">${escape(review?.metric_label || "尚未复盘")} · ${accuracy}</b><button data-review-date="${day.date}">收盘复盘</button></div></div>
      ${sources || '<div class="review-empty">当日没有候选。</div>'}
      ${review ? `<div class="review-rule-box"><strong>规则结论：${review.rule_adjustment.status}</strong><br>${review.rule_adjustment.principle}${suggestions.length ? `<br>调整方向：${suggestions.join("；")}` : ""}</div>` : ""}
    </article>`;
  }

  function renderSelectedDay() {
    const tabs = byId("historyReviewDateTabs"), container = byId("historyReviewDays");
    if (!historyDays.length) {
      tabs.innerHTML = "";
      container.innerHTML = '<div class="card review-empty">暂无打板记录。刷新“打板决策”后，系统会冻结当天首次有效候选。</div>';
      return;
    }
    if (!historyDays.some((day) => day.date === selectedReviewDate)) selectedReviewDate = historyDays[0].date;
    tabs.innerHTML = historyDays.map((day) => `<button type="button" class="review-date-tab ${day.date === selectedReviewDate ? "active" : ""}" data-history-date="${day.date}">${day.date.slice(5)}${day.review ? " · 已复盘" : " · 待复盘"}</button>`).join("");
    const selected = historyDays.find((day) => day.date === selectedReviewDate);
    container.innerHTML = selected ? dayCard(selected) : "";
  }

  async function loadHistory() {
    const loading = byId("historyReviewLoading"), error = byId("historyReviewError"), content = byId("historyReviewContent");
    loading.hidden = false; error.hidden = true; content.hidden = true;
    try {
      const response = await fetch("/api/history");
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "历史记录读取失败");
      const visibleDays = data.days.slice(0, 5);
      const reviewed = visibleDays.filter((day) => day.review);
      const counted = reviewed.reduce((sum, day) => sum + (day.review.counted || 0), 0);
      const successes = reviewed.reduce((sum, day) => sum + (day.review.successes || 0), 0);
      byId("historyReviewSummary").innerHTML = [
        ["记录交易日", visibleDays.length], ["已复盘交易日", reviewed.length],
        ["五日T+1涨停可评样本", counted], ["五日T+1涨停占比（非胜率）", counted ? `${number(successes / counted * 100, 1)}%` : "--"]
      ].map(([name, value]) => `<article class="card"><span>${name}</span><strong>${value}</strong><small>规则版本 ${data.rule_version}</small></article>`).join("");
      historyDays = visibleDays;
      renderAudit(data.rule_audit);
      renderSelectedDay();
      content.hidden = false;
      const now = new Date();
      const today = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
      const pendingToday = visibleDays.find((day) => day.date === today && !day.review);
      if (pendingToday && (now.getHours() > 15 || (now.getHours() === 15 && now.getMinutes() >= 5)) && !autoReviewRunning) {
        autoReviewRunning = true;
        await review(today);
      }
    } catch (err) {
      error.textContent = err.message; error.hidden = false;
    } finally { loading.hidden = true; }
  }

  async function review(date) {
    const button = document.querySelector(`[data-review-date="${date}"]`);
    if (button) { button.disabled = true; button.textContent = "复盘中…"; }
    try {
      const response = await fetch("/api/history/review", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({date})});
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "收盘复盘失败");
      await loadHistory();
    } catch (err) {
      byId("historyReviewError").textContent = err.message;
      byId("historyReviewError").hidden = false;
      if (button) { button.disabled = false; button.textContent = "收盘复盘"; }
    }
  }

  byId("historyReviewRefresh")?.addEventListener("click", loadHistory);
  byId("historyReviewDays")?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-review-date]");
    if (button) review(button.dataset.reviewDate);
  });
  byId("historyReviewDateTabs")?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-history-date]");
    if (!button) return;
    selectedReviewDate = button.dataset.historyDate;
    renderSelectedDay();
  });
  document.querySelector('[data-tab="historyReviewTab"]')?.addEventListener("click", loadHistory);
})();
