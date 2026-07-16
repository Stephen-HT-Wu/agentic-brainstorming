"""
Stage 15（stage15-market-fit）：策略導向、由上而下的市場競爭力驗證
流程——打破 stage9-14 一路沿用的「由下而上發現問題」框架。

stage9-14 全部都建立在「問題本身需要被發現/驗證」這個前提上：desk
research 假設候選 job → 訪談驗證 → 收斂成一個問題陳述 → 才開始想解法。
但真實世界常見的另一種情境是策略方向由公司高層直接給定（例如「APP
建立會員付費訂閱加值功能機制」），不需要再驗證「這個方向對不對」——
高層已經決定了。這時候真正要快速回答的問題是：「在這個既定策略方向
下，做什麼樣的具體功能會有市場競爭力？」這個 stage 因此整個跳過
Discover/Define（假設候選 job、訪談驗證、收斂成一個問題陳述），`topic`
的語意從「待發現的開放問題」變成「公司給定的策略方向」。

**歷史教訓（設計時必須避開）**：stage9-12 的 `analyze_and_scope()` 曾經
在任何訪談發生之前，就用一次 LLM 呼叫「發明」一個 `strategic_goal`，
導致嚴重的多樣性坍縮（`idea_diversity.avg_distance` 量到
0.2382/0.2537/0.2821，全部低於「同一個 idea 換句話說」的校準基準值
0.579——見 stage12/note.md）。這次的策略方向雖然是外部合法給定的（不是
LLM 自己編的），但同樣的坍縮風險會在功能層級重現：如果流程讓某一步在
市場驗證之前就收斂成「一個」功能提案，一樣會複製這個 bug。設計上維持
「策略方向給定，但候選功能提案仍然平行、多元」的結構，驗證完才收斂
（`pick_winner`）。

新拓樸：`research_competitive_landscape`（真實 web_search 找競品，程式碼
驗證 source_url 確實來自搜尋結果，不信任 LLM 自稱——取代 Discover/
Define 整個階段）→ `assemble_persona_team`（沿用 `derive_company_domains()`
從 company.md 衍生彼此不同的職能）→ `draft_one_feature`（每位參與者
獨立發想一個功能提案，明確要求對比真實競品講差異化，帶
target_segment/monetization_mechanism）→ `ask_question`/`answer_question`
（HITL，原封不動沿用）→ `validate_market_fit`（模式 B，依序同步呼叫
虛擬問卷/概念測試訪談/DFV 四面向評分三個子圖，第 4 個「市場競爭力」
lens 讀前兩者的彙整結果當佐證）→ `pick_winner` → `generate_prototype` →
`generate_evaluators`。

節點拓樸是「兩種已驗證安全的 fan-in 模式」（Send() fan-out 到同一個
節點名稱；單一節點內同步呼叫 subgraph.invoke()）遞迴組合而成，不引入
任何新的 join 寫法——這是 stage12/note.md 記錄的真實教訓：LangGraph
對「兩條長度不同的分支都指到同一個節點」不保證等全部前驅完成才觸發。

本檔以 stage14-signals/graph.py 為起點複製再重構（不 import
stage14-signals），延續「每個 stage 一份完整獨立副本」的既有慣例。底層
工具（web 搜尋、embedding/dedup、call_llm/emit_event、BMC 量化與合併、
JSON 解析、landing page 原型渲染、baseline、盲測評分、HITL 驅動迴圈、
DFV 評分機制、pick_winner）直接沿用既有實作，只有取代 Discover/Define
的部分跟 Develop/Deliver 前段的節點邏輯與圖拓樸重寫。

執行前在 practice/.env 設定 ANTHROPIC_API_KEY（見 .env.example）。
"""
from __future__ import annotations

import argparse
import hashlib
import html as html_lib
import itertools
import json
import math
import operator
import os
import random
import re
import sqlite3
import sys
import time
import uuid
import warnings
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock, Thread
from typing import Annotated, Any, List, Literal, Optional, TypedDict

import anthropic
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
# 獨立於其他 stage 的檔名，CLI 裸跑（不透過 run_worker.py 的 per-run
# monkeypatch）時才不會跟其他 stage 的事件流/checkpoint 共用同一份
# 檔案——這個專案稍早踩過「checkpoint db 一直存在導致 is_fresh_thread
# 判斷失準、events.jsonl 從未被清空」的真實 bug，兩個 stage 用不同檔名
# 從根本上避開同一個問題。
EVENTS_PATH = OUTPUT_DIR / "stage15_events.jsonl"
CHECKPOINT_DB_PATH = OUTPUT_DIR / "stage15_checkpoints.sqlite"
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
client = anthropic.Anthropic(timeout=90.0, max_retries=5)  # stage7 真實跑測踩過 3+ 小時網路卡死，見 stage7 note
# 5-Whys 把訪談輪數從 2 拉到 5，一場會議的 LLM 呼叫量大增，撞到 Anthropic
# API 暫時性過載（529 Overloaded）的機率也跟著變高——真實跑測撞過 SDK
# 預設 max_retries=2 扛不過去、整場會議直接崩潰的情況，加大到 5 次重試
# （SDK 內建指數退避＋jitter，不用自己刻重試迴圈）。

DEDUP_SIMILARITY_THRESHOLD = 0.80
EMBED_DIM = 256

# stage15-market-fit：策略方向由公司高層給定，不需要 Discover/Define
# 階段的候選 job 假設/訪談驗證，所以拿掉 N_CANDIDATE_JOBS 這類「找問題」
# 用的常數。訪談變成 validate_market_fit 內部的簡化版概念測試（1-2 輪
# 固定問題，不是 5 輪 JTBD switch）——為了保持簡單、符合「快速驗證」的
# 精神，概念測試對象是同一組固定人數的訪談對象小組，對每個候選功能都問
# 一輪，不是像 stage14-signals 那樣為每個候選 job 各自動態生成專屬訪談
# 對象（避免多一次「依 target_segment 生成人設」的 LLM 呼叫與 id 對應
# 複雜度，這個決策不影響拓樸設計）。
N_CONCEPT_TEST_INTERVIEWEES = 3  # 概念測試訪談小組人數（跑時可被
                                  # state["n_concept_test_interviewees"]
                                  # 覆蓋，這裡只是沒有指定時的預設值）
N_PERSONAS = 5                 # Develop 階段扣著公司能力衍生職能的參與
                                # 者數（從 stage12 的 3 提高，畢竟沒有
                                # 成本上限，且職能覆蓋度值得多留幾個席位）
N_EVALUATORS = 3

# 使用者要求把三位大師的簡短點評改成結構化評分——每位評審只負責一個
# 面向，給 0-10 分＋一大段文字批評，收斂時把全部面向的分數加總、選總分
# 最高的 idea（見 pick_winner()，加總邏輯 agnostic 於 lens 數量）。
# stage15-market-fit 新增第 4 個「市場競爭力」lens：策略方向由公司給定、
# 不驗證「這個方向對不對」之後，真正要快速驗證的是「這個功能有沒有
# 市場競爭力」——這個 lens 的 prompt 會額外帶入 competitive_landscape／
# survey_summary／concept_test_summary 當佐證，見 score_one_dimension()
# 對 market_fit lens 的特別處理。
DFV_LENSES = [
    {"id": "desirability", "name": "顧客需求性評審", "dimension": "desirability",
     "angle": "使用者真的想要、會用嗎？是不是解決了真實痛點？"},
    {"id": "feasibility", "name": "技術可行性評審", "dimension": "feasibility",
     "angle": "技術上做不做得出來？架構與資料風險有多大？"},
    {"id": "viability", "name": "商業存續性評審", "dimension": "viability",
     "angle": "商業模式撐不撐得住？誰付錢、划不划算？"},
    {"id": "market_fit", "name": "市場競爭力評審", "dimension": "market_fit",
     "angle": "跟現有競品比，這個功能有沒有明確差異化？市場上買不買單？"},
]

BMC_KEYS = [
    "客群", "價值主張", "通路", "顧客關係", "收益流", "關鍵資源", "關鍵活動", "關鍵夥伴", "成本結構",
]

# 「收益流」「成本結構」是結構化物件（narrative + monthly_estimate_twd +
# basis），才能算出淨利、判斷這個商業模式划不划算。其餘七格維持一句話
# 文字。stage12 全場只算一份共用 BMC（第 1 點確認：persona 各自發想的
# idea 不再各自帶 BMC），這份常數跟合併/驗證邏輯原封不動沿用 stage9。
QUANTIFIED_BMC_KEYS = ["收益流", "成本結構"]

usage_log: list = []
_event_role: ContextVar[str] = ContextVar("event_role", default="system")
_events_lock = Lock()


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
        text_parts = [block.text for block in response.content if block.type == "text"]
        # 真實跑測踩過：extended thinking 有時會把整個 token 預算耗在
        # thinking 內容上，完全沒剩空間給真正的文字輸出——這種情況
        # stop_reason 不一定是 "max_tokens"（可能是 "end_turn"），原本
        # 只看 stop_reason 的重試邏輯接不住，直接炸掉整場會議。「沒有
        # text block」本質上跟「被截斷」是同一種預算不足的問題，用同一招
        # 加大 max_tokens 重試。
        if (stop_reason == "max_tokens" or not text_parts) and attempt == 0:
            print(f"  [call_llm] 截斷或無文字輸出（max_tokens={tokens}，stop_reason={stop_reason}），加大重試…")
            continue
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


def _bmc_quant_cell_valid(val) -> bool:
    return (
        isinstance(val, dict)
        and isinstance(val.get("narrative"), str) and bool(val["narrative"].strip())
        and isinstance(val.get("monthly_estimate_twd"), (int, float))
        and not isinstance(val.get("monthly_estimate_twd"), bool)
    )


def _bmc_cell_filled(key: str, val) -> bool:
    if key in QUANTIFIED_BMC_KEYS:
        return _bmc_quant_cell_valid(val)
    return isinstance(val, str) and bool(val.strip())


def _format_bmc_line(key: str, val) -> str:
    """報告用的 markdown 一行——量化格是物件，直接 f-string 印會變成
    Python dict repr，這裡格式化成「敘述＋金額＋依據」的可讀文字。"""
    if key in QUANTIFIED_BMC_KEYS and isinstance(val, dict):
        basis = f"（依據：{val.get('basis')}）" if val.get("basis") else ""
        return f"{val.get('narrative', '')}，估算 NT${val.get('monthly_estimate_twd', 0):,.0f}/月{basis}"
    return str(val or "")


def assert_bmc_complete(proposal: dict) -> List[str]:
    bmc = proposal.get("bmc") or {}
    issues = []
    for key in BMC_KEYS:
        val = bmc.get(key)
        if key in QUANTIFIED_BMC_KEYS:
            if not _bmc_quant_cell_valid(val):
                issues.append(f"缺漏或無效:{key}")
        elif not isinstance(val, str) or not val.strip():
            issues.append(f"缺漏或無效:{key}")
    issues.extend(f"額外欄位:{key}" for key in bmc if key not in BMC_KEYS)
    return issues


def _merge_bmc_cell(key: str, candidate_val, prev_val):
    if key in QUANTIFIED_BMC_KEYS:
        if _bmc_quant_cell_valid(candidate_val):
            return {
                "narrative": candidate_val["narrative"].strip(),
                "monthly_estimate_twd": float(candidate_val["monthly_estimate_twd"]),
                "basis": _safe_str(candidate_val.get("basis")),
            }
        return prev_val if isinstance(prev_val, dict) else {"narrative": "", "monthly_estimate_twd": 0.0, "basis": ""}
    return candidate_val if isinstance(candidate_val, str) and candidate_val.strip() else (prev_val or "")


def _merge_bmc(candidate_bmc: dict, prev_bmc: dict) -> dict:
    candidate_bmc = candidate_bmc or {}
    prev_bmc = prev_bmc or {}
    return {key: _merge_bmc_cell(key, candidate_bmc.get(key), prev_bmc.get(key)) for key in BMC_KEYS}


def compute_unit_economics(bmc: dict) -> dict:
    """純函式、零額外 LLM 成本：從量化後的收益流/成本結構算出淨利，
    是 refine()/revise_after_feedback()/co_create_turn() 軟性引導的依據。"""
    bmc = bmc or {}
    revenue = (bmc.get("收益流") or {}).get("monthly_estimate_twd") or 0
    cost = (bmc.get("成本結構") or {}).get("monthly_estimate_twd") or 0
    margin = revenue - cost
    return {
        "monthly_revenue_twd": revenue,
        "monthly_cost_twd": cost,
        "monthly_margin_twd": margin,
        "is_viable": margin > 0,
    }


def _viability_nudge(prev: dict) -> str:
    """軟性引導（使用者確認的做法）：把估算損益回饋進修正迴圈的 prompt，
    不划算時提醒 LLM 認真考慮換方向，而不是新增一個獨立的可行性關卡節點。"""
    ue = prev.get("unit_economics") or compute_unit_economics(prev.get("bmc") or {})
    note = (
        f"上一版估算：月收入 {ue['monthly_revenue_twd']:.0f} 元、"
        f"月成本 {ue['monthly_cost_twd']:.0f} 元、淨利 {ue['monthly_margin_twd']:+.0f} 元。"
    )
    if ue["is_viable"]:
        note += "目前打平或有賺，可以繼續深化。"
    else:
        note += "這樣不划算——這一輪不要只是微調用詞，請認真考慮換一個核心價值主張或商業模式方向。"
    return note


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


