"""
階段 7：大師點評 + 集體智慧庫（真實向量庫 Chroma，跨輪 RAG 記憶）

目標：Facilitator 收斂會議後，三位大師（技術／商業／策略）對整場最終
提案組合給高階點評；點評、最終提案摘要、訪談洞見一起寫入 **Chroma**
（真實語意 embedding，不是 stage1-6 用的 feature-hashing）。下一輪
（不同主題但同一個 Chroma 資料庫）做功課時，每位 persona 先查詢過去
輪次的相關記憶，寫研究彙整與提案時可以引用。

對應 PLAN.md「階段 7」驗收：
- 三大師點評整場最終提案
- 點評／訪談洞見寫入 Chroma，帶 metadata（round_id/topic/doc_type）
- 跑兩輪不同主題，第二輪能引用第一輪的相關結論與訪談反應
  （recall 命中數 > 0），引用可追溯到向量庫原始文件

本檔是 stage 6 的完整獨立副本再擴充（不 import stage6）。做功課子圖、
同儕互評子圖、Facilitator、HITL 逐行沿用 stage6；新增的部分：
`recall_memory`（做功課子圖新節點）、`memory_refs`（提案協議新欄位）、
`master_panel_graph` + `run_masters`（父圖新步驟）、`write_wisdom`
（父圖新步驟，寫入 Chroma）。

執行前在 practice/.env 設定 ANTHROPIC_API_KEY（見 .env.example）。
"""
from __future__ import annotations

import argparse
import hashlib
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
CHECKPOINT_DB_PATH = OUTPUT_DIR / "stage7_checkpoints.sqlite"
CHROMA_DIR = PRACTICE_DIR / "chroma_db"  # 已在 .gitignore（集體智慧庫，含會議內容）

_env_file = PRACTICE_DIR / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _key, _, _value = _line.partition("=")
            os.environ.setdefault(_key.strip(), _value.strip())

CHEAP_MODEL = "claude-haiku-4-5-20251001"
FACILITATOR_MODEL = "claude-sonnet-5"  # Facilitator／大師都用這個，沿用 PLAN.md 的模型分級
client = anthropic.Anthropic(timeout=90.0)  # 真實跑測踩過：round2 有一次呼叫卡住 3+ 小時
# 完全沒有錯誤、沒有進度（SDK 預設 timeout=600s 理論上該擋住，但這次沒生效，
# 疑似 sandbox 網路環境的邊界情況），process 卡死到只能手動 kill，白白浪費
# 已經跑完的前面步驟（無法斷點續跑，見 stage6 note 的『checkpoint 只在整個
# super-step 成功才前進』）。改成更短的顯式 timeout，讓單次呼叫最多卡 90 秒
# 就丟例外，SDK 內建的 max_retries=2 會先重試，真的兩次都失敗才會讓
# call_llm 往上拋，走既有的『崩潰』路徑（已知行為）而不是無限期掛著。

DEDUP_SIMILARITY_THRESHOLD = 0.80
IDEA_DEDUP_THRESHOLD = 0.75
EMBED_DIM = 256
REFINE_ROUNDS = 3
INTERVIEW_ROUNDS = 3

MAX_ROUNDS = 6
MAX_BUDGET_USD = 1.2

RECALL_N_RESULTS = 3       # 每位 persona 最多引用幾筆跨輪記憶
RECALL_MAX_DISTANCE = 1.0  # cosine 距離門檻，越小越相關；真實跑測校準見 note.md

MASTERS = [
    {
        "id": "tech_master", "name": "技術大師",
        "angle": "技術可行性、架構與資料風險、能不能規模化執行",
    },
    {
        "id": "biz_master", "name": "商業大師",
        "angle": "商業模式健全度、單位經濟（誰付錢、成本結構撐不撐得住）",
    },
    {
        "id": "strategy_master", "name": "策略大師",
        "angle": "跟公司定位的契合度、長期護城河、機會成本",
    },
]

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
    """`dict.get(key)` 只保證有沒有這個鍵，不保證值的型別——stage6 真實跑測
    踩過模型把預期是字串的欄位吐成 list 導致 `.strip()` 崩潰，這裡統一
    處理成：非字串一律當空字串，交給既有的『結構性保底』邏輯補上預設值。"""
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
# Embedding（純 Python feature hashing——仍用在搜尋結果去重／多樣性度量；
# 跨輪記憶已升級成 Chroma 的真實語意 embedding，見上面 get_chroma_collection）
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
    """跟 count_real_citations／count_real_insight_refs 同一種『可驗證引用』
    設計——提案宣稱引用了跨輪記憶，id 必須真的存在於這次 recall 到的結果裡，
    不能空口說『參考過去經驗』。"""
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
# 做功課子圖 state（延伸自 stage6：新增 round_id、recalled_memory）
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
        extra={"queries": queries, "n_results": len(raw)},
    )
    return {"raw_results": raw}


