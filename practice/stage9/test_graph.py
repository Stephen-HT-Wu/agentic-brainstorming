from unittest import mock
import unittest

import graph


PERSONAS = [
    {"id": "a", "name": "A"}, {"id": "b", "name": "B"},
    {"id": "c", "name": "C"}, {"id": "d", "name": "D"},
]


class ThreeLensFanOutTests(unittest.TestCase):
    def test_fan_out_covers_every_persona_times_every_top_k_proposal_including_self(self):
        top_k_proposals = {"a": {"title": "TA"}, "b": {"title": "TB"}, "c": {"title": "TC"}}
        sends = graph.fan_out_three_lens({"personas": PERSONAS, "top_k_proposals": top_k_proposals, "checks": []})
        # 4 人 × 3 個提案 = 12，包含自評（跟同儕互評的排除自己不同，這裡是刻意設計）
        self.assertEqual(len(sends), 12)
        self.assertTrue(all(s.node == "three_lens_check" for s in sends))
        self_check_pairs = [
            s for s in sends if s.arg["persona"]["id"] == s.arg["target_persona_id"]
        ]
        self.assertEqual(len(self_check_pairs), 3)  # a 評 a、b 評 b、c 評 c 都該存在

    def test_three_lens_check_always_returns_exact_3_3_3_shape(self):
        task = {"persona": PERSONAS[0], "target_persona_id": "b", "proposal": {"title": "T", "summary": "S", "bmc": {}}}
        with mock.patch.object(graph, "call_llm", return_value='{"positive": ["p1"], "negative": [], "insight": ["i1","i2","i3","i4"]}'), \
             mock.patch.object(graph, "emit_event"):
            result = graph.three_lens_check(task)
        check = result["checks"][0]
        self.assertEqual(len(check["positive"]), 3)  # 少了，補到 3
        self.assertEqual(len(check["negative"]), 3)  # 完全沒給，全部補保底
        self.assertEqual(len(check["insight"]), 3)   # 多了，截斷到 3
        self.assertEqual(check["target_persona_id"], "b")
        self.assertEqual(check["persona_id"], "a")


class RunThreeLensCheckIntegrationTests(unittest.TestCase):
    def test_run_three_lens_check_uses_post_test_proposals_not_pre_test(self):
        state = {
            "personas": PERSONAS[:2],
            "prototypes": [
                {"persona_id": "a", "before": {"title": "舊-A"}, "after": {"title": "新-A"}},
                {"persona_id": "b", "before": {"title": "舊-B"}, "after": {"title": "新-B"}},
            ],
        }
        captured = {}

        def fake_invoke(payload):
            captured["payload"] = payload
            return {"checks": [{"target_persona_id": "a"}, {"target_persona_id": "b"}]}

        with mock.patch.object(graph.three_lens_panel_graph, "invoke", side_effect=fake_invoke):
            result = graph.run_three_lens_check(state)

        # 三鏡檢核要看的是『Test 之後』的版本（after），不是 Prototype 之前的草稿
        self.assertEqual(captured["payload"]["top_k_proposals"]["a"]["title"], "新-A")
        self.assertEqual(captured["payload"]["top_k_proposals"]["b"]["title"], "新-B")
        self.assertEqual(len(result["three_lens_checks"]), 2)


class FinalReportMarkdownTests(unittest.TestCase):
    def _base_kwargs(self, **overrides):
        kwargs = dict(
            round_id="r1",
            topic="測試主題",
            personas=PERSONAS[:2],
            users=[{"id": "u1", "name": "U1"}],
            persona_results=[
                {
                    "persona": {"id": "a", "name": "A"},
                    "pov": "用戶需要 X", "hmw": "我們可以怎麼做 X？",
                    "interview_transcript": [
                        {"user_name": "U1", "round": 1, "question": "Q1", "answer": "Ans1"},
                    ],
                },
            ],
            facilitator_log=[{"round": 1, "action": "present", "chosen_persona_name": "A", "reason": "第一位", "forced": True}],
            human_qa_log=[],
            master_critiques=[{"master_name": "技術大師", "angle": "可行性", "critique": "還不錯", "top_pick_persona": "A"}],
            score_aggregates={"a": {"mean": 8.0, "stdev": 0.5, "n": 1}},
            top_k_ids=["a"],
            prototypes=[{
                "persona_id": "a", "persona_name": "A",
                "after": {"title": "新標題", "summary": "新摘要", "bmc": {k: "x" for k in graph.BMC_KEYS}},
                "html_path": "/tmp/a.html", "revision_note": "微調", "embedding_distance": 0.2,
                "reactions": [{"user_name": "U1", "reaction": "還可以"}],
            }],
            three_lens_checks=[{
                "persona_id": "a", "persona_name": "A", "target_persona_id": "a",
                "positive": ["p1", "p2", "p3"], "negative": ["n1", "n2", "n3"], "insight": ["i1", "i2", "i3"],
            }],
            baseline_proposal={"title": "baseline標題", "summary": "s"},
            baseline_metrics={"real_citations": 0, "bmc_filled": 9, "cost_usd": 0.001},
            diversity_before={"avg_distance": 0.3},
            diversity_after={"avg_distance": 0.25},
        )
        kwargs.update(overrides)
        return kwargs

    def test_report_contains_all_required_sections(self):
        report = graph.build_final_report_markdown(**self._base_kwargs())
        for marker in ("人類提問記錄", "第一輪訪談（Empathize", "第二輪訪談（Test", "三鏡檢核", "大師點評", "集體評分聚合", "Baseline 對照"):
            self.assertIn(marker, report)

    def test_report_handles_empty_human_qa_log_gracefully(self):
        report = graph.build_final_report_markdown(**self._base_kwargs(human_qa_log=[]))
        self.assertIn("本場沒有人類提問", report)

    def test_report_includes_real_qa_when_present(self):
        qa_log = [{"presenter_name": "A", "question": "為什麼？", "answer": "因為市場需求。"}]
        report = graph.build_final_report_markdown(**self._base_kwargs(human_qa_log=qa_log))
        self.assertIn("為什麼？", report)
        self.assertIn("因為市場需求。", report)

    def test_report_includes_pov_hmw_and_prototype_path(self):
        report = graph.build_final_report_markdown(**self._base_kwargs())
        self.assertIn("用戶需要 X", report)
        self.assertIn("我們可以怎麼做 X？", report)
        self.assertIn("/tmp/a.html", report)


class RegressionCarryoverTests(unittest.TestCase):
    """輕量回歸檢查：確認 stage8 沿用過來的關鍵行為沒有被 stage9 的修改動到。"""

    def test_facilitator_end_decision_still_routes_to_run_masters(self):
        state = {
            "personas": PERSONAS[:1],
            "facilitator_log": [{"round": 1, "action": "present", "chosen_persona_id": "a", "reason": "x"}],
            "idea_pool_versions": [], "persona_results": [],
        }
        with mock.patch.object(graph, "call_llm", return_value='{"action":"end","reason":"done"}'), \
             mock.patch.object(graph, "emit_event"):
            cmd = graph.facilitator_decide(state)
        self.assertEqual(cmd.goto, "run_masters")

    def test_bmc_structural_invariant_still_enforced(self):
        valid = {k: "內容" for k in graph.BMC_KEYS}
        self.assertEqual(graph.assert_bmc_complete({"bmc": valid}), [])
        self.assertTrue(graph.assert_bmc_complete({"bmc": dict(valid, 額外="不允許")}))


if __name__ == "__main__":
    unittest.main()
