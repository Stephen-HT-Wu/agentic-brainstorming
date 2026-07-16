"""
階段 13：Double Diamond 重構（Discover → Define → Develop → Deliver），
完全脫離 stage12「5 分鐘 demo、盡量省成本」的限制，把「發現問題」跟
「發想解法」兩個菱形真正分開。

stage12 真實跑測一路挖出同一個根因：`analyze_and_scope()` 在任何訪談
發生之前，就已經把問題收斂成一個具體、常常連產品命名都定了的
`strategic_goal`——不管輸入的 topic 多開放，這一步都會收斂成一個方向，
後面訪談做得多深、BMC 各自設計得多獨立，所有 persona 都只能在同一個
已經選定的框架裡精修。真實跑測量到的 `idea_diversity.avg_distance`
（0.2382、0.2537、0.2821）全部低於「同一個 idea 換句話說」的校準基準值
（見 stage12/note.md）。

stage13 用 Double Diamond 重排節點順序：

- **Discover**（發散）：`desk_research_hypothesize_jobs` 桌面研究只
  假設 2-4 個候選 job（JTBD 陳述，刻意不含任何解法/產品命名），
  `discover_and_evaluate_jobs` 對每個候選 job 各自訪談一組小規模對象，
  訪談問法改用 JTBD 的「switch」技巧（回溯式：什麼契機讓你行動、
  考慮過哪些替代方案又為什麼放棄、卡關的瞬間是什麼），不是驗證某個
  預設方向。
- **Define**（收斂：定義問題，不定義解法）：`select_job_and_define_problem`
  根據訪談證據選出 ONE 最真實/最未被滿足的 job，推導出 target_audience
  跟一個解法無關的 problem_statement/hmw——這是修掉 premature
  convergence 的關鍵欄位，state 裡不再有 `strategic_goal` 這種解法
  欄位。
- **Develop**（發散：解法）：`assemble_persona_team` 先用
  `derive_company_domains()` 從 `company.md` 衍生出這家公司實際具備、
  彼此明顯不同的職能（不是跟公司無關的任意領域，也不是 LLM 自己判斷的
  「互補團隊」），各自獨立生成一位參與者，每人針對 problem_statement/hmw
  獨立發想一個 idea＋自己的 BMC。
- **Deliver**（收斂：定案）：DFV 三面向結構化評分→選總分最高→原型→
  跟訪談過的人不重複的最終評估者→誠實對照 baseline——這半段沿用
  stage12 已驗證的機制，沒有被診斷出問題，不重新設計。

節點拓樸是「兩種已驗證安全的 fan-in 模式」（Send() fan-out 到同一個
節點名稱；單一節點內同步呼叫 subgraph.invoke()）遞迴組合而成，不引入
任何新的 join 寫法——這是 stage12/note.md 記錄的真實教訓：LangGraph
對「兩條長度不同的分支都指到同一個節點」不保證等全部前驅完成才觸發。

本檔以 stage12/graph.py 為起點複製再重構（不 import stage12），延續
「每個 stage 一份完整獨立副本」的既有慣例。底層工具（web 搜尋、
embedding/dedup、call_llm/emit_event、BMC 量化與合併、JSON 解析、
landing page 原型渲染、baseline、盲測評分、HITL 驅動迴圈、DFV 評分、
pick_winner）直接沿用 stage12 已經驗證過的實作，只有 Discover/Define/
Develop 這半段的節點邏輯跟圖拓樸重寫。

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
EVENTS_PATH = OUTPUT_DIR / "stage13_events.jsonl"
CHECKPOINT_DB_PATH = OUTPUT_DIR / "stage13_checkpoints.sqlite"
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
# stage13 把 Discover 階段的訪談從「驗證某個預設方向」改成 JTBD 的
# 「switch」技巧（回溯式重建一次真實轉換/採取行動決策的時間軸，用來
# 檢驗候選 job 假設是不是真的）——輪次意圖固定（見
# SWITCH_INTERVIEW_ROUND_INTENTS），輪數維持 stage12 修正後的深度
# （沒有成本上限也不隨意加減）。
SWITCH_INTERVIEW_ROUNDS = 5

# stage13 完全脫離 stage12「5 分鐘 demo、盡量省成本」的限制——候選 job
# 數／每個候選 job 的訪談對象數／persona 數／評估者數都不再被「數量小
# 才撐得住 5 分鐘目標」綁住，優先把 Double Diamond 的每個菱形做完整。
N_CANDIDATE_JOBS = 3          # Discover 階段假設的候選 job 數（2-4 之間）
N_INTERVIEWEES_PER_JOB = 2    # 每個候選 job 各自訪談的人數
N_PERSONAS = 5                 # Develop 階段扣著公司能力衍生職能的參與
                                # 者數（從 stage12 的 3 提高，畢竟沒有
                                # 成本上限，且職能覆蓋度值得多留幾個席位）
N_EVALUATORS = 3

# 使用者要求把三位大師的簡短點評改成 Desirability/Feasibility/
# Viability 三面向結構化評分——每位評審只負責一個面向，給 0-10 分＋
# 一大段文字批評（使用者確認保留現在這種長篇批評的份量），收斂時把
# 三個面向的分數加總、選總分最高的 idea（見 pick_winner()）。
DFV_LENSES = [
    {"id": "desirability", "name": "顧客需求性評審", "dimension": "desirability",
     "angle": "使用者真的想要、會用嗎？是不是解決了真實痛點？"},
    {"id": "feasibility", "name": "技術可行性評審", "dimension": "feasibility",
     "angle": "技術上做不做得出來？架構與資料風險有多大？"},
    {"id": "viability", "name": "商業存續性評審", "dimension": "viability",
     "angle": "商業模式撐不撐得住？誰付錢、划不划算？"},
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


# stage12 用 5-Whys（往下挖一個已知症狀的根因，前提是已經知道要往哪裡
# 挖）；stage13 的 Discover 階段要檢驗的是「這個候選 job 假設是不是
# 真的」，不是驗證某個預設方向——這是不同形狀的問題，不是深度差異，
# 所以改用 JTBD 的「switch」訪談技巧：回溯式重建一次真實「轉換／採取
# 行動」決策的時間軸（什麼契機讓你行動、考慮過哪些替代方案又為什麼
# 放棄、卡關的瞬間是什麼），輪次意圖固定，不是自由連續追問「為什麼」。
SWITCH_INTERVIEW_ROUND_INTENTS = [
    {"round": 1, "intent": "retrospective_trigger",
     "hint": "回想上一次你真的採取行動處理這個情境的那一刻，什麼契機讓你當時決定要做點什麼？"},
    {"round": 2, "intent": "passive_looking",
     "hint": "在那之前，你有沒有偶爾想過這件事，但還沒認真找解法？當時腦中閃過什麼念頭？"},
    {"round": 3, "intent": "alternatives_considered_and_rejected",
     "hint": "你當時考慮過哪些其他做法/工具/服務？為什麼最後沒選它們？"},
    {"round": 4, "intent": "struggling_moment",
     "hint": "整個過程中有沒有哪個瞬間讓你特別卡關、猶豫、甚至想放棄？"},
    {"round": 5, "intent": "ongoing_satisfaction",
     "hint": "你現在的做法讓你滿意嗎？還會想換嗎？"},
]


def generate_switch_followup_question(persona: dict, prior_turns: List[dict], round_i: int) -> str:
    """取代 stage12 的 generate_followup_question()——呼叫形狀一樣（餵
    先前逐字稿、拿到下一句問句），但每一輪的「意圖」是固定的（JTBD
    switch 訪談的五個標準元素，見 SWITCH_INTERVIEW_ROUND_INTENTS），不是
    自由不斷往下問「為什麼」。固定意圖比自由連續追問更可靠地把這些具體
    元素問出來，同時仍讓 LLM 依上一輪的實際回答動態措辭這一輪的問句
    （不是逐字照搬 hint 文字）。"""
    last_answer = prior_turns[-1]["answer"]
    history = "\n".join(f"Q: {t['question']}\nA: {t['answer']}" for t in prior_turns)
    round_spec = next(
        (r for r in SWITCH_INTERVIEW_ROUND_INTENTS if r["round"] == round_i),
        SWITCH_INTERVIEW_ROUND_INTENTS[-1],
    )
    system = (
        f"你是 {persona['name']}，正在做用戶需求訪談（JTBD switch 訪談，"
        "探索階段，不能提任何點子/解法）。這一輪的訪談意圖固定是："
        f"「{round_spec['intent']}」——{round_spec['hint']}"
        "請根據對方剛剛的回答，把這個意圖轉換成一句貼合對話脈絡的具體"
        "問句，不要逐字照搬提示文字，也不要偏離這一輪的意圖去問別的層次。"
        "只輸出問題本身（<=30 字），不要加解說或引號。"
    )
    prompt = f"訪談記錄：\n{history}\n\n對方剛回答：{last_answer}\n\n這一輪要問的問句？"
    question = call_llm(SMART_MODEL, system, prompt, max_tokens=150).strip()
    return question or round_spec["hint"]


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
    topic: str
    company: str
    round_id: str
    # ---- Discover（發散）----
    # desk_research_hypothesize_jobs 產出：五力+趨勢分析 → 2-4 個 JTBD
    # 候選 job（刻意不含任何解法/產品命名），各自帶一組小規模訪談對象
    five_forces: dict
    trend_analysis: str
    candidate_jobs: List[dict]
    used_fallback_candidate_jobs: bool
    research_queries: List[str]
    research_items: List[dict]
    # discover_and_evaluate_jobs 產出：每個候選 job 平行完整訪談，逐字稿
    # 全部匯總，加上每個候選 job 各自一筆的證據萃取（job_evidence）
    interview_transcript: Annotated[List[dict], operator.add]
    job_evidence: Annotated[List[dict], operator.add]
    # ---- Define（收斂：定義問題，不定義解法）----
    # select_job_and_define_problem 產出：從全部候選 job 的證據裡選一個
    # job，定義出 target_audience／problem_statement／hmw，並把雀屏中選
    # 那個 job 的洞見留給 Develop 階段的 draft_one_idea 引用
    selected_job_id: str
    selected_job: dict
    target_audience: str
    problem_statement: str
    hmw: str
    insights: List[dict]
    # ---- Develop（發散：解法）----
    # assemble_persona_team 產出：扣著公司實際能力衍生職能的腦力激盪參與者
    personas: List[dict]
    used_fallback_personas: bool
    # draft_ideas 產出：N 位 persona 各自獨立發想 1 個 idea，各自帶自己
    # 設計的 bmc（不互評、不自己改，BMC 不是共用一份）
    ideas: Annotated[List[dict], operator.add]
    # ask_question（HITL，僅保留的互動環節）
    pending_question: Optional[str]
    pending_question_target_idea_id: Optional[str]
    pending_question_asked_by: Optional[str]
    human_qa_log: Annotated[List[dict], operator.add]
    # ---- Deliver（收斂：定案）----
    # dfv_scoring 產出：3 面向 lens × N 個 idea 的評分
    dfv_scores: Annotated[List[dict], operator.add]
    # pick_winner 產出：加總選出的最終 idea
    winner_idea: dict
    idea_diversity: dict
    # generate_prototype 產出：單次生成，不修正
    prototype: dict
    # generate_evaluators 產出：動態生成、跟全部候選 job 訪談對象都不重複
    # 的最終評估者
    evaluators: List[dict]
    used_fallback_evaluators: bool
    # 注意：baseline_proposal／user_evaluation／final_verdict 不在這個
    # state 裡——它們不是圖節點的產出，是 main() 在 run_meeting() 跑完
    # 之後才計算的（見 build_parent_graph() 的 docstring 解釋原因）。


def desk_research_hypothesize_jobs(state: MeetingState) -> dict:
    """Discover 階段起點，取代 stage12 的 `analyze_and_scope()`。

    stage12 的根因問題：`analyze_and_scope()` 在任何訪談發生之前就把
    問題收斂成一個具體、常常連解法/產品命名都定了的 `strategic_goal`，
    後面不管訪談做得多深、persona 各自設計多獨立的 BMC，所有人都只能
    在同一個已選定的框架裡精修，這是真實跑測量到多樣性偏低
    （avg_distance 0.2382/0.2537/0.2821，全部低於「換句話說」基準值
    0.579）的上游根因。

    這裡改成只做 JTBD 意義下的「假設」，不做「選擇」：一次 LLM 呼叫，
    輸出五力分析＋趨勢分析（形狀跟 stage12 一樣）＋ 2-4 個候選 job
    （`candidate_jobs`），prompt 明確禁止提出任何解法/機制/產品命名，
    只描述情境／想達成的進展／現有替代方案為什麼不滿意。每個候選 job
    各自帶一組小規模訪談對象（`interview_pool`），交給
    `discover_and_evaluate_jobs` 平行訪談驗證，選哪一個 job 是
    `select_job_and_define_problem`（Define 收斂）的事，不是這裡的事。"""
    topic = state["topic"]
    company = state["company"]
    role_token = _event_role.set("problem_analysis")
    try:
        queries = [
            f"{topic} 產業 趨勢 2026",
            f"{topic} 科技 環境 人口結構 世代 變化",
            f"{topic} 競爭者 替代方案",
        ]
        raw: List[dict] = []
        for q in queries:
            try:
                hits = web_search(q, max_results=4)
                usable = [hit for hit in hits if is_usable_search_result(hit)]
                raw.extend(usable)
                print(f"  [desk_research_hypothesize_jobs] query={q!r} → {len(usable)}/{len(hits)} 筆可用")
            except Exception as exc:  # noqa: BLE001
                print(f"  [desk_research_hypothesize_jobs] query={q!r} 失敗：{exc}")
        sources_block = "\n".join(
            f"- {it.get('title')} | {it.get('url')}\n  {it.get('snippet', '')[:200]}" for it in raw
        ) or "（無搜尋結果）"

        system = (
            "你是 JTBD（Jobs-to-be-Done）研究員。用 Porter 五力分析＋趨勢"
            "分析（科技/環境/人口結構/世代價值觀變化）針對這家公司分析這個"
            f"主題，接著只做「假設」不做「選擇」：提出 {N_CANDIDATE_JOBS} 個"
            "彼此明顯不同的候選 job（顧客想在什麼情境下達成什麼進展、為什麼"
            "現有替代方案不滿意）。這一步嚴禁提出任何解法/機制/功能/產品"
            "命名——job 陳述只能描述情境與想要的進展，不能講「用什麼方式」"
            "達成。每個候選 job 各自搭配一組具體的訪談對象（終端使用者，"
            f"不是公司內部人員），恰好 {N_INTERVIEWEES_PER_JOB} 位。只輸出"
            "JSON：{"
            "\"five_forces\":{\"新進入者威脅\":\"<=60字\",\"替代品威脅\":\"<=60字\","
            "\"顧客議價力\":\"<=60字\",\"供應商議價力\":\"<=60字\",\"現有競爭者強度\":\"<=60字\"},"
            "\"trend_analysis\":\"<=200字，涵蓋科技/環境/人口結構/世代價值觀變化中跟主題最相關的部分\","
            "\"candidate_jobs\":["
            "{\"id\":\"job1\",\"job_statement\":\"<=60字，情境+想達成的進展，不能出現解法\","
            "\"hypothesis_rationale\":\"<=80字，為什麼值得假設這個 job 是真的、值得訪談驗證\","
            "\"interview_pool\":[{\"id\":\"u1\",\"name\":\"...\",\"age\":數字,\"context\":\"<=60字\","
            "\"pain_points\":[\"...\",\"...\"],\"tone\":\"<=20字\"}]"
            f"，恰好 {N_INTERVIEWEES_PER_JOB} 位}}"
            f"]，恰好 {N_CANDIDATE_JOBS} 個候選 job，彼此的情境/進展要明顯不同}}"
        )
        user = f"主題：{topic}\n\n公司定位：\n{company}\n\n搜尋素材：\n{sources_block}"
        raw_text = call_llm(SMART_MODEL, system, user, max_tokens=3000)
        data = extract_json_object(raw_text)
        if not data:
            data = extract_json_object(repair_json_text(raw_text))
        data = data or {}

        five_forces = data.get("five_forces")
        trend_analysis = _safe_str(data.get("trend_analysis"))
        candidate_jobs_raw = [
            cj for cj in (data.get("candidate_jobs") or [])
            if isinstance(cj, dict) and _safe_str(cj.get("job_statement"))
        ][:N_CANDIDATE_JOBS]

        candidate_jobs: List[dict] = []
        for n, cj in enumerate(candidate_jobs_raw, 1):
            pool = [
                p for p in (cj.get("interview_pool") or [])
                if isinstance(p, dict) and _safe_str(p.get("name"))
            ][:N_INTERVIEWEES_PER_JOB]
            candidate_jobs.append({
                "id": _safe_str(cj.get("id")) or f"job{n}",
                "job_statement": _safe_str(cj.get("job_statement")),
                "hypothesis_rationale": _safe_str(cj.get("hypothesis_rationale")),
                "interview_pool": pool,
            })

        used_fallback = False
        if len(candidate_jobs) < 2:
            # 解析失敗或候選 job 數明顯不足——退回保底候選 job，各自從
            # load_users() 分配訪談對象，不讓整場會議因為這步失敗跑不下去。
            fallback_users = load_users()
            candidate_jobs = []
            for n in range(1, N_CANDIDATE_JOBS + 1):
                start = (n - 1) * N_INTERVIEWEES_PER_JOB
                pool = fallback_users[start:start + N_INTERVIEWEES_PER_JOB] or fallback_users[:N_INTERVIEWEES_PER_JOB]
                candidate_jobs.append({
                    "id": f"job{n}",
                    "job_statement": f"（系統保底）「{topic}」情境下的通用需求假設 {n}",
                    "hypothesis_rationale": "（系統保底）分析未產生具體內容。",
                    "interview_pool": pool,
                })
            used_fallback = True
        for cj in candidate_jobs:
            if len(cj["interview_pool"]) < 1:
                cj["interview_pool"] = load_users()[:N_INTERVIEWEES_PER_JOB]

        # 真實跑測發現的坑：LLM 幫每個候選 job 各自生成訪談對象時，id 是
        # 各自從 u1/u2 起算（fallback 分支也可能因為 load_users() 樣本不夠
        # 而讓不同 job 分到同一批 id）——同一個 id 在不同候選 job 之間會
        # 撞號，即時畫面／回放頁的 findUser(id) 用 .find() 找第一個符合的，
        # 撞號時只會顯示錯的那一位受訪者的人物設定。這裡在全部候選 job
        # 底定後做一次全域重新編號，保證整場會議的訪談對象 id 不重複。
        _interviewee_seq = 0
        for cj in candidate_jobs:
            renumbered = []
            for person in cj["interview_pool"]:
                _interviewee_seq += 1
                renumbered.append({**person, "id": f"u{_interviewee_seq}"})
            cj["interview_pool"] = renumbered

        if not isinstance(five_forces, dict) or not five_forces:
            five_forces = {k: "（系統保底）分析未產生具體內容" for k in
                            ["新進入者威脅", "替代品威脅", "顧客議價力", "供應商議價力", "現有競爭者強度"]}
        if not trend_analysis:
            trend_analysis = "（系統保底）分析未產生具體內容。"

        print(
            f"  [desk_research_hypothesize_jobs] {len(candidate_jobs)} 個候選 job"
            f"（fallback={used_fallback}）：{[cj['job_statement'] for cj in candidate_jobs]}"
        )
        emit_event(
            "desk_research_hypothesize_jobs",
            f"假設 {len(candidate_jobs)} 個候選 job",
            role="problem_analysis",
            extra={
                "five_forces": five_forces,
                "trend_analysis": trend_analysis,
                "candidate_jobs": candidate_jobs,
                "queries": queries,
                "n_results": len(raw),
                "used_fallback_candidate_jobs": used_fallback,
            },
        )
    finally:
        _event_role.reset(role_token)
    return {
        "five_forces": five_forces,
        "trend_analysis": trend_analysis,
        "candidate_jobs": candidate_jobs,
        "used_fallback_candidate_jobs": used_fallback,
        "research_queries": queries,
        "research_items": raw,
    }


# ---------------------------------------------------------------------------
# Discover：對每個候選 job 平行做完整的 JTBD switch 訪談
# ---------------------------------------------------------------------------

class IntervieweeTask(TypedDict):
    interviewee: dict
    topic: str
    candidate_job_statement: str


class InterviewPanelState(TypedDict):
    topic: str
    candidate_job_statement: str
    interviewees: List[dict]
    interview_transcript: Annotated[List[dict], operator.add]


def fan_out_interviews(state: InterviewPanelState) -> List[Send]:
    return [
        Send("interview_one_person", {
            "interviewee": person, "topic": state["topic"],
            "candidate_job_statement": state["candidate_job_statement"],
        })
        for person in state["interviewees"]
    ]


def interview_one_person(task: IntervieweeTask) -> dict:
    """訪談者身份是「系統研究員」，不是某位 persona，同一個候選 job 底下
    的訪談對象平行 fan-out。訪談技巧改用 JTBD 的「switch」訪談（見
    `generate_switch_followup_question`）：回溯式重建一次真實的「轉換／
    採取行動」決策時間軸，用來檢驗這個候選 job 假設是不是真的，不是
    stage12 的 5-Whys（深挖已知症狀的根因，前提是已經知道要往哪裡挖）
    ——這是不同形狀的問題，不是深度差異。"""
    interviewee = task["interviewee"]
    role_token = _event_role.set(f"user:{interviewee.get('name')}")
    transcript: List[dict] = []
    try:
        interviewer_persona = {"name": "系統研究員", "role": "需求研究"}
        first_question = (
            f"關於「{task['candidate_job_statement']}」這種情境，能不能先聊聊"
            "你上一次真的遇到這種情況是什麼時候？"
        )
        for round_i in range(1, SWITCH_INTERVIEW_ROUNDS + 1):
            question = (
                first_question if round_i == 1
                else generate_switch_followup_question(interviewer_persona, transcript, round_i)
            )
            answer = simulate_user_answer(interviewee, question, transcript)
            turn = {
                "user_id": interviewee.get("id"), "user_name": interviewee.get("name"),
                "round": round_i, "question": question, "answer": answer,
            }
            transcript.append(turn)
            emit_event(
                "interview_turn", f"訪談 {interviewee.get('name')} 第{round_i}輪：{question}",
                role=f"user:{interviewee.get('name')}", extra=turn,
            )
        print(f"  [research_one_candidate_job:interview] {interviewee.get('name')} 完成 {SWITCH_INTERVIEW_ROUNDS} 輪")
    finally:
        _event_role.reset(role_token)
    return {"interview_transcript": transcript}


def build_interview_panel_subgraph():
    g = StateGraph(InterviewPanelState)
    g.add_node("interview_one_person", instrument("interview_one_person", interview_one_person))
    g.add_conditional_edges(START, fan_out_interviews, ["interview_one_person"])
    g.add_edge("interview_one_person", END)
    return g.compile()


interview_panel_graph = build_interview_panel_subgraph()


class CandidateJobTask(TypedDict):
    candidate_job: dict
    topic: str


class CandidateJobPanelState(TypedDict):
    topic: str
    candidate_jobs: List[dict]
    job_evidence: Annotated[List[dict], operator.add]
    interview_transcript: Annotated[List[dict], operator.add]


def fan_out_candidate_jobs(state: CandidateJobPanelState) -> List[Send]:
    return [
        Send("research_one_candidate_job", {"candidate_job": cj, "topic": state["topic"]})
        for cj in state["candidate_jobs"]
    ]


def _synthesize_job_evidence(candidate_job: dict, transcript: List[dict]) -> dict:
    lines = "\n".join(f"[{t['user_name']}] Q:{t['question']} / A:{t['answer']}" for t in transcript)
    system = (
        "你是 JTBD 研究員，剛完成一個候選 job 的 switch 訪談，現在要誠實"
        "評估這個候選 job 假設站不站得住腳，並萃取洞見給後續使用（不管這個"
        "job 最後有沒有雀屏中選，這份證據都要能被稽核）。只輸出 JSON：{"
        "\"supported\": true/false（根據逐字稿，這個候選 job 是不是真實、"
        "未被滿足的需求）,"
        "\"evidence_summary\":\"<=80字，從逐字稿具體回溯的判斷依據\","
        "\"insights\": [{\"id\":\"i1\",\"text\":\"一句話洞見，<=50字\"}]"
        "（最多 5 則，每則必須具體可回溯到某位受訪者說的話，不要空泛通則）}"
    )
    user = f"候選 job：{candidate_job['job_statement']}\n\n完整逐字稿：\n{lines}"
    raw = call_llm(SMART_MODEL, system, user, max_tokens=2000)
    data = extract_json_object(raw)
    if not data:
        data = extract_json_object(repair_json_text(raw))
    data = data or {}

    raw_insights = [
        i for i in (data.get("insights") or [])
        if isinstance(i, dict) and (i.get("text") or "").strip()
    ]
    if not raw_insights:
        raw_insights = [{"text": f"{t['user_name']}：{t['answer'][:50]}"} for t in transcript[:2]]
    insights = [{"id": f"i{n}", "text": it["text"].strip()} for n, it in enumerate(raw_insights, 1)]

    return {
        "job_id": candidate_job["id"],
        "job_statement": candidate_job["job_statement"],
        "supported": bool(data.get("supported", True)),
        "evidence_summary": _safe_str(data.get("evidence_summary")) or "（系統保底）分析未產生具體內容。",
        "insights": insights,
    }


def research_one_candidate_job(task: CandidateJobTask) -> dict:
    """對一個候選 job 做完整訪談＋證據萃取——內部同步呼叫既有的
    `interview_panel_graph.invoke()`（模式 B，沿用 stage12 的
    `interview_panel_graph`/`fan_out_interviews`/`interview_one_person`/
    `build_interview_panel_subgraph` 原封不動，只是欄位改名成
    `candidate_job_statement`），保證這個候選 job 底下所有訪談對象都
    訪談完才返回。這個節點本身又是被 `discover_and_evaluate_jobs` 用
    `Send()` 平行呼叫的（模式 A）——四層巢狀 fan-out/fan-in，每一層都是
    模式 A 或 B 的直接重複，沒有發明新的 join 原語。"""
    candidate_job = task["candidate_job"]
    result = interview_panel_graph.invoke({
        "topic": task["topic"],
        "candidate_job_statement": candidate_job["job_statement"],
        "interviewees": candidate_job["interview_pool"],
        "interview_transcript": [],
    })
    transcript = result["interview_transcript"]
    for turn in transcript:
        turn["candidate_job_id"] = candidate_job["id"]
    evidence = _synthesize_job_evidence(candidate_job, transcript)
    emit_event(
        "research_one_candidate_job",
        f"候選 job「{candidate_job['job_statement']}」訪談完成，"
        f"supported={evidence['supported']}",
        extra={"candidate_job": candidate_job, "evidence": evidence},
    )
    print(f"  [research_one_candidate_job] {candidate_job['job_statement']}：supported={evidence['supported']}")
    return {"job_evidence": [evidence], "interview_transcript": transcript}


def build_candidate_job_panel_subgraph():
    g = StateGraph(CandidateJobPanelState)
    g.add_node("research_one_candidate_job", instrument("research_one_candidate_job", research_one_candidate_job))
    g.add_conditional_edges(START, fan_out_candidate_jobs, ["research_one_candidate_job"])
    g.add_edge("research_one_candidate_job", END)
    return g.compile()


candidate_job_panel_graph = build_candidate_job_panel_subgraph()


def discover_and_evaluate_jobs(state: MeetingState) -> dict:
    """Discover 階段的 fan-in：單一節點同步呼叫
    `candidate_job_panel_graph.invoke()`（模式 B），保證全部候選 job 各自
    的訪談都跑完才返回，再交給 `select_job_and_define_problem`（Define
    收斂，單一前驅，沒有 join 風險）。"""
    result = candidate_job_panel_graph.invoke({
        "topic": state["topic"],
        "candidate_jobs": state["candidate_jobs"],
        "job_evidence": [],
        "interview_transcript": [],
    })
    return {
        "job_evidence": result["job_evidence"],
        "interview_transcript": result["interview_transcript"],
    }


def select_job_and_define_problem(state: MeetingState) -> dict:
    """Define 收斂：把兩個菱形的分界線畫在這裡——這一步只定義「問題是
    什麼」，禁止提出任何解法。輸入全部候選 job 的證據
    （`job_evidence`，含每個 job 的 supported/evidence_summary/insights），
    一次 LLM 呼叫選出一個 job（`why_selected` 必須回溯具體證據，不是空泛
    判斷），定義 `target_audience`／`problem_statement`／`hmw`。雀屏中選
    那個 job 的洞見留給 `assemble_persona_team` 之後的 `draft_one_idea`
    引用；落選的候選 job 證據仍然留在 `job_evidence` 裡，方便稽核 Define
    階段的決策，不是只看得到贏家。"""
    candidate_jobs = state["candidate_jobs"]
    job_evidence = state["job_evidence"]
    evidence_block = "\n".join(
        f"- [{ev['job_id']}] {ev['job_statement']}\n"
        f"  supported={ev['supported']}，依據：{ev['evidence_summary']}\n"
        f"  洞見：{'; '.join(i['text'] for i in ev['insights'])}"
        for ev in job_evidence
    ) or "（無證據）"
    system = (
        "你是產品策略顧問，Double Diamond 的 Define 階段：只定義問題，"
        "嚴禁提出任何解法/機制/產品命名。根據每個候選 job 的訪談證據，"
        "選出最真實、最未被滿足的一個 job，說明依據哪些具體證據選它"
        "（不能是空泛判斷）。定義出這個 job 對應的 target_audience"
        "（終端使用者輪廓），寫一份解法無關的 problem_statement（POV 風格："
        "情境+使用者+未被滿足的需求+為什麼現狀不滿意），以及一句 How Might"
        "We（hmw，銜接後續發想，但本身也不能點名解法）。只輸出 JSON：{"
        "\"selected_job_id\":\"...\",\"why_selected\":\"<=100字，引用具體證據\","
        "\"target_audience\":\"<=80字\",\"problem_statement\":\"<=150字，"
        "不能出現解法/產品名稱\",\"hmw\":\"<=40字，How Might We 開頭\"}"
    )
    user = f"候選 job 與證據：\n{evidence_block}"
    raw = call_llm(SMART_MODEL, system, user, max_tokens=1500)
    data = extract_json_object(raw)
    if not data:
        data = extract_json_object(repair_json_text(raw))
    data = data or {}

    evidence_by_id = {ev["job_id"]: ev for ev in job_evidence}
    selected_job_id = _safe_str(data.get("selected_job_id"))
    if selected_job_id not in evidence_by_id:
        # 解析失敗或選到不存在的 job id——保底選 supported=True 的第一個，
        # 否則選第一個候選 job，不讓整場會議因為這步失敗跑不下去。
        supported_first = next((ev for ev in job_evidence if ev.get("supported")), None)
        selected_job_id = (supported_first or job_evidence[0])["job_id"] if job_evidence else candidate_jobs[0]["id"]

    why_selected = _safe_str(data.get("why_selected")) or "（系統保底）解析失敗，退回保底選擇。"
    target_audience = _safe_str(data.get("target_audience")) or "（系統保底）一般終端使用者。"
    problem_statement = _safe_str(data.get("problem_statement"))
    hmw = _safe_str(data.get("hmw"))

    selected_job_full = next((cj for cj in candidate_jobs if cj["id"] == selected_job_id), candidate_jobs[0])
    selected_evidence = evidence_by_id.get(selected_job_id, job_evidence[0] if job_evidence else {"insights": []})
    if not problem_statement:
        problem_statement = f"（系統保底）{selected_job_full['job_statement']}"
    if not hmw:
        hmw = f"（系統保底）How Might We 讓 {target_audience} 達成：{selected_job_full['job_statement']}"

    selected_job = {
        "id": selected_job_id,
        "job_statement": selected_job_full["job_statement"],
        "why_selected": why_selected,
    }
    insights = selected_evidence.get("insights", [])

    print(f"  [select_job_and_define_problem] 選定 {selected_job_id}：{problem_statement}")
    emit_event(
        "select_job_and_define_problem",
        f"選定候選 job {selected_job_id}，problem_statement：{problem_statement}",
        extra={
            "selected_job_id": selected_job_id,
            "selected_job": selected_job,
            "target_audience": target_audience,
            "problem_statement": problem_statement,
            "hmw": hmw,
            "candidate_jobs": candidate_jobs,
            "job_evidence": job_evidence,
        },
    )
    return {
        "selected_job_id": selected_job_id,
        "selected_job": selected_job,
        "target_audience": target_audience,
        "problem_statement": problem_statement,
        "hmw": hmw,
        "insights": insights,
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


def derive_company_domains(company: str, problem_statement: str, hmw: str) -> tuple[List[str], bool]:
    """回傳 (domains, used_fallback)。讓每位腦力激盪參與者的職能都扣著
    「這家公司真的有的能力」，而不是跟公司八竿子打不著的任意領域——這樣
    發想出來的 idea 才有機會用公司現有的人/技術/資源真的做出來，不是空談。
    仍然要求彼此明顯不同（同一份 prompt 裡明講：不要全部落在行銷/內容這種
    表面領域），避免又退回「LLM 自己判斷互補團隊」時常見的同質化問題。"""
    system = (
        "你是組織設計顧問。仔細讀這家公司的定位/資源/能力描述（部門、"
        "技術、內容素材、通路、既有業務關係等），列出這家公司內部或能"
        f"直接調度的資源裡，恰好 {N_PERSONAS} 個彼此明顯不同的職能／專業"
        "角度——目的是組一支跨職能腦力激盪小組，讓每位成員都從公司真正"
        "具備的某種能力出發思考這個 How Might We，而不是天馬行空地假設"
        "公司做得到某件事。嚴禁全部落在同一個大領域的不同分工（例如都是"
        "「社群行銷」「內容企劃」「品牌公關」這種同一種能力的變體）——"
        "要橫跨這家公司真正擁有的不同種類能力（例如內容製作技術、工程/"
        "資料、通路/業務關係、既有異業合作、會員營運等，實際個數與內容"
        "依這家公司的真實描述決定，不要套用這個例子）。只輸出 JSON：{"
        "\"domains\":[\"...\"]}，字串要具體到看得出是這家公司的哪個實際"
        f"能力，不要用空泛通稱，恰好 {N_PERSONAS} 個"
    )
    user = f"公司定位／資源／能力：\n{company}\n\nHow Might We：{hmw}\n\n問題陳述：{problem_statement}"
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
    problem_statement: str
    hmw: str
    target_audience: str
    idx: int


class PersonaTeamPanelState(TypedDict):
    domains: List[str]
    problem_statement: str
    hmw: str
    target_audience: str
    personas: Annotated[List[dict], operator.add]


def fan_out_persona_domains(state: PersonaTeamPanelState) -> List[Send]:
    return [
        Send("generate_one_persona_for_domain", {
            "domain": domain,
            "problem_statement": state["problem_statement"],
            "hmw": state["hmw"],
            "target_audience": state["target_audience"],
            "idx": idx,
        })
        for idx, domain in enumerate(state["domains"], 1)
    ]


def generate_one_persona_for_domain(task: PersonaDomainTask) -> dict:
    """每位參與者各自獨立一次 LLM 呼叫生成（不是像 stage12 那樣一次呼叫
    生成全部 N 位）——獨立呼叫才不會讓角色彼此的措辭/選擇互相關聯。domain
    是 `derive_company_domains()` 從公司實際能力衍生出來的職能（不是跟
    公司無關的任意領域），這裡的參與者是公司內部/可調度資源裡真的具備
    這個職能的人，這樣他發想的 idea 才有機會用公司現有的東西做出來。"""
    system = (
        f"你是組織設計顧問。針對這個 How Might We，設計一位背景是"
        f"「{task['domain']}」的腦力激盪參與者——這是這家公司內部或可"
        "直接調度的一種實際職能，這位參與者要真的具備這個職能的專業，"
        "他會用自己職能的思維方式去理解這個問題、並提出這家公司真的做"
        "得到的方案，不是天馬行空假設公司有他其實沒有的能力。只輸出 "
        "JSON：{\"name\":\"...\",\"role\":\"<=20字\",\"background\":\"<=60字，"
        "明確扣住指定的職能\",\"focus\":[\"...\",\"...\"],\"style\":\"<=20字\"}"
    )
    user = (
        f"專業領域：{task['domain']}\n\nHow Might We：{task['hmw']}\n\n"
        f"問題陳述：{task['problem_statement']}\n\ntarget audience：{task['target_audience']}"
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


def assemble_persona_team(state: MeetingState) -> dict:
    """Develop 起點。跟 `select_job_and_define_problem` 是嚴格單一前驅鏈
    （不是像 stage12 `system_research`／`generate_personas` 那樣兩個互不
    相依卻要同時完成的平行分支），所以這個接縫本身就不是 join，不需要
    「折成一個節點依序呼叫」的技巧——這是 JTBD 重排序的額外好處，直接
    消除了一整類 join 風險。

    domain 的來源改成 `derive_company_domains()`（從 `company.md` 衍生
    出這家公司實際具備的職能），不是跟公司無關的 `DOMAIN_ARCHETYPE_POOL`
    ——使用者在真實驗證後指出跨領域抽樣雖然拉開了多樣性，但那些領域跟
    公司無關，發想出來的方案很可能公司根本沒能力做出來。"""
    domains, used_fallback_domains = derive_company_domains(
        state["company"], state["problem_statement"], state["hmw"],
    )
    result = persona_team_panel_graph.invoke({
        "domains": domains,
        "problem_statement": state["problem_statement"],
        "hmw": state["hmw"],
        "target_audience": state["target_audience"],
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

class IdeaTask(TypedDict):
    persona: dict
    problem_statement: str
    hmw: str
    target_audience: str
    insights: List[dict]
    research_items: List[dict]


def fan_out_ideas(state: MeetingState) -> List[Send]:
    return [
        Send("draft_one_idea", {
            "persona": persona,
            "problem_statement": state["problem_statement"],
            "hmw": state["hmw"],
            "target_audience": state["target_audience"],
            "insights": state["insights"],
            "research_items": state["research_items"],
        })
        for persona in state["personas"]
    ]


_IDEA_SCHEMA_HINT = f"""
只輸出一個 JSON 物件（不要 markdown 圍欄），欄位：
- title: string（<=40字）
- summary: string（2-3句，<=120字）
- rationale: string（<=80字，說明這個 idea 怎麼呼應問題陳述跟訪談洞見）
- insight_refs: [string] 1-3 筆，必須是提供的洞見 id（例如 "i1"），不能捏造
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
    data.setdefault("insight_refs", [])
    return data


