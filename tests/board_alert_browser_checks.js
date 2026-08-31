// 在独立8001假数据页面用 agent-browser eval -b 执行；仅修改隔离测试会话数据。
(async () => {
  if (location.origin !== "http://127.0.0.1:8001") throw new Error("只能在隔离假数据服务执行");
  const checks = [];
  const assert = (condition, label) => { if (!condition) throw new Error(label); checks.push(label); };
  const stored = () => JSON.parse(localStorage.getItem("board-condition-alerts-v2"));
  const emit = (data) => window.dispatchEvent(new CustomEvent("board:live-snapshot", { detail: data }));
  const sample = await (await fetch("/api/board-open-guard")).json();
  // 模拟服务端今日最多5只正式名单；名单外候选即使带推荐标记也不可预警。
  const issued = [...sample.daily_focus.issued, ...Array.from({ length: 4 }, (_, index) => ({
    code: String(610001 + index), first_at: sample.generated_at,
  }))];
  const snapshot = (picks, extra = {}) => ({ ...sample, generated_at: new Date().toISOString(), candidates: picks, watch_candidates: [],
    daily_focus: { ...sample.daily_focus, issued, issued_count: issued.length, primary_code: picks[0]?.code, locked_code: null }, ...extra });
  emit(sample);
  const originalCount = stored().events.length;
  emit(sample);
  assert(stored().events.length === originalCount, "同日重复采样不重复预警");
  assert(document.querySelectorAll("#strategyCandidates .strategy-candidate").length === 1, "策略页仅显示唯一首选");
  assert(stored().notified_codes.length === 1, "7只合格备选只消耗1个提示名额");
  const extra = { ...sample.candidates[0], code: "610001", name: "边界测试", price: null, change_percent: null };
  emit(snapshot([extra], { selected_date: "2000-01-01" }));
  emit(snapshot([extra], { historical: true }));
  emit(snapshot([extra], { generated_at: new Date(Date.now() - 61000).toISOString() }));
  emit(snapshot([extra], { generated_at: new Date(Date.now() + 60000).toISOString() }));
  assert(stored().events.length === originalCount, "历史、过期和未来时间戳不触发");
  emit(snapshot([extra], { daily_focus: undefined }));
  assert(stored().events.length === originalCount, "旧接口没有每日首选策略时不触发");
  const six = Array.from({ length: 6 }, (_, index) => ({ ...extra, code: String(610001 + index) }));
  emit(snapshot(six));
  assert(stored().events.length === originalCount + 1, "多条候选异常输入也只提示当前主选代码");
  assert(stored().events.filter((row) => row.active).length === 1, "旧首选移出时标为历史");
  assert(document.querySelector('[data-alert-id$="610001|sealed"]').textContent.includes("--"), "缺失价格显示--");
  emit(snapshot([{ ...extra, recommendation_kind: "strong_open", sealed: false }]));
  assert(stored().events.length === originalCount + 1, "同代码切换确认条件也不再提示");
  for (const row of six.slice(1)) emit(snapshot([row]));
  assert(stored().notified_codes.length === 5, "多轮换股全天累计最多5个代码");
  assert(stored().events.filter((row) => row.tone === "confirm").length === 5, "第6只及以后的正向提示被拦截");
  const beforeRisk = stored().events.length;
  emit(snapshot([], { watch_candidates: [{ ...extra, recommended: false, sealed: false, failed_board: true, decision: "炸板转弱" }] }));
  assert(stored().events.length === beforeRisk + 1 && stored().events[0].tone === "risk", "达到名额上限后已提示股票仍有炸板风险提醒");
  emit(snapshot([], { watch_candidates: [{ ...extra, code: "610099", recommended: false, failed_board: true }] }));
  assert(stored().events.length === beforeRisk + 1, "陌生观察票风险不弹窗打扰");
  window.dispatchEvent(new CustomEvent("board:focus-policy", { detail: { ...sample.daily_focus, locked_code: sample.candidates[0].code } }));
  assert(document.getElementById("boardAlertStats").textContent.includes("已锁定"), "锁定立即同步预警浮窗");
  assert(stored().events.every((row) => !row.active), "锁定后旧买点不再标为可执行");
  emit(snapshot([], { daily_focus: { ...sample.daily_focus, locked_code: sample.candidates[0].code },
    watch_candidates: [{ ...sample.candidates[0], recommended: false, corporate_event_risk: { level: "high", label: "测试风险" } }] }));
  assert(stored().events[0].rule === "event-risk", "锁定后已提示股票的重大事项风险继续提醒");
  const oldConfirm = window.confirm;
  window.confirm = () => true;
  document.getElementById("boardAlertClear").click();
  window.confirm = oldConfirm;
  assert(stored().events.length === 0 && stored().notified_codes.length === 5, "清空显示记录不返还名额");
  window.dispatchEvent(new CustomEvent("board:live-unavailable"));
  assert(document.getElementById("boardAlertRunState").textContent.includes("等待"), "断网不显示运行中");
  return { passed: checks.length, checks };
})()
