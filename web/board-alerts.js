(() => {
  const DEFAULT_SETTINGS = Object.freeze({
    enabled: true,
    autoOpen: true,
    sound: true,
    desktop: false,
    rules: Object.freeze({
      auction: true,
      highTurnover: true,
      discovered: true,
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
    rules: { ...DEFAULT_SETTINGS.rules, ...(value.rules || {}) },
  });

  const eventKey = (tradeDate, code, rule) => `${tradeDate}|${code}|${rule}`;

  const classifyAuctionCandidate = (candidate, rawSettings = DEFAULT_SETTINGS) => {
    const settings = normalizeSettings(rawSettings);
    const eventRisk = candidate?.corporate_event_risk?.level === "high";
    if (eventRisk) {
      return settings.rules.risk ? {
        rule: "event-risk", label: "并购重组风险剔除", tone: "risk",
        detail: candidate.corporate_event_risk?.label || "重大事项高风险",
      } : null;
    }
    if (!candidate || candidate.risk_veto || candidate.eligible === false) return null;
    if (candidate.high_turnover_chain_matched || candidate.strategy_mode === "高换手强竞价连板") {
      return settings.rules.highTurnover ? {
        rule: "high-turnover-auction", label: "高换手强竞价连板", tone: "confirm",
        detail: "换手>1.2%、高开≥5%、竞价额>5000万",
      } : null;
    }
    if (!settings.rules.auction) return null;
    if (candidate.nuclear_button_matched || candidate.strategy_mode === "反核按钮竞价抄底") {
      return { rule: "auction", label: "反核竞价观察", tone: "watch", detail: candidate.action || "等待开盘承接" };
    }
    if (candidate.strong_one_price_one_to_two) {
      return { rule: "auction", label: "强竞价1进2", tone: "watch", detail: candidate.action || "一字板排队观察" };
    }
    const selected = Boolean(candidate.actionable || candidate.board_entry_allowed || candidate.recommendation_badge);
    return {
      rule: "auction",
      label: selected ? "09:25竞价入选" : "09:25竞价观察",
      tone: selected ? "confirm" : "watch",
      detail: candidate.action || candidate.strategy_mode || "等待09:30确认",
    };
  };

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
    if (candidate.sealed) {
      return settings.rules.sealed ? {
        rule: "sealed", label: "涨停封板确认", tone: candidate.tone === "confirm" ? "confirm" : "watch",
        detail: candidate.decision || candidate.summary || "观察封单稳定性",
      } : null;
    }
    const discovered = candidate.discovery_source && candidate.discovery_source !== "09:25冻结候选";
    if (discovered) {
      return settings.rules.discovered ? {
        rule: "live-discovered", label: candidate.discovery_source, tone: "watch",
        detail: candidate.decision || candidate.summary || "09:30盘中新增",
      } : null;
    }
    if (candidate.tone === "confirm") {
      return settings.rules.confirm ? {
        rule: "open-confirm", label: "开盘承接确认", tone: "confirm",
        detail: candidate.decision || candidate.summary || "开盘条件确认",
      } : null;
    }
    return null;
  };

  const api = { DEFAULT_SETTINGS, normalizeSettings, eventKey, classifyAuctionCandidate, classifyLiveCandidate };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (typeof window === "undefined" || typeof document === "undefined") return;
  window.BoardAlertEngine = api;

  const STORAGE_KEY = "board-condition-alerts-v1";
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

  const localDate = () => {
    const now = new Date();
    return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
  };
  const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  })[char]);
  const number = (value) => Number.isFinite(Number(value)) ? Number(value) : null;
  const fixed = (value, digits = 2) => number(value) == null ? "--" : number(value).toFixed(digits);
  const signed = (value) => number(value) == null ? "--" : `${number(value) >= 0 ? "+" : ""}${number(value).toFixed(2)}%`;
  const displayTime = (value) => {
    const parsed = new Date(value || Date.now());
    return Number.isNaN(parsed.getTime()) ? new Date().toLocaleTimeString("zh-CN", { hour12: false }) : parsed.toLocaleTimeString("zh-CN", { hour12: false });
  };

  const loadState = () => {
    let saved = {};
    try { saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}"); } catch (_) { saved = {}; }
    const today = localDate();
    return {
      date: today,
      events: saved.date === today && Array.isArray(saved.events) ? saved.events.slice(0, MAX_EVENTS) : [],
      triggered: saved.date === today && saved.triggered && typeof saved.triggered === "object" ? saved.triggered : {},
      settings: normalizeSettings(saved.settings),
    };
  };

  let state = loadState();
  let selectedEventId = state.events[0]?.id || null;
  let monitoredCount = 0;
  let audioContext = null;

  const persist = () => {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(state)); } catch (_) { /* 浏览器禁用存储时仍保留本页预警。 */ }
  };

  const ensureToday = () => {
    const today = localDate();
    if (state.date === today) return;
    state = { date: today, events: [], triggered: {}, settings: state.settings };
    selectedEventId = null;
    persist();
  };

  const render = () => {
    ensureToday();
    const unread = state.events.filter((item) => item.unread).length;
    if (runState) {
      runState.textContent = state.settings.enabled ? "[运行中]" : "[已暂停]";
      runState.style.color = state.settings.enabled ? "var(--green)" : "var(--amber)";
    }
    if (stats) stats.textContent = `品种数:${monitoredCount} · 条数:${state.events.length}${unread ? ` · 未读:${unread}` : ""}`;
    if (toggleButton) toggleButton.textContent = state.settings.enabled ? "关闭预警" : "启动预警";
    if (unreadBadge) unreadBadge.textContent = String(unread);
    rows.innerHTML = state.events.length ? state.events.map((item) => `
      <button class="board-alert-row${item.id === selectedEventId ? " is-selected" : ""}${item.unread ? " is-unread" : ""}" data-alert-id="${escapeHtml(item.id)}" data-tone="${escapeHtml(item.tone)}" role="option" aria-selected="${item.id === selectedEventId}">
        <span class="board-alert-stock"><i>${item.tone === "risk" ? "◆" : "♟"}</i><span>${escapeHtml(item.name)} <small>${escapeHtml(item.code)}</small></span></span>
        <span>${escapeHtml(item.time)}</span><span>${fixed(item.price)}</span>
        <span class="board-alert-change ${number(item.change) != null && number(item.change) < 0 ? "is-negative" : "is-positive"}">${signed(item.change)}</span>
        <span class="board-alert-rule" title="${escapeHtml(item.detail)}">${escapeHtml(item.label)}</span>
      </button>`).join("") : `<div class="board-alert-empty">${state.settings.enabled ? "等待09:25竞价或09:30盘中条件首次命中…" : "条件预警已暂停，可点击“启动预警”恢复。"}</div>`;
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
    monitoredCount = Array.isArray(candidates) ? candidates.length : 0;
    if (!state.settings.enabled || !Array.isArray(candidates)) { render(); return; }
    const newEvents = [];
    for (const candidate of candidates) {
      const classified = classifier(candidate, state.settings);
      if (!classified || !candidate?.code) continue;
      const key = eventKey(state.date, candidate.code, classified.rule);
      if (state.triggered[key]) continue;
      const event = {
        id: key,
        code: String(candidate.code),
        name: String(candidate.name || candidate.code),
        time: source === "auction" ? "09:25:00" : displayTime(generatedAt),
        price: number(candidate.price ?? candidate.auction_price ?? candidate.previous_close),
        change: number(candidate.change_percent ?? candidate.auction_gap_percent),
        source,
        unread: true,
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
    playAlertSound(newEvents.some((item) => item.tone === "risk") ? "risk" : "confirm");
    desktopNotify(newEvents);
    render();
  };

  const processAuctionSnapshot = (data) => {
    if (!data || data.selected_date !== localDate() || data.historical || data.auction_phase !== "final") return;
    const candidates = [...(data.candidates || []), ...(data.risk_exclusions || []).filter((item) => item?.corporate_event_risk?.level === "high")];
    addEvents(candidates, classifyAuctionCandidate, data.generated_at, "auction");
  };

  const processLiveSnapshot = (data) => addEvents(data?.candidates || [], classifyLiveCandidate, data?.generated_at, "live");

  const selectEvent = (id) => {
    const item = state.events.find((event) => event.id === id);
    if (!item) return;
    selectedEventId = id;
    item.unread = false;
    persist();
    render();
  };

  const openSelectedStock = () => {
    const item = state.events.find((event) => event.id === selectedEventId) || state.events[0];
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

  window.addEventListener("board:auction-snapshot", (event) => processAuctionSnapshot(event.detail));
  window.addEventListener("board:live-snapshot", (event) => processLiveSnapshot(event.detail));
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
    state.events.forEach((item) => { item.unread = false; });
    persist(); closeOperations(); render();
  });
  document.getElementById("boardAlertClear")?.addEventListener("click", () => {
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
      const left = Math.max(6, Math.min(window.innerWidth - panel.offsetWidth - 6, moveEvent.clientX - offsetX));
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

  render();
})();