def metrics_of(proposal: dict, research_items: List[dict], insights: List[dict], cost: float) -> dict:
    missing = assert_bmc_complete(proposal)
    return {
        "bmc_complete": len(missing) == 0,
        "bmc_missing": missing,
        "bmc_filled": sum(
            _bmc_cell_filled(k, (proposal.get("bmc") or {}).get(k))
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
# 訪談模擬共用工具
# ---------------------------------------------------------------------------

def _persona_label(persona: dict) -> str:
    return f"persona:{persona.get('name', persona.get('id', '?'))}"


def _user_system_prompt(user: dict) -> str:
    return (
        f"你是 {user['name']}，{user.get('age', '')} 歲。"
        f"情境：{user.get('context', '')}。"
        f"平常的困擾：{'；'.join(user.get('pain_points') or [])}。"
        f"說話風格：{user.get('tone', '')}。"
        "你完全不知道對方在幫公司做產品研究，只是在回答一位訪談者的問題。"
        "只根據你自己真實的生活情境回答，不要幫忙想點子、不要談商業模式；"
        "如果被問到解法，就誠實說你現在實際上怎麼應付這件事（可能很克難、"
        "不方便，或其實你根本沒認真想過怎麼解決），用你自己的話具體描述"
        "細節。回答 1-3 句話，符合你的說話風格，不要條列。"
    )


def simulate_user_answer(user: dict, question: str, prior_turns: List[dict]) -> str:
    system = _user_system_prompt(user)
    history = "\n".join(f"Q: {t['question']}\nA: {t['answer']}" for t in prior_turns) or "（尚無先前對話）"
    prompt = f"先前對話：\n{history}\n\n新問題：{question}"
    return call_llm(SMART_MODEL, system, prompt, max_tokens=300).strip()


# ---------------------------------------------------------------------------
# Landing page 原型渲染工具
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


def generate_prototype(state: MeetingState) -> dict:
    """使用者要求原型只生成一次，不測試不修正（第 2 點）——比 stage9
    `generate_prototype_and_test()` 少了『用模擬使用者測試再修正一次』
    的迴圈，直接生成 landing page 就送去給評估者打分。BMC 現在是贏家
    idea 自己在 `draft_one_idea` 時設計的那一份（不再是全場共用一份，
    見 `draft_one_idea` 的說明——共用 BMC 被實測發現會壓低點子多樣性）。"""
    idea = state["winner_idea"]
    bmc = idea.get("bmc") or {}
    proposal_like = {"title": idea.get("title"), "summary": idea.get("summary"), "bmc": bmc}
    role_token = _event_role.set(f"persona:{idea.get('persona_name')}")
    try:
        system = (
            f"你是{idea.get('persona_name')}，要把這個 idea 包裝成 landing page 文案，"
            "吸引 target audience 點進來。"
            + _LANDING_PAGE_SCHEMA_HINT
        )
        user = (
            f"標題：{idea.get('title')}\n摘要：{idea.get('summary')}\n"
            f"BMC：{json.dumps(bmc, ensure_ascii=False)}"
        )
        raw = call_llm(SMART_MODEL, system, user, max_tokens=1200)
        data = extract_json_object(raw)
        if not data:
            data = extract_json_object(repair_json_text(raw))
        data = _ensure_landing_page_fields(data or {}, proposal_like)
        page_html = render_landing_page_html(data, proposal_like)
        PROTOTYPE_DIR.mkdir(parents=True, exist_ok=True)
        html_path = PROTOTYPE_DIR / f"{state['round_id']}-winner.html"
        html_path.write_text(page_html, encoding="utf-8")
        prototype = {
            "idea_id": idea.get("id"), "persona_name": idea.get("persona_name"),
            "title": idea.get("title"), "summary": idea.get("summary"), "bmc": bmc,
            "landing_page": data, "html_path": str(html_path),
        }
        emit_event(
            "generate_prototype", f"{idea.get('persona_name')} 的原型：{data['headline']}",
            extra={"html_path": str(html_path), "prototype": prototype},
        )
        print(f"  [generate_prototype] {data['headline']} → {html_path}")
    finally:
        _event_role.reset(role_token)
    return {"prototype": prototype}


# ---------------------------------------------------------------------------
# 父圖
# ---------------------------------------------------------------------------

class MeetingState(TypedDict):
    # stage15-market-fit：topic 的語意從「待發現的開放問題」變成「公司
    # 給定的策略方向」（例如「APP 建立會員付費訂閱加值功能機制」）——
    # 不需要驗證這個方向對不對，高層已經決定了，這裡跳過整個 Discover/
    # Define（假設候選 job、訪談驗證、收斂成一個問題陳述），直接從
    # 「這個策略方向下，什麼功能有市場競爭力」出發。
    topic: str
    company: str
    round_id: str
    # ---- 策略方向 + 市場現況（取代 Discover/Define）----
    # research_competitive_landscape 產出：真實搜尋到的競品名稱/功能/
    # 來源網址（程式碼驗證過 source_url 確實出現在這次 web_search() 的
    # 結果裡，不是 LLM 自稱），見該函式 docstring 對舊版五力分析空泛問題
    # 的分析
    competitive_landscape: List[dict]
    used_fallback_competitive_landscape: bool
    research_queries: List[str]
    research_items: List[dict]
    # ---- Develop（發散：功能提案）----
    # assemble_persona_team 產出：扣著公司實際能力衍生職能的腦力激盪參與者
    personas: List[dict]
    used_fallback_personas: bool
    # draft_one_feature 產出：N 位 persona 各自獨立發想 1 個具體功能提案，
    # 各自帶自己設計的 bmc，以及 target_segment／monetization_mechanism／
    # differentiation_vs_competitors（跟 competitive_landscape 裡的真實
    # 競品做具體對比，不能空泛喊「更好用」）
    ideas: Annotated[List[dict], operator.add]
    # ask_question（HITL，僅保留的互動環節）
    pending_question: Optional[str]
    pending_question_target_idea_id: Optional[str]
    pending_question_asked_by: Optional[str]
    human_qa_log: Annotated[List[dict], operator.add]
    # ---- validate_market_fit（收斂前的快速市場驗證）----
    # 依序同步呼叫三個子圖，見 validate_market_fit() 的 docstring：
    # 虛擬問卷（購買意願/差異化感受）→ 概念測試訪談（簡化版，1-2 輪）→
    # DFV 四面向結構化評分（新增「市場競爭力」lens，讀前兩步的彙整結果
    # 當佐證）
    n_concept_test_interviewees: int
    survey_respondents_per_stratum: int
    survey_results: Annotated[List[dict], operator.add]
    survey_summary: dict
    concept_test_results: Annotated[List[dict], operator.add]
    concept_test_summary: dict
    dfv_scores: Annotated[List[dict], operator.add]
    # pick_winner 產出：加總選出的最終 idea（agnostic 於 lens 數量，4 個
    # lens 不用改這個函式）
    winner_idea: dict
    idea_diversity: dict
    # generate_prototype 產出：單次生成，不修正
    prototype: dict
    # generate_evaluators 產出：動態生成、跟全部候選功能訪談對象都不重複
    # 的最終評估者
    evaluators: List[dict]
    used_fallback_evaluators: bool
    # 注意：baseline_proposal／user_evaluation／final_verdict 不在這個
    # state 裡——它們不是圖節點的產出，是 main() 在 run_meeting() 跑完
    # 之後才計算的（見 build_parent_graph() 的 docstring 解釋原因）。


def research_competitive_landscape(state: MeetingState) -> dict:
    """取代整個 Discover/Define：策略方向（`topic`）由公司高層給定，不
    需要驗證「這個方向對不對」——高層已經決定了。這裡只做市場現況掃描：
    找真實存在的競品，讓後面的功能提案跟評分有具體的市場事實可以對比，
    不是憑空喊「有競爭力」。

    真實踩過的坑（`stage14-signals` 的舊版五力分析「現有競爭者強度」
    欄位長期產出空泛描述，例如「競爭激烈，同質化程度高」這類換主題也
    差不多的話術）：追查根因有兩層——(1) 搜尋詞太籠統
    （`f"{topic} 競爭者 替代方案"`），搜到的多半是產業趨勢文章，不是
    具體競品頁面；(2) 輸出欄位只要求一個 <=60 字的抽象強度描述，沒有
    結構性要求引用具體事實，LLM 在沒有證據時自然滑向安全、泛用的說法。
    這個節點的存在意義就是要做到舊版做不到的具體競品事實，所以：
    - 搜尋詞刻意鎖定「找得到具體名字」（品牌/APP/服務名稱），不是
      「找產業趨勢」
    - 解析後**用程式碼驗證**每筆競品的 `source_url` 是否真的出現在這次
      `web_search()` 的結果網址清單裡——不信任 LLM 自稱，跟
      `draft_one_feature()` 的 `sources` 驗證、stage14-signals（舊版）
      `_synthesize_job_evidence()` 的 `insight_refs` 驗證是同一個「事後
      用程式驗證，不信任模型自稱」原則。驗證不過的條目直接丟棄，不硬湊
      數量；一筆都驗證不過就誠實標記 `used_fallback_competitive_landscape`，
      不假裝有扎實的競品事實可以引用。"""
    topic = state["topic"]
    company = state["company"]
    role_token = _event_role.set("problem_analysis")
    try:
        queries = [
            f"{topic} 有哪些 APP 或服務 案例",
            f"{topic} 知名 品牌 功能",
            f"{topic} 競品 比較",
        ]
        raw: List[dict] = []
        for q in queries:
            try:
                hits = web_search(q, max_results=4)
                usable = [hit for hit in hits if is_usable_search_result(hit)]
                raw.extend(usable)
                print(f"  [research_competitive_landscape] query={q!r} → {len(usable)}/{len(hits)} 筆可用")
            except Exception as exc:  # noqa: BLE001
                print(f"  [research_competitive_landscape] query={q!r} 失敗：{exc}")
        known_urls = {it.get("url") for it in raw if it.get("url")}
        sources_block = "\n".join(
            f"- {it.get('title')} | {it.get('url')}\n  {it.get('snippet', '')[:200]}" for it in raw
        ) or "（無搜尋結果）"

        system = (
            "你是市場研究員。根據下面提供的搜尋素材，找出跟這個策略方向"
            "直接相關、真實存在的競品/現有方案，列出具體公司或產品名稱、"
            "他們實際做了什麼功能。只能根據搜尋素材裡真實出現的內容回答，"
            "找不到具體名稱就不要輸出這筆——嚴禁自己編造競品名稱或網址。"
            "只輸出JSON：{\"competitors\":["
            "{\"competitor_name\":\"必須是搜尋素材裡真實出現過的公司/產品"
            "名稱\",\"feature_description\":\"<=80字，具體描述這家做了"
            "什麼功能，不能是空泛形容詞\",\"source_url\":\"必須是搜尋"
            "素材裡列出的其中一個網址，不能捏造\"}"
            "]}（最多 6 筆，找不到具體的就少列，不要硬湊）"
        )
        user = f"策略方向：{topic}\n\n公司定位：\n{company}\n\n搜尋素材：\n{sources_block}"
        raw_text = call_llm(SMART_MODEL, system, user, max_tokens=2000)
        data = extract_json_object(raw_text)
        if not data:
            data = extract_json_object(repair_json_text(raw_text))
        data = data or {}

        competitive_landscape: List[dict] = []
        for c in (data.get("competitors") or []):
            if not isinstance(c, dict):
                continue
            name = _safe_str(c.get("competitor_name"))
            url = _safe_str(c.get("source_url"))
            desc = _safe_str(c.get("feature_description"))
            if not name or not desc:
                continue
            if url not in known_urls:
                print(f"  [research_competitive_landscape] 丟棄未驗證競品：{name}（url 不在搜尋結果裡）")
                continue
            competitive_landscape.append({
                "competitor_name": name, "feature_description": desc, "source_url": url,
            })

        used_fallback = not competitive_landscape
        if used_fallback:
            competitive_landscape = [{
                "competitor_name": "（系統保底）",
                "feature_description": "本次未能取得具體競品資訊，以下評分僅供參考。",
                "source_url": "",
            }]

        print(
            f"  [research_competitive_landscape] {len(competitive_landscape)} 筆驗證通過的競品"
            f"（fallback={used_fallback}）"
        )
        emit_event(
            "research_competitive_landscape",
            f"找到 {len(competitive_landscape)} 筆真實競品資訊" if not used_fallback else "未能取得具體競品資訊",
            role="problem_analysis",
            extra={
                "competitive_landscape": competitive_landscape,
                "queries": queries,
                "n_results": len(raw),
                "used_fallback_competitive_landscape": used_fallback,
            },
        )
    finally:
        _event_role.reset(role_token)
    return {
        "competitive_landscape": competitive_landscape,
        "used_fallback_competitive_landscape": used_fallback,
        "research_queries": queries,
        "research_items": raw,
    }


# ---------------------------------------------------------------------------
# 虛擬問卷：購買意願/差異化感受的量化補充訊號
# ---------------------------------------------------------------------------

# 明確寫死的人口特徵分層（不是讓 LLM 自己判斷母體長什麼樣子）——目的是
# 讓這個本身方法論就比較脆弱的模組至少可重現、可稽核。8 組，年齡層 ×
# 性別 × 職業類別交叉。跟 stage14-signals 完全沿用同一份分層定義。
SURVEY_STRATA = [
    {"id": "s1", "label": "18-24歲／女性／學生"},
    {"id": "s2", "label": "18-24歲／男性／學生"},
    {"id": "s3", "label": "25-34歲／女性／上班族"},
    {"id": "s4", "label": "25-34歲／男性／上班族"},
    {"id": "s5", "label": "35-44歲／女性／自營業"},
    {"id": "s6", "label": "35-44歲／男性／自營業"},
    {"id": "s7", "label": "45歲以上／女性／退休或自由業"},
    {"id": "s8", "label": "45歲以上／男性／退休或自由業"},
]
DEFAULT_SURVEY_RESPONDENTS_PER_STRATUM = 8   # 預設總模擬樣本數 ≈ 64

# 這段警語必須原封不動跟著 survey_summary 一起傳遞到報告/回放/market_fit
# lens 的 prompt 裡——不管哪一層下游用到這份資料，都不能把它靜默丟掉。
SURVEY_METHOD_CAVEAT = (
    "以下是虛擬問卷產生的方向性訊號：由單一 LLM 內部分布模擬出的虛擬受訪者，"
    "不是真實人類母體的獨立抽樣，不具統計顯著性，只能當作質化概念測試訪談"
    "之外的『參考訊號』，不能單獨用來推翻質化證據，也不能被包裝成有統計"
    "意義的真實數據。"
)


class SurveyStratumTask(TypedDict):
    stratum: dict
    candidate_features: List[dict]
    n_respondents: int


class SurveyPanelState(TypedDict):
    candidate_features: List[dict]
    n_respondents: int
    survey_results: Annotated[List[dict], operator.add]


def fan_out_survey_strata(state: SurveyPanelState) -> List[Send]:
    return [
        Send("survey_one_stratum", {
            "stratum": stratum, "candidate_features": state["candidate_features"],
            "n_respondents": state["n_respondents"],
        })
        for stratum in SURVEY_STRATA
    ]


def survey_one_stratum(task: SurveyStratumTask) -> dict:
    """一次 LLM 呼叫模擬一個人口特徵分層裡 `n_respondents` 位虛擬受訪者，
    對每個候選功能給出彙整後的統計量（不是逐字稿）——不對每一位虛擬
    受訪者各自 fan-out，避免撞 rate limit、拉長壁鐘時間（stage12/note.md
    已經分析過這個障礙）。

    stage15-market-fit：測的指標從 stage14-signals 的困擾度/強度換成
    購買意願／差異化感受——這裡驗證的是「這個功能有沒有市場競爭力」，
    不是「這個情境困不困擾」。"""
    stratum = task["stratum"]
    candidate_features = task["candidate_features"]
    n_respondents = task["n_respondents"]
    role_token = _event_role.set(f"survey:{stratum['label']}")
    features_block = "\n".join(f"- [{f['id']}] {f.get('title', '')}：{f.get('summary', '')}" for f in candidate_features)
    system = (
        f"你要模擬「{stratum['label']}」這個人口特徵分層裡 {n_respondents} 位"
        "虛擬受訪者，針對下面每個功能提案，估計這個分層裡的人的購買意願"
        "比例、以及覺得這個功能明顯不同於現有方案的比例。這是模擬訊號，"
        "不是真實調查，但仍要誠實反映這個分層的合理差異，不要對每個功能"
        "都給同樣的數字。只輸出JSON：{\"features\": [{\"feature_id\":\"...\","
        "\"purchase_intent_pct\":0-100的數字（這個分層裡會想付費/切換使用"
        "這個功能的模擬比例）,\"differentiation_pct\":0-100的數字（這個"
        "分層裡覺得這個功能明顯不同於現有方案的模擬比例）,\"sample_quote\":"
        "\"<=40字，一則標明是模擬的代表性引述，或空字串\"}]}"
    )
    user = f"候選功能清單：\n{features_block}"
    try:
        raw = call_llm(SMART_MODEL, system, user, max_tokens=1500)
        data = extract_json_object(raw)
        if not data:
            data = extract_json_object(repair_json_text(raw))
        data = data or {}
    finally:
        _event_role.reset(role_token)

    features_by_id = {f.get("feature_id"): f for f in (data.get("features") or []) if isinstance(f, dict)}
    used_fallback = False
    feature_stats = []
    for cf in candidate_features:
        f = features_by_id.get(cf["id"])
        if f is None:
            used_fallback = True
            feature_stats.append({
                "feature_id": cf["id"], "feature_title": cf.get("title", ""),
                "purchase_intent_pct": 50.0, "differentiation_pct": 50.0,
                "sample_quote": "",
            })
            continue
        try:
            purchase_pct = max(0.0, min(100.0, float(f.get("purchase_intent_pct"))))
        except (TypeError, ValueError):
            purchase_pct = 50.0
            used_fallback = True
        try:
            diff_pct = max(0.0, min(100.0, float(f.get("differentiation_pct"))))
        except (TypeError, ValueError):
            diff_pct = 50.0
            used_fallback = True
        feature_stats.append({
            "feature_id": cf["id"], "feature_title": cf.get("title", ""),
            "purchase_intent_pct": purchase_pct, "differentiation_pct": diff_pct,
            "sample_quote": _safe_str(f.get("sample_quote")),
        })

    result = {
        "stratum_id": stratum["id"], "stratum_label": stratum["label"],
        "n_simulated": n_respondents, "used_fallback": used_fallback,
        "feature_stats": feature_stats,
    }
    emit_event(
        "survey_one_stratum", f"分層「{stratum['label']}」虛擬問卷完成（模擬 {n_respondents} 人）",
        role=f"survey:{stratum['label']}", extra=result,
    )
    print(f"  [survey_one_stratum] {stratum['label']}（fallback={used_fallback}）")
    return {"survey_results": [result]}


def build_survey_panel_subgraph():
    g = StateGraph(SurveyPanelState)
    g.add_node("survey_one_stratum", instrument("survey_one_stratum", survey_one_stratum))
    g.add_conditional_edges(START, fan_out_survey_strata, ["survey_one_stratum"])
    g.add_edge("survey_one_stratum", END)
    return g.compile()


survey_panel_graph = build_survey_panel_subgraph()


def _aggregate_survey_results(survey_results: List[dict], candidate_features: List[dict]) -> dict:
    """依 n_simulated 加權平均每個候選功能的統計量，回傳的 dict 一律內含
    SURVEY_METHOD_CAVEAT 原文，保證不會在往後傳遞時被靜默丟掉。"""
    by_feature: dict = {}
    for res in survey_results:
        n = res["n_simulated"]
        for fs in res["feature_stats"]:
            acc = by_feature.setdefault(fs["feature_id"], {
                "weighted_purchase": 0.0, "weighted_diff": 0.0, "total_n": 0, "quotes": [],
            })
            acc["weighted_purchase"] += fs["purchase_intent_pct"] * n
            acc["weighted_diff"] += fs["differentiation_pct"] * n
            acc["total_n"] += n
            if fs.get("sample_quote") and len(acc["quotes"]) < 2:
                acc["quotes"].append(f"[{res['stratum_label']}] {fs['sample_quote']}")

    title_by_id = {cf["id"]: cf.get("title", "") for cf in candidate_features}
    by_feature_summary = {}
    for feature_id, acc in by_feature.items():
        total_n = acc["total_n"] or 1
        by_feature_summary[feature_id] = {
            "feature_title": title_by_id.get(feature_id, ""),
            "purchase_intent_pct": round(acc["weighted_purchase"] / total_n, 1),
            "differentiation_pct": round(acc["weighted_diff"] / total_n, 1),
            "sample_quotes": acc["quotes"],
        }
    return {
        "caveat": SURVEY_METHOD_CAVEAT,
        "total_simulated_n": sum(res["n_simulated"] for res in survey_results),
        "by_feature": by_feature_summary,
    }


# ---------------------------------------------------------------------------
# 概念測試訪談：簡化版（1-2 輪固定問題，不是 5 輪 JTBD switch）
# ---------------------------------------------------------------------------

class ConceptTestTask(TypedDict):
    feature: dict
    interviewee: dict


class ConceptTestPanelState(TypedDict):
    candidate_features: List[dict]
    interviewees: List[dict]
    concept_test_results: Annotated[List[dict], operator.add]


def fan_out_concept_tests(state: ConceptTestPanelState) -> List[Send]:
    # 笛卡兒積：每個候選功能 × 每位訪談對象，用既有 fan_out_* 寫法直接
    # 組出 task list，不需要新的 fan-out 模式。
    return [
        Send("concept_test_one_person", {"feature": feature, "interviewee": interviewee})
        for feature in state["candidate_features"]
        for interviewee in state["interviewees"]
    ]


def concept_test_one_person(task: ConceptTestTask) -> dict:
    """簡化版概念測試訪談：固定問 2 輪（不是 5 輪 JTBD switch），問「看到
    這個功能的第一反應」跟「會不會付費/切換」，符合「快速驗證」的需求，
    刻意犧牲深度換取速度——這裡的訪談對象是同一組固定小組，對每個候選
    功能都問一輪，不像 stage14-signals 那樣為每個候選 job 動態生成專屬
    訪談對象（決策見 note.md，不影響拓樸設計）。跟虛擬問卷一樣是模擬
    訊號，不是真實統計。"""
    feature = task["feature"]
    interviewee = task["interviewee"]
    role_token = _event_role.set(f"concept_test:{interviewee.get('name')}")
    transcript: List[dict] = []
    try:
        first_q = f"看到「{feature.get('title', '')}」這個功能：{feature.get('summary', '')}，你的第一反應是什麼？"
        a1 = simulate_user_answer(interviewee, first_q, [])
        transcript.append({"round": 1, "question": first_q, "answer": a1})
        second_q = "如果要付費或要從你現在用的方案切換過來才能用這個功能，你會不會考慮？為什麼？"
        a2 = simulate_user_answer(interviewee, second_q, transcript)
        transcript.append({"round": 2, "question": second_q, "answer": a2})

        classify_system = (
            "根據下面這段對話，判斷這位使用者是否表達出願意付費/切換使用"
            "這個功能的意願。只輸出 JSON：{\"would_pay\": true/false,"
            "\"reaction_summary\":\"<=50字，濃縮這位使用者的整體反應\"}"
        )
        classify_user = "\n".join(f"Q:{t['question']}\nA:{t['answer']}" for t in transcript)
        raw = call_llm(CHEAP_MODEL, classify_system, classify_user, max_tokens=200)
        data = extract_json_object(raw) or {}
        would_pay = bool(data.get("would_pay", False))
        reaction_summary = _safe_str(data.get("reaction_summary")) or a1[:50]

        result = {
            "feature_id": feature.get("id"), "feature_title": feature.get("title", ""),
            "interviewee_name": interviewee.get("name"),
            # 完整內嵌訪談對象的人物設定（不只是名字）——概念測試訪談對象
            # 是同一組固定小組，不像 stage14-signals 那樣有一份全域的
            # candidate_jobs.interview_pool 可以事後查表，內嵌在這裡讓
            # 事件本身、回放頁都能直接顯示「誰在什麼情境下這樣說」，不用
            # 額外維護一份訪談對象名單。
            "interviewee": interviewee,
            "transcript": transcript, "would_pay": would_pay, "reaction_summary": reaction_summary,
        }
        emit_event(
            "concept_test_turn", f"{interviewee.get('name')} 對《{feature.get('title', '')}》的反應",
            role=f"concept_test:{interviewee.get('name')}", extra=result,
        )
        print(f"  [concept_test_one_person] {interviewee.get('name')} × {feature.get('title')}：would_pay={would_pay}")
    finally:
        _event_role.reset(role_token)
    return {"concept_test_results": [result]}


def build_concept_test_panel_subgraph():
    g = StateGraph(ConceptTestPanelState)
    g.add_node("concept_test_one_person", instrument("concept_test_one_person", concept_test_one_person))
    g.add_conditional_edges(START, fan_out_concept_tests, ["concept_test_one_person"])
    g.add_edge("concept_test_one_person", END)
    return g.compile()


concept_test_panel_graph = build_concept_test_panel_subgraph()


def _aggregate_concept_test_results(concept_test_results: List[dict], candidate_features: List[dict]) -> dict:
    """依 feature 分組彙整 would_pay 比例跟代表性反應摘要，供
    market_fit lens 的 prompt 引用。"""
    by_feature: dict = {}
    for res in concept_test_results:
        acc = by_feature.setdefault(res["feature_id"], {"n": 0, "would_pay_count": 0, "reactions": []})
        acc["n"] += 1
        if res["would_pay"]:
            acc["would_pay_count"] += 1
        if len(acc["reactions"]) < 2:
            acc["reactions"].append(f"[{res['interviewee_name']}] {res['reaction_summary']}")

    title_by_id = {cf["id"]: cf.get("title", "") for cf in candidate_features}
    by_feature_summary = {}
    for feature_id, acc in by_feature.items():
        n = acc["n"] or 1
        by_feature_summary[feature_id] = {
            "feature_title": title_by_id.get(feature_id, ""),
            "would_pay_pct": round(100 * acc["would_pay_count"] / n, 1),
            "n_interviewed": acc["n"],
            "sample_reactions": acc["reactions"],
        }
    return {"by_feature": by_feature_summary}


def validate_market_fit(state: MeetingState) -> dict:
    """收斂前的快速市場驗證：模式 B（單一節點依序同步呼叫三個子圖），
    接在 `ask_question`/`answer_question` HITL 迴圈之後、`pick_winner`
    之前，全程單一前驅鏈，不是三條分支各自指到 `pick_winner`（那會複製
    stage12 踩過的 join bug——見 build_parent_graph() 的說明）。

    依序呼叫：(1) 虛擬問卷（購買意願/差異化，`survey_panel_graph`）；
    (2) 概念測試訪談（簡化版，`concept_test_panel_graph`）；(3) DFV
    四面向評分（`dfv_panel_graph`，第 4 個「市場競爭力」lens 的 prompt
    讀前兩步彙整的結果當佐證）——依序呼叫是刻意的，DFV 的 market_fit
    lens 需要前兩步的彙整結果才能引用，不是三者互相獨立、順序隨意。"""
    candidate_features = state["ideas"]
    n_respondents = state.get("survey_respondents_per_stratum") or DEFAULT_SURVEY_RESPONDENTS_PER_STRATUM
    survey_result = survey_panel_graph.invoke({
        "candidate_features": candidate_features,
        "n_respondents": n_respondents,
        "survey_results": [],
    })
    survey_results = survey_result["survey_results"]
    survey_summary = _aggregate_survey_results(survey_results, candidate_features)
    print(f"  [validate_market_fit] 虛擬問卷：{len(survey_results)} 個分層，總模擬樣本數 {survey_summary['total_simulated_n']}")

    n_interviewees = state.get("n_concept_test_interviewees") or N_CONCEPT_TEST_INTERVIEWEES
    interviewees = load_users()[:n_interviewees]
    concept_test_result = concept_test_panel_graph.invoke({
        "candidate_features": candidate_features,
        "interviewees": interviewees,
        "concept_test_results": [],
    })
    concept_test_results = concept_test_result["concept_test_results"]
    concept_test_summary = _aggregate_concept_test_results(concept_test_results, candidate_features)
    print(f"  [validate_market_fit] 概念測試：{len(concept_test_results)} 筆訪談反應")

    dfv_result = dfv_panel_graph.invoke({
        "ideas": candidate_features,
        "strategic_directive": state["topic"],
        "competitive_landscape": state["competitive_landscape"],
        "survey_summary": survey_summary,
        "concept_test_summary": concept_test_summary,
        "dfv_scores": [],
    })
    dfv_scores = dfv_result["dfv_scores"]
    print(f"  [validate_market_fit] DFV 評分：{len(dfv_scores)} 筆（{len(DFV_LENSES)} 面向 × {len(candidate_features)} 個功能）")

    emit_event(
        "validate_market_fit",
        f"市場驗證完成：虛擬問卷 {len(survey_results)} 分層、概念測試 {len(concept_test_results)} 筆訪談、"
        f"DFV {len(dfv_scores)} 筆評分",
        extra={"survey_summary": survey_summary, "concept_test_summary": concept_test_summary},
    )
    return {
        "survey_results": survey_results, "survey_summary": survey_summary,
        "concept_test_results": concept_test_results, "concept_test_summary": concept_test_summary,
        "dfv_scores": dfv_scores,
    }


# ---------------------------------------------------------------------------
# Develop 起點：從公司實際能力衍生出彼此不同的參與者職能
# ---------------------------------------------------------------------------

# 真實跑測發現的坑：跨領域抽樣（建築、餐飲內場、農業供應鏈…）確實拉開了
# idea 多樣性，但這些領域跟公司本身完全無關，發想出來的方案常常需要公司
# 根本不具備的能力才做得到（例如叫一位農業供應鏈專家幫公司 APP 提案，
# 想出來的東西可能沒人做得出來）。改成先讀 company.md，讓 LLM 從「這家
# 公司實際擁有的部門/技術/素材/通路/既有業務關係」裡衍生出彼此明顯不同的
# 職能——多樣性的來源從「跟公司無關的任意領域」換成「公司內部真正存在、
# 但彼此少有交集的能力」。解析失敗或衍生數量不足時，才退回這份跟公司無關
# 的保底領域池（比空手或崩潰好，但明知不是最佳選擇）。
DOMAIN_ARCHETYPE_POOL = [
    "建築與都市規劃", "餐飲內場管理", "製造業產線管理", "高等教育行政",
    "保險理賠", "農業供應鏈", "職業運動訓練", "法律實務",
    "社工／社福第一線", "遊戲設計", "零售門市營運", "硬體工程",
]


def derive_company_domains(company: str, strategic_directive: str, competitive_summary: str) -> tuple[List[str], bool]:
    """回傳 (domains, used_fallback)。讓每位腦力激盪參與者的職能都扣著
    「這家公司真的有的能力」，而不是跟公司八竿子打不著的任意領域——這樣
    發想出來的功能提案才有機會用公司現有的人/技術/資源真的做出來，不是
    空談。仍然要求彼此明顯不同（同一份 prompt 裡明講：不要全部落在行銷/
    內容這種表面領域），避免又退回「LLM 自己判斷互補團隊」時常見的同質
    化問題。

    stage15-market-fit：簽章從 (company, problem_statement, hmw) 改成
    (company, strategic_directive, competitive_summary)——沒有 Discover/
    Define 收斂出的問題陳述了，這裡直接讀公司給定的策略方向跟競品掃描
    結果，函式本身邏輯不用改。"""
    system = (
        "你是組織設計顧問。仔細讀這家公司的定位/資源/能力描述（部門、"
        "技術、內容素材、通路、既有業務關係等），列出這家公司內部或能"
        f"直接調度的資源裡，恰好 {N_PERSONAS} 個彼此明顯不同的職能／專業"
        "角度——目的是組一支跨職能腦力激盪小組，讓每位成員都從公司真正"
        "具備的某種能力出發思考這個策略方向下能做什麼具體功能，而不是"
        "天馬行空地假設公司做得到某件事。嚴禁全部落在同一個大領域的不同"
        "分工（例如都是「社群行銷」「內容企劃」「品牌公關」這種同一種"
        "能力的變體）——要橫跨這家公司真正擁有的不同種類能力（例如內容"
        "製作技術、工程/資料、通路/業務關係、既有異業合作、會員營運等，"
        "實際個數與內容依這家公司的真實描述決定，不要套用這個例子）。"
        "只輸出 JSON：{\"domains\":[\"...\"]}，字串要具體到看得出是這家"
        f"公司的哪個實際能力，不要用空泛通稱，恰好 {N_PERSONAS} 個"
    )
    user = (
        f"公司定位／資源／能力：\n{company}\n\n策略方向：{strategic_directive}"
        f"\n\n市場現況（真實競品掃描）：\n{competitive_summary}"
    )
    raw = call_llm(SMART_MODEL, system, user, max_tokens=800)
    data = extract_json_object(raw)
    if not data:
        data = extract_json_object(repair_json_text(raw))
    data = data or {}
    raw_domains = [d for d in (data.get("domains") or []) if _safe_str(d)]
    domains = list(dict.fromkeys(raw_domains))[:N_PERSONAS]  # 去重（保留原順序），避免 LLM 重複列同一個職能

    used_fallback = False
    if len(domains) < 2:
        # 解析失敗或衍生數量明顯不足——退回跟公司無關的保底領域池，不讓
        # 整場會議因為這步失敗而跑不下去（但明知這不是最佳選擇，只是保底）。
        domains = random.sample(DOMAIN_ARCHETYPE_POOL, k=min(N_PERSONAS, len(DOMAIN_ARCHETYPE_POOL)))
        used_fallback = True

    emit_event(
        "derive_company_domains",
        f"從公司能力衍生出 {len(domains)} 個職能：{domains}",
        extra={"domains": domains, "used_fallback_domains": used_fallback},
    )
    print(f"  [derive_company_domains] {domains}（fallback={used_fallback}）")
    return domains, used_fallback


class PersonaDomainTask(TypedDict):
    domain: str
    strategic_directive: str
    competitive_summary: str
    idx: int


class PersonaTeamPanelState(TypedDict):
    domains: List[str]
    strategic_directive: str
    competitive_summary: str
    personas: Annotated[List[dict], operator.add]


def fan_out_persona_domains(state: PersonaTeamPanelState) -> List[Send]:
    return [
        Send("generate_one_persona_for_domain", {
            "domain": domain,
            "strategic_directive": state["strategic_directive"],
            "competitive_summary": state["competitive_summary"],
            "idx": idx,
        })
        for idx, domain in enumerate(state["domains"], 1)
    ]


def generate_one_persona_for_domain(task: PersonaDomainTask) -> dict:
    """每位參與者各自獨立一次 LLM 呼叫生成（不是像 stage12 那樣一次呼叫
    生成全部 N 位）——獨立呼叫才不會讓角色彼此的措辭/選擇互相關聯。domain
    是 `derive_company_domains()` 從公司實際能力衍生出來的職能（不是跟
    公司無關的任意領域），這裡的參與者是公司內部/可調度資源裡真的具備
    這個職能的人，這樣他發想的功能提案才有機會用公司現有的東西做出來。"""
    system = (
        f"你是組織設計顧問。針對這個策略方向，設計一位背景是"
        f"「{task['domain']}」的腦力激盪參與者——這是這家公司內部或可"
        "直接調度的一種實際職能，這位參與者要真的具備這個職能的專業，"
        "他會用自己職能的思維方式去理解這個策略方向、並提出這家公司真的"
        "做得到的功能，不是天馬行空假設公司有他其實沒有的能力。只輸出 "
        "JSON：{\"name\":\"...\",\"role\":\"<=20字\",\"background\":\"<=60字，"
        "明確扣住指定的職能\",\"focus\":[\"...\",\"...\"],\"style\":\"<=20字\"}"
    )
    user = (
        f"專業領域：{task['domain']}\n\n策略方向：{task['strategic_directive']}"
        f"\n\n市場現況（真實競品掃描）：\n{task['competitive_summary']}"
    )
    raw = call_llm(SMART_MODEL, system, user, max_tokens=600)
    data = extract_json_object(raw)
    if not data:
        data = extract_json_object(repair_json_text(raw))
    data = data or {}
    persona = {
        "id": f"p{task['idx']}",
        "name": _safe_str(data.get("name")) or f"參與者{task['idx']}",
        "role": _safe_str(data.get("role")) or task["domain"],
        "background": _safe_str(data.get("background")) or f"專業背景：{task['domain']}",
        "focus": [f for f in (data.get("focus") or []) if _safe_str(f)] or [task["domain"]],
        "style": _safe_str(data.get("style")) or "直接",
        "domain": task["domain"],
    }
    emit_event(
        "generate_one_persona_for_domain",
        f"生成參與者 {persona['name']}（領域：{task['domain']}）",
        extra={"persona": persona},
    )
    print(f"  [generate_one_persona_for_domain] {task['domain']} → {persona['name']}")
    return {"personas": [persona]}


def build_persona_team_panel_subgraph():
    g = StateGraph(PersonaTeamPanelState)
    g.add_node(
        "generate_one_persona_for_domain",
        instrument("generate_one_persona_for_domain", generate_one_persona_for_domain),
    )
    g.add_conditional_edges(START, fan_out_persona_domains, ["generate_one_persona_for_domain"])
    g.add_edge("generate_one_persona_for_domain", END)
    return g.compile()


persona_team_panel_graph = build_persona_team_panel_subgraph()


def _competitive_summary(competitive_landscape: List[dict]) -> str:
    """把 research_competitive_landscape() 驗證過的競品清單整理成一段
    給下游 prompt 讀的文字摘要——集中在一處，避免 assemble_persona_team／
    draft_one_feature／score_one_dimension（market_fit lens）各自重複
    寫一次同樣的格式化邏輯。"""
    lines = [
        f"- {c['competitor_name']}：{c['feature_description']}"
        for c in competitive_landscape if c.get("competitor_name") and c.get("competitor_name") != "（系統保底）"
    ]
    return "\n".join(lines) or "（本次未能取得具體競品資訊）"


def assemble_persona_team(state: MeetingState) -> dict:
    """Develop 起點。策略方向由公司給定，不需要 Discover/Define 驗證，
    這裡直接接在 `research_competitive_landscape` 後面，是嚴格單一前驅鏈
    （不是像 stage12 `system_research`／`generate_personas` 那樣兩個互不
    相依卻要同時完成的平行分支），這個接縫本身就不是 join，不需要「折成
    一個節點依序呼叫」的技巧。

    domain 的來源是 `derive_company_domains()`（從 `company.md` 衍生出
    這家公司實際具備的職能），不是跟公司無關的 `DOMAIN_ARCHETYPE_POOL`
    ——使用者在 stage14-signals 真實驗證後指出跨領域抽樣雖然拉開了多樣性，
    但那些領域跟公司無關，發想出來的方案很可能公司根本沒能力做出來。"""
    competitive_summary = _competitive_summary(state["competitive_landscape"])
    domains, used_fallback_domains = derive_company_domains(
        state["company"], state["topic"], competitive_summary,
    )
    result = persona_team_panel_graph.invoke({
        "domains": domains,
        "strategic_directive": state["topic"],
        "competitive_summary": competitive_summary,
        "personas": [],
    })
    personas = result["personas"]

    used_fallback = used_fallback_domains
    if len(personas) < 2:
        # 解析失敗或生成的人數明顯不足——退回既有的 load_personas() 保底，
        # 不讓整場會議因為這步失敗而跑不下去。
        personas = load_personas()[:N_PERSONAS]
        used_fallback = True

    print(f"  [assemble_persona_team] {[p.get('name') for p in personas]}（fallback={used_fallback}）")
    emit_event(
        "assemble_persona_team", f"組成扣著公司能力的腦力激盪團隊：{[p.get('name') for p in personas]}",
        extra={"personas": personas, "used_fallback_personas": used_fallback},
    )
    return {"personas": personas, "used_fallback_personas": used_fallback}


# ---------------------------------------------------------------------------
# 平行發散：N 位 persona 各自獨立發想一個 idea
# ---------------------------------------------------------------------------

class FeatureTask(TypedDict):
    persona: dict
    strategic_directive: str
    competitive_landscape: List[dict]
    research_items: List[dict]


def fan_out_ideas(state: MeetingState) -> List[Send]:
    return [
        Send("draft_one_feature", {
            "persona": persona,
            "strategic_directive": state["topic"],
            "competitive_landscape": state["competitive_landscape"],
            "research_items": state["research_items"],
        })
        for persona in state["personas"]
    ]


_FEATURE_SCHEMA_HINT = f"""
只輸出一個 JSON 物件（不要 markdown 圍欄），欄位：
- title: string（<=40字）
- summary: string（2-3句，<=120字）
- rationale: string（<=80字，說明這個功能為什麼在這個策略方向下值得做）
- target_segment: string（<=40字，這個功能主要鎖定的目標客群，盡量跟
  其他人不同，不要每個人都寫「一般使用者」這種空泛答案）
- monetization_mechanism: string（<=40字，具體的貨幣化機制，例如訂閱
  分級/單次買斷/廣告分潤/合作抽成，盡量跟其他人不同）
- differentiation_vs_competitors: string（<=100字，必須具體點名下面
  「市場現況」列出的其中一個真實競品，講清楚差異化在哪裡；如果市場
  現況顯示沒有取得具體競品資訊，誠實說明沒有具體對比對象，不要編造
  競品名稱）
- sources: [{{"title","url","how_used"}}] 最多 2 筆 — url 必須來自提供的搜尋素材
- bmc: 物件，這是「你自己」設計的 Business Model Canvas（不是共用範本，
  別人可能畫出完全不同的版本），鍵必須恰好包含且僅包含這九個：
  {json.dumps(BMC_KEYS, ensure_ascii=False)}，其中「收益流」「成本結構」
  兩格必須是物件：{{"narrative":"<=40字","monthly_estimate_twd":數字（新台幣/月，
  粗略估算即可）,"basis":"<=50字估算依據"}}，其餘七格維持一句話文字
務必輸出精簡合法 JSON，避免長文導致截斷。
"""


def _parse_idea(text: str) -> dict:
    try:
        data = extract_json(text)
    except (json.JSONDecodeError, ValueError):
        try:
            data = extract_json(repair_json_text(text))
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"idea JSON 解析失敗：{exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("idea 不是 JSON object")
    data.setdefault("sources", [])
    return data


def draft_one_feature(task: FeatureTask) -> dict:
    """使用者要求刪掉互評/自己改——每位 persona 獨立發想一個功能提案就
    結束，不會再被自己或別人修改。

    stage15-market-fit：取代 stage14-signals 的 `draft_one_idea()`——沒有
    Discover/Define 收斂出的 `hmw`/`problem_statement`/`insights` 可以
    呼應了（策略方向由公司給定，不是訪談驗證出來的），改成直接呼應
    `strategic_directive`，並要求明確對比 `competitive_landscape` 裡的
    真實競品講差異化——這是這個 stage 存在的核心目的，不是選配。
    `target_segment`/`monetization_mechanism` 要求盡量彼此不同，延續
    `derive_company_domains()` 強制跨職能抽樣的同一個精神，用既有的
    `pairwise_diversity()` 驗收多樣性，不新增測量機制。

    BMC 仍然是每位 persona 自己設計的一份，不是共用範本（真實跑測發現：
    全場共用一份 BMC 會把商業模式先框死，所有人只能在同一個框架裡想
    變化，導致 idea 多樣性量出來比「同一個 idea 換句話說」還低，見
    stage14-signals/note.md）——這裡跟功能提案本身、同一次 LLM 呼叫
    產生，不多花一次呼叫的成本，用 `_merge_bmc()` 保證結構合法。"""
    persona = task["persona"]
    role_token = _event_role.set(_persona_label(persona))
    try:
        competitive_block = _competitive_summary(task["competitive_landscape"])
        sources_block = "\n".join(
            f"- {it.get('title')} | {it.get('url')}" for it in task["research_items"][:8]
        ) or "（無）"
        system = (
            f"你是 {persona['name']}（{persona.get('role', '')}），背景：{persona.get('background', '')}。"
            f"關注：{', '.join(persona.get('focus') or [])}。發言風格：{persona.get('style', '')}。"
            "獨立發想一個能在這個策略方向下、具備市場競爭力的具體功能，"
            "並自己設計一份支撐這個功能的 Business Model Canvas——這是你"
            "自己的提案跟你自己的商業模式判斷，不用參考其他人的想法（現在"
            "還沒有其他人的提案，也沒有範本 BMC 可以照抄）。"
            "differentiation_vs_competitors 必須具體點名下面市場現況裡的"
            "真實競品，不能空泛喊「更好用」；url 只能來自提供的搜尋素材，"
            "不能捏造連結。"
            + _FEATURE_SCHEMA_HINT
        )
        user = (
            f"策略方向：{task['strategic_directive']}\n\n"
            f"市場現況（真實競品掃描）：\n{competitive_block}\n\n"
            f"可用搜尋素材：\n{sources_block}"
        )
        raw = call_llm(SMART_MODEL, system, user, max_tokens=1600)
        idea = _parse_idea(raw)
        idea["id"] = persona.get("id")
        idea["persona_id"] = persona.get("id")
        idea["persona_name"] = persona.get("name")
        idea["bmc"] = _merge_bmc(idea.get("bmc"), {})
        emit_event(
            "draft_idea", f"{persona['name']} 提出《{idea.get('title', '')}》",
            role=_persona_label(persona), extra={"idea": idea},
        )
        print(f"  [draft_idea:{persona['name']}] {idea.get('title')}")
    finally:
        _event_role.reset(role_token)
    return {"ideas": [idea]}


def ask_question(state: MeetingState) -> dict:
    """使用者要求保留的唯一互動環節（第 3 點）——不靠 facilitator 的
    `Command.goto` 動態導向，改成靜態邊直接進來（`draft_ideas` fan-out
    完，經 `team_and_research_ready`-style 的隱含 join 後）。一次看到
    全部 N 個 idea，可以指定任何一個 idea 問一題，答完會繞回這裡（可
    連續問），輸入空字串／skip 才真的往下走進 DFV 評分。"""
    ideas = state["ideas"]
    payload = {
        "ideas": [
            {"id": i.get("id"), "persona_name": i.get("persona_name"),
             "title": i.get("title"), "summary": i.get("summary"), "rationale": i.get("rationale")}
            for i in ideas
        ],
        "questions_asked_so_far": len(state["human_qa_log"]),
        "prompt": (
            f"{len(ideas)} 個 idea 都發想完了。要問哪一個一個問題嗎？"
            "可連續問多題，輸入空字串／skip 結束提問進入評分。"
        ),
    }
    answer_signal = interrupt(payload)
    if isinstance(answer_signal, dict) and answer_signal.get("action") == "ask":
        question = _safe_str(answer_signal.get("question"))
        target_idea_id = _safe_str(answer_signal.get("target_idea_id"))
        if question and any(i.get("id") == target_idea_id for i in ideas):
            asked_by = _safe_str(answer_signal.get("asked_by")) or "匿名"
            return {
                "pending_question": question,
                "pending_question_target_idea_id": target_idea_id,
                "pending_question_asked_by": asked_by,
            }
    return {"pending_question": None, "pending_question_target_idea_id": None, "pending_question_asked_by": None}


def route_after_question(state: MeetingState) -> Literal["answer_question", "validate_market_fit"]:
    return "answer_question" if state.get("pending_question") else "validate_market_fit"


def answer_question(state: MeetingState) -> dict:
    """回答問題純粹是給 demo 現場的人問得出問題、看得到回答——不修改
    idea 內容（第 8 點已經刪掉「回饋會改提案」的機制，這裡維持這個
    精神）。"""
    target_id = state["pending_question_target_idea_id"]
    idea = next(i for i in state["ideas"] if i.get("id") == target_id)
    question = state["pending_question"]
    presenter_name = idea.get("persona_name")
    role_token = _event_role.set(f"persona:{presenter_name}")
    system = (
        f"你是 {presenter_name}，剛發想完一個 idea，現在有人類當場提問。"
        "請根據你的 idea 內容誠實回答，2-4 句話，不知道的部分就承認不"
        "知道，不要瞎掰。"
    )
    user = (
        f"你的 idea 標題：{idea.get('title')}\n摘要：{idea.get('summary')}\n"
        f"理由：{idea.get('rationale')}\n\n人類提問：{question}"
    )
    try:
        answer = call_llm(SMART_MODEL, system, user, max_tokens=800).strip()
    finally:
        _event_role.reset(role_token)
    qa_entry = {
        "target_idea_id": target_id,
        "presenter_name": presenter_name,
        "question": question,
        "answer": answer,
        "asked_by": state.get("pending_question_asked_by") or "匿名",
    }
    emit_event(
        "human_qa", f"人類問 {presenter_name}：{question}",
        role=f"persona:{presenter_name}", extra=qa_entry,
    )
    print(f"  [human_qa] Q: {question}\n             A: {answer}")
    return {
        "human_qa_log": [qa_entry],
        "pending_question": None,
        "pending_question_target_idea_id": None,
        "pending_question_asked_by": None,
    }


# ---------------------------------------------------------------------------
# DFV（Desirability/Feasibility/Viability/Market Fit）結構化評分收斂
# ---------------------------------------------------------------------------

class DfvTask(TypedDict):
    lens: dict
    idea: dict
    strategic_directive: str
    competitive_landscape: List[dict]
    survey_summary: dict
    concept_test_summary: dict


class DfvPanelState(TypedDict):
    ideas: List[dict]
    strategic_directive: str
    competitive_landscape: List[dict]
    survey_summary: dict
    concept_test_summary: dict
    dfv_scores: Annotated[List[dict], operator.add]


def fan_out_dfv(state: DfvPanelState) -> List[Send]:
    """4 個面向 × N 個功能提案的雙迴圈，跟 stage9 `fan_out_three_lens`
    是同一個形狀，只是這裡每次呼叫只問一個面向、給一個分數，不是
    正面/負面/洞見各 3 則。stage15-market-fit 新增第 4 個「市場競爭力」
    lens，所以每個 task 額外帶 competitive_landscape／survey_summary／
    concept_test_summary，供 `score_one_dimension()` 判斷是不是
    market_fit lens 再決定要不要把這些當佐證塞進 prompt。"""
    return [
        Send("score_one_dimension", {
            "lens": lens, "idea": idea,
            "strategic_directive": state["strategic_directive"],
            "competitive_landscape": state["competitive_landscape"],
            "survey_summary": state["survey_summary"],
            "concept_test_summary": state["concept_test_summary"],
        })
        for lens in DFV_LENSES
        for idea in state["ideas"]
    ]


def score_one_dimension(task: DfvTask) -> dict:
    lens = task["lens"]
    idea = task["idea"]
    role_token = _event_role.set(f"dfv:{lens['name']}")
    evidence_block = ""
    market_fit_instruction = ""
    if lens["id"] == "market_fit":
        # 市場競爭力這個 lens 額外讀競品掃描/虛擬問卷/概念測試彙整結果
        # 當佐證——質化+量化訊號給評審參考，不越俎代庖幫評審下結論
        # （跟 stage14-signals select_job_and_define_problem 讀
        # survey_summary 的既有精神一致）。
        competitive_block = _competitive_summary(task["competitive_landscape"])
        survey_stats = (task["survey_summary"].get("by_feature") or {}).get(idea.get("id"))
        concept_stats = (task["concept_test_summary"].get("by_feature") or {}).get(idea.get("id"))
        survey_line = (
            f"模擬購買意願 {survey_stats['purchase_intent_pct']}%，"
            f"模擬差異化感受 {survey_stats['differentiation_pct']}%"
            if survey_stats else "（無虛擬問卷資料）"
        )
        concept_line = (
            f"{concept_stats['n_interviewed']} 位模擬訪談中 {concept_stats['would_pay_pct']}% "
            f"表示願意付費/切換；代表反應：{'；'.join(concept_stats['sample_reactions'])}"
            if concept_stats else "（無概念測試資料）"
        )
        evidence_block = (
            f"\n\n市場現況（真實競品掃描）：\n{competitive_block}\n\n"
            f"虛擬問卷（模擬訊號，非真人樣本）：{survey_line}\n\n"
            f"概念測試訪談（模擬訊號，非真人樣本）：{concept_line}\n\n"
            f"{SURVEY_METHOD_CAVEAT}"
        )
        market_fit_instruction = (
            "上面提供的市場現況/虛擬問卷/概念測試資料可以當作佐證，"
            "但虛擬問卷跟概念測試是模擬訊號，不具統計顯著性，不能單獨"
            "用來下結論，也不能講成有統計意義的真實數據。"
        )
    system = (
        f"你是{lens['name']}，只用「{lens['angle']}」這個角度評估這個功能"
        f"提案，不要評論其他面向。{market_fit_instruction}只輸出 JSON："
        "{\"score\": 0-10 的數字（可以有小數），\"critique\": \"具體、"
        "有憑有據的一大段批評，講清楚為什麼給這個分數，不要空泛通則，"
        "<=200字\"}"
    )
    user = (
        f"策略方向：{task['strategic_directive']}\n\n"
        f"功能提案：《{idea.get('title')}》{idea.get('summary')}\n理由：{idea.get('rationale')}\n"
        f"差異化說法：{idea.get('differentiation_vs_competitors', '')}"
        + evidence_block
    )
    try:
        raw = call_llm(SMART_MODEL, system, user, max_tokens=800)
        data = extract_json_object(raw)
        if not data:
            data = extract_json_object(repair_json_text(raw))
        data = data or {}
    finally:
        _event_role.reset(role_token)
    try:
        score = max(0.0, min(10.0, float(data.get("score"))))
    except (TypeError, ValueError):
        score = 5.0
    critique = _safe_str(data.get("critique")) or "（系統保底）提供的資訊不足以評論。"
    result = {
        "idea_id": idea.get("id"), "persona_name": idea.get("persona_name"),
        "lens_id": lens["id"], "lens_name": lens["name"], "dimension": lens["dimension"],
        "score": score, "critique": critique,
    }
    emit_event(
        "dfv_score", f"{lens['name']} 對《{idea.get('title')}》：{score} 分",
        role=f"dfv:{lens['name']}", extra=result,
    )
    print(f"  [dfv:{lens['name']}] {idea.get('persona_name')} 的 idea → {score} 分")
    return {"dfv_scores": [result]}


def build_dfv_panel_subgraph():
    g = StateGraph(DfvPanelState)
    g.add_node("score_one_dimension", instrument("score_one_dimension", score_one_dimension))
    g.add_conditional_edges(START, fan_out_dfv, ["score_one_dimension"])
    g.add_edge("score_one_dimension", END)
    return g.compile()


dfv_panel_graph = build_dfv_panel_subgraph()


def pick_winner(state: MeetingState) -> dict:
    """使用者要求收斂機制改成「三面向分數加總選最高分」（第 10 點）——
    不是共創合併，是選拔；純 Python 計算，零額外 LLM 成本。順便算一下
    N 個獨立發想的 idea 彼此的多樣性，作為「真的是平行發散、不是換句
    話說」的量化佐證（跟 stage9 的 diversity 敘事同一個精神，但這裡只有
    一次測量，沒有『收斂前後』可比較）。"""
    totals: dict = {}
    for s in state["dfv_scores"]:
        totals[s["idea_id"]] = totals.get(s["idea_id"], 0.0) + s["score"]
    winner_id = max(totals, key=totals.get)
    winner_idea = next(i for i in state["ideas"] if i.get("id") == winner_id)
    winner_idea = dict(winner_idea, total_score=totals[winner_id])
    diversity = pairwise_diversity(state["ideas"])
    name_by_id = {i.get("id"): i.get("persona_name") for i in state["ideas"]}
    totals_by_name = {name_by_id.get(idea_id): round(t, 1) for idea_id, t in totals.items()}
    print(
        f"  [pick_winner] 總分：{totals_by_name} "
        f"→ 贏家：{winner_idea.get('persona_name')}《{winner_idea.get('title')}》"
    )
    emit_event(
        "pick_winner", f"總分最高：{winner_idea.get('persona_name')}《{winner_idea.get('title')}》",
        extra={"totals": totals, "winner_idea": winner_idea, "idea_diversity": diversity},
    )
    return {"winner_idea": winner_idea, "idea_diversity": diversity}


def generate_evaluators(state: MeetingState) -> dict:
    """使用者要求最終評估者跟訪談對象不能重複個體——生成時明確排除
    `validate_market_fit` 概念測試訪談過的姓名，因為這場會議裡概念測試
    訪談過的人都是模擬 persona，不該讓其中任何一位又兼任盲評評估者。

    stage15-market-fit：沒有單一 `target_audience`（每個候選功能各自有
    自己的 `target_segment`，見 `draft_one_feature()`），改用策略方向 +
    全部候選功能的 target_segment 聯集當生成脈絡。解析失敗或人數不足時
    退回 `load_users()`（同樣排除已訪談過的名字）。"""
    interviewee_names = {res["interviewee_name"] for res in state["concept_test_results"]}
    segments = "、".join(dict.fromkeys(i.get("target_segment", "") for i in state["ideas"] if i.get("target_segment")))
    system = (
        f"你是使用者研究員。針對這個策略方向，額外生成 {N_EVALUATORS} "
        "位不同於已訪談過的具體人物，用來最後評估一個產品原型——他們要"
        "橫跨下面列出的目標客群範圍，但不能是以下已經訪談過的人。"
        "只輸出 JSON：{\"evaluators\":[{\"id\":\"e1\",\"name\":\"...\","
        "\"age\":數字,\"context\":\"<=60字\",\"pain_points\":[\"...\",\"...\"],"
        "\"tone\":\"<=20字\"}]}，恰好 " + str(N_EVALUATORS) + " 位"
    )
    user = (
        f"策略方向：{state['topic']}\n\n目標客群範圍：{segments or '一般使用者'}\n\n"
        f"已訪談過、不能重複的人：{', '.join(interviewee_names) or '（無）'}"
    )
    raw = call_llm(SMART_MODEL, system, user, max_tokens=1200)
    data = extract_json_object(raw)
    if not data:
        data = extract_json_object(repair_json_text(raw))
    data = data or {}
    evaluators = [
        e for e in (data.get("evaluators") or [])
        if isinstance(e, dict) and _safe_str(e.get("name")) and e.get("name") not in interviewee_names
    ][:N_EVALUATORS]

    used_fallback = False
    if len(evaluators) < 2:
        fallback_pool = [u for u in load_users() if u.get("name") not in interviewee_names]
        evaluators = (fallback_pool or load_users())[:N_EVALUATORS]
        used_fallback = True

    print(f"  [generate_evaluators] {[e.get('name') for e in evaluators]}（fallback={used_fallback}）")
    emit_event(
        "generate_evaluators", f"生成最終評估者：{[e.get('name') for e in evaluators]}",
        extra={"evaluators": evaluators, "used_fallback_evaluators": used_fallback},
    )
    return {"evaluators": evaluators, "used_fallback_evaluators": used_fallback}


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------

_BASELINE_SCHEMA_HINT = f"""
只輸出一個 JSON 物件（不要 markdown 圍欄），欄位：
- title: string（<=40字）
- summary: string（2-3句，<=120字）
- sources: [{{"title","url","how_used"}}] 最多 3 筆（可以是一般知識，不必有真實 URL，但請誠實標注）
- bmc: 物件，鍵必須恰好包含且僅包含這九個：
  {json.dumps(BMC_KEYS, ensure_ascii=False)}
  其中「收益流」「成本結構」兩格必須是物件：
  {{"narrative": "<=40字", "monthly_estimate_twd": 數字（新台幣/月）, "basis": "<=50字估算依據"}}；
  其餘七格維持一句話文字，<=40字
- self_score: number 1-10
務必輸出精簡合法 JSON，避免長文導致截斷。
"""


def _parse_baseline_proposal(text: str) -> dict:
    try:
        data = extract_json(text)
    except (json.JSONDecodeError, ValueError):
        try:
            data = extract_json(repair_json_text(text))
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"baseline 提案 JSON 解析失敗：{exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("baseline 提案不是 JSON object")
    data.setdefault("sources", [])
    data.setdefault("bmc", {})
    data.setdefault("self_score", 0)
    try:
        data["self_score"] = float(data["self_score"])
    except (TypeError, ValueError):
        data["self_score"] = 0.0
    return data


def run_baseline(topic: str, company: str) -> dict:
    role_token = _event_role.set("baseline")
    set_current_node("baseline")
    system = (
        "你是一位產品策略顧問。請針對主題直接給一個產品點子提案。"
        "若你提到依據，請誠實標注（可以是一般知識，不必有真實 URL）。"
        + _BASELINE_SCHEMA_HINT
    )
    user = f"主題：{topic}\n\n公司背景：\n{company}\n\n請直接給提案 JSON。"
    try:
        raw = call_llm(SMART_MODEL, system, user, max_tokens=2000)
        proposal = _parse_baseline_proposal(raw)
        proposal["bmc"] = _merge_bmc(proposal.get("bmc"), {})
        proposal["unit_economics"] = compute_unit_economics(proposal["bmc"])
    finally:
        _event_role.reset(role_token)
    # 使用者要求即時畫面就看得到 baseline 的完整陳述跟 BMC（第 13 點）——
    # stage9 這裡的 emit_event 只有一句摘要，即時畫面要等回放才看得到
    # 完整內容，這次改成直接把整包 proposal 放進 extra。
    emit_event(
        "baseline", f"直接問 LLM《{proposal.get('title', '')}》",
        role="baseline", extra={"proposal": proposal},
    )
    print(f"  [baseline] 《{proposal.get('title', '')}》")
    return proposal


def evaluate_final_outputs_with_users(
    *, final_proposal: dict, baseline_proposal: dict, users: List[dict],
) -> dict:
    """使用者要求：這個 demo 要誠實回答「編排 agents 是否真的比直接問一次
    LLM 更有性價比」——這裡讓動態生成的最終評估者（跟訪談對象同一個
    target audience，但不重複個體）分別對 prototype 跟 baseline 各自給
    意見＋0-10 分（使用者已確認：兩邊獨立評分，不是只給一個整體比較
    分數）。刻意用「方案 A／方案 B」盲測命名，不讓評估者知道哪個花了
    更多功夫做出來——不然「這個比較努力」的印象分數會污染評分，違背
    「誠實比對」的初衷。跟 `run_baseline()` 一樣，這個函式不是圖節點，
    在 `main()` 裡直接呼叫（真實跑測踩過的坑：原本包成圖節點、靠跟
    `run_baseline_node` 的邊 join，結果 LangGraph 沒有等主線分支跑完就
    先觸發，算出一堆假的 0 分——這兩個分支長度差太多，不能用「兩個圖
    節點各自邊接同一個下游節點」這招做同步，回到 stage9 就驗證過的
    「main() 裡依序直接呼叫」才是可靠的做法）。手動
    `set_current_node("evaluate_with_agents")`，事件的 node 欄位才會
    正確標成這個名字，不會沿用呼叫序列裡上一個設定的節點名稱。"""
    set_current_node("evaluate_with_agents")
    evaluations: List[dict] = []
    for user in users:
        role_token = _event_role.set(f"user:{user.get('name')}")
        system = (
            _user_system_prompt(user)
            + "現在有兩個產品概念要給你看，請分別依你的角度誠實給意見"
            "（喜歡/不喜歡/會不會用/有沒有疑慮），並各自打一個 0-10 分"
            "（0=完全不會用，10=非常想要）。兩個概念哪個做起來比較花"
            "功夫你不知道也不用管，只憑你自己的感受評分，不用客氣。"
            "只輸出 JSON：{\"a_reaction\":\"<=80字\",\"a_score\":數字,"
            "\"b_reaction\":\"<=80字\",\"b_score\":數字}"
        )
        user_prompt = (
            f"方案 A：{final_proposal.get('title', '')}\n{final_proposal.get('summary', '')}\n"
            f"BMC：{json.dumps(final_proposal.get('bmc'), ensure_ascii=False)}\n\n"
            f"方案 B：{baseline_proposal.get('title', '')}\n{baseline_proposal.get('summary', '')}\n"
            f"BMC：{json.dumps(baseline_proposal.get('bmc'), ensure_ascii=False)}"
        )
        try:
            raw = call_llm(SMART_MODEL, system, user_prompt, max_tokens=400)
            data = extract_json_object(raw)
        finally:
            _event_role.reset(role_token)
        # 分數 clamp／保底沿用 score_proposal() 同款防呆：解析失敗不能讓
        # 平均分被污染成 0，保底給中位數 5.0。
        try:
            agent_score = max(0.0, min(10.0, float(data.get("a_score"))))
        except (TypeError, ValueError):
            agent_score = 5.0
        try:
            baseline_score = max(0.0, min(10.0, float(data.get("b_score"))))
        except (TypeError, ValueError):
            baseline_score = 5.0
        entry = {
            "user_id": user.get("id"), "user_name": user.get("name"),
            "agent_reaction": _safe_str(data.get("a_reaction")) or "（無反應）",
            "agent_score": agent_score,
            "baseline_reaction": _safe_str(data.get("b_reaction")) or "（無反應）",
            "baseline_score": baseline_score,
        }
        evaluations.append(entry)
        emit_event(
            "evaluate_final_outputs",
            f"{user.get('name')} 評分：共創方案 {agent_score} 分／baseline {baseline_score} 分",
            role=f"user:{user.get('name')}", extra=entry,
        )
        print(f"  [evaluate:{user.get('name')}] 共創={agent_score} baseline={baseline_score}")

    n = len(evaluations) or 1
    agent_avg = round(sum(e["agent_score"] for e in evaluations) / n, 2)
    baseline_avg = round(sum(e["baseline_score"] for e in evaluations) / n, 2)
    summary = {
        "evaluations": evaluations,
        "agent_avg_score": agent_avg,
        "baseline_avg_score": baseline_avg,
        "score_delta": round(agent_avg - baseline_avg, 2),
    }
    # 這筆事件的 extra 把完整的兩份提案都放進去（不是只有分數）——使用者
    # 要求即時畫面/回放器點開能「兩者平行呈現」，不用另外拼湊多筆事件
    # 才看得到完整 BMC。
    emit_event(
        "user_evaluation_summary",
        f"模擬使用者評分：共創方案平均 {agent_avg} 分／baseline 平均 {baseline_avg} 分"
        f"（差距 {summary['score_delta']:+.2f}）",
        extra={**summary, "final_proposal": final_proposal, "baseline_proposal": baseline_proposal},
    )
    print(
        f"  [user_evaluation_summary] 共創平均={agent_avg} baseline平均={baseline_avg} "
        f"差距={summary['score_delta']:+.2f}"
    )
    return summary


def generate_final_verdict(
    *,
    topic: str,
    winner_idea: dict,
    baseline_proposal: dict,
    baseline_metrics: dict,
    idea_diversity: dict,
    user_evaluation: dict,
) -> str:
    """使用者要求：最後讓 AI 直接比較這場真實資料裡 agent 流程 vs baseline
    的優劣，不是只有結構性的數字對照表——這段話要有觀點、具體點名兩邊的
    優勢與代價，也要誠實承認 baseline 的價值（速度快、成本低），不是為了
    捧 agent 流程而失真。`user_evaluation` 是動態生成的最終評估者對兩邊
    各自打的真實 0-10 分——沒有這個之前，這段評語只是「AI 自己讀結構性
    數字寫的感想」，等於自問自答；餵進真實的第三方評分數字，才是使用者
    要的「誠實比對，不是自說自話」。"""
    system = (
        "你是一位產品策略顧問，要針對這場真實跑出來的資料，比較「多 agent 平行發散+"
        "DFV 結構化評分收斂」產出的最終 idea，跟「直接問 LLM 一次」的 baseline 提案，"
        "寫一段有觀點的優劣分析。要具體點名兩邊各自的優勢與代價（不是空泛通則），"
        "並誠實承認 baseline 也有它的價值（例如速度快、成本低、適合初步發散），"
        "不要為了捧多 agent 流程而失真。模擬評估者的真實評分是這場比較最重要的證據，"
        "務必在評語中明確引用這組分數。只輸出正文，<=300字。"
    )
    user = (
        f"主題：{topic}\n\n"
        f"多 agent 流程選出的最終 idea：《{winner_idea.get('title', '')}》"
        f"{winner_idea.get('summary', '')}\n\n"
        f"Baseline 提案：《{baseline_proposal.get('title', '')}》{baseline_proposal.get('summary', '')}\n\n"
        f"模擬評估者對照評分（0-10 分，各自獨立評分，不知道哪個花了更多功夫做出來）："
        f"agent 方案平均 {user_evaluation.get('agent_avg_score')} 分，"
        f"baseline 平均 {user_evaluation.get('baseline_avg_score')} 分，"
        f"差距 {user_evaluation.get('score_delta'):+.2f}\n\n"
        f"量化對照：{N_PERSONAS} 位與會者各自獨立發想的 idea 彼此多樣性（兩兩平均距離）="
        f"{idea_diversity.get('avg_distance')}（顯示是真的平行發散，不是同一個想法換句話說），"
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
    competitive_landscape: List[dict],
    used_fallback_competitive_landscape: bool,
    research_queries: List[str],
    research_items: List[dict],
    personas: List[dict],
    used_fallback_personas: bool,
    ideas: List[dict],
    human_qa_log: List[dict],
    survey_results: List[dict],
    survey_summary: dict,
    concept_test_results: List[dict],
    concept_test_summary: dict,
    dfv_scores: List[dict],
    winner_idea: dict,
    idea_diversity: dict,
    prototype: dict,
    evaluators: List[dict],
    used_fallback_evaluators: bool,
    baseline_proposal: dict,
    baseline_metrics: dict,
    user_evaluation: dict,
    final_verdict: str = "",
) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = {
        "run_id": stamp,
        "round_id": round_id,
        "topic": topic,
        "competitive_landscape": competitive_landscape,
        "used_fallback_competitive_landscape": used_fallback_competitive_landscape,
        "research_queries": research_queries,
        "research_items": research_items,
        "personas": personas,
        "used_fallback_personas": used_fallback_personas,
        "ideas": ideas,
        "human_qa_log": human_qa_log,
        "survey_results": survey_results,
        "survey_summary": survey_summary,
        "concept_test_results": concept_test_results,
        "concept_test_summary": concept_test_summary,
        "dfv_scores": dfv_scores,
        "winner_idea": winner_idea,
        "idea_diversity": idea_diversity,
        "prototype": prototype,
        "evaluators": evaluators,
        "used_fallback_evaluators": used_fallback_evaluators,
        "baseline": {"proposal": baseline_proposal, "metrics": baseline_metrics},
        # 使用者要求「誠實比對」的核心證據：動態生成的最終評估者對
        # prototype 跟 baseline 各自獨立打的 0-10 分（不是 AI 自己讀數字
        # 寫感想）。
        "user_evaluation": user_evaluation,
        "final_verdict": final_verdict,
        "total_cost_usd": round(total_cost(), 6),
    }
    path = OUTPUT_DIR / f"stage15-run-{stamp}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUTPUT_DIR / f"stage15-latest-{topic[:12]}.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


# ---------------------------------------------------------------------------
# 最終報告
# ---------------------------------------------------------------------------

def build_final_report_markdown(
    *,
    round_id: str,
    topic: str,
    competitive_landscape: List[dict],
    used_fallback_competitive_landscape: bool,
    survey_summary: dict,
    concept_test_summary: dict,
    personas: List[dict],
    ideas: List[dict],
    human_qa_log: List[dict],
    dfv_scores: List[dict],
    winner_idea: dict,
    idea_diversity: dict,
    prototype: dict,
    evaluators: List[dict],
    baseline_proposal: dict,
    baseline_metrics: dict,
    user_evaluation: dict,
    final_verdict: str = "",
) -> str:
    # 使用者要求報告裡重要的是「角色」，不是單純的人名——同一個名字對讀者
    # 沒有意義，只有搭配 persona 的專業角色／訪談對象的身份與情境，才看得
    # 出「這個功能提案是從什麼角度想出來的」。這裡建兩份 name -> 角色描述
    # 的對照表，讓後面每一處提到人名的地方都能順手帶出角色，不用每次
    # 重複寫查表邏輯。
    role_by_persona_name = {p.get("name"): p.get("role", "") for p in personas}

    def _person_role_label(person: dict) -> str:
        age = person.get("age")
        context = person.get("context", "")
        age_part = f"{age}歲，" if age else ""
        return f"{age_part}{context}" if context else age_part.rstrip("，")

    role_by_user_name = {u.get("name"): _person_role_label(u) for u in evaluators}

    def _persona_with_role(name: str) -> str:
        role = role_by_persona_name.get(name)
        return f"{name}（{role}）" if role else name

    def _user_with_role(name: str) -> str:
        role = role_by_user_name.get(name)
        return f"{name}（{role}）" if role else name

    L: List[str] = []
    L.append("# 策略導向市場競爭力驗證最終報告\n")
    L.append(f"- **策略方向**：{topic}")
    L.append(f"- **Round ID**：{round_id}")
    L.append(f"- **腦力激盪參與者**：{'、'.join(_persona_with_role(p['name']) for p in personas)}")
    L.append(f"- **最終評估者**：{'、'.join(_user_with_role(e['name']) for e in evaluators)}\n")
    L.append("---\n")

    L.append("## 市場現況：真實競品掃描\n")
    if used_fallback_competitive_landscape:
        L.append("（本次未能取得具體競品資訊，以下功能提案的差異化評估僅供參考）\n")
    else:
        for c in competitive_landscape:
            L.append(f"- **{c.get('competitor_name', '')}**：{c.get('feature_description', '')}")
            if c.get("source_url"):
                L.append(f"  （來源：{c['source_url']}）")
        L.append("")
    L.append("---\n")

    L.append("## 腦力激盪參與者各自的功能提案（各自設計自己的 BMC）\n")
    L.append("**參與者側寫**（各自帶著什麼專業角度發想，比名字本身重要）：\n")
    for p in personas:
        L.append(f"- {_persona_with_role(p.get('name'))}：{p.get('background', '')}")
    L.append("")
    for idea in ideas:
        L.append(f"### {_persona_with_role(idea.get('persona_name'))}：《{idea.get('title', '')}》")
        L.append(f"{idea.get('summary', '')}")
        L.append(f"- 理由：{idea.get('rationale', '')}")
        L.append(f"- 目標客群：{idea.get('target_segment', '')}")
        L.append(f"- 貨幣化機制：{idea.get('monetization_mechanism', '')}")
        L.append(f"- 差異化說法：{idea.get('differentiation_vs_competitors', '')}\n")
        L.append("**這位參與者自己設計的 Business Model Canvas**：\n")
        for k in BMC_KEYS:
            L.append(f"- {k}：{_format_bmc_line(k, (idea.get('bmc') or {}).get(k))}")
        L.append("")
    L.append("---\n")

    L.append("## 虛擬問卷（模擬訊號，非真人樣本）\n")
    if survey_summary.get("by_feature"):
        L.append(f"總模擬樣本數：{survey_summary.get('total_simulated_n', 0)}\n")
        for feature_id, stats in survey_summary["by_feature"].items():
            L.append(f"- [{feature_id}] {stats.get('feature_title', '')}")
            L.append(
                f"  - 模擬購買意願：{stats.get('purchase_intent_pct')}%，"
                f"模擬差異化感受：{stats.get('differentiation_pct')}%"
            )
            for q in stats.get("sample_quotes", []):
                L.append(f"  - 模擬引述：{q}")
        L.append(f"\n> {survey_summary.get('caveat', SURVEY_METHOD_CAVEAT)}\n")
    else:
        L.append("（本次未產生虛擬問卷資料）\n")
    L.append("---\n")

    L.append("## 概念測試訪談（模擬訊號，簡化版 1-2 輪固定問題）\n")
    if concept_test_summary.get("by_feature"):
        for feature_id, stats in concept_test_summary["by_feature"].items():
            L.append(f"- [{feature_id}] {stats.get('feature_title', '')}")
            L.append(
                f"  - {stats.get('n_interviewed')} 位模擬訪談中 "
                f"{stats.get('would_pay_pct')}% 表示願意付費/切換"
            )
            for r in stats.get("sample_reactions", []):
                L.append(f"  - 代表反應：{r}")
        L.append(f"\n> {SURVEY_METHOD_CAVEAT}\n")
    else:
        L.append("（本次未產生概念測試資料）\n")
    L.append("---\n")

    L.append("## 人類提問記錄\n")
    if human_qa_log:
        for qa in human_qa_log:
            L.append(f"**問 {_persona_with_role(qa['presenter_name'])}**：{qa['question']}")
            L.append(f"> {qa['answer']}\n")
    else:
        L.append("（本場沒有人類提問，全程跳過）\n")

    L.append("## DFV 結構化評分（Desirability / Feasibility / Viability / Market Fit）\n")
    name_by_id = {idea.get("id"): idea.get("persona_name") for idea in ideas}
    for lens in DFV_LENSES:
        L.append(f"### {lens['name']}（{lens['angle']}）\n")
        for s in [s for s in dfv_scores if s["lens_id"] == lens["id"]]:
            L.append(f"- **{_persona_with_role(name_by_id.get(s['idea_id'], s['idea_id']))}**：{s['score']} 分 — {s['critique']}")
        L.append("")

    L.append("## 收斂結果\n")
    L.append(
        f"總分最高：**{_persona_with_role(winner_idea.get('persona_name'))}**《{winner_idea.get('title', '')}》"
        f"（總分 {winner_idea.get('total_score', 0):.1f}）\n"
    )
    L.append(f"idea 多樣性（發想階段彼此的兩兩平均距離）：{idea_diversity.get('avg_distance')}\n")

    L.append("## Prototype\n")
    L.append(f"**{_persona_with_role(prototype.get('persona_name'))}**：《{prototype.get('title', '')}》")
    L.append(f"{prototype.get('summary', '')}\n")
    L.append(f"原型：`{prototype.get('html_path')}`（可直接用瀏覽器開啟）\n")

    L.append("## Baseline 對照（直接問 LLM）\n")
    L.append(f"**Baseline 提案**：《{baseline_proposal.get('title', '')}》{baseline_proposal.get('summary', '')}\n")
    L.append(f"- 真實搜尋引用：{baseline_metrics.get('real_citations', 0)}（可能編造，無法驗證）")
    L.append(f"- 成本：${baseline_metrics.get('cost_usd', 0):.4f}（單次呼叫，沒有訪談／評分依據）\n")

    L.append("## 最終評估者對照評分（Prototype vs Baseline）\n")
    L.append(
        "動態生成、跟訪談對象不重複的最終評估者，在不知道哪個方案花了更多"
        "功夫做出來的情況下，分別對兩個方案給意見＋0-10 分（評估者是誰、"
        "站在什麼情境給分，比名字本身重要）：\n"
    )
    for e_profile in evaluators:
        pain = "、".join(e_profile.get("pain_points") or [])
        L.append(f"- {_user_with_role(e_profile.get('name'))}" + (f"，痛點：{pain}" if pain else ""))
    L.append("")
    evaluations = user_evaluation.get("evaluations") or []
    if evaluations:
        L.append("| 評估者 | Prototype 評分 | Prototype 意見 | Baseline 評分 | Baseline 意見 |")
        L.append("|---|---|---|---|---|")
        for e in evaluations:
            # 側寫已經在上面列過一次，表格欄位只留名字，不然每一列都重複
            # 整段情境描述會讓表格塞爆、比對分數反而變難讀。
            L.append(
                f"| {e['user_name']} | {e['agent_score']} | {e['agent_reaction']} "
                f"| {e['baseline_score']} | {e['baseline_reaction']} |"
            )
        L.append("")
        L.append(
            f"**平均分**：agent 方案 {user_evaluation.get('agent_avg_score')} 分 vs "
            f"baseline {user_evaluation.get('baseline_avg_score')} 分"
            f"（差距 {user_evaluation.get('score_delta', 0):+.2f}）\n"
        )
    else:
        L.append("（本場沒有評估者評分紀錄）\n")

    if final_verdict:
        L.append("## AI 對照評語（agent 流程 vs baseline）\n")
        L.append(f"{final_verdict}\n")

    return "\n".join(L)


def build_parent_graph(checkpointer):
    """策略導向、由上而下的市場競爭力驗證拓樸——純線性 DAG，沒有任何
    多重靜態入邊節點（見 test_graph.py 的拓樸安全性測試，把這件事從
    docstring 宣稱變成自動化回歸測試）。stage12 踩到的坑（詳見
    stage14-signals/note.md）：LangGraph 對「兩個長度差很多的分支各自
    邊接同一個下游節點」不保證會等全部前驅都完成——這張圖延續既有修好
    的做法，只用兩種驗證過安全的 fan-in 模式的遞迴組合：(A) `Send()`
    fan-out 到同一個節點名稱、(B) 單一節點內同步呼叫 `xxx_graph.invoke()`。

    跟 stage14-signals 最大的差異：策略方向（`topic`）由公司高層給定，
    不需要 Discover/Define 階段的候選 job 假設/訪談驗證，所以
    `research_competitive_landscape`（市場現況掃描，取代整個 Discover/
    Define）→ `assemble_persona_team`（模式 B，內部模式 A fan-out 到
    `generate_one_persona_for_domain`）→ `draft_one_feature`（模式 A
    fan-out）→ `ask_question`/`answer_question`（HITL 迴圈，原封不動
    沿用）→ `validate_market_fit`（模式 B，內部依序同步呼叫三個子圖：
    虛擬問卷、概念測試訪談、DFV 四面向評分——見該函式 docstring 說明
    為什麼是依序呼叫，不是三條分支各自指到 `pick_winner`）→
    `pick_winner` → `generate_prototype` → `generate_evaluators`，全程
    嚴格單一前驅鏈。baseline／最終評分／存檔報告整個沿用 stage9 驗證過的
    模式——不是圖節點，在 `main()` 裡用背景執行緒跟主線平行跑、最後
    `join()`。"""
    g = StateGraph(MeetingState)
    g.add_node(
        "research_competitive_landscape",
        instrument("research_competitive_landscape", research_competitive_landscape),
    )
    g.add_node("assemble_persona_team", instrument("assemble_persona_team", assemble_persona_team))
    g.add_node("draft_one_feature", instrument("draft_one_feature", draft_one_feature))
    g.add_node("ask_question", instrument("ask_question", ask_question))
    g.add_node("answer_question", instrument("answer_question", answer_question))
    g.add_node("validate_market_fit", instrument("validate_market_fit", validate_market_fit))
    g.add_node("pick_winner", instrument("pick_winner", pick_winner))
    g.add_node("generate_prototype", instrument("generate_prototype", generate_prototype))
    g.add_node("generate_evaluators", instrument("generate_evaluators", generate_evaluators))

    g.add_edge(START, "research_competitive_landscape")
    g.add_edge("research_competitive_landscape", "assemble_persona_team")
    g.add_conditional_edges("assemble_persona_team", fan_out_ideas, ["draft_one_feature"])
    g.add_edge("draft_one_feature", "ask_question")

    g.add_conditional_edges(
        "ask_question", route_after_question,
        {"answer_question": "answer_question", "validate_market_fit": "validate_market_fit"},
    )
    g.add_edge("answer_question", "ask_question")

    g.add_edge("validate_market_fit", "pick_winner")
    g.add_edge("pick_winner", "generate_prototype")
    g.add_edge("generate_prototype", "generate_evaluators")
    g.add_edge("generate_evaluators", END)
    return g.compile(checkpointer=checkpointer)


# ---------------------------------------------------------------------------
# CLI 驅動
# ---------------------------------------------------------------------------

def _load_script(path: Optional[str]) -> Optional[dict]:
    if not path:
        return None
    return json.loads(Path(path).read_text(encoding="utf-8"))


def get_human_input(payload: dict, script: Optional[dict]) -> dict:
    """使用者要求反過來從問題出發（stage12 版）：`ask_question` 的
    payload 現在是「列出全部 N 個 idea」，不是單一 presenter，這裡的
    resume 格式跟著改成要指定 `target_idea_id`（真實跑測踩過的坑：這個
    函式沒跟著 `ask_question` 一起改，還在讀舊的 `payload["presenter_id"]`，
    非互動執行時直接 KeyError 崩潰）。"""
    ideas = payload.get("ideas") or []
    asked = payload.get("questions_asked_so_far", 0)
    if script is not None:
        entry = script if isinstance(script, dict) else {"skip": True}
        if entry.get("skip"):
            return {"action": "skip"}
        questions = entry.get("questions") or []
        if asked < len(questions):
            q = questions[asked]
            target_idea_id = q.get("target_idea_id") or (ideas[0]["id"] if ideas else None)
            print(f"  [scripted] 對 idea {target_idea_id} 提問：{q.get('question')}")
            return {"action": "ask", "target_idea_id": target_idea_id, "question": q.get("question")}
        return {"action": "skip"}
    print("\n" + json.dumps(payload, ensure_ascii=False, indent=2))
    try:
        idx_raw = input(f"要問哪個 idea？輸入編號 1-{len(ideas)}，或直接按 Enter 跳過：").strip()
    except EOFError:
        idx_raw = ""
    if not idx_raw:
        return {"action": "skip"}
    try:
        target_idea_id = ideas[int(idx_raw) - 1]["id"]
    except (ValueError, IndexError):
        return {"action": "skip"}
    try:
        question = input("問題內容：").strip()
    except EOFError:
        question = ""
    if not question:
        return {"action": "skip"}
    return {"action": "ask", "target_idea_id": target_idea_id, "question": question}


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
    parser = argparse.ArgumentParser(description="Stage 13：Double Diamond 腦力激盪（不設時間/成本上限）")
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
    company = load_company()
    script = _load_script(args.script)
    n_concept_test_interviewees = int(
        os.environ.get("BRAINSTORM_N_CONCEPT_TEST_INTERVIEWEES", str(N_CONCEPT_TEST_INTERVIEWEES))
    )
    survey_n = int(os.environ.get("BRAINSTORM_SURVEY_N", str(DEFAULT_SURVEY_RESPONDENTS_PER_STRATUM)))

    print(f"主題：{topic}")
    print(f"Thread／round_id：{thread_id}（checkpoint db：{CHECKPOINT_DB_PATH}）")
    print("不設時間/成本上限——設計正確優先，誠實記錄真實耗時/成本（見 note.md）")
    print(f"事件流：{EVENTS_PATH}")
    print()

    # 使用者要求 baseline 從一開始就跟主線平行跑，縮短壁鐘時間（第 4 點）
    # ——真實跑測踩過的坑：原本想把這個包成圖節點、靠邊 join 回主線，結果
    # LangGraph 沒有等主線跑完就先觸發下游，算出一堆假資料（詳見
    # build_parent_graph() 的 docstring）。改回 stage9 驗證過的模式：
    # 背景執行緒 + 之後 join()，完全不碰圖拓樸，簡單可靠。
    baseline_result: dict = {}

    def _run_baseline_in_background():
        baseline_result["proposal"] = run_baseline(topic, company)

    baseline_thread = Thread(target=_run_baseline_in_background, daemon=True)
    baseline_thread.start()

    wall_t0 = time.perf_counter()
    final_state = run_meeting(
        meeting_graph,
        config,
        {
            "topic": topic,
            "company": company,
            "round_id": round_id,
            "competitive_landscape": [],
            "used_fallback_competitive_landscape": False,
            "research_queries": [],
            "research_items": [],
            "personas": [],
            "used_fallback_personas": False,
            "ideas": [],
            "pending_question": None,
            "pending_question_target_idea_id": None,
            "pending_question_asked_by": None,
            "human_qa_log": [],
            "n_concept_test_interviewees": n_concept_test_interviewees,
            "survey_respondents_per_stratum": survey_n,
            "survey_results": [],
            "survey_summary": {},
            "concept_test_results": [],
            "concept_test_summary": {},
            "dfv_scores": [],
            "winner_idea": {},
            "idea_diversity": {},
            "prototype": {},
            "evaluators": [],
            "used_fallback_evaluators": False,
        },
        script=script,
        stop_after_first_interrupt=args.stop_after_first_interrupt,
    )
    wall_elapsed = time.perf_counter() - wall_t0

    if final_state is None:
        print(f"\n（process 於 {wall_elapsed:.1f}s 後主動結束，尚未完成——用同個 --thread {thread_id} 續跑）")
        return

    ideas = final_state["ideas"]
    personas = final_state["personas"]
    competitive_landscape = final_state["competitive_landscape"]
    survey_results = final_state["survey_results"]
    concept_test_results = final_state["concept_test_results"]
    concept_test_interviewee_names = {res["interviewee_name"] for res in concept_test_results}
    dfv_scores = final_state["dfv_scores"]
    winner_idea = final_state["winner_idea"]
    idea_diversity = final_state["idea_diversity"]
    prototype = final_state["prototype"]
    evaluators = final_state["evaluators"]
    human_qa_log = final_state["human_qa_log"]

    baseline_thread.join(timeout=120)
    baseline_proposal = baseline_result.get("proposal")
    if baseline_proposal is None:
        print("\n（baseline 背景執行緒逾時未完成，用系統保底提案代替）")
        baseline_proposal = {"title": "（系統保底）baseline 逾時", "summary": "", "bmc": _merge_bmc(None, {})}

    print()
    print("=== 收斂結果 ===")
    print(
        f"贏家：{winner_idea.get('persona_name')}《{winner_idea.get('title')}》"
        f"（總分 {winner_idea.get('total_score', 0):.1f}）"
    )

    print()
    print("=== 最終評估者對照評分：agent 方案 vs baseline ===")
    final_proposal = {
        "title": prototype.get("title"), "summary": prototype.get("summary"), "bmc": prototype.get("bmc"),
    }
    user_evaluation = evaluate_final_outputs_with_users(
        final_proposal=final_proposal, baseline_proposal=baseline_proposal, users=evaluators,
    )

    baseline_cost = _role_cost("baseline")
    baseline_metrics = metrics_of(baseline_proposal, [], [], baseline_cost)

    print()
    print("=== AI 對照評語：agent 流程 vs baseline ===")
    final_verdict = generate_final_verdict(
        topic=topic, winner_idea=winner_idea, baseline_proposal=baseline_proposal,
        baseline_metrics=baseline_metrics, idea_diversity=idea_diversity, user_evaluation=user_evaluation,
    )
    print(f"  {final_verdict}")

    out_path = save_outputs(
        round_id=round_id, topic=topic,
        competitive_landscape=competitive_landscape,
        used_fallback_competitive_landscape=final_state["used_fallback_competitive_landscape"],
        research_queries=final_state["research_queries"], research_items=final_state["research_items"],
        personas=personas,
        used_fallback_personas=final_state["used_fallback_personas"], ideas=ideas,
        human_qa_log=human_qa_log,
        survey_results=survey_results, survey_summary=final_state["survey_summary"],
        concept_test_results=concept_test_results, concept_test_summary=final_state["concept_test_summary"],
        dfv_scores=dfv_scores,
        winner_idea=winner_idea, idea_diversity=idea_diversity,
        prototype=prototype, evaluators=evaluators,
        used_fallback_evaluators=final_state["used_fallback_evaluators"],
        baseline_proposal=baseline_proposal, baseline_metrics=baseline_metrics,
        user_evaluation=user_evaluation, final_verdict=final_verdict,
    )

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report_md = build_final_report_markdown(
        round_id=round_id, topic=topic,
        competitive_landscape=competitive_landscape,
        used_fallback_competitive_landscape=final_state["used_fallback_competitive_landscape"],
        survey_summary=final_state["survey_summary"], concept_test_summary=final_state["concept_test_summary"],
        personas=personas, ideas=ideas,
        human_qa_log=human_qa_log, dfv_scores=dfv_scores,
        winner_idea=winner_idea, idea_diversity=idea_diversity,
        prototype=prototype, evaluators=evaluators,
        baseline_proposal=baseline_proposal, baseline_metrics=baseline_metrics,
        user_evaluation=user_evaluation, final_verdict=final_verdict,
    )
    report_path = REPORT_DIR / f"{round_id}-final-report.md"
    report_path.write_text(report_md, encoding="utf-8")
    print()
    print(f"最終報告：{report_path}")
    print(f"已存檔：{out_path}")

    print()
    print("=== 驗收 ===")
    ideas_ok = len(ideas) == len(personas) and len(personas) >= 2
    dfv_ok = len(dfv_scores) == len(DFV_LENSES) * len(ideas)
    prototype_ok = (
        bool(prototype.get("html_path"))
        and Path(prototype["html_path"]).exists()
        and Path(prototype["html_path"]).stat().st_size > 0
    )
    evaluators_ok = len(evaluators) >= 2 and all(
        e.get("name") not in concept_test_interviewee_names for e in evaluators
    )
    user_evaluation_ok = len(user_evaluation.get("evaluations") or []) == len(evaluators) and all(
        0 <= e["agent_score"] <= 10 and 0 <= e["baseline_score"] <= 10
        for e in user_evaluation.get("evaluations") or []
    )
    report_text = report_path.read_text(encoding="utf-8") if report_path.exists() else ""
    report_complete = (
        report_path.exists()
        and len(report_text) > 500
        and "市場現況" in report_text
        and "DFV 結構化評分" in report_text
        and "最終評估者對照評分" in report_text
    )
    # 真實跑測踩過的坑：evaluate_with_agents 一度因為 join 沒等到主線
    # 資料，讓 baseline/agent 平均分都變成假的 0.0——這裡明確驗證兩邊
    # 平均分不是同時剛好是 0（那組合幾乎不可能是真實評分，一出現就代表
    # 評分用的資料是空的）。
    scores_look_real = not (user_evaluation.get("agent_avg_score") == 0.0 and user_evaluation.get("baseline_avg_score") == 0.0)

    # stage15-market-fit 特有的驗收項——虛擬問卷跑滿全部人口分層、概念
    # 測試跑滿「候選功能 × 訪談對象」的笛卡兒積、參與者的公司衍生職能
    # 彼此不重複。
    survey_ok = len(survey_results) == len(SURVEY_STRATA)
    concept_test_ok = len(concept_test_results) == len(ideas) * n_concept_test_interviewees
    persona_domains = [p.get("domain") for p in personas if p.get("domain")]
    personas_domain_ok = len(persona_domains) == len(set(persona_domains))

    print(f"每位參與者都提了一個功能提案（{len(ideas)}/{len(personas)}）：{'是' if ideas_ok else '否'}")
    print(f"DFV 評分筆數正確（{len(dfv_scores)}，應為 {len(DFV_LENSES)}x{len(ideas)}）：{'是' if dfv_ok else '否'}")
    print(f"Prototype 已寫出可開啟的 HTML：{'是' if prototype_ok else '否'}")
    print(f"最終評估者跟概念測試訪談對象都不重複（{len(evaluators)} 位）：{'是' if evaluators_ok else '否'}")
    print(f"最終評估者對兩個方案都留下合法評分：{'是' if user_evaluation_ok else '否'}")
    print(f"評分數字不是可疑的雙 0（agent={user_evaluation.get('agent_avg_score')}, baseline={user_evaluation.get('baseline_avg_score')}）：{'是' if scores_look_real else '否'}")
    print(f"最終報告完整：{'是' if report_complete else '否'}")
    print(f"虛擬問卷跑滿全部 {len(SURVEY_STRATA)} 個人口分層：{'是' if survey_ok else '否'}")
    print(f"概念測試訪談筆數正確（{len(concept_test_results)} 筆）：{'是' if concept_test_ok else '否'}")
    print(f"參與者的公司衍生職能不重複（{len(persona_domains)} 個 domain）：{'是' if personas_domain_ok else '否'}")
    print(f"總耗時：{wall_elapsed:.1f}s（不設上限，誠實記錄）")
    print(f"總成本：${total_cost():.4f}（不設上限，誠實記錄）")
    print(f"idea 多樣性 avg_distance：{idea_diversity.get('avg_distance')}"
          "（對照 stage12 歷史值 0.2382/0.2537/0.2821，見 note.md）")
    print(f"事件流：{EVENTS_PATH}")
    print_run_summary()

    ok = (
        ideas_ok and dfv_ok and prototype_ok and evaluators_ok and user_evaluation_ok
        and scores_look_real and report_complete
        and survey_ok and concept_test_ok and personas_domain_ok
        and EVENTS_PATH.exists()
    )
    if not ok:
        print("\n驗收未通過，請檢查上方輸出。")
        sys.exit(2)
    print("\n驗收通過。")


if __name__ == "__main__":
    main()
