import unittest

import build_replay as br

BMC_KEYS = ["客群", "價值主張", "通路", "顧客關係", "收益流", "關鍵資源", "關鍵活動", "關鍵夥伴", "成本結構"]


def _valid_bmc(text="x", revenue=1000, cost=1000):
    bmc = {k: text for k in BMC_KEYS}
    bmc["收益流"] = {"narrative": text, "monthly_estimate_twd": revenue, "basis": "b"}
    bmc["成本結構"] = {"narrative": text, "monthly_estimate_twd": cost, "basis": "b"}
    return bmc


class BmcFilledCountTests(unittest.TestCase):
    def test_counts_only_nonempty_strings(self):
        bmc = _valid_bmc("內容")
        bmc["客群"] = ""
        bmc["通路"] = 123
        self.assertEqual(br._bmc_filled_count(bmc), 7)

    def test_quant_cells_require_valid_structure_not_just_presence(self):
        bmc = _valid_bmc("內容")
        bmc["收益流"] = "退化成純文字"
        self.assertEqual(br._bmc_filled_count(bmc), 8)

    def test_empty_dict_is_zero(self):
        self.assertEqual(br._bmc_filled_count({}), 0)
        self.assertEqual(br._bmc_filled_count(None), 0)


class UnitEconomicsTests(unittest.TestCase):
    def test_computes_margin_and_viability(self):
        ue = br._unit_economics(_valid_bmc("x", revenue=5000, cost=2000))
        self.assertEqual(ue["monthly_margin_twd"], 3000)
        self.assertTrue(ue["is_viable"])

    def test_not_viable_when_cost_exceeds_revenue(self):
        ue = br._unit_economics(_valid_bmc("x", revenue=1000, cost=4000))
        self.assertEqual(ue["monthly_margin_twd"], -3000)
        self.assertFalse(ue["is_viable"])

    def test_handles_missing_bmc_gracefully(self):
        ue = br._unit_economics({})
        self.assertEqual(ue["monthly_margin_twd"], 0)
        self.assertFalse(ue["is_viable"])


class ComputeComparisonTests(unittest.TestCase):
    def _run_data(self, **overrides):
        base = {
            "topic": "測試主題",
            "candidate_jobs": [
                {"id": "job1", "job_statement": "情境一", "interview_pool": [{"id": "u1", "name": "陳先生"}]},
                {"id": "job2", "job_statement": "情境二", "interview_pool": [{"id": "u2", "name": "林小姐"}]},
                {"id": "job3", "job_statement": "情境三", "interview_pool": [{"id": "u3", "name": "王先生"}]},
            ],
            "job_evidence": [
                {"job_id": "job1", "supported": True}, {"job_id": "job2", "supported": True},
                {"job_id": "job3", "supported": False},
            ],
            "selected_job_id": "job1",
            "personas": [
                {"id": "p1", "name": "A", "domain": "建築與都市規劃"},
                {"id": "p2", "name": "B", "domain": "餐飲內場管理"},
                {"id": "p3", "name": "C", "domain": "遊戲設計"},
            ],
            "ideas": [{"id": "p1"}, {"id": "p2"}, {"id": "p3"}],
            "winner_idea": {"id": "p1", "sources": [{"url": "u1"}, {"url": "u2"}], "bmc": _valid_bmc("贏家自己想的")},
            "dfv_scores": [
                {"idea_id": "p1", "lens_id": "desirability"}, {"idea_id": "p2", "lens_id": "desirability"},
                {"idea_id": "p3", "lens_id": "desirability"}, {"idea_id": "p1", "lens_id": "feasibility"},
                {"idea_id": "p2", "lens_id": "feasibility"}, {"idea_id": "p3", "lens_id": "feasibility"},
                {"idea_id": "p1", "lens_id": "viability"}, {"idea_id": "p2", "lens_id": "viability"},
                {"idea_id": "p3", "lens_id": "viability"},
            ],
            "idea_diversity": {"avg_distance": 0.42},
            "human_qa_log": [{}],
            "baseline": {
                "proposal": {"sources": [{"url": "b1"}], "bmc": {k: "x" for k in BMC_KEYS[:5]}},
                "metrics": {"cost_usd": 0.003},
            },
            "user_evaluation": {
                "agent_avg_score": 5.67, "baseline_avg_score": 3.33, "score_delta": 2.34,
                "evaluations": [{}, {}, {}],
            },
            "total_cost_usd": 0.38,
            "final_verdict": "agent 方案較優",
        }
        base.update(overrides)
        return base

    def test_real_sources_uses_winner_idea_not_all_ideas(self):
        c = br.compute_comparison(self._run_data())
        self.assertEqual(c["real_sources"]["agent_total"], 2)
        self.assertEqual(c["real_sources"]["baseline"], 1)

    def test_bmc_completeness_reads_winner_ideas_own_bmc(self):
        # BMC 不再是全場共用一份——每個 idea 自己設計自己的，這裡「agent
        # 端」的完整度要讀 winner_idea 自己的 bmc，不是某個共用欄位。
        c = br.compute_comparison(self._run_data())
        self.assertEqual(c["bmc_completeness"]["agent_winner"], "9/9")
        # fixture 的 baseline bmc 只填了前 5 格文字，其中「收益流」被填成
        # 純字串（不是量化物件），所以只有 4 格算數。
        self.assertEqual(c["bmc_completeness"]["baseline"], "4/9")

    def test_dfv_scoring_counts_lenses_times_ideas(self):
        c = br.compute_comparison(self._run_data())
        self.assertIn("9 筆", c["dfv_scoring"]["agent"])
        self.assertIn("3 面向", c["dfv_scoring"]["agent"])
        self.assertIn("3 個 idea", c["dfv_scoring"]["agent"])

    def test_idea_diversity_baseline_is_explicitly_not_applicable(self):
        c = br.compute_comparison(self._run_data())
        self.assertIn("N/A", c["idea_diversity"]["baseline"])
        self.assertEqual(c["idea_diversity"]["agent"], 0.42)

    def test_team_formation_reports_disparate_domain_count(self):
        # 職能是從 derive_company_domains() 衍生的（扣著公司實際能力，
        # 不是跟公司無關的任意領域），這裡驗證回放頁報告的是「不重複職能
        # 數」，不是單純參與者人數。
        c = br.compute_comparison(self._run_data())
        self.assertIn("3", c["team_formation"]["agent"])
        self.assertIn("3 個不重複職能", c["team_formation"]["agent"])
        self.assertIn("N/A", c["team_formation"]["baseline"])

    def test_candidate_job_coverage_reports_hypothesis_count_and_selection(self):
        c = br.compute_comparison(self._run_data())
        self.assertIn("3 個候選 job", c["candidate_job_coverage"]["agent"])
        self.assertIn("2 個訪談後判定成立", c["candidate_job_coverage"]["agent"])
        self.assertIn("job1", c["candidate_job_coverage"]["agent"])
        self.assertIn("N/A", c["candidate_job_coverage"]["baseline"])

    def test_evaluator_scores_pulled_from_user_evaluation(self):
        c = br.compute_comparison(self._run_data())
        self.assertEqual(c["evaluator_scores"]["agent_avg"], 5.67)
        self.assertEqual(c["evaluator_scores"]["baseline_avg"], 3.33)
        self.assertEqual(c["evaluator_scores"]["delta"], 2.34)
        self.assertEqual(c["evaluator_scores"]["evaluator_count"], 3)

    def test_handles_missing_optional_fields_gracefully(self):
        c = br.compute_comparison({"topic": "x"})
        self.assertEqual(c["real_sources"]["agent_total"], 0)
        self.assertEqual(c["dfv_scoring"]["agent"], "0 筆（0 面向 × 0 個 idea）")
        self.assertIsNone(c["evaluator_scores"]["agent_avg"])