def draft_one_idea(task: IdeaTask) -> dict:
    """使用者要求刪掉互評/自己改（第 8 點）——每位 persona 獨立發想一個
    idea 就結束，不會再被自己或別人修改。

    BMC 現在是每位 persona 自己設計的一份，不是共用範本（真實跑測發現：
    全場共用一份 BMC 會把商業模式先框死，所有人只能在同一個框架裡想
    變化，導致三個 idea 的多樣性量出來比「同一個 idea 換句話說」還低，
    見 note.md）——這裡跟 idea 本身一起、同一次 LLM 呼叫產生，不多花
    一次呼叫的成本，用 `_merge_bmc()` 保證結構合法（缺漏格用安全預設值
    補，跟 stage9 對每位 persona 提案的既有慣例一致）。"""
    persona = task["persona"]
    role_token = _event_role.set(_persona_label(persona))
    try:
        insights_block = "\n".join(f"- [{i['id']}] {i['text']}" for i in task["insights"]) or "（無）"
        sources_block = "\n".join(
            f"- {it.get('title')} | {it.get('url')}" for it in task["research_items"][:8]
        ) or "（無）"
        system = (
            f"你是 {persona['name']}（{persona.get('role', '')}），背景：{persona.get('background', '')}。"
            f"關注：{', '.join(persona.get('focus') or [])}。發言風格：{persona.get('style', '')}。"
            "獨立發想一個能回應這個 How Might We 的具體 idea，並自己設計一份"
            "支撐這個 idea 的 Business Model Canvas——這是你自己的提案跟你"
            "自己的商業模式判斷，不用參考其他人的想法（現在還沒有其他人的"
            "提案，也沒有範本 BMC 可以照抄）。必須明確呼應提供的訪談洞見，"
            "insight_refs 只能引用下面列出的洞見 id，url 只能來自提供的"
            "搜尋素材，不能捏造連結。"
            + _IDEA_SCHEMA_HINT
        )
        user = (
            f"How Might We：{task['hmw']}\n\n問題陳述：{task['problem_statement']}\n\n"
            f"target audience：{task['target_audience']}\n\n"
            f"可引用的訪談洞見：\n{insights_block}\n\n"
            f"可用搜尋素材：\n{sources_block}"
        )
        raw = call_llm(SMART_MODEL, system, user, max_tokens=1600)
        idea = _parse_idea(raw)
        known_ids = {i.get("id") for i in task["insights"] if i.get("id")}
        refs = [r for r in (idea.get("insight_refs") or []) if r in known_ids]
        if not refs and task["insights"]:
            refs = [task["insights"][0]["id"]]
        idea["insight_refs"] = refs
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


