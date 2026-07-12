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
            co_creation_log=[
                {"turn": 1, "persona_id": "b", "persona_name": "B",
                 "built_on_persona_ids": ["a"], "contribution_note": "補了通路細節",
                 "embedding_distance": 0.15},
            ],
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
        for marker in ("人類提問記錄", "第一輪訪談（Empathize", "第二輪訪談（Test", "三鏡檢核", "大師點評", "共創歷程", "共創最終提案", "Baseline 對照"):
            self.assertIn(marker, report)

    def test_report_includes_co_creation_turn_detail(self):
        report = graph.build_final_report_markdown(**self._base_kwargs())
        self.assertIn("補了通路細節", report)
        self.assertIn("B", report)

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


class RunMastersSeedSelectionTests(unittest.TestCase):
    """使用者要求把「全員互評選 Top-K」改成「共創收斂成一個提案」——共創
    草稿的種子要來自討論實際收斂到的地方（facilitator_log 最後一筆
    present），不是固定的 personas[0]，這裡鎖住這個行為。"""

    def _proposal(self, title: str) -> dict:
        return {"title": title, "summary": "s", "bmc": {k: "x" for k in graph.BMC_KEYS}}

    def test_seed_comes_from_last_present_decision_not_persona_order(self):
        state = {
            "topic": "T",
            "idea_pool_versions": [
                {"persona_id": "a", "persona_name": "A", "proposal_after": self._proposal("A提案")},
                {"persona_id": "c", "persona_name": "C", "proposal_after": self._proposal("C提案")},
            ],
            "personas": PERSONAS,
            "persona_results": [],
            # 最後一筆 present 是 c，不是設定檔順序的第一位 a——種子要選 c
            "facilitator_log": [
                {"round": 1, "action": "present", "chosen_persona_id": "a", "reason": "x"},
                {"round": 2, "action": "present", "chosen_persona_id": "c", "reason": "y"},
                {"round": 3, "action": "end", "chosen_persona_id": None, "reason": "z"},
            ],
        }
        with mock.patch.object(graph.master_panel_graph, "invoke", return_value={"critiques": []}):
            result = graph.run_masters(state)
        self.assertEqual(result["shared_draft"]["title"], "C提案")
        self.assertEqual(set(result["co_creation_order"]), {"a", "b", "d"})
        self.assertNotIn("c", result["co_creation_order"])
        self.assertEqual(result["co_creation_turn_index"], 0)

    def test_falls_back_to_first_persona_when_nobody_ever_presented(self):
        state = {
            "topic": "T", "idea_pool_versions": [], "personas": PERSONAS,
            "persona_results": [], "facilitator_log": [],
        }
        with mock.patch.object(graph.master_panel_graph, "invoke", return_value={"critiques": []}):
            result = graph.run_masters(state)
        self.assertEqual(result["co_creation_order"], ["b", "c", "d"])


class CoCreateTurnTests(unittest.TestCase):
    """共創迴圈的核心：4 位 persona 依序在同一份共享草稿上各自編輯一輪。"""

    def _state(self, **overrides):
        base = {
            "personas": PERSONAS,
            "co_creation_order": ["b", "c", "d"],
            "co_creation_turn_index": 0,
            "shared_draft": {"title": "種子草稿", "summary": "s", "bmc": {k: "x" for k in graph.BMC_KEYS}},
            "master_critiques": [{"master_name": "技術大師", "critique": "還不錯"}],
            "persona_results": [
                {"persona": {"id": "b", "name": "B"}, "proposal": {"title": "B原本的提案"},
                 "insights": [{"id": "i1", "text": "洞見1"}], "recalled_memory": [], "research_items": []},
            ],
        }
        base.update(overrides)
        return base

    def test_picks_correct_persona_and_advances_turn_index(self):
        state = self._state()
        mock_response = graph.json.dumps({
            "title": "更新後標題", "summary": "更新後摘要",
            "bmc": {k: "y" for k in graph.BMC_KEYS},
            "self_score": 8, "insight_refs": ["i1"], "memory_refs": [],
            "built_on_persona_ids": ["a", "not_a_real_persona"],
            "contribution_note": "補了 X",
        })
        with mock.patch.object(graph, "call_llm", return_value=mock_response), \
             mock.patch.object(graph, "emit_event"):
            result = graph.co_create_turn(state)
        self.assertEqual(result["co_creation_turn_index"], 1)
        self.assertEqual(result["shared_draft"]["title"], "更新後標題")
        turn = result["co_creation_log"][0]
        self.assertEqual(turn["persona_id"], "b")
        self.assertEqual(turn["turn"], 1)
        # 幻覺出來的 "not_a_real_persona" 要被過濾掉，只留真實存在的 "a"
        self.assertEqual(turn["built_on_persona_ids"], ["a"])

    def test_insight_refs_get_persona_id_prefix_to_avoid_collision(self):
        state = self._state()
        mock_response = graph.json.dumps({
            "title": "T", "summary": "s", "bmc": {k: "y" for k in graph.BMC_KEYS},
            "insight_refs": ["i1"], "memory_refs": [], "built_on_persona_ids": [],
        })
        with mock.patch.object(graph, "call_llm", return_value=mock_response), \
             mock.patch.object(graph, "emit_event"):
            result = graph.co_create_turn(state)
        self.assertIn("b:i1", result["shared_draft"]["insight_refs"])

    def test_route_loops_until_order_exhausted_then_goes_to_prototype_test(self):
        mid_state = {"co_creation_order": ["b", "c", "d"], "co_creation_turn_index": 1}
        self.assertEqual(graph.route_after_co_create_turn(mid_state), "co_create_turn")
        done_state = {"co_creation_order": ["b", "c", "d"], "co_creation_turn_index": 3}
        self.assertEqual(graph.route_after_co_create_turn(done_state), "run_prototype_test")


class RunPrototypeTestSingleProposalTests(unittest.TestCase):
    """使用者要求原型測試/三鏡檢核只針對共創出的『一個』提案，不是 Top-K
    候選清單——這裡鎖住餵給子圖的 payload 真的只有一筆、且用合成的
    共創小組 persona，不是某一個真實成員。"""

    def test_feeds_single_synthetic_persona_item_not_top_k_list(self):
        state = {
            "shared_draft": {"title": "共創最終提案", "summary": "s"},
            "users": [{"id": "u1", "name": "U1"}],
            "round_id": "r1",
        }
        captured = {}

        def fake_invoke(payload):
            captured["payload"] = payload
            return {"prototypes": [{"persona_id": "co_created"}]}

        with mock.patch.object(graph.prototype_test_graph, "invoke", side_effect=fake_invoke):
            graph.run_prototype_test(state)
        items = captured["payload"]["top_k_items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["proposal"]["title"], "共創最終提案")
        self.assertEqual(items[0]["persona"]["id"], "co_created")
        self.assertNotIn(items[0]["persona"]["id"], {p["id"] for p in PERSONAS})


if __name__ == "__main__":
    unittest.main()
