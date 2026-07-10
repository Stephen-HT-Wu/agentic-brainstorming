"""
階段 1：單一 persona 做功課子圖 + baseline

目標：一個 persona 完整跑
    collect(web search) → dedup(embedding) → 彙整 → 撰寫提案(含 BMC) → 自我修正×3
並量測每輪 delta；同場再跑一次「直接問 LLM」baseline 存檔備比；
每個節點寫入 outputs/events.jsonl。

對應 PLAN.md「階段 1」驗收：
- 提案引用真實搜尋結果
- BMC 九格齊全（結構不變量）
- 三輪 delta 數字能回答「第三輪值不值得」
- baseline 產出與指標已存檔

執行前在 practice/.env 設定 ANTHROPIC_API_KEY（見 .env.example）。
"""
from __future__ import annotations

import hashlib
import json
import math
import operator
import os
import re
import sys
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, List, Literal, Optional, TypedDict

import anthropic
import yaml
from langchain_core._api.deprecation import LangChainPendingDeprecationWarning
from langgraph.graph import END, START, StateGraph

warnings.filterwarnings("ignore", category=LangChainPendingDeprecationWarning)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _common import (  # noqa: E402
    cost_of,
    current_node,
    instrument,
    node_times,
    reset_metrics,
    set_current_node,
)

# ---------------------------------------------------------------------------
# 路徑 / 環境
# ---------------------------------------------------------------------------

PRACTICE_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PRACTICE_DIR / "outputs"
EVENTS_PATH = OUTPUT_DIR / "events.jsonl"

_env_file = PRACTICE_DIR / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _key, _, _value = _line.partition("=")
            os.environ.setdefault(_key.strip(), _value.strip())

CHEAP_MODEL = "claude-haiku-4-5-20251001"  # persona 做功課／提案／修正
client = anthropic.Anthropic()

DEDUP_SIMILARITY_THRESHOLD = 0.80
EMBED_DIM = 256
REFINE_ROUNDS = 3

BMC_KEYS = [
    "客群",
    "價值主張",
    "通路",
    "顧客關係",
    "收益流",
    "關鍵資源",
    "關鍵活動",
    "關鍵夥伴",
    "成本結構",
]

usage_log: list = []
_event_role = "system"


# ---------------------------------------------------------------------------
# 設定檔雙軌載入
# ---------------------------------------------------------------------------

def _load_text_dual(real_name: str, example_name: str) -> str:
    real = PRACTICE_DIR / real_name
    example = PRACTICE_DIR / example_name
    path = real if real.exists() else example
    if not path.exists():
        raise FileNotFoundError(f"找不到 {real_name} 或 {example_name}")
    return path.read_text(encoding="utf-8")


def load_persona() -> dict:
    """真實 personas.yaml 優先，否則 personas.example.yaml；stage 1 只取第一人。"""
    real = PRACTICE_DIR / "personas.yaml"
    example = PRACTICE_DIR / "personas.example.yaml"
    path = real if real.exists() else example
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    personas = data["personas"]
    if not personas:
        raise ValueError(f"{path} 沒有 personas")
    return personas[0]


def load_company() -> str:
    return _load_text_dual("company.md", "company.example.md")


# ---------------------------------------------------------------------------
# LLM / JSON / events
# ---------------------------------------------------------------------------

def call_llm(model: str, system: str, user: str, max_tokens: int = 2500) -> str:
    """LLM 呼叫入口；若 stop_reason=max_tokens 則加大上限重試一次。"""

    def _create(tokens: int):
        return client.messages.create(
            model=model,
            max_tokens=tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )

    for attempt in range(2):
        tokens = max_tokens if attempt == 0 else min(max_tokens * 2, 8000)
        response = _create(tokens)
        usage_log.append({
            "node": current_node(),
            "model": model,
            "input": response.usage.input_tokens,
            "output": response.usage.output_tokens,
        })
        stop_reason = getattr(response, "stop_reason", None)
        if stop_reason == "max_tokens" and attempt == 0:
            print(f"  [call_llm] 截斷（max_tokens={tokens}），加大重試…")
            continue
        text_parts = [block.text for block in response.content if block.type == "text"]
        if not text_parts:
            raise ValueError(f"模型回覆中沒有 text block：{response.content!r}")
        return "".join(text_parts)
    raise ValueError("模型回覆仍被截斷（已重試）。")


