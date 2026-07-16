"""
Stage 15（stage15-market-fit）專屬的完整回放頁產生器（比照
stage14-signals/build_replay.py 的整體架構複製再改——不 import
stage14-signals，各自維護是這個專案一路以來的既有慣例，理由見
stage10/build_replay.py 開頭的說明）。

跟 stage14-signals 最大的差異：策略導向、由上而下的市場競爭力驗證流程
跳過整個 Discover/Define，state 形狀完全不同（沒有
`candidate_jobs`/`job_evidence`/`selected_job`/`problem_statement`/
`hmw`，改成 `competitive_landscape`——真實 web_search 找到的競品清單），
所以「可量化差異」對比表裡「候選 job 覆蓋率」換成「競品掃描覆蓋度」
（真實找到幾筆競品資訊，程式碼驗證過來源網址不是 LLM 自稱），「虛擬
問卷」這一列的文字從困擾度/強度改成購買意願/差異化感受（跟 graph.py
`survey_one_stratum()` 測的指標一致）。其餘維度（真實搜尋依據、BMC
完整度、單位經濟、idea 多樣性、DFV 結構化評分覆蓋、最終評估者誠實
對照評分）跟 stage14-signals 同一套資料形狀，直接沿用。

`COST_SNAPSHOT_ACTIONS` 只需要排除 `evaluate_final_outputs`——
stage15-market-fit 沒有 stage14-signals 那種「同一位受訪者多輪訪談共用
一個 invocation」的 `interview_turn` 模式了（`concept_test_one_person`
固定 2 輪問答＋分類，全部包在同一次呼叫裡，只在結尾 emit 一次，不是
mid-snapshot），`sum_display_cost()` 因此不需要再對 `interview_turn`
做特殊處理。

用法：
    python build_replay.py <events.jsonl 路徑> <stage15-run-*.json 路徑> <輸出.html 路徑>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

SHARED_RENDERERS_JS = (
    Path(__file__).resolve().parent.parent / "stage10" / "shared_renderers.js"
).read_text(encoding="utf-8")

# 跟 stage15-market-fit/graph.py 的 QUANTIFIED_BMC_KEYS 保持一致——「收益流」
# 「成本結構」兩格是結構化物件（narrative/monthly_estimate_twd/basis），
# 不是純字串。
QUANT_BMC_KEYS = {"收益流", "成本結構"}

# evaluate_final_outputs_with_users() 對每位評估者各發一則
# evaluate_final_outputs 事件，全部共用同一個 instrument() invocation
# （set_current_node("evaluate_with_agents") 只在迴圈開始前呼叫一次）——
# 每則事件的 cost_usd 是「這個 invocation 至今累計」，不是單則增量，是
# 「mid-snapshot」模式：真正的累計總額在迴圈結束後的
# user_evaluation_summary 事件裡，evaluate_final_outputs 整批都是重複
# 計入的中途快照，要整批排除——跟
# stage15-market-fit/static/index.html 的 STAGE15_COST_SNAPSHOT_ACTIONS 保持一致。
COST_SNAPSHOT_ACTIONS = {"evaluate_final_outputs"}


def sum_display_cost(events: list) -> float:
    """stage15-market-fit 沒有 stage14-signals 那種「同一位受訪者多輪
    switch 訪談共用一個 invocation」的 `interview_turn` mid-snapshot
    模式了（`concept_test_one_person` 固定 2 輪問答＋分類全部包在同一次
    呼叫裡，只在結尾 emit 一次），所以這裡直接用 COST_SNAPSHOT_ACTIONS
    整批排除即可，不需要 stage14-signals 那種「每位受訪者只留最後一輪」
    的特殊處理。"""
    total = 0.0
    for e in events:
        if e.get("action") in COST_SNAPSHOT_ACTIONS:
            continue
        total += e.get("cost_usd", 0.0)
    return total


def _bmc_cell_filled(key: str, val) -> bool:
    if key in QUANT_BMC_KEYS:
        return (
            isinstance(val, dict)
            and isinstance(val.get("narrative"), str) and bool(val["narrative"].strip())
            and isinstance(val.get("monthly_estimate_twd"), (int, float))
            and not isinstance(val.get("monthly_estimate_twd"), bool)
        )
    return isinstance(val, str) and bool(val.strip())


def _bmc_filled_count(bmc: dict) -> int:
    return sum(1 for k, v in (bmc or {}).items() if _bmc_cell_filled(k, v))


def _unit_economics(bmc: dict) -> dict:
    bmc = bmc or {}
    revenue_cell = bmc.get("收益流")
    cost_cell = bmc.get("成本結構")
    revenue = revenue_cell.get("monthly_estimate_twd") if isinstance(revenue_cell, dict) else None
    cost = cost_cell.get("monthly_estimate_twd") if isinstance(cost_cell, dict) else None
    revenue = revenue or 0
    cost = cost or 0
    margin = revenue - cost
    return {
        "monthly_revenue_twd": revenue,
        "monthly_cost_twd": cost,
        "monthly_margin_twd": margin,
        "is_viable": margin > 0,
    }


def compute_comparison(run_data: dict) -> dict:
    baseline = run_data.get("baseline") or {}
    baseline_proposal = baseline.get("proposal") or {}
    baseline_metrics = baseline.get("metrics") or {}
    winner_idea = run_data.get("winner_idea") or {}
    # BMC 不再是全場共用一份（實測發現共用會壓低點子多樣性，見
    # stage12/note.md）——每個 idea 現在各自帶自己的 bmc，這裡的「agent
    # 端」指標一律看贏家 idea 自己設計的那一份，跟 real_sources 用同一個
    # 資料來源。
    winner_bmc = winner_idea.get("bmc") or {}
    ideas = run_data.get("ideas") or []
    personas = run_data.get("personas") or []
    dfv_scores = run_data.get("dfv_scores") or []
    idea_diversity = run_data.get("idea_diversity") or {}
    user_evaluation = run_data.get("user_evaluation") or {}
    lens_count = len({s.get("lens_id") for s in dfv_scores if s.get("lens_id")})

    competitive_landscape = run_data.get("competitive_landscape") or []
    used_fallback_competitive_landscape = run_data.get("used_fallback_competitive_landscape", False)
    domains = [p.get("domain") for p in personas if p.get("domain")]

    survey_summary = run_data.get("survey_summary") or {}
    survey_by_feature = survey_summary.get("by_feature") or {}
    winner_survey_stats = survey_by_feature.get(winner_idea.get("id"))

    return {
        "topic": run_data.get("topic"),
        "real_sources": {
            "baseline": len(baseline_proposal.get("sources") or []),
            "agent_total": len(winner_idea.get("sources") or []),
        },
        "bmc_completeness": {
            "baseline": f"{_bmc_filled_count(baseline_proposal.get('bmc') or {})}/9",
            "agent_winner": f"{_bmc_filled_count(winner_bmc)}/9",
        },
        "unit_economics": {
            "baseline": baseline_proposal.get("unit_economics") or _unit_economics(baseline_proposal.get("bmc") or {}),
            "agent": _unit_economics(winner_bmc),
        },
        # Develop 起點的職能來源是 `derive_company_domains()`——從
        # company.md 衍生出這家公司實際具備、彼此明顯不同的職能，不是跟
        # 公司無關的任意領域，也不是 LLM 自己判斷的「互補團隊」（真實跑測
        # 發現後者常常還是同一個大領域裡的不同分工）。這裡直接報告 domain
        # 不重複數，讓「真的彼此不同」這件事可以被檢驗。
        "team_formation": {
            "baseline": "N/A（單一模型單次生成，沒有組隊）",
            "agent": f"從公司能力衍生 {len(personas)} 位（{len(set(domains))} 個不重複職能），各自獨立發想",
        },
        "competitive_landscape_coverage": {
            "baseline": "N/A（沒有市場現況掃描）",
            "agent": (
                "本次未能取得具體競品資訊（評分僅供參考）" if used_fallback_competitive_landscape
                else f"找到 {len(competitive_landscape)} 筆真實競品資訊（web_search，程式碼驗證過來源網址）"
            ),
        },
        "idea_diversity": {
            "baseline": "N/A（單一方案，無法比較多樣性）",
            "agent": idea_diversity.get("avg_distance"),
        },
        "virtual_survey": {
            "baseline": "N/A（沒有量化補充訊號）",
            "agent": (
                f"總模擬樣本數 {survey_summary.get('total_simulated_n', 0)}，"
                f"贏家功能模擬購買意願 {winner_survey_stats['purchase_intent_pct']}%／"
                f"差異化感受 {winner_survey_stats['differentiation_pct']}%"
                if winner_survey_stats else "（本次未產生虛擬問卷資料）"
            ),
        },
        "dfv_scoring": {
            "baseline": "無結構化評分",
            "agent": f"{len(dfv_scores)} 筆（{lens_count} 面向 × {len(ideas)} 個 idea）",
        },
        "cost": {
            "baseline": baseline_metrics.get("cost_usd", 0),
            "agent_total": run_data.get("total_cost_usd", 0),
            "agent_persona_count": len(personas),
        },
        "human_qa_count": len(run_data.get("human_qa_log") or []),
        "evaluator_scores": {
            "agent_avg": user_evaluation.get("agent_avg_score"),
            "baseline_avg": user_evaluation.get("baseline_avg_score"),
            "delta": user_evaluation.get("score_delta"),
            "evaluator_count": len(user_evaluation.get("evaluations") or []),
        },
        "final_verdict": run_data.get("final_verdict", ""),
    }


ROLE_COLOR_PALETTE = ["#5b7cff", "#3ed9c2", "#ffb454", "#ff6b9d", "#9d7bff", "#4ade80"]


def _role_color(role: str, palette_cache: dict) -> str:
    # 跟 stage12/static/index.html 的 roleColor() 同一套邏輯（沒有
    # facilitator/master，DFV 三面向評審用 "dfv:" 前綴）。
    if role.startswith("dfv:"):
        return "#9d7bff"
    if role == "baseline":
        return "#8a93a6"
    if role == "system":
        return "#5a6478"
    if role == "verdict":
        return "#ffb454"
    if role == "problem_analysis":
        return "#f97066"
    if role.startswith("user:"):
        return "#3ed9c2"
    if role not in palette_cache:
        palette_cache[role] = ROLE_COLOR_PALETTE[len(palette_cache) % len(ROLE_COLOR_PALETTE)]
    return palette_cache[role]


def build_replay_html(
    events: list, comparison: dict, title: str,
    *, personas: list | None = None, users: list | None = None,
) -> str:
    palette_cache: dict = {}
    roles = sorted({e.get("role", "system") for e in events})
    legend = [{"role": r, "color": _role_color(r, palette_cache)} for r in roles]
    for e in events:
        e["_color"] = _role_color(e.get("role", "system"), palette_cache)

    events_json = json.dumps(events, ensure_ascii=False)
    comparison_json = json.dumps(comparison, ensure_ascii=False)
    legend_json = json.dumps(legend, ensure_ascii=False)
    personas_json = json.dumps(personas or [], ensure_ascii=False)
    users_json = json.dumps(users or [], ensure_ascii=False)

    return HTML_TEMPLATE.replace("__TITLE__", title) \
        .replace("__EVENTS_JSON__", events_json) \
        .replace("__COMPARISON_JSON__", comparison_json) \
        .replace("__LEGEND_JSON__", legend_json) \
        .replace("__PERSONAS_JSON__", personas_json) \
        .replace("__USERS_JSON__", users_json) \
        .replace("__SHARED_JS__", SHARED_RENDERERS_JS)


HTML_TEMPLATE = r"""<!doctype html>
<html lang="zh-Hant"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__ — Agentic Brainstorming（策略導向市場驗證）會議回放</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    font-family: -apple-system, "PingFang TC", "Microsoft JhengHei", sans-serif;
    margin: 0; background: #0b0f19; color: #e8ecf4; line-height: 1.6;
  }
  header { padding: 32px 24px 20px; text-align: center; background: linear-gradient(135deg,#1b2540,#0b0f19); }
  header h1 { margin: 0 0 6px; font-size: 1.6rem; }
  header p { margin: 0; color: #9aa5c0; }
  .wrap { max-width: 1600px; margin: 0 auto; padding: 24px; }

  .player-layout { display: flex; gap: 20px; align-items: flex-start; }
  .player-main { flex: 1 1 56%; min-width: 0; }
  .player-side { flex: 1 1 44%; min-width: 340px; position: sticky; top: 20px; align-self: flex-start; }
  @media (max-width: 900px) {
    .player-layout { flex-direction: column; }
    .player-side { position: static; width: 100%; }
  }

  .section-title { font-size: 1.15rem; font-weight: 700; margin: 36px 0 14px; color: #8fb0ff; }

  table.compare { width: 100%; border-collapse: collapse; background: #131a2c; border-radius: 12px; overflow: hidden; }
  table.compare th, table.compare td { padding: 12px 16px; text-align: left; border-bottom: 1px solid #1f2942; }
  table.compare th { background: #1b2540; color: #8fb0ff; font-weight: 600; }
  table.compare td.baseline { color: #ff9d9d; }
  table.compare td.agent { color: #7ee8b8; font-weight: 600; }
  table.compare tr:last-child td { border-bottom: none; }

  .stat-row { display: flex; flex-wrap: wrap; gap: 16px; margin: 16px 0; }
  .stat-card { flex: 1 1 150px; background: #131a2c; border-radius: 12px; padding: 16px; text-align: center; }
  .stat-card .num { font-size: 1.6rem; font-weight: 700; color: #8fb0ff; }
  .stat-card .label { font-size: 0.85rem; color: #9aa5c0; margin-top: 4px; }

  .player { background: #131a2c; border-radius: 12px; padding: 20px; }
  .controls { display: flex; align-items: center; gap: 10px; margin-bottom: 16px; flex-wrap: wrap; }
  .controls button {
    background: #5b7cff; color: white; border: none; border-radius: 8px;
    padding: 8px 16px; cursor: pointer; font-size: 0.9rem; font-weight: 600;
  }
  .controls button:hover { background: #4667e0; }
  .controls select { background: #1b2540; color: #e8ecf4; border: 1px solid #2a3557; border-radius: 8px; padding: 8px; }
  .controls .progress-text { color: #9aa5c0; font-size: 0.85rem; margin-left: auto; }

  .cost-meter { height: 10px; background: #1f2942; border-radius: 6px; overflow: hidden; margin-bottom: 20px; }
  .cost-meter-fill { height: 100%; background: linear-gradient(90deg,#5b7cff,#3ed9c2); transition: width 0.3s; }
  .cost-label { font-size: 0.85rem; color: #9aa5c0; margin-bottom: 4px; display: flex; justify-content: space-between; }

  .legend { display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 16px; font-size: 0.82rem; }
  .legend span { display: inline-flex; align-items: center; gap: 6px; color: #9aa5c0; }
  .legend .dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }

  .timeline { max-height: 420px; overflow-y: auto; border: 1px solid #1f2942; border-radius: 8px; }
  .event-row {
    display: flex; gap: 12px; padding: 10px 14px; border-bottom: 1px solid #1a2138;
    cursor: pointer; opacity: 0.45; transition: opacity 0.2s, background 0.2s;
  }
  .event-row:hover { background: #17203a; }
  .event-row.active { opacity: 1; background: #1b2540; }
  .event-row.past { opacity: 0.8; }
  .event-dot { width: 10px; height: 10px; border-radius: 50%; margin-top: 5px; flex-shrink: 0; }
  .event-body { flex: 1; min-width: 0; }
  .event-role { font-weight: 700; font-size: 0.85rem; }
  .event-summary { font-size: 0.88rem; color: #c4cbe0; overflow-wrap: break-word; }
  .event-meta { font-size: 0.75rem; color: #6b7590; margin-top: 2px; }

  .detail-panel { background: #0f1526; border-radius: 8px; padding: 16px; min-height: 90px; max-height: calc(100vh - 40px); overflow-y: auto; }
  .detail-panel .role { font-weight: 700; margin-bottom: 6px; }
  .detail-panel pre { white-space: pre-wrap; word-break: break-word; font-family: inherit; margin: 8px 0 0; font-size: 0.85rem; color: #a8b2cc; }
  .detail-panel .summary-line { color: #c4cbe0; margin-bottom: 4px; }
  .detail-note { color: #8a93a6; font-size: 0.82rem; font-style: italic; margin: 6px 0; }
  .detail-proposal-title { font-size: 1.05rem; font-weight: 700; color: #e8ecf4; margin-top: 8px; }
  .detail-block { margin-top: 14px; }
  .detail-block-title { font-size: 0.78rem; font-weight: 700; color: #8fb0ff; text-transform: uppercase; letter-spacing: 0.04em; margin-bottom: 6px; }
  .detail-list { margin: 0; padding-left: 18px; }
  .detail-list li { margin-bottom: 4px; font-size: 0.88rem; color: #c4cbe0; }
  .kv { display: flex; gap: 8px; font-size: 0.85rem; margin-bottom: 4px; }
  .kv .k { color: #6b7590; flex-shrink: 0; }
  .kv .v { color: #c4cbe0; }
  .bmc-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 8px; }
  .bmc-cell { background: #131a2c; border-radius: 6px; padding: 8px 10px; }
  .bmc-key { font-size: 0.75rem; color: #8fb0ff; font-weight: 700; margin-bottom: 3px; }
  .bmc-val { font-size: 0.82rem; color: #c4cbe0; }
  .quote { background: #131a2c; border-left: 3px solid #2a3557; border-radius: 4px; padding: 8px 12px; margin-bottom: 8px; font-size: 0.88rem; color: #c4cbe0; }
  .quote b { color: #8fb0ff; }

  footer { text-align: center; padding: 30px; color: #5a6478; font-size: 0.82rem; }
</style>
</head>
<body>
<header>
  <h1>__TITLE__</h1>
  <p>Agentic Brainstorming（策略導向市場驗證）— Research→Ideate→Validate→Deliver 會議回放</p>
</header>
<div class="wrap">

  <div class="section-title">為什麼這不是「直接問 ChatGPT」——結構化的差異</div>
  <div id="compareTable"></div>

  <div class="section-title">會議實況回放</div>
  <div class="player">
    <div class="legend" id="legend"></div>
    <div class="cost-label"><span>累計成本</span><span id="costText">$0.0000</span></div>
    <div class="cost-meter"><div class="cost-meter-fill" id="costFill" style="width:0%"></div></div>
    <div class="controls">
      <button id="btnPlay">▶ 播放</button>
      <button id="btnPrev">◀ 上一步</button>
      <button id="btnNext">下一步 ▶</button>
      <select id="speedSelect">
        <option value="1500">慢速</option>
        <option value="700" selected>正常</option>
        <option value="250">快速</option>
      </select>
      <span class="progress-text" id="progressText">0 / 0</span>
    </div>
    <div class="player-layout">
      <div class="player-main">
        <div class="timeline" id="timeline"></div>
      </div>
      <div class="player-side">
        <div class="detail-panel" id="detailPanel">
          <div class="role">點一則事件，或按播放開始</div>
        </div>
      </div>
    </div>
  </div>

</div>
<footer>零依賴單檔 HTML，事件資料已內嵌，離線亦可開啟。</footer>

<script>
const EVENTS = __EVENTS_JSON__;
const COMPARISON = __COMPARISON_JSON__;
const LEGEND = __LEGEND_JSON__;
const PERSONAS = __PERSONAS_JSON__;
const USERS = __USERS_JSON__;

function fmtUnitEconomics(ue) {
  if (!ue) return 'N/A';
  const margin = Number(ue.monthly_margin_twd || 0);
  return `淨利 ${margin >= 0 ? '+' : ''}${margin.toFixed(0)} 元/月（${ue.is_viable ? '可行' : '不可行'}）`;
}

function renderCompareTable() {
  const c = COMPARISON;
  const rows = [
    ["真實搜尋依據", `${c.real_sources.baseline} 筆（模型可能編造，無法驗證）`, `${c.real_sources.agent_total} 筆（贏家 idea，來自真實搜尋結果）`],
    ["BMC 完整度", c.bmc_completeness.baseline, c.bmc_completeness.agent_winner + "（贏家 idea 自己設計，通過結構驗證）"],
    ["單位經濟（收益流－成本結構）", fmtUnitEconomics(c.unit_economics && c.unit_economics.baseline), fmtUnitEconomics(c.unit_economics && c.unit_economics.agent)],
    ["動態組隊（扣著公司能力衍生職能）", c.team_formation.baseline, c.team_formation.agent],
    ["競品掃描覆蓋度（取代 Discover/Define）", c.competitive_landscape_coverage.baseline, c.competitive_landscape_coverage.agent],
    ["虛擬問卷（模擬訊號，非真人樣本，僅供參考）", c.virtual_survey.baseline, c.virtual_survey.agent],
    ["idea 多樣性（發想階段兩兩平均距離，0-1）", c.idea_diversity.baseline, c.idea_diversity.agent],
    ["DFV 結構化評分覆蓋", c.dfv_scoring.baseline, c.dfv_scoring.agent],
    ["成本", `$${Number(c.cost.baseline).toFixed(4)}（一次呼叫）`, `$${Number(c.cost.agent_total).toFixed(4)}（完整流程：${c.cost.agent_persona_count} 位參與者 + 訪談 + DFV 評分 + 原型）`],
  ];
  let html = '<table class="compare"><tr><th>維度</th><th>直接問 LLM（Baseline）</th><th>Agent 會議流程</th></tr>';
  for (const [dim, b, a] of rows) {
    html += `<tr><td>${dim}</td><td class="baseline">${b}</td><td class="agent">${a}</td></tr>`;
  }
  html += '</table>';

  const ev = c.evaluator_scores || {};
  html += `<div class="stat-row">
    <div class="stat-card"><div class="num">${ev.evaluator_count ?? '-'}</div><div class="label">最終評估者人數（不重複訪談對象）</div></div>
    <div class="stat-card"><div class="num">${ev.agent_avg ?? '-'}</div><div class="label">Agent 方案平均分</div></div>
    <div class="stat-card"><div class="num">${ev.baseline_avg ?? '-'}</div><div class="label">Baseline 平均分</div></div>
    <div class="stat-card"><div class="num">${ev.delta != null ? (ev.delta > 0 ? '+' : '') + ev.delta : '-'}</div><div class="label">差距</div></div>
    <div class="stat-card"><div class="num">${c.human_qa_count}</div><div class="label">人類提問</div></div>
  </div>`;
  if (c.final_verdict) {
    html += `<div class="detail-block" style="margin-top:16px;"><div class="detail-block-title">AI 對照評語（agent 流程 vs baseline）</div><p>${esc(c.final_verdict)}</p></div>`;
  }
  document.getElementById('compareTable').innerHTML = html;
}

function renderLegend() {
  document.getElementById('legend').innerHTML = LEGEND.map(
    l => `<span><span class="dot" style="background:${l.color}"></span>${l.role}</span>`
  ).join('');
}

let cursor = -1;
let playing = false;
let timer = null;
let cumulativeCost = 0;

function renderTimeline() {
  const el = document.getElementById('timeline');
  el.innerHTML = EVENTS.map((e, i) => `
    <div class="event-row" data-idx="${i}" id="ev-${i}">
      <div class="event-dot" style="background:${e._color}"></div>
      <div class="event-body">
        <div class="event-role" style="color:${e._color}">${e.role} · ${e.action}</div>
        <div class="event-summary">${escapeHtml(e.summary || '')}</div>
        <div class="event-meta">${(e.ts || '').replace('T',' ').slice(0,19)} UTC ${e.cost_usd ? '· $' + e.cost_usd.toFixed(4) : ''}</div>
      </div>
    </div>`).join('');
  el.querySelectorAll('.event-row').forEach(row => {
    row.addEventListener('click', () => { pause(); goTo(parseInt(row.dataset.idx)); });
  });
}

__SHARED_JS__

const totalCost = sumDisplayCost(EVENTS) || 1;

function goTo(idx) {
  if (idx < 0 || idx >= EVENTS.length) return;
  cursor = idx;
  cumulativeCost = sumDisplayCost(EVENTS.slice(0, idx + 1));
  document.querySelectorAll('.event-row').forEach((row, i) => {
    row.classList.toggle('active', i === idx);
    row.classList.toggle('past', i < idx);
  });
  const activeRow = document.getElementById('ev-' + idx);
  if (activeRow) activeRow.scrollIntoView({block: 'nearest'});

  const e = EVENTS[idx];
  document.getElementById('detailPanel').innerHTML = `
    <div class="role" style="color:${e._color}">${e.role} — ${e.node} / ${e.action}</div>
    <div class="summary-line">${escapeHtml(e.summary || '')}</div>
    ${renderDetail(e)}`;

  document.getElementById('progressText').textContent = `${idx + 1} / ${EVENTS.length}`;
  document.getElementById('costText').textContent = '$' + cumulativeCost.toFixed(4);
  document.getElementById('costFill').style.width = Math.min(100, (cumulativeCost / totalCost) * 100) + '%';
}

function play() {
  playing = true;
  document.getElementById('btnPlay').textContent = '⏸ 暫停';
  const speed = parseInt(document.getElementById('speedSelect').value);
  timer = setInterval(() => {
    if (cursor >= EVENTS.length - 1) { pause(); return; }
    goTo(cursor + 1);
  }, speed);
}
function pause() {
  playing = false;
  document.getElementById('btnPlay').textContent = '▶ 播放';
  if (timer) clearInterval(timer);
}

document.getElementById('btnPlay').addEventListener('click', () => playing ? pause() : play());
document.getElementById('btnPrev').addEventListener('click', () => { pause(); goTo(cursor - 1); });
document.getElementById('btnNext').addEventListener('click', () => { pause(); goTo(cursor + 1); });
document.getElementById('speedSelect').addEventListener('change', () => { if (playing) { pause(); play(); } });

renderCompareTable();
renderLegend();
renderTimeline();
goTo(0);
</script>
</body></html>
"""


def main() -> None:
    if len(sys.argv) != 4:
        print("用法：python build_replay.py <events.jsonl> <stage15-run.json> <輸出.html>")
        sys.exit(1)
    events_path, run_json_path, out_path = (Path(p) for p in sys.argv[1:4])

    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    run_data = json.loads(run_json_path.read_text(encoding="utf-8"))
    comparison = compute_comparison(run_data)
    title = f"{run_data.get('topic', '')}".strip() or "Agentic Brainstorming"

    # 概念測試訪談對象是同一組固定小組，完整內嵌在每筆 concept_test_results
    # 的 interviewee 欄位裡（見 graph.py concept_test_one_person() 的
    # 說明），不像 stage14-signals 那樣有一份全域的
    # candidate_jobs.interview_pool 可以直接攤平——這裡去重後重建成
    # USERS 清單，讓 findUser() 對其他事件（例如最終評估者）也能正確
    # 解析成對得上的姓名/情境。
    seen_ids = set()
    interviewees = []
    for res in (run_data.get("concept_test_results") or []):
        person = res.get("interviewee")
        if person and person.get("id") not in seen_ids:
            seen_ids.add(person.get("id"))
            interviewees.append(person)
    html = build_replay_html(
        events, comparison, title,
        personas=run_data.get("personas"), users=(interviewees + (run_data.get("evaluators") or [])),
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")

    display_total = sum_display_cost(events)
    true_total = run_data.get("total_cost_usd", 0.0)
    if abs(display_total - true_total) > 0.001:
        print(f"⚠️  回放器成本加總（${display_total:.4f}）跟 pipeline 真實總成本"
              f"（${true_total:.4f}）對不上，events.jsonl 可能有新的多次 emit 節點，"
              f"檢查 COST_SNAPSHOT_ACTIONS 是不是要更新。")
    print(f"寫出 {out_path}（{len(events)} 筆事件，{len(html)} bytes，成本 ${display_total:.4f}）")


if __name__ == "__main__":
    main()
