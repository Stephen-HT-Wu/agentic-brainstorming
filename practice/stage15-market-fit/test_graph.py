from collections import defaultdict
from unittest import mock
import unittest

import graph


PERSONAS = [
    {"id": "p1", "name": "林美"}, {"id": "p2", "name": "陳亞力克斯"}, {"id": "p3", "name": "周依絲"},
]


def _valid_bmc(text="x", revenue=1000, cost=1000):
    """BMC 量化後「收益流」「成本結構」是結構化物件，不是純字串——跟
    stage9/test_graph.py 的同名 helper 是同一個道理。"""
    bmc = {k: text for k in graph.BMC_KEYS}
    for k in graph.QUANTIFIED_BMC_KEYS:
        bmc[k] = {"narrative": text, "monthly_estimate_twd": revenue if k == "收益流" else cost, "basis": "測試假設"}
    return bmc


class GraphTopologySafetyTests(unittest.TestCase):
    """把 join-safety 從 docstring 宣稱變成自動化回歸測試——stage12
    真實跑測踩到的坑是「兩條長度不同的分支都指到同一個節點」不保證等
    全部前驅完成才觸發。這裡斷言除了 ask_question（HITL 迴圈的意圖式
    「either/or」入口，不是需要等待全部前驅的 fan-in）以外，沒有任何
    節點有兩個以上不同的靜態前驅——stage15-market-fit 拿掉 Discover/
    Define，新增 validate_market_fit（內部依序同步呼叫三個子圖，不是
    三條分支各自指到 pick_winner），這個斷言必須繼續成立。"""

    def test_no_unexpected_multi_predecessor_nodes(self):
        compiled = graph.build_parent_graph(None)
        preds = defaultdict(set)
        for edge in compiled.get_graph().edges:
            preds[edge.target].add(edge.source)
        multi = {node: srcs for node, srcs in preds.items() if len(srcs) > 1}
        self.assertEqual(set(multi.keys()), {"ask_question"})


class ResearchCompetitiveLandscapeTests(unittest.TestCase):
    """取代整個 Discover/Define——真實踩過舊版五力分析空泛問題後設計的
    節點，重點驗證：(1) 找到真實競品時正確解析；(2) source_url 沒有出現
    在這次 web_search() 結果裡的條目會被丟棄，不信任 LLM 自稱；
    (3) 一筆都驗證不過時誠實標記 used_fallback。"""

    def _state(self):
        return {"topic": "APP建立會員付費訂閱加值功能機制", "company": "某新聞媒體"}

    def _search_hits(self):
        return [
            {"title": "A 服務介紹", "url": "https://a.example.com", "snippet": "A 提供訂閱制內容"},
            {"title": "B 服務介紹", "url": "https://b.example.com", "snippet": "B 提供付費會員機制"},
        ]

    def test_happy_path_keeps_only_verified_competitors(self):
        llm_response = graph.json.dumps({"competitors": [
            {"competitor_name": "A", "feature_description": "A 做訂閱分級", "source_url": "https://a.example.com"},
            {"competitor_name": "捏造的公司", "feature_description": "編造的功能", "source_url": "https://not-real.example.com"},
        ]})
        with mock.patch.object(graph, "web_search", return_value=self._search_hits()), \
             mock.patch.object(graph, "call_llm", return_value=llm_response), \
             mock.patch.object(graph, "emit_event"):
            result = graph.research_competitive_landscape(self._state())
        names = [c["competitor_name"] for c in result["competitive_landscape"]]
        self.assertEqual(names, ["A"])
        self.assertFalse(result["used_fallback_competitive_landscape"])

    def test_all_competitors_unverified_falls_back(self):
        llm_response = graph.json.dumps({"competitors": [
            {"competitor_name": "捏造", "feature_description": "編造", "source_url": "https://not-real.example.com"},
        ]})
        with mock.patch.object(graph, "web_search", return_value=self._search_hits()), \
             mock.patch.object(graph, "call_llm", return_value=llm_response), \
             mock.patch.object(graph, "emit_event"):
            result = graph.research_competitive_landscape(self._state())
        self.assertTrue(result["used_fallback_competitive_landscape"])
        self.assertEqual(len(result["competitive_landscape"]), 1)  # 保底那一筆

    def test_unparseable_llm_output_falls_back(self):
        with mock.patch.object(graph, "web_search", return_value=self._search_hits()), \
             mock.patch.object(graph, "call_llm", return_value="不是合法 JSON"), \
             mock.patch.object(graph, "emit_event"):
            result = graph.research_competitive_landscape(self._state())
        self.assertTrue(result["used_fallback_competitive_landscape"])