def dedup(state: HomeworkState) -> dict:
    raw = state["raw_results"]
    items = dedup_by_embedding(raw)
    print(f"  [dedup:{state['persona'].get('name')}] {len(raw)} → {len(items)}（門檻 {DEDUP_SIMILARITY_THRESHOLD}）")
    emit_event("dedup", f"embedding 去重 {len(raw)} → {len(items)}")
    return {"research_items": items}


def recall_memory(state: HomeworkState) -> dict:
    """本階段核心新節點：查 Chroma 找過去輪次的相關集體智慧／訪談洞見。
    排除自己這一輪剛寫進去的資料（用 round_id 過濾），確保引用的是真的
    『跨輪』記憶，不是自己講過的話繞了一圈回來。"""
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
    for h in hits:
        print(f"    - [{h['id']}] dist={h['distance']} ({h['collection']}/{h['topic']}): {h['text'][:40]}")
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
    brief = call_llm(CHEAP_MODEL, system, user, max_tokens=1200)
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
    raw = call_llm(CHEAP_MODEL, system, user, max_tokens=500)
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
    return call_llm(CHEAP_MODEL, system, prompt, max_tokens=300).strip()


def generate_followup_question(persona: dict, prior_turns: List[dict]) -> str:
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
    raw = call_llm(CHEAP_MODEL, system, user, max_tokens=800)
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
    raw = call_llm(CHEAP_MODEL, system, user, max_tokens=500)
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
    proposal["pov"] = state["pov"]
    proposal["hmw"] = state["hmw"]
    insights = state["insights"]
    known_ids = {i.get("id") for i in insights if i.get("id")}
    refs = [r for r in (proposal.get("insight_refs") or []) if r in known_ids]
    if not refs and insights:
        refs = [insights[0]["id"]]
    proposal["insight_refs"] = refs
    # memory_refs 不強制非空——round1 或查無相關記憶時，空陣列就是誠實的答案
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
        extra={"bmc_missing": assert_bmc_complete(proposal)},
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
    """做功課子圖：collect→dedup→recall_memory→synthesize→…→refine×3。
    `recall_memory` 是本階段唯一新插入的節點，位置刻意放在 dedup 之後、
    synthesize 之前——去重先把搜尋結果整理乾淨，再去查跨輪記憶，兩者互不
    依賴但邏輯上先『這輪的新資訊』再『過去的舊智慧』比較自然。"""
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
# 同儕互評子圖（逐行同 stage6）
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
        raw = call_llm(CHEAP_MODEL, system, user, max_tokens=700)
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
        raw = call_llm(CHEAP_MODEL, system, user, max_tokens=2000)
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
# 大師點評子圖（新，stage7 核心之一）
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
        # max_tokens 開大一點：FACILITATOR_MODEL 真實跑測踩過『整個預算被
        # extended thinking 吃光、連 retry 兩次後都吐不出一個字』的崩潰
        # （見 facilitator_decide 同一個模型的完整踩坑記錄），這裡先開夠，
        # 不依賴 call_llm 的 retry-doubling 機制兜底。
        raw = call_llm(FACILITATOR_MODEL, system, user, max_tokens=2000)
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
# 集體智慧庫（Chroma 寫入）
# ---------------------------------------------------------------------------

