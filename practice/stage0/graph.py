"""
階段 0：骨架 + checkpointer 基礎

目標：先不呼叫任何 LLM，確認 LangGraph 能跑，並看懂 checkpointer 的
thread / checkpoint 概念——這是之後 HITL（interrupt/resume）與跨 session
續跑的地基，也是跟 agentic-articles stage0（只看 state 傳遞）最大的差異。

對應 PLAN.md「階段 0」驗收標準：
最小 StateGraph + MemorySaver，同 thread 連續 invoke，state 延續。
"""
import operator
import warnings
from typing import Annotated, List, TypedDict

from langchain_core._api.deprecation import LangChainPendingDeprecationWarning

warnings.filterwarnings("ignore", category=LangChainPendingDeprecationWarning)

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph


# state 是整張圖共用的「會議記錄」。
#
# ideas 欄位用 Annotated[..., operator.add] 宣告了一個 reducer：
# 節點回傳 {"ideas": [新點子]} 時，LangGraph 不會「覆蓋」而是「累加」到現有列表。
# 之後階段 2 平行 fan-out 時，多個 persona 同時回傳點子也靠同一個機制合併。
class MeetingState(TypedDict):
    topic: str                                  # 會議主題（輸入）
    ideas: Annotated[List[str], operator.add]   # 點子池（reducer：累加）


def add_idea(state: MeetingState) -> dict:
    """唯一的節點：機械式地丟出下一號點子（不呼叫 LLM，零成本）。

    編號刻意根據「目前 state 裡已有幾個點子」計算——
    如果 checkpointer 有把上次的 state 存下來，同一個 thread 再跑一次時
    編號就會接續（#2、#3…），而不是每次都從 #1 開始。
    """
    n = len(state["ideas"]) + 1
    return {"ideas": [f"點子 #{n}（主題：{state['topic']}）"]}


builder = StateGraph(MeetingState)
builder.add_node("add_idea", add_idea)
builder.add_edge(START, "add_idea")
builder.add_edge("add_idea", END)

# checkpointer 是這個階段的主角：
# compile 時掛上 MemorySaver，圖每跑完一步就把當下的 state 存成一個 checkpoint。
# 「存到哪一份記錄」由呼叫時 config 裡的 thread_id 決定——
# 同一個 thread_id ＝ 同一場會議的延續；不同 thread_id ＝ 各自獨立的會議。
# （MemorySaver 存在記憶體，process 結束就消失；階段 5 會換成 SqliteSaver 落地。）
checkpointer = MemorySaver()
graph = builder.compile(checkpointer=checkpointer)


if __name__ == "__main__":
    meeting_a = {"configurable": {"thread_id": "meeting-A"}}
    meeting_b = {"configurable": {"thread_id": "meeting-B"}}

    print("=== 1) 同一個 thread（meeting-A）連續 invoke 三次 ===")
    # 只有第一次需要給完整的初始 state；
    # 之後傳入的部分輸入會跟 checkpoint 裡的舊 state 合併（ideas 走 reducer 累加）。
    result = graph.invoke({"topic": "如何提升新聞短影音互動率", "ideas": []}, meeting_a)
    print("第 1 次 invoke：", result["ideas"])
    result = graph.invoke({"topic": result["topic"]}, meeting_a)
    print("第 2 次 invoke：", result["ideas"])
    result = graph.invoke({"topic": result["topic"]}, meeting_a)
    print("第 3 次 invoke：", result["ideas"])

    print()
    print("=== 2) 換一個 thread（meeting-B）→ 全新的 state，互不干擾 ===")
    result_b = graph.invoke({"topic": "另一場會議的主題", "ideas": []}, meeting_b)
    print("meeting-B 第 1 次 invoke：", result_b["ideas"])

    print()
    print("=== 3) get_state()：不重跑，直接讀某個 thread 目前的 checkpoint ===")
    snapshot = graph.get_state(meeting_a)
    print("meeting-A 目前的 state：", snapshot.values["ideas"])
    print("（snapshot.next =", snapshot.next, "→ 空 tuple 代表這場跑到 END、沒有停在半路）")

    print()
    print("=== 4) get_state_history()：同一個 thread 的每一步都被存成 checkpoint ===")
    history = list(graph.get_state_history(meeting_a))
    print(f"meeting-A 共有 {len(history)} 個 checkpoint（新到舊）：")
    for snap in history:
        step = snap.metadata.get("step")
        print(f"  step={step:>2}  ideas={snap.values.get('ideas', [])}")

    print()
    print("驗收：meeting-A 的點子編號跨 invoke 接續（#1→#2→#3），"
          "meeting-B 從 #1 重新開始，歷史 checkpoint 可逐步回看。")
