from unittest import mock
import unittest

import graph


PERSONAS = [
    {"id": "a", "name": "A"}, {"id": "b", "name": "B"},
    {"id": "c", "name": "C"}, {"id": "d", "name": "D"},
]


def _valid_bmc(text="x"):
    """BMC 量化後「收益流」「成本結構」是結構化物件，不是純字串——
    測試 fixture 統一用這個 helper 產生合法 BMC，不用每處各自處理兩種
    不同的欄位形狀。"""
    bmc = {k: text for k in graph.BMC_KEYS}
    for k in graph.QUANTIFIED_BMC_KEYS:
        bmc[k] = {"narrative": text, "monthly_estimate_twd": 1000, "basis": "測試假設"}
    return bmc


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
                "after": {"title": "新標題", "summary": "新摘要", "bmc": _valid_bmc("x")},
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
            user_evaluation={
                "evaluations": [
                    {"user_id": "u1", "user_name": "U1", "agent_reaction": "喜歡共創方案",
                     "agent_score": 8.0, "baseline_reaction": "還好", "baseline_score": 5.0},
                ],
                "agent_avg_score": 8.0, "baseline_avg_score": 5.0, "score_delta": 3.0,
            },
            problem_analysis={
                "five_forces": {"新進入者威脅": "低", "替代品威脅": "中", "顧客議價力": "高",
                                 "供應商議價力": "低", "現有競爭者強度": "中"},
                "trend_analysis": "短影音消費持續成長。",
                "problem_statement": "如何在通勤情境下提升互動率。",
                "used_fallback_users": False,
            },
        )
        kwargs.update(overrides)
        return kwargs

    def test_report_contains_all_required_sections(self):
        report = graph.build_final_report_markdown(**self._base_kwargs())
        for marker in (
            "問題定義", "人類提問記錄", "第一輪訪談（Empathize", "第二輪訪談（Test", "三鏡檢核",
            "大師點評", "共創歷程", "共創最終提案", "Baseline 對照", "模擬使用者評分對照",
        ):
            self.assertIn(marker, report)

    def test_report_includes_user_evaluation_table_and_averages(self):
        report = graph.build_final_report_markdown(**self._base_kwargs())
        self.assertIn("喜歡共創方案", report)
        self.assertIn("8.0", report)
        self.assertIn("5.0", report)
        self.assertIn("+3.00", report)  # score_delta 格式化成帶正負號兩位小數

    def test_report_handles_no_user_evaluations_gracefully(self):
        report = graph.build_final_report_markdown(**self._base_kwargs(
            user_evaluation={"evaluations": [], "agent_avg_score": 0, "baseline_avg_score": 0, "score_delta": 0},
        ))
        self.assertIn("沒有模擬使用者評分紀錄", report)

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
        valid = _valid_bmc("內容")
        self.assertEqual(graph.assert_bmc_complete({"bmc": valid}), [])
        self.assertTrue(graph.assert_bmc_complete({"bmc": dict(valid, 額外="不允許")}))


class RunMastersSeedSelectionTests(unittest.TestCase):
    """使用者要求把「全員互評選 Top-K」改成「共創收斂成一個提案」——共創
    草稿的種子要來自討論實際收斂到的地方（facilitator_log 最後一筆
    present），不是固定的 personas[0]，這裡鎖住這個行為。"""

    def _proposal(self, title: str) -> dict:
        return {"title": title, "summary": "s", "bmc": _valid_bmc("x")}

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
            "shared_draft": {"title": "種子草稿", "summary": "s", "bmc": _valid_bmc("x")},
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
            "bmc": _valid_bmc("y"),
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
            "title": "T", "summary": "s", "bmc": _valid_bmc("y"),
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