def extract_json(text: str) -> Any:
    """從模型回覆抽出 JSON（允許包在 ```json 區塊裡）。"""
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    raw = fence.group(1).strip() if fence else text.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start >= 0 and end > start:
            return json.loads(raw[start : end + 1])
        raise


def repair_json_text(bad_text: str) -> str:
    """請模型把壞掉的 JSON 修成合法物件（只輸出 JSON）。"""
    return call_llm(
        CHEAP_MODEL,
        "你是 JSON 修復器。只輸出一個合法 JSON object，不要 markdown、不要解說。",
        f"請修復以下內容為合法 JSON object：\n\n{bad_text[:6000]}",
        max_tokens=3000,
    )


def emit_event(
    action: str,
    summary: str,
    *,
    role: Optional[str] = None,
    extra: Optional[dict] = None,
) -> None:
    """每個節點寫一筆結構化事件到 outputs/events.jsonl（stage 1 起必寫）。"""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    node = current_node()
    calls = [e for e in usage_log if e["node"] == node]
    # 只計「自從上次 emit 後」不好追，改記該節點累計；同一節點多次 emit 時用 delta 近似
    tokens_in = sum(e["input"] for e in calls)
    tokens_out = sum(e["output"] for e in calls)
    cost = sum(cost_of(e) for e in calls)
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "role": role or _event_role,
        "action": action,
        "node": node,
        "summary": summary,
        "tokens": {"input": tokens_in, "output": tokens_out},
        "cost_usd": round(cost, 6),
    }
    if extra:
        record["extra"] = extra
    with EVENTS_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Embedding（純 Python feature hashing，免 API；之後 stage 7 再換 Chroma）
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> List[str]:
    return re.findall(r"[a-z0-9\u4e00-\u9fff]+", text.lower())


