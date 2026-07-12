"""
階段 9：三鏡檢核 + 最終報告（全員共同動作、完整會議報告）

目標：top-K 最終提案（Prototype/Test 之後的版本）交給全體 persona 做
「三鏡檢核」——正面／負面／洞見三種視角，這是**全員共同動作**，不是
某個人審核別人；跟 stage4 的同儕互評（發表當下、N-1 位審閱者評提案者）
是同一套 3/3/3 結構化協議，但套用的時機與對象不同：這裡是會議尾聲對
「已經定案的少數候選」做最後一輪、每個人（含自己）都要參與的品質關卡。
最後把整場會議的完整歷程（Facilitator 軌跡、人類問答、兩輪訪談逐字稿、
大師點評、集體評分、原型、三鏡檢核）組成一份可讀的 Markdown 報告。

對應 PLAN.md「階段 9」驗收：
- 三鏡檢核格式不變量通過（每筆恰好 3 正面／3 負面／3 洞見）
- 報告完整含人類問答與兩輪訪談記錄（Empathize 需求探索 + Test 概念驗證）

本檔是 stage 8 的完整獨立副本再擴充（不 import stage8）。做功課子圖、
同儕互評子圖、Facilitator、HITL、大師點評、Chroma 集體智慧、集體評分、
Prototype/Test 逐行沿用 stage8；新增的部分集中在 `run_prototype_test`
之後：`run_three_lens_check`（新子圖 `three_lens_panel_graph`）→ END，
再加上 `main()` 裡的 `build_final_report_markdown`（純組裝，不是圖節點，
因為報告需要 baseline 對照資料，那一直是在 `graph.invoke()` 完成之後
才在 `main()` 裡跑的，沿用既有設計，不把 baseline 硬塞進圖裡）。

執行前在 practice/.env 設定 ANTHROPIC_API_KEY（見 .env.example）。
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import html as html_lib
import itertools
import json
import math
import operator
import os
import re
import sqlite3
import sys
import time
import uuid
import warnings
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Annotated, Any, List, Literal, Optional, TypedDict

import anthropic
import chromadb
import yaml
from langchain_core._api.deprecation import LangChainPendingDeprecationWarning
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send, interrupt

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
CHECKPOINT_DB_PATH = OUTPUT_DIR / "stage9_checkpoints.sqlite"
CHROMA_DIR = PRACTICE_DIR / "chroma_db"
PROTOTYPE_DIR = OUTPUT_DIR / "prototypes"
REPORT_DIR = OUTPUT_DIR / "reports"

_env_file = PRACTICE_DIR / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _key, _, _value = _line.partition("=")
            os.environ.setdefault(_key.strip(), _value.strip())

CHEAP_MODEL = "claude-haiku-4-5-20251001"
SMART_MODEL = "claude-sonnet-5"
client = anthropic.Anthropic(timeout=90.0)  # stage7 真實跑測踩過 3+ 小時網路卡死，見 stage7 note

DEDUP_SIMILARITY_THRESHOLD = 0.80
IDEA_DEDUP_THRESHOLD = 0.75
EMBED_DIM = 256
REFINE_ROUNDS = 2
INTERVIEW_ROUNDS = 3

MAX_ROUNDS = 6
MAX_BUDGET_USD = 5.0

RECALL_N_RESULTS = 3
RECALL_MAX_DISTANCE = 1.0

TOP_K = 3  # 進入 Prototype/Test 階段的提案數上限

MASTERS = [
    {"id": "tech_master", "name": "技術大師", "angle": "技術可行性、架構與資料風險、能不能規模化執行"},
    {"id": "biz_master", "name": "商業大師", "angle": "商業模式健全度、單位經濟（誰付錢、成本結構撐不撐得住）"},
    {"id": "strategy_master", "name": "策略大師", "angle": "跟公司定位的契合度、長期護城河、機會成本"},
]

BMC_KEYS = [
    "客群", "價值主張", "通路", "顧客關係", "收益流", "關鍵資源", "關鍵活動", "關鍵夥伴", "成本結構",
]

usage_log: list = []
_event_role: ContextVar[str] = ContextVar("event_role", default="system")
_events_lock = Lock()

_chroma_client = None


def get_chroma_collection(name: str):
    global _chroma_client
    if _chroma_client is None:
        _chroma_client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    return _chroma_client.get_or_create_collection(name, metadata={"hnsw:space": "cosine"})


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
    real = PRACTICE_DIR / "personas.yaml"
    example = PRACTICE_DIR / "personas.example.yaml"
    path = real if real.exists() else example
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    personas = data["personas"]
    if not personas:
        raise ValueError(f"{path} 沒有 personas")
    return personas


def load_users() -> List[dict]:
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
    try:
        data = extract_json(text)
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _safe_str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def repair_json_text(bad_text: str) -> str:
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
# Embedding
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> List[str]:
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
    tavily_key = os.environ.get("TAVILY_API_KEY", "").strip()
    if tavily_key:
        return _search_tavily(query, tavily_key, max_results)
    return _search_ddg(query, max_results)


def is_usable_search_result(item: dict) -> bool:
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
    bmc = proposal.get("bmc") or {}
    parts = [
        proposal.get("title", ""),
        proposal.get("summary", ""),
        "\n".join(str(v) for v in bmc.values()),
    ]
    return "\n".join(parts)


def assert_bmc_complete(proposal: dict) -> List[str]:
    bmc = proposal.get("bmc") or {}
    issues = []
    for key in BMC_KEYS:
        val = bmc.get(key)
        if not isinstance(val, str) or not val.strip():
            issues.append(f"缺漏或無效:{key}")
    issues.extend(f"額外欄位:{key}" for key in bmc if key not in BMC_KEYS)
    return issues


def count_real_citations(proposal: dict, research_items: List[dict]) -> int:
    known = {item.get("url", "").rstrip("/") for item in research_items if item.get("url")}
    n = 0
    for src in proposal.get("sources") or []:
        url = (src.get("url") or "").rstrip("/")
        if url and url in known:
            n += 1
    return n


def count_real_insight_refs(proposal: dict, insights: List[dict]) -> int:
    known = {i.get("id") for i in insights if i.get("id")}
    refs = proposal.get("insight_refs") or []
    return sum(1 for r in refs if r in known)


def count_real_memory_refs(proposal: dict, recalled_memory: List[dict]) -> int:
    known = {m.get("id") for m in recalled_memory if m.get("id")}
    refs = proposal.get("memory_refs") or []
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
        "has_pov_hmw": bool(_safe_str(proposal.get("pov"))) and bool(_safe_str(proposal.get("hmw"))),
        "self_score": proposal.get("self_score"),
        "cost_usd": round(cost, 6),
    }


# ---------------------------------------------------------------------------
# 做功課子圖 state（逐行同 stage 7）
# ---------------------------------------------------------------------------

class HomeworkState(TypedDict):
    topic: str
    persona: dict
    company: str
    users: List[dict]
    round_id: str
    raw_results: List[dict]
    research_items: List[dict]
    research_brief: str
    recalled_memory: List[dict]
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
        except Exception as exc:  # noqa: BLE001
            print(f"  [collect:{persona.get('name')}] query={q!r} 失敗：{exc}")
    emit_event(
        "collect",
        f"搜尋 {len(queries)} 組查詢，共 {len(raw)} 筆原始結果",
        # 使用者要求能追溯「看了哪些網頁才形成後面的想法」——之前這裡只記
        # 筆數，真正的 title/url/snippet 只活在記憶體裡的 raw_results，
        # events.jsonl／存檔 JSON 完全沒有任何 URL。這裡把完整結果放進
        # extra，畫面才有東西可以呈現研究足跡。
        extra={"queries": queries, "n_results": len(raw), "results": raw},
    )
    return {"raw_results": raw}


def dedup(state: HomeworkState) -> dict:
    raw = state["raw_results"]
    items = dedup_by_embedding(raw)
    print(f"  [dedup:{state['persona'].get('name')}] {len(raw)} → {len(items)}（門檻 {DEDUP_SIMILARITY_THRESHOLD}）")
    # 這份 items 就是真正流進 synthesize()/draft_proposal() 的
    # research_items，是「最後真的形成想法依據」的那份清單，不是
    # collect() 那份含重複的原始結果——兩邊都留著方便對照篩掉了什麼。
    emit_event("dedup", f"embedding 去重 {len(raw)} → {len(items)}", extra={"items": items})
    return {"research_items": items}


def recall_memory(state: HomeworkState) -> dict:
    persona = state["persona"]
    topic = state["topic"]
    round_id = state["round_id"]
    query_text = f"{topic} {', '.join(persona.get('focus') or [])}"
    hits: List[dict] = []
    for col_name in ("wisdom", "interviews"):
        col = get_chroma_collection(col_name)
        count = col.count()
        if count == 0:
            continue
        n = min(RECALL_N_RESULTS * 3, count)
        res = col.query(query_texts=[query_text], n_results=n)
        for id_, doc, meta, dist in zip(
            res["ids"][0], res["documents"][0], res["metadatas"][0], res["distances"][0]
        ):
            if meta.get("round_id") == round_id:
                continue
            if dist <= RECALL_MAX_DISTANCE:
                hits.append({
                    "id": id_, "text": doc, "collection": col_name,
                    "round_id": meta.get("round_id"), "topic": meta.get("topic"),
                    "distance": round(dist, 4),
                })
    hits.sort(key=lambda h: h["distance"])
    hits = hits[:RECALL_N_RESULTS]
    emit_event(
        "recall_memory",
        f"{persona.get('name')} 命中 {len(hits)} 筆跨輪記憶（query={query_text!r}）",
        extra={"hits": hits},
    )
    print(f"  [recall:{persona.get('name')}] query={query_text!r} → {len(hits)} 筆命中")
    return {"recalled_memory": hits}


def synthesize(state: HomeworkState) -> dict:
    persona = state["persona"]
    items = state["research_items"]
    bullet = "\n".join(
        f"- {it.get('title')} | {it.get('url')}\n  {it.get('snippet', '')[:240]}"
        for it in items
    ) or "（無搜尋結果）"
    memory = state["recalled_memory"]
    memory_block = "\n".join(f"- [{m['id']}] {m['text']}" for m in memory) or "（本輪查無相關的跨輪記憶）"
    system = (
        f"你是 {persona['name']}（{persona.get('role', '')}）。"
        f"背景：{persona.get('background', '')}。"
        f"關注：{', '.join(persona.get('focus') or [])}。"
        f"發言風格：{persona.get('style', '')}。"
        "請根據搜尋素材寫一份精簡研究彙整，必須標注可追溯的 URL。"
        "如果下面列出的跨輪記憶跟主題相關，可以引用（提到對應的 id）；不相關就不要硬拗。"
        "只輸出正文，不要 JSON。"
    )
    user = (
        f"會議主題：{state['topic']}\n\n"
        f"自家公司定位：\n{state['company']}\n\n"
        f"過去輪次的相關記憶：\n{memory_block}\n\n"
        f"搜尋素材：\n{bullet}\n\n"
        "請輸出：1) 市場／競品觀察 2) 對自家有利的切入點 3) 風險與未知。"
    )
    brief = call_llm(SMART_MODEL, system, user, max_tokens=2000)
    emit_event("synthesize", f"彙整 brief {len(brief)} 字，素材 {len(items)} 筆，跨輪記憶 {len(memory)} 筆")
    return {"research_brief": brief}


def design_interview_guide(state: HomeworkState) -> dict:
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
    raw = call_llm(SMART_MODEL, system, user, max_tokens=500)
    data = extract_json_object(raw)
    questions = [q for q in (data.get("questions") or []) if isinstance(q, str) and q.strip()]
    if not questions:
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
    system = _user_system_prompt(user)
    history = "\n".join(f"Q: {t['question']}\nA: {t['answer']}" for t in prior_turns) or "（尚無先前對話）"
    prompt = f"先前對話：\n{history}\n\n新問題：{question}"
    return call_llm(SMART_MODEL, system, prompt, max_tokens=300).strip()


def generate_followup_question(persona: dict, prior_turns: List[dict]) -> str:
    last_answer = prior_turns[-1]["answer"]
    history = "\n".join(f"Q: {t['question']}\nA: {t['answer']}" for t in prior_turns)
    system = (
        f"你是 {persona['name']}，正在做用戶需求訪談（探索階段，不能提任何點子）。"
        "根據對方剛剛的回答，問一個更深入的追問，聚焦在痛點或情境細節。"
        "只輸出問題本身（<=30 字），不要加解說或引號。"
    )
    prompt = f"訪談記錄：\n{history}\n\n對方剛回答：{last_answer}\n\n下一個追問？"
    question = call_llm(SMART_MODEL, system, prompt, max_tokens=150).strip()
    return question or f"能不能多說一點「{last_answer[:15]}」這件事？"


def conduct_interviews(state: HomeworkState) -> dict:
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
    raw = call_llm(SMART_MODEL, system, user, max_tokens=1200)
    data = extract_json_object(raw)
    if not data:
        data = extract_json_object(repair_json_text(raw))
    raw_insights = [
        i for i in (data.get("insights") or [])
        if isinstance(i, dict) and (i.get("text") or "").strip()
    ]
    if not raw_insights:
        raw_insights = [{"text": f"{t['user_name']}：{t['answer'][:50]}"} for t in transcript[:2]]
    insights = [{"id": f"i{n}", "text": it["text"].strip()} for n, it in enumerate(raw_insights, 1)]
    emit_event("extract_insights", f"萃取 {len(insights)} 則洞見", extra={"insights": insights})
    return {"insights": insights}


def write_pov_hmw(state: HomeworkState) -> dict:
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
    raw = call_llm(SMART_MODEL, system, user, max_tokens=900)
    data = extract_json_object(raw)
    pov = _safe_str(data.get("pov"))
    hmw = _safe_str(data.get("hmw"))
    if not pov or not hmw:
        top = insights[0]["text"] if insights else state["topic"]
        pov = pov or f"用戶需要更好的方式面對「{top}」這個處境。"
        hmw = hmw or f"我們可以怎麼協助用戶解決「{top}」？"
    emit_event("write_pov_hmw", f"POV：{pov} / HMW：{hmw}", extra={"pov": pov, "hmw": hmw})
    return {"pov": pov, "hmw": hmw}


_PROPOSAL_SCHEMA_HINT = f"""
請只輸出一個 JSON 物件（不要 markdown 圍欄），欄位：
- title: string（<=40字）
- summary: string（2-3句，<=120字）
- hmw_response: string（<=60字，說明這個提案怎麼回應你的 HMW）
- insight_refs: [string] 1-3 筆，必須是提供的訪談洞見 id（例如 "i1"），不能捏造
- memory_refs: [string] 0-2 筆，只有在有相關跨輪記憶時才填，必須是提供的記憶 id，沒有相關的就給空陣列，不能捏造
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
    data.setdefault("memory_refs", [])
    data.setdefault("self_score", 0)
    try:
        data["self_score"] = float(data["self_score"])
    except (TypeError, ValueError):
        data["self_score"] = 0.0
    return data


