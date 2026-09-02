(() => {
  const DEFAULT_SETTINGS = Object.freeze({
    enabled: true,
    autoOpen: true,
    sound: true,
    desktop: false,
    rules: Object.freeze({
      auction: false,
      highTurnover: false,
      discovered: false,
      confirm: true,
      sealed: true,
      failed: true,
      risk: true,
    }),
  });

  const normalizeSettings = (value = {}) => ({
    enabled: value.enabled !== false,
    autoOpen: value.autoOpen !== false,
    sound: value.sound !== false,
    desktop: value.desktop === true,
    rules: { ...DEFAULT_SETTINGS.rules, ...(value.rules || {}), auction: false, highTurnover: false, discovered: false },
  });

  const eventKey = (tradeDate, code, rule) => `${tradeDate}|${code}|${rule}`;
  const DAILY_LIMIT = 5;
  const tradingDate = (value) => {
    if (value == null || value === "") return null;
    const parsed = new Date(value);
    if (!Number.isFinite(parsed.getTime())) return null;
    const parts = new Intl.DateTimeFormat("en", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(parsed);
    const field = (type) => parts.find((part) => part.type === type).value;
    return `${field("year")}-${field("month")}-${field("day")}`;
  };
  // 只有服务端当日正式提示记录可以授权预警；浏览器旧记录仅用于去重和保留名额。
  const todayBoardCodes = (snapshot, today) => {
    const focus = snapshot?.daily_focus;
    if (snapshot?.selected_date !== today || snapshot?.historical || focus?.available !== true
        || !Array.isArray(focus.issued) || focus.issued.length > DAILY_LIMIT) return new Set();
    return new Set(focus.issued.filter((item) => /^\d{6}$/.test(String(item?.code)) && tradingDate(item?.first_at) === today).map((item) => String(item.code)));
  };
  const todayPriorityWatchCodes = (snapshot, today) => {
    const rows = snapshot?.priority_watch_candidates;
    if (snapshot?.selected_date !== today || snapshot?.historical || !Array.isArray(rows) || rows.length > DAILY_LIMIT) return new Set();
    return new Set(rows.filter((item) => item?.priority_watch === true && /^\d{6}$/.test(String(item.code))).map((item) => String(item.code)));
  };
  const todayAlertEvents = (events, today, allowedCodes, priorityCodes = new Set()) => events.filter((item) => {
    if (!item || (item.trade_date != null && item.trade_date !== today)
        || typeof item.id !== "string" || !item.id.startsWith(`${today}|${item.code}|`)) return false;
    if (item.scope === "priority_watch") {
      return priorityCodes.has(String(item.code)) && ["watch", "risk"].includes(item.tone);
    }
    return allowedCodes.has(String(item.code)) && ["confirm", "risk"].includes(item.tone);
  });
  const notifiedCodes = (saved, today) => saved.date !== today ? [] : [...new Set([
    ...(Array.isArray(saved.notified_codes) ? saved.notified_codes : []),
    ...(Array.isArray(saved.events) ? saved.events : []).filter((item) => item?.tone === "confirm").map((item) => item.code),
    ...Object.keys(saved.triggered || {}).filter((key) => key.startsWith(`${today}|`) && /\|(sealed|open-confirm)$/.test(key)).map((key) => key.split("|")[1]),
  ].map(String).filter((code) => /^\d{6}$/.test(code)))];
  // 按代码而非规则累计，开盘确认后再封板不能重复消耗或提醒。
  const canPrompt = (codes, code, locked = false) => !locked && /^\d{6}$/.test(String(code))
    && !codes.includes(String(code)) && codes.length < DAILY_LIMIT;

  // 保留分类接口兼容性；竞价冻结结果不再产生任何条件预警。
  const classifyAuctionCandidate = () => null;

  const classifyLiveCandidate = (candidate, rawSettings = DEFAULT_SETTINGS) => {
    const settings = normalizeSettings(rawSettings);
    const eventRisk = candidate?.corporate_event_risk?.level === "high";
    if (eventRisk) {
      return settings.rules.risk ? {
        rule: "event-risk", label: "并购重组风险剔除", tone: "risk",
        detail: candidate.corporate_event_risk?.label || candidate.decision || "重大事项高风险",
      } : null;
    }
    if (!candidate) return null;
    if (candidate.failed_board || candidate.near_limit_failure) {
      return settings.rules.failed ? {
        rule: candidate.failed_board ? "failed-board" : "near-limit-failure",
        label: candidate.failed_board ? "炸板风险" : "冲板未封",
        tone: "risk", detail: candidate.decision || candidate.summary || "放弃追入",
      } : null;
    }
    if (candidate.risk_veto || candidate.regulatory_risk?.level === "high") {
      return settings.rules.risk ? { rule: "selection-risk", label: "首选风险失效", tone: "risk",
        detail: candidate.selection_reason || candidate.decision || "风险门槛不再通过" } : null;
    }
    if (candidate.tone === "reject" || candidate.corporate_event_risk?.available === false) return null;
    if (candidate.primary_pick === true && !candidate.focus_locked && candidate.recommended === true && candidate.recommendation_kind === "sealed" && candidate.sealed) {
      return settings.rules.sealed ? {
        rule: "sealed", label: "唯一首选·封板确认", tone: "confirm",
        detail: candidate.decision || candidate.summary || "观察封单稳定性",
      } : null;
    }
    if (candidate.primary_pick === true && !candidate.focus_locked && candidate.recommended === true && candidate.recommendation_kind === "strong_open") {
      return settings.rules.confirm ? {
        rule: "open-confirm", label: "唯一首选·极强开盘", tone: "confirm",
        detail: candidate.decision || candidate.selection_reason || "开盘例外通过两次采样",
      } : null;
    }
    const discovered = candidate.discovery_source && candidate.discovery_source !== "09:25冻结候选";
    if (discovered) {
      return settings.rules.discovered ? {
        rule: "live-discovered", label: candidate.discovery_source, tone: "watch",
        detail: candidate.decision || candidate.summary || "盘中新增观察，不是买点",
      } : null;
    }
    return null;
  };

  const classifyPriorityWatchCandidate = (candidate) => {
    if (!candidate || candidate.formal_recommendation !== false || candidate.recommended || candidate.actionable) return null;
    if (candidate.status === "invalid" && candidate.alert_level === "risk") {
      return { rule: "watch-invalid", label: "观察候选失效·停止等待", tone: "risk",
        detail: candidate.status_detail || "盘中结构已经破坏预案" };
    }
    if (candidate.status === "triggered" && candidate.alert_level === "watch") {
      return { rule: "watch-triggered", label: "候选封板触发·立即复核", tone: "watch",
        detail: `${candidate.status_detail || "封板触发"}；快速提醒不是正式买入推荐` };
    }
    if (candidate.status === "approaching" && candidate.alert_level === "watch") {
      return { rule: "watch-approaching", label: "距涨停不足1%·准备复核", tone: "watch",
        detail: candidate.status_detail || "提前打开盘口，未封板前不追入" };
    }
    return null;
  };

  const api = { DEFAULT_SETTINGS, normalizeSettings, eventKey, classifyAuctionCandidate, classifyLiveCandidate,
    classifyPriorityWatchCandidate, notifiedCodes, canPrompt, tradingDate, todayBoardCodes,
    todayPriorityWatchCodes, todayAlertEvents };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (typeof window === "undefined" || typeof document === "undefined") return;
  window.BoardAlertEngine = api;

  // 延续v2历史及每日名额，升级/清空展示记录不能重置额度。
  const STORAGE_KEY = "board-condition-alerts-v2";
  const MAX_EVENTS = 100;
  const panel = document.getElementById("boardAlertPanel");
  const rows = document.getElementById("boardAlertRows");
  const runState = document.getElementById("boardAlertRunState");
  const stats = document.getElementById("boardAlertStats");
  const launcher = document.getElementById("boardAlertLauncher");
  const unreadBadge = document.getElementById("boardAlertUnreadBadge");
  const toggleButton = document.getElementById("boardAlertToggle");
  const settingsDialog = document.getElementById("boardAlertSettings");
  const settingsForm = document.getElementById("boardAlertSettingsForm");
  if (!panel || !rows || !settingsDialog || !settingsForm) return;

  const localDate = () => tradingDate(new Date());
  const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  })[char]);
  const number = (value) => value == null || value === "" ? null : Number.isFinite(Number(value)) ? Number(value) : null;
  const fixed = (value, digits = 2) => number(value) == null ? "--" : number(value).toFixed(digits);
  const signed = (value) => number(value) == null ? "--" : `${number(value) >= 0 ? "+" : ""}${number(value).toFixed(2)}%`;
  const displayTime = (value) => {
    const parsed = new Date(value || Date.now());
    return Number.isNaN(parsed.getTime()) ? new Date().toLocaleTimeString("zh-CN", { hour12: false }) : parsed.toLocaleTimeString("zh-CN", { hour12: false });
  };

  const loadState = () => {
    let saved = {};
    try { saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}") || {}; } catch (_) { saved = {}; }
    const today = localDate();
    return {
      date: today,
      events: saved.date === today && Array.isArray(saved.events) ? saved.events.slice(0, MAX_EVENTS).map((item) => ({ ...item, active: false })) : [],
      triggered: saved.date === today && saved.triggered && typeof saved.triggered === "object" ? saved.triggered : {},
      notified_codes: notifiedCodes(saved, today),
      settings: normalizeSettings(saved.settings),
    };
  };

  let state = loadState();
  let selectedEventId = state.events[0]?.id || null;
  let selectedCount = 0;
  let lastScanAt = 0;
  let focusPolicyAt = 0;
  let dailyFocus = null;
  let allowedCodes = new Set();
  let priorityCodes = new Set();
  const visibleEvents = () => todayAlertEvents(state.events, state.date, allowedCodes, priorityCodes);
  let audioContext = null;

  const persist = () => {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(state)); } catch (_) { /* 浏览器禁用存储时仍保留本页预警。 */ }
  };

  const ensureToday = () => {
    const today = localDate();
    if (state.date === today) return;
    state = { date: today, events: [], triggered: {}, notified_codes: [], settings: state.settings };
    dailyFocus = null;
    allowedCodes = new Set();
    priorityCodes = new Set();
    selectedCount = 0;
    lastScanAt = 0;
    focusPolicyAt = 0;
    selectedEventId = null;
    persist();
  };

  const render = () => {
    ensureToday();
    const displayed = visibleEvents();
    if (!displayed.some((item) => item.id === selectedEventId)) selectedEventId = displayed[0]?.id || null;
    const unread = displayed.filter((item) => item.unread).length;
    if (runState) {
      const fresh = Date.now() - lastScanAt <= 60000;
      runState.textContent = !state.settings.enabled ? "[已暂停]" : fresh ? "[运行中]" : "[等待新数据]";
      runState.style.color = state.settings.enabled && fresh ? "var(--green)" : "var(--amber)";
    }
    if (stats) stats.textContent = `首选:${selectedCount}/1 · 观察:${priorityCodes.size}/5 · 今日:${Math.max(state.notified_codes.length, dailyFocus?.issued_count || 0)}/5${dailyFocus?.locked_code ? " · 已锁定" : ""} · 今日记录:${displayed.length}${unread ? ` · 未读:${unread}` : ""}`;
    if (toggleButton) toggleButton.textContent = state.settings.enabled ? "关闭预警" : "启动预警";
    if (unreadBadge) unreadBadge.textContent = String(unread);
    rows.innerHTML = displayed.length ? displayed.map((item) => `
      <button class="board-alert-row${item.id === selectedEventId ? " is-selected" : ""}${item.unread ? " is-unread" : ""}" data-alert-id="${escapeHtml(item.id)}" data-tone="${item.tone === "confirm" && !item.active ? "watch" : escapeHtml(item.tone)}" role="option" aria-selected="${item.id === selectedEventId}">
        <span class="board-alert-stock"><i>${item.tone === "risk" ? "◆" : item.tone === "watch" ? "◎" : "♟"}</i><span>${escapeHtml(item.name)} <small>${escapeHtml(item.code)}</small></span></span>
        <span>${escapeHtml(item.time)}</span><span>${fixed(item.price)}</span>
        <span class="board-alert-change ${number(item.change) != null && number(item.change) < 0 ? "is-negative" : "is-positive"}">${signed(item.change)}</span>
        <span class="board-alert-rule" title="${escapeHtml(item.detail)}">${escapeHtml(item.label)}${item.tone === "watch" ? " · 非正式买点" : item.tone === "confirm" && !item.active ? " · 今日记录/非当前买点" : ""}</span>
      </button>`).join("") : `<div class="board-alert-empty">${state.settings.enabled ? "等待今日正式首选或重点观察票触发；观察提醒会明确标注非正式买点。" : "条件预警已暂停，可点击“启动预警”恢复。"}</div>`;
  };

  const playAlertSound = (tone = "confirm") => {
    if (!state.settings.sound && tone !== "test") return;
    try {
      const AudioContextClass = window.AudioContext || window.webkitAudioContext;
      if (!AudioContextClass) return;
      audioContext = audioContext || new AudioContextClass();
      if (audioContext.state === "suspended") audioContext.resume();
      const oscillator = audioContext.createOscillator();
      const gain = audioContext.createGain();
      oscillator.type = "sine";
      oscillator.frequency.setValueAtTime(tone === "risk" ? 420 : 820, audioContext.currentTime);
      oscillator.frequency.exponentialRampToValueAtTime(tone === "risk" ? 260 : 620, audioContext.currentTime + 0.22);
      gain.gain.setValueAtTime(0.0001, audioContext.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.12, audioContext.currentTime + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, audioContext.currentTime + 0.28);
      oscillator.connect(gain).connect(audioContext.destination);
      oscillator.start();
      oscillator.stop(audioContext.currentTime + 0.3);
    } catch (_) { /* 浏览器未授权音频时静默降级。 */ }
  };

  const desktopNotify = (newEvents) => {
    if (!state.settings.desktop || typeof Notification === "undefined" || Notification.permission !== "granted" || !newEvents.length) return;
    const first = newEvents[0];
    const title = newEvents.length === 1 ? `${first.name} · ${first.label}` : `新增${newEvents.length}条打板条件预警`;
    const body = newEvents.length === 1 ? `${first.code} 现价${fixed(first.price)} 涨幅${signed(first.change)}` : newEvents.slice(0, 3).map((item) => `${item.name} ${item.label}`).join("；");
    try { new Notification(title, { body, tag: `board-alert-${state.date}` }); } catch (_) { /* 桌面通知不可用时保留站内浮窗。 */ }
  };

  const openPanel = () => {
    panel.hidden = false;
    if (launcher) launcher.hidden = true;
  };

  const hidePanel = () => {
    panel.hidden = true;
    if (launcher) launcher.hidden = false;
  };

  const addEvents = (candidates, classifier, generatedAt, source) => {
    ensureToday();
    if (!state.settings.enabled || !Array.isArray(candidates)) { render(); return; }
    const newEvents = [];
    for (const candidate of candidates) {
      const classified = classifier(candidate, state.settings);
      if (!classified || !candidate?.code || classified.tone === "watch") continue;
      const code = String(candidate.code);
      if (!allowedCodes.has(code)) continue;
      if (classified.tone === "confirm" && !canPrompt(state.notified_codes, code, dailyFocus?.locked_code)) continue;
      const key = eventKey(state.date, candidate.code, classified.rule);
      if (state.triggered[key]) continue;
      const event = {
        id: key,
        trade_date: state.date,
        code: String(candidate.code),
        name: String(candidate.name || candidate.code),
        time: source === "auction" ? "09:25:00" : displayTime(generatedAt),
        price: number(source === "auction" ? candidate.auction_price ?? candidate.previous_close : candidate.price),
        change: number(source === "auction" ? candidate.auction_gap_percent : candidate.change_percent),
        source,
        active: classified.tone === "confirm",
        unread: true,
        ...classified,
      };
      state.triggered[key] = generatedAt || new Date().toISOString();
      if (classified.tone === "confirm") state.notified_codes.push(code);
      newEvents.push(event);
    }
    if (!newEvents.length) { render(); return; }
    state.events = [...newEvents, ...state.events].slice(0, MAX_EVENTS);
    selectedEventId = newEvents[0].id;
    persist();
    if (state.settings.autoOpen) openPanel();
    playAlertSound(newEvents.some((item) => item.tone === "risk") ? "risk" : "confirm");
    desktopNotify(newEvents);
    render();
  };

  const addPriorityWatchEvents = (candidates, generatedAt) => {
    ensureToday();
    if (!state.settings.enabled || !Array.isArray(candidates)) { render(); return; }
    const newEvents = [];
    for (const candidate of candidates) {
      const code = String(candidate?.code || "");
      const classified = classifyPriorityWatchCandidate(candidate);
      if (!classified || !priorityCodes.has(code)) continue;
      const key = eventKey(state.date, code, classified.rule);
      if (state.triggered[key]) continue;
      const event = {
        id: key, trade_date: state.date, scope: "priority_watch",
        code, name: String(candidate.name || code), time: displayTime(generatedAt),
        price: number(candidate.price), change: number(candidate.change_percent),
        source: "priority_watch", active: candidate.status === "triggered", unread: true,
        ...classified,
      };
      state.triggered[key] = generatedAt || new Date().toISOString();
      newEvents.push(event);
    }
    if (!newEvents.length) { render(); return; }
    state.events = [...newEvents, ...state.events].slice(0, MAX_EVENTS);
    selectedEventId = newEvents[0].id;
    persist();
    if (state.settings.autoOpen) openPanel();
    playAlertSound(newEvents.some((item) => item.tone === "risk") ? "risk" : "watch");
    desktopNotify(newEvents);
    render();
  };

  const processLiveSnapshot = (data) => {
    ensureToday();
    if (!data || data.selected_date !== localDate() || data.historical) return;
    if (tradingDate(data.generated_at) !== state.date) return;
    const generated = new Date(data.generated_at).getTime();
    ensureToday();
    if (!Number.isFinite(generated) || Date.now() - generated > 60000 || generated > Date.now() + 5000 || generated < Math.max(lastScanAt, focusPolicyAt)) return;
    lastScanAt = generated;
    dailyFocus = data.daily_focus || null;
    allowedCodes = todayBoardCodes(data, state.date);
    priorityCodes = todayPriorityWatchCodes(data, state.date);
    const picks = dailyFocus?.available ? (data.candidates || []).filter((item) => allowedCodes.has(String(item.code)) && item.primary_pick === true && item.recommended === true && item.code === dailyFocus.primary_code).slice(0, 1) : [];
    selectedCount = picks.length;
    const activeCodes = new Set(picks.map((item) => item.code));
    state.events.forEach((item) => { if (item.tone === "confirm") item.active = state.settings.enabled && !dailyFocus?.locked_code && activeCodes.has(item.code); });
    const watches = (data.watch_candidates || []).map((item) => ({ ...item, recommended: false }));
    persist();
    addEvents([...picks, ...watches], classifyLiveCandidate, data.generated_at, "live");
  };

  const processPriorityWatchSnapshot = (data) => {
    ensureToday();
    if (!data || data.selected_date !== state.date || data.historical || tradingDate(data.generated_at) !== state.date) return;
    const generated = new Date(data.generated_at).getTime();
    if (!Number.isFinite(generated) || Date.now() - generated > 15000 || generated > Date.now() + 5000) return;
    lastScanAt = Math.max(lastScanAt, generated);
    addPriorityWatchEvents((data.candidates || []).filter((item) => priorityCodes.has(String(item.code))), data.generated_at);
  };

  const selectEvent = (id) => {
    ensureToday();
    const item = visibleEvents().find((event) => event.id === id);
    if (!item) return;
    selectedEventId = id;
    item.unread = false;
    persist();
    render();
  };

  const openSelectedStock = () => {
    ensureToday();
    const displayed = visibleEvents();
    const item = displayed.find((event) => event.id === selectedEventId) || displayed[0];
    if (!item) return;
    const stockTab = document.querySelector('.top-tab[data-tab="stockTab"]');
    const input = document.getElementById("code");
    const form = document.getElementById("search");
    stockTab?.click();
    if (!input || !form) return;
    input.value = item.code;
    window.requestAnimationFrame(() => form.requestSubmit());
  };

  const fillSettingsForm = () => {
    const values = state.settings;
    for (const name of ["enabled", "autoOpen", "sound", "desktop"]) settingsForm.elements[name].checked = values[name];
    for (const name of Object.keys(DEFAULT_SETTINGS.rules)) settingsForm.elements[name].checked = values.rules[name];
  };

  const closeOperations = () => document.getElementById("boardAlertOperations")?.removeAttribute("open");

  window.addEventListener("board:live-snapshot", (event) => processLiveSnapshot(event.detail));
  window.addEventListener("board:watch-snapshot", (event) => processPriorityWatchSnapshot(event.detail));
  window.addEventListener("board:focus-policy", (event) => {
    ensureToday();
    dailyFocus = event.detail;
    allowedCodes = todayBoardCodes({ selected_date: state.date, daily_focus: dailyFocus }, state.date);
    focusPolicyAt = Date.now();
    state.events.forEach((item) => { item.active = false; });
    persist();
    render();
  });
  window.addEventListener("board:live-unavailable", () => {
    lastScanAt = 0;
    selectedCount = 0;
    state.events.forEach((item) => { item.active = false; });
    persist();
    render();
  });
  rows.addEventListener("click", (event) => {
    const row = event.target.closest("[data-alert-id]");
    if (row) selectEvent(row.dataset.alertId);
  });
  rows.addEventListener("dblclick", (event) => {
    const row = event.target.closest("[data-alert-id]");
    if (row) { selectEvent(row.dataset.alertId); openSelectedStock(); }
  });
  toggleButton?.addEventListener("click", () => {
    state.settings.enabled = !state.settings.enabled;
    if (!state.settings.enabled) state.events.forEach((item) => { item.active = false; });
    persist();
    render();
  });
  document.getElementById("boardAlertClose")?.addEventListener("click", hidePanel);
  document.getElementById("boardAlertMinimize")?.addEventListener("click", (event) => {
    const minimized = panel.classList.toggle("is-minimized");
    event.currentTarget.textContent = minimized ? "+" : "—";
    event.currentTarget.title = minimized ? "展开" : "最小化";
  });
  launcher?.addEventListener("click", openPanel);
  document.getElementById("boardAlertOpenStock")?.addEventListener("click", openSelectedStock);
  document.getElementById("boardAlertMarkRead")?.addEventListener("click", () => {
    ensureToday();
    visibleEvents().forEach((item) => { item.unread = false; });
    persist(); closeOperations(); render();
  });
  document.getElementById("boardAlertClear")?.addEventListener("click", () => {
    if (!window.confirm("清空今日预警记录？无法撤销；每日5只名额及已提示代码不会重置。")) return;
    state.events = [];
    selectedEventId = null;
    persist(); closeOperations(); render();
  });
  document.getElementById("boardAlertTestSound")?.addEventListener("click", () => {
    playAlertSound("test"); closeOperations();
  });
  document.getElementById("boardAlertSettingsButton")?.addEventListener("click", () => {
    fillSettingsForm();
    settingsDialog.showModal();
  });
  document.getElementById("boardAlertSettingsClose")?.addEventListener("click", () => settingsDialog.close());
  document.getElementById("boardAlertSettingsCancel")?.addEventListener("click", () => settingsDialog.close());
  settingsForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const next = {
      enabled: settingsForm.elements.enabled.checked,
      autoOpen: settingsForm.elements.autoOpen.checked,
      sound: settingsForm.elements.sound.checked,
      desktop: settingsForm.elements.desktop.checked,
      rules: Object.fromEntries(Object.keys(DEFAULT_SETTINGS.rules).map((name) => [name, settingsForm.elements[name].checked])),
    };
    if (next.desktop && typeof Notification !== "undefined" && Notification.permission !== "granted") {
      try { next.desktop = (await Notification.requestPermission()) === "granted"; } catch (_) { next.desktop = false; }
    }
    state.settings = normalizeSettings(next);
    if (!state.settings.enabled) state.events.forEach((item) => { item.active = false; });
    persist();
    settingsDialog.close();
    if (state.settings.sound) playAlertSound("test");
    render();
  });

  const dragHandle = panel.querySelector("[data-alert-drag-handle]");
  dragHandle?.addEventListener("pointerdown", (event) => {
    if (event.target.closest("button")) return;
    const rect = panel.getBoundingClientRect();
    const offsetX = event.clientX - rect.left;
    const offsetY = event.clientY - rect.top;
    panel.style.right = "auto";
    panel.style.bottom = "auto";
    const move = (moveEvent) => {
      const left = Math.max(6, Math.min(document.documentElement.clientWidth - panel.offsetWidth - 6, moveEvent.clientX - offsetX));
      const top = Math.max(6, Math.min(window.innerHeight - panel.offsetHeight - 6, moveEvent.clientY - offsetY));
      panel.style.left = `${left}px`;
      panel.style.top = `${top}px`;
    };
    const stop = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", stop);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", stop, { once: true });
  });

  window.addEventListener("resize", () => {
    for (const key of ["left", "top", "right", "bottom"]) panel.style.removeProperty(key);
  });
  window.setInterval(() => {
    if (Date.now() - lastScanAt > 60000) {
      selectedCount = 0;
      state.events.forEach((item) => { item.active = false; });
    }
    render();
  }, 10000);
  render();
})();