class EvaluateFinalOutputsWithUsersTests(unittest.TestCase):
    """使用者要求「誠實比對」的核心資料來源：模擬使用者對共創方案跟
    baseline 各自獨立打分。"""

    USERS = [
        {"id": "u1", "name": "陳小姐", "age": 32, "context": "通勤族", "pain_points": [], "tone": ""},
        {"id": "u2", "name": "王先生", "age": 45, "context": "家長", "pain_points": [], "tone": ""},
    ]

    def _proposal(self, title: str) -> dict:
        return {"title": title, "summary": "s", "bmc": _valid_bmc("x")}

    def test_prompt_uses_blind_labels_not_agent_or_baseline(self):
        # 使用者要求盲測命名，不能讓模擬使用者從 prompt 內容看出哪個是
        # 花更多功夫做出來的——鎖住 user prompt 真的用「方案 A/方案 B」，
        # 不含 "agent"、"baseline"、"共創" 這類會洩漏身份的字眼。
        captured_prompts = []

        def fake_call_llm(model, system, user, max_tokens=400):
            captured_prompts.append(user)
            return '{"a_score": 8, "a_reaction": "喜歡", "b_score": 4, "b_reaction": "普通"}'

        with mock.patch.object(graph, "call_llm", side_effect=fake_call_llm), \
             mock.patch.object(graph, "emit_event"):
            graph.evaluate_final_outputs_with_users(
                final_proposal=self._proposal("查證電子報"), baseline_proposal=self._proposal("新聞摘要卡"),
                users=self.USERS[:1],
            )
        prompt = captured_prompts[0]
        self.assertIn("方案 A", prompt)
        self.assertIn("方案 B", prompt)
        self.assertNotIn("agent", prompt.lower())
        self.assertNotIn("baseline", prompt.lower())
        self.assertNotIn("共創", prompt)

    def test_averages_scores_across_multiple_users(self):
        responses = iter([
            '{"a_score": 8, "a_reaction": "r1a", "b_score": 4, "b_reaction": "r1b"}',
            '{"a_score": 6, "a_reaction": "r2a", "b_score": 2, "b_reaction": "r2b"}',
        ])
        with mock.patch.object(graph, "call_llm", side_effect=lambda *a, **k: next(responses)), \
             mock.patch.object(graph, "emit_event"):
            summary = graph.evaluate_final_outputs_with_users(
                final_proposal=self._proposal("共創提案"), baseline_proposal=self._proposal("baseline提案"),
                users=self.USERS,
            )
        self.assertEqual(summary["agent_avg_score"], 7.0)  # (8+6)/2
        self.assertEqual(summary["baseline_avg_score"], 3.0)  # (4+2)/2
        self.assertEqual(summary["score_delta"], 4.0)
        self.assertEqual(len(summary["evaluations"]), 2)

    def test_malformed_score_falls_back_to_midpoint_not_zero(self):
        # 解析失敗不能讓平均分被污染成 0——沿用 score_proposal() 同款
        # 防呆，保底給中位數 5.0。
        with mock.patch.object(graph, "call_llm", return_value='{"a_reaction": "x", "b_reaction": "y"}'), \
             mock.patch.object(graph, "emit_event"):
            summary = graph.evaluate_final_outputs_with_users(
                final_proposal=self._proposal("共創提案"), baseline_proposal=self._proposal("baseline提案"),
                users=self.USERS[:1],
            )
        self.assertEqual(summary["evaluations"][0]["agent_score"], 5.0)
        self.assertEqual(summary["evaluations"][0]["baseline_score"], 5.0)

    def test_score_clamped_to_0_10_range(self):
        with mock.patch.object(graph, "call_llm", return_value='{"a_score": 99, "b_score": -5}'), \
             mock.patch.object(graph, "emit_event"):
            summary = graph.evaluate_final_outputs_with_users(
                final_proposal=self._proposal("共創提案"), baseline_proposal=self._proposal("baseline提案"),
                users=self.USERS[:1],
            )
        self.assertEqual(summary["evaluations"][0]["agent_score"], 10.0)
        self.assertEqual(summary["evaluations"][0]["baseline_score"], 0.0)


class GenerateFinalVerdictCitesUserEvaluationTests(unittest.TestCase):
    def test_verdict_prompt_includes_real_user_scores(self):
        captured = {}

        def fake_call_llm(model, system, user, max_tokens=900):
            captured["user"] = user
            return "評語內容"

        with mock.patch.object(graph, "call_llm", side_effect=fake_call_llm), \
             mock.patch.object(graph, "emit_event"):
            graph.generate_final_verdict(
                topic="T",
                co_created_proposal={"title": "共創", "summary": "s"},
                baseline_proposal={"title": "baseline", "summary": "s"},
                baseline_metrics={"real_citations": 0, "cost_usd": 0.01},
                diversity_after={"avg_distance": 0.3},
                user_evaluation={"agent_avg_score": 7.5, "baseline_avg_score": 4.2, "score_delta": 3.3},
            )
        self.assertIn("7.5", captured["user"])
        self.assertIn("4.2", captured["user"])
        self.assertIn("+3.30", captured["user"])


