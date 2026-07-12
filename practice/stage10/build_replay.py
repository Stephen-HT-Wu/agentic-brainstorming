"""
Stage 10 工具二：把 events.jsonl + stage9 的 run JSON 轉成一個零依賴、
雙擊即開的 HTML 回放器——這是 PLAN.md 說的「demo 敘事」載體。

零依賴的關鍵決定：事件資料直接內嵌進 <script> 標籤裡的 JS 變數，不是用
`fetch()` 讀外部檔案——瀏覽器在 `file://` 協定下，大多數瀏覽器的同源
政策會擋掉本地檔案的 fetch，內嵌資料才能保證「雙擊就開，不用架伺服器」。

同一支腳本也把 PLAN.md 點名的六個對比維度（真實依據引用數、BMC 完整度、
點子多樣性、被批判後改良次數、跨場記憶引用、成本）從 stage9 的 run JSON
算出來，做成 HTML 最上方的『Executive Summary』對比表——這是給不懂技術
的人看的第一眼，事件回放放在對比表下面，給想深入看『怎麼做到的』的人。

用法：
    python build_replay.py <events.jsonl 路徑> <stage9-run-*.json 路徑> <輸出 .html 路徑>

跟 stage11 共用的事件細節渲染邏輯（esc/renderProposal/renderExtraGeneric...）
抽到 shared_renderers.js，這裡在產生 HTML 的當下把那份檔案的內容讀進來、
內嵌進 <script> 區塊——輸出的 replay.html 還是完全自足、零依賴（雙擊即開
的承諾不變），只是原始碼不再是兩份手動同步的複製。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

SHARED_RENDERERS_JS = (Path(__file__).resolve().parent / "shared_renderers.js").read_text(encoding="utf-8")


def _bmc_filled_count(bmc: dict) -> int:
    return sum(1 for v in (bmc or {}).values() if isinstance(v, str) and v.strip())


# 真實資料踩到的坑：stage3 起，`conduct_interviews` 每一輪訪談都會呼叫一次
# `emit_event("interview_turn", ...)`，而 `emit_event` 算 cost_usd 的方式是
# 「這次節點呼叫（invocation）目前為止的累計花費」——這對『一次呼叫只 emit
# 一次事件』的節點沒問題，但 `conduct_interviews`／`generate_prototype_and_test`
# 這種一次呼叫裡多次 emit 的節點，中間每筆事件的 cost_usd 其實是累計值，
# 不是這筆事件單獨的花費。直接把 events.jsonl 全部事件的 cost_usd 加總會
# 嚴重高估：實測一場真實會議原始加總 $0.8741，但 pipeline 自己算出的真實
# 總成本是 $0.6680。這幾個 action 只是『過程中的快照』，真正該算進總額的
# 是同一次呼叫最後那筆『總結』事件（conduct_interviews／refine_after_test）。
COST_SNAPSHOT_ACTIONS = {"interview_turn", "generate_prototype", "test_prototype"}


def sum_display_cost(events: list) -> float:
    """回放器成本表用這個算總額，不能直接對 events.jsonl 的 cost_usd 加總。"""
    return sum(e.get("cost_usd", 0.0) for e in events if e.get("action") not in COST_SNAPSHOT_ACTIONS)


def compute_comparison(run_data: dict) -> dict:
    """算出 PLAN.md 點名的六個維度，agent 端一律取 idea_pool_versions
    （每位 persona 同儕互評後的最終版）算平均，不是只挑最好的那份。"""
    idea_pool_versions = run_data.get("idea_pool_versions") or []
    baseline_proposal = run_data.get("baseline", {}).get("proposal") or {}
    baseline_metrics = run_data.get("baseline", {}).get("metrics") or {}

    agent_proposals = [v["proposal_after"] for v in idea_pool_versions if v.get("proposal_after")]
    n = len(agent_proposals) or 1

    agent_source_counts = [len(p.get("sources") or []) for p in agent_proposals]
    agent_bmc_filled = [_bmc_filled_count(p.get("bmc") or {}) for p in agent_proposals]
    revision_count = len(idea_pool_versions) + len(run_data.get("prototypes") or [])
    memory_refs_total = sum(len(p.get("memory_refs") or []) for p in agent_proposals)

    return {
        "topic": run_data.get("topic"),
        "real_sources": {
            "baseline": len(baseline_proposal.get("sources") or []),
            "baseline_verified": 0,  # baseline 沒有真實搜尋池，來源無法驗證是否真實存在
            "agent_avg": round(sum(agent_source_counts) / n, 2),
            "agent_total": sum(agent_source_counts),
        },
        "bmc_completeness": {
            "baseline": f"{_bmc_filled_count(baseline_proposal.get('bmc') or {})}/9",
            "agent_all": f"{min(agent_bmc_filled) if agent_bmc_filled else 0}/9 ~ {max(agent_bmc_filled) if agent_bmc_filled else 0}/9",
        },
        "diversity": {
            "baseline": "N/A（只有一個答案，無法量測多樣性）",
            "agent": run_data.get("diversity_after_review", {}).get("avg_distance"),
        },
        "revision_count": {
            "baseline": 0,
            "agent": revision_count,
        },
        "cross_round_memory": {
            "baseline": 0,
            "agent_recall_hits": run_data.get("recall_hits_total", 0),
            "agent_actually_cited": memory_refs_total,
        },
        "cost": {
            "baseline": baseline_metrics.get("cost_usd", 0),
            "agent_total": run_data.get("total_cost_usd", 0),
            "agent_persona_count": run_data.get("persona_count"),
        },
        "master_critiques_count": len(run_data.get("master_critiques") or []),
        "three_lens_checks_count": len(run_data.get("three_lens_checks") or []),
        "human_qa_count": len(run_data.get("human_qa_log") or []),
        "facilitator_rounds": len(run_data.get("facilitator_log") or []),
        # 使用者要求最後有一段 AI 直接比較兩邊優劣的評語，不是只有結構性
        # 數字對照表——放進對照表旁邊，不用點進某個事件才看得到。
        "final_verdict": run_data.get("final_verdict", ""),
    }


def _persona_name_from_role(role: str) -> str | None:
    if isinstance(role, str) and role.startswith("persona:"):
        return role[len("persona:"):]
    return None


def _clean_prototype_html_path(p: dict) -> dict:
    """run_data.prototypes[].html_path 記的是產生當下的本機絕對路徑
    （demo_workspace/outputs/prototypes/...）——不能直接信任這個欄位，因為它跟
    events.jsonl 裡的路徑是同一次跑測產生的，只有事後手動 sed 過的
    demo/sample-run/ 副本才乾淨。改成依 persona_id 直接算出跟 demo/sample-run/
    實際檔名（`prototype-<id>.html`）一致的相對路徑，這樣不管 build_replay.py
    是對著哪一份原始 run 資料跑，輸出永遠不會外洩本機路徑。"""
    clean = dict(p)
    persona_id = p.get("persona_id")
    if persona_id:
        clean["html_path"] = f"prototype-{persona_id}.html"
    else:
        clean["html_path"] = Path(p.get("html_path", "")).name
    return clean


def _sanitize_html_path(path: str, prototype_by_name: dict, persona_name: str | None) -> str:
    if persona_name and persona_name in prototype_by_name:
        clean = prototype_by_name[persona_name].get("html_path")
        if clean:
            return clean
    return Path(path).name


def _attach_details(events: list, run_data: dict) -> None:
    """把 present／baseline／原型事件跟 run_data 裡的完整資料串起來，回放器點擊事件
    時能看到完整提案（BMC 九宮格/真實來源/POV-HMW）跟原型內容，不是只有事件當下記錄的
    摘要片段——這是使用者要求「點每一步都能看到做了什麼功課、形成的意見是什麼」的關鍵。"""
    proposal_by_name = {v.get("persona_name"): v for v in (run_data.get("idea_pool_versions") or [])}
    prototype_by_name = {
        p.get("persona_name"): _clean_prototype_html_path(p)
        for p in (run_data.get("prototypes") or [])
    }
    baseline_proposal = (run_data.get("baseline") or {}).get("proposal")

    for e in events:
        action = e.get("action")
        name = _persona_name_from_role(e.get("role", ""))

        if action == "present" and name in proposal_by_name:
            v = proposal_by_name[name]
            e["detail"] = {
                "kind": "proposal",
                "note": f"發表當下標題為《{v.get('before_title')}》；以下顯示的是經過同儕互評修正後的最終版本。",
                "proposal": v.get("proposal_after"),
            }
        elif action == "baseline" and baseline_proposal:
            e["detail"] = {
                "kind": "proposal",
                "note": "單次 LLM 呼叫直接生成，未經搜尋、互評或測試。",
                "proposal": baseline_proposal,
            }
        elif action in ("generate_prototype", "refine_after_test") and name in prototype_by_name:
            e["detail"] = {"kind": "prototype", "prototype": prototype_by_name[name]}

        extra = e.get("extra")
        if isinstance(extra, dict) and extra.get("html_path"):
            extra["html_path"] = _sanitize_html_path(extra["html_path"], prototype_by_name, name)


ROLE_COLOR_PALETTE = [
    "#5b7cff", "#3ed9c2", "#ffb454", "#ff6b9d", "#9d7bff", "#4ade80",
]


def _role_color(role: str, palette_cache: dict) -> str:
    if role in ("facilitator",):
        return "#ffb454"
    if role.startswith("master:"):
        return "#9d7bff"
    if role == "baseline":
        return "#8a93a6"
    if role == "system":
        return "#5a6478"
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
    # 使用者要求：互評/評分/收斂等事件裡到處混著英文 persona_id（例如
    # target_persona_id="alex"）跟中文姓名（例如 role="persona:陳建宏"），
    # 名字時中時英很難認人；還要看得到被訪談的模擬用戶「人物設定」（不是
    # 只有名字），才知道他們的反應是基於什麼理由——這兩者都要在畫面上
    # 解析 id，所以把完整的 personas／users 清單（含 role/background/focus
    # 跟 age/context/pain_points/tone）內嵌進頁面，交給 JS 端統一解析、
    # 顯示。舊的 run JSON（這個功能上線前跑的）沒有這兩個欄位，
    # `personas`/`users` 參數預設 None、空清單一樣能跑，只是解析不到就
    # 原樣顯示 id（見 JS 端 personaName() 的 fallback）。
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
<title>__TITLE__ — Agentic Brainstorming 會議回放</title>
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

  /* 使用者要求：點事件不用往下滑才看得到內容——左邊事件列表，右邊
     sticky 詳情欄，點了立刻在旁邊看到，不用捲頁面。窄螢幕（例如手機
     橫向）疊回單欄，sticky 在小螢幕上體驗不好。 */
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
  <p>Agentic Brainstorming — 多 agent 腦力激盪會議回放</p>
</header>
<div class="wrap">

  <div class="section-title">為什麼這不是「直接問 ChatGPT」——六個可量化的差異</div>
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

function renderCompareTable() {
  const c = COMPARISON;
  const rows = [
    ["真實搜尋依據", `${c.real_sources.baseline} 筆（模型可能編造，無法驗證）`, `平均 ${c.real_sources.agent_avg} 筆／份，來自真實搜尋結果`],
    ["BMC 完整度", c.bmc_completeness.baseline, c.bmc_completeness.agent_all + "（每份皆通過結構驗證）"],
    ["點子多樣性", c.diversity.baseline, `兩兩平均距離 ${c.diversity.agent}（真的是 ${c.cost.agent_persona_count} 份不同觀點）`],
    ["被批判／測試後改良次數", `${c.revision_count.baseline}（從未被挑戰）`, `${c.revision_count.agent} 次（同儕互評 + 用戶測試雙重驅動）`],
    ["跨場記憶引用", `${c.cross_round_memory.baseline}（無記憶，每次都是新對話）`, `${c.cross_round_memory.agent_actually_cited} 筆真實引用（命中 ${c.cross_round_memory.agent_recall_hits} 次）`],
    ["成本", `$${Number(c.cost.baseline).toFixed(4)}（一次呼叫）`, `$${Number(c.cost.agent_total).toFixed(4)}（完整流程：${c.cost.agent_persona_count} 位 persona + 大師 + 訪談 + 測試）`],
  ];
  let html = '<table class="compare"><tr><th>維度</th><th>直接問 LLM（Baseline）</th><th>Agent 會議流程</th></tr>';
  for (const [dim, b, a] of rows) {
    html += `<tr><td>${dim}</td><td class="baseline">${b}</td><td class="agent">${a}</td></tr>`;
  }
  html += '</table>';
  html += `<div class="stat-row">
    <div class="stat-card"><div class="num">${c.master_critiques_count}</div><div class="label">大師點評</div></div>
    <div class="stat-card"><div class="num">${c.three_lens_checks_count}</div><div class="label">三鏡檢核筆數</div></div>
    <div class="stat-card"><div class="num">${c.human_qa_count}</div><div class="label">人類提問</div></div>
    <div class="stat-card"><div class="num">${c.facilitator_rounds}</div><div class="label">Facilitator 決策輪數</div></div>
  </div>`;
  if (c.final_verdict) {
    // 使用者要求最後有一段 AI 直接比較兩邊優劣的評語，不是只有數字——
    // 放在對照表正下方，不用點進某個事件才看得到。
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
        print("用法：python build_replay.py <events.jsonl> <stage-run.json> <輸出.html>")
        sys.exit(1)
    events_path, run_json_path, out_path = (Path(p) for p in sys.argv[1:4])

    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    run_data = json.loads(run_json_path.read_text(encoding="utf-8"))
    _attach_details(events, run_data)
    comparison = compute_comparison(run_data)
    title = f"{run_data.get('topic', '')}".strip() or "Agentic Brainstorming"

    html = build_replay_html(
        events, comparison, title,
        personas=run_data.get("personas"), users=run_data.get("users"),
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
