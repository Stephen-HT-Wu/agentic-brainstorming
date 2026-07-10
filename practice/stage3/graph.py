"""
階段 3：需求探索訪談 + Define（agent 對 agent 角色扮演多輪對話）

目標：每個 persona 做完功課後、**在提出任何點子之前**，先獨立設計自己的
需求訪綱，訪談 `users.yaml` 定義的模擬使用者（也是 agent 扮演，2-3 位），
從逐字稿萃取洞見，寫出 POV 陳述 + HMW 問句，最後提案必須明確回應自己的
HMW、並引用真實訪談洞見。對應 Design Thinking 的 Empathize → Define。

對應 PLAN.md「階段 3」驗收：
- 訪綱彼此不同（每個 persona 獨立設計，不共用同一份）
- 每個提案都有明確對應的 POV/HMW
- 提案有可歸因到訪談洞見的內容（insight_refs 可驗證，不是憑空引用）
- 逐字稿完整記錄

本檔是 stage 2 的完整獨立副本再擴充（不 import stage2），沿用專案慣例：
改一個階段不牽動其他階段。新增的部分集中在做功課子圖裡
synthesize 之後、draft_proposal 之前新插入的四個節點：
design_interview_guide → conduct_interviews → extract_insights → write_pov_hmw。

執行前在 practice/.env 設定 ANTHROPIC_API_KEY（見 .env.example）。
"""
from __future__ import annotations

import hashlib
import itertools
import json
import math
import operator
import os
import re
import sys
import time
import warnings
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Annotated, Any, List, Literal, Optional, TypedDict

import anthropic
import yaml
from langchain_core._api.deprecation import LangChainPendingDeprecationWarning
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

warnings.filterwarnings("ignore", category=LangChainPendingDeprecationWarning)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _common import (  # noqa: E402
    cost_of,
    current_invocation,
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

CHEAP_MODEL = "claude-haiku-4-5-20251001"  # persona 做功課／訪談／提案／修正
client = anthropic.Anthropic()

DEDUP_SIMILARITY_THRESHOLD = 0.80  # 搜尋結果去重門檻
IDEA_DEDUP_THRESHOLD = 0.75        # 跨 persona 提案去重門檻
EMBED_DIM = 256
REFINE_ROUNDS = 3
INTERVIEW_ROUNDS = 3  # 每位模擬使用者訪談幾輪（第 1 輪用訪綱開場問題，之後動態追問）

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
_event_role: ContextVar[str] = ContextVar("event_role", default="system")
_events_lock = Lock()  # 平行節點會同時 append 同一個檔案，寫檔要序列化


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


def load_personas() -> List[dict]:
    """真實 personas.yaml 優先，否則 personas.example.yaml；回傳全部人。"""
    real = PRACTICE_DIR / "personas.yaml"
    example = PRACTICE_DIR / "personas.example.yaml"
    path = real if real.exists() else example
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    personas = data["personas"]
    if not personas:
        raise ValueError(f"{path} 沒有 personas")
    return personas


def load_users() -> List[dict]:
    """真實 users.yaml 優先，否則 users.example.yaml；每位 persona 都會訪談全部人。"""
    real = PRACTICE_DIR / "users.yaml"
    example = PRACTICE_DIR / "users.example.yaml"
    path = real if real.exists() else example
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    users = data["users"]
    if not users:
        raise ValueError(f"{path} 沒有 users")
    return users


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
            "invocation": current_invocation(),
            "role": _event_role.get(),
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


def extract_json_object(text: str) -> dict:
    """跟 extract_json 一樣，但保證回傳 dict——extract_json 底層是 json.loads，
    合法 JSON 也可能是 list/字串/數字，直接 .get() 會炸 AttributeError
    （design_interview_guide/extract_insights/write_pov_hmw 都只期待 object，
    真實跑測時模型確實吐過一次 list，這裡統一擋掉）。"""
    try:
        data = extract_json(text)
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


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
    """每個節點寫一筆結構化事件到 outputs/events.jsonl；平行節點共用檔案，寫檔序列化。"""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    node = current_node()
    invocation = current_invocation()
    calls = [e for e in usage_log if e.get("invocation") == invocation]
    tokens_in = sum(e["input"] for e in calls)
    tokens_out = sum(e["output"] for e in calls)
    cost = sum(cost_of(e) for e in calls)
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "role": role or _event_role.get(),
        "action": action,
        "node": node,
        "summary": summary,
        "tokens": {"input": tokens_in, "output": tokens_out},
        "cost_usd": round(cost, 6),
    }
    if extra:
        record["extra"] = extra
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with _events_lock:
        with EVENTS_PATH.open("a", encoding="utf-8") as f:
            f.write(line)


