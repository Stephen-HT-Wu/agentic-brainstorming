import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import graph
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command


def _fake_homework_result(persona: dict) -> dict:
    return {
        "proposal": {
            "title": f"提案-{persona['id']}", "summary": "s", "bmc": {k: "v" for k in graph.BMC_KEYS},
            "pov": "p", "hmw": "h", "insight_refs": [], "hmw_response": "r", "sources": [],
            "self_score": 8.0,
        },
        "research_items": [], "research_brief": "", "interview_guide": {}, "interview_transcript": [],
        "insights": [], "pov": "p", "hmw": "h", "refine_deltas": [],
    }


def _fake_review_round(payload: dict) -> dict:
    reviewers = payload["reviewers"]
    reviews = [
        {
            "reviewer_id": r["id"], "reviewer_name": r["name"], "presenter_name": payload["presenter_name"],
            "agreements": ["a1", "a2", "a3"], "disagreements": ["d1", "d2", "d3"], "insights": ["i1", "i2", "i3"],
            "hmw_addressed": True, "hmw_addressed_reason": "ok",
        }
        for r in reviewers
    ]
    revised = dict(payload["proposal"])
    revised["title"] = payload["proposal"]["title"] + "-revised"
    revised["addressed_reviewer_ids"] = [reviewers[0]["id"]] if reviewers else []
    revised["revision_note"] = "調整"
    return {"reviews": reviews, "revised_proposal": revised}


class Stage5RoutingTests(unittest.TestCase):
    def test_route_presenter(self):
        state = {"presenter_index": 0, "personas": [{"id": "a"}, {"id": "b"}]}
        self.assertEqual(graph.route_presenter(state), "ask_question")
        state["presenter_index"] = 2
        self.assertEqual(graph.route_presenter(state), graph.END)

    def test_route_after_question(self):
        self.assertEqual(graph.route_after_question({"pending_question": "x"}), "answer_question")
        self.assertEqual(graph.route_after_question({"pending_question": None}), "run_peer_review")

    def test_proposal_for_index_matches_by_persona_id(self):
        state = {
            "personas": [{"id": "a"}, {"id": "b"}],
            "persona_results": [
                {"persona": {"id": "a"}, "proposal": {"title": "A"}},
                {"persona": {"id": "b"}, "proposal": {"title": "B"}},
            ],
        }
        self.assertEqual(graph._proposal_for_index(state, 1)["title"], "B")


class Stage5InterruptResumeTests(unittest.TestCase):
    """驗證 interrupt()/Command(resume=...) 迴圈的行為，全程монkeypatch 掉真正的
    LLM 呼叫（homework_graph.invoke / review_round_graph.invoke / call_llm），
    零 API 成本，只測 graph 的控制流程。"""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmpdir.name) / "test.sqlite")
        self.personas = [
            {"id": "a", "name": "A"},
            {"id": "b", "name": "B"},
        ]
        self.initial_input = {
            "topic": "t", "company": "c", "personas": self.personas, "users": [],
            "persona_results": [], "presenter_index": 0, "pending_question": None,
            "human_qa_log": [], "review_log": [], "idea_pool_versions": [],
        }

    def tearDown(self):
        self.tmpdir.cleanup()

    def _build(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        return graph.build_parent_graph(SqliteSaver(conn))

    def test_ask_skip_ask_skip_completes_and_records_qa(self):
        with mock.patch.object(graph.homework_graph, "invoke", side_effect=lambda inp: _fake_homework_result(inp["persona"])), \
             mock.patch.object(graph.review_round_graph, "invoke", side_effect=_fake_review_round), \
             mock.patch.object(graph, "call_llm", return_value="模擬回答"), \
             mock.patch.object(graph, "emit_event"):
            meeting_graph = self._build()
            config = {"configurable": {"thread_id": "t1"}}
            meeting_graph.invoke(self.initial_input, config)

            snap = meeting_graph.get_state(config)
            self.assertEqual(snap.next, ("ask_question",))
            payload = snap.tasks[0].interrupts[0].value
            self.assertEqual(payload["presenter_index"], 0)
            self.assertEqual(payload["presenter_name"], "A")

            # 對 A 提問一次
            meeting_graph.invoke(Command(resume={"action": "ask", "question": "為什麼？"}), config)
            snap = meeting_graph.get_state(config)
            self.assertEqual(snap.next, ("ask_question",))  # 問完一題後迴圈回到 ask_question，可以再問
            self.assertEqual(len(snap.values["human_qa_log"]), 1)

            # 跳過（結束對 A 的提問）→ 進入互評 → 換 B
            result = meeting_graph.invoke(Command(resume={"action": "skip"}), config)
            snap = meeting_graph.get_state(config)
            self.assertEqual(snap.next, ("ask_question",))
            self.assertEqual(snap.values["presenter_index"], 1)
            self.assertEqual(snap.tasks[0].interrupts[0].value["presenter_name"], "B")

            # B 直接跳過
            meeting_graph.invoke(Command(resume={"action": "skip"}), config)
            snap = meeting_graph.get_state(config)
            self.assertEqual(snap.next, ())  # 完成

            final = snap.values
            self.assertEqual(len(final["idea_pool_versions"]), 2)
            self.assertEqual(len(final["human_qa_log"]), 1)
            self.assertEqual(len(final["review_log"]), 2)  # 2 人 × 1 個審閱者
            # A 被問過、B 沒被問過 —— 兩條路徑都真的跑到
            asked_ids = {qa["presenter_id"] for qa in final["human_qa_log"]}
            self.assertEqual(asked_ids, {"a"})

    def test_cross_process_resume_uses_fresh_graph_and_connection(self):
        """模擬『process 中途結束、換一個全新 process 用同個 thread_id 續跑』：
        第一個 graph/connection 物件只跑到第一次 interrupt 就丟棄，
        第二個是完全獨立建出來的 graph/connection，指向同一個 sqlite 檔案。"""
        with mock.patch.object(graph.homework_graph, "invoke", side_effect=lambda inp: _fake_homework_result(inp["persona"])), \
             mock.patch.object(graph.review_round_graph, "invoke", side_effect=_fake_review_round), \
             mock.patch.object(graph, "call_llm", return_value="模擬回答"), \
             mock.patch.object(graph, "emit_event"):
            config = {"configurable": {"thread_id": "cross-process"}}

            graph_a = self._build()
            graph_a.invoke(self.initial_input, config)
            snap_a = graph_a.get_state(config)
            self.assertEqual(snap_a.next, ("ask_question",))
            del graph_a  # 模擬 process 在這裡結束

            graph_b = self._build()  # 全新 graph + connection，只靠同一個 db 檔案 + thread_id
            snap_b = graph_b.get_state(config)
            self.assertEqual(snap_b.next, ("ask_question",))
            self.assertEqual(snap_b.tasks[0].interrupts[0].value["presenter_name"], "A")

            graph_b.invoke(Command(resume={"action": "skip"}), config)
            graph_b.invoke(Command(resume={"action": "skip"}), config)
            final_snap = graph_b.get_state(config)
            self.assertEqual(final_snap.next, ())
            self.assertEqual(len(final_snap.values["idea_pool_versions"]), 2)


if __name__ == "__main__":
    unittest.main()
