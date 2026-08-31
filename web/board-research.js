(() => {
  "use strict";
  const byId = (id) => document.getElementById(id);
  window.BoardResearchStatus = (research) => {
    const status = byId("researchRecordingStatus");
    if (!status) return;
    status.textContent = !research ? "研究留痕未就绪，请重启后端加载新功能。" : !research.available ? research.message : !research.recording ? research.message : "研究留痕：累计 " + research.sample_count + " 条 · 静默实验 " + research.shadow_stock_count + " 只（非推荐）· 最近采样 " + (research.last_collected_at || "暂无");
    status.style.color = research?.available === false ? "var(--red)" : "var(--muted)";
  };
  if (!byId("researchForm")) return;
  const escape = (value) => String(value ?? "—").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const time = (value) => value ? String(value).replace("T", " ").replace(/\+08:00$/, "") : "未提供";
  const number = (value, digits = 2) => typeof value === "number" && Number.isFinite(value) ? value.toFixed(digits) : "—";
  const storageKey = "board-research-details-v1";
  let generation = 0;
  let activeDate = "";
  let opened = {};
  try { opened = JSON.parse(localStorage.getItem(storageKey) || "{}"); if (!opened || Array.isArray(opened) || typeof opened !== "object") opened = {}; } catch { opened = {}; }
  const today = new Intl.DateTimeFormat("en-CA", {timeZone:"Asia/Shanghai", year:"numeric", month:"2-digit", day:"2-digit"}).format(new Date());
  byId("researchDate").value = today;
  const request = async (params) => {
    const response = await fetch("/api/board-research?" + new URLSearchParams(params), {cache:"no-store"});
    let data;
    try { data = await response.json(); } catch { throw new Error("研究接口未就绪，请重启后端服务加载新功能。"); }
    if (!response.ok || !data.available) throw new Error(data.error || "研究记录暂不可用，请确认后端已更新。");
    return data;
  };
  const sampleRows = (samples) => samples.map((row) => {
    const funds = row.funds || {};
    const label = row.status === "fresh" ? "新行情" : row.status === "duplicate" ? "重复快照" : "无效行情";
    return '<tr><td>' + escape(time(row.collected_at)) + '</td><td>' + escape(time(row.quote_time)) + '</td><td>' + number(row.price) + '<br><small>封单 ' + (row.seal_amount == null ? "未知" : number(row.seal_amount / 10000) + "万") + '</small></td><td>' + label + '<br>' + (row.shadow_match ? "静默条件通过" : "未通过静默条件") + '<br><small>' + (row.baseline?.recommended ? "正式策略当时已提示" : "正式策略当时未提示") + '</small></td><td>' + escape(row.data_reason || row.shadow_reason) + '<br><small>资金日期：' + escape(funds.date) + '<br>源时间：' + escape(time(funds.source_time)) + '<br>获取时间：' + escape(time(funds.retrieved_at)) + '<br>正式策略：' + escape(row.baseline?.selection_reason) + '</small></td></tr>';
  }).join("");
  const loadStock = async (details, append = false) => {
    if (details.dataset.loading === "1") return;
    const token = generation;
    const code = details.dataset.code;
    const box = details.querySelector(".stock-samples");
    const date = activeDate;
    const params = {date, code, limit:"50"};
    if (append && details.dataset.before) params.before = details.dataset.before;
    details.dataset.loading = "1";
    if (!append) box.textContent = "正在读取真实采样…";
    try {
      const data = await request(params);
      if (token !== generation || !details.isConnected) return;
      if (append) box.querySelector("tbody").insertAdjacentHTML("beforeend", sampleRows(data.samples));
      else box.innerHTML = data.samples.length ? '<div class="table-scroll" tabindex="0" aria-label="采样明细，可横向滚动"><table><thead><tr><th>获取时间</th><th>行情源时间</th><th>价格／封单</th><th>状态</th><th>依据及数据时间</th></tr></thead><tbody>' + sampleRows(data.samples) + '</tbody></table></div>' : '<p class="muted">没有采样明细。</p>';
      box.querySelector("button")?.remove();
      if (data.next_before) {
        details.dataset.before = String(data.next_before);
        const more = document.createElement("button");
        more.type = "button"; more.textContent = "加载更早记录";
        more.addEventListener("click", () => loadStock(details, true));
        box.append(more);
      } else delete details.dataset.before;
      details.dataset.loaded = "1";
    } catch (error) {
      if (token === generation && details.isConnected) { box.textContent = error.message; details.dataset.loaded = ""; }
    } finally { details.dataset.loading = ""; }
  };
  const render = (data) => {
    const summary = data.summary;
    byId("researchSummary").innerHTML = [["采样记录", summary.sample_count], ["已采样股票", summary.stock_count], ["原70–74分静默实验", summary.shadow_stock_count], ["留痕中的正式提示股票", summary.formal_stock_count], ["早封连板55–74分实验", summary.early_chain_stock_count || 0]].map(([label, value]) => '<div class="metric"><b>' + escape(value) + '</b><span>' + label + '</span></div>').join("");
    byId("researchRule").textContent = data.rule;
    byId("researchNote").textContent = data.note;
    byId("researchStocks").innerHTML = data.stocks.length ? data.stocks.map((stock) => '<details class="stock" data-code="' + escape(stock.code) + '"><summary>' + escape(stock.name) + ' · ' + escape(stock.code) + '<small>' + (stock.first_shadow_at ? "曾通过静默实验 · 非买点" : "已留痕 · 未命中实验") + '</small></summary><p>首次观察：' + escape(time(stock.first_seen_at)) + '<br>首次静默触发：' + escape(time(stock.first_shadow_at)) + '；假设参考价：' + number(stock.first_shadow_price) + '（不代表成交）</p><p>观测炸板至少 ' + escape(stock.observed_breaks || 0) + ' 次 · 观测回封 ' + escape(stock.observed_reseals || 0) + ' 次 · 实验有效采样 ' + escape(stock.shadow_count || 0) + ' 次<br>最新记录：' + escape(time(stock.last_seen_at)) + ' · ' + escape(stock.latest_reason) + '</p><div class="stock-samples"></div></details>').join("") : '<p class="empty">所选日期或股票尚无采样记录。新功能只记录运行后实际取得的行情，不生成历史假数据。</p>';
    byId("researchStocks").querySelectorAll("details").forEach((details) => {
      const stock = data.stocks.find((item) => item.code === details.dataset.code);
      const chain = stock.early_chain || {};
      const info = document.createElement("p");
      info.textContent = "早封连板55–74分独立实验：首次触发 " + time(chain.first_at) + "；假设参考价 " + number(chain.first_price) + "；当前有效采样 " + (chain.count || 0) + " 次。非正式推荐，不与原实验凑次数。";
      details.querySelector(".stock-samples").before(info);
      if (chain.first_at) details.querySelector("summary small").textContent = "曾通过结构静默实验 · 非买点";
      const key = activeDate + ":" + details.dataset.code;
      details.addEventListener("toggle", () => {
        opened[key] = details.open;
        try { localStorage.setItem(storageKey, JSON.stringify(opened)); } catch { /* 显示偏好存储失败不影响查询。 */ }
        if (details.open && !details.dataset.loaded) loadStock(details);
      });
      if (opened[key]) details.open = true;
    });
    byId("researchStatus").textContent = "只读查询完成 · 最近采样：" + time(summary.last_collected_at) + " · 此处不是当前推荐名单";
  };
  const load = async (event) => {
    event?.preventDefault();
    const token = ++generation;
    const date = byId("researchDate").value;
    const code = byId("researchCode").value.trim();
    activeDate = date;
    byId("researchStocks").replaceChildren();
    byId("researchSummary").replaceChildren();
    byId("researchStatus").className = "";
    byId("researchStatus").textContent = "正在查询留痕…";
    try {
      const data = await request({date, ...(code ? {code} : {}), limit:"1"});
      if (token === generation) render(data);
    } catch (error) {
      if (token === generation) { byId("researchStatus").textContent = error.message; byId("researchStatus").className = "error"; }
    }
  };
  byId("researchForm").addEventListener("submit", load);
  load();
})();
