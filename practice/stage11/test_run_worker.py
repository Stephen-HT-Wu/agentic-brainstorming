"""
零成本單元測試：不呼叫任何真實 LLM/Chroma，只驗證 run_worker.py 的核心假設。

背景：真實跑測時撞到一個崩潰——cmd_resume() 先手動呼叫一次
`graph.invoke(Command(resume=...), config)`（一個 raw invoke，語意上會一路跑到
下一個 interrupt() 或整場結束才回傳），完事後為了重用 sg.main() 收尾的
baseline 對照／寫最終報告／save_outputs 邏輯，又呼叫一次 sg.main()（會進去
呼叫 stage9 的 run_meeting()）。如果那次 resume 剛好讓會議直接跑到底，
run_meeting() 開頭只看 snapshot.next 是不是空的來判斷「這個 thread 有沒有
未完成工作」——但「從沒開始過」跟「剛剛才跑完」兩種情況 snapshot.next 都是空
tuple，run_meeting() 會誤判成前者，用 initial_input 把整場會議從頭重跑一次。
寫進 Chroma 的 wisdom id（用 round_id 當前綴、deterministic）撞到已經寫過的
id，直接 DuplicateIDError 崩潰——真實發生過一次，燒了一整場的真實 API 成本
才抓到。

修法是 run_worker.py 用 `_safe_run_meeting` 包住 `sg.run_meeting`：resume 完
先看 snapshot.next 是不是已經空了，空的話直接回傳目前 state、不呼叫原本會
再跑一次 initial_input 的那個分支。這樣 sg.main() 收尾的邏輯還是會正常跑，
只是不會再把整張圖重跑一次。
"""
import sqlite3
import unittest

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from typing_extensions import TypedDict

import run_worker


class _State(TypedDict):
    x: int


def _build_toy_graph(call_counter: dict):
    """形狀刻意對齊 stage9 的真實拓樸：一個一般節點 + 一個會 interrupt() 的
    節點，讓 run_meeting()／_safe_run_meeting 可以直接套用，不用改介面。"""

    def step_a(state):
        call_counter["step_a"] = call_counter.get("step_a", 0) + 1
        return {"x": state["x"] + 1}

    def step_b(state):
        answer = interrupt({"ask": "continue?"})
        return {"x": state["x"] + (1 if answer == "yes" else 0)}

    g = StateGraph(_State)
    g.add_node("step_a", step_a)
    g.add_node("step_b", step_b)
    g.add_edge(START, "step_a")
    g.add_edge("step_a", "step_b")
    g.add_edge("step_b", END)

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    checkpointer = SqliteSaver(conn)
    return g.compile(checkpointer=checkpointer)


class RerunAfterCompletionIsARealLangGraphBehaviorTests(unittest.TestCase):
    def test_invoking_initial_input_again_after_completion_reruns_the_whole_graph(self):
        """先證明風險本身是真的存在，不是憑空腦補：對一個已經完成的
        thread_id 再呼叫一次 graph.invoke(initial_input, config)，
        LangGraph 真的會把整張圖從頭重跑一次。"""
        calls: dict = {}
        graph = _build_toy_graph(calls)
        config = {"configurable": {"thread_id": "t1"}}

        graph.invoke({"x": 0}, config)
        graph.invoke(Command(resume="yes"), config)
        self.assertEqual(calls["step_a"], 1)

        snapshot = graph.get_state(config)
        self.assertEqual(snapshot.next, ())  # 完成跟「從沒開始過」在這裡長得一樣

        graph.invoke({"x": 999}, config)  # 未受保護的 run_meeting() else 分支在做的事
        self.assertEqual(calls["step_a"], 2, "整張圖被重跑了一次——這正是要避免的行為")


class SafeRunMeetingWrapperTests(unittest.TestCase):
    """直接測 run_worker._safe_run_meeting 這個實際會被 monkeypatch 進
    sg.run_meeting 的函式，不是重新發明一個獨立的 guard。"""

    def test_does_not_rerun_when_thread_already_complete(self):
        calls: dict = {}
        graph = _build_toy_graph(calls)
        config = {"configurable": {"thread_id": "t1"}}

        graph.invoke({"x": 0}, config)
        graph.invoke(Command(resume="yes"), config)
        self.assertEqual(calls["step_a"], 1)

        result = run_worker._safe_run_meeting(
            graph, config, {"x": 999}, script=None, stop_after_first_interrupt=True
        )
        self.assertEqual(calls["step_a"], 1, "已完成的 thread 不該被重跑")
        self.assertEqual(result["x"], 2)  # 回傳的是既有的最終 state，不是重跑後的新結果

    def test_still_advances_a_genuinely_paused_thread(self):
        """確保修法沒有矯枉過正——真的還在等 interrupt 的 thread 要能正常繼續跑，
        不能被誤判成「已完成」。"""
        calls: dict = {}
        graph = _build_toy_graph(calls)
        config = {"configurable": {"thread_id": "t1"}}

        graph.invoke({"x": 0}, config)  # 卡在 step_b 的 interrupt()，還沒完成
        snapshot = graph.get_state(config)
        self.assertNotEqual(snapshot.next, ())

        # run_worker.py 實際上永遠用 stop_after_first_interrupt=True 呼叫
        # （見 cmd_start/cmd_resume）——這裡故意再呼叫一次，驗證「還在等
        # interrupt 的 thread」不會被誤判成完成，而是正常回報「還沒好」
        # （None）且不會重跑 step_a。
        result = run_worker._safe_run_meeting(
            graph, config, {"x": 0}, script=None, stop_after_first_interrupt=True
        )
        self.assertEqual(calls["step_a"], 1, "一開始就卡住了，step_a 不該被重跑")
        self.assertIsNone(result, "還在等 interrupt，stop_after_first_interrupt=True 應該回傳 None")

    def test_starts_a_fresh_thread_normally(self):
        """全新 thread（真的沒有任何 checkpoint）要能正常從 initial_input 開始跑，
        不能被誤判成"已完成"而回傳空值。"""
        calls: dict = {}
        graph = _build_toy_graph(calls)
        config = {"configurable": {"thread_id": "brand-new"}}

        result = run_worker._safe_run_meeting(
            graph, config, {"x": 0}, script=None, stop_after_first_interrupt=True
        )
        self.assertEqual(calls["step_a"], 1)
        self.assertIsNone(result)  # 卡在 interrupt，stop_after_first_interrupt=True 回傳 None


if __name__ == "__main__":
    unittest.main()