class DeriveCompanyDomainsTests(unittest.TestCase):
    def test_happy_path_derives_distinct_domains_from_company(self):
        llm_response = graph.json.dumps({"domains": ["APP工程", "在地異業合作開發", "會員數據/CRM", "短影音後製", "廣告業務"]})
        with mock.patch.object(graph, "call_llm", return_value=llm_response), \
             mock.patch.object(graph, "emit_event"):
            domains, used_fallback = graph.derive_company_domains("公司定位描述", "策略方向", "競品摘要")
        self.assertEqual(len(domains), 5)
        self.assertFalse(used_fallback)

    def test_dedupes_repeated_domains_from_llm(self):
        llm_response = graph.json.dumps({"domains": ["APP工程", "APP工程", "會員數據/CRM"]})
        with mock.patch.object(graph, "call_llm", return_value=llm_response), \
             mock.patch.object(graph, "emit_event"):
            domains, used_fallback = graph.derive_company_domains("公司定位描述", "策略方向", "競品摘要")
        self.assertEqual(domains, ["APP工程", "會員數據/CRM"])
        self.assertFalse(used_fallback)

    def test_falls_back_to_domain_pool_when_too_few(self):
        llm_response = graph.json.dumps({"domains": ["只有一個"]})
        with mock.patch.object(graph, "call_llm", return_value=llm_response), \
             mock.patch.object(graph, "emit_event"):
            domains, used_fallback = graph.derive_company_domains("公司定位描述", "策略方向", "競品摘要")
        self.assertTrue(used_fallback)
        self.assertTrue(set(domains) <= set(graph.DOMAIN_ARCHETYPE_POOL))

    def test_falls_back_when_llm_output_unparseable(self):
        with mock.patch.object(graph, "call_llm", return_value="不是合法 JSON"), \
             mock.patch.object(graph, "emit_event"):
            domains, used_fallback = graph.derive_company_domains("公司定位描述", "策略方向", "競品摘要")
        self.assertTrue(used_fallback)
        self.assertGreaterEqual(len(domains), 2)


class CompetitiveSummaryTests(unittest.TestCase):
    def test_formats_real_competitors(self):
        landscape = [{"competitor_name": "A", "feature_description": "做訂閱分級"}]
        summary = graph._competitive_summary(landscape)
        self.assertIn("A", summary)
        self.assertIn("做訂閱分級", summary)

    def test_fallback_entry_produces_honest_placeholder(self):
        landscape = [{"competitor_name": "（系統保底）", "feature_description": "本次未能取得具體競品資訊。"}]
        summary = graph._competitive_summary(landscape)
        self.assertIn("未能取得具體競品資訊", summary)
        self.assertNotIn("（系統保底）", summary)


class AssemblePersonaTeamTests(unittest.TestCase):
    def _state(self):
        return {
            "company": "公司定位描述", "topic": "策略方向",
            "competitive_landscape": [{"competitor_name": "A", "feature_description": "做訂閱分級"}],
        }

    def test_happy_path_produces_disparate_domain_personas(self):
        domains = graph.DOMAIN_ARCHETYPE_POOL[: graph.N_PERSONAS]
        result_personas = [
            {"id": f"p{n}", "name": f"人{n}", "domain": d} for n, d in enumerate(domains, 1)
        ]
        with mock.patch.object(graph, "derive_company_domains", return_value=(domains, False)), \
             mock.patch.object(graph, "persona_team_panel_graph") as mock_panel, \
             mock.patch.object(graph, "emit_event"):
            mock_panel.invoke.return_value = {"personas": result_personas}
            result = graph.assemble_persona_team(self._state())
        self.assertEqual(len(result["personas"]), graph.N_PERSONAS)
        self.assertFalse(result["used_fallback_personas"])
        found_domains = [p["domain"] for p in result["personas"]]
        self.assertEqual(len(found_domains), len(set(found_domains)))

    def test_falls_back_when_too_few_personas(self):
        fallback = [{"id": "p1", "name": "林美"}, {"id": "p2", "name": "陳亞"}, {"id": "p3", "name": "周依"}]
        with mock.patch.object(graph, "derive_company_domains", return_value=(["職能A", "職能B"], False)), \
             mock.patch.object(graph, "persona_team_panel_graph") as mock_panel, \
             mock.patch.object(graph, "emit_event"), \
             mock.patch.object(graph, "load_personas", return_value=fallback):
            mock_panel.invoke.return_value = {"personas": [{"id": "p1", "name": "只有一位"}]}
            result = graph.assemble_persona_team(self._state())
        self.assertEqual(result["personas"], fallback[: graph.N_PERSONAS])
        self.assertTrue(result["used_fallback_personas"])

    def test_propagates_fallback_flag_from_domain_derivation(self):
        domains = graph.DOMAIN_ARCHETYPE_POOL[: graph.N_PERSONAS]
        result_personas = [
            {"id": f"p{n}", "name": f"人{n}", "domain": d} for n, d in enumerate(domains, 1)
        ]
        with mock.patch.object(graph, "derive_company_domains", return_value=(domains, True)), \
             mock.patch.object(graph, "persona_team_panel_graph") as mock_panel, \
             mock.patch.object(graph, "emit_event"):
            mock_panel.invoke.return_value = {"personas": result_personas}
            result = graph.assemble_persona_team(self._state())
        self.assertTrue(result["used_fallback_personas"])


