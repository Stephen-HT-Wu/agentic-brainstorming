import unittest
from unittest import mock

import graph


class Stage3UnitTests(unittest.TestCase):
    def test_bmc_requires_exact_nonempty_nine_fields(self):
        valid = {key: "內容" for key in graph.BMC_KEYS}
        self.assertEqual(graph.assert_bmc_complete({"bmc": valid}), [])
        invalid = dict(valid, 額外="不允許")
        self.assertTrue(graph.assert_bmc_complete({"bmc": invalid}))

    def test_count_real_insight_refs_only_counts_known_ids(self):
        insights = [{"id": "i1", "text": "a"}, {"id": "i2", "text": "b"}]
        proposal = {"insight_refs": ["i1", "i99", "i1"]}
        self.assertEqual(graph.count_real_insight_refs(proposal, insights), 2)

    def test_ensure_hmw_fields_copies_pov_hmw_and_repairs_bad_refs(self):
        state = {
            "pov": "用戶需要更快看懂新聞",
            "hmw": "我們可以怎麼幫用戶更快看懂新聞？",
            "insights": [{"id": "i1", "text": "通勤族沒空看長文"}],
        }
        proposal = {"insight_refs": ["i999"], "hmw_response": ""}
        fixed = graph._ensure_hmw_fields(proposal, state)
        self.assertEqual(fixed["pov"], state["pov"])
        self.assertEqual(fixed["hmw"], state["hmw"])
        self.assertEqual(fixed["insight_refs"], ["i1"])  # 壞 id 被換成保底的第一則洞見
        self.assertTrue(fixed["hmw_response"])  # 空字串被保底文字補上

    def test_ensure_hmw_fields_keeps_valid_refs(self):
        state = {
            "pov": "p", "hmw": "h",
            "insights": [{"id": "i1", "text": "a"}, {"id": "i2", "text": "b"}],
        }
        proposal = {"insight_refs": ["i2"], "hmw_response": "已經回應"}
        fixed = graph._ensure_hmw_fields(proposal, state)
        self.assertEqual(fixed["insight_refs"], ["i2"])
        self.assertEqual(fixed["hmw_response"], "已經回應")

    def test_fan_out_passes_users_to_every_persona(self):
        personas = [{"id": "a"}, {"id": "b"}]
        users = [{"id": "u1"}, {"id": "u2"}]
        sends = graph.fan_out_personas({
            "topic": "t", "company": "c", "personas": personas,
            "users": users, "persona_results": [],
        })
        self.assertEqual(len(sends), 2)
        self.assertTrue(all(s.arg["users"] == users for s in sends))

    def test_conduct_interviews_produces_users_times_rounds_turns(self):
        """monkeypatch 掉真正的 LLM 呼叫，驗證逐字稿長度＝人數 × 輪數的結構不變量。"""
        persona = {"name": "測試人", "id": "t"}
        users = [{"id": "u1", "name": "甲"}, {"id": "u2", "name": "乙"}]
        state = {
            "persona": persona,
            "users": users,
            "interview_guide": {"questions": ["開場問題？"]},
        }
        with mock.patch.object(graph, "simulate_user_answer", return_value="模擬回答"), \
             mock.patch.object(graph, "generate_followup_question", return_value="追問？"), \
             mock.patch.object(graph, "emit_event"):
            result = graph.conduct_interviews(state)
        transcript = result["interview_transcript"]
        self.assertEqual(len(transcript), len(users) * graph.INTERVIEW_ROUNDS)
        # 每位使用者的第一輪一定用訪綱的開場問題，不是動態追問
        first_rounds = [t for t in transcript if t["round"] == 1]
        self.assertTrue(all(t["question"] == "開場問題？" for t in first_rounds))

    def test_dedup_proposals_collapses_near_duplicates(self):
        proposals = [
            {"title": "A", "summary": "新聞短影音互動策略", "bmc": {}},
            {"title": "A2", "summary": "新聞短影音的互動策略", "bmc": {}},
            {"title": "B", "summary": "量子電腦晶片供應鏈風險評估", "bmc": {}},
        ]
        distinct = graph.dedup_proposals(proposals, threshold=0.5)
        self.assertEqual(len(distinct), 2)

    def test_extract_json_object_handles_non_dict_json(self):
        """真實跑測時模型對 write_pov_hmw 吐過一次合法 JSON 但頂層是 list，
        直接 .get() 會 AttributeError 讓整個 persona 的 worker 掛掉——回歸測試。"""
        self.assertEqual(graph.extract_json_object("[1, 2, 3]"), {})
        self.assertEqual(graph.extract_json_object('"just a string"'), {})
        self.assertEqual(graph.extract_json_object("not json at all"), {})
        self.assertEqual(graph.extract_json_object('{"pov": "p"}'), {"pov": "p"})

    def test_guide_text_joins_questions(self):
        self.assertEqual(
            graph.guide_text({"questions": ["Q1", "Q2"]}), "Q1\nQ2"
        )
        self.assertEqual(graph.guide_text({}), "")


if __name__ == "__main__":
    unittest.main()