def _request_proposal(system: str, user: str) -> dict:
    raw = call_llm(SMART_MODEL, system, user, max_tokens=2500)
    try:
        return _parse_proposal(raw)
    except ValueError:
        print("  [proposal] JSON 失敗，改以精簡指令重試…")
        compact_system = (
            "只輸出精簡合法 JSON object，不要 markdown。"
            + _PROPOSAL_SCHEMA_HINT
        )
        raw2 = call_llm(SMART_MODEL, compact_system, user[:6000], max_tokens=2500)
        return _parse_proposal(raw2)


def _ensure_hmw_fields(proposal: dict, state: HomeworkState) -> dict:
    proposal["pov"] = state["pov"]
    proposal["hmw"] = state["hmw"]
    insights = state["insights"]
    known_ids = {i.get("id") for i in insights if i.get("id")}
    refs = [r for r in (proposal.get("insight_refs") or []) if r in known_ids]
    if not refs and insights:
        refs = [insights[0]["id"]]
    proposal["insight_refs"] = refs
    known_mem_ids = {m.get("id") for m in state["recalled_memory"] if m.get("id")}
    proposal["memory_refs"] = [r for r in (proposal.get("memory_refs") or []) if r in known_mem_ids]
    if not _safe_str(proposal.get("hmw_response")):
        proposal["hmw_response"] = f"（系統保底）呼應 HMW：{state['hmw'][:50]}"
    return proposal


def draft_proposal(state: HomeworkState) -> dict:
    persona = state["persona"]
    items = state["research_items"]
    insights = state["insights"]
    memory = state["recalled_memory"]
    sources_block = "\n".join(
        f"- {it.get('title')} | {it.get('url')}\n  {it.get('snippet', '')[:200]}"
        for it in items
    ) or "（無）"
    insights_block = "\n".join(f"- [{i['id']}] {i['text']}" for i in insights) or "（無）"
    memory_block = "\n".join(f"- [{m['id']}] {m['text']}" for m in memory) or "（本輪查無相關跨輪記憶）"
    system = (
        f"你是 {persona['name']}（{persona.get('role', '')}），正在腦力激盪會議提案。"
        f"你先前訪談用戶後定義的 HMW 是：「{state['hmw']}」——提案必須明確回應這個 HMW，"
        "不能是跟訪談洞見無關的天外飛來一筆。"
        "提案必須引用提供的真實搜尋 URL，禁止捏造連結。insight_refs 只能引用下面列出的洞見 id。"
        "memory_refs 只有在真的用到跨輪記憶時才填，不相關就給空陣列。"
        "BMC 九格必須齊全。"
        + _PROPOSAL_SCHEMA_HINT
    )
    user = (
        f"主題：{state['topic']}\n\n公司：\n{state['company']}\n\n"
        f"POV：{state['pov']}\nHMW：{state['hmw']}\n\n"
        f"可引用的訪談洞見：\n{insights_block}\n\n"
        f"可引用的跨輪記憶：\n{memory_block}\n\n"
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
        f"insight_refs={proposal.get('insight_refs')} memory_refs={proposal.get('memory_refs')}",
        # 使用者要求 refine 完形成提案當下就能看到完整提案跟 BMC，不用等到
        # facilitator 選中發表或會議跑完看回放——這裡的 proposal 這時已經
        # 通過上面的 assert_bmc_complete()，直接整包放進 extra，即時畫面/
        # 回放器才有資料可以渲染完整內容，不用再等 present 事件。
        extra={"bmc_missing": assert_bmc_complete(proposal), "proposal": proposal},
    )
    return {
        "proposal": proposal,
        "proposal_versions": [proposal],
        "refine_round": 0,
    }


def refine(state: HomeworkState) -> dict:
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
        "產出強化後的完整提案 JSON。url 仍只能來自可用來源，insight_refs/memory_refs 仍只能引用提供的 id。"
        + _PROPOSAL_SCHEMA_HINT
    )
    user = (
        f"主題：{state['topic']}\n\n研究彙整（節錄）：\n{state['research_brief'][:1200]}\n\n"
        f"可用來源：\n{sources_block}\n\n"
        f"可引用的訪談洞見：\n" + "\n".join(f"- [{i['id']}] {i['text']}" for i in state['insights']) + "\n\n"
        f"可引用的跨輪記憶：\n" + "\n".join(f"- [{m['id']}] {m['text']}" for m in state['recalled_memory']) + "\n\n"
        f"上一版提案 JSON：\n{json.dumps(prev, ensure_ascii=False)}"
    )
    nxt = _request_proposal(system, user)
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
        # 使用者要求看到「agents 具體的成長弧線」，不是只有分數——
        # _diff_proposals() 已經在 generate_prototype_and_test() 驗證過
        # 能用（graph.py 原本的測試後修正那段），這裡純接線、不用多花
        # LLM 成本或新寫 diff 邏輯。
        "diff_text": _diff_proposals(prev, nxt),
        # 同上：使用者要求 refine 完當下就看得到完整提案跟 BMC，不是只有
        # diff/分數——diff 回答「改了什麼」，完整提案回答「現在長怎樣」，
        # 兩者互補，缺一不可。
        "proposal": nxt,
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
    g = StateGraph(HomeworkState)
    g.add_node("collect", instrument("collect", collect))
    g.add_node("dedup", instrument("dedup", dedup))
    g.add_node("recall_memory", instrument("recall_memory", recall_memory))
    g.add_node("synthesize", instrument("synthesize", synthesize))
    g.add_node("design_interview_guide", instrument("design_interview_guide", design_interview_guide))
    g.add_node("conduct_interviews", instrument("conduct_interviews", conduct_interviews))
    g.add_node("extract_insights", instrument("extract_insights", extract_insights))
    g.add_node("write_pov_hmw", instrument("write_pov_hmw", write_pov_hmw))
    g.add_node("draft_proposal", instrument("draft_proposal", draft_proposal))
    g.add_node("refine", instrument("refine", refine))
    g.add_edge(START, "collect")
    g.add_edge("collect", "dedup")
    g.add_edge("dedup", "recall_memory")
    g.add_edge("recall_memory", "synthesize")
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
# 同儕互評子圖（逐行同 stage7）
# ---------------------------------------------------------------------------

_REVISION_SCHEMA_HINT = f"""
請只輸出一個 JSON 物件（不要 markdown 圍欄），欄位：
- title, summary, sources, self_score, score_reason：跟原提案同格式
- bmc: 物件，鍵必須恰好包含且僅包含這九個：
  {json.dumps(BMC_KEYS, ensure_ascii=False)}
- revision_note: string（<=80字，具體說明改了什麼、回應了哪些人的異議，不能空泛帶過）
- addressed_reviewer_ids: [string] 1-3 筆，必須是收到意見的審閱者 id，不能捏造
務必輸出精簡合法 JSON。
"""


class ReviewTask(TypedDict):
    reviewer: dict
    presenter_name: str
    proposal: dict
    qa_context: List[dict]


class ReviewRoundState(TypedDict):
    presenter_id: str
    presenter_name: str
    proposal: dict
    reviewers: List[dict]
    qa_context: List[dict]
    reviews: Annotated[List[dict], operator.add]
    revised_proposal: dict


def _pad_to_three(items: Optional[list], filler_prefix: str) -> List[str]:
    clean = [x for x in (items or []) if isinstance(x, str) and x.strip()][:3]
    while len(clean) < 3:
        clean.append(f"{filler_prefix} #{len(clean) + 1}（系統保底，模型未提供足夠項目）")
    return clean[:3]


def _ensure_review_shape(data: dict, reviewer: dict, presenter_name: str) -> dict:
    return {
        "reviewer_id": reviewer.get("id"),
        "reviewer_name": reviewer.get("name"),
        "presenter_name": presenter_name,
        "agreements": _pad_to_three(data.get("agreements"), "認同"),
        "disagreements": _pad_to_three(data.get("disagreements"), "異議"),
        "insights": _pad_to_three(data.get("insights"), "洞見"),
        "hmw_addressed": data.get("hmw_addressed") if isinstance(data.get("hmw_addressed"), bool) else None,
        "hmw_addressed_reason": _safe_str(data.get("hmw_addressed_reason")),
    }


def fan_out_reviewers(state: ReviewRoundState) -> List[Send]:
    return [
        Send("give_feedback", {
            "reviewer": reviewer,
            "presenter_name": state["presenter_name"],
            "proposal": state["proposal"],
            "qa_context": state["qa_context"],
        })
        for reviewer in state["reviewers"]
    ]


def give_feedback(task: ReviewTask) -> dict:
    reviewer = task["reviewer"]
    presenter_name = task["presenter_name"]
    proposal = task["proposal"]
    qa_context = task.get("qa_context") or []
    role_token = _event_role.set(_persona_label(reviewer))
    qa_block = (
        "\n".join(f"- 人類問：{q['question']}\n  {presenter_name} 答：{q['answer']}" for q in qa_context)
        if qa_context else "（沒有人類提問，發表後直接進入互評）"
    )
    system = (
        f"你是 {reviewer['name']}（{reviewer.get('role', '')}），背景：{reviewer.get('background', '')}。"
        f"正在聽 {presenter_name} 發表提案，要給結構化意見。發表後的人類問答（如果有）也要納入考量。"
        "只輸出 JSON：{\"agreements\": [恰好3則，每則<=30字], "
        "\"disagreements\": [恰好3則，每則<=30字，要具體可執行，不要空泛], "
        "\"insights\": [恰好3則，每則<=30字], "
        "\"hmw_addressed\": true 或 false, \"hmw_addressed_reason\": \"<=40字\"}。"
    )
    user = (
        f"提案標題：{proposal.get('title')}\n摘要：{proposal.get('summary')}\n"
        f"HMW：{proposal.get('hmw')}\nHMW 回應說明：{proposal.get('hmw_response')}\n"
        f"BMC：{json.dumps(proposal.get('bmc'), ensure_ascii=False)}\n\n"
        f"發表後的人類問答：\n{qa_block}"
    )
    try:
        raw = call_llm(SMART_MODEL, system, user, max_tokens=700)
        data = extract_json_object(raw)
        review = _ensure_review_shape(data, reviewer, presenter_name)
    finally:
        _event_role.reset(role_token)
    emit_event(
        "give_feedback",
        f"{reviewer['name']} 評 {presenter_name}：hmw_addressed={review['hmw_addressed']}",
        role=_persona_label(reviewer),
        extra=review,
    )
    return {"reviews": [review]}