class GenerateOnePersonaForDomainTests(unittest.TestCase):
    def test_happy_path(self):
        llm_response = graph.json.dumps({
            "name": "陳工程師", "role": "硬體工程師", "background": "十年硬體經驗",
            "focus": ["可靠度"], "style": "務實",
        })
        task = {"domain": "硬體工程", "strategic_directive": "策略方向", "competitive_summary": "摘要", "idx": 1}
        with mock.patch.object(graph, "call_llm", return_value=llm_response), \
             mock.patch.object(graph, "emit_event"):
            result = graph.generate_one_persona_for_domain(task)
        persona = result["personas"][0]
        self.assertEqual(persona["domain"], "硬體工程")
        self.assertEqual(persona["id"], "p1")

    def test_falls_back_to_domain_name_when_llm_output_unparseable(self):
        task = {"domain": "農業供應鏈", "strategic_directive": "策略方向", "competitive_summary": "摘要", "idx": 2}
        with mock.patch.object(graph, "call_llm", return_value="不是合法 JSON"), \
             mock.patch.object(graph, "emit_event"):
            result = graph.generate_one_persona_for_domain(task)
        persona = result["personas"][0]
        self.assertEqual(persona["domain"], "農業供應鏈")
        self.assertTrue(persona["name"])


class FanOutIdeasTests(unittest.TestCase):
    def test_fan_out_covers_every_persona(self):
        state = {
            "topic": "策略方向", "competitive_landscape": [{"competitor_name": "A"}],
            "research_items": [], "personas": [{"id": "p1"}, {"id": "p2"}],
        }
        sends = graph.fan_out_ideas(state)
        self.assertEqual(len(sends), 2)
        self.assertTrue(all(s.node == "draft_one_feature" for s in sends))


class DraftOneFeatureTests(unittest.TestCase):
    def _task(self):
        return {
            "persona": {"id": "p1", "name": "林美", "role": "產品", "background": "b", "focus": ["f"], "style": "s"},
            "strategic_directive": "APP建立會員付費訂閱加值功能機制",
            "competitive_landscape": [{"competitor_name": "A", "feature_description": "做訂閱分級"}],
            "research_items": [],
        }

    def test_feature_gets_own_bmc_and_new_schema_fields(self):
        mock_response = graph.json.dumps({
            "title": "T", "summary": "S", "rationale": "R",
            "target_segment": "重度使用者", "monetization_mechanism": "訂閱分級",
            "differentiation_vs_competitors": "比 A 多了個人化推薦",
            "sources": [], "bmc": _valid_bmc("自己想的"),
        })
        with mock.patch.object(graph, "call_llm", return_value=mock_response), \
             mock.patch.object(graph, "emit_event"):
            result = graph.draft_one_feature(self._task())
        idea = result["ideas"][0]
        self.assertEqual(idea["id"], "p1")
        self.assertEqual(idea["persona_name"], "林美")
        self.assertEqual(idea["target_segment"], "重度使用者")
        self.assertEqual(idea["monetization_mechanism"], "訂閱分級")
        self.assertIn("A", idea["differentiation_vs_competitors"])
        self.assertEqual(set(idea["bmc"].keys()), set(graph.BMC_KEYS))

    def test_missing_bmc_from_llm_still_produces_structurally_valid_default(self):
        mock_response = graph.json.dumps({
            "title": "T", "summary": "S", "rationale": "R",
            "target_segment": "A", "monetization_mechanism": "B", "differentiation_vs_competitors": "C",
        })
        with mock.patch.object(graph, "call_llm", return_value=mock_response), \
             mock.patch.object(graph, "emit_event"):
            result = graph.draft_one_feature(self._task())
        idea = result["ideas"][0]
        self.assertIn("bmc", idea)
        self.assertEqual(set(idea["bmc"].keys()), set(graph.BMC_KEYS))


class FanOutSurveyStrataTests(unittest.TestCase):
    def test_fan_out_covers_every_fixed_stratum(self):
        state = {
            "candidate_features": [{"id": "p1", "title": "T"}], "n_respondents": 5,
            "survey_results": [],
        }
        sends = graph.fan_out_survey_strata(state)
        self.assertEqual(len(sends), len(graph.SURVEY_STRATA))
        self.assertTrue(all(s.node == "survey_one_stratum" for s in sends))
        stratum_ids = {s.arg["stratum"]["id"] for s in sends}
        self.assertEqual(stratum_ids, {s["id"] for s in graph.SURVEY_STRATA})
        self.assertTrue(all(s.arg["n_respondents"] == 5 for s in sends))


