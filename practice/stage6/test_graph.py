import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import graph
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command


PERSONAS = [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}, {"id": "c", "name": "C"}]


def _base_state(**overrides) -> dict:
    state = {
        "personas": PERSONAS,
        "facilitator_log": [],
        "idea_pool_versions": [],
        "persona_results": [{"persona": {"id": p["id"]}, "proposal": {"title": p["id"]}} for p in PERSONAS],
    }
    state.update(overrides)
    return state


class FacilitatorDecisionTests(unittest.TestCase):
    """facilitator_decide 直接單元測試——重點是硬性上限即使 LLM 不配合也要生效，
    不能只在『LLM 乖乖聽話』的情況下才通過測試。"""

    def setUp(self):
        graph.usage_log.clear()

    def test_llm_picking_any_unpresented_persona_is_honored(self):
        # 第一輪大家都沒發表過，b 是合法的「尚未發表者」，不該被覆寫
        with mock.patch.object(graph, "call_llm", return_value='{"action":"present","persona_id":"b","reason":"x"}'), \
             mock.patch.object(graph, "emit_event"):
            cmd = graph.facilitator_decide(_base_state())
        self.assertEqual(cmd.goto, "ask_question")
        self.assertEqual(cmd.update["next_presenter_id"], "b")
        self.assertFalse(cmd.update["facilitator_log"][0]["forced"])

    def test_forces_unpresented_persona_when_llm_tries_to_repeat_someone(self):
        # a 已經發表過一次，b、c 還沒——LLM 卻想讓 a 再講一次，程式要強制改成 b 或 c
        log = [{"round": 1, "action": "present", "chosen_persona_id": "a", "reason": "x"}]
        state = _base_state(facilitator_log=log)
        with mock.patch.object(graph, "call_llm", return_value='{"action":"present","persona_id":"a","reason":"x"}'), \
             mock.patch.object(graph, "emit_event"):
            cmd = graph.facilitator_decide(state)
        self.assertEqual(cmd.goto, "ask_question")
        self.assertEqual(cmd.update["next_presenter_id"], "b")  # never_presented 裡的第一個（b、c 皆合法，程式選第一個）
        self.assertTrue(cmd.update["facilitator_log"][0]["forced"])

    def test_forces_end_when_round_cap_exceeded_without_calling_llm(self):
        log = [{"round": i, "action": "present", "chosen_persona_id": "a", "reason": "x"} for i in range(1, graph.MAX_ROUNDS + 1)]
        state = _base_state(facilitator_log=log)
        with mock.patch.object(graph, "call_llm") as mock_llm, \
             mock.patch.object(graph, "emit_event"):
            cmd = graph.facilitator_decide(state)
        mock_llm.assert_not_called()  # 硬上限直接短路，連 LLM 都不用問
        self.assertEqual(cmd.goto, graph.END)
        self.assertTrue(cmd.update["facilitator_log"][0]["forced"])

    def test_forces_end_when_budget_exceeded(self):
        # 每人都發表過一次，不受『優先未發表者』規則影響
        log = [{"round": i + 1, "action": "present", "chosen_persona_id": p["id"], "reason": "x"} for i, p in enumerate(PERSONAS)]
        graph.usage_log.append({
            "node": "x", "role": "x", "model": graph.FACILITATOR_MODEL,
            "input": 1_000_000, "output": 1_000_000,  # 故意灌爆成本
        })
        state = _base_state(facilitator_log=log)
        with mock.patch.object(graph, "call_llm") as mock_llm, \
             mock.patch.object(graph, "emit_event"):
            cmd = graph.facilitator_decide(state)
        mock_llm.assert_not_called()
        self.assertEqual(cmd.goto, graph.END)

    def test_honors_llm_choice_to_add_a_round_after_everyone_presented(self):
        log = [{"round": i + 1, "action": "present", "chosen_persona_id": p["id"], "reason": "x"} for i, p in enumerate(PERSONAS)]
        state = _base_state(facilitator_log=log)
        with mock.patch.object(graph, "call_llm", return_value='{"action":"present","persona_id":"a","reason":"還有爭議"}'), \
             mock.patch.object(graph, "emit_event"):
            cmd = graph.facilitator_decide(state)
        self.assertEqual(cmd.goto, "ask_question")
        self.assertEqual(cmd.update["next_presenter_id"], "a")
        self.assertFalse(cmd.update["facilitator_log"][0]["forced"])

    def test_honors_llm_choice_to_end(self):
        log = [{"round": i + 1, "action": "present", "chosen_persona_id": p["id"], "reason": "x"} for i, p in enumerate(PERSONAS)]
        state = _base_state(facilitator_log=log)
        with mock.patch.object(graph, "call_llm", return_value='{"action":"end","reason":"討論充分"}'), \
             mock.patch.object(graph, "emit_event"):
            cmd = graph.facilitator_decide(state)
        self.assertEqual(cmd.goto, graph.END)
        self.assertFalse(cmd.update["facilitator_log"][0]["forced"])

    def test_resolves_persona_name_to_id_instead_of_forcing_end(self):
        """真實跑測踩過的情況：Facilitator 判斷『A 還有異議該加輪』，reason
        講得很清楚，但 persona_id 填了中文名字而非 id——修正前這會被誤判成
        『id 無效』硬性結束會議，完全違背模型原本的判斷。"""
        log = [{"round": i + 1, "action": "present", "chosen_persona_id": p["id"], "reason": "x"} for i, p in enumerate(PERSONAS)]
        state = _base_state(facilitator_log=log)
        with mock.patch.object(graph, "call_llm", return_value='{"action":"present","persona_id":"A","reason":"A 還有異議該加輪"}'), \
             mock.patch.object(graph, "emit_event"):
            cmd = graph.facilitator_decide(state)
        self.assertEqual(cmd.goto, "ask_question")
        self.assertEqual(cmd.update["next_presenter_id"], "a")
        self.assertFalse(cmd.update["facilitator_log"][0]["forced"])
        self.assertEqual(cmd.update["facilitator_log"][0]["reason"], "A 還有異議該加輪")

    def test_rejects_invalid_persona_id_and_forces_end(self):
        log = [{"round": i + 1, "action": "present", "chosen_persona_id": p["id"], "reason": "x"} for i, p in enumerate(PERSONAS)]
        state = _base_state(facilitator_log=log)
        with mock.patch.object(graph, "call_llm", return_value='{"action":"present","persona_id":"nonexistent","reason":"x"}'), \
             mock.patch.object(graph, "emit_event"):
            cmd = graph.facilitator_decide(state)
        self.assertEqual(cmd.goto, graph.END)
        self.assertTrue(cmd.update["facilitator_log"][0]["forced"])