def _ensure_revision_fields(revised: dict, original: dict, reviews: List[dict]) -> dict:
    valid_ids = {r["reviewer_id"] for r in reviews if r.get("reviewer_id")}
    name_to_id = {r["reviewer_name"]: r["reviewer_id"] for r in reviews if r.get("reviewer_name")}

    revised.setdefault("sources", original.get("sources") or [])
    revised.setdefault("bmc", {})
    try:
        revised["self_score"] = float(revised.get("self_score", original.get("self_score", 0)))
    except (TypeError, ValueError):
        revised["self_score"] = float(original.get("self_score") or 0)
    revised["pov"] = original.get("pov", "")
    revised["hmw"] = original.get("hmw", "")
    revised["insight_refs"] = original.get("insight_refs", [])
    revised["memory_refs"] = original.get("memory_refs", [])
    if not _safe_str(revised.get("hmw_response")):
        revised["hmw_response"] = original.get("hmw_response", "")

    resolved: List[str] = []
    for item in revised.get("addressed_reviewer_ids") or []:
        rid = item if item in valid_ids else name_to_id.get(item)
        if rid and rid not in resolved:
            resolved.append(rid)
    if not resolved and valid_ids:
        resolved = [sorted(valid_ids)[0]]
    revised["addressed_reviewer_ids"] = resolved

    if not _safe_str(revised.get("revision_note")):
        revised["revision_note"] = "（系統保底）已依審閱意見微調提案。"
    return revised


def revise_after_feedback(state: ReviewRoundState) -> dict:
    presenter_name = state["presenter_name"]
    proposal = state["proposal"]
    reviews = state["reviews"]
    role_token = _event_role.set(f"persona:{presenter_name}")
    reviews_block = "\n".join(
        f"[{r['reviewer_id']}:{r['reviewer_name']}] "
        f"異議：{r['disagreements']} / 洞見：{r['insights']}"
        for r in reviews
    )
    system = (
        f"你是 {presenter_name}，剛發表完提案，聽取了其他與會者的意見。"
        "請針對『異議』做出實質修正，不能左耳進右耳出、也不能整份重寫到面目全非。"
        + _REVISION_SCHEMA_HINT
    )
    user = f"你的提案：\n{json.dumps(proposal, ensure_ascii=False)}\n\n收到的意見：\n{reviews_block}"
    try:
        raw = call_llm(SMART_MODEL, system, user, max_tokens=2000)
        data = extract_json_object(raw)
        if not data:
            data = extract_json_object(repair_json_text(raw))
        revised = _ensure_revision_fields(data, proposal, reviews)
        candidate_bmc = revised.get("bmc") or {}
        orig_bmc = proposal.get("bmc") or {}
        revised["bmc"] = {
            key: candidate_bmc.get(key)
            if isinstance(candidate_bmc.get(key), str) and candidate_bmc[key].strip()
            else orig_bmc.get(key, "")
            for key in BMC_KEYS
        }
        if issues := assert_bmc_complete(revised):
            raise ValueError(f"BMC 結構不變量失敗：{issues}")
    finally:
        _event_role.reset(role_token)
    emit_event(
        "revise_after_feedback",
        f"{presenter_name} 修正：{revised.get('revision_note')}",
        role=f"persona:{presenter_name}",
        extra={"addressed_reviewer_ids": revised.get("addressed_reviewer_ids")},
    )
    return {"revised_proposal": revised}


def build_review_round_subgraph():
    g = StateGraph(ReviewRoundState)
    g.add_node("give_feedback", instrument("give_feedback", give_feedback))
    g.add_node("revise_after_feedback", instrument("revise_after_feedback", revise_after_feedback))
    g.add_conditional_edges(START, fan_out_reviewers, ["give_feedback"])
    g.add_edge("give_feedback", "revise_after_feedback")
    g.add_edge("revise_after_feedback", END)
    return g.compile()


review_round_graph = build_review_round_subgraph()


# ---------------------------------------------------------------------------
# 大師點評子圖（逐行同 stage7）
# ---------------------------------------------------------------------------

class MasterTask(TypedDict):
    master: dict
    topic: str
    idea_pool_summary: str


class MasterPanelState(TypedDict):
    topic: str
    idea_pool_summary: str
    critiques: Annotated[List[dict], operator.add]


def fan_out_masters(state: MasterPanelState) -> List[Send]:
    return [
        Send("master_critique", {
            "master": m, "topic": state["topic"], "idea_pool_summary": state["idea_pool_summary"],
        })
        for m in MASTERS
    ]


def master_critique(task: MasterTask) -> dict:
    master = task["master"]
    role_token = _event_role.set(f"master:{master['name']}")
    system = (
        f"你是{master['name']}，用「{master['angle']}」的角度審視整場腦力激盪會議收斂出的"
        "最終提案組合。只輸出 JSON：{\"critique\": \"<=150字，具體點名哪個提案在你的角度上"
        "最強／最弱，不要空泛通則\", \"top_pick_persona\": \"你認為最值得往下推進的 persona 名字\"}。"
    )
    user = f"主題：{task['topic']}\n\n最終提案組合：\n{task['idea_pool_summary']}"
    try:
        raw = call_llm(SMART_MODEL, system, user, max_tokens=2000)
        data = extract_json_object(raw)
    finally:
        _event_role.reset(role_token)
    critique = _safe_str(data.get("critique")) or "（系統保底）提供的資訊不足以評論。"
    top_pick = _safe_str(data.get("top_pick_persona"))
    result = {
        "master_id": master["id"], "master_name": master["name"], "angle": master["angle"],
        "critique": critique, "top_pick_persona": top_pick,
    }
    emit_event("master_critique", f"{master['name']}：{critique[:60]}", role=f"master:{master['name']}", extra=result)
    print(f"  [master:{master['name']}] {critique}")
    return {"critiques": [result]}


def build_master_panel_subgraph():
    g = StateGraph(MasterPanelState)
    g.add_node("master_critique", instrument("master_critique", master_critique))
    g.add_conditional_edges(START, fan_out_masters, ["master_critique"])
    g.add_edge("master_critique", END)
    return g.compile()


master_panel_graph = build_master_panel_subgraph()


# ---------------------------------------------------------------------------
# 集體智慧庫（Chroma 寫入，逐行同 stage7）
# ---------------------------------------------------------------------------

def write_collective_wisdom(
    *,
    round_id: str,
    topic: str,
    master_critiques: List[dict],
    idea_pool_versions: List[dict],
    persona_results: List[dict],
    three_lens_checks: List[dict],
) -> dict:
    # 使用者要求把各環節的 insight 也寫進 RAG，日後能重用——之前只寫
    # 大師點評跟最終提案標題摘要跟訪談洞見，POV/HMW、同儕互評內容、三鏡
    # 檢核結果、完整訪談逐字稿都是「算出來卻從沒進 RAG」。刻意不把原始
    # 搜尋結果/URL 寫進來——那是雜訊不是洞見，項目1的「研究足跡」是給人
    # 看的追溯功能，跟「該不該讓未來會議語意檢索到」是兩回事。
    now = datetime.now(timezone.utc).isoformat()
    wisdom_col = get_chroma_collection("wisdom")
    docs, metas, ids = [], [], []
    for m in master_critiques:
        docs.append(f"[{m['master_name']}] {m['critique']}")
        metas.append({
            "round_id": round_id, "topic": topic, "doc_type": "master_critique",
            "master_id": m["master_id"], "created_at": now,
        })
        ids.append(f"{round_id}-master-{m['master_id']}")
    final_by_persona: dict = {}
    for v in idea_pool_versions:
        final_by_persona[v["persona_id"]] = v
    for pid, v in final_by_persona.items():
        p = v["proposal_after"]
        docs.append(f"《{p.get('title', '')}》{p.get('summary', '')}")
        metas.append({
            "round_id": round_id, "topic": topic, "doc_type": "final_proposal",
            "persona": v["persona_name"], "created_at": now,
        })
        ids.append(f"{round_id}-proposal-{pid}")

    for r in persona_results:
        pid = r["persona"]["id"]
        pname = r["persona"]["name"]
        pov, hmw = r.get("pov"), r.get("hmw")
        if pov or hmw:
            docs.append(f"{pname} 的 POV：{pov} / HMW：{hmw}")
            metas.append({
                "round_id": round_id, "topic": topic, "doc_type": "pov_hmw",
                "persona": pname, "created_at": now,
            })
            ids.append(f"{round_id}-povhmw-{pid}")

    for version_idx, v in enumerate(idea_pool_versions):
        presenter_pid = v["persona_id"]
        for review in v.get("reviews") or []:
            reviewer_id = review.get("reviewer_id", "?")
            agreements = "；".join(review.get("agreements") or [])
            disagreements = "；".join(review.get("disagreements") or [])
            insights = "；".join(review.get("insights") or [])
            docs.append(
                f"{review.get('reviewer_name')} 對 {review.get('presenter_name')} 的互評："
                f"認同：{agreements}｜異議：{disagreements}｜洞見：{insights}"
            )
            metas.append({
                "round_id": round_id, "topic": topic, "doc_type": "peer_review",
                "reviewer": review.get("reviewer_name"), "presenter": review.get("presenter_name"),
                "created_at": now,
            })
            # 同一位 presenter 可能在多輪討論中被同一批人重複審閱（facilitator
            # 讓她「加輪」回應異議時，會再跑一次 run_peer_review）——(reviewer,
            # presenter) 這組 key 因此不保證唯一，真實跑測就撞過
            # DuplicateIDError。version_idx 是 idea_pool_versions 裡的順位，
            # 用它加進 id 保留每一輪的獨立內容，而不是覆蓋掉後面幾輪的回饋。
            ids.append(f"{round_id}-review-{reviewer_id}-{presenter_pid}-v{version_idx}")

    for c in three_lens_checks:
        positive = "；".join(c.get("positive") or [])
        negative = "；".join(c.get("negative") or [])
        insight = "；".join(c.get("insight") or [])
        docs.append(
            f"{c.get('persona_name')} 對 {c.get('target_persona_name') or c.get('target_persona_id')} 的三鏡檢核："
            f"正面：{positive}｜負面：{negative}｜洞見：{insight}"
        )
        metas.append({
            "round_id": round_id, "topic": topic, "doc_type": "three_lens_check",
            "persona": c.get("persona_name"), "target": c.get("target_persona_name") or c.get("target_persona_id"),
            "created_at": now,
        })
        ids.append(f"{round_id}-threelens-{c.get('persona_id')}-{c.get('target_persona_id')}")

    if docs:
        wisdom_col.add(documents=docs, metadatas=metas, ids=ids)

    interviews_col = get_chroma_collection("interviews")
    idocs, imetas, iids = [], [], []
    for r in persona_results:
        pid = r["persona"]["id"]
        pname = r["persona"]["name"]
        for insight in r.get("insights") or []:
            idocs.append(insight["text"])
            imetas.append({
                "round_id": round_id, "topic": topic, "doc_type": "interview_insight",
                "persona": pname, "created_at": now,
            })
            iids.append(f"{round_id}-insight-{pid}-{insight['id']}")
        for turn in r.get("interview_transcript") or []:
            idocs.append(f"[{turn.get('user_name')}] Q：{turn.get('question')} / A：{turn.get('answer')}")
            imetas.append({
                "round_id": round_id, "topic": topic, "doc_type": "interview_transcript",
                "persona": pname, "user": turn.get("user_name"), "created_at": now,
            })
            iids.append(f"{round_id}-transcript-{pid}-{turn.get('user_id')}-r{turn.get('round')}")
    if idocs:
        interviews_col.add(documents=idocs, metadatas=imetas, ids=iids)

    return {"wisdom_written": len(docs), "interviews_written": len(idocs)}


# ---------------------------------------------------------------------------
# 集體評分子圖（新，stage8 核心之一）
# ---------------------------------------------------------------------------

class ScoringTask(TypedDict):
    rater: dict
    target_persona_id: str
    target_persona_name: str
    proposal: dict


class ScoringPanelState(TypedDict):
    personas: List[dict]
    final_proposals: dict  # persona_id -> proposal
    scores: Annotated[List[dict], operator.add]