class SurveyOneStratumTests(unittest.TestCase):
    def _task(self):
        return {
            "stratum": {"id": "s1", "label": "18-24歲／女性／學生"},
            "candidate_features": [{"id": "p1", "title": "功能一"}, {"id": "p2", "title": "功能二"}],
            "n_respondents": 8,
        }

    def test_happy_path_returns_per_feature_stats(self):
        llm_response = graph.json.dumps({"features": [
            {"feature_id": "p1", "purchase_intent_pct": 62.5, "differentiation_pct": 40.0, "sample_quote": "（模擬）引述一"},
            {"feature_id": "p2", "purchase_intent_pct": 20, "differentiation_pct": 10.0, "sample_quote": ""},
        ]})
        with mock.patch.object(graph, "call_llm", return_value=llm_response), \
             mock.patch.object(graph, "emit_event"):
            result = graph.survey_one_stratum(self._task())
        self.assertEqual(len(result["survey_results"]), 1)
        entry = result["survey_results"][0]
        self.assertEqual(entry["stratum_id"], "s1")
        self.assertEqual(entry["n_simulated"], 8)
        self.assertFalse(entry["used_fallback"])
        stats_by_feature = {j["feature_id"]: j for j in entry["feature_stats"]}
        self.assertEqual(stats_by_feature["p1"]["purchase_intent_pct"], 62.5)
        self.assertEqual(stats_by_feature["p1"]["differentiation_pct"], 40.0)
        self.assertEqual(stats_by_feature["p1"]["sample_quote"], "（模擬）引述一")

    def test_missing_feature_in_llm_output_falls_back_to_midpoint(self):
        llm_response = graph.json.dumps({"features": [
            {"feature_id": "p1", "purchase_intent_pct": 50, "differentiation_pct": 50, "sample_quote": ""},
        ]})
        with mock.patch.object(graph, "call_llm", return_value=llm_response), \
             mock.patch.object(graph, "emit_event"):
            result = graph.survey_one_stratum(self._task())
        entry = result["survey_results"][0]
        self.assertTrue(entry["used_fallback"])
        stats_by_feature = {j["feature_id"]: j for j in entry["feature_stats"]}
        self.assertEqual(stats_by_feature["p2"]["purchase_intent_pct"], 50.0)
        self.assertEqual(stats_by_feature["p2"]["differentiation_pct"], 50.0)

    def test_unparseable_llm_output_falls_back_for_all_features(self):
        with mock.patch.object(graph, "call_llm", return_value="不是合法 JSON"), \
             mock.patch.object(graph, "emit_event"):
            result = graph.survey_one_stratum(self._task())
        entry = result["survey_results"][0]
        self.assertTrue(entry["used_fallback"])
        self.assertEqual(len(entry["feature_stats"]), 2)

    def test_out_of_range_values_are_clamped(self):
        llm_response = graph.json.dumps({"features": [
            {"feature_id": "p1", "purchase_intent_pct": 250, "differentiation_pct": 99, "sample_quote": ""},
            {"feature_id": "p2", "purchase_intent_pct": -10, "differentiation_pct": -5, "sample_quote": ""},
        ]})
        with mock.patch.object(graph, "call_llm", return_value=llm_response), \
             mock.patch.object(graph, "emit_event"):
            result = graph.survey_one_stratum(self._task())
        stats_by_feature = {j["feature_id"]: j for j in result["survey_results"][0]["feature_stats"]}
        self.assertEqual(stats_by_feature["p1"]["purchase_intent_pct"], 100.0)
        self.assertEqual(stats_by_feature["p1"]["differentiation_pct"], 99.0)
        self.assertEqual(stats_by_feature["p2"]["purchase_intent_pct"], 0.0)
        self.assertEqual(stats_by_feature["p2"]["differentiation_pct"], 0.0)


class AggregateSurveyResultsTests(unittest.TestCase):
    def test_weighted_average_across_strata(self):
        candidate_features = [{"id": "p1", "title": "功能一"}]
        survey_results = [
            {"stratum_label": "A", "n_simulated": 2, "feature_stats": [
                {"feature_id": "p1", "purchase_intent_pct": 40.0, "differentiation_pct": 20.0, "sample_quote": "引述A"},
            ]},
            {"stratum_label": "B", "n_simulated": 6, "feature_stats": [
                {"feature_id": "p1", "purchase_intent_pct": 80.0, "differentiation_pct": 60.0, "sample_quote": ""},
            ]},
        ]
        summary = graph._aggregate_survey_results(survey_results, candidate_features)
        self.assertEqual(summary["caveat"], graph.SURVEY_METHOD_CAVEAT)
        self.assertEqual(summary["total_simulated_n"], 8)
        p1 = summary["by_feature"]["p1"]
        # (40*2 + 80*6) / 8 = 70.0；(20*2 + 60*6) / 8 = 50.0
        self.assertEqual(p1["purchase_intent_pct"], 70.0)
        self.assertEqual(p1["differentiation_pct"], 50.0)
        self.assertEqual(p1["sample_quotes"], ["[A] 引述A"])

    def test_at_most_two_quotes_kept_per_feature(self):
        candidate_features = [{"id": "p1", "title": "x"}]
        survey_results = [
            {"stratum_label": f"S{i}", "n_simulated": 1, "feature_stats": [
                {"feature_id": "p1", "purchase_intent_pct": 50.0, "differentiation_pct": 50.0, "sample_quote": f"引述{i}"},
            ]}
            for i in range(4)
        ]
        summary = graph._aggregate_survey_results(survey_results, candidate_features)
        self.assertEqual(len(summary["by_feature"]["p1"]["sample_quotes"]), 2)


