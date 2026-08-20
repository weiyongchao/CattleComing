(() => {
  let autoReviewRunning = false;
  let historyDays = [];
  let selectedReviewDate = "";
  const byId = (id) => document.getElementById(id);
  const number = (value, digits = 2) => Number.isFinite(Number(value)) ? Number(value).toFixed(digits) : "--";
  const signed = (value, digits = 2) => Number.isFinite(Number(value)) ? `${Number(value) >= 0 ? "+" : ""}${Number(value).toFixed(digits)}%` : "--";
  const label = (source) => source === "board" ? "打板候选" : "主板候选";
  const tone = (value) => value === "规则有效" ? "positive" : value === "规则问题" ? "negative" : "neutral";

  function candidateRows(snapshot, review) {
    const candidates = (review?.candidates || snapshot.candidates || []);
    return candidates.map((item, index) => `
      <tr>
        <td>${index + 1}</td>
        <td><strong>${item.name || "--"}</strong><br><small>${item.code}</small></td>
        <td>${number(item.score, 0)}分<br><small>${item.decision || item.signal || "观察"}</small></td>
        <td>¥${number(item.reference_price)}<br><small>${snapshot.source === "board" ? `竞价 ${signed(item.auction_gap_percent)}` : `入选时 ${signed(item.change_percent_at_selection)}`}</small></td>
        <td>${item.outcome ? `¥${number(item.outcome.close)}<br><small>日涨跌 ${signed(item.outcome.daily_change_percent)}${snapshot.source === "board" ? ` · ${item.outcome.same_day_sealed ? "封板" : "未封板"}` : ""}</small>` : `<span class="neutral">${item.error || "待收盘复盘"}</span>`}</td>
        <td class="${Number(item.return_percent) >= 0 ? "positive" : "negative"}">${snapshot.source === "board" && item.outcome?.next_day ? `开 ${signed(item.outcome.next_day.open_return_percent)}<br><small>高 ${signed(item.outcome.next_day.high_return_percent)} · 收 ${signed(item.outcome.next_day.close_return_percent)} · ${item.outcome.next_day.limit_up ? "T+1涨停" : "未涨停"}</small>` : item.outcome ? signed(item.return_percent) : "--"}</td>
        <td><span class="review-tag ${tone(item.attribution)}">${item.attribution || (item.counted === false ? "未计入" : "待复盘")}</span><br><small>${item.cause || ""}</small></td>
      </tr>`).join("");
  }

  function sourceSection(name, snapshot, reviewed) {
    const reviewSource = reviewed?.sources?.[name];
    return `<section class="review-source"><h4>${label(name)} · ${snapshot.candidates.length}只</h4>
      <table class="review-table"><thead><tr><th>#</th><th>股票</th><th>入选结论</th><th>记录价</th><th>T日收盘</th><th>${name === "board" ? "T+1表现" : "记录后表现"}</th><th>归因</th></tr></thead>
      <tbody>${candidateRows(snapshot, reviewSource)}</tbody></table></section>`;
  }

  function dayCard(day) {
    const review = day.review;
    const accuracy = review?.accuracy_percent == null ? "待复盘" : `${number(review.accuracy_percent, 1)}%`;
    const sources = Object.entries(day.sources || {}).map(([name, snapshot]) => sourceSection(name, snapshot, review)).join("");
    const suggestions = review?.rule_adjustment?.suggestions || [];
    return `<article class="card review-day">
      <div class="review-day-header"><div><span class="eyebrow">DAILY AUDIT</span><h3>${day.date}</h3></div>
        <div class="review-date-actions"><b class="${tone(review?.diagnosis)}">${review?.diagnosis || "尚未复盘"} · ${accuracy}</b><button data-review-date="${day.date}">收盘复盘</button></div></div>
      ${sources || '<div class="review-empty">当日没有候选。</div>'}
      ${review ? `<div class="review-rule-box"><strong>规则结论：${review.rule_adjustment.status}</strong><br>${review.rule_adjustment.principle}${suggestions.length ? `<br>调整方向：${suggestions.join("；")}` : ""}</div>` : ""}
    </article>`;
  }

  function renderSelectedDay() {
    const tabs = byId("historyReviewDateTabs"), container = byId("historyReviewDays");
    if (!historyDays.length) {
      tabs.innerHTML = "";
      container.innerHTML = '<div class="card review-empty">暂无记录。先刷新“打板决策”或“主板筛选”，系统会冻结当天首次有效候选。</div>';
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
      const reviewed = data.days.filter((day) => day.review);
      const counted = reviewed.reduce((sum, day) => sum + (day.review.counted || 0), 0);
      const successes = reviewed.reduce((sum, day) => sum + (day.review.successes || 0), 0);
      byId("historyReviewSummary").innerHTML = [
        ["记录交易日", data.days.length], ["已复盘交易日", reviewed.length],
        ["累计有效样本", counted], ["累计准确率", counted ? `${number(successes / counted * 100, 1)}%` : "--"]
      ].map(([name, value]) => `<article class="card"><span>${name}</span><strong>${value}</strong><small>规则版本 ${data.rule_version}</small></article>`).join("");
      historyDays = data.days;
      renderSelectedDay();
      content.hidden = false;
      const now = new Date();
      const today = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
      const pendingToday = data.days.find((day) => day.date === today && !day.review);
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
