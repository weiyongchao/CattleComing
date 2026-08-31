// 通过 agent-browser eval 在隔离8001测试页面运行，不访问业务服务。
(async () => {
  if (location.origin !== "http://127.0.0.1:8001") throw new Error("仅限离线测试页面");
  const checks = [];
  const assert = (value, label) => { if (!value) throw new Error(label); checks.push(label); };
  const originalFetch = window.fetch;
  let phase = "indicative";
  window.fetch = (url, options) => {
    if (String(url).startsWith("/api/board-plan")) return originalFetch(`/api/board-plan?phase=${phase}`, options);
    if (String(url).startsWith("/api/board-open-guard") && phase === "indicative") {
      return Promise.resolve(new Response(JSON.stringify({ error: "09:30以后才确认推荐（离线测试）" }), { status: 409 }));
    }
    return originalFetch(url, options);
  };
  const waitFor = async (predicate) => {
    const until = Date.now() + 8000;
    while (!predicate()) {
      if (Date.now() > until) throw new Error("等待页面阶段切换超时");
      await new Promise((resolve) => setTimeout(resolve, 40));
    }
  };
  const title = () => document.getElementById("boardCandidates").closest("article").querySelector("h2").textContent;
  const note = () => document.getElementById("boardHandoffNote").textContent;
  const refresh = async (newPhase, expected) => {
    phase = newPhase;
    await waitFor(() => !document.getElementById("boardPlanButton").disabled);
    document.getElementById("boardPlanButton").click();
    await waitFor(() => title().includes(expected) && !document.getElementById("boardPlanButton").disabled);
  };
  try {
    await refresh("indicative", "09:20–09:25竞价预选");
    assert(note().includes("不是买入推荐"), "竞价优先观察不冒充买点");
    assert(note().includes("后台采集已启动"), "展示后台采集状态");
    assert(document.querySelectorAll("#boardCandidates > .stock-disclosure").length === 5, "竞价主榜最多5只");
    assert(document.getElementById("boardCandidates").textContent.includes("参考量额（待最终核验）"), "竞价参考量额与最终成交区分");
    await waitFor(() => document.getElementById("boardOpenGuardList").textContent.includes("原精选已暂停"));
    assert(!document.querySelector("#boardOpenGuardList > .stock-disclosure"), "竞价阶段没有正式首选卡片");
    await refresh("final", "09:25竞价预选定稿");
    assert(note().includes("09:30后"), "最终竞价仍需开盘交易确认");
    assert(!document.getElementById("boardCandidates").textContent.includes("参考量额（待最终核验）"), "最终竞价恢复成交字段标识");
    await refresh("late", "开盘后新增观察");
    assert(note().includes("不倒写"), "迟到候选不冒充09:25入选");
    await refresh("historical", "历史回放");
    assert(note().includes("不代表当时"), "回放不能冒充已发推荐");
    await refresh("indicative", "09:20–09:25竞价预选");
    return { passed: checks.length, checks };
  } finally {
    window.fetch = originalFetch;
  }
})();