def route_after_question(state: MeetingState) -> Literal["answer_question", "dfv_scoring"]:
    return "answer_question" if state.get("pending_question") else "dfv_scoring"


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
# DFV（Desirability/Feasibility/Viability）結構化評分收斂
# ---------------------------------------------------------------------------

class DfvTask(TypedDict):
    lens: dict
    idea: dict
    problem_statement: str


class DfvPanelState(TypedDict):
    ideas: List[dict]
    problem_statement: str
    dfv_scores: Annotated[List[dict], operator.add]


def fan_out_dfv(state: DfvPanelState) -> List[Send]:
    """使用者要求把三位大師的簡短點評改成 DFV 三面向結構化評分（第 9
    點）——3 個面向 × N 個 idea 的雙迴圈，跟 stage9 `fan_out_three_lens`
    是同一個形狀，只是這裡每次呼叫只問一個面向、給一個分數，不是
    正面/負面/洞見各 3 則。"""
    return [
        Send("score_one_dimension", {
            "lens": lens, "idea": idea, "problem_statement": state["problem_statement"],
        })
        for lens in DFV_LENSES
        for idea in state["ideas"]
    ]


def score_one_dimension(task: DfvTask) -> dict:
    lens = task["lens"]
    idea = task["idea"]
    role_token = _event_role.set(f"dfv:{lens['name']}")
    system = (
        f"你是{lens['name']}，只用「{lens['angle']}」這個角度評估這個 idea，"
        "不要評論其他面向。只輸出 JSON：{\"score\": 0-10 的數字（可以有"
        "小數），\"critique\": \"具體、有憑有據的一大段批評，講清楚為什麼"
        "給這個分數，不要空泛通則，<=200字\"}"
    )
    user = (
        f"問題陳述：{task['problem_statement']}\n\n"
        f"idea：《{idea.get('title')}》{idea.get('summary')}\n理由：{idea.get('rationale')}"
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


def dfv_scoring(state: MeetingState) -> dict:
    result = dfv_panel_graph.invoke({
        "ideas": state["ideas"], "problem_statement": state["problem_statement"], "dfv_scores": [],
    })
    scores = result["dfv_scores"]
    print(f"  [dfv_scoring] {len(scores)} 筆評分（{len(DFV_LENSES)} 面向 × {len(state['ideas'])} 個 idea）")
    return {"dfv_scores": scores}


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
    """使用者要求最終評估者跟訪談對象是同一個 target audience，但不能
    重複個體——生成時明確排除全部候選 job（不只是雀屏中選那個）訪談過
    的姓名聯集，因為這場會議裡所有候選 job 訪談過的人都是模擬 persona，
    不該讓其中任何一位又兼任盲評評估者。解析失敗或人數不足時退回
    `load_users()`（同樣排除已訪談過的名字）。"""
    target_audience = state["target_audience"]
    interviewee_names = {
        person.get("name")
        for cj in state["candidate_jobs"]
        for person in cj.get("interview_pool", [])
    }
    system = (
        f"你是使用者研究員。針對這個 target audience，額外生成 {N_EVALUATORS} "
        "位不同於已訪談過的具體人物，用來最後評估一個產品原型——他們要在"
        "同一個 target audience 範圍內，但不能是以下已經訪談過的人。"
        "只輸出 JSON：{\"evaluators\":[{\"id\":\"e1\",\"name\":\"...\","
        "\"age\":數字,\"context\":\"<=60字\",\"pain_points\":[\"...\",\"...\"],"
        "\"tone\":\"<=20字\"}]}，恰好 " + str(N_EVALUATORS) + " 位"
    )
    user = (
        f"target audience：{target_audience}\n\n"
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
    candidate_jobs: List[dict],
    used_fallback_candidate_jobs: bool,
    job_evidence: List[dict],
    selected_job_id: str,
    selected_job: dict,
    target_audience: str,
    problem_statement: str,
    hmw: str,
    five_forces: dict,
    trend_analysis: str,
    interview_transcript: List[dict],
    insights: List[dict],
    personas: List[dict],
    used_fallback_personas: bool,
    ideas: List[dict],
    human_qa_log: List[dict],
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
        "candidate_jobs": candidate_jobs,
        "used_fallback_candidate_jobs": used_fallback_candidate_jobs,
        "job_evidence": job_evidence,
        "selected_job_id": selected_job_id,
        "selected_job": selected_job,
        "target_audience": target_audience,
        "problem_statement": problem_statement,
        "hmw": hmw,
        "five_forces": five_forces,
        "trend_analysis": trend_analysis,
        "interview_transcript": interview_transcript,
        "insights": insights,
        "personas": personas,
        "used_fallback_personas": used_fallback_personas,
        "ideas": ideas,
        "human_qa_log": human_qa_log,
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
    path = OUTPUT_DIR / f"stage13-run-{stamp}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUTPUT_DIR / f"stage13-latest-{topic[:12]}.json").write_text(
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
    candidate_jobs: List[dict],
    used_fallback_candidate_jobs: bool,
    job_evidence: List[dict],
    selected_job_id: str,
    selected_job: dict,
    target_audience: str,
    problem_statement: str,
    hmw: str,
    five_forces: dict,
    trend_analysis: str,
    interview_transcript: List[dict],
    insights: List[dict],
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
    interviewees = [person for cj in candidate_jobs for person in cj.get("interview_pool", [])]
    evidence_by_job_id = {ev["job_id"]: ev for ev in job_evidence}
    # 使用者要求報告裡重要的是「角色」，不是單純的人名——同一個名字對讀者
    # 沒有意義，只有搭配 persona 的專業角色／訪談對象的身份與情境，才看得
    # 出「這個 idea 是從什麼角度想出來的」「這個訪談洞見是誰在什麼處境下
    # 說的」。這裡建兩份 name -> 角色描述的對照表，讓後面每一處提到人名的
    # 地方都能順手帶出角色，不用每次重複寫查表邏輯。
    role_by_persona_name = {p.get("name"): p.get("role", "") for p in personas}

    def _person_role_label(person: dict) -> str:
        age = person.get("age")
        context = person.get("context", "")
        age_part = f"{age}歲，" if age else ""
        return f"{age_part}{context}" if context else age_part.rstrip("，")

    role_by_user_name = {u.get("name"): _person_role_label(u) for u in (list(interviewees) + list(evaluators))}

    def _persona_with_role(name: str) -> str:
        role = role_by_persona_name.get(name)
        return f"{name}（{role}）" if role else name

    def _user_with_role(name: str) -> str:
        role = role_by_user_name.get(name)
        return f"{name}（{role}）" if role else name

    L: List[str] = []
    L.append("# Double Diamond 腦力激盪最終報告\n")
    L.append(f"- **主題**：{topic}")
    L.append(f"- **Round ID**：{round_id}")
    L.append(f"- **腦力激盪參與者**：{'、'.join(_persona_with_role(p['name']) for p in personas)}")
    L.append(f"- **訪談對象**：{'、'.join(_user_with_role(i['name']) for i in interviewees)}")
    L.append(f"- **最終評估者**：{'、'.join(_user_with_role(e['name']) for e in evaluators)}\n")
    L.append("---\n")

    L.append("## Discover：候選 job 假設與五力／趨勢分析\n")
    if five_forces:
        L.append("**五力分析**：\n")
        for k, v in five_forces.items():
            L.append(f"- {k}：{v}")
        L.append("")
    L.append(f"**趨勢分析**：{trend_analysis}\n")
    if used_fallback_candidate_jobs:
        L.append("（候選 job 因分析解析失敗退回系統保底假設）\n")
    L.append(
        "以下列出**全部**候選 job 與各自的訪談證據——包含落選的，讓人可以"
        "稽核 Define 階段的決策，不是只看得到贏家：\n"
    )
    for cj in candidate_jobs:
        ev = evidence_by_job_id.get(cj["id"], {})
        mark = "✅ 雀屏中選" if cj["id"] == selected_job_id else "－ 未雀屏中選"
        L.append(f"### [{cj['id']}] {cj['job_statement']}（{mark}）\n")
        L.append(f"- 假設依據：{cj.get('hypothesis_rationale', '')}")
        L.append(f"- 訪談對象：{'、'.join(_user_with_role(p.get('name')) for p in cj.get('interview_pool', []))}")
        if ev:
            L.append(f"- supported：{ev.get('supported')}")
            L.append(f"- 證據摘要：{ev.get('evidence_summary', '')}")
            for i in ev.get("insights", []):
                L.append(f"  - [{i['id']}] {i['text']}")
        L.append("")
    L.append("---\n")

    L.append("## Define：問題定義（不含解法）\n")
    L.append(f"**選定的候選 job**：[{selected_job_id}] {selected_job.get('job_statement', '')}\n")
    L.append(f"**選定依據（why_selected）**：{selected_job.get('why_selected', '')}\n")
    L.append(f"**Target Audience**：{target_audience}\n")
    L.append(f"**Problem Statement**：{problem_statement}\n")
    L.append(f"**How Might We**：{hmw}\n")
    L.append("---\n")

    L.append("## 訪談逐字稿（JTBD switch 訪談，五輪固定意圖）\n")
    L.append("**受訪者側寫**（誰在什麼處境下說了這些話，比名字本身重要）：\n")
    for i in interviewees:
        pain = "、".join(i.get("pain_points") or [])
        L.append(f"- {_user_with_role(i.get('name'))}" + (f"，痛點：{pain}" if pain else ""))
    L.append("")
    L.append("每個候選 job 各自的訪談對象平行受訪，訪談技巧是 JTBD 的")
    L.append("「switch」訪談（回溯式重建一次真實的轉換／採取行動決策時間軸：")
    L.append("契機、被動觀望、考慮過的替代方案、卡關瞬間、現況滿意度），不是")
    L.append("stage12 的 5-Whys（深挖已知症狀根因）——這是不同形狀的問題，")
    L.append("不是深度差異。**BMC 不是這裡產生的共用範本**：每位參與者會依")
    L.append("雀屏中選那個 job 的洞見，各自設計自己的商業模式（見下一節）：\n")
    for t in interview_transcript:
        # 側寫已經在上面列過一次，這裡逐輪引用就不用每行重複整段情境
        # 描述，只留名字，避免每一輪都重複同一段文字造成閱讀疲勞。
        job_tag = f"[{t.get('candidate_job_id')}] " if t.get("candidate_job_id") else ""
        L.append(f"- {job_tag}[{t['user_name']} 第{t['round']}輪] Q：{t['question']} / A：{t['answer']}")
    L.append("")
    L.append("**萃取洞見（雀屏中選 job 的洞見，餵給下面的發想階段）**：\n")
    for i in insights:
        L.append(f"- [{i['id']}] {i['text']}")
    L.append("")

    L.append("## 腦力激盪參與者各自的 idea（各自設計自己的 BMC）\n")
    L.append("**參與者側寫**（各自帶著什麼專業角度發想，比名字本身重要）：\n")
    for p in personas:
        L.append(f"- {_persona_with_role(p.get('name'))}：{p.get('background', '')}")
    L.append("")
    for idea in ideas:
        L.append(f"### {_persona_with_role(idea.get('persona_name'))}：《{idea.get('title', '')}》")
        L.append(f"{idea.get('summary', '')}")
        L.append(f"- 理由：{idea.get('rationale', '')}\n")
        L.append("**這位參與者自己設計的 Business Model Canvas**：\n")
        for k in BMC_KEYS:
            L.append(f"- {k}：{_format_bmc_line(k, (idea.get('bmc') or {}).get(k))}")
        L.append("")

    L.append("## 人類提問記錄\n")
    if human_qa_log:
        for qa in human_qa_log:
            L.append(f"**問 {_persona_with_role(qa['presenter_name'])}**：{qa['question']}")
            L.append(f"> {qa['answer']}\n")
    else:
        L.append("（本場沒有人類提問，全程跳過）\n")

    L.append("## DFV 結構化評分（Desirability / Feasibility / Viability）\n")
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
    """Double Diamond 拓樸——純線性 DAG，沒有任何多重靜態入邊節點
    （見 test_graph.py 的拓樸安全性測試，把這件事從 docstring 宣稱變成
    自動化回歸測試）。stage12 踩到的坑（詳見 note.md）：LangGraph 對
    「兩個長度差很多的分支各自邊接同一個下游節點」不保證會等全部前驅都
    完成——這張圖延續 stage12 修好的做法，只用兩種驗證過安全的 fan-in
    模式的遞迴組合：(A) `Send()` fan-out 到同一個節點名稱、(B) 單一節點
    內同步呼叫 `xxx_graph.invoke()`。

    `discover_and_evaluate_jobs`（模式 B，內部對每個候選 job 用模式 A
    fan-out 到 `research_one_candidate_job`，該節點內部又同步呼叫模式 B
    的 `interview_panel_graph`，其內部再用模式 A fan-out 到
    `interview_one_person`——四層巢狀，每一層都是模式 A/B 的直接重複）→
    `select_job_and_define_problem`（Define 收斂，單一前驅，不是 join）→
    `assemble_persona_team`（模式 B，內部模式 A fan-out 到
    `generate_one_persona_for_domain`——這個接縫本身也不是 join，因為
    JTBD 重排序後兩者是嚴格單一前驅鏈，不像 stage12
    `system_research`／`generate_personas` 是兩個互不相依卻要同時完成的
    平行分支）。baseline／最終評分／存檔報告整個退回 stage9 驗證過的
    模式——不是圖節點，在 `main()` 裡用背景執行緒跟主線平行跑、最後
    `join()`。"""
    g = StateGraph(MeetingState)
    g.add_node(
        "desk_research_hypothesize_jobs",
        instrument("desk_research_hypothesize_jobs", desk_research_hypothesize_jobs),
    )
    g.add_node(
        "discover_and_evaluate_jobs",
        instrument("discover_and_evaluate_jobs", discover_and_evaluate_jobs),
    )
    g.add_node(
        "select_job_and_define_problem",
        instrument("select_job_and_define_problem", select_job_and_define_problem),
    )
    g.add_node("assemble_persona_team", instrument("assemble_persona_team", assemble_persona_team))
    g.add_node("draft_one_idea", instrument("draft_one_idea", draft_one_idea))
    g.add_node("ask_question", instrument("ask_question", ask_question))
    g.add_node("answer_question", instrument("answer_question", answer_question))
    g.add_node("dfv_scoring", instrument("dfv_scoring", dfv_scoring))
    g.add_node("pick_winner", instrument("pick_winner", pick_winner))
    g.add_node("generate_prototype", instrument("generate_prototype", generate_prototype))
    g.add_node("generate_evaluators", instrument("generate_evaluators", generate_evaluators))

    g.add_edge(START, "desk_research_hypothesize_jobs")
    g.add_edge("desk_research_hypothesize_jobs", "discover_and_evaluate_jobs")
    g.add_edge("discover_and_evaluate_jobs", "select_job_and_define_problem")
    g.add_edge("select_job_and_define_problem", "assemble_persona_team")
    g.add_conditional_edges("assemble_persona_team", fan_out_ideas, ["draft_one_idea"])
    g.add_edge("draft_one_idea", "ask_question")

    g.add_conditional_edges(
        "ask_question", route_after_question,
        {"answer_question": "answer_question", "dfv_scoring": "dfv_scoring"},
    )
    g.add_edge("answer_question", "ask_question")

    g.add_edge("dfv_scoring", "pick_winner")
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
            "five_forces": {},
            "trend_analysis": "",
            "candidate_jobs": [],
            "used_fallback_candidate_jobs": False,
            "research_queries": [],
            "research_items": [],
            "interview_transcript": [],
            "job_evidence": [],
            "selected_job_id": "",
            "selected_job": {},
            "target_audience": "",
            "problem_statement": "",
            "hmw": "",
            "insights": [],
            "personas": [],
            "used_fallback_personas": False,
            "ideas": [],
            "pending_question": None,
            "pending_question_target_idea_id": None,
            "pending_question_asked_by": None,
            "human_qa_log": [],
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
    candidate_jobs = final_state["candidate_jobs"]
    interviewees = [person for cj in candidate_jobs for person in cj.get("interview_pool", [])]
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
        candidate_jobs=candidate_jobs, used_fallback_candidate_jobs=final_state["used_fallback_candidate_jobs"],
        job_evidence=final_state["job_evidence"],
        selected_job_id=final_state["selected_job_id"], selected_job=final_state["selected_job"],
        target_audience=final_state["target_audience"], problem_statement=final_state["problem_statement"],
        hmw=final_state["hmw"],
        five_forces=final_state["five_forces"], trend_analysis=final_state["trend_analysis"],
        interview_transcript=final_state["interview_transcript"], insights=final_state["insights"],
        personas=personas,
        used_fallback_personas=final_state["used_fallback_personas"], ideas=ideas,
        human_qa_log=human_qa_log, dfv_scores=dfv_scores,
        winner_idea=winner_idea, idea_diversity=idea_diversity,
        prototype=prototype, evaluators=evaluators,
        used_fallback_evaluators=final_state["used_fallback_evaluators"],
        baseline_proposal=baseline_proposal, baseline_metrics=baseline_metrics,
        user_evaluation=user_evaluation, final_verdict=final_verdict,
    )

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report_md = build_final_report_markdown(
        round_id=round_id, topic=topic,
        candidate_jobs=candidate_jobs, used_fallback_candidate_jobs=final_state["used_fallback_candidate_jobs"],
        job_evidence=final_state["job_evidence"],
        selected_job_id=final_state["selected_job_id"], selected_job=final_state["selected_job"],
        target_audience=final_state["target_audience"], problem_statement=final_state["problem_statement"],
        hmw=final_state["hmw"],
        five_forces=final_state["five_forces"], trend_analysis=final_state["trend_analysis"],
        interview_transcript=final_state["interview_transcript"], insights=final_state["insights"],
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
    interviewee_names = {i.get("name") for i in interviewees}
    evaluators_ok = len(evaluators) >= 2 and all(e.get("name") not in interviewee_names for e in evaluators)
    user_evaluation_ok = len(user_evaluation.get("evaluations") or []) == len(evaluators) and all(
        0 <= e["agent_score"] <= 10 and 0 <= e["baseline_score"] <= 10
        for e in user_evaluation.get("evaluations") or []
    )
    report_text = report_path.read_text(encoding="utf-8") if report_path.exists() else ""
    report_complete = (
        report_path.exists()
        and len(report_text) > 500
        and "Define" in report_text
        and "DFV 結構化評分" in report_text
        and "最終評估者對照評分" in report_text
    )
    # 真實跑測踩過的坑：evaluate_with_agents 一度因為 join 沒等到主線
    # 資料，讓 baseline/agent 平均分都變成假的 0.0——這裡明確驗證兩邊
    # 平均分不是同時剛好是 0（那組合幾乎不可能是真實評分，一出現就代表
    # 評分用的資料是空的）。
    scores_look_real = not (user_evaluation.get("agent_avg_score") == 0.0 and user_evaluation.get("baseline_avg_score") == 0.0)

    # Double Diamond 重構特有的驗收項——candidate_jobs 數量在 2-4 之間、
    # 每個候選 job 都完成自己的訪談與證據萃取、selected_job 有具體
    # why_selected 依據、參與者的公司衍生職能彼此不重複。
    candidate_jobs_ok = 2 <= len(candidate_jobs) <= 4 and len(final_state["job_evidence"]) == len(candidate_jobs)
    selected_job_ok = bool(final_state["selected_job"].get("why_selected"))
    persona_domains = [p.get("domain") for p in personas if p.get("domain")]
    personas_domain_ok = len(persona_domains) == len(set(persona_domains))

    print(f"每位參與者都提了一個 idea（{len(ideas)}/{len(personas)}）：{'是' if ideas_ok else '否'}")
    print(f"DFV 評分筆數正確（{len(dfv_scores)}，應為 {len(DFV_LENSES)}x{len(ideas)}）：{'是' if dfv_ok else '否'}")
    print(f"Prototype 已寫出可開啟的 HTML：{'是' if prototype_ok else '否'}")
    print(f"最終評估者跟全部候選 job 訪談對象都不重複（{len(evaluators)} 位）：{'是' if evaluators_ok else '否'}")
    print(f"最終評估者對兩個方案都留下合法評分：{'是' if user_evaluation_ok else '否'}")
    print(f"評分數字不是可疑的雙 0（agent={user_evaluation.get('agent_avg_score')}, baseline={user_evaluation.get('baseline_avg_score')}）：{'是' if scores_look_real else '否'}")
    print(f"最終報告完整：{'是' if report_complete else '否'}")
    print(f"候選 job 數在 2-4 之間且各自完成訪談證據（{len(candidate_jobs)} 個）：{'是' if candidate_jobs_ok else '否'}")
    print(f"選定 job 有具體 why_selected 依據：{'是' if selected_job_ok else '否'}")
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
        and candidate_jobs_ok and selected_job_ok and personas_domain_ok
        and EVENTS_PATH.exists()
    )
    if not ok:
        print("\n驗收未通過，請檢查上方輸出。")
        sys.exit(2)
    print("\n驗收通過。")


if __name__ == "__main__":
    main()