class BmcQuantificationTests(unittest.TestCase):
    """使用者要求「收益流」「成本結構」要真的量化才有用——這裡鎖住結構
    驗證／合併／損益計算三個純函式的行為。"""

    def _quant_cell(self, narrative="敘述", amount=1000, basis="依據"):
        return {"narrative": narrative, "monthly_estimate_twd": amount, "basis": basis}

    def test_assert_bmc_complete_rejects_plain_string_for_quant_keys(self):
        bmc = _valid_bmc("x")
        bmc["收益流"] = "純文字，舊格式"  # 量化前的舊格式，現在應該被拒絕
        issues = graph.assert_bmc_complete({"bmc": bmc})
        self.assertIn("缺漏或無效:收益流", issues)

    def test_assert_bmc_complete_accepts_valid_quant_object(self):
        bmc = _valid_bmc("x")
        self.assertEqual(graph.assert_bmc_complete({"bmc": bmc}), [])

    def test_assert_bmc_complete_rejects_missing_monthly_estimate(self):
        bmc = _valid_bmc("x")
        bmc["成本結構"] = {"narrative": "有敘述但沒有數字", "basis": "b"}
        issues = graph.assert_bmc_complete({"bmc": bmc})
        self.assertIn("缺漏或無效:成本結構", issues)

    def test_merge_bmc_keeps_valid_candidate_quant_cell(self):
        prev = _valid_bmc("舊")
        candidate = _valid_bmc("舊")
        candidate["收益流"] = self._quant_cell("新的收益流敘述", 5000, "新依據")
        merged = graph._merge_bmc(candidate, prev)
        self.assertEqual(merged["收益流"]["monthly_estimate_twd"], 5000)
        self.assertEqual(merged["收益流"]["narrative"], "新的收益流敘述")

    def test_merge_bmc_falls_back_to_prev_when_candidate_invalid(self):
        prev = _valid_bmc("舊")
        candidate = _valid_bmc("舊")
        candidate["成本結構"] = "模型退化成純文字"  # 不合法的候選值
        merged = graph._merge_bmc(candidate, prev)
        self.assertEqual(merged["成本結構"], prev["成本結構"])

    def test_merge_bmc_zeroed_placeholder_when_no_prev_either(self):
        candidate = {}
        merged = graph._merge_bmc(candidate, {})
        self.assertEqual(merged["收益流"], {"narrative": "", "monthly_estimate_twd": 0.0, "basis": ""})

    def test_compute_unit_economics_viable_when_revenue_exceeds_cost(self):
        bmc = _valid_bmc("x")
        bmc["收益流"]["monthly_estimate_twd"] = 5000
        bmc["成本結構"]["monthly_estimate_twd"] = 2000
        ue = graph.compute_unit_economics(bmc)
        self.assertEqual(ue["monthly_margin_twd"], 3000)
        self.assertTrue(ue["is_viable"])

    def test_compute_unit_economics_not_viable_when_cost_exceeds_revenue(self):
        bmc = _valid_bmc("x")
        bmc["收益流"]["monthly_estimate_twd"] = 1000
        bmc["成本結構"]["monthly_estimate_twd"] = 4000
        ue = graph.compute_unit_economics(bmc)
        self.assertEqual(ue["monthly_margin_twd"], -3000)
        self.assertFalse(ue["is_viable"])


class ViabilityNudgeTests(unittest.TestCase):
    """使用者確認的「軟性引導」：修正迴圈的 prompt 要看得到上一版損益，
    不划算時要有明確的「認真考慮換方向」提示，不是新增一個獨立關卡節點。"""

    def test_nudge_text_signals_pivot_when_not_viable(self):
        bmc = _valid_bmc("x")
        bmc["收益流"]["monthly_estimate_twd"] = 100
        bmc["成本結構"]["monthly_estimate_twd"] = 9000
        note = graph._viability_nudge({"bmc": bmc})
        self.assertIn("不划算", note)
        self.assertIn("換一個核心價值主張", note)

    def test_nudge_text_encourages_deepening_when_viable(self):
        bmc = _valid_bmc("x")
        bmc["收益流"]["monthly_estimate_twd"] = 9000
        bmc["成本結構"]["monthly_estimate_twd"] = 100
        note = graph._viability_nudge({"bmc": bmc})
        self.assertIn("可以繼續深化", note)
        self.assertNotIn("不划算", note)

    def test_refine_prompt_includes_viability_nudge(self):
        prev = {
            "title": "T", "summary": "s", "bmc": _valid_bmc("x"),
            "self_score": 5,
        }
        prev["bmc"]["收益流"]["monthly_estimate_twd"] = 100
        prev["bmc"]["成本結構"]["monthly_estimate_twd"] = 9000
        state = {
            "persona": {"name": "A"}, "proposal": prev, "refine_round": 0,
            "research_items": [], "pov": "POV", "hmw": "HMW", "topic": "T",
            "research_brief": "brief", "insights": [], "recalled_memory": [],
        }
        candidate = graph.json.dumps({
            "title": "T2", "summary": "s2", "bmc": _valid_bmc("y"),
            "self_score": 6, "insight_refs": [], "memory_refs": [],
        })
        captured = {}

        def fake_call_llm(model, system, user, max_tokens=2500):
            captured["system"] = system
            return candidate

        with mock.patch.object(graph, "call_llm", side_effect=fake_call_llm), \
             mock.patch.object(graph, "emit_event"):
            graph.refine(state)
        self.assertIn("不划算", captured["system"])