def write_collective_wisdom(
    *,
    round_id: str,
    topic: str,
    master_critiques: List[dict],
    idea_pool_versions: List[dict],
    persona_results: List[dict],
) -> dict:
    """把這一輪的大師點評、最終提案摘要、訪談洞見寫進 Chroma，供之後的
    輪次 `recall_memory` 檢索。回傳寫入筆數方便驗收與存檔。"""
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
    # 只取每位 persona 最終（最後一次修正後）的版本，不是每一版都寫進去
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
    if idocs:
        interviews_col.add(documents=idocs, metadatas=imetas, ids=iids)

    return {"wisdom_written": len(docs), "interviews_written": len(idocs)}


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
    human_qa_log: Annotated[List[dict], operator.add]
    review_log: Annotated[List[dict], operator.add]
    idea_pool_versions: Annotated[List[dict], operator.add]
    facilitator_log: Annotated[List[dict], operator.add]
    master_critiques: Annotated[List[dict], operator.add]
    wisdom_stats: dict


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
    """主持人（supervisor）：逐行沿用 stage6，唯一差異是收斂後不直接
    `goto=END`，而是 `goto="run_masters"`——會議結束後還有大師點評與
    集體智慧寫入兩個新步驟才是真正的終點。"""
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
        "\"reason\": \"<=50字，你的判斷理由\"}"
    )
    user = (
        f"與會者發言次數：\n{summary_block}\n\n"
        f"尚未發表過的人：{[p.get('name') for p in never_presented] or '（無，大家都發表過了）'}\n\n"
        f"最近幾輪決策：\n{recent_log}\n\n"
        f"最近幾輪收到的異議：\n{_recent_review_summary(state)}"
    )
    role_token = _event_role.set("facilitator")
    try:
        # 真實跑測踩過的坑：max_tokens=300 太小，FACILITATOR_MODEL 這次判斷
        # 陷入 extended thinking，把整個預算耗在 ThinkingBlock 上，兩次重試
        # （300→600）後依然沒有半個 TextBlock，call_llm 判定「沒有 text
        # block」直接崩潰——而且是發生在第 5 輪，之前 4 輪已經花的錢全部
        # 泡湯（跟 stage6 note 記錄的『checkpoint 只在整個 super-step 成功
        # 才前進』是同一個代價）。改成直接開大到 2000，不依賴重試機制兜底。
        raw = call_llm(FACILITATOR_MODEL, system, user, max_tokens=2000)
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
    payload = {
        "presenter_id": pid,
        "presenter_name": presenter.get("name"),
        "proposal_title": proposal.get("title"),
        "proposal_summary": proposal.get("summary"),
        "questions_asked_so_far": asked_so_far,
        "prompt": (
            f"{presenter.get('name')} 剛發表《{proposal.get('title')}》。"
            "要提問嗎？可連續問多題，輸入空字串／skip 結束提問進入互評。"
        ),
    }
    answer_signal = interrupt(payload)
    if isinstance(answer_signal, dict) and answer_signal.get("action") == "ask":
        question = _safe_str(answer_signal.get("question"))
        if question:
            return {"pending_question": question}
    return {"pending_question": None}


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
        answer = call_llm(CHEAP_MODEL, system, user, max_tokens=400).strip()
    finally:
        _event_role.reset(role_token)
    qa_entry = {
        "presenter_id": presenter.get("id"),
        "presenter_name": presenter.get("name"),
        "question": question,
        "answer": answer,
    }
    emit_event(
        "human_qa",
        f"人類問 {presenter.get('name')}：{question}",
        role=_persona_label(presenter),
        extra=qa_entry,
    )
    print(f"  [human_qa] Q: {question}\n             A: {answer}")
    return {"human_qa_log": [qa_entry], "pending_question": None}


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
    """會議收斂後才跑：三位大師看的是最終（含所有修正）的提案組合。"""
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
    )
    emit_event("write_wisdom", f"寫入集體智慧庫：{stats}", extra=stats)
    print(f"  [write_wisdom] {stats}")
    return {"wisdom_stats": stats}