def embed_text(text: str, dim: int = EMBED_DIM) -> List[float]:
    vec = [0.0] * dim
    for token in _tokenize(text):
        digest = hashlib.md5(token.encode("utf-8")).hexdigest()
        idx = int(digest, 16) % dim
        vec[idx] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def cosine_similarity(a: List[float], b: List[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def embedding_distance(a: str, b: str) -> float:
    """1 - cosine；越大代表兩版文字差越多。"""
    return 1.0 - cosine_similarity(embed_text(a), embed_text(b))


def dedup_by_embedding(items: List[dict], threshold: float = DEDUP_SIMILARITY_THRESHOLD) -> List[dict]:
    kept: List[dict] = []
    kept_vecs: List[List[float]] = []
    for item in items:
        text = f"{item.get('title', '')} {item.get('snippet', '')}"
        vec = embed_text(text)
        if any(cosine_similarity(vec, prev) >= threshold for prev in kept_vecs):
            continue
        kept.append(item)
        kept_vecs.append(vec)
    return kept


# ---------------------------------------------------------------------------
# Web search tool
# ---------------------------------------------------------------------------

def web_search(query: str, max_results: int = 5) -> List[dict]:
    """優先 Tavily（若有 key），否則 DuckDuckGo（免 key）。"""
    tavily_key = os.environ.get("TAVILY_API_KEY", "").strip()
    if tavily_key:
        return _search_tavily(query, tavily_key, max_results)
    return _search_ddg(query, max_results)


def _search_ddg(query: str, max_results: int) -> List[dict]:
    from ddgs import DDGS

    out: List[dict] = []
    with DDGS() as ddgs:
        for row in ddgs.text(query, max_results=max_results):
            out.append({
                "title": row.get("title") or "",
                "url": row.get("href") or row.get("link") or "",
                "snippet": row.get("body") or row.get("snippet") or "",
                "query": query,
            })
    return out


def _search_tavily(query: str, api_key: str, max_results: int) -> List[dict]:
    import urllib.request

    payload = json.dumps({
        "api_key": api_key,
        "query": query,
        "max_results": max_results,
        "search_depth": "basic",
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.tavily.com/search",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    out = []
    for row in data.get("results", []):
        out.append({
            "title": row.get("title") or "",
            "url": row.get("url") or "",
            "snippet": row.get("content") or "",
            "query": query,
        })
    return out


# ---------------------------------------------------------------------------
# BMC / 提案協議
# ---------------------------------------------------------------------------

def proposal_text_for_embed(proposal: dict) -> str:
    bmc = proposal.get("bmc") or {}
    parts = [
        proposal.get("title", ""),
        proposal.get("summary", ""),
        json.dumps(bmc, ensure_ascii=False, sort_keys=True),
    ]
    return "\n".join(parts)


def assert_bmc_complete(proposal: dict) -> List[str]:
    """回傳缺漏的 BMC 欄位；空 list = 九格齊全。"""
    bmc = proposal.get("bmc") or {}
    missing = []
    for key in BMC_KEYS:
        val = bmc.get(key)
        if val is None or (isinstance(val, str) and not val.strip()):
            missing.append(key)
    return missing


def count_real_citations(proposal: dict, research_items: List[dict]) -> int:
    """提案 sources 的 url 有出現在真實搜尋結果裡才算『真實依據』。"""
    known = {item.get("url", "").rstrip("/") for item in research_items if item.get("url")}
    n = 0
    for src in proposal.get("sources") or []:
        url = (src.get("url") or "").rstrip("/")
        if url and url in known:
            n += 1
    return n


def metrics_of(proposal: dict, research_items: List[dict], cost: float) -> dict:
    missing = assert_bmc_complete(proposal)
    return {
        "bmc_complete": len(missing) == 0,
        "bmc_missing": missing,
        "bmc_filled": 9 - len(missing),
        "real_citations": count_real_citations(proposal, research_items),
        "source_count": len(proposal.get("sources") or []),
        "self_score": proposal.get("self_score"),
        "cost_usd": round(cost, 6),
    }


# ---------------------------------------------------------------------------
# 做功課子圖 state
# ---------------------------------------------------------------------------

class HomeworkState(TypedDict):
    topic: str
    persona: dict
    company: str
    raw_results: List[dict]
    research_items: List[dict]
    research_brief: str
    proposal: dict
    proposal_versions: Annotated[List[dict], operator.add]
    refine_deltas: Annotated[List[dict], operator.add]
    refine_round: int


def _persona_label(persona: dict) -> str:
    return f"persona:{persona.get('name', persona.get('id', '?'))}"


def collect(state: HomeworkState) -> dict:
    """依 persona 關注面向組查詢，呼叫 web search（agent tool use）。"""
    global _event_role
    persona = state["persona"]
    _event_role = _persona_label(persona)
    topic = state["topic"]
    focus = persona.get("focus") or ["市場現況"]
    queries = [
        f"{topic} {focus[0]}",
        f"{topic} 短影音 互動率 策略",
        f"新聞短影音 競品 留存 案例",
    ]
    raw: List[dict] = []
    for q in queries:
        try:
            hits = web_search(q, max_results=4)
            raw.extend(hits)
            print(f"  [collect] query={q!r} → {len(hits)} 筆")
        except Exception as exc:  # noqa: BLE001 — 搜尋失敗不中斷整場
            print(f"  [collect] query={q!r} 失敗：{exc}")
    emit_event(
        "collect",
        f"搜尋 {len(queries)} 組查詢，共 {len(raw)} 筆原始結果",
        extra={"queries": queries, "n_results": len(raw)},
    )
    return {"raw_results": raw}


def dedup(state: HomeworkState) -> dict:
    raw = state["raw_results"]
    items = dedup_by_embedding(raw)
    print(f"  [dedup] {len(raw)} → {len(items)}（門檻 {DEDUP_SIMILARITY_THRESHOLD}）")
    emit_event("dedup", f"embedding 去重 {len(raw)} → {len(items)}")
    return {"research_items": items}


def synthesize(state: HomeworkState) -> dict:
    """把去重後素材彙整成研究 brief（寫入 state，等同個人記憶草稿）。"""
    persona = state["persona"]
    items = state["research_items"]
    bullet = "\n".join(
        f"- {it.get('title')} | {it.get('url')}\n  {it.get('snippet', '')[:240]}"
        for it in items
    ) or "（無搜尋結果）"
    system = (
        f"你是 {persona['name']}（{persona.get('role', '')}）。"
        f"背景：{persona.get('background', '')}。"
        f"關注：{', '.join(persona.get('focus') or [])}。"
        f"發言風格：{persona.get('style', '')}。"
        "請根據搜尋素材寫一份精簡研究彙整，必須標注可追溯的 URL。"
        "只輸出正文，不要 JSON。"
    )
    user = (
        f"會議主題：{state['topic']}\n\n"
        f"自家公司定位：\n{state['company']}\n\n"
        f"搜尋素材：\n{bullet}\n\n"
        "請輸出：1) 市場／競品觀察 2) 對自家有利的切入點 3) 風險與未知。"
    )
    brief = call_llm(CHEAP_MODEL, system, user, max_tokens=1200)
    emit_event("synthesize", f"彙整 brief {len(brief)} 字，素材 {len(items)} 筆")
    return {"research_brief": brief}


_PROPOSAL_SCHEMA_HINT = f"""
請只輸出一個 JSON 物件（不要 markdown 圍欄），欄位：
- title: string（<=40字）
- summary: string（2-3句，<=120字）
- sources: [{{"title","url","how_used"}}] 最多 3 筆 — url 必須來自提供的搜尋素材
- bmc: 物件，鍵必須恰好包含且僅包含這九個：
  {json.dumps(BMC_KEYS, ensure_ascii=False)}
  每格一句話，<=40字
- self_score: number 1-10
- score_reason: string（<=40字）
務必輸出精簡合法 JSON，避免長文導致截斷。
"""


def _parse_proposal(text: str) -> dict:
    try:
        data = extract_json(text)
    except (json.JSONDecodeError, ValueError):
        try:
            data = extract_json(repair_json_text(text))
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"提案 JSON 解析失敗：{exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("提案不是 JSON object")
    data.setdefault("sources", [])
    data.setdefault("bmc", {})
    data.setdefault("self_score", 0)
    try:
        data["self_score"] = float(data["self_score"])
    except (TypeError, ValueError):
        data["self_score"] = 0.0
    return data


def _request_proposal(system: str, user: str) -> dict:
    """呼叫模型拿提案；失敗則用更短的『只輸出 JSON』指令再要一次。"""
    raw = call_llm(CHEAP_MODEL, system, user, max_tokens=2500)
    try:
        return _parse_proposal(raw)
    except ValueError:
        print("  [proposal] JSON 失敗，改以精簡指令重試…")
        compact_system = (
            "只輸出精簡合法 JSON object，不要 markdown。"
            + _PROPOSAL_SCHEMA_HINT
        )
        raw2 = call_llm(CHEAP_MODEL, compact_system, user[:6000], max_tokens=2500)
        return _parse_proposal(raw2)


def draft_proposal(state: HomeworkState) -> dict:
    persona = state["persona"]
    items = state["research_items"]
    sources_block = "\n".join(
        f"- {it.get('title')} | {it.get('url')}\n  {it.get('snippet', '')[:200]}"
        for it in items
    ) or "（無）"
    system = (
        f"你是 {persona['name']}（{persona.get('role', '')}），正在腦力激盪會議提案。"
        "提案必須引用提供的真實搜尋 URL，禁止捏造連結。"
        "BMC 九格必須齊全。"
        + _PROPOSAL_SCHEMA_HINT
    )
    user = (
        f"主題：{state['topic']}\n\n公司：\n{state['company']}\n\n"
        f"研究彙整：\n{state['research_brief']}\n\n"
        f"可用來源：\n{sources_block}"
    )
    proposal = _request_proposal(system, user)
    missing = assert_bmc_complete(proposal)
    if missing:
        # 一次補齊缺格，維持結構不變量
        proposal = _request_proposal(
            "你只負責補齊 BMC 缺漏欄位。輸出完整提案 JSON（含原有內容）。"
            + _PROPOSAL_SCHEMA_HINT,
            f"缺漏：{missing}\n\n原提案：\n{json.dumps(proposal, ensure_ascii=False)}",
        )
    emit_event(
        "draft_proposal",
        f"初稿《{proposal.get('title', '')}》self_score={proposal.get('self_score')}",
        extra={"bmc_missing": assert_bmc_complete(proposal)},
    )
    return {
        "proposal": proposal,
        "proposal_versions": [proposal],
        "refine_round": 0,
    }


def refine(state: HomeworkState) -> dict:
    """自我修正一輪：對照 brief／來源挑弱點，產出新版並量測 delta。"""
    persona = state["persona"]
    prev = state["proposal"]
    round_i = int(state.get("refine_round") or 0) + 1
    items = state["research_items"]
    sources_block = "\n".join(
        f"- {it.get('title')} | {it.get('url')}" for it in items
    )
    system = (
        f"你是 {persona['name']}，正在做第 {round_i}/{REFINE_ROUNDS} 輪自我修正。"
        "請挑出上一版最弱的 2-3 點（商業模式、依據不足、或與公司定位不合），"
        "產出強化後的完整提案 JSON。url 仍只能來自可用來源。"
        + _PROPOSAL_SCHEMA_HINT
    )
    user = (
        f"主題：{state['topic']}\n\n研究彙整（節錄）：\n{state['research_brief'][:1200]}\n\n"
        f"可用來源：\n{sources_block}\n\n"
        f"上一版提案 JSON：\n{json.dumps(prev, ensure_ascii=False)}"
    )
    nxt = _request_proposal(system, user)
    if assert_bmc_complete(nxt):
        # 不變量：缺格就保留上一版 bmc 對應格
        bmc = dict(prev.get("bmc") or {})
        bmc.update({k: v for k, v in (nxt.get("bmc") or {}).items() if v})
        nxt["bmc"] = bmc

    dist = embedding_distance(proposal_text_for_embed(prev), proposal_text_for_embed(nxt))
    score_delta = float(nxt.get("self_score") or 0) - float(prev.get("self_score") or 0)
    delta = {
        "round": round_i,
        "embedding_distance": round(dist, 4),
        "self_score_before": prev.get("self_score"),
        "self_score_after": nxt.get("self_score"),
        "self_score_delta": round(score_delta, 3),
    }
    print(
        f"  [refine #{round_i}] embed_dist={delta['embedding_distance']} "
        f"score {delta['self_score_before']}→{delta['self_score_after']} "
        f"(Δ{delta['self_score_delta']:+})"
    )
    emit_event(
        "refine",
        f"第 {round_i} 輪修正：embed_dist={delta['embedding_distance']} "
        f"score_Δ={delta['self_score_delta']:+}",
        extra=delta,
    )
    return {
        "proposal": nxt,
        "proposal_versions": [nxt],
        "refine_deltas": [delta],
        "refine_round": round_i,
    }


def route_after_refine(state: HomeworkState) -> Literal["refine", "done"]:
    if int(state.get("refine_round") or 0) < REFINE_ROUNDS:
        return "refine"
    return "done"


def build_homework_subgraph():
    """做功課子圖：collect→dedup→synthesize→draft→refine×3。"""
    g = StateGraph(HomeworkState)
    g.add_node("collect", instrument("collect", collect))
    g.add_node("dedup", instrument("dedup", dedup))
    g.add_node("synthesize", instrument("synthesize", synthesize))
    g.add_node("draft_proposal", instrument("draft_proposal", draft_proposal))
    g.add_node("refine", instrument("refine", refine))
    g.add_edge(START, "collect")
    g.add_edge("collect", "dedup")
    g.add_edge("dedup", "synthesize")
    g.add_edge("synthesize", "draft_proposal")
    g.add_edge("draft_proposal", "refine")
    g.add_conditional_edges(
        "refine",
        route_after_refine,
        {"refine": "refine", "done": END},
    )
    return g.compile()


homework_graph = build_homework_subgraph()


# ---------------------------------------------------------------------------
# 父圖：把子圖當一個節點（示範巢狀子圖邊界）
# ---------------------------------------------------------------------------

class MeetingState(TypedDict):
    topic: str
    persona: dict
    company: str
    research_items: List[dict]
    research_brief: str
    proposal: dict
    proposal_versions: List[dict]
    refine_deltas: List[dict]


def run_homework(state: MeetingState) -> dict:
    """父節點：invoke 做功課子圖，再把結果寫回會議 state。"""
    global _event_role
    _event_role = _persona_label(state["persona"])
    emit_event("homework_start", f"開始做功課子圖，主題={state['topic']!r}")
    result = homework_graph.invoke({
        "topic": state["topic"],
        "persona": state["persona"],
        "company": state["company"],
        "raw_results": [],
        "research_items": [],
        "research_brief": "",
        "proposal": {},
        "proposal_versions": [],
        "refine_deltas": [],
        "refine_round": 0,
    })
    emit_event(
        "homework_done",
        f"子圖完成，提案《{(result.get('proposal') or {}).get('title', '')}》，"
        f"修正 {len(result.get('refine_deltas') or [])} 輪",
    )
    return {
        "research_items": result.get("research_items") or [],
        "research_brief": result.get("research_brief") or "",
        "proposal": result.get("proposal") or {},
        "proposal_versions": result.get("proposal_versions") or [],
        "refine_deltas": result.get("refine_deltas") or [],
    }


def build_parent_graph():
    g = StateGraph(MeetingState)
    g.add_node("homework", instrument("homework", run_homework))
    g.add_edge(START, "homework")
    g.add_edge("homework", END)
    return g.compile()


meeting_graph = build_parent_graph()


# ---------------------------------------------------------------------------
# Baseline：同主題一次直接問 LLM（無搜尋、無修正）
# ---------------------------------------------------------------------------

def run_baseline(topic: str, company: str) -> dict:
    """對照組：一次單純 LLM 呼叫，模擬『直接問 ChatGPT/Claude』。"""
    global _event_role
    _event_role = "baseline"
    set_current_node("baseline")
    t0 = time.perf_counter()
    system = (
        "你是一位產品策略顧問。請針對主題直接給一個產品點子提案。"
        "若你提到依據，請誠實標注（可以是一般知識，不必有真實 URL）。"
        + _PROPOSAL_SCHEMA_HINT
    )
    user = f"主題：{topic}\n\n公司背景：\n{company}\n\n請直接給提案 JSON。"
    proposal = _request_proposal(system, user)
    node_times["baseline"] = node_times.get("baseline", 0.0) + (
        time.perf_counter() - t0
    )
    emit_event("baseline", f"直接問 LLM《{proposal.get('title', '')}》")
    return proposal


# ---------------------------------------------------------------------------
# 存檔 / 總結
# ---------------------------------------------------------------------------

def _node_cost(name: str) -> float:
    return sum(cost_of(e) for e in usage_log if e["node"] == name)


def total_cost() -> float:
    return sum(cost_of(e) for e in usage_log)


def print_run_summary() -> None:
    print(f"\n{'=' * 72}")
    print("執行總結")
    print(f"{'-' * 72}")
    print(f"{'節點':<18}{'時間':>8}{'呼叫':>6}{'in':>10}{'out':>10}{'USD':>10}")
    names = list(node_times.keys())
    for name in names:
        calls = [e for e in usage_log if e["node"] == name]
        t = node_times.get(name, 0.0)
        tin = sum(e["input"] for e in calls)
        tout = sum(e["output"] for e in calls)
        c = sum(cost_of(e) for e in calls)
        print(f"{name:<18}{t:>7.1f}s{len(calls):>6}{tin:>10}{tout:>10}{c:>10.4f}")
    print(f"{'-' * 72}")
    print(f"總成本 USD：{total_cost():.4f}")


def judge_third_round(deltas: List[dict]) -> str:
    if len(deltas) < 3:
        return "修正輪次不足 3，無法判斷。"
    d3 = deltas[2]
    # 經驗規則：embed 距離很小且分數幾乎沒動 → 第三輪性價比低
    if d3["embedding_distance"] < 0.05 and abs(d3["self_score_delta"]) < 0.5:
        return (
            f"第三輪 embed_dist={d3['embedding_distance']}、"
            f"score_Δ={d3['self_score_delta']} → 改善很小，性價比偏低。"
        )
    if d3["embedding_distance"] >= 0.08 or d3["self_score_delta"] >= 0.5:
        return (
            f"第三輪 embed_dist={d3['embedding_distance']}、"
            f"score_Δ={d3['self_score_delta']} → 仍有可見改善，值得保留。"
        )
    return (
        f"第三輪 embed_dist={d3['embedding_distance']}、"
        f"score_Δ={d3['self_score_delta']} → 改善有限，可視預算取捨。"
    )


def save_outputs(
    *,
    topic: str,
    persona: dict,
    agent_result: dict,
    baseline_proposal: dict,
    agent_metrics: dict,
    baseline_metrics: dict,
    deltas: List[dict],
) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = {
        "run_id": stamp,
        "topic": topic,
        "persona": {
            "id": persona.get("id"),
            "name": persona.get("name"),
            "role": persona.get("role"),
        },
        "agent": {
            "proposal": agent_result.get("proposal"),
            "research_brief": agent_result.get("research_brief"),
            "research_items": agent_result.get("research_items"),
            "proposal_versions": agent_result.get("proposal_versions"),
            "refine_deltas": deltas,
            "metrics": agent_metrics,
            "third_round_verdict": judge_third_round(deltas),
        },
        "baseline": {
            "proposal": baseline_proposal,
            "metrics": baseline_metrics,
        },
        "comparison": {
            "real_citations_agent": agent_metrics["real_citations"],
            "real_citations_baseline": baseline_metrics["real_citations"],
            "bmc_filled_agent": agent_metrics["bmc_filled"],
            "bmc_filled_baseline": baseline_metrics["bmc_filled"],
            "cost_agent_usd": agent_metrics["cost_usd"],
            "cost_baseline_usd": baseline_metrics["cost_usd"],
        },
        "total_cost_usd": round(total_cost(), 6),
    }
    path = OUTPUT_DIR / f"stage1-run-{stamp}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    # 另存一份 latest 方便回看
    (OUTPUT_DIR / "stage1-latest.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def main() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("缺少 ANTHROPIC_API_KEY。請複製 practice/.env.example 為 practice/.env 並填入。")
        sys.exit(1)

    reset_metrics()
    usage_log.clear()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    # 每場 run 覆寫事件流，避免與舊場混在一起（跨場累積留給之後 demo）
    if EVENTS_PATH.exists():
        EVENTS_PATH.unlink()

    topic = os.environ.get("BRAINSTORM_TOPIC", "如何提升新聞短影音互動率")
    persona = load_persona()
    company = load_company()

    print(f"主題：{topic}")
    print(f"Persona：{persona.get('name')}（{persona.get('role')}）")
    print(f"事件流：{EVENTS_PATH}")
    print()

    print("=== A) Agent 路徑：做功課子圖 ===")
    agent_state = meeting_graph.invoke({
        "topic": topic,
        "persona": persona,
        "company": company,
        "research_items": [],
        "research_brief": "",
        "proposal": {},
        "proposal_versions": [],
        "refine_deltas": [],
    })
    agent_cost = total_cost()
    agent_metrics = metrics_of(
        agent_state["proposal"],
        agent_state["research_items"],
        agent_cost,
    )

    print()
    print("=== B) Baseline：直接問 LLM 一次 ===")
    cost_before_baseline = total_cost()
    baseline_proposal = run_baseline(topic, company)
    baseline_cost = total_cost() - cost_before_baseline
    baseline_metrics = metrics_of(baseline_proposal, [], baseline_cost)
    # baseline 沒有真實搜尋池 → real_citations 應為 0（即便模型編了 url）
    baseline_metrics["real_citations"] = count_real_citations(baseline_proposal, [])

    deltas = agent_state.get("refine_deltas") or []
    out_path = save_outputs(
        topic=topic,
        persona=persona,
        agent_result=agent_state,
        baseline_proposal=baseline_proposal,
        agent_metrics=agent_metrics,
        baseline_metrics=baseline_metrics,
        deltas=deltas,
    )

    print()
    print("=== 驗收 ===")
    missing = assert_bmc_complete(agent_state["proposal"])
    print(f"BMC 九格齊全：{'是' if not missing else '否，缺 ' + str(missing)}")
    print(f"真實搜尋引用數（agent）：{agent_metrics['real_citations']}")
    print(f"真實搜尋引用數（baseline）：{baseline_metrics['real_citations']}")
    print("自我修正 delta：")
    for d in deltas:
        print(
            f"  round {d['round']}: embed_dist={d['embedding_distance']} "
            f"score {d['self_score_before']}→{d['self_score_after']} "
            f"(Δ{d['self_score_delta']:+})"
        )
    print(f"第三輪判斷：{judge_third_round(deltas)}")
    print()
    print("對照（同一套指標）：")
    print(
        f"  agent    citations={agent_metrics['real_citations']} "
        f"bmc={agent_metrics['bmc_filled']}/9 cost=${agent_metrics['cost_usd']:.4f}"
    )
    print(
        f"  baseline citations={baseline_metrics['real_citations']} "
        f"bmc={baseline_metrics['bmc_filled']}/9 cost=${baseline_metrics['cost_usd']:.4f}"
    )
    print(f"\n已存檔：{out_path}")
    print(f"事件流：{EVENTS_PATH}（{sum(1 for _ in EVENTS_PATH.open())} 筆）")
    print_run_summary()

    # 硬驗收：失敗就非零退出，方便之後 CI／自己確認
    ok = (
        not missing
        and agent_metrics["real_citations"] >= 1
        and len(deltas) == REFINE_ROUNDS
        and out_path.exists()
        and EVENTS_PATH.exists()
    )
    if not ok:
        print("\n驗收未通過，請檢查上方輸出。")
        sys.exit(2)
    print("\n驗收通過。")


if __name__ == "__main__":
    main()