class AnalyzeProblemTests(unittest.TestCase):
    """使用者要求反過來從問題出發：五力＋趨勢分析定義問題後，才動態生成
    訪談對象，不是先有固定的 users.yaml 名單。"""

    def _state(self):
        return {"topic": "如何提升新聞短影音互動率", "company": "北辰短影音"}

    def test_happy_path_parses_five_forces_trend_and_interview_targets(self):
        llm_response = graph.json.dumps({
            "five_forces": {
                "新進入者威脅": "低", "替代品威脅": "中", "顧客議價力": "高",
                "供應商議價力": "低", "現有競爭者強度": "中",
            },
            "trend_analysis": "短影音消費持續成長，年輕世代偏好即時互動。",
            "problem_statement": "如何在通勤情境下提升互動率而非單純完播率。",
            "interview_targets": [
                {"id": "u1", "name": "通勤族小林", "age": 29, "context": "每天通勤 40 分鐘",
                 "pain_points": ["滑手機時間零碎"], "tone": "直接"},
                {"id": "u2", "name": "退休族陳伯", "age": 63, "context": "在家看新聞",
                 "pain_points": ["字太小"], "tone": "客氣"},
            ],
        })
        with mock.patch.object(graph, "web_search", return_value=[]), \
             mock.patch.object(graph, "call_llm", return_value=llm_response), \
             mock.patch.object(graph, "emit_event"):
            result = graph.analyze_problem(self._state())
        self.assertEqual(len(result["users"]), 2)
        self.assertEqual(result["users"][0]["name"], "通勤族小林")
        self.assertIn("如何在通勤情境下提升互動率", result["problem_brief"])
        self.assertFalse(result["problem_analysis"]["used_fallback_users"])
        self.assertEqual(result["problem_analysis"]["five_forces"]["顧客議價力"], "高")

    def test_falls_back_to_load_users_when_interview_targets_empty(self):
        llm_response = graph.json.dumps({
            "five_forces": {}, "trend_analysis": "", "problem_statement": "",
            "interview_targets": [],
        })
        fallback_users = [{"id": "u1", "name": "陳小姐", "age": 32, "context": "通勤族", "pain_points": [], "tone": ""}]
        with mock.patch.object(graph, "web_search", return_value=[]), \
             mock.patch.object(graph, "call_llm", return_value=llm_response), \
             mock.patch.object(graph, "emit_event"), \
             mock.patch.object(graph, "load_users", return_value=fallback_users):
            result = graph.analyze_problem(self._state())
        self.assertEqual(result["users"], fallback_users)
        self.assertTrue(result["problem_analysis"]["used_fallback_users"])

    def test_falls_back_to_load_users_when_llm_output_unparseable(self):
        fallback_users = [{"id": "u1", "name": "陳小姐", "age": 32, "context": "通勤族", "pain_points": [], "tone": ""}]
        with mock.patch.object(graph, "web_search", return_value=[]), \
             mock.patch.object(graph, "call_llm", return_value="不是合法 JSON"), \
             mock.patch.object(graph, "emit_event"), \
             mock.patch.object(graph, "load_users", return_value=fallback_users):
            result = graph.analyze_problem(self._state())
        self.assertEqual(result["users"], fallback_users)
        self.assertTrue(result["problem_analysis"]["used_fallback_users"])
        # 保底文字要有內容，不能是空字串
        self.assertTrue(result["problem_analysis"]["problem_statement"])


if __name__ == "__main__":
    unittest.main()