class SumDisplayCostTests(unittest.TestCase):
    def test_excludes_evaluate_final_outputs_mid_snapshots(self):
        events = [
            {"action": "evaluate_final_outputs", "cost_usd": 0.01},
            {"action": "evaluate_final_outputs", "cost_usd": 0.02},
            {"action": "user_evaluation_summary", "cost_usd": 0.02},
            {"action": "generate_prototype", "cost_usd": 0.01},
        ]
        # evaluate_final_outputs 排除（跟 stage12 的 system_research 是
        # 同一種 mid-snapshot 模式），user_evaluation_summary／
        # generate_prototype 照算。
        self.assertAlmostEqual(br.sum_display_cost(events), 0.02 + 0.01)

    def test_interview_turn_keeps_only_last_round_per_interviewee(self):
        # 真實跑測撞到的坑：同一位受訪者 5 輪 switch 訪談共用一個
        # invocation，每輪 cost_usd 是累計數，不是單輪增量——整批排除會把
        # 訪談成本整個漏算，只留每位受訪者「round 最大」的那一則才是這次
        # 訪談的真實總額。
        events = [
            {"action": "interview_turn", "extra": {"user_name": "陳先生", "round": 1}, "cost_usd": 0.002},
            {"action": "interview_turn", "extra": {"user_name": "陳先生", "round": 2}, "cost_usd": 0.006},
            {"action": "interview_turn", "extra": {"user_name": "陳先生", "round": 3}, "cost_usd": 0.010},
            {"action": "interview_turn", "extra": {"user_name": "林小姐", "round": 1}, "cost_usd": 0.003},
            {"action": "interview_turn", "extra": {"user_name": "林小姐", "round": 2}, "cost_usd": 0.007},
            {"action": "generate_prototype", "cost_usd": 0.01},
        ]
        self.assertAlmostEqual(br.sum_display_cost(events), 0.010 + 0.007 + 0.01)

    def test_interview_turn_groups_by_user_name_not_colliding_user_id(self):
        # 真實跑測撞到的坑：不同候選 job 的訪談對象 id 各自從 u1/u2 起算
        # 會撞號（研究員 A 是 job1 的 u1，研究員 B 是 job2 的 u1）——用
        # user_name 分組才不會把兩位不同受訪者的訪談成本混在一起算成同
        # 一組的「最後一輪」。
        events = [
            {"action": "interview_turn", "extra": {"user_id": "u1", "user_name": "陳先生", "round": 1}, "cost_usd": 0.002},
            {"action": "interview_turn", "extra": {"user_id": "u1", "user_name": "陳先生", "round": 2}, "cost_usd": 0.005},
            {"action": "interview_turn", "extra": {"user_id": "u1", "user_name": "林小姐", "round": 1}, "cost_usd": 0.003},
            {"action": "interview_turn", "extra": {"user_id": "u1", "user_name": "林小姐", "round": 2}, "cost_usd": 0.008},
        ]
        self.assertAlmostEqual(br.sum_display_cost(events), 0.005 + 0.008)


if __name__ == "__main__":
    unittest.main()
