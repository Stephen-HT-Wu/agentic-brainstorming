import operator
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Annotated, List, TypedDict
from unittest import mock

import graph
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt


def _reset_chroma(tmp_path: Path):
    graph._chroma_client = None
    return mock.patch.object(graph, "CHROMA_DIR", tmp_path)


class MemoryRefsTests(unittest.TestCase):
    def test_count_real_memory_refs_only_counts_known_ids(self):
        recalled = [{"id": "m1", "text": "a"}, {"id": "m2", "text": "b"}]
        proposal = {"memory_refs": ["m1", "m99"]}
        self.assertEqual(graph.count_real_memory_refs(proposal, recalled), 1)

    def test_ensure_hmw_fields_filters_bogus_memory_refs_but_allows_empty(self):
        state = {
            "pov": "p", "hmw": "h",
            "insights": [{"id": "i1", "text": "x"}],
            "recalled_memory": [{"id": "m1", "text": "y"}],
        }
        proposal = {"memory_refs": ["m1", "bogus"], "insight_refs": [], "hmw_response": ""}
        fixed = graph._ensure_hmw_fields(proposal, state)
        self.assertEqual(fixed["memory_refs"], ["m1"])  # bogus 濾掉，真的存在的留著

        proposal2 = {"memory_refs": [], "insight_refs": [], "hmw_response": ""}
        fixed2 = graph._ensure_hmw_fields(proposal2, state)
        self.assertEqual(fixed2["memory_refs"], [])  # 空陣列是誠實的答案，不強制塞東西


class _CrashResumeState(TypedDict):
    log: Annotated[List[str], operator.add]
    ask_count: int


class RunMeetingCrashVsInterruptTests(unittest.TestCase):
    """真實跑測踩過的情況：facilitator_decide 拋未捕捉例外崩潰後，
    `run_meeting` 誤把「沒有 interrupt 的 pending task」當成「有 interrupt
    payload 可以解析」，直接對空 dict 呼叫 payload["presenter_id"] 導致
    KeyError。用一個獨立的 toy graph（不需要真的跑 stage7 全流程、零成本）
    驗證 `run_meeting` 現在能正確分辨『崩潰待重跑』跟『真的在等人類輸入』。"""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmpdir.name) / "test.sqlite")
        self._attempts = {"n": 0}

    def tearDown(self):
        self.tmpdir.cleanup()

    def _build(self, flaky: bool):
        def step_a(state):
            return {"log": ["a"]}

        def flaky_or_interrupt_step(state):
            if flaky:
                self._attempts["n"] += 1
                if self._attempts["n"] == 1:
                    raise ValueError("simulated crash")
                return {"log": ["b-after-crash"]}
            answer = interrupt({"presenter_id": "p1", "presenter_name": "P1", "questions_asked_so_far": 0})
            return {"log": [f"b-resumed-with-{answer}"]}

        g = StateGraph(_CrashResumeState)
        g.add_node("step_a", step_a)
        g.add_node("step_b", flaky_or_interrupt_step)
        g.add_edge(START, "step_a")
        g.add_edge("step_a", "step_b")
        g.add_edge("step_b", END)
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        return g.compile(checkpointer=SqliteSaver(conn))

    def test_run_meeting_recovers_from_a_crashed_node_without_reraising_keyerror(self):
        crashy_graph = self._build(flaky=True)
        config = {"configurable": {"thread_id": "t1"}}
        # 第一次呼叫本身就會在 step_b 崩潰；run_meeting 內部要能吞下這個狀態
        # 並在下一輪迴圈用 invoke(None, ...) 重跑，而不是對著空 payload 解析。
        with self.assertRaises(ValueError):
            crashy_graph.invoke({"log": [], "ask_count": 0}, config)
        result = graph.run_meeting(
            crashy_graph, config, {"log": [], "ask_count": 0},
            script=None, stop_after_first_interrupt=False,
        )
        self.assertEqual(result["log"], ["a", "b-after-crash"])

    def test_run_meeting_still_uses_command_resume_for_real_interrupts(self):
        interrupt_graph = self._build(flaky=False)
        config = {"configurable": {"thread_id": "t2"}}
        script = {"p1": {"questions": []}}  # 沒有問題可問 -> 直接 skip
        result = graph.run_meeting(
            interrupt_graph, config, {"log": [], "ask_count": 0},
            script=script, stop_after_first_interrupt=False,
        )
        self.assertEqual(result["log"], ["a", "b-resumed-with-{'action': 'skip'}"])


class MasterPanelTests(unittest.TestCase):
    def test_fan_out_masters_produces_one_send_per_master(self):
        sends = graph.fan_out_masters({
            "topic": "t", "idea_pool_summary": "s", "critiques": [],
        })
        self.assertEqual(len(sends), len(graph.MASTERS))
        self.assertTrue(all(s.node == "master_critique" for s in sends))
        ids = {s.arg["master"]["id"] for s in sends}
        self.assertEqual(ids, {m["id"] for m in graph.MASTERS})

    def test_master_critique_falls_back_when_llm_gives_empty_critique(self):
        task = {"master": graph.MASTERS[0], "topic": "t", "idea_pool_summary": "s"}
        with mock.patch.object(graph, "call_llm", return_value='{"critique": "", "top_pick_persona": ""}'), \
             mock.patch.object(graph, "emit_event"):
            result = graph.master_critique(task)
        self.assertTrue(result["critiques"][0]["critique"])  # 保底文字非空


