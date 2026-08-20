(() => {
  const list = document.getElementById("intradayCandidates");
  if (!list) return;

  let enhancing = false;
  const enhance = async () => {
    const cards = Array.from(list.querySelectorAll(":scope > article"));
    if (!cards.length || enhancing || cards.every((card) => card.dataset.leadershipEnhanced === "1")) return;
    enhancing = true;
    try {
      const response = await fetch("/api/intraday-plan");
      const data = await response.json();
      if (!response.ok) return;
      cards.forEach((card, index) => {
        const candidate = data.candidates[index];
        if (!candidate || card.dataset.leadershipEnhanced === "1") return;
        const evidence = document.createElement("p");
        evidence.style.cssText = "color:#bfd0ca;font-size:12px;line-height:1.8;margin:4px 0 8px";
        evidence.textContent = `${candidate.leader_label} · 龙头成熟度 ${candidate.leadership_score}分 · 行业市值第${candidate.industry_cap_rank}名 · 行业成交额第${candidate.industry_amount_rank}名 · 总市值${(candidate.market_cap / 1e8).toFixed(0)}亿`;
        const content = card.firstElementChild?.firstElementChild;
        (content || card).appendChild(evidence);
        card.dataset.leadershipEnhanced = "1";
      });
      const count = document.getElementById("intradayCount");
      if (count) count.textContent = `展示 ${data.candidates.length} 只龙头 · 龙头合格 ${data.qualified_count} 只`;
      const note = document.getElementById("intradayNote");
      if (note) {
        note.textContent = `技术合格 ${data.technical_qualified_count} 只 · 龙头成熟度淘汰 ${data.leadership_filtered_count} 只 · 龙头合格 ${data.qualified_count} 只。${data.method} ${data.disclaimer}`;
      }
    } catch (_) {
      // 原候选结果仍可正常展示，增强信息失败不阻断主流程。
    } finally {
      enhancing = false;
    }
  };

  new MutationObserver(enhance).observe(list, { childList: true });
  enhance();
})();