# ---------------------------------------------------------------------------
# Embedding（純 Python feature hashing，免 API；之後 stage 7 再換 Chroma）
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> List[str]:
    """英文切詞、中文切 2/3-gram，讓小幅中文改寫仍有共同特徵。"""
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    for chunk in re.findall(r"[一-鿿]+", text):
        if len(chunk) == 1:
            tokens.append(chunk)
            continue
        for n in (2, 3):
            tokens.extend(chunk[i : i + n] for i in range(len(chunk) - n + 1))
    return tokens


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


def is_usable_search_result(item: dict) -> bool:
    """排除空連結及搜尋引擎廣告跳轉頁，避免它們進入研究與引用池。"""
    url = (item.get("url") or "").strip().lower()
    if not url.startswith(("http://", "https://")):
        return False
    return not any(marker in url for marker in ("bing.com/aclick", "googleadservices.com"))


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
    """只取 BMC 的『值』，不含鍵名——九個鍵名在每份提案裡都逐字相同，
    直接塞整包 json.dumps(bmc) 會稀釋真正的內容差異訊號（stage2 踩過的坑）。"""
    bmc = proposal.get("bmc") or {}
    parts = [
        proposal.get("title", ""),
        proposal.get("summary", ""),
        "\n".join(str(v) for v in bmc.values()),
    ]
    return "\n".join(parts)


def assert_bmc_complete(proposal: dict) -> List[str]:
    """回傳 BMC 結構問題；空 list = 恰好九格且每格為非空字串。"""
    bmc = proposal.get("bmc") or {}
    issues = []
    for key in BMC_KEYS:
        val = bmc.get(key)
        if not isinstance(val, str) or not val.strip():
            issues.append(f"缺漏或無效:{key}")
    issues.extend(f"額外欄位:{key}" for key in bmc if key not in BMC_KEYS)
    return issues


def count_real_citations(proposal: dict, research_items: List[dict]) -> int:
    """提案 sources 的 url 有出現在真實搜尋結果裡才算『真實依據』。"""
    known = {item.get("url", "").rstrip("/") for item in research_items if item.get("url")}
    n = 0
    for src in proposal.get("sources") or []:
        url = (src.get("url") or "").rstrip("/")
        if url and url in known:
            n += 1
    return n


def count_real_insight_refs(proposal: dict, insights: List[dict]) -> int:
    """提案 insight_refs 裡有幾個 id 真的存在於這位 persona 自己萃取的洞見池——
    跟 count_real_citations 同一種『可驗證引用』設計，避免模型隨口編 id。"""
    known = {i.get("id") for i in insights if i.get("id")}
    refs = proposal.get("insight_refs") or []
    return sum(1 for r in refs if r in known)