def fan_out_scoring(state: ScoringPanelState) -> List[Send]:
    """N 位 persona 交叉評分彼此的最終提案——不評自己的，這是『多 agent
    判斷聚合』的核心：最後的排名不是任何單一 agent（包括 Facilitator／
    大師）說了算，是全體評分平均出來的。"""
    id_to_name = {p.get("id"): p.get("name") for p in state["personas"]}
    sends = []
    for rater in state["personas"]:
        for pid, proposal in state["final_proposals"].items():
            if pid == rater.get("id"):
                continue
            sends.append(Send("score_proposal", {
                "rater": rater, "target_persona_id": pid,
                "target_persona_name": id_to_name.get(pid, pid), "proposal": proposal,
            }))
    return sends


def score_proposal(task: ScoringTask) -> dict:
    rater = task["rater"]
    proposal = task["proposal"]
    role_token = _event_role.set(_persona_label(rater))
    system = (
        f"你是 {rater['name']}（{rater.get('role', '')}）。針對這個提案給 1-10 分的整體評分"
        "（可行性、影響力、跟公司定位契合度綜合考量，不用手軟）。"
        "只輸出 JSON：{\"score\": 數字, \"reason\": \"<=40字\"}"
    )
    user = (
        f"標題：{proposal.get('title')}\n摘要：{proposal.get('summary')}\n"
        f"BMC：{json.dumps(proposal.get('bmc'), ensure_ascii=False)}"
    )
    try:
        raw = call_llm(CHEAP_MODEL, system, user, max_tokens=400)
        data = extract_json_object(raw)
    finally:
        _event_role.reset(role_token)
    try:
        score = float(data.get("score"))
        score = max(1.0, min(10.0, score))
    except (TypeError, ValueError):
        score = 5.0  # 保底中位數，解析失敗不能讓平均分被污染成 0
    reason = _safe_str(data.get("reason")) or "（系統保底）評分理由缺失。"
    target_name = task.get("target_persona_name") or task["target_persona_id"]
    result = {
        "rater_id": rater.get("id"), "rater_name": rater.get("name"),
        "target_persona_id": task["target_persona_id"], "target_persona_name": target_name,
        "score": score, "reason": reason,
    }
    emit_event(
        "score_proposal", f"{rater['name']} 評 {target_name}: {score}",
        role=_persona_label(rater), extra=result,
    )
    return {"scores": [result]}


def build_scoring_panel_subgraph():
    g = StateGraph(ScoringPanelState)
    g.add_node("score_proposal", instrument("score_proposal", score_proposal))
    g.add_conditional_edges(START, fan_out_scoring, ["score_proposal"])
    g.add_edge("score_proposal", END)
    return g.compile()


scoring_panel_graph = build_scoring_panel_subgraph()


def compute_score_aggregates(scores: List[dict], persona_ids: List[str]) -> dict:
    """回傳每個提案的平均分＋標準差——標準差就是驗收要的『分歧度數字』：
    數字越大代表評分者意見越分歧，不是只看平均分排名。"""
    by_target: dict = {pid: [] for pid in persona_ids}
    for s in scores:
        tid = s["target_persona_id"]
        if tid in by_target:
            by_target[tid].append(s["score"])
    aggregates = {}
    for pid, vals in by_target.items():
        if not vals:
            aggregates[pid] = {"mean": 0.0, "stdev": 0.0, "n": 0}
            continue
        mean = sum(vals) / len(vals)
        var = sum((v - mean) ** 2 for v in vals) / len(vals)
        aggregates[pid] = {"mean": round(mean, 3), "stdev": round(var ** 0.5, 3), "n": len(vals)}
    return aggregates


def select_top_k(aggregates: dict, k: int) -> List[str]:
    ranked = sorted(aggregates.items(), key=lambda kv: kv[1]["mean"], reverse=True)
    return [pid for pid, _ in ranked[:k]]


# ---------------------------------------------------------------------------
# Prototype + Test 子圖（新，stage8 核心之二）
# ---------------------------------------------------------------------------

_LANDING_PAGE_SCHEMA_HINT = """
只輸出 JSON：{
  "headline": "<=20字，主標",
  "subheadline": "<=40字，副標",
  "features": [3-4 個 {"title":"<=10字","desc":"<=30字"}],
  "cta_text": "<=10字，行動呼籲按鈕文字",
  "concept_one_pager": "<=400字，一頁概念書：價值主張／用戶旅程／storyboard 文字"
}
"""


def _ensure_landing_page_fields(data: dict, proposal: dict) -> dict:
    data["headline"] = _safe_str(data.get("headline")) or (proposal.get("title") or "")[:20]
    data["subheadline"] = _safe_str(data.get("subheadline")) or (proposal.get("summary") or "")[:40]
    clean_features = []
    features = data.get("features")
    if isinstance(features, list):
        for f in features:
            if isinstance(f, dict) and _safe_str(f.get("title")):
                clean_features.append({"title": _safe_str(f.get("title")), "desc": _safe_str(f.get("desc"))})
    if not clean_features:
        bmc = proposal.get("bmc") or {}
        clean_features = [{"title": k, "desc": v} for k, v in list(bmc.items())[:3] if isinstance(v, str)]
    data["features"] = clean_features[:4] or [{"title": "特色", "desc": "（系統保底）內容待補充"}]
    data["cta_text"] = _safe_str(data.get("cta_text")) or "了解更多"
    data["concept_one_pager"] = _safe_str(data.get("concept_one_pager")) or (proposal.get("summary") or "")
    return data


def render_landing_page_html(data: dict, proposal: dict) -> str:
    """Guardrail 在程式碼：版面／樣式固定，模型只負責填文案內容，
    全部經 html.escape 處理，保證輸出是合法、可直接開啟的靜態 HTML。"""
    features_html = "\n".join(
        f'<div class="feature"><h3>{html_lib.escape(f["title"])}</h3>'
        f'<p>{html_lib.escape(f["desc"])}</p></div>'
        for f in data["features"]
    )
    concept = html_lib.escape(data["concept_one_pager"])
    return f"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<title>{html_lib.escape(data['headline'])}</title>
