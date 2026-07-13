import unittest

import build_replay as br


BMC_KEYS = ["客群", "價值主張", "通路", "顧客關係", "收益流", "關鍵資源", "關鍵活動", "關鍵夥伴", "成本結構"]
QUANT_BMC_KEYS = {"收益流", "成本結構"}


def _valid_bmc(text="x", revenue=1000, cost=1000):
    """跟 stage9/test_graph.py 的同名 helper 是同一個道理——BMC 量化後
    「收益流」「成本結構」是結構化物件，不是純字串。"""
    bmc = {k: text for k in BMC_KEYS}
    bmc["收益流"] = {"narrative": text, "monthly_estimate_twd": revenue, "basis": "b"}
    bmc["成本結構"] = {"narrative": text, "monthly_estimate_twd": cost, "basis": "b"}
    return bmc


class BmcFilledCountTests(unittest.TestCase):
    def test_counts_only_nonempty_strings(self):
        bmc = _valid_bmc("內容")
        bmc["客群"] = ""  # 空字串不算
        bmc["通路"] = 123  # 非字串不算
        self.assertEqual(br._bmc_filled_count(bmc), 7)  # 7 個文字格 + 2 個合法量化格 - 2 個被清空的格

    def test_quant_cells_require_valid_structure_not_just_presence(self):
        bmc = _valid_bmc("內容")
        bmc["收益流"] = "退化成純文字"  # 舊格式，不再算數
        self.assertEqual(br._bmc_filled_count(bmc), 8)  # 9 格 - 1 個不合法的收益流

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
            "persona_count": 3,
            "idea_pool_versions": [
                {"persona_id": "a", "proposal_after": {
                    "sources": [{"url": "u1"}, {"url": "u2"}],
                    "bmc": _valid_bmc("x"),
                    "memory_refs": ["m1", "m2"],
                }},
                {"persona_id": "b", "proposal_after": {
                    "sources": [{"url": "u3"}],
                    "bmc": _valid_bmc("x"),
                    "memory_refs": [],
                }},
            ],
            "prototypes": [{"persona_id": "a"}],
            "recall_hits_total": 5,
            "diversity_after_review": {"avg_distance": 0.31},
            "personas": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
            "co_creation_log": [
                {"persona_id": "b", "built_on_persona_ids": ["a"]},
                {"persona_id": "c", "built_on_persona_ids": ["a", "b"]},
            ],
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

    def test_real_sources_falls_back_to_legacy_average_when_no_final_proposal(self):
        # fixture 的 prototypes[0] 沒有 "after"，也沒有 co_created_proposal
        # ——這是共創重構前的舊 run 資料形狀，要退回舊算法（取
        # idea_pool_versions 平均），不能直接爆掉或回傳假資料。
        c = br.compute_comparison(self._run_data())
        self.assertEqual(c["real_sources"]["agent_total"], 1.5)  # (2+1)/2
        self.assertEqual(c["real_sources"]["baseline"], 1)

    def test_real_sources_uses_single_final_proposal_when_available(self):
        # 使用者回報「六個可量化的差異未更新」的根因：改成共創收斂後，
        # agent 端應該只看『一個』真正的最終提案（原型測試後版本），不是
        # 4 份共創編輯前的個別提案取平均——這裡鎖住新行為。
        c = br.compute_comparison(self._run_data(prototypes=[{
            "persona_id": "co_created",
            "after": {
                "sources": [{"url": "u1"}, {"url": "u2"}, {"url": "u3"}],
                "bmc": _valid_bmc("x"),
                "memory_refs": ["m1"],
            },
        }]))
        self.assertEqual(c["real_sources"]["agent_total"], 3)
        self.assertEqual(c["bmc_completeness"]["agent_all"], "9/9")
        self.assertEqual(c["cross_round_memory"]["agent_actually_cited"], 1)

    def test_real_sources_uses_co_created_proposal_when_no_prototype_yet(self):
        # 原型測試階段還沒跑完（例如即時畫面還在進行中）時，退回共創草稿
        # 本身，不是等到有 prototypes 才顯示資料。
        c = br.compute_comparison(self._run_data(
            prototypes=[],
            co_created_proposal={
                "sources": [{"url": "u1"}],
                "bmc": _valid_bmc("x"),
                "memory_refs": [],
            },
        ))
        self.assertEqual(c["real_sources"]["agent_total"], 1)

    def test_revision_count_sums_versions_and_prototypes(self):
        c = br.compute_comparison(self._run_data())
        # 2 idea_pool_versions + 2 co_creation_log 輪次 + 1 prototype
        self.assertEqual(c["revision_count"]["agent"], 2 + 2 + 1)
        self.assertEqual(c["revision_count"]["baseline"], 0)

    def test_cross_round_memory_counts_actual_citations_not_just_hits(self):
        c = br.compute_comparison(self._run_data())
        # a 引用了 2 筆、b 引用 0 筆 memory_refs -> 實際引用 2，即使 recall_hits_total 回報 5
        self.assertEqual(c["cross_round_memory"]["agent_actually_cited"], 2)
        self.assertEqual(c["cross_round_memory"]["agent_recall_hits"], 5)

    def test_cross_domain_integration_baseline_is_explicitly_not_applicable(self):
        c = br.compute_comparison(self._run_data())
        self.assertIn("N/A", c["cross_domain_integration"]["baseline"])

    def test_cross_domain_integration_counts_seed_plus_referenced_editors(self):
        # 種子 persona 是沒有 co_creation_log 條目的那一個（a）——算 1 位真實
        # 整合；b/c 都在某一輪的 built_on_persona_ids 裡出現過，也各算 1 位。
        # a 自己雖然也在 built_on_persona_ids 裡，但那不重複計算（集合聯集）。
        c = br.compute_comparison(self._run_data())
        self.assertIn("2/3", c["cross_domain_integration"]["agent"])

    def test_cross_domain_integration_falls_back_gracefully_with_no_co_creation_log(self):
        c = br.compute_comparison(self._run_data(co_creation_log=[], personas=[]))
        self.assertIn("N/A", c["cross_domain_integration"]["baseline"])
        self.assertIn("0/1", c["cross_domain_integration"]["agent"])

    def test_handles_zero_agent_proposals_without_crashing(self):
        c = br.compute_comparison(self._run_data(idea_pool_versions=[]))
        self.assertEqual(c["real_sources"]["agent_total"], 0)

    def test_unit_economics_row_reads_final_proposal_and_baseline(self):
        # 使用者要求 BMC 要能量出可行性——對比表新增的「單位經濟」列要讀
        # 真正的最終提案（跟 real_sources 用同一份 final_proposal 判斷邏輯），
        # 不是 idea_pool_versions 裡收斂前的個別版本。
        c = br.compute_comparison(self._run_data(prototypes=[{
            "persona_id": "co_created",
            "after": {"sources": [], "bmc": _valid_bmc("x", revenue=5000, cost=2000), "memory_refs": []},
        }]))
        self.assertEqual(c["unit_economics"]["agent"]["monthly_margin_twd"], 3000)
        self.assertTrue(c["unit_economics"]["agent"]["is_viable"])
        # baseline fixture 的 bmc 只有 5 格（BMC_KEYS[:5]），"收益流" 是純
        # 字串（不是合法量化物件），所以 baseline 端退回零值、不可行。
        self.assertEqual(c["unit_economics"]["baseline"]["monthly_margin_twd"], 0)
        self.assertFalse(c["unit_economics"]["baseline"]["is_viable"])

    def test_unit_economics_agent_is_none_when_no_final_proposal(self):
        c = br.compute_comparison(self._run_data())
        self.assertIsNone(c["unit_economics"]["agent"])


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

    def test_defaults_to_empty_personas_and_users_when_omitted(self):
        """使用者要求：互評/評分等事件裡到處混著英文 persona_id 跟中文姓名，
        統一解析成姓名要靠內嵌的 PERSONAS／USERS 清單——舊的 run JSON（這個
        功能上線前跑的）沒有這兩個欄位，personas/users 沒給也不能整個崩掉，
        要能退化成空清單（JS 端 personaName() 再退回顯示原始 id）。"""
        html = br.build_replay_html([], {
            "real_sources": {}, "bmc_completeness": {}, "diversity": {}, "revision_count": {},
            "cross_round_memory": {}, "cost": {},
            "master_critiques_count": 0, "three_lens_checks_count": 0, "human_qa_count": 0, "facilitator_rounds": 0,
        }, "測試")
        self.assertIn("const PERSONAS = [];", html)
        self.assertIn("const USERS = [];", html)

    def test_embeds_full_personas_and_users_when_provided(self):
        html = br.build_replay_html(
            [], {
                "real_sources": {}, "bmc_completeness": {}, "diversity": {}, "revision_count": {},
                "cross_round_memory": {}, "cost": {},
                "master_critiques_count": 0, "three_lens_checks_count": 0, "human_qa_count": 0, "facilitator_rounds": 0,
            }, "測試",
            personas=[{"id": "alex", "name": "陳建宏", "role": "技術架構師"}],
            users=[{"id": "commuter", "name": "陳小姐", "age": 32, "context": "通勤族", "pain_points": ["沒時間"], "tone": "直接"}],
        )
        self.assertIn('"id": "alex"', html)
        self.assertIn("技術架構師", html)
        self.assertIn('"id": "commuter"', html)
        self.assertIn("通勤族", html)


if __name__ == "__main__":
    unittest.main()
