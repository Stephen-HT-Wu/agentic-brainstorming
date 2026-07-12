import unittest

import build_replay as br


BMC_KEYS = ["客群", "價值主張", "通路", "顧客關係", "收益流", "關鍵資源", "關鍵活動", "關鍵夥伴", "成本結構"]


class BmcFilledCountTests(unittest.TestCase):
    def test_counts_only_nonempty_strings(self):
        bmc = {k: "內容" for k in BMC_KEYS}
        bmc["客群"] = ""  # 空字串不算
        bmc["通路"] = 123  # 非字串不算
        self.assertEqual(br._bmc_filled_count(bmc), 7)

    def test_empty_dict_is_zero(self):
        self.assertEqual(br._bmc_filled_count({}), 0)
        self.assertEqual(br._bmc_filled_count(None), 0)


class ComputeComparisonTests(unittest.TestCase):
    def _run_data(self, **overrides):
        base = {
            "topic": "測試主題",
            "persona_count": 3,
            "idea_pool_versions": [
                {"persona_id": "a", "proposal_after": {
                    "sources": [{"url": "u1"}, {"url": "u2"}],
                    "bmc": {k: "x" for k in BMC_KEYS},
                    "memory_refs": ["m1", "m2"],
                }},
                {"persona_id": "b", "proposal_after": {
                    "sources": [{"url": "u3"}],
                    "bmc": {k: "x" for k in BMC_KEYS},
                    "memory_refs": [],
                }},
            ],
            "prototypes": [{"persona_id": "a"}],
            "recall_hits_total": 5,
            "diversity_after_review": {"avg_distance": 0.31},
            "master_critiques": [{}, {}, {}],
            "three_lens_checks": [{}] * 6,
            "human_qa_log": [{}],
            "facilitator_log": [{}] * 4,
            "baseline": {
                "proposal": {"sources": [{"url": "b1"}], "bmc": {k: "x" for k in BMC_KEYS[:5]}},
                "metrics": {"cost_usd": 0.003},
            },
            "total_cost_usd": 0.55,
        }
        base.update(overrides)
        return base

    def test_real_sources_averages_across_agent_proposals(self):
        c = br.compute_comparison(self._run_data())
        self.assertEqual(c["real_sources"]["agent_avg"], 1.5)  # (2+1)/2
        self.assertEqual(c["real_sources"]["agent_total"], 3)
        self.assertEqual(c["real_sources"]["baseline"], 1)

    def test_revision_count_sums_versions_and_prototypes(self):
        c = br.compute_comparison(self._run_data())
        self.assertEqual(c["revision_count"]["agent"], 2 + 1)  # 2 idea_pool_versions + 1 prototype
        self.assertEqual(c["revision_count"]["baseline"], 0)

    def test_cross_round_memory_counts_actual_citations_not_just_hits(self):
        c = br.compute_comparison(self._run_data())
        # a 引用了 2 筆、b 引用 0 筆 memory_refs -> 實際引用 2，即使 recall_hits_total 回報 5
        self.assertEqual(c["cross_round_memory"]["agent_actually_cited"], 2)
        self.assertEqual(c["cross_round_memory"]["agent_recall_hits"], 5)

    def test_diversity_baseline_is_explicitly_not_applicable(self):
        c = br.compute_comparison(self._run_data())
        self.assertIn("N/A", c["diversity"]["baseline"])
        self.assertEqual(c["diversity"]["agent"], 0.31)

    def test_handles_zero_agent_proposals_without_crashing(self):
        c = br.compute_comparison(self._run_data(idea_pool_versions=[]))
        self.assertEqual(c["real_sources"]["agent_avg"], 0)


class SumDisplayCostTests(unittest.TestCase):
    """真實資料踩到的坑：conduct_interviews 每輪訪談的 emit_event 記的是
    『這次節點呼叫目前為止的累計花費』，不是這筆事件單獨的花費——直接對
    events.jsonl 全部事件的 cost_usd 加總會嚴重高估。"""

    def test_excludes_cumulative_snapshot_events_from_total(self):
        events = [
            {"action": "collect", "cost_usd": 0.0},
            {"action": "interview_turn", "cost_usd": 0.001},   # 快照
            {"action": "interview_turn", "cost_usd": 0.003},   # 快照（累計）
            {"action": "interview_turn", "cost_usd": 0.006},   # 快照（累計）
            {"action": "conduct_interviews", "cost_usd": 0.006},  # 總結，等於最後一筆快照
            {"action": "draft_proposal", "cost_usd": 0.02},
        ]
        # 正確總額 = collect(0) + conduct_interviews 總結(0.006) + draft_proposal(0.02)
        self.assertAlmostEqual(br.sum_display_cost(events), 0.026)

    def test_naive_sum_would_overcount(self):
        events = [
            {"action": "interview_turn", "cost_usd": 0.001},
            {"action": "interview_turn", "cost_usd": 0.003},
            {"action": "conduct_interviews", "cost_usd": 0.003},
        ]
        naive_total = sum(e["cost_usd"] for e in events)
        self.assertGreater(naive_total, br.sum_display_cost(events))
        self.assertAlmostEqual(br.sum_display_cost(events), 0.003)

    def test_matches_real_run_ground_truth(self):
        """用真實跑測資料鎖住這個修正：pipeline 自己印出的總成本是 $0.6680，
        原始 events.jsonl 全部加總會變成 $0.8741（高估約 31%）。"""
        events = [
            {"action": "interview_turn", "cost_usd": 0.00093},
            {"action": "interview_turn", "cost_usd": 0.002693},
            {"action": "interview_turn", "cost_usd": 0.004962},
            {"action": "conduct_interviews", "cost_usd": 0.004962},
            {"action": "draft_proposal", "cost_usd": 0.663038},
        ]
        self.assertAlmostEqual(br.sum_display_cost(events), 0.668, places=3)