<style>
  body {{ font-family: -apple-system, "PingFang TC", "Microsoft JhengHei", sans-serif;
          margin:0; background:#0b0f19; color:#e8ecf4; }}
  .hero {{ padding: 64px 24px; text-align:center; background: linear-gradient(135deg,#1b2540,#0b0f19); }}
  .hero h1 {{ font-size: 2.1rem; margin-bottom: 12px; }}
  .hero p {{ font-size: 1.05rem; color:#9aa5c0; max-width:560px; margin:0 auto 28px; }}
  .cta {{ display:inline-block; padding:12px 28px; background:#5b7cff; color:white;
          border-radius:8px; text-decoration:none; font-weight:600; }}
  .features {{ display:flex; flex-wrap:wrap; gap:20px; justify-content:center;
               padding:48px 24px; max-width:900px; margin:0 auto; }}
  .feature {{ flex:1 1 220px; background:#131a2c; border-radius:12px; padding:20px; }}
  .feature h3 {{ margin:0 0 8px; color:#8fb0ff; }}
  .concept {{ max-width:720px; margin:0 auto; padding:0 24px 64px; line-height:1.8;
              color:#c4cbe0; white-space:pre-wrap; }}
  .concept h2 {{ color:#e8ecf4; }}
</style></head>
<body>
  <div class="hero">
    <h1>{html_lib.escape(data['headline'])}</h1>
    <p>{html_lib.escape(data['subheadline'])}</p>
    <a class="cta" href="#">{html_lib.escape(data['cta_text'])}</a>
  </div>
  <div class="features">{features_html}</div>
  <div class="concept"><h2>概念說明</h2>{concept}</div>
</body></html>"""


def _diff_proposals(before: dict, after: dict) -> str:
    before_lines = proposal_text_for_embed(before).splitlines()
    after_lines = proposal_text_for_embed(after).splitlines()
    diff = difflib.unified_diff(before_lines, after_lines, lineterm="", fromfile="before", tofile="after")
    return "\n".join(diff)


class PrototypeTask(TypedDict):
    persona: dict
    proposal: dict
    users: List[dict]
    round_id: str


class PrototypeTestState(TypedDict):
    top_k_items: List[dict]
    users: List[dict]
    round_id: str
    prototypes: Annotated[List[dict], operator.add]


def fan_out_prototypes(state: PrototypeTestState) -> List[Send]:
    return [
        Send("generate_prototype_and_test", {
            "persona": item["persona"], "proposal": item["proposal"],
            "users": state["users"], "round_id": state["round_id"],
        })
        for item in state["top_k_items"]
    ]


def generate_prototype_and_test(task: PrototypeTask) -> dict:
    """一個 top-K 點子的完整 Prototype→Test→最終 refine，三步驟串在
    同一個節點裡（跟 stage6/7 的『present』類似：內部循序、跨 item 平行）。"""
    persona = task["persona"]
    proposal = task["proposal"]
    users = task["users"]
    round_id = task["round_id"]
    role_token = _event_role.set(_persona_label(persona))
    try:
        # 1) Prototype：生成 landing page 文案，程式碼渲染成真的 .html 檔案
        system = (
            f"你是 {persona['name']}，要把提案包裝成 landing page 文案，吸引用戶點進來。"
            + _LANDING_PAGE_SCHEMA_HINT
        )
        user = (
            f"標題：{proposal.get('title')}\n摘要：{proposal.get('summary')}\n"
            f"BMC：{json.dumps(proposal.get('bmc'), ensure_ascii=False)}"
        )
        raw = call_llm(SMART_MODEL, system, user, max_tokens=1200)
        data = extract_json_object(raw)
        data = _ensure_landing_page_fields(data, proposal)
        page_html = render_landing_page_html(data, proposal)
        PROTOTYPE_DIR.mkdir(parents=True, exist_ok=True)
        html_path = PROTOTYPE_DIR / f"{round_id}-{persona.get('id')}.html"
        html_path.write_text(page_html, encoding="utf-8")
        emit_event(
            "generate_prototype", f"{persona['name']} 原型：{data['headline']}",
            extra={"html_path": str(html_path)},
        )
        print(f"  [prototype:{persona['name']}] {data['headline']} → {html_path}")

        # 2) Test：模擬使用者看原型概念的第一反應（不是抽象點子，是具體文案）
        reactions = []
        for user in users:
            system_u = (
                _user_system_prompt(user)
                + "現在有人要給你看一個新產品概念，請依你的角度誠實反應"
                "（喜歡/不喜歡/會不會用/有沒有疑慮），不用客氣。"
            )
            user_prompt = (
                f"概念說明：\n{data['concept_one_pager']}\n\n"
                f"主打特色：{[f['title'] for f in data['features']]}\n\n你的第一反應是？"
            )
            reaction = call_llm(SMART_MODEL, system_u, user_prompt, max_tokens=300).strip()
            reactions.append({"user_id": user.get("id"), "user_name": user.get("name"), "reaction": reaction})
            emit_event(
                "test_prototype", f"{user.get('name')} 對 {persona['name']} 原型的反應",
                extra={"user_id": user.get("id"), "user_name": user.get("name"), "reaction": reaction},
            )

        # 3) 最終修正：根據真實反應調整，不是照單全收也不是完全不理
        reactions_block = "\n".join(f"- {r['user_name']}：{r['reaction']}" for r in reactions)
        system_r = (
            f"你是 {persona['name']}，剛把原型概念拿給幾位用戶測試，聽到了他們的第一反應。"
            "請根據這些反應微調提案，不能忽略明顯的疑慮，但也不用照單全收沒有主見的建議。"
            "只輸出 JSON：{\"title\":\"<=40字\",\"summary\":\"<=120字\",\"bmc\":{九格同前}, "
            "\"self_score\":數字,\"score_reason\":\"<=40字\","
            "\"revision_note\":\"<=80字，具體說明因為哪則用戶反應改了什麼\"}"
        )
        user_r = f"原提案：\n{json.dumps(proposal, ensure_ascii=False)}\n\n用戶測試反應：\n{reactions_block}"
        raw2 = call_llm(SMART_MODEL, system_r, user_r, max_tokens=2000)
        data2 = extract_json_object(raw2)
        if not data2:
            data2 = extract_json_object(repair_json_text(raw2))

        final_proposal = dict(proposal)
        final_proposal["title"] = _safe_str(data2.get("title")) or proposal.get("title", "")
        final_proposal["summary"] = _safe_str(data2.get("summary")) or proposal.get("summary", "")
        candidate_bmc = data2.get("bmc") or {}
        orig_bmc = proposal.get("bmc") or {}
        final_proposal["bmc"] = {
            k: candidate_bmc.get(k) if isinstance(candidate_bmc.get(k), str) and candidate_bmc[k].strip() else orig_bmc.get(k, "")
            for k in BMC_KEYS
        }
        try:
            final_proposal["self_score"] = float(data2.get("self_score", proposal.get("self_score", 0)))
        except (TypeError, ValueError):
            final_proposal["self_score"] = proposal.get("self_score", 0)
        revision_note = _safe_str(data2.get("revision_note")) or "（系統保底）已依用戶測試反應微調。"
        if issues := assert_bmc_complete(final_proposal):
            raise ValueError(f"BMC 結構不變量失敗（Test 後 refine）：{issues}")

        dist = embedding_distance(proposal_text_for_embed(proposal), proposal_text_for_embed(final_proposal))
        diff_text = _diff_proposals(proposal, final_proposal)
    finally:
        _event_role.reset(role_token)

    result = {
        "persona_id": persona.get("id"), "persona_name": persona.get("name"),
        "html_path": str(html_path), "landing_page": data,
        "reactions": reactions,
        "before": proposal, "after": final_proposal,
        "revision_note": revision_note,
        "embedding_distance": round(dist, 4),
        "diff_text": diff_text,
    }
    emit_event(
        "refine_after_test", f"{persona['name']} 測試後修正：{revision_note}",
        extra={"embedding_distance": round(dist, 4)},
    )
    print(f"    → 測試後修正：{revision_note}（embed_dist={round(dist, 4)}）")
    return {"prototypes": [result]}


def build_prototype_test_subgraph():
    g = StateGraph(PrototypeTestState)
    g.add_node("generate_prototype_and_test", instrument("generate_prototype_and_test", generate_prototype_and_test))
    g.add_conditional_edges(START, fan_out_prototypes, ["generate_prototype_and_test"])
    g.add_edge("generate_prototype_and_test", END)
    return g.compile()


prototype_test_graph = build_prototype_test_subgraph()


# ---------------------------------------------------------------------------
# 三鏡檢核子圖（新，stage9 核心之一）——全員對每個 top-K 提案都要做，
# 不是同儕互評那種「別人評我」，是「大家一起對定案候選做最後檢視」。
# ---------------------------------------------------------------------------

class ThreeLensTask(TypedDict):
    persona: dict
    target_persona_id: str
    target_persona_name: str
    proposal: dict


class ThreeLensPanelState(TypedDict):
    personas: List[dict]
    top_k_proposals: dict  # persona_id -> 最終（Test 之後）提案
    checks: Annotated[List[dict], operator.add]


def fan_out_three_lens(state: ThreeLensPanelState) -> List[Send]:
    """N 位 persona × K 個 top 提案，每一格都要做——包含自己評自己的提案，
    這是刻意的：三鏡檢核是『全員共同動作』，不是同儕互評那種排除自己。"""
    id_to_name = {p.get("id"): p.get("name") for p in state["personas"]}
    return [
        Send("three_lens_check", {
            "persona": persona, "target_persona_id": pid,
            "target_persona_name": id_to_name.get(pid, pid), "proposal": proposal,
        })
        for persona in state["personas"]
        for pid, proposal in state["top_k_proposals"].items()
    ]


def three_lens_check(task: ThreeLensTask) -> dict:
    persona = task["persona"]
    proposal = task["proposal"]
    role_token = _event_role.set(_persona_label(persona))
    system = (
        f"你是 {persona['name']}（{persona.get('role', '')}）。這是全員共同的最終品質檢核，"
        "不是同儕互評——請從三個鏡片檢視這個已經過用戶測試修正的提案："
        "正面（這個提案做對了什麼）、負面（還有什麼風險或沒解決的問題）、"
        "洞見（你看到什麼別人可能沒特別注意到的觀點）。"
        "只輸出 JSON：{\"positive\": [恰好3則，每則<=30字], "
        "\"negative\": [恰好3則，每則<=30字], \"insight\": [恰好3則，每則<=30字]}"
    )
    user = (
        f"標題：{proposal.get('title')}\n摘要：{proposal.get('summary')}\n"
        f"BMC：{json.dumps(proposal.get('bmc'), ensure_ascii=False)}"
    )
    try:
        raw = call_llm(SMART_MODEL, system, user, max_tokens=700)
        data = extract_json_object(raw)
    finally:
        _event_role.reset(role_token)
    target_name = task.get("target_persona_name") or task["target_persona_id"]
    result = {
        "persona_id": persona.get("id"), "persona_name": persona.get("name"),
        "target_persona_id": task["target_persona_id"], "target_persona_name": target_name,
        "positive": _pad_to_three(data.get("positive"), "正面"),
        "negative": _pad_to_three(data.get("negative"), "負面"),
        "insight": _pad_to_three(data.get("insight"), "洞見"),
    }
    emit_event(
        "three_lens_check", f"{persona['name']} 對 {target_name} 的三鏡檢核",
        role=_persona_label(persona), extra=result,
    )
    return {"checks": [result]}


def build_three_lens_panel_subgraph():
    g = StateGraph(ThreeLensPanelState)
    g.add_node("three_lens_check", instrument("three_lens_check", three_lens_check))
    g.add_conditional_edges(START, fan_out_three_lens, ["three_lens_check"])
    g.add_edge("three_lens_check", END)
    return g.compile()


three_lens_panel_graph = build_three_lens_panel_subgraph()


# ---------------------------------------------------------------------------
# 父圖
# ---------------------------------------------------------------------------

class PersonaTask(TypedDict):
    topic: str
    company: str
    persona: dict
    users: List[dict]
    round_id: str


class MeetingState(TypedDict):
    topic: str
    company: str
    round_id: str
    personas: List[dict]
    users: List[dict]
    persona_results: Annotated[List[dict], operator.add]
    next_presenter_id: Optional[str]
    pending_question: Optional[str]
    pending_question_asked_by: Optional[str]
    human_qa_log: Annotated[List[dict], operator.add]
    review_log: Annotated[List[dict], operator.add]
    idea_pool_versions: Annotated[List[dict], operator.add]
    facilitator_log: Annotated[List[dict], operator.add]
    master_critiques: Annotated[List[dict], operator.add]
    wisdom_stats: dict
    score_log: Annotated[List[dict], operator.add]
    score_aggregates: dict
    top_k_ids: List[str]
    prototypes: Annotated[List[dict], operator.add]
    three_lens_checks: Annotated[List[dict], operator.add]


def fan_out_personas(state: MeetingState) -> List[Send]:
    return [
        Send("homework_worker", {
            "topic": state["topic"],
            "company": state["company"],
            "persona": persona,
            "users": state["users"],
            "round_id": state["round_id"],
        })
        for persona in state["personas"]
    ]


def homework_worker(task: PersonaTask) -> dict:
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
            "round_id": task["round_id"],
            "raw_results": [],
            "research_items": [],
            "research_brief": "",
            "recalled_memory": [],
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
            "recalled_memory": result.get("recalled_memory") or [],
            "interview_guide": result.get("interview_guide") or {},
            "interview_transcript": result.get("interview_transcript") or [],
            "insights": result.get("insights") or [],
            "pov": result.get("pov") or "",
            "hmw": result.get("hmw") or "",
            "refine_deltas": result.get("refine_deltas") or [],
            "elapsed_s": round(elapsed, 2),
        }]
    }


def _proposal_for_persona(state: MeetingState, pid: str) -> dict:
    for v in reversed(state["idea_pool_versions"]):
        if v["persona_id"] == pid:
            return v["proposal_after"]
    for r in state["persona_results"]:
        if r["persona"]["id"] == pid:
            return r["proposal"]
    return {}


def _recent_review_summary(state: MeetingState) -> str:
    versions = state["idea_pool_versions"]
    if not versions:
        return "（尚無互評紀錄）"
    lines = []
    for v in versions[-3:]:
        disagreements = [d for r in v["reviews"] for d in r["disagreements"]]
        lines.append(f"- {v['persona_name']}：異議={disagreements[:3]}")
    return "\n".join(lines)


def facilitator_decide(state: MeetingState) -> Command[Literal["ask_question", "run_masters"]]:
    personas = state["personas"]
    log = state["facilitator_log"]
    presented_counts: dict = {}
    for entry in log:
        if entry["action"] == "present":
            pid = entry["chosen_persona_id"]
            presented_counts[pid] = presented_counts.get(pid, 0) + 1

    round_no = len(log) + 1
    presented_so_far = sum(1 for e in log if e["action"] == "present")
    budget_used = total_cost()

    if presented_so_far >= MAX_ROUNDS or budget_used > MAX_BUDGET_USD:
        decision = {
            "round": round_no, "action": "end", "chosen_persona_id": None, "chosen_persona_name": None,
            "reason": f"超過硬性上限（已發表 {presented_so_far}/{MAX_ROUNDS} 輪, cost=${budget_used:.3f}/${MAX_BUDGET_USD}），強制收斂",
            "budget_used_usd": round(budget_used, 4), "forced": True,
        }
        emit_event("facilitator_decide", decision["reason"], role="facilitator", extra=decision)
        print(f"  [facilitator] 第{round_no}輪：強制收斂 — {decision['reason']}")
        return Command(goto="run_masters", update={"facilitator_log": [decision]})

    never_presented = [p for p in personas if presented_counts.get(p.get("id"), 0) == 0]
    summary_block = "\n".join(
        f"- {p.get('name')}：已發表 {presented_counts.get(p.get('id'), 0)} 次" for p in personas
    )
    recent_log = "\n".join(
        f"第{e['round']}輪：{e['action']} {e.get('chosen_persona_name') or ''} — {e['reason']}"
        for e in log[-3:]
    ) or "（尚未開始）"

    system = (
        "你是這場腦力激盪會議的主持人（Facilitator）。任務：讓每個人在有限頻寬內"
        "均衡發言，並判斷討論是否已經充分、該收斂了。"
        f"目前第 {round_no} 輪（硬上限 {MAX_ROUNDS} 輪），預算 ${budget_used:.3f}/${MAX_BUDGET_USD}。"
        "規則：每個人至少要發表過一次才可以結束；如果某人的提案還有明顯沒解決的"
        "爭議（從最近的異議判斷），可以讓他『加輪』再發表一次接受更多意見，"
        "但不要讓同一人連續霸佔超過必要、也不要無意義地拖長會議。"
        "只輸出 JSON：{\"action\": \"present\" 或 \"end\", "
        "\"persona_id\": \"要選的人 id（action=present 時必填）\", "
        "\"reason\": \"<=150字，你的判斷理由——這段話會直接被拿去給真人與會者看，"
        "所以要講清楚具體根據（誰的異議、哪一點還沒解決），不要只是空泛通則\"}"
    )
    user = (
        f"與會者發言次數：\n{summary_block}\n\n"
        f"尚未發表過的人：{[p.get('name') for p in never_presented] or '（無，大家都發表過了）'}\n\n"
        f"最近幾輪決策：\n{recent_log}\n\n"
        f"最近幾輪收到的異議：\n{_recent_review_summary(state)}"
    )
    role_token = _event_role.set("facilitator")
    try:
        # max_tokens 開大：SMART_MODEL 真實跑測踩過『整個預算被 extended
        # thinking 吃光、連 retry 兩次後都吐不出一個字』的崩潰（見 stage7 note）。
        raw = call_llm(SMART_MODEL, system, user, max_tokens=2000)
        data = extract_json_object(raw)
    finally:
        _event_role.reset(role_token)

    action = data.get("action")
    chosen_id_raw = data.get("persona_id")
    chosen_id = chosen_id_raw if isinstance(chosen_id_raw, str) else None
    reason = _safe_str(data.get("reason"))
    valid_ids = {p.get("id") for p in personas}
    name_to_id = {p.get("name"): p.get("id") for p in personas}
    if chosen_id not in valid_ids and chosen_id in name_to_id:
        chosen_id = name_to_id[chosen_id]

    forced = False
    if never_presented:
        if action != "present" or chosen_id not in {p.get("id") for p in never_presented}:
            chosen_id = never_presented[0].get("id")
            reason = reason or "（系統保底）優先讓尚未發表的人先發表。"
            forced = True
        action = "present"
    elif action == "present" and chosen_id not in valid_ids:
        action = "end"
        reason = reason or "（系統保底）模型指定的 persona_id 無效，改為收斂。"
        forced = True
    elif action not in ("present", "end"):
        action = "end"
        reason = reason or "（系統保底）模型輸出格式異常，改為收斂。"
        forced = True

    chosen_persona = next((p for p in personas if p.get("id") == chosen_id), None) if action == "present" else None
    decision = {
        "round": round_no,
        "action": action,
        "chosen_persona_id": chosen_id if action == "present" else None,
        "chosen_persona_name": chosen_persona.get("name") if chosen_persona else None,
        "reason": reason,
        "budget_used_usd": round(budget_used, 4),
        "forced": forced,
    }
    emit_event(
        "facilitator_decide",
        f"第{round_no}輪：{action} {decision['chosen_persona_name'] or ''} — {reason}",
        role="facilitator",
        extra=decision,
    )
    print(f"  [facilitator] 第{round_no}輪：{action} {decision['chosen_persona_name'] or ''} — {reason}")

    if action == "end":
        return Command(goto="run_masters", update={"facilitator_log": [decision]})
    return Command(
        goto="ask_question",
        update={"facilitator_log": [decision], "next_presenter_id": chosen_id},
    )


def ask_question(state: MeetingState) -> dict:
    pid = state["next_presenter_id"]
    presenter = next(p for p in state["personas"] if p.get("id") == pid)
    proposal = _proposal_for_persona(state, pid)
    asked_so_far = len([q for q in state["human_qa_log"] if q["presenter_id"] == pid])
    facilitator_log = state["facilitator_log"]
    # facilitator_decide 剛選完這個人就 Command(goto="ask_question")，
    # 所以這裡 log 最後一筆一定是「為什麼選這個人」的理由——之前這個理由
    # 只寫進 events extra，真人要點開事件細節才看得到；使用者要求把它
    # 直接曝光在提問點，人類才知道主持人憑什麼理由讓這個人發表。
    facilitator_reason = facilitator_log[-1].get("reason") if facilitator_log else None
    payload = {
        "presenter_id": pid,
        "presenter_name": presenter.get("name"),
        "proposal_title": proposal.get("title"),
        "proposal_summary": proposal.get("summary"),
        "questions_asked_so_far": asked_so_far,
        "facilitator_reason": facilitator_reason,
        "prompt": (
            f"{presenter.get('name')} 剛發表《{proposal.get('title')}》。"
            "要提問嗎？可連續問多題，輸入空字串／skip 結束提問進入互評。"
        ),
    }
    answer_signal = interrupt(payload)
    if isinstance(answer_signal, dict) and answer_signal.get("action") == "ask":
        question = _safe_str(answer_signal.get("question"))
        if question:
            # 使用者要求 HITL 紀錄知道「誰問的」——不做真的身份驗證，就是
            # 提問當下附帶的顯示名稱，沒填就記「匿名」。
            asked_by = _safe_str(answer_signal.get("asked_by")) or "匿名"
            return {"pending_question": question, "pending_question_asked_by": asked_by}
    return {"pending_question": None, "pending_question_asked_by": None}


def route_after_question(state: MeetingState) -> Literal["answer_question", "run_peer_review"]:
    return "answer_question" if state.get("pending_question") else "run_peer_review"


def answer_question(state: MeetingState) -> dict:
    pid = state["next_presenter_id"]
    presenter = next(p for p in state["personas"] if p.get("id") == pid)
    proposal = _proposal_for_persona(state, pid)
    question = state["pending_question"]
    role_token = _event_role.set(_persona_label(presenter))
    system = (
        f"你是 {presenter['name']}（{presenter.get('role', '')}），剛發表完提案，"
        "現在有人類與會者當場提問。請根據你的提案內容誠實回答，2-4 句話，"
        "不知道的部分就承認不知道，不要瞎掰。"
    )
    user = (
        f"你的提案標題：{proposal.get('title')}\n摘要：{proposal.get('summary')}\n"
        f"HMW：{proposal.get('hmw')}\nBMC：{json.dumps(proposal.get('bmc'), ensure_ascii=False)}\n\n"
        f"人類提問：{question}"
    )
    try:
        answer = call_llm(SMART_MODEL, system, user, max_tokens=800).strip()
    finally:
        _event_role.reset(role_token)
    qa_entry = {
        "presenter_id": presenter.get("id"),
        "presenter_name": presenter.get("name"),
        "question": question,
        "answer": answer,
        "asked_by": state.get("pending_question_asked_by") or "匿名",
    }
    emit_event(
        "human_qa",
        f"人類問 {presenter.get('name')}：{question}",
        role=_persona_label(presenter),
        extra=qa_entry,
    )
    print(f"  [human_qa] Q: {question}\n             A: {answer}")
    return {"human_qa_log": [qa_entry], "pending_question": None, "pending_question_asked_by": None}


def run_peer_review(state: MeetingState) -> dict:
    pid = state["next_presenter_id"]
    presenter = next(p for p in state["personas"] if p.get("id") == pid)
    proposal = _proposal_for_persona(state, pid)
    reviewers = [p for p in state["personas"] if p.get("id") != pid]
    qa_context = [q for q in state["human_qa_log"] if q["presenter_id"] == pid]

    emit_event(
        "present",
        f"{presenter.get('name')} 發表《{proposal.get('title', '')}》（人類問了 {len(qa_context)} 題）",
        role=_persona_label(presenter),
    )
    print(f"  [present] {presenter.get('name')} 發表《{proposal.get('title', '')}》，人類問了 {len(qa_context)} 題")

    round_result = review_round_graph.invoke({
        "presenter_id": pid,
        "presenter_name": presenter.get("name"),
        "proposal": proposal,
        "reviewers": reviewers,
        "qa_context": qa_context,
        "reviews": [],
        "revised_proposal": {},
    })
    reviews = round_result["reviews"]
    revised = round_result["revised_proposal"]
    dist = embedding_distance(proposal_text_for_embed(proposal), proposal_text_for_embed(revised))
    version_entry = {
        "persona_id": pid,
        "persona_name": presenter.get("name"),
        "before_title": proposal.get("title"),
        "after_title": revised.get("title"),
        "revision_note": revised.get("revision_note"),
        "addressed_reviewer_ids": revised.get("addressed_reviewer_ids"),
        "embedding_distance": round(dist, 4),
        "human_questions_asked": len(qa_context),
        "reviews": reviews,
        "proposal_after": revised,
    }
    print(
        f"    → 修正：{revised.get('revision_note')} "
        f"(回應 {revised.get('addressed_reviewer_ids')}，embed_dist={round(dist, 4)})"
    )
    return {
        "review_log": reviews,
        "idea_pool_versions": [version_entry],
    }


def run_masters(state: MeetingState) -> dict:
    idea_pool_summary = "\n".join(
        f"- {v['persona_name']}：《{v['proposal_after'].get('title')}》{v['proposal_after'].get('summary')}"
        for v in state["idea_pool_versions"]
    ) or "（沒有任何提案）"
    result = master_panel_graph.invoke({
        "topic": state["topic"], "idea_pool_summary": idea_pool_summary, "critiques": [],
    })
    return {"master_critiques": result["critiques"]}


def write_wisdom(state: MeetingState) -> dict:
    stats = write_collective_wisdom(
        round_id=state["round_id"],
        topic=state["topic"],
        master_critiques=state["master_critiques"],
        idea_pool_versions=state["idea_pool_versions"],
        persona_results=state["persona_results"],
        three_lens_checks=state["three_lens_checks"],
    )
    emit_event("write_wisdom", f"寫入集體智慧庫：{stats}", extra=stats)
    print(f"  [write_wisdom] {stats}")
    return {"wisdom_stats": stats}


def run_collective_scoring(state: MeetingState) -> dict:
    """全體交叉評分彼此的最終提案，聚合出平均分＋標準差，選出 top-K
    進入 Prototype/Test 階段。"""
    final_proposals = {v["persona_id"]: v["proposal_after"] for v in state["idea_pool_versions"]}
    result = scoring_panel_graph.invoke({
        "personas": state["personas"], "final_proposals": final_proposals, "scores": [],
    })
    scores = result["scores"]
    aggregates = compute_score_aggregates(scores, list(final_proposals.keys()))
    k = min(TOP_K, len(final_proposals))
    top_k_ids = select_top_k(aggregates, k)
    id_to_name = {p.get("id"): p.get("name") for p in state["personas"]}
    top_k_names = [id_to_name.get(pid, pid) for pid in top_k_ids]
    emit_event(
        "run_collective_scoring", f"評分完成，top-{k}：{top_k_names}",
        extra={"aggregates": aggregates, "top_k_ids": top_k_ids},
    )
    print("  [collective_scoring] 評分聚合：")
    for pid, agg in sorted(aggregates.items(), key=lambda kv: kv[1]["mean"], reverse=True):
        marker = "★" if pid in top_k_ids else " "
        print(f"    {marker} {pid}: mean={agg['mean']} stdev={agg['stdev']} (n={agg['n']})")
    return {"score_log": scores, "score_aggregates": aggregates, "top_k_ids": top_k_ids}


def run_prototype_test(state: MeetingState) -> dict:
    final_proposals = {v["persona_id"]: v["proposal_after"] for v in state["idea_pool_versions"]}
    persona_by_id = {p.get("id"): p for p in state["personas"]}
    top_k_items = [
        {"persona": persona_by_id[pid], "proposal": final_proposals[pid]}
        for pid in state["top_k_ids"] if pid in final_proposals and pid in persona_by_id
    ]
    result = prototype_test_graph.invoke({
        "top_k_items": top_k_items, "users": state["users"], "round_id": state["round_id"], "prototypes": [],
    })
    return {"prototypes": result["prototypes"]}


def run_three_lens_check(state: MeetingState) -> dict:
    """全員對每個 top-K（Test 之後的最終版）提案做三鏡檢核。"""
    top_k_proposals = {p["persona_id"]: p["after"] for p in state["prototypes"]}
    result = three_lens_panel_graph.invoke({
        "personas": state["personas"], "top_k_proposals": top_k_proposals, "checks": [],
    })
    checks = result["checks"]
    print(f"  [three_lens_check] {len(checks)} 筆檢核（{len(state['personas'])} 人 × {len(top_k_proposals)} 個提案）")
    return {"three_lens_checks": checks}


def build_parent_graph(checkpointer):
    g = StateGraph(MeetingState)
    g.add_node("homework_worker", instrument("homework_worker", homework_worker))
    g.add_node("facilitator_decide", instrument("facilitator_decide", facilitator_decide))
    g.add_node("ask_question", instrument("ask_question", ask_question))
    g.add_node("answer_question", instrument("answer_question", answer_question))
    g.add_node("run_peer_review", instrument("run_peer_review", run_peer_review))
    g.add_node("run_masters", instrument("run_masters", run_masters))
    g.add_node("write_wisdom", instrument("write_wisdom", write_wisdom))
    g.add_node("run_collective_scoring", instrument("run_collective_scoring", run_collective_scoring))
    g.add_node("run_prototype_test", instrument("run_prototype_test", run_prototype_test))
    g.add_node("run_three_lens_check", instrument("run_three_lens_check", run_three_lens_check))
    g.add_conditional_edges(START, fan_out_personas, ["homework_worker"])
    g.add_edge("homework_worker", "facilitator_decide")
    g.add_conditional_edges(
        "ask_question", route_after_question,
        {"answer_question": "answer_question", "run_peer_review": "run_peer_review"},
    )
    g.add_edge("answer_question", "ask_question")
    g.add_edge("run_peer_review", "facilitator_decide")
    # write_wisdom 刻意排在 run_three_lens_check 之後、真正的會議尾聲
    # 才執行（原本排在 run_masters 後面）——使用者要求把三鏡檢核／收斂
    # 前互評／POV-HMW 也寫進 RAG，這些資料在原本的順序下 write_wisdom
    # 執行當下根本還不存在（run_three_lens_check 是最後一個節點）。
    # 移到最後一次寫，一次拿到全部資料，不用拆成兩次寫入。
    g.add_edge("run_masters", "run_collective_scoring")
    g.add_edge("run_collective_scoring", "run_prototype_test")
    g.add_edge("run_prototype_test", "run_three_lens_check")
    g.add_edge("run_three_lens_check", "write_wisdom")
    g.add_edge("write_wisdom", END)
    return g.compile(checkpointer=checkpointer)


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------

def run_baseline(topic: str, company: str) -> dict:
    role_token = _event_role.set("baseline")
    set_current_node("baseline")
    t0 = time.perf_counter()
    system = (
        "你是一位產品策略顧問。請針對主題直接給一個產品點子提案。"
        "若你提到依據，請誠實標注（可以是一般知識，不必有真實 URL）。"
        "沒有做過用戶訪談或同儕互評，pov/hmw/insight_refs/memory_refs 留空或合理帶過即可。"
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


def generate_final_verdict(
    *,
    topic: str,
    top_k_proposals: List[dict],
    baseline_proposal: dict,
    baseline_metrics: dict,
    diversity_after: dict,
) -> str:
    """使用者要求：最後讓 AI 直接比較這場真實資料裡 agent 流程 vs baseline
    的優劣，不是只有結構性的數字對照表——這段話要有觀點、具體點名兩邊的
    優勢與代價，也要誠實承認 baseline 的價值（速度快、成本低），不是為了
    捧 agent 流程而失真。"""
    top_k_summaries = "\n".join(
        f"- 《{p.get('title', '')}》{p.get('summary', '')}" for p in top_k_proposals
    ) or "（無 top-K 提案）"
    system = (
        "你是一位產品策略顧問，要針對這場真實跑出來的資料，比較「多 agent 腦力激盪流程」"
        "產出的 top-K 提案，跟「直接問 LLM 一次」的 baseline 提案，寫一段有觀點的優劣分析。"
        "要具體點名兩邊各自的優勢與代價（不是空泛通則），並誠實承認 baseline 也有它的價值"
        "（例如速度快、成本低、適合初步發散），不要為了捧多 agent 流程而失真。只輸出正文，"
        "<=300字。"
    )
    user = (
        f"主題：{topic}\n\n"
        f"多 agent 流程 top-K 提案：\n{top_k_summaries}\n\n"
        f"Baseline 提案：《{baseline_proposal.get('title', '')}》{baseline_proposal.get('summary', '')}\n\n"
        f"量化對照：同儕互評後點子多樣性（兩兩平均距離）={diversity_after.get('avg_distance')}，"
        f"baseline 真實搜尋引用數={baseline_metrics.get('real_citations')}（可能編造，無法驗證），"
        f"baseline 成本=${baseline_metrics.get('cost_usd', 0):.4f}"
    )
    role_token = _event_role.set("verdict")
    set_current_node("generate_final_verdict")
    try:
        verdict = call_llm(SMART_MODEL, system, user, max_tokens=900).strip()
    finally:
        _event_role.reset(role_token)
    emit_event("generate_final_verdict", verdict[:80], role="verdict", extra={"verdict": verdict})
    return verdict


# ---------------------------------------------------------------------------
# 多樣性度量
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
    print(f"{'節點':<24}{'時間':>8}{'呼叫':>6}{'in':>10}{'out':>10}{'USD':>10}")
    for name in list(node_times.keys()):
        calls = [e for e in usage_log if e["node"] == name]
        t = node_times.get(name, 0.0)
        tin = sum(e["input"] for e in calls)
        tout = sum(e["output"] for e in calls)
        c = sum(cost_of(e) for e in calls)
        print(f"{name:<24}{t:>7.1f}s{len(calls):>6}{tin:>10}{tout:>10}{c:>10.4f}")
    print(f"{'-' * 72}")
    print(f"總成本 USD：{total_cost():.4f}")


def save_outputs(
    *,
    round_id: str,
    topic: str,
    personas: List[dict],
    users: List[dict],
    persona_results: List[dict],
    review_log: List[dict],
    idea_pool_versions: List[dict],
    human_qa_log: List[dict],
    facilitator_log: List[dict],
    master_critiques: List[dict],
    wisdom_stats: dict,
    recall_hits_total: int,
    score_log: List[dict],
    score_aggregates: dict,
    top_k_ids: List[str],
    prototypes: List[dict],
    three_lens_checks: List[dict],
    baseline_proposal: dict,
    baseline_metrics: dict,
    diversity_before: dict,
    diversity_after: dict,
    final_verdict: str = "",
) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = {
        "run_id": stamp,
        "round_id": round_id,
        "topic": topic,
        "persona_count": len(personas),
        "user_count": len(users),
        "personas": personas,
        "users": users,
        # persona_results 本來就有完整 research_items（title/url/snippet）、
        # research_brief、interview_transcript——之前算出來卻沒存，研究
        # 足跡跟完整逐字稿存檔後就沒了。回放器/即時畫面要追溯「看了哪些
        # 網頁才形成想法」就是靠這個欄位。
        "persona_results": persona_results,
        "facilitator_log": facilitator_log,
        "human_qa_log": human_qa_log,
        "idea_pool_versions": idea_pool_versions,
        "review_log": review_log,
        "master_critiques": master_critiques,
        "wisdom_stats": wisdom_stats,
        "recall_hits_total": recall_hits_total,
        "score_log": score_log,
        "score_aggregates": score_aggregates,
        "top_k_ids": top_k_ids,
        "prototypes": prototypes,
        "three_lens_checks": three_lens_checks,
        "diversity_before_review": diversity_before,
        "diversity_after_review": diversity_after,
        "baseline": {"proposal": baseline_proposal, "metrics": baseline_metrics},
        "final_verdict": final_verdict,
        "total_cost_usd": round(total_cost(), 6),
    }
    path = OUTPUT_DIR / f"stage9-run-{stamp}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUTPUT_DIR / f"stage9-latest-{topic[:12]}.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


# ---------------------------------------------------------------------------
# 最終報告（純組裝，不是圖節點——需要 baseline 對照資料，那一直是
# graph.invoke() 完成後才在 main() 裡跑的，這裡沿用同一個時序）
# ---------------------------------------------------------------------------

def build_final_report_markdown(
    *,
    round_id: str,
    topic: str,
    personas: List[dict],
    users: List[dict],
    persona_results: List[dict],
    facilitator_log: List[dict],
    human_qa_log: List[dict],
    master_critiques: List[dict],
    score_aggregates: dict,
    top_k_ids: List[str],
    prototypes: List[dict],
    three_lens_checks: List[dict],
    baseline_proposal: dict,
    baseline_metrics: dict,
    diversity_before: dict,
    diversity_after: dict,
    final_verdict: str = "",
) -> str:
    id_to_name = {p.get("id"): p.get("name") for p in personas}
    L: List[str] = []
    L.append(f"# 腦力激盪會議最終報告\n")
    L.append(f"- **主題**：{topic}")
    L.append(f"- **Round ID**：{round_id}")
    L.append(f"- **與會者**：{', '.join(p['name'] for p in personas)}")
    L.append(f"- **模擬使用者**：{', '.join(u['name'] for u in users)}\n")
    L.append("---\n")

    L.append("## 會議軌跡（Facilitator 決策 log）\n")
    for d in facilitator_log:
        tag = "（強制）" if d.get("forced") else ""
        L.append(f"- 第{d['round']}輪：{d['action']} {d.get('chosen_persona_name') or ''} {tag} — {d['reason']}")
    L.append("")

    L.append("## 人類提問記錄\n")
    if human_qa_log:
        for qa in human_qa_log:
            L.append(f"**問 {qa['presenter_name']}**：{qa['question']}")
            L.append(f"> {qa['answer']}\n")
    else:
        L.append("（本場沒有人類提問，全程跳過）\n")

    L.append("## 大師點評\n")
    for m in master_critiques:
        L.append(f"### {m['master_name']}（{m['angle']}）")
        L.append(f"{m['critique']}")
        L.append(f"- 首選：{m['top_pick_persona']}\n")

    L.append("## 集體評分聚合\n")
    L.append("| Persona | 平均分 | 標準差（分歧度） | Top-K |")
    L.append("|---|---|---|---|")
    for pid, agg in sorted(score_aggregates.items(), key=lambda kv: kv[1]["mean"], reverse=True):
        marker = "★" if pid in top_k_ids else ""
        L.append(f"| {id_to_name.get(pid, pid)} | {agg['mean']} | {agg['stdev']} | {marker} |")
    L.append("")

    L.append("## Top-K 最終提案詳情\n")
    prototypes_by_pid = {p["persona_id"]: p for p in prototypes}
    persona_result_by_pid = {r["persona"]["id"]: r for r in persona_results}
    checks_by_target: dict = {}
    for c in three_lens_checks:
        checks_by_target.setdefault(c["target_persona_id"], []).append(c)

    for pid in top_k_ids:
        proto = prototypes_by_pid.get(pid)
        if not proto:
            continue
        pr = persona_result_by_pid.get(pid, {})
        final_proposal = proto["after"]
        L.append(f"### {proto['persona_name']}：《{final_proposal.get('title', '')}》\n")
        L.append(f"**摘要**：{final_proposal.get('summary', '')}\n")
        L.append(f"**POV**：{pr.get('pov', '')}")
        L.append(f"**HMW**：{pr.get('hmw', '')}\n")
        L.append("**BMC**：\n")
        for k in BMC_KEYS:
            L.append(f"- {k}：{(final_proposal.get('bmc') or {}).get(k, '')}")
        L.append(f"\n**原型**：`{proto['html_path']}`（可直接用瀏覽器開啟）\n")
        L.append(f"**測試後修正說明**：{proto['revision_note']}（embedding 距離 {proto['embedding_distance']}）\n")

        L.append("**第一輪訪談（Empathize，需求探索，做功課階段）**：\n")
        transcript = pr.get("interview_transcript") or []
        if transcript:
            for t in transcript:
                L.append(f"- [{t['user_name']} 第{t['round']}輪] Q：{t['question']} / A：{t['answer']}")
        else:
            L.append("（無逐字稿記錄）")
        L.append("")

        L.append("**第二輪訪談（Test，概念驗證，看過原型後的反應）**：\n")
        for r in proto["reactions"]:
            L.append(f"- {r['user_name']}：{r['reaction']}")
        L.append("")

        L.append("**三鏡檢核（全員共同動作）**：\n")
        for c in checks_by_target.get(pid, []):
            L.append(f"- **{c['persona_name']}** — 正面：{c['positive']}")
            L.append(f"  負面：{c['negative']}")
            L.append(f"  洞見：{c['insight']}")
        L.append("\n---\n")

    L.append("## Baseline 對照（直接問 LLM vs 完整會議流程）\n")
    L.append(f"**Baseline 提案**：《{baseline_proposal.get('title', '')}》{baseline_proposal.get('summary', '')}\n")
    L.append(f"- 真實搜尋引用：{baseline_metrics['real_citations']}（會議流程的每份提案都有真實引用來源）")
    L.append(f"- BMC 完整度：{baseline_metrics['bmc_filled']}/9")
    L.append(f"- 成本：${baseline_metrics['cost_usd']:.4f}（單次呼叫，沒有訪談／互評／測試依據）\n")

    L.append("## 提案多樣性\n")
    L.append(f"- 同儕互評前：{diversity_before['avg_distance']}")
    L.append(f"- 同儕互評後：{diversity_after['avg_distance']}\n")

    if final_verdict:
        L.append("## AI 對照評語（agent 流程 vs baseline）\n")
        L.append(f"{final_verdict}\n")

    return "\n".join(L)


# ---------------------------------------------------------------------------
# CLI 驅動
# ---------------------------------------------------------------------------

def _load_script(path: Optional[str]) -> Optional[dict]:
    if not path:
        return None
    return json.loads(Path(path).read_text(encoding="utf-8"))


def get_human_input(payload: dict, script: Optional[dict]) -> dict:
    pid = payload["presenter_id"]
    asked = payload["questions_asked_so_far"]
    if script is not None:
        entry = script.get(pid) if isinstance(script, dict) else None
        entry = entry or {"skip": True}
        if entry.get("skip"):
            return {"action": "skip"}
        questions = entry.get("questions") or []
        if asked < len(questions):
            q = questions[asked]
            print(f"  [scripted] 對 {payload['presenter_name']} 提問：{q}")
            return {"action": "ask", "question": q}
        return {"action": "skip"}
    print("\n" + json.dumps(payload, ensure_ascii=False, indent=2))
    try:
        raw = input("輸入問題內容，或直接按 Enter 跳過此人：").strip()
    except EOFError:
        raw = ""
    if not raw:
        return {"action": "skip"}
    return {"action": "ask", "question": raw}


def run_meeting(
    graph,
    config: dict,
    initial_input: dict,
    *,
    script: Optional[dict],
    stop_after_first_interrupt: bool,
) -> Optional[dict]:
    snapshot = graph.get_state(config)
    if snapshot.next:
        print(f"偵測到 thread {config['configurable']['thread_id']!r} 有未完成的會議，從斷點續跑…")
        # 上次卡住可能是真正的 interrupt()（等人類輸入），也可能是節點本身
        # 拋了未捕捉的例外（stage7 真實跑測踩過：facilitator_decide 崩潰）——
        # `task.interrupts` 是空 tuple 就代表是崩潰、不是 interrupt()，這時
        # 用 `invoke(None, config)` 讓 LangGraph 直接重跑那個崩潰的節點。
        task = snapshot.tasks[0] if snapshot.tasks else None
        if not (task and task.interrupts):
            print(f"（上次是節點執行中崩潰：{task.error if task else '未知'}——不是等待人類輸入，直接從斷點重跑）")
            graph.invoke(None, config)
    else:
        graph.invoke(initial_input, config)

    while True:
        snapshot = graph.get_state(config)
        if not snapshot.next:
            return snapshot.values
        task = snapshot.tasks[0] if snapshot.tasks else None
        if not (task and task.interrupts):
            print(f"（節點執行中崩潰：{task.error if task else '未知'}——重跑該節點）")
            graph.invoke(None, config)
            continue
        payload = task.interrupts[0].value
        if stop_after_first_interrupt:
            print("\n=== 命中第一個人類介入點，--stop-after-first-interrupt 生效，結束 process ===")
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return None
        user_input = get_human_input(payload, script)
        graph.invoke(Command(resume=user_input), config)


def main() -> None:
    parser = argparse.ArgumentParser(description="Stage 9：三鏡檢核 + 最終報告")
    parser.add_argument("--thread", default=None)
    parser.add_argument("--script", default=None)
    parser.add_argument("--stop-after-first-interrupt", action="store_true")
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("缺少 ANTHROPIC_API_KEY。請複製 practice/.env.example 為 practice/.env 並填入。")
        sys.exit(1)

    thread_id = args.thread or f"meeting-{uuid.uuid4().hex[:8]}"
    round_id = thread_id
    is_fresh_thread = args.thread is None or not CHECKPOINT_DB_PATH.exists()

    reset_metrics()
    usage_log.clear()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if is_fresh_thread and EVENTS_PATH.exists():
        EVENTS_PATH.unlink()

    conn = sqlite3.connect(str(CHECKPOINT_DB_PATH), check_same_thread=False)
    checkpointer = SqliteSaver(conn)
    meeting_graph = build_parent_graph(checkpointer)
    config = {"configurable": {"thread_id": thread_id}}

    topic = os.environ.get("BRAINSTORM_TOPIC", "如何提升新聞短影音互動率")
    personas = load_personas()
    users = load_users()
    company = load_company()
    script = _load_script(args.script)

    wisdom_count_before = get_chroma_collection("wisdom").count()
    interviews_count_before = get_chroma_collection("interviews").count()

    print(f"主題：{topic}")
    print(f"Thread／round_id：{thread_id}（checkpoint db：{CHECKPOINT_DB_PATH}）")
    print(f"Persona 人數：{len(personas)}；模擬使用者人數：{len(users)}；硬上限：{MAX_ROUNDS} 輪／${MAX_BUDGET_USD}")
    print(f"Top-K：{TOP_K}")
    print(f"Chroma 現有筆數：wisdom={wisdom_count_before}, interviews={interviews_count_before}")
    print(f"事件流：{EVENTS_PATH}")
    print()

    wall_t0 = time.perf_counter()
    final_state = run_meeting(
        meeting_graph,
        config,
        {
            "topic": topic,
            "company": company,
            "round_id": round_id,
            "personas": personas,
            "users": users,
            "persona_results": [],
            "next_presenter_id": None,
            "pending_question": None,
            "pending_question_asked_by": None,
            "human_qa_log": [],
            "review_log": [],
            "idea_pool_versions": [],
            "facilitator_log": [],
            "master_critiques": [],
            "wisdom_stats": {},
            "score_log": [],
            "score_aggregates": {},
            "top_k_ids": [],
            "prototypes": [],
            "three_lens_checks": [],
        },
        script=script,
        stop_after_first_interrupt=args.stop_after_first_interrupt,
    )
    wall_elapsed = time.perf_counter() - wall_t0

    if final_state is None:
        print(f"\n（process 於 {wall_elapsed:.1f}s 後主動結束，尚未完成——用同個 --thread {thread_id} 續跑）")
        return

    persona_results = final_state["persona_results"]
    review_log = final_state["review_log"]
    idea_pool_versions = final_state["idea_pool_versions"]
    human_qa_log = final_state["human_qa_log"]
    facilitator_log = final_state["facilitator_log"]
    master_critiques = final_state["master_critiques"]
    wisdom_stats = final_state["wisdom_stats"]
    score_log = final_state["score_log"]
    score_aggregates = final_state["score_aggregates"]
    top_k_ids = final_state["top_k_ids"]
    prototypes = final_state["prototypes"]
    three_lens_checks = final_state["three_lens_checks"]

    recall_hits_total = sum(len(r.get("recalled_memory") or []) for r in persona_results)

    print()
    print("=== Baseline：直接問 LLM 一次 ===")
    cost_before_baseline = total_cost()
    baseline_proposal = run_baseline(topic, company)
    baseline_cost = total_cost() - cost_before_baseline
    baseline_metrics = metrics_of(baseline_proposal, [], [], baseline_cost)
    baseline_metrics["real_citations"] = count_real_citations(baseline_proposal, [])

    before_proposals = [r["proposal"] for r in persona_results]
    after_proposals_by_persona = {v["persona_id"]: v["proposal_after"] for v in idea_pool_versions}
    after_proposals = list(after_proposals_by_persona.values())
    diversity_before = pairwise_diversity(before_proposals)
    diversity_after = pairwise_diversity(after_proposals)

    print()
    print("=== AI 對照評語：agent 流程 vs baseline ===")
    top_k_proposals = [after_proposals_by_persona[pid] for pid in top_k_ids if pid in after_proposals_by_persona]
    final_verdict = generate_final_verdict(
        topic=topic, top_k_proposals=top_k_proposals, baseline_proposal=baseline_proposal,
        baseline_metrics=baseline_metrics, diversity_after=diversity_after,
    )
    print(f"  {final_verdict}")

    out_path = save_outputs(
        round_id=round_id,
        topic=topic,
        personas=personas,
        users=users,
        persona_results=persona_results,
        review_log=review_log,
        idea_pool_versions=idea_pool_versions,
        human_qa_log=human_qa_log,
        facilitator_log=facilitator_log,
        master_critiques=master_critiques,
        wisdom_stats=wisdom_stats,
        recall_hits_total=recall_hits_total,
        score_log=score_log,
        score_aggregates=score_aggregates,
        top_k_ids=top_k_ids,
        prototypes=prototypes,
        three_lens_checks=three_lens_checks,
        baseline_proposal=baseline_proposal,
        baseline_metrics=baseline_metrics,
        diversity_before=diversity_before,
        diversity_after=diversity_after,
        final_verdict=final_verdict,
    )

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report_md = build_final_report_markdown(
        round_id=round_id,
        topic=topic,
        personas=personas,
        users=users,
        persona_results=persona_results,
        facilitator_log=facilitator_log,
        human_qa_log=human_qa_log,
        master_critiques=master_critiques,
        score_aggregates=score_aggregates,
        top_k_ids=top_k_ids,
        prototypes=prototypes,
        three_lens_checks=three_lens_checks,
        baseline_proposal=baseline_proposal,
        baseline_metrics=baseline_metrics,
        diversity_before=diversity_before,
        diversity_after=diversity_after,
        final_verdict=final_verdict,
    )
    report_path = REPORT_DIR / f"{round_id}-final-report.md"
    report_path.write_text(report_md, encoding="utf-8")

    print()
    print("=== 集體評分聚合 ===")
    for pid, agg in sorted(score_aggregates.items(), key=lambda kv: kv[1]["mean"], reverse=True):
        marker = "★ top-K" if pid in top_k_ids else "  "
        print(f"  {marker} {pid}: mean={agg['mean']} stdev={agg['stdev']}（分歧度）(n={agg['n']})")

    print()
    print("=== Prototype + Test（top-K）===")
    for p in prototypes:
        print(f"  {p['persona_name']}：{p['landing_page']['headline']}")
        print(f"    HTML：{p['html_path']}")
        print(f"    測試後修正：{p['revision_note']}（embed_dist={p['embedding_distance']}）")
        for r in p["reactions"]:
            print(f"      {r['user_name']}：{r['reaction'][:60]}")

    print()
    print("=== 三鏡檢核（全員共同動作）===")
    checks_by_target: dict = {}
    for c in three_lens_checks:
        checks_by_target.setdefault(c["target_persona_id"], []).append(c)
    for pid in top_k_ids:
        print(f"  提案 {pid}：{len(checks_by_target.get(pid, []))} 筆檢核")
        for c in checks_by_target.get(pid, []):
            print(f"    {c['persona_name']} → 正面{len(c['positive'])}/負面{len(c['negative'])}/洞見{len(c['insight'])}")

    print()
    print(f"最終報告：{report_path}")

    print()
    print("=== 驗收 ===")
    presented_ids = {v["persona_id"] for v in idea_pool_versions}
    all_persona_ids = {p.get("id") for p in personas}
    everyone_presented = all_persona_ids <= presented_ids
    within_round_cap = len(idea_pool_versions) <= MAX_ROUNDS
    reviews_shape_ok = all(
        len(r["agreements"]) == 3 and len(r["disagreements"]) == 3 and len(r["insights"]) == 3
        for r in review_log
    )
    masters_ok = len(master_critiques) == len(MASTERS) and all(m["critique"] for m in master_critiques)
    scoring_ok = all(pid in score_aggregates for pid in presented_ids) and all(
        agg["n"] >= 1 for agg in score_aggregates.values()
    )
    prototypes_ok = (
        len(prototypes) == len(top_k_ids)
        and all(Path(p["html_path"]).exists() and Path(p["html_path"]).stat().st_size > 0 for p in prototypes)
    )
    test_changed_version = all(
        p["embedding_distance"] > 0.0 and p["diff_text"].strip() for p in prototypes
    )
    three_lens_shape_ok = (
        len(three_lens_checks) == len(personas) * len(top_k_ids)
        and all(
            len(c["positive"]) == 3 and len(c["negative"]) == 3 and len(c["insight"]) == 3
            for c in three_lens_checks
        )
    )
    # 報告完整性：檔案存在、有實質內容，且真的含有人類問答與兩輪訪談的區塊標題
    # ——不是只檢查檔案存在，是檢查驗收要求的具體內容真的被組進去了。
    report_text = report_path.read_text(encoding="utf-8")
    report_complete = (
        report_path.exists()
        and len(report_text) > 500
        and "人類提問記錄" in report_text
        and "第一輪訪談（Empathize" in report_text
        and "第二輪訪談（Test" in report_text
        and "三鏡檢核" in report_text
    )

    print(f"每人至少發表一次：{'是' if everyone_presented else '否'}（{len(presented_ids)}/{len(personas)}）")
    print(f"發表總次數：{len(idea_pool_versions)}（硬上限 {MAX_ROUNDS}）：{'未超過' if within_round_cap else '超過！'}")
    print(f"互評每則恰好 3/3/3：{'是' if reviews_shape_ok else '否'}")
    print(f"三大師皆有點評：{'是' if masters_ok else '否'}")
    print(f"每份最終提案都有評分聚合（含分歧度）：{'是' if scoring_ok else '否'}")
    print(f"Top-K 原型皆已寫出可開啟的 HTML：{'是' if prototypes_ok else '否'}")
    print(f"測試反應真的改變了最終版本（embed_dist>0 且有實際 diff）：{'是' if test_changed_version else '否'}")
    print(f"三鏡檢核格式不變量（{len(personas)}人×{len(top_k_ids)}提案，每筆恰好3/3/3）：{'是' if three_lens_shape_ok else '否'}")
    print(f"最終報告完整（含人類問答＋兩輪訪談記錄）：{'是' if report_complete else '否'}")
    print(f"提案多樣性：互評前 {diversity_before['avg_distance']} → 互評後 {diversity_after['avg_distance']}")
    print(f"總耗時：{wall_elapsed:.1f}s")
    print(f"已存檔：{out_path}")
    print(f"事件流：{EVENTS_PATH}")
    print_run_summary()

    ok = (
        len(personas) >= 3
        and everyone_presented
        and within_round_cap
        and reviews_shape_ok
        and masters_ok
        and scoring_ok
        and prototypes_ok
        and test_changed_version
        and three_lens_shape_ok
        and report_complete
        and total_cost() <= MAX_BUDGET_USD + 0.5  # prototype/test/three-lens 階段的花費不算進 facilitator 的預算控管
        and out_path.exists()
        and EVENTS_PATH.exists()
    )
    if not ok:
        print("\n驗收未通過，請檢查上方輸出。")
        sys.exit(2)
    print("\n驗收通過。")


if __name__ == "__main__":
    main()
