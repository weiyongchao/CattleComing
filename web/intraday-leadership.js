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
        evidence.style.cssText = `color:${candidate.limit_down_reversal ? "var(--red)" : "#bfd0ca"};font-size:12px;line-height:1.8;margin:4px 0 8px`;
        evidence.textContent = candidate.limit_down_reversal
          ? `${candidate.signal} · 开盘${candidate.open_gap_percent >= 0 ? "+" : ""}${candidate.open_gap_percent.toFixed(2)}% · 已拉离跌停${candidate.rebound_from_low_percent.toFixed(2)}% · 近5日${candidate.recent_5_limit_up_count}次涨停 · 建议仓位上限${candidate.position_cap_percent}%`
          : `${candidate.leader_label} · 龙头成熟度 ${candidate.leadership_score}分 · 行业市值第${candidate.industry_cap_rank}名 · 行业成交额第${candidate.industry_amount_rank}名 · 总市值${(candidate.market_cap / 1e8).toFixed(0)}亿`;
        const content = card.firstElementChild?.firstElementChild;
        (content || card).appendChild(evidence);
        if (candidate.limit_down_reversal && candidate.risks?.length) {
          const risk = document.createElement("p");
          risk.style.cssText = "color:var(--red);font-size:12px;line-height:1.8;margin:4px 0 8px";
          risk.textContent = `高风险：${candidate.risks.join("；")}`;
          (content || card).appendChild(risk);
        }
        card.dataset.leadershipEnhanced = "1";
      });
      const count = document.getElementById("intradayCount");
      if (count) count.textContent = `展示 ${data.candidates.length} 只 · 高风险反核 ${data.reversal_count || 0} 只 · 合格 ${data.qualified_count} 只`;
      const note = document.getElementById("intradayNote");
      if (note) {
        note.textContent = `技术合格 ${data.technical_qualified_count} 只 · 龙头成熟度淘汰 ${data.leadership_filtered_count} 只 · 高风险反核 ${data.reversal_count || 0} 只 · 最终合格 ${data.qualified_count} 只。${data.method} ${data.disclaimer}`;
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