class AttachDetailsTests(unittest.TestCase):
    """使用者要求「點每一步都能看到做了什麼功課、形成的意見是什麼」——present／
    baseline／原型事件要串上 run_data 裡的完整資料，且不能洩漏本機絕對路徑。"""

    def _run_data(self):
        return {
            "idea_pool_versions": [{
                "persona_id": "a", "persona_name": "林美華",
                "before_title": "初版標題",
                "proposal_after": {"title": "修正版標題", "bmc": {}},
            }],
            "prototypes": [{
                "persona_id": "mei", "persona_name": "林美華",
                "html_path": "/Users/someone/demo_workspace/outputs/prototypes/demo-sample-round2-mei.html",
                "landing_page": {"headline": "h"},
            }],
            "baseline": {"proposal": {"title": "baseline 標題"}},
        }

    def test_present_event_gets_full_proposal_matched_by_persona_name(self):
        events = [{"role": "persona:林美華", "action": "present"}]
        br._attach_details(events, self._run_data())
        self.assertEqual(events[0]["detail"]["kind"], "proposal")
        self.assertEqual(events[0]["detail"]["proposal"]["title"], "修正版標題")
        self.assertIn("初版標題", events[0]["detail"]["note"])

    def test_baseline_event_gets_baseline_proposal(self):
        events = [{"role": "baseline", "action": "baseline"}]
        br._attach_details(events, self._run_data())
        self.assertEqual(events[0]["detail"]["proposal"]["title"], "baseline 標題")

    def test_generate_prototype_event_gets_prototype_detail(self):
        events = [{"role": "persona:林美華", "action": "generate_prototype"}]
        br._attach_details(events, self._run_data())
        self.assertEqual(events[0]["detail"]["kind"], "prototype")
        self.assertEqual(events[0]["detail"]["prototype"]["landing_page"]["headline"], "h")

    def test_attached_prototype_detail_also_has_leaked_path_scrubbed(self):
        """不只 extra.html_path，attach 進 detail 的完整 prototype 物件本身的
        html_path 也是同一份本機絕對路徑，兩個地方都要清乾淨。"""
        events = [{"role": "persona:林美華", "action": "generate_prototype"}]
        br._attach_details(events, self._run_data())
        self.assertEqual(events[0]["detail"]["prototype"]["html_path"], "prototype-mei.html")

    def test_leaked_absolute_html_path_replaced_with_clean_relative_path(self):
        """真實踩到的坑：generate_prototype 的 extra.html_path 是本機絕對路徑
        （demo_workspace/outputs/prototypes/...），直接內嵌進公開的 replay.html
        會洩漏本機使用者名稱與資料夾結構。"""
        events = [{
            "role": "persona:林美華", "action": "generate_prototype",
            "extra": {"html_path": "/Users/stephen/agentic-brainstorming/practice/stage10/demo_workspace/outputs/prototypes/demo-sample-round2-mei.html"},
        }]
        br._attach_details(events, self._run_data())
        self.assertEqual(events[0]["extra"]["html_path"], "prototype-mei.html")
        self.assertNotIn("/Users/", events[0]["extra"]["html_path"])

    def test_unmatched_persona_falls_back_to_basename_not_full_path(self):
        events = [{
            "role": "persona:不存在的人", "action": "generate_prototype",
            "extra": {"html_path": "/Users/stephen/secret/prototypes/x.html"},
        }]
        br._attach_details(events, self._run_data())
        self.assertEqual(events[0]["extra"]["html_path"], "x.html")


class RoleColorTests(unittest.TestCase):
    def test_special_roles_get_fixed_colors(self):
        cache = {}
        self.assertEqual(br._role_color("facilitator", cache), "#ffb454")
        self.assertEqual(br._role_color("baseline", cache), "#8a93a6")
        self.assertEqual(br._role_color("master:技術大師", cache), "#9d7bff")

    def test_persona_roles_get_distinct_cached_colors(self):
        cache = {}
        c1 = br._role_color("persona:林美華", cache)
        c2 = br._role_color("persona:陳建宏", cache)
        c3 = br._role_color("persona:林美華", cache)  # 第二次查同一個人要拿到一樣的顏色
        self.assertNotEqual(c1, c2)
        self.assertEqual(c1, c3)


class BuildReplayHtmlTests(unittest.TestCase):
    def test_output_is_self_contained_html_with_embedded_data(self):
        events = [{"ts": "t", "role": "persona:A", "action": "collect", "node": "collect", "summary": "s", "cost_usd": 0.01}]
        comparison = {
            "real_sources": {"baseline": 0, "agent_avg": 1}, "bmc_completeness": {"baseline": "9/9", "agent_all": "9/9"},
            "diversity": {"baseline": "N/A", "agent": 0.3}, "revision_count": {"baseline": 0, "agent": 1},
            "cross_round_memory": {"baseline": 0, "agent_actually_cited": 0, "agent_recall_hits": 0},
            "cost": {"baseline": 0.001, "agent_total": 0.5, "agent_persona_count": 1},
            "master_critiques_count": 0, "three_lens_checks_count": 0, "human_qa_count": 0, "facilitator_rounds": 0,
        }
        html = br.build_replay_html(events, comparison, "測試")
        self.assertTrue(html.startswith("<!doctype html>"))
        self.assertNotIn("fetch(", html)  # 零依賴：不能靠 fetch() 讀外部檔案
        self.assertIn("const EVENTS = [", html)
        self.assertIn("測試", html)


if __name__ == "__main__":
    unittest.main()