class ConceptTestOnePersonTests(unittest.TestCase):
    """簡化版概念測試訪談：固定 2 輪問答＋一次輕量分類（CHEAP_MODEL）判斷
    would_pay，不是 5 輪 JTBD switch——mock simulate_user_answer（不是
    call_llm），這樣 call_llm 就只剩分類那一次呼叫，好斷言。"""

    def _task(self):
        return {
            "feature": {"id": "p1", "title": "功能一", "summary": "摘要"},
            "interviewee": {"id": "u1", "name": "陳先生", "age": 30, "context": "c", "pain_points": [], "tone": "t"},
        }

    def test_happy_path_classifies_would_pay(self):
        classify_response = graph.json.dumps({"would_pay": True, "reaction_summary": "很心動"})
        with mock.patch.object(graph, "simulate_user_answer", return_value="還不錯"), \
             mock.patch.object(graph, "call_llm", return_value=classify_response), \
             mock.patch.object(graph, "emit_event"):
            result = graph.concept_test_one_person(self._task())
        entry = result["concept_test_results"][0]
        self.assertEqual(entry["feature_id"], "p1")
        self.assertEqual(entry["interviewee_name"], "陳先生")
        self.assertEqual(entry["interviewee"], self._task()["interviewee"])
        self.assertTrue(entry["would_pay"])
        self.assertEqual(entry["reaction_summary"], "很心動")
        self.assertEqual(len(entry["transcript"]), 2)

    def test_unparseable_classification_falls_back_to_first_answer_and_false(self):
        with mock.patch.object(graph, "simulate_user_answer", return_value="還好而已"), \
             mock.patch.object(graph, "call_llm", return_value="不是合法 JSON"), \
             mock.patch.object(graph, "emit_event"):
            result = graph.concept_test_one_person(self._task())
        entry = result["concept_test_results"][0]
        self.assertFalse(entry["would_pay"])
        self.assertEqual(entry["reaction_summary"], "還好而已")


class ValidateMarketFitTests(unittest.TestCase):
    """收斂前的快速市場驗證：模式 B 依序同步呼叫三個子圖，不是三條分支
    各自指到 pick_winner（會複製 stage12 的 join bug）。這裡驗證三個
    子圖都被呼叫、彙整結果正確組裝、config 有從 state 讀（沒給就用模組
    預設值）。"""

    def _state(self, **overrides):
        state = {
            "topic": "策略方向", "company": "C",
            "ideas": [{"id": "p1", "title": "功能一"}],
            "competitive_landscape": [{"competitor_name": "A", "feature_description": "做訂閱分級"}],
        }
        state.update(overrides)
        return state

    def test_invokes_three_subgraphs_in_order_and_assembles_result(self):
        survey_panel_result = {"survey_results": [
            {"stratum_id": "s1", "stratum_label": "A", "n_simulated": 8, "used_fallback": False,
             "feature_stats": [{"feature_id": "p1", "purchase_intent_pct": 60.0, "differentiation_pct": 40.0, "sample_quote": ""}]},
        ]}
        concept_test_panel_result = {"concept_test_results": [
            {"feature_id": "p1", "feature_title": "功能一", "interviewee_name": "陳先生", "interviewee": {},
             "transcript": [], "would_pay": True, "reaction_summary": "喜歡"},
        ]}
        dfv_panel_result = {"dfv_scores": [{"idea_id": "p1", "lens_id": "market_fit", "score": 7.0, "critique": "x"}]}
        with mock.patch.object(graph, "survey_panel_graph") as mock_survey, \
             mock.patch.object(graph, "concept_test_panel_graph") as mock_concept, \
             mock.patch.object(graph, "dfv_panel_graph") as mock_dfv, \
             mock.patch.object(graph, "load_users", return_value=[{"id": "u1", "name": "陳先生"}]), \
             mock.patch.object(graph, "emit_event"):
            mock_survey.invoke.return_value = survey_panel_result
            mock_concept.invoke.return_value = concept_test_panel_result
            mock_dfv.invoke.return_value = dfv_panel_result
            result = graph.validate_market_fit(self._state())
        self.assertEqual(result["dfv_scores"], dfv_panel_result["dfv_scores"])
        self.assertEqual(result["survey_summary"]["by_feature"]["p1"]["purchase_intent_pct"], 60.0)
        self.assertEqual(result["concept_test_summary"]["by_feature"]["p1"]["would_pay_pct"], 100.0)
        # DFV 呼叫時應該已經帶入前兩步的彙整結果，不是空字典
        dfv_call_state = mock_dfv.invoke.call_args[0][0]
        self.assertEqual(dfv_call_state["survey_summary"]["by_feature"]["p1"]["purchase_intent_pct"], 60.0)
        self.assertTrue(dfv_call_state["concept_test_summary"]["by_feature"])

    def test_falls_back_to_default_config_when_state_missing(self):
        with mock.patch.object(graph, "survey_panel_graph") as mock_survey, \
             mock.patch.object(graph, "concept_test_panel_graph") as mock_concept, \
             mock.patch.object(graph, "dfv_panel_graph") as mock_dfv, \
             mock.patch.object(graph, "load_users", return_value=[{"id": "u1", "name": "陳先生"}] * 10), \
             mock.patch.object(graph, "emit_event"):
            mock_survey.invoke.return_value = {"survey_results": []}
            mock_concept.invoke.return_value = {"concept_test_results": []}
            mock_dfv.invoke.return_value = {"dfv_scores": []}
            graph.validate_market_fit(self._state())
        survey_call_state = mock_survey.invoke.call_args[0][0]
        self.assertEqual(survey_call_state["n_respondents"], graph.DEFAULT_SURVEY_RESPONDENTS_PER_STRATUM)
        concept_call_state = mock_concept.invoke.call_args[0][0]
        self.assertEqual(len(concept_call_state["interviewees"]), graph.N_CONCEPT_TEST_INTERVIEWEES)