def metrics_of(proposal: dict, research_items: List[dict], insights: List[dict], cost: float) -> dict:
    missing = assert_bmc_complete(proposal)
    return {
        "bmc_complete": len(missing) == 0,
        "bmc_missing": missing,
        "bmc_filled": sum(
            isinstance((proposal.get("bmc") or {}).get(k), str)
            and bool((proposal.get("bmc") or {}).get(k).strip())
            for k in BMC_KEYS
        ),
        "real_citations": count_real_citations(proposal, research_items),
        "real_insight_refs": count_real_insight_refs(proposal, insights),
        "source_count": len(proposal.get("sources") or []),
        "has_pov_hmw": bool((proposal.get("pov") or "").strip()) and bool((proposal.get("hmw") or "").strip()),
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
    users: List[dict]
    raw_results: List[dict]
    research_items: List[dict]
    research_brief: str
    interview_guide: dict
    interview_transcript: List[dict]
    insights: List[dict]
    pov: str
    hmw: str
    proposal: dict
    proposal_versions: Annotated[List[dict], operator.add]
    refine_deltas: Annotated[List[dict], operator.add]
    refine_round: int


def _persona_label(persona: dict) -> str:
    return f"persona:{persona.get('name', persona.get('id', '?'))}"


def collect(state: HomeworkState) -> dict:
    """依 persona 關注面向組查詢，呼叫 web search（agent tool use）。"""
    persona = state["persona"]
    _event_role.set(_persona_label(persona))
    topic = state["topic"]
    focus = persona.get("focus") or ["市場現況"]
    queries = [
        f"{topic} {focus[0]}",
        f"{topic} 市場趨勢 競品 案例",
        f"{topic} {focus[1] if len(focus) > 1 else '用戶需求 商業模式'}",
    ]
    raw: List[dict] = []
    for q in queries:
        try:
            hits = web_search(q, max_results=4)
            usable = [hit for hit in hits if is_usable_search_result(hit)]
            raw.extend(usable)
            print(f"  [collect:{persona.get('name')}] query={q!r} → {len(usable)}/{len(hits)} 筆可用")
        except Exception as exc:  # noqa: BLE001 — 搜尋失敗不中斷整場
            print(f"  [collect:{persona.get('name')}] query={q!r} 失敗：{exc}")
    emit_event(
        "collect",
        f"搜尋 {len(queries)} 組查詢，共 {len(raw)} 筆原始結果",
        extra={"queries": queries, "n_results": len(raw)},
    )
    return {"raw_results": raw}


def dedup(state: HomeworkState) -> dict:
    raw = state["raw_results"]
    items = dedup_by_embedding(raw)
    print(f"  [dedup:{state['persona'].get('name')}] {len(raw)} → {len(items)}（門檻 {DEDUP_SIMILARITY_THRESHOLD}）")
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


# ---------------------------------------------------------------------------
# Empathize：需求探索訪談（新，stage 3 核心）
# ---------------------------------------------------------------------------

def design_interview_guide(state: HomeworkState) -> dict:
    """persona 獨立設計自己的訪綱——刻意不給任何點子／公司資訊，只給關注面向
    與研究彙整，確保訪談焦點是『需求探索』而不是『驗證某個已經想好的方案』。"""
    persona = state["persona"]
    system = (
        f"你是 {persona['name']}（{persona.get('role', '')}），正要對用戶做需求探索訪談，"
        "還沒有任何具體點子。訪綱只能聚焦在：現有痛點、使用情境、目前怎麼解決——"
        "絕對不能出現任何具體產品點子、功能或解法字眼。"
        "只輸出 JSON：{\"questions\": [3 個開放式問題，每題 <=30 字]}"
    )
    user = (
        f"會議主題：{state['topic']}\n\n"
        f"你的關注面向：{', '.join(persona.get('focus') or [])}\n\n"
        f"研究彙整（節錄）：\n{state['research_brief'][:800]}"
    )
    raw = call_llm(CHEAP_MODEL, system, user, max_tokens=500)
    data = extract_json_object(raw)
    questions = [q for q in (data.get("questions") or []) if isinstance(q, str) and q.strip()]
    if not questions:
        # 結構性保底：訪綱不能是空的，用機械式後備問題頂著，不讓整場卡住
        questions = [
            f"你平常怎麼接觸「{state['topic']}」相關的內容？",
            "遇到最不方便或最困擾的地方是什麼？",
            "現在都怎麼解決或應付這個困擾？",
        ]
    guide = {"questions": questions[:3]}
    emit_event("design_interview_guide", f"訪綱：{questions}", extra=guide)
    return {"interview_guide": guide}


def _user_system_prompt(user: dict) -> str:
    return (
        f"你是 {user['name']}，{user.get('age', '')} 歲。"
        f"情境：{user.get('context', '')}。"
        f"平常的困擾：{'；'.join(user.get('pain_points') or [])}。"
        f"說話風格：{user.get('tone', '')}。"
        "你完全不知道對方在幫公司做產品研究，只是在回答一位訪談者的問題。"
        "只根據你自己真實的生活情境回答，不要幫忙想點子、不要談商業模式；"
        "如果被問到解法，就誠實說你目前的土法煉鋼做法或『我沒想過』。"
        "回答 1-3 句話，符合你的說話風格，不要條列。"
    )


def simulate_user_answer(user: dict, question: str, prior_turns: List[dict]) -> str:
    """模擬使用者回答——跟 persona 的角色扮演對話是這個階段唯一的新機制：
    雙方都是 agent，各自只看得到自己該看的資訊（使用者看不到商業考量）。"""
    system = _user_system_prompt(user)
    history = "\n".join(f"Q: {t['question']}\nA: {t['answer']}" for t in prior_turns) or "（尚無先前對話）"
    prompt = f"先前對話：\n{history}\n\n新問題：{question}"
    return call_llm(CHEAP_MODEL, system, prompt, max_tokens=300).strip()


def generate_followup_question(persona: dict, prior_turns: List[dict]) -> str:
    """persona 根據對方剛剛的回答動態決定下一句追問——不是照本宣科念完訪綱，
    這是『agent 自己決定接下來要問什麼』的地方。"""
    last_answer = prior_turns[-1]["answer"]
    history = "\n".join(f"Q: {t['question']}\nA: {t['answer']}" for t in prior_turns)
    system = (
        f"你是 {persona['name']}，正在做用戶需求訪談（探索階段，不能提任何點子）。"
        "根據對方剛剛的回答，問一個更深入的追問，聚焦在痛點或情境細節。"
        "只輸出問題本身（<=30 字），不要加解說或引號。"
    )
    prompt = f"訪談記錄：\n{history}\n\n對方剛回答：{last_answer}\n\n下一個追問？"
    question = call_llm(CHEAP_MODEL, system, prompt, max_tokens=150).strip()
    return question or f"能不能多說一點「{last_answer[:15]}」這件事？"


def conduct_interviews(state: HomeworkState) -> dict:
    """對 state["users"] 裡的每一位模擬使用者各跑 INTERVIEW_ROUNDS 輪對話。"""
    persona = state["persona"]
    guide = state["interview_guide"]
    users = state["users"]
    transcript: List[dict] = []
    for user in users:
        user_turns: List[dict] = []
        for round_i in range(1, INTERVIEW_ROUNDS + 1):
            question = (
                guide["questions"][0] if round_i == 1 and guide.get("questions")
                else generate_followup_question(persona, user_turns)
            )
            answer = simulate_user_answer(user, question, user_turns)
            turn = {
                "user_id": user.get("id"),
                "user_name": user.get("name"),
                "round": round_i,
                "question": question,
                "answer": answer,
            }
            user_turns.append(turn)
            transcript.append(turn)
            emit_event(
                "interview_turn",
                f"訪談 {user.get('name')} 第{round_i}輪：{question}",
                extra=turn,
            )
        print(f"  [interview:{persona['name']}] 完成與 {user.get('name')} 的 {INTERVIEW_ROUNDS} 輪訪談")
    emit_event(
        "conduct_interviews",
        f"完成 {len(users)} 位模擬使用者 × {INTERVIEW_ROUNDS} 輪，共 {len(transcript)} 筆逐字稿",
    )
    return {"interview_transcript": transcript}


def extract_insights(state: HomeworkState) -> dict:
    """把完整逐字稿萃取成帶 id 的洞見清單——之後 draft_proposal 只能引用這些 id，
    不能空口說『用戶反應很好』，這是本階段『可歸因』驗收的資料來源。"""
    persona = state["persona"]
    transcript = state["interview_transcript"]
    lines = "\n".join(
        f"[{t['user_name']}] Q:{t['question']} / A:{t['answer']}" for t in transcript
    )
    system = (
        f"你是 {persona['name']}，剛做完使用者訪談，現在要萃取洞見。"
        "只輸出 JSON：{\"insights\": [{\"text\": \"一句話洞見，<=50字\"}]}，"
        "最多 5 則，每則必須具體可回溯到某位受訪者說的話，不要空泛通則。"
    )
    user = f"完整逐字稿：\n{lines}"
    raw = call_llm(CHEAP_MODEL, system, user, max_tokens=800)
    data = extract_json_object(raw)
    if not data:
        data = extract_json_object(repair_json_text(raw))
    raw_insights = [
        i for i in (data.get("insights") or [])
        if isinstance(i, dict) and (i.get("text") or "").strip()
    ]
    if not raw_insights:
        # 結構性保底：一則洞見都沒有就直接拿第一輪問答當洞見，保證非空
        raw_insights = [{"text": f"{t['user_name']}：{t['answer'][:50]}"} for t in transcript[:2]]
    insights = [{"id": f"i{n}", "text": it["text"].strip()} for n, it in enumerate(raw_insights, 1)]
    emit_event("extract_insights", f"萃取 {len(insights)} 則洞見", extra={"insights": insights})
    return {"insights": insights}


def write_pov_hmw(state: HomeworkState) -> dict:
    """寫 Design Thinking 的 POV 陳述 + HMW 問句——之後提案必須明確回應這個 HMW。"""
    persona = state["persona"]
    insights = state["insights"]
    insights_block = "\n".join(f"- [{i['id']}] {i['text']}" for i in insights)
    system = (
        f"你是 {persona['name']}。根據訪談洞見寫出 POV 陳述與 HMW 問句。"
        "只輸出 JSON：{\"pov\": \"[用戶] 需要 [需求]，因為 [洞見]\", "
        "\"hmw\": \"How might we ...?（中文可）\"}。"
        "兩者都要具體對應到下面列出的洞見，不要寫空泛通則。"
    )
    user = f"洞見清單：\n{insights_block}"
    raw = call_llm(CHEAP_MODEL, system, user, max_tokens=500)
    data = extract_json_object(raw)
    pov = (data.get("pov") or "").strip()
    hmw = (data.get("hmw") or "").strip()
    if not pov or not hmw:
        # 結構性保底：POV/HMW 絕不能是空字串（下游 draft_proposal 的硬驗收依賴它）
        top = insights[0]["text"] if insights else state["topic"]
        pov = pov or f"用戶需要更好的方式面對「{top}」這個處境。"
        hmw = hmw or f"我們可以怎麼協助用戶解決「{top}」？"
    emit_event("write_pov_hmw", f"POV：{pov} / HMW：{hmw}", extra={"pov": pov, "hmw": hmw})
    return {"pov": pov, "hmw": hmw}


# ---------------------------------------------------------------------------
# Ideate：提案協議（沿用 stage1/2 的 BMC 骨架，新增 hmw_response/insight_refs）
# ---------------------------------------------------------------------------

_PROPOSAL_SCHEMA_HINT = f"""
請只輸出一個 JSON 物件（不要 markdown 圍欄），欄位：
- title: string（<=40字）
- summary: string（2-3句，<=120字）
- hmw_response: string（<=60字，說明這個提案怎麼回應你的 HMW）
- insight_refs: [string] 1-3 筆，必須是提供的訪談洞見 id（例如 "i1"），不能捏造
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
    data.setdefault("insight_refs", [])
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


def _ensure_hmw_fields(proposal: dict, state: HomeworkState) -> dict:
    """POV/HMW 直接由程式從 state 覆寫（不靠模型覆誦，避免抄錯字）；
    insight_refs 只保留真的存在於這位 persona 洞見池的 id，缺了就補第一則洞見，
    確保『提案可歸因到訪談洞見』這個結構不變量永遠成立。"""
    proposal["pov"] = state["pov"]
    proposal["hmw"] = state["hmw"]
    insights = state["insights"]
    known_ids = {i.get("id") for i in insights if i.get("id")}
    refs = [r for r in (proposal.get("insight_refs") or []) if r in known_ids]
    if not refs and insights:
        refs = [insights[0]["id"]]
    proposal["insight_refs"] = refs
    if not (proposal.get("hmw_response") or "").strip():
        proposal["hmw_response"] = f"（系統保底）呼應 HMW：{state['hmw'][:50]}"
    return proposal


def draft_proposal(state: HomeworkState) -> dict:
    persona = state["persona"]
    items = state["research_items"]
    insights = state["insights"]
    sources_block = "\n".join(
        f"- {it.get('title')} | {it.get('url')}\n  {it.get('snippet', '')[:200]}"
        for it in items
    ) or "（無）"
    insights_block = "\n".join(f"- [{i['id']}] {i['text']}" for i in insights) or "（無）"
    system = (
        f"你是 {persona['name']}（{persona.get('role', '')}），正在腦力激盪會議提案。"
        f"你先前訪談用戶後定義的 HMW 是：「{state['hmw']}」——提案必須明確回應這個 HMW，"
        "不能是跟訪談洞見無關的天外飛來一筆。"
        "提案必須引用提供的真實搜尋 URL，禁止捏造連結。insight_refs 只能引用下面列出的洞見 id。"
        "BMC 九格必須齊全。"
        + _PROPOSAL_SCHEMA_HINT
    )
    user = (
        f"主題：{state['topic']}\n\n公司：\n{state['company']}\n\n"
        f"POV：{state['pov']}\nHMW：{state['hmw']}\n\n"
        f"可引用的訪談洞見：\n{insights_block}\n\n"
        f"研究彙整：\n{state['research_brief']}\n\n"
        f"可用來源：\n{sources_block}"
    )
    proposal = _request_proposal(system, user)
    issues = assert_bmc_complete(proposal)
    if issues:
        proposal = _request_proposal(
            "你只負責補齊 BMC 缺漏欄位。輸出完整提案 JSON（含原有內容）。"
            + _PROPOSAL_SCHEMA_HINT,
            f"結構問題：{issues}\n\n原提案：\n{json.dumps(proposal, ensure_ascii=False)}",
        )
    proposal = _ensure_hmw_fields(proposal, state)
    if issues := assert_bmc_complete(proposal):
        raise ValueError(f"BMC 結構不變量失敗：{issues}")
    emit_event(
        "draft_proposal",
        f"初稿《{proposal.get('title', '')}》self_score={proposal.get('self_score')} "
        f"insight_refs={proposal.get('insight_refs')}",
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
        f"你的 HMW 是：「{state['hmw']}」，修正後仍要回應它。"
        "請挑出上一版最弱的 2-3 點（商業模式、依據不足、或與公司定位不合），"
        "產出強化後的完整提案 JSON。url 仍只能來自可用來源，insight_refs 仍只能引用提供的洞見 id。"
        + _PROPOSAL_SCHEMA_HINT
    )
    user = (
        f"主題：{state['topic']}\n\n研究彙整（節錄）：\n{state['research_brief'][:1200]}\n\n"
        f"可用來源：\n{sources_block}\n\n"
        f"可引用的訪談洞見：\n" + "\n".join(f"- [{i['id']}] {i['text']}" for i in state['insights']) + "\n\n"
        f"上一版提案 JSON：\n{json.dumps(prev, ensure_ascii=False)}"
    )
    nxt = _request_proposal(system, user)
    # 只接受協議內的九格；缺格或無效值沿用上一版。
    candidate_bmc = nxt.get("bmc") or {}
    prev_bmc = prev.get("bmc") or {}
    nxt["bmc"] = {
        key: candidate_bmc.get(key)
        if isinstance(candidate_bmc.get(key), str) and candidate_bmc[key].strip()
        else prev_bmc.get(key, "")
        for key in BMC_KEYS
    }
    nxt = _ensure_hmw_fields(nxt, state)
    if issues := assert_bmc_complete(nxt):
        raise ValueError(f"BMC 結構不變量失敗：{issues}")

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
        f"  [refine:{persona.get('name')} #{round_i}] embed_dist={delta['embedding_distance']} "
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
    """做功課子圖：collect→dedup→synthesize→
    design_interview_guide→conduct_interviews→extract_insights→write_pov_hmw→
    draft_proposal→refine×3。新插入的四個節點是本階段唯一的新架構。"""
    g = StateGraph(HomeworkState)
    g.add_node("collect", instrument("collect", collect))
    g.add_node("dedup", instrument("dedup", dedup))
    g.add_node("synthesize", instrument("synthesize", synthesize))
    g.add_node("design_interview_guide", instrument("design_interview_guide", design_interview_guide))
    g.add_node("conduct_interviews", instrument("conduct_interviews", conduct_interviews))
    g.add_node("extract_insights", instrument("extract_insights", extract_insights))
    g.add_node("write_pov_hmw", instrument("write_pov_hmw", write_pov_hmw))
    g.add_node("draft_proposal", instrument("draft_proposal", draft_proposal))
    g.add_node("refine", instrument("refine", refine))
    g.add_edge(START, "collect")
    g.add_edge("collect", "dedup")
    g.add_edge("dedup", "synthesize")
    g.add_edge("synthesize", "design_interview_guide")
    g.add_edge("design_interview_guide", "conduct_interviews")
    g.add_edge("conduct_interviews", "extract_insights")
    g.add_edge("extract_insights", "write_pov_hmw")
    g.add_edge("write_pov_hmw", "draft_proposal")
    g.add_edge("draft_proposal", "refine")
    g.add_conditional_edges(
        "refine",
        route_after_refine,
        {"refine": "refine", "done": END},
    )
    return g.compile()


homework_graph = build_homework_subgraph()


# ---------------------------------------------------------------------------
# 父圖：Send fan-out（同 stage 2，新增 users 隨 Send payload 傳下去）
# ---------------------------------------------------------------------------

class PersonaTask(TypedDict):
    topic: str
    company: str
    persona: dict
    users: List[dict]


class MeetingState(TypedDict):
    topic: str
    company: str
    personas: List[dict]
    users: List[dict]
    persona_results: Annotated[List[dict], operator.add]


def fan_out_personas(state: MeetingState) -> List[Send]:
    return [
        Send("homework_worker", {
            "topic": state["topic"],
            "company": state["company"],
            "persona": persona,
            "users": state["users"],
        })
        for persona in state["personas"]
    ]


def homework_worker(task: PersonaTask) -> dict:
    """worker 節點：對一位 persona 完整跑做功課＋訪談＋提案子圖，量測實際耗時。"""
    persona = task["persona"]
    role_token = _event_role.set(_persona_label(persona))
    t0 = time.perf_counter()
    emit_event("homework_start", f"開始做功課子圖，主題={task['topic']!r}")
    try:
        result = homework_graph.invoke({
            "topic": task["topic"],
            "persona": persona,
            "company": task["company"],
            "users": task["users"],
            "raw_results": [],
            "research_items": [],
            "research_brief": "",
            "interview_guide": {},
            "interview_transcript": [],
            "insights": [],
            "pov": "",
            "hmw": "",
            "proposal": {},
            "proposal_versions": [],
            "refine_deltas": [],
            "refine_round": 0,
        })
    finally:
        _event_role.reset(role_token)
    elapsed = time.perf_counter() - t0
    emit_event(
        "homework_done",
        f"子圖完成，提案《{(result.get('proposal') or {}).get('title', '')}》，"
        f"耗時 {elapsed:.1f}s",
        role=_persona_label(persona),
        extra={"elapsed_s": round(elapsed, 2)},
    )
    return {
        "persona_results": [{
            "persona": {
                "id": persona.get("id"),
                "name": persona.get("name"),
                "role": persona.get("role"),
            },
            "proposal": result.get("proposal") or {},
            "research_items": result.get("research_items") or [],
            "research_brief": result.get("research_brief") or "",
            "interview_guide": result.get("interview_guide") or {},
            "interview_transcript": result.get("interview_transcript") or [],
            "insights": result.get("insights") or [],
            "pov": result.get("pov") or "",
            "hmw": result.get("hmw") or "",
            "refine_deltas": result.get("refine_deltas") or [],
            "elapsed_s": round(elapsed, 2),
        }]
    }


def build_parent_graph():
    g = StateGraph(MeetingState)
    g.add_node("homework_worker", instrument("homework_worker", homework_worker))
    g.add_conditional_edges(START, fan_out_personas, ["homework_worker"])
    g.add_edge("homework_worker", END)
    return g.compile()


meeting_graph = build_parent_graph()


# ---------------------------------------------------------------------------
# Baseline：同主題一次直接問 LLM
# ---------------------------------------------------------------------------

def run_baseline(topic: str, company: str) -> dict:
    role_token = _event_role.set("baseline")
    set_current_node("baseline")
    t0 = time.perf_counter()
    system = (
        "你是一位產品策略顧問。請針對主題直接給一個產品點子提案。"
        "若你提到依據，請誠實標注（可以是一般知識，不必有真實 URL）。"
        "沒有做過用戶訪談，pov/hmw/insight_refs 留空或合理帶過即可。"
        + _PROPOSAL_SCHEMA_HINT
    )
    user = f"主題：{topic}\n\n公司背景：\n{company}\n\n請直接給提案 JSON。"
    try:
        proposal = _request_proposal(system, user)
    finally:
        _event_role.reset(role_token)
    node_times["baseline"] = node_times.get("baseline", 0.0) + (
        time.perf_counter() - t0
    )
    emit_event("baseline", f"直接問 LLM《{proposal.get('title', '')}》", role="baseline")
    return proposal


# ---------------------------------------------------------------------------
# 多樣性度量（提案／訪綱皆可用同一套 pairwise 距離）
# ---------------------------------------------------------------------------

def pairwise_text_diversity(labeled_texts: List[tuple]) -> dict:
    pairs = []
    for (la, ta), (lb, tb) in itertools.combinations(labeled_texts, 2):
        pairs.append({"a": la, "b": lb, "distance": round(embedding_distance(ta, tb), 4)})
    avg = sum(p["distance"] for p in pairs) / len(pairs) if pairs else 0.0
    return {"avg_distance": round(avg, 4), "pairs": pairs}


def pairwise_diversity(proposals: List[dict]) -> dict:
    labeled = [(p.get("title", f"#{i}"), proposal_text_for_embed(p)) for i, p in enumerate(proposals)]
    return pairwise_text_diversity(labeled)


def dedup_proposals(proposals: List[dict], threshold: float = IDEA_DEDUP_THRESHOLD) -> List[dict]:
    kept: List[dict] = []
    kept_vecs: List[List[float]] = []
    for p in proposals:
        vec = embed_text(proposal_text_for_embed(p))
        if any(cosine_similarity(vec, prev) >= threshold for prev in kept_vecs):
            continue
        kept.append(p)
        kept_vecs.append(vec)
    return kept


def guide_text(guide: dict) -> str:
    return "\n".join(guide.get("questions") or [])


# ---------------------------------------------------------------------------
# 存檔 / 總結
# ---------------------------------------------------------------------------

def _role_cost(role: str) -> float:
    return sum(cost_of(e) for e in usage_log if e.get("role") == role)


def total_cost() -> float:
    return sum(cost_of(e) for e in usage_log)


def print_run_summary() -> None:
    print(f"\n{'=' * 72}")
    print("執行總結")
    print(f"{'-' * 72}")
    print(f"{'節點':<22}{'時間':>8}{'呼叫':>6}{'in':>10}{'out':>10}{'USD':>10}")
    for name in list(node_times.keys()):
        calls = [e for e in usage_log if e["node"] == name]
        t = node_times.get(name, 0.0)
        tin = sum(e["input"] for e in calls)
        tout = sum(e["output"] for e in calls)
        c = sum(cost_of(e) for e in calls)
        print(f"{name:<22}{t:>7.1f}s{len(calls):>6}{tin:>10}{tout:>10}{c:>10.4f}")
    print(f"{'-' * 72}")
    print(f"總成本 USD：{total_cost():.4f}")


def save_outputs(
    *,
    topic: str,
    personas: List[dict],
    users: List[dict],
    persona_results: List[dict],
    baseline_proposal: dict,
    baseline_metrics: dict,
    diversity: dict,
    guide_diversity: dict,
    distinct_proposals: List[dict],
    timing: dict,
) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    agent_cost = total_cost() - baseline_metrics["cost_usd"]
    out = {
        "run_id": stamp,
        "topic": topic,
        "persona_count": len(personas),
        "user_count": len(users),
        "persona_results": [
            {
                "persona": r["persona"],
                "pov": r["pov"],
                "hmw": r["hmw"],
                "interview_guide": r["interview_guide"],
                "interview_transcript": r["interview_transcript"],
                "insights": r["insights"],
                "proposal": r["proposal"],
                "refine_deltas": r["refine_deltas"],
                "elapsed_s": r["elapsed_s"],
                "metrics": metrics_of(
                    r["proposal"], r["research_items"], r["insights"],
                    _role_cost(_persona_label(r["persona"])),
                ),
            }
            for r in persona_results
        ],
        "diversity": diversity,
        "interview_guide_diversity": guide_diversity,
        "distinct_proposal_count": len(distinct_proposals),
        "cost_per_distinct_idea_usd": round(agent_cost / max(len(distinct_proposals), 1), 6),
        "timing": timing,
        "baseline": {"proposal": baseline_proposal, "metrics": baseline_metrics},
        "total_cost_usd": round(total_cost(), 6),
    }
    path = OUTPUT_DIR / f"stage3-run-{stamp}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUTPUT_DIR / "stage3-latest.json").write_text(
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
    if EVENTS_PATH.exists():
        EVENTS_PATH.unlink()

    topic = os.environ.get("BRAINSTORM_TOPIC", "如何提升新聞短影音互動率")
    personas = load_personas()
    users = load_users()
    company = load_company()

    print(f"主題：{topic}")
    print(f"Persona 人數：{len(personas)}；模擬使用者人數：{len(users)}")
    for p in personas:
        print(f"  - {p.get('name')}（{p.get('role')}）")
    print(f"事件流：{EVENTS_PATH}")
    print()

    print(f"=== A) Agent 路徑：{len(personas)} 位 persona 平行做功課＋訪談＋提案 ===")
    wall_t0 = time.perf_counter()
    agent_state = meeting_graph.invoke({
        "topic": topic,
        "company": company,
        "personas": personas,
        "users": users,
        "persona_results": [],
    })
    wall_elapsed = time.perf_counter() - wall_t0
    persona_results = agent_state["persona_results"]

    sequential_estimate = sum(r["elapsed_s"] for r in persona_results)
    speedup = sequential_estimate / wall_elapsed if wall_elapsed > 0 else 0.0
    timing = {
        "parallel_wall_s": round(wall_elapsed, 2),
        "sequential_estimate_s": round(sequential_estimate, 2),
        "speedup_x": round(speedup, 2),
    }

    print()
    print("=== B) Baseline：直接問 LLM 一次 ===")
    cost_before_baseline = total_cost()
    baseline_proposal = run_baseline(topic, company)
    baseline_cost = total_cost() - cost_before_baseline
    baseline_metrics = metrics_of(baseline_proposal, [], [], baseline_cost)
    baseline_metrics["real_citations"] = count_real_citations(baseline_proposal, [])

    proposals = [r["proposal"] for r in persona_results]
    diversity = pairwise_diversity(proposals)
    distinct_proposals = dedup_proposals(proposals)
    guide_diversity = pairwise_text_diversity([
        (r["persona"]["name"], guide_text(r["interview_guide"])) for r in persona_results
    ])

    out_path = save_outputs(
        topic=topic,
        personas=personas,
        users=users,
        persona_results=persona_results,
        baseline_proposal=baseline_proposal,
        baseline_metrics=baseline_metrics,
        diversity=diversity,
        guide_diversity=guide_diversity,
        distinct_proposals=distinct_proposals,
        timing=timing,
    )

    print()
    print("=== 驗收 ===")
    bmc_all_ok = True
    pov_hmw_all_ok = True
    insight_refs_all_ok = True
    for r in persona_results:
        proposal = r["proposal"]
        missing = assert_bmc_complete(proposal)
        real_refs = count_real_insight_refs(proposal, r["insights"])
        has_pov_hmw = bool(proposal.get("pov")) and bool(proposal.get("hmw"))
        cost = _role_cost(_persona_label(r["persona"]))
        print(
            f"  {r['persona']['name']:<6} BMC={'OK' if not missing else missing}  "
            f"POV/HMW={'OK' if has_pov_hmw else '缺'}  insight_refs真實引用={real_refs}  "
            f"self_score={proposal.get('self_score')}  耗時={r['elapsed_s']}s  cost=${cost:.4f}"
        )
        print(f"    HMW：{r['hmw']}")
        print(f"    訪談逐字稿：{len(r['interview_transcript'])} 筆；洞見：{len(r['insights'])} 則")
        bmc_all_ok = bmc_all_ok and not missing
        pov_hmw_all_ok = pov_hmw_all_ok and has_pov_hmw
        insight_refs_all_ok = insight_refs_all_ok and real_refs >= 1

    print()
    print(f"平行 wall-clock：{timing['parallel_wall_s']}s")
    print(f"循序估計：{timing['sequential_estimate_s']}s（加速 {timing['speedup_x']}x）")
    print()
    print(f"提案兩兩平均 embedding 距離：{diversity['avg_distance']}")
    print(f"訪綱兩兩平均 embedding 距離：{guide_diversity['avg_distance']}")
    for pair in guide_diversity["pairs"]:
        print(f"  {pair['a']} vs {pair['b']}: {pair['distance']}")
    print(f"去重後獨特提案數：{len(distinct_proposals)} / {len(proposals)}")
    agent_cost = total_cost() - baseline_metrics["cost_usd"]
    print(f"USD / 獨特提案：{agent_cost / max(len(distinct_proposals), 1):.4f}")
    print()
    print(f"已存檔：{out_path}")
    print(f"事件流：{EVENTS_PATH}（{sum(1 for _ in EVENTS_PATH.open())} 筆）")
    print_run_summary()

    # 硬驗收：結構不變量（BMC／POV-HMW／可驗證的洞見引用／平行真的比循序快）
    # 用 assert；多樣性夠不夠是敘事判斷，印出來給人看，不做成硬 gate
    # （跟 stage1 的 judge_third_round()、stage2 的 diversity 印出來同一種精神）。
    ok = (
        len(personas) >= 3
        and len(users) >= 2
        and bmc_all_ok
        and pov_hmw_all_ok
        and insight_refs_all_ok
        and len(persona_results) == len(personas)
        and all(len(r["interview_transcript"]) == len(users) * INTERVIEW_ROUNDS for r in persona_results)
        and timing["speedup_x"] > 1.0
        and guide_diversity["avg_distance"] > 0.05
        and out_path.exists()
        and EVENTS_PATH.exists()
    )
    if not ok:
        print("\n驗收未通過，請檢查上方輸出。")
        sys.exit(2)
    print("\n驗收通過。")


if __name__ == "__main__":
    main()