class SafeStrTests(unittest.TestCase):
    """真實跑測踩過的情況：模型把預期是字串的欄位吐成 list（例如
    write_pov_hmw 收到 {"pov": [...]}），`(x or "").strip()` 對 list 會
    直接 AttributeError，讓整個 persona 的做功課子圖崩潰。"""

    def test_returns_stripped_string_for_string_input(self):
        self.assertEqual(graph._safe_str("  hi  "), "hi")

    def test_returns_empty_string_for_non_string_input(self):
        self.assertEqual(graph._safe_str(["a", "b"]), "")
        self.assertEqual(graph._safe_str({"x": 1}), "")
        self.assertEqual(graph._safe_str(None), "")
        self.assertEqual(graph._safe_str(42), "")

    def test_facilitator_decide_does_not_crash_on_non_string_persona_id(self):
        log = [{"round": i + 1, "action": "present", "chosen_persona_id": p["id"], "reason": "x"} for i, p in enumerate(PERSONAS)]
        state = _base_state(facilitator_log=log)
        with mock.patch.object(graph, "call_llm", return_value='{"action":"present","persona_id":["a","b"],"reason":"x"}'), \
             mock.patch.object(graph, "emit_event"):
            cmd = graph.facilitator_decide(state)  # 不該拋例外
        self.assertEqual(cmd.goto, graph.END)  # 非字串 id 解析不出來，保底收斂
        self.assertTrue(cmd.update["facilitator_log"][0]["forced"])


class ProposalLookupTests(unittest.TestCase):
    def test_prefers_latest_idea_pool_version_over_homework_draft(self):
        state = {
            "persona_results": [{"persona": {"id": "a"}, "proposal": {"title": "原始草稿"}}],
            "idea_pool_versions": [
                {"persona_id": "a", "proposal_after": {"title": "第一次修正"}},
                {"persona_id": "a", "proposal_after": {"title": "第二次修正（加輪後）"}},
            ],
        }
        self.assertEqual(graph._proposal_for_persona(state, "a")["title"], "第二次修正（加輪後）")

    def test_falls_back_to_homework_draft_when_never_revised(self):
        state = {
            "persona_results": [{"persona": {"id": "b"}, "proposal": {"title": "草稿"}}],
            "idea_pool_versions": [],
        }
        self.assertEqual(graph._proposal_for_persona(state, "b")["title"], "草稿")