class FacilitatorRoutesToMastersTests(unittest.TestCase):
    """stage7 跟 stage6 的唯一路由差異：收斂後 goto 是 "run_masters" 不是 END。"""

    def test_hard_cap_routes_to_run_masters_not_end(self):
        personas = [{"id": "a", "name": "A"}]
        log = [{"round": i, "action": "present", "chosen_persona_id": "a", "reason": "x"} for i in range(1, graph.MAX_ROUNDS + 1)]
        state = {"personas": personas, "facilitator_log": log, "idea_pool_versions": [], "persona_results": []}
        with mock.patch.object(graph, "call_llm") as mock_llm, mock.patch.object(graph, "emit_event"):
            cmd = graph.facilitator_decide(state)
        mock_llm.assert_not_called()
        self.assertEqual(cmd.goto, "run_masters")

    def test_llm_end_decision_routes_to_run_masters_not_end(self):
        personas = [{"id": "a", "name": "A"}]
        log = [{"round": 1, "action": "present", "chosen_persona_id": "a", "reason": "x"}]
        state = {"personas": personas, "facilitator_log": log, "idea_pool_versions": [], "persona_results": []}
        with mock.patch.object(graph, "call_llm", return_value='{"action":"end","reason":"done"}'), \
             mock.patch.object(graph, "emit_event"):
            cmd = graph.facilitator_decide(state)
        self.assertEqual(cmd.goto, "run_masters")


class ChromaWisdomTests(unittest.TestCase):
    """用真的 Chroma（本地 embedding，零 API 成本）驗證寫入/檢索邏輯，
    指到一個暫存目錄，不動到真正的 practice/chroma_db。"""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.patcher = _reset_chroma(Path(self.tmpdir.name))
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        graph._chroma_client = None
        self.tmpdir.cleanup()

    def test_write_collective_wisdom_writes_expected_counts(self):
        master_critiques = [
            {"master_id": "tech_master", "master_name": "技術大師", "critique": "技術上可行"},
        ]
        idea_pool_versions = [
            {
                "persona_id": "a", "persona_name": "A",
                "proposal_after": {"title": "T1", "summary": "S1"},
            },
            {
                "persona_id": "a", "persona_name": "A",  # 同一人加輪過，只該算最後一版
                "proposal_after": {"title": "T1-v2", "summary": "S1-v2"},
            },
        ]
        persona_results = [
            {"persona": {"id": "a", "name": "A"}, "insights": [{"id": "i1", "text": "洞見一"}, {"id": "i2", "text": "洞見二"}]},
        ]
        stats = graph.write_collective_wisdom(
            round_id="r1", topic="測試主題",
            master_critiques=master_critiques,
            idea_pool_versions=idea_pool_versions,
            persona_results=persona_results,
        )
        self.assertEqual(stats["wisdom_written"], 2)  # 1 大師 + 1 最終提案（不是 2 個版本都寫）
        self.assertEqual(stats["interviews_written"], 2)  # 2 則洞見
        self.assertEqual(graph.get_chroma_collection("wisdom").count(), 2)
        self.assertEqual(graph.get_chroma_collection("interviews").count(), 2)

    def test_recall_memory_excludes_current_round_and_finds_related_topic(self):
        # 先寫入「上一輪」的智慧
        graph.write_collective_wisdom(
            round_id="round-past", topic="新聞短影音互動率",
            master_critiques=[{"master_id": "tech_master", "master_name": "技術大師", "critique": "開場鉤子決定完播率"}],
            idea_pool_versions=[{"persona_id": "x", "persona_name": "X", "proposal_after": {"title": "舊提案", "summary": "用戶不信任沒有來源標註的內容"}}],
            persona_results=[{"persona": {"id": "x", "name": "X"}, "insights": [{"id": "i1", "text": "通勤族沒空看長文"}]}],
        )
        state = {
            "persona": {"name": "測試人", "focus": ["用戶留存"]},
            "topic": "新聞短影音互動率",
            "round_id": "round-current",  # 跟寫入時不同 round_id
        }
        with mock.patch.object(graph, "emit_event"):
            result = graph.recall_memory(state)
        hits = result["recalled_memory"]
        self.assertGreater(len(hits), 0)  # 相關主題應該命中
        self.assertTrue(all(h["round_id"] != "round-current" for h in hits))

    def test_recall_memory_excludes_hits_from_same_round_id(self):
        # 寫入的 round_id 跟查詢時的 round_id 相同——不該引用『自己這輪』
        graph.write_collective_wisdom(
            round_id="round-same", topic="新聞短影音互動率",
            master_critiques=[{"master_id": "tech_master", "master_name": "技術大師", "critique": "開場鉤子決定完播率"}],
            idea_pool_versions=[],
            persona_results=[],
        )
        state = {
            "persona": {"name": "測試人", "focus": ["用戶留存"]},
            "topic": "新聞短影音互動率",
            "round_id": "round-same",
        }
        with mock.patch.object(graph, "emit_event"):
            result = graph.recall_memory(state)
        self.assertEqual(result["recalled_memory"], [])

    def test_recall_memory_returns_empty_when_chroma_is_empty(self):
        state = {
            "persona": {"name": "測試人", "focus": ["用戶留存"]},
            "topic": "任何主題",
            "round_id": "r1",
        }
        with mock.patch.object(graph, "emit_event"):
            result = graph.recall_memory(state)
        self.assertEqual(result["recalled_memory"], [])


if __name__ == "__main__":
    unittest.main()
