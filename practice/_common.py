"""
跨階段共用的基礎設施。

只放進穩定、各 stage 應保持一致的部分：instrument / cost_of / PRICING。

call_llm、embedding、events 寫入等還會隨 stage 演化的邏輯刻意不放這裡——
抽太早會讓「改一個階段、牽動全部階段」的風險提早發生。
（模式沿用 agentic-articles，本 repo 重新抄寫，不跨 repo import。）
"""

import time
import uuid
from contextvars import ContextVar
from threading import Lock

PRICING = {
    "claude-haiku-4-5-20251001": (1.00, 5.00),
    "claude-sonnet-5": (3.00, 15.00),
}

node_times: dict = {}
_metrics_lock = Lock()
_current_node: ContextVar[str] = ContextVar("current_node", default="?")
_current_invocation: ContextVar[str] = ContextVar("current_invocation", default="?")


def reset_metrics() -> None:
    """每次 run 開始前呼叫，清空上一輪殘留的計時資料。"""
    node_times.clear()
    _current_node.set("?")
    _current_invocation.set("?")


def current_node() -> str:
    """給各階段的 call_llm 用，取得 instrument() 目前設定的節點名稱。"""
    return _current_node.get()


def current_invocation() -> str:
    """目前 node invocation 的唯一識別；平行執行時用來正確歸屬 usage。"""
    return _current_invocation.get()


def set_current_node(name: str) -> None:
    """給不經由 graph node 的呼叫（例如 baseline）手動標註節點名。"""
    _current_node.set(name)
    _current_invocation.set(f"{name}:{uuid.uuid4().hex}")


def cost_of(entry: dict) -> float:
    """單筆 LLM 呼叫的成本（USD）。"""
    price_in, price_out = PRICING.get(entry["model"], (0.0, 0.0))
    return entry["input"] / 1e6 * price_in + entry["output"] / 1e6 * price_out


def instrument(name: str, fn):
    """包住節點函式：記錄執行時間，並讓 call_llm 知道現在跑到哪個節點。"""

    def wrapped(state):
        node_token = _current_node.set(name)
        invocation_token = _current_invocation.set(f"{name}:{uuid.uuid4().hex}")
        start = time.perf_counter()
        try:
            return fn(state)
        finally:
            with _metrics_lock:
                node_times[name] = node_times.get(name, 0.0) + (
                    time.perf_counter() - start
                )
            _current_invocation.reset(invocation_token)
            _current_node.reset(node_token)

    return wrapped