class GetHumanInputTests(unittest.TestCase):
    def test_script_keyed_by_persona_id(self):
        payload = {"presenter_id": "a", "presenter_name": "A", "questions_asked_so_far": 0}
        script = {"a": {"questions": ["Q1"]}, "b": {"skip": True}}
        self.assertEqual(graph.get_human_input(payload, script), {"action": "ask", "question": "Q1"})

        payload2 = {"presenter_id": "b", "presenter_name": "B", "questions_asked_so_far": 0}
        self.assertEqual(graph.get_human_input(payload2, script), {"action": "skip"})

        payload3 = {"presenter_id": "c", "presenter_name": "C", "questions_asked_so_far": 0}
        self.assertEqual(graph.get_human_input(payload3, script), {"action": "skip"})  # 沒在腳本裡的人預設跳過


def _fake_homework_result(persona: dict) -> dict:
    return {
        "proposal": {
            "title": f"提案-{persona['id']}", "summary": "s", "bmc": {k: "v" for k in graph.BMC_KEYS},
            "pov": "p", "hmw": "h", "insight_refs": [], "hmw_response": "r", "sources": [], "self_score": 8.0,
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


class Stage6IntegrationTests(unittest.TestCase):
    """全圖整合測試：mock 掉所有真正的 LLM 呼叫，驗證即使 Facilitator（被 mock
    成永遠選同一人）想無限『加輪』，硬上限還是會在 MAX_ROUNDS 把它砍斷。"""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmpdir.name) / "test.sqlite")
        graph.usage_log.clear()

    def tearDown(self):
        self.tmpdir.cleanup()

    def _build(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        return graph.build_parent_graph(SqliteSaver(conn))

    def test_pathological_facilitator_always_picks_same_persona_still_hits_hard_cap(self):
        personas = [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}]
        initial_input = {
            "topic": "t", "company": "c", "personas": personas, "users": [],
            "persona_results": [], "next_presenter_id": None, "pending_question": None,
            "human_qa_log": [], "review_log": [], "idea_pool_versions": [], "facilitator_log": [],
        }

        def always_pick_a(*_a, **_k):
            return '{"action":"present","persona_id":"a","reason":"永遠選 a（模擬失控的模型）"}'

        with mock.patch.object(graph.homework_graph, "invoke", side_effect=lambda inp: _fake_homework_result(inp["persona"])), \
             mock.patch.object(graph.review_round_graph, "invoke", side_effect=_fake_review_round), \
             mock.patch.object(graph, "call_llm", side_effect=always_pick_a), \
             mock.patch.object(graph, "emit_event"):
            meeting_graph = self._build()
            config = {"configurable": {"thread_id": "t1"}}
            meeting_graph.invoke(initial_input, config)

            # 一路跳過所有人類提問，直到圖跑完（或卡住太久就當測試失敗）
            for _ in range(graph.MAX_ROUNDS + 3):
                snap = meeting_graph.get_state(config)
                if not snap.next:
                    break
                meeting_graph.invoke(Command(resume={"action": "skip"}), config)

            final_snap = meeting_graph.get_state(config)
            self.assertEqual(final_snap.next, ())  # 真的結束了，沒有卡住
            log = final_snap.values["facilitator_log"]
            idea_pool_versions = final_snap.values["idea_pool_versions"]
            # 發表次數（= present 動作數）不能超過硬上限，log 總長度可以是
            # MAX_ROUNDS+1（多出來的一筆是最後那個 end 決策本身）。
            present_count = sum(1 for e in log if e["action"] == "present")
            self.assertEqual(present_count, len(idea_pool_versions))
            self.assertLessEqual(present_count, graph.MAX_ROUNDS)
            self.assertLessEqual(len(log), graph.MAX_ROUNDS + 1)
            self.assertTrue(log[-1]["forced"])  # 最後一輪是硬上限強制收斂，不是 LLM 自願結束
            self.assertEqual(log[-1]["action"], "end")
            # b 第一輪就該被強制排進去（因為她還沒發表過），不是被 always_pick_a 永遠忽略
            presented_ids = {v["persona_id"] for v in idea_pool_versions}
            self.assertIn("b", presented_ids)


if __name__ == "__main__":
    unittest.main()