class FanOutDfvTests(unittest.TestCase):
    def test_fan_out_covers_every_lens_times_every_idea(self):
        ideas = [{"id": "p1", "title": "A"}, {"id": "p2", "title": "B"}]
        state = {
            "ideas": ideas, "strategic_directive": "策略方向",
            "competitive_landscape": [], "survey_summary": {}, "concept_test_summary": {},
        }
        sends = graph.fan_out_dfv(state)
        self.assertEqual(len(sends), len(graph.DFV_LENSES) * len(ideas))
        self.assertTrue(all(s.node == "score_one_dimension" for s in sends))
        lens_ids = {s.arg["lens"]["id"] for s in sends}
        self.assertEqual(lens_ids, {lens["id"] for lens in graph.DFV_LENSES})


class ScoreOneDimensionTests(unittest.TestCase):
    def _task(self, lens=None):
        return {
            "lens": lens or graph.DFV_LENSES[0], "strategic_directive": "策略方向",
            "competitive_landscape": [{"competitor_name": "A", "feature_description": "做訂閱分級"}],
            "survey_summary": {}, "concept_test_summary": {},
            "idea": {"id": "p1", "persona_name": "林美", "title": "T", "summary": "S", "rationale": "R",
                     "differentiation_vs_competitors": "比 A 更個人化"},
        }

    def test_score_clamped_to_0_10(self):
        with mock.patch.object(graph, "call_llm", return_value='{"score": 99, "critique": "x"}'), \
             mock.patch.object(graph, "emit_event"):
            result = graph.score_one_dimension(self._task())
        self.assertEqual(result["dfv_scores"][0]["score"], 10.0)

    def test_malformed_score_falls_back_to_midpoint(self):
        with mock.patch.object(graph, "call_llm", return_value='{"critique": "x"}'), \
             mock.patch.object(graph, "emit_event"):
            result = graph.score_one_dimension(self._task())
        self.assertEqual(result["dfv_scores"][0]["score"], 5.0)

    def test_critique_has_fallback_text(self):
        with mock.patch.object(graph, "call_llm", return_value='{"score": 7}'), \
             mock.patch.object(graph, "emit_event"):
            result = graph.score_one_dimension(self._task())
        self.assertTrue(result["dfv_scores"][0]["critique"])

    def test_market_fit_lens_injects_evidence_and_caveat_into_prompt(self):
        market_fit_lens = next(l for l in graph.DFV_LENSES if l["id"] == "market_fit")
        task = self._task(lens=market_fit_lens)
        task["survey_summary"] = {"by_feature": {"p1": {"purchase_intent_pct": 60.0, "differentiation_pct": 40.0}}}
        task["concept_test_summary"] = {"by_feature": {"p1": {"n_interviewed": 3, "would_pay_pct": 66.7, "sample_reactions": ["喜歡"]}}}
        with mock.patch.object(graph, "call_llm", return_value='{"score": 7, "critique": "x"}') as mock_llm, \
             mock.patch.object(graph, "emit_event"):
            graph.score_one_dimension(task)
        system_prompt = mock_llm.call_args[0][1]
        user_prompt = mock_llm.call_args[0][2]
        self.assertIn("不具統計顯著性", system_prompt)
        self.assertIn("A", user_prompt)
        self.assertIn("60.0", user_prompt)
        self.assertIn(graph.SURVEY_METHOD_CAVEAT, user_prompt)

    def test_non_market_fit_lens_omits_evidence_block(self):
        task = self._task(lens=graph.DFV_LENSES[0])  # desirability
        with mock.patch.object(graph, "call_llm", return_value='{"score": 7, "critique": "x"}') as mock_llm, \
             mock.patch.object(graph, "emit_event"):
            graph.score_one_dimension(task)
        user_prompt = mock_llm.call_args[0][2]
        self.assertNotIn(graph.SURVEY_METHOD_CAVEAT, user_prompt)


