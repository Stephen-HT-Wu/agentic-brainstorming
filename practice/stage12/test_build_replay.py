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
            "personas": [{"id": "p1", "name": "A"}, {"id": "p2", "name": "B"}, {"id": "p3", "name": "C"}],
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

    def test_team_formation_counts_personas(self):
        c = br.compute_comparison(self._run_data())
        self.assertIn("3", c["team_formation"]["agent"])
        self.assertIn("N/A", c["team_formation"]["baseline"])

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
    def test_excludes_snapshot_actions(self):
        events = [
            {"action": "interview_turn", "cost_usd": 0.01},
            {"action": "system_research", "cost_usd": 0.02},
            {"action": "generate_personas", "cost_usd": 0.05},
            {"action": "generate_prototype", "cost_usd": 0.01},
        ]
        # interview_turn／system_research 排除，其餘照計；跟 stage12 的
        # generate_prototype 是單次生成、應該照算——跟 stage9/10/11 的排除
        # 清單不同，這裡鎖住這個差異。
        self.assertAlmostEqual(br.sum_display_cost(events), 0.05 + 0.01)


if __name__ == "__main__":
    unittest.main()