def build_parent_graph(checkpointer):
    g = StateGraph(MeetingState)
    g.add_node("homework_worker", instrument("homework_worker", homework_worker))
    g.add_node("facilitator_decide", instrument("facilitator_decide", facilitator_decide))
    g.add_node("ask_question", instrument("ask_question", ask_question))
    g.add_node("answer_question", instrument("answer_question", answer_question))
    g.add_node("run_peer_review", instrument("run_peer_review", run_peer_review))
    g.add_node("run_masters", instrument("run_masters", run_masters))
    g.add_node("write_wisdom", instrument("write_wisdom", write_wisdom))
    g.add_conditional_edges(START, fan_out_personas, ["homework_worker"])
    g.add_edge("homework_worker", "facilitator_decide")
    # facilitator_decide 的下一步（"ask_question" 或 "run_masters"）由它自己
    # 回傳的 Command(goto=...) 決定，不需要（也不能同時）再配路由函式。
    g.add_conditional_edges(
        "ask_question", route_after_question,
        {"answer_question": "answer_question", "run_peer_review": "run_peer_review"},
    )
    g.add_edge("answer_question", "ask_question")
    g.add_edge("run_peer_review", "facilitator_decide")
    g.add_edge("run_masters", "write_wisdom")
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
    baseline_proposal: dict,
    baseline_metrics: dict,
    diversity_before: dict,
    diversity_after: dict,
) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = {
        "run_id": stamp,
        "round_id": round_id,
        "topic": topic,
        "persona_count": len(personas),
        "user_count": len(users),
        "facilitator_log": facilitator_log,
        "human_qa_log": human_qa_log,
        "idea_pool_versions": idea_pool_versions,
        "review_log": review_log,
        "master_critiques": master_critiques,
        "wisdom_stats": wisdom_stats,
        "recall_hits_total": recall_hits_total,
        "diversity_before_review": diversity_before,
        "diversity_after_review": diversity_after,
        "baseline": {"proposal": baseline_proposal, "metrics": baseline_metrics},
        "total_cost_usd": round(total_cost(), 6),
    }
    path = OUTPUT_DIR / f"stage7-run-{stamp}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUTPUT_DIR / f"stage7-latest-{topic[:12]}.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


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
        # 拋了未捕捉的例外（真實跑測踩過：facilitator_decide 崩潰）——兩種
        # 情況的續跑方式不同，不能都當成「有 interrupt payload 可以解析」。
        # `task.interrupts` 是空 tuple 就代表是崩潰、不是 interrupt()，這時
        # 用 `invoke(None, config)` 讓 LangGraph 直接重跑那個崩潰的節點，
        # 不能硬塞一個空 payload 進 get_human_input()（會 KeyError）。
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
    parser = argparse.ArgumentParser(description="Stage 7：大師點評 + 集體智慧庫（Chroma 跨輪記憶）")
    parser.add_argument("--thread", default=None)
    parser.add_argument("--script", default=None)
    parser.add_argument("--stop-after-first-interrupt", action="store_true")
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("缺少 ANTHROPIC_API_KEY。請複製 practice/.env.example 為 practice/.env 並填入。")
        sys.exit(1)

    thread_id = args.thread or f"meeting-{uuid.uuid4().hex[:8]}"
    round_id = thread_id  # 一個 thread = 一場會議 = 一個跨輪記憶的 round_id
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
            "human_qa_log": [],
            "review_log": [],
            "idea_pool_versions": [],
            "facilitator_log": [],
            "master_critiques": [],
            "wisdom_stats": {},
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
        baseline_proposal=baseline_proposal,
        baseline_metrics=baseline_metrics,
        diversity_before=diversity_before,
        diversity_after=diversity_after,
    )

    print()
    print("=== 大師點評 ===")
    for m in master_critiques:
        print(f"  {m['master_name']}（{m['angle']}）")
        print(f"    {m['critique']}")
        print(f"    首選：{m['top_pick_persona']}")

    print()
    print("=== 集體智慧庫 ===")
    print(f"這輪寫入：wisdom +{wisdom_stats.get('wisdom_written', 0)}，interviews +{wisdom_stats.get('interviews_written', 0)}")
    print(f"這輪之前 Chroma 既有筆數：wisdom={wisdom_count_before}, interviews={interviews_count_before}")
    print(f"這輪 recall 命中總數（跨所有 persona）：{recall_hits_total}")
    if wisdom_count_before == 0 and interviews_count_before == 0:
        print("（Chroma 是空的——這是第一輪，recall 命中數預期為 0，之後的輪次才有得檢索）")

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
    memory_refs_verifiable = True
    for r in persona_results:
        proposal = r["proposal"]
        refs = proposal.get("memory_refs") or []
        if refs:
            known = {m["id"] for m in (r.get("recalled_memory") or [])}
            if not all(ref in known for ref in refs):
                memory_refs_verifiable = False

    print(f"每人至少發表一次：{'是' if everyone_presented else '否'}（{len(presented_ids)}/{len(personas)}）")
    print(f"發表總次數：{len(idea_pool_versions)}（硬上限 {MAX_ROUNDS}）：{'未超過' if within_round_cap else '超過！'}")
    print(f"互評每則恰好 3/3/3：{'是' if reviews_shape_ok else '否'}")
    print(f"三大師皆有點評：{'是' if masters_ok else '否'}")
    print(f"memory_refs 皆可追溯到真實 recall 結果：{'是' if memory_refs_verifiable else '否'}")
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
        and memory_refs_verifiable
        and total_cost() <= MAX_BUDGET_USD + 0.05
        and out_path.exists()
        and EVENTS_PATH.exists()
    )
    if not ok:
        print("\n驗收未通過，請檢查上方輸出。")
        sys.exit(2)
    print("\n驗收通過。")


if __name__ == "__main__":
    main()