class PickWinnerTests(unittest.TestCase):
    def test_picks_highest_total_score(self):
        ideas = [
            {"id": "p1", "persona_name": "林美", "title": "A"},
            {"id": "p2", "persona_name": "陳亞力克斯", "title": "B"},
        ]
        dfv_scores = [
            {"idea_id": "p1", "score": 3.0}, {"idea_id": "p1", "score": 4.0}, {"idea_id": "p1", "score": 2.0},
            {"idea_id": "p2", "score": 8.0}, {"idea_id": "p2", "score": 7.0}, {"idea_id": "p2", "score": 9.0},
        ]
        state = {"ideas": ideas, "dfv_scores": dfv_scores}
        with mock.patch.object(graph, "emit_event"):
            result = graph.pick_winner(state)
        self.assertEqual(result["winner_idea"]["id"], "p2")
        self.assertAlmostEqual(result["winner_idea"]["total_score"], 24.0)

    def test_diversity_computed_from_all_ideas(self):
        ideas = [
            {"id": "p1", "persona_name": "A", "title": "T1", "summary": "完全不同的內容一"},
            {"id": "p2", "persona_name": "B", "title": "T2", "summary": "完全不同的內容二"},
        ]
        dfv_scores = [{"idea_id": "p1", "score": 5.0}, {"idea_id": "p2", "score": 1.0}]
        with mock.patch.object(graph, "emit_event"):
            result = graph.pick_winner({"ideas": ideas, "dfv_scores": dfv_scores})
        self.assertIn("avg_distance", result["idea_diversity"])

    def test_pick_winner_agnostic_to_number_of_lenses(self):
        # stage15-market-fit 加了第 4 個 market_fit lens——pick_winner 純
        # 加總，不用知道有幾個 lens 才能正確運作。
        ideas = [{"id": "p1", "persona_name": "A", "title": "T1"}]
        dfv_scores = [
            {"idea_id": "p1", "score": 5.0}, {"idea_id": "p1", "score": 6.0},
            {"idea_id": "p1", "score": 7.0}, {"idea_id": "p1", "score": 8.0},
        ]
        with mock.patch.object(graph, "emit_event"):
            result = graph.pick_winner({"ideas": ideas, "dfv_scores": dfv_scores})
        self.assertAlmostEqual(result["winner_idea"]["total_score"], 26.0)


class GenerateEvaluatorsTests(unittest.TestCase):
    def _state(self, **overrides):
        state = {
            "topic": "策略方向",
            "ideas": [{"id": "p1", "target_segment": "重度使用者"}, {"id": "p2", "target_segment": "輕度使用者"}],
            "concept_test_results": [{"interviewee_name": "陳先生"}],
        }
        state.update(overrides)
        return state

    def test_excludes_concept_test_interviewee_names(self):
        llm_response = graph.json.dumps({
            "evaluators": [
                {"id": "e1", "name": "張三", "age": 30, "context": "c", "pain_points": [], "tone": "t"},
                {"id": "e2", "name": "李四", "age": 40, "context": "c", "pain_points": [], "tone": "t"},
                {"id": "e3", "name": "陳先生", "age": 32, "context": "c", "pain_points": [], "tone": "t"},  # 重複，該被排除
            ],
        })
        with mock.patch.object(graph, "call_llm", return_value=llm_response), \
             mock.patch.object(graph, "emit_event"):
            result = graph.generate_evaluators(self._state())
        names = {e["name"] for e in result["evaluators"]}
        self.assertNotIn("陳先生", names)

    def test_falls_back_when_too_few_after_exclusion(self):
        llm_response = graph.json.dumps({"evaluators": []})
        fallback_pool = [
            {"id": "u1", "name": "陳先生"}, {"id": "u2", "name": "張三"}, {"id": "u3", "name": "李四"},
        ]
        with mock.patch.object(graph, "call_llm", return_value=llm_response), \
             mock.patch.object(graph, "emit_event"), \
             mock.patch.object(graph, "load_users", return_value=fallback_pool):
            result = graph.generate_evaluators(self._state())
        self.assertTrue(result["used_fallback_evaluators"])
        self.assertNotIn("陳先生", {e["name"] for e in result["evaluators"]})


class AskQuestionFlowTests(unittest.TestCase):
    def _ideas(self):
        return [
            {"id": "p1", "persona_name": "林美", "title": "T1", "summary": "S1", "rationale": "R1"},
            {"id": "p2", "persona_name": "陳亞", "title": "T2", "summary": "S2", "rationale": "R2"},
        ]

    def test_ask_question_payload_lists_all_ideas(self):
        state = {"ideas": self._ideas(), "human_qa_log": []}
        with mock.patch.object(graph, "interrupt", return_value={"action": "skip"}) as mock_interrupt:
            result = graph.ask_question(state)
        payload = mock_interrupt.call_args[0][0]
        self.assertEqual(len(payload["ideas"]), 2)
        self.assertIsNone(result["pending_question"])

    def test_ask_question_rejects_unknown_target_idea_id(self):
        state = {"ideas": self._ideas(), "human_qa_log": []}
        with mock.patch.object(graph, "interrupt", return_value={
            "action": "ask", "target_idea_id": "not_real", "question": "why?",
        }):
            result = graph.ask_question(state)
        self.assertIsNone(result["pending_question"])

    def test_route_after_question(self):
        self.assertEqual(graph.route_after_question({"pending_question": "q"}), "answer_question")
        self.assertEqual(graph.route_after_question({"pending_question": None}), "validate_market_fit")

    def test_answer_question_does_not_mutate_idea(self):
        ideas = self._ideas()
        state = {
            "ideas": ideas, "pending_question_target_idea_id": "p1",
            "pending_question": "為什麼選這個方向？", "pending_question_asked_by": "小明",
        }
        with mock.patch.object(graph, "call_llm", return_value="因為這樣做。"), \
             mock.patch.object(graph, "emit_event"):
            result = graph.answer_question(state)
        self.assertEqual(ideas, self._ideas())  # idea 內容沒被改動
        self.assertEqual(result["human_qa_log"][0]["asked_by"], "小明")
        self.assertIsNone(result["pending_question"])


class BaselineProposalTests(unittest.TestCase):
    def test_run_baseline_produces_quantified_bmc(self):
        mock_response = graph.json.dumps({
            "title": "T", "summary": "S", "sources": [], "self_score": 7,
            "bmc": _valid_bmc("x", revenue=5000, cost=2000),
        })
        with mock.patch.object(graph, "call_llm", return_value=mock_response), \
             mock.patch.object(graph, "emit_event"):
            proposal = graph.run_baseline("主題", "公司")
        self.assertEqual(proposal["unit_economics"]["monthly_margin_twd"], 3000)
        self.assertTrue(proposal["unit_economics"]["is_viable"])


class BuildFinalReportMarkdownTests(unittest.TestCase):
    def _base_kwargs(self, **overrides):
        kwargs = dict(
            round_id="r1", topic="APP建立會員付費訂閱加值功能機制",
            competitive_landscape=[
                {"competitor_name": "A", "feature_description": "做訂閱分級", "source_url": "https://a.example.com"},
            ],
            used_fallback_competitive_landscape=False,
            survey_summary={
                "caveat": graph.SURVEY_METHOD_CAVEAT, "total_simulated_n": 64,
                "by_feature": {
                    "p1": {"feature_title": "功能一", "purchase_intent_pct": 62.5,
                           "differentiation_pct": 40.0, "sample_quotes": ["[分層A] 模擬引述"]},
                },
            },
            concept_test_summary={
                "by_feature": {
                    "p1": {"feature_title": "功能一", "would_pay_pct": 66.7, "n_interviewed": 3,
                           "sample_reactions": ["[陳先生] 喜歡"]},
                },
            },
            personas=[{"id": "p1", "name": "林美", "domain": "硬體工程"}, {"id": "p2", "name": "陳亞", "domain": "遊戲設計"}],
            ideas=[{"id": "p1", "persona_name": "林美", "title": "T1", "summary": "S1", "rationale": "R1",
                    "target_segment": "重度使用者", "monetization_mechanism": "訂閱分級",
                    "differentiation_vs_competitors": "比 A 更個人化", "bmc": _valid_bmc("y")}],
            human_qa_log=[],
            dfv_scores=[
                {"idea_id": "p1", "lens_id": "desirability", "score": 8.0, "critique": "很想要"},
                {"idea_id": "p1", "lens_id": "market_fit", "score": 7.0, "critique": "有差異化"},
            ],
            winner_idea={"persona_name": "林美", "title": "T1", "total_score": 24.0},
            idea_diversity={"avg_distance": 0.3},
            prototype={"persona_name": "林美", "title": "T1", "summary": "S1", "html_path": "/tmp/a.html"},
            evaluators=[{"id": "e1", "name": "張三"}],
            baseline_proposal={"title": "baseline標題", "summary": "s"},
            baseline_metrics={"real_citations": 0, "cost_usd": 0.001},
            user_evaluation={
                "evaluations": [
                    {"user_id": "e1", "user_name": "張三", "agent_reaction": "喜歡", "agent_score": 8.0,
                     "baseline_reaction": "普通", "baseline_score": 5.0},
                ],
                "agent_avg_score": 8.0, "baseline_avg_score": 5.0, "score_delta": 3.0,
            },
            final_verdict="這是評語。",
        )
        kwargs.update(overrides)
        return kwargs

    def test_report_contains_required_sections(self):
        report = graph.build_final_report_markdown(**self._base_kwargs())
        for marker in (
            "市場現況", "真實競品掃描", "虛擬問卷", "概念測試訪談", "人類提問記錄",
            "DFV 結構化評分", "收斂結果", "Prototype", "Baseline 對照",
            "最終評估者對照評分", "重度使用者", "訂閱分級",
        ):
            self.assertIn(marker, report)

    def test_report_shows_competitor_source_url(self):
        report = graph.build_final_report_markdown(**self._base_kwargs())
        self.assertIn("https://a.example.com", report)

    def test_report_handles_fallback_competitive_landscape(self):
        report = graph.build_final_report_markdown(**self._base_kwargs(used_fallback_competitive_landscape=True))
        self.assertIn("本次未能取得具體競品資訊", report)

    def test_report_handles_empty_human_qa_log(self):
        report = graph.build_final_report_markdown(**self._base_kwargs(human_qa_log=[]))
        self.assertIn("本場沒有人類提問", report)

    def test_report_includes_final_verdict(self):
        report = graph.build_final_report_markdown(**self._base_kwargs())
        self.assertIn("這是評語。", report)


if __name__ == "__main__":
    unittest.main()
