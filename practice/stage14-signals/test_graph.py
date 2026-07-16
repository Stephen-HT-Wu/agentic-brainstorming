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
    節點有兩個以上不同的靜態前驅。"""

    def test_no_unexpected_multi_predecessor_nodes(self):
        compiled = graph.build_parent_graph(None)
        preds = defaultdict(set)
        for edge in compiled.get_graph().edges:
            preds[edge.target].add(edge.source)
        multi = {node: srcs for node, srcs in preds.items() if len(srcs) > 1}
        self.assertEqual(set(multi.keys()), {"ask_question"})


class SwitchFollowupQuestionTests(unittest.TestCase):
    def test_uses_round_specific_intent_hint_as_fallback(self):
        prior_turns = [{"question": "Q1", "answer": "A1"}]
        with mock.patch.object(graph, "call_llm", return_value=""):
            question = graph.generate_switch_followup_question(
                {"name": "系統研究員"}, prior_turns, round_i=3,
            )
        self.assertEqual(question, graph.SWITCH_INTERVIEW_ROUND_INTENTS[2]["hint"])

    def test_unknown_round_falls_back_to_last_intent(self):
        with mock.patch.object(graph, "call_llm", return_value=""):
            question = graph.generate_switch_followup_question({"name": "x"}, [{"question": "q", "answer": "a"}], round_i=99)
        self.assertEqual(question, graph.SWITCH_INTERVIEW_ROUND_INTENTS[-1]["hint"])


class DeskResearchHypothesizeJobsTests(unittest.TestCase):
    def _state(self):
        return {"topic": "如何提升訂閱率", "company": "北辰短影音"}

    def _valid_llm_response(self, n_jobs=3):
        return graph.json.dumps({
            "five_forces": {
                "新進入者威脅": "低", "替代品威脅": "中", "顧客議價力": "高",
                "供應商議價力": "低", "現有競爭者強度": "中",
            },
            "trend_analysis": "訂閱疲勞持續加劇。",
            "candidate_jobs": [
                {
                    "id": f"job{n}", "job_statement": f"情境{n}下想達成的進展",
                    "hypothesis_rationale": "值得驗證",
                    "interview_pool": [
                        {"id": f"u{n}1", "name": f"受訪者{n}甲", "age": 30, "context": "c", "pain_points": ["p"], "tone": "t"},
                        {"id": f"u{n}2", "name": f"受訪者{n}乙", "age": 40, "context": "c", "pain_points": ["p"], "tone": "t"},
                    ],
                }
                for n in range(1, n_jobs + 1)
            ],
        })

    def test_happy_path_parses_candidate_jobs_no_solution_leakage_check(self):
        with mock.patch.object(graph, "web_search", return_value=[]), \
             mock.patch.object(graph, "call_llm", return_value=self._valid_llm_response()), \
             mock.patch.object(graph, "emit_event"):
            result = graph.desk_research_hypothesize_jobs(self._state())
        self.assertEqual(len(result["candidate_jobs"]), 3)
        self.assertFalse(result["used_fallback_candidate_jobs"])
        for cj in result["candidate_jobs"]:
            self.assertEqual(len(cj["interview_pool"]), graph.N_INTERVIEWEES_PER_JOB)

    def test_falls_back_when_too_few_candidate_jobs(self):
        llm_response = graph.json.dumps({
            "five_forces": {}, "trend_analysis": "", "candidate_jobs": [
                {"id": "job1", "job_statement": "只有一個", "interview_pool": []},
            ],
        })
        fallback_users = [
            {"id": "u1", "name": "陳小姐"}, {"id": "u2", "name": "王先生"},
            {"id": "u3", "name": "小宇"}, {"id": "u4", "name": "阿花"},
            {"id": "u5", "name": "小明"}, {"id": "u6", "name": "小華"},
        ]
        with mock.patch.object(graph, "web_search", return_value=[]), \
             mock.patch.object(graph, "call_llm", return_value=llm_response), \
             mock.patch.object(graph, "emit_event"), \
             mock.patch.object(graph, "load_users", return_value=fallback_users):
            result = graph.desk_research_hypothesize_jobs(self._state())
        self.assertTrue(result["used_fallback_candidate_jobs"])
        self.assertEqual(len(result["candidate_jobs"]), graph.N_CANDIDATE_JOBS)

    def test_falls_back_when_llm_output_unparseable(self):
        fallback_users = [
            {"id": "u1", "name": "陳小姐"}, {"id": "u2", "name": "王先生"},
        ]
        with mock.patch.object(graph, "web_search", return_value=[]), \
             mock.patch.object(graph, "call_llm", return_value="不是合法 JSON"), \
             mock.patch.object(graph, "emit_event"), \
             mock.patch.object(graph, "load_users", return_value=fallback_users):
            result = graph.desk_research_hypothesize_jobs(self._state())
        self.assertTrue(result["used_fallback_candidate_jobs"])
        self.assertTrue(all(cj["job_statement"] for cj in result["candidate_jobs"]))


class InterviewModeConfigTests(unittest.TestCase):
    """stage14-signals 新增：訪談對象生成視角/人數改成從 state 讀（有預設值）
    ——這裡驗證 state 有給值時真的會覆蓋模組預設值，且 mode_fragment 真的
    被接進 system prompt，不是讀了卻沒用。"""

    def _valid_llm_response(self, n_interviewees):
        return graph.json.dumps({
            "five_forces": {}, "trend_analysis": "趨勢",
            "candidate_jobs": [
                {
                    "id": "job1", "job_statement": "情境1", "hypothesis_rationale": "r",
                    "interview_pool": [
                        {"id": f"u{i}", "name": f"受訪者{i}", "age": 30, "context": "c", "pain_points": [], "tone": "t"}
                        for i in range(1, n_interviewees + 3)  # 故意給多一點，驗證真的按 n_interviewees 截斷
                    ],
                },
                {
                    "id": "job2", "job_statement": "情境2", "hypothesis_rationale": "r",
                    "interview_pool": [
                        {"id": f"v{i}", "name": f"受訪者v{i}", "age": 30, "context": "c", "pain_points": [], "tone": "t"}
                        for i in range(1, n_interviewees + 3)
                    ],
                },
            ],
        })

    def test_state_n_interviewees_overrides_module_default(self):
        state = {"topic": "T", "company": "C", "n_interviewees_per_job": 4}
        with mock.patch.object(graph, "web_search", return_value=[]), \
             mock.patch.object(graph, "call_llm", return_value=self._valid_llm_response(4)) as mock_llm, \
             mock.patch.object(graph, "emit_event"):
            result = graph.desk_research_hypothesize_jobs(state)
        for cj in result["candidate_jobs"]:
            self.assertEqual(len(cj["interview_pool"]), 4)
        # system prompt 裡真的有帶入 4，不是還在用模組預設值 2
        system_prompt = mock_llm.call_args[0][1]
        self.assertIn("恰好 4 位", system_prompt)

    def test_state_diverse_mode_injects_diverse_prompt_fragment(self):
        state = {"topic": "T", "company": "C", "interview_mode": "diverse"}
        with mock.patch.object(graph, "web_search", return_value=[]), \
             mock.patch.object(graph, "call_llm", return_value=self._valid_llm_response(graph.N_INTERVIEWEES_PER_JOB)) as mock_llm, \
             mock.patch.object(graph, "emit_event"):
            graph.desk_research_hypothesize_jobs(state)
        system_prompt = mock_llm.call_args[0][1]
        self.assertIn(graph.INTERVIEW_MODE_PROMPT_FRAGMENTS["diverse"], system_prompt)

    def test_missing_state_config_falls_back_to_module_defaults(self):
        state = {"topic": "T", "company": "C"}
        with mock.patch.object(graph, "web_search", return_value=[]), \
             mock.patch.object(graph, "call_llm", return_value=self._valid_llm_response(graph.N_INTERVIEWEES_PER_JOB)) as mock_llm, \
             mock.patch.object(graph, "emit_event"):
            graph.desk_research_hypothesize_jobs(state)
        system_prompt = mock_llm.call_args[0][1]
        self.assertIn(graph.INTERVIEW_MODE_PROMPT_FRAGMENTS[graph.DEFAULT_INTERVIEW_MODE], system_prompt)

    def test_fallback_branch_honors_custom_n_interviewees(self):
        fallback_users = [{"id": f"u{i}", "name": f"人{i}"} for i in range(1, 21)]
        state = {"topic": "T", "company": "C", "n_interviewees_per_job": 5}
        llm_response = graph.json.dumps({"five_forces": {}, "trend_analysis": "", "candidate_jobs": []})
        with mock.patch.object(graph, "web_search", return_value=[]), \
             mock.patch.object(graph, "call_llm", return_value=llm_response), \
             mock.patch.object(graph, "emit_event"), \
             mock.patch.object(graph, "load_users", return_value=fallback_users):
            result = graph.desk_research_hypothesize_jobs(state)
        self.assertTrue(result["used_fallback_candidate_jobs"])
        for cj in result["candidate_jobs"]:
            self.assertEqual(len(cj["interview_pool"]), 5)


class FanOutCandidateJobsTests(unittest.TestCase):
    def test_fan_out_covers_every_candidate_job(self):
        candidate_jobs = [{"id": "job1"}, {"id": "job2"}, {"id": "job3"}]
        sends = graph.fan_out_candidate_jobs({"topic": "T", "candidate_jobs": candidate_jobs, "job_evidence": [], "interview_transcript": []})
        self.assertEqual(len(sends), 3)
        self.assertTrue(all(s.node == "research_one_candidate_job" for s in sends))
        job_ids = {s.arg["candidate_job"]["id"] for s in sends}
        self.assertEqual(job_ids, {"job1", "job2", "job3"})


class ResearchOneCandidateJobTests(unittest.TestCase):
    def _task(self):
        return {
            "topic": "T",
            "candidate_job": {
                "id": "job1", "job_statement": "情境下想達成的進展",
                "interview_pool": [{"id": "u1", "name": "陳先生"}],
            },
        }

    def test_tags_transcript_with_candidate_job_id_and_returns_evidence(self):
        transcript = [{"user_id": "u1", "user_name": "陳先生", "round": 1, "question": "Q", "answer": "A"}]
        evidence_response = graph.json.dumps({
            "supported": True, "evidence_summary": "有具體證據",
            "insights": [{"id": "i1", "text": "洞見1"}],
        })
        with mock.patch.object(graph, "interview_panel_graph") as mock_panel, \
             mock.patch.object(graph, "call_llm", return_value=evidence_response), \
             mock.patch.object(graph, "emit_event"):
            mock_panel.invoke.return_value = {"interview_transcript": transcript}
            result = graph.research_one_candidate_job(self._task())
        self.assertEqual(result["interview_transcript"][0]["candidate_job_id"], "job1")
        self.assertEqual(len(result["job_evidence"]), 1)
        self.assertTrue(result["job_evidence"][0]["supported"])
        self.assertEqual(result["job_evidence"][0]["job_id"], "job1")


class FanOutSurveyStrataTests(unittest.TestCase):
    def test_fan_out_covers_every_fixed_stratum(self):
        state = {
            "topic": "T", "candidate_jobs": [{"id": "job1"}], "n_respondents": 5,
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
            "candidate_jobs": [{"id": "job1", "job_statement": "情境一"}, {"id": "job2", "job_statement": "情境二"}],
            "topic": "T", "n_respondents": 8,
        }

    def test_happy_path_returns_per_job_stats(self):
        llm_response = graph.json.dumps({"jobs": [
            {"job_id": "job1", "high_distress_pct": 62.5, "avg_intensity": 3.4, "sample_quote": "（模擬）引述一"},
            {"job_id": "job2", "high_distress_pct": 20, "avg_intensity": 2.0, "sample_quote": ""},
        ]})
        with mock.patch.object(graph, "call_llm", return_value=llm_response), \
             mock.patch.object(graph, "emit_event"):
            result = graph.survey_one_stratum(self._task())
        self.assertEqual(len(result["survey_results"]), 1)
        entry = result["survey_results"][0]
        self.assertEqual(entry["stratum_id"], "s1")
        self.assertEqual(entry["n_simulated"], 8)
        self.assertFalse(entry["used_fallback"])
        stats_by_job = {j["job_id"]: j for j in entry["job_stats"]}
        self.assertEqual(stats_by_job["job1"]["high_distress_pct"], 62.5)
        self.assertEqual(stats_by_job["job1"]["avg_intensity"], 3.4)
        self.assertEqual(stats_by_job["job1"]["sample_quote"], "（模擬）引述一")

    def test_missing_job_in_llm_output_falls_back_to_midpoint(self):
        llm_response = graph.json.dumps({"jobs": [
            {"job_id": "job1", "high_distress_pct": 50, "avg_intensity": 3.0, "sample_quote": ""},
        ]})
        with mock.patch.object(graph, "call_llm", return_value=llm_response), \
             mock.patch.object(graph, "emit_event"):
            result = graph.survey_one_stratum(self._task())
        entry = result["survey_results"][0]
        self.assertTrue(entry["used_fallback"])
        stats_by_job = {j["job_id"]: j for j in entry["job_stats"]}
        self.assertEqual(stats_by_job["job2"]["high_distress_pct"], 50.0)
        self.assertEqual(stats_by_job["job2"]["avg_intensity"], 3.0)

    def test_unparseable_llm_output_falls_back_for_all_jobs(self):
        with mock.patch.object(graph, "call_llm", return_value="不是合法 JSON"), \
             mock.patch.object(graph, "emit_event"):
            result = graph.survey_one_stratum(self._task())
        entry = result["survey_results"][0]
        self.assertTrue(entry["used_fallback"])
        self.assertEqual(len(entry["job_stats"]), 2)

    def test_out_of_range_values_are_clamped(self):
        llm_response = graph.json.dumps({"jobs": [
            {"job_id": "job1", "high_distress_pct": 250, "avg_intensity": 99, "sample_quote": ""},
            {"job_id": "job2", "high_distress_pct": -10, "avg_intensity": 0, "sample_quote": ""},
        ]})
        with mock.patch.object(graph, "call_llm", return_value=llm_response), \
             mock.patch.object(graph, "emit_event"):
            result = graph.survey_one_stratum(self._task())
        stats_by_job = {j["job_id"]: j for j in result["survey_results"][0]["job_stats"]}
        self.assertEqual(stats_by_job["job1"]["high_distress_pct"], 100.0)
        self.assertEqual(stats_by_job["job1"]["avg_intensity"], 5.0)
        self.assertEqual(stats_by_job["job2"]["high_distress_pct"], 0.0)
        self.assertEqual(stats_by_job["job2"]["avg_intensity"], 1.0)


class AggregateSurveyResultsTests(unittest.TestCase):
    def test_weighted_average_across_strata(self):
        candidate_jobs = [{"id": "job1", "job_statement": "情境一"}]
        survey_results = [
            {"stratum_label": "A", "n_simulated": 2, "job_stats": [
                {"job_id": "job1", "high_distress_pct": 40.0, "avg_intensity": 2.0, "sample_quote": "引述A"},
            ]},
            {"stratum_label": "B", "n_simulated": 6, "job_stats": [
                {"job_id": "job1", "high_distress_pct": 80.0, "avg_intensity": 4.0, "sample_quote": ""},
            ]},
        ]
        summary = graph._aggregate_survey_results(survey_results, candidate_jobs)
        self.assertEqual(summary["caveat"], graph.SURVEY_METHOD_CAVEAT)
        self.assertEqual(summary["total_simulated_n"], 8)
        job1 = summary["by_job"]["job1"]
        # (40*2 + 80*6) / 8 = 70.0；(2*2 + 4*6) / 8 = 3.5
        self.assertEqual(job1["high_distress_pct"], 70.0)
        self.assertEqual(job1["avg_intensity"], 3.5)
        self.assertEqual(job1["sample_quotes"], ["[A] 引述A"])

    def test_at_most_two_quotes_kept_per_job(self):
        candidate_jobs = [{"id": "job1", "job_statement": "x"}]
        survey_results = [
            {"stratum_label": f"S{i}", "n_simulated": 1, "job_stats": [
                {"job_id": "job1", "high_distress_pct": 50.0, "avg_intensity": 3.0, "sample_quote": f"引述{i}"},
            ]}
            for i in range(4)
        ]
        summary = graph._aggregate_survey_results(survey_results, candidate_jobs)
        self.assertEqual(len(summary["by_job"]["job1"]["sample_quotes"]), 2)


class RunVirtualSurveyTests(unittest.TestCase):
    def test_invokes_panel_graph_and_aggregates(self):
        state = {
            "topic": "T", "candidate_jobs": [{"id": "job1", "job_statement": "情境一"}],
            "survey_respondents_per_stratum": 3,
        }
        panel_result = {
            "survey_results": [
                {"stratum_id": "s1", "stratum_label": "A", "n_simulated": 3, "used_fallback": False,
                 "job_stats": [{"job_id": "job1", "high_distress_pct": 60.0, "avg_intensity": 3.0, "sample_quote": ""}]},
            ],
        }
        with mock.patch.object(graph, "survey_panel_graph") as mock_panel:
            mock_panel.invoke.return_value = panel_result
            result = graph.run_virtual_survey(state)
        self.assertEqual(result["survey_results"], panel_result["survey_results"])
        self.assertEqual(result["survey_summary"]["by_job"]["job1"]["high_distress_pct"], 60.0)
        # 讀了 state 的 survey_respondents_per_stratum，不是永遠用模組預設值
        called_with = mock_panel.invoke.call_args[0][0]
        self.assertEqual(called_with["n_respondents"], 3)

    def test_falls_back_to_default_respondents_when_state_missing(self):
        state = {"topic": "T", "candidate_jobs": [{"id": "job1", "job_statement": "x"}]}
        with mock.patch.object(graph, "survey_panel_graph") as mock_panel:
            mock_panel.invoke.return_value = {"survey_results": []}
            graph.run_virtual_survey(state)
        called_with = mock_panel.invoke.call_args[0][0]
        self.assertEqual(called_with["n_respondents"], graph.DEFAULT_SURVEY_RESPONDENTS_PER_STRATUM)


class SelectJobAndDefineProblemTests(unittest.TestCase):
    def _state(self):
        return {
            "candidate_jobs": [
                {"id": "job1", "job_statement": "情境一"},
                {"id": "job2", "job_statement": "情境二"},
            ],
            "job_evidence": [
                {"job_id": "job1", "job_statement": "情境一", "supported": True,
                 "evidence_summary": "證據一", "insights": [{"id": "i1", "text": "洞見一"}]},
                {"job_id": "job2", "job_statement": "情境二", "supported": False,
                 "evidence_summary": "證據二", "insights": [{"id": "i2", "text": "洞見二"}]},
            ],
        }

    def test_happy_path_selects_job_and_defines_problem(self):
        llm_response = graph.json.dumps({
            "selected_job_id": "job1", "why_selected": "job1 訪談證據明確支持",
            "target_audience": "通勤族", "problem_statement": "情境一的未滿足需求",
            "hmw": "How Might We 幫助通勤族",
        })
        with mock.patch.object(graph, "call_llm", return_value=llm_response), \
             mock.patch.object(graph, "emit_event"):
            result = graph.select_job_and_define_problem(self._state())
        self.assertEqual(result["selected_job_id"], "job1")
        self.assertEqual(result["selected_job"]["why_selected"], "job1 訪談證據明確支持")
        self.assertEqual(result["insights"], [{"id": "i1", "text": "洞見一"}])

    def test_falls_back_to_supported_job_when_llm_output_unparseable(self):
        with mock.patch.object(graph, "call_llm", return_value="不是合法 JSON"), \
             mock.patch.object(graph, "emit_event"):
            result = graph.select_job_and_define_problem(self._state())
        self.assertEqual(result["selected_job_id"], "job1")  # 唯一 supported=True 的
        self.assertTrue(result["problem_statement"])
        self.assertTrue(result["hmw"])

    def test_survey_summary_injected_into_prompt_with_caveat(self):
        state = self._state()
        state["survey_summary"] = {
            "caveat": graph.SURVEY_METHOD_CAVEAT, "total_simulated_n": 16,
            "by_job": {"job1": {"job_statement": "情境一", "high_distress_pct": 62.5,
                                  "avg_intensity": 3.4, "sample_quotes": ["[分層A] 模擬引述"]}},
        }
        llm_response = graph.json.dumps({
            "selected_job_id": "job1", "why_selected": "job1 訪談證據明確支持",
            "target_audience": "通勤族", "problem_statement": "情境一的未滿足需求",
            "hmw": "How Might We 幫助通勤族",
        })
        with mock.patch.object(graph, "call_llm", return_value=llm_response) as mock_llm, \
             mock.patch.object(graph, "emit_event"):
            graph.select_job_and_define_problem(state)
        system_prompt = mock_llm.call_args[0][1]
        user_prompt = mock_llm.call_args[0][2]
        self.assertIn("量化補充", user_prompt)
        self.assertIn(graph.SURVEY_METHOD_CAVEAT, user_prompt)
        self.assertIn("不具統計顯著性", system_prompt)

    def test_missing_survey_summary_omits_quant_block(self):
        with mock.patch.object(graph, "call_llm", return_value=graph.json.dumps({
            "selected_job_id": "job1", "why_selected": "x", "target_audience": "y",
            "problem_statement": "z", "hmw": "h",
        })) as mock_llm, mock.patch.object(graph, "emit_event"):
            graph.select_job_and_define_problem(self._state())
        user_prompt = mock_llm.call_args[0][2]
        self.assertNotIn("量化補充", user_prompt)


class DeriveCompanyDomainsTests(unittest.TestCase):
    def test_happy_path_derives_distinct_domains_from_company(self):
        llm_response = graph.json.dumps({"domains": ["APP工程", "在地異業合作開發", "會員數據/CRM", "短影音後製", "廣告業務"]})
        with mock.patch.object(graph, "call_llm", return_value=llm_response), \
             mock.patch.object(graph, "emit_event"):
            domains, used_fallback = graph.derive_company_domains("公司定位描述", "P", "H")
        self.assertEqual(len(domains), 5)
        self.assertFalse(used_fallback)

    def test_dedupes_repeated_domains_from_llm(self):
        llm_response = graph.json.dumps({"domains": ["APP工程", "APP工程", "會員數據/CRM"]})
        with mock.patch.object(graph, "call_llm", return_value=llm_response), \
             mock.patch.object(graph, "emit_event"):
            domains, used_fallback = graph.derive_company_domains("公司定位描述", "P", "H")
        self.assertEqual(domains, ["APP工程", "會員數據/CRM"])
        self.assertFalse(used_fallback)

    def test_falls_back_to_domain_pool_when_too_few(self):
        llm_response = graph.json.dumps({"domains": ["只有一個"]})
        with mock.patch.object(graph, "call_llm", return_value=llm_response), \
             mock.patch.object(graph, "emit_event"):
            domains, used_fallback = graph.derive_company_domains("公司定位描述", "P", "H")
        self.assertTrue(used_fallback)
        self.assertTrue(set(domains) <= set(graph.DOMAIN_ARCHETYPE_POOL))

    def test_falls_back_when_llm_output_unparseable(self):
        with mock.patch.object(graph, "call_llm", return_value="不是合法 JSON"), \
             mock.patch.object(graph, "emit_event"):
            domains, used_fallback = graph.derive_company_domains("公司定位描述", "P", "H")
        self.assertTrue(used_fallback)
        self.assertGreaterEqual(len(domains), 2)


class AssemblePersonaTeamTests(unittest.TestCase):
    def _state(self):
        return {"company": "公司定位描述", "problem_statement": "P", "hmw": "H", "target_audience": "A"}

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
        # derive_company_domains 自己就退回保底領域池時，即使後續每位
        # persona 都生成成功，used_fallback_personas 仍要誠實反映「這場
        # 職能其實不是扣著公司能力衍生的」。
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
        task = {"domain": "硬體工程", "problem_statement": "P", "hmw": "H", "target_audience": "A", "idx": 1}
        with mock.patch.object(graph, "call_llm", return_value=llm_response), \
             mock.patch.object(graph, "emit_event"):
            result = graph.generate_one_persona_for_domain(task)
        persona = result["personas"][0]
        self.assertEqual(persona["domain"], "硬體工程")
        self.assertEqual(persona["id"], "p1")

    def test_falls_back_to_domain_name_when_llm_output_unparseable(self):
        task = {"domain": "農業供應鏈", "problem_statement": "P", "hmw": "H", "target_audience": "A", "idx": 2}
        with mock.patch.object(graph, "call_llm", return_value="不是合法 JSON"), \
             mock.patch.object(graph, "emit_event"):
            result = graph.generate_one_persona_for_domain(task)
        persona = result["personas"][0]
        self.assertEqual(persona["domain"], "農業供應鏈")
        self.assertTrue(persona["name"])


class FanOutDfvTests(unittest.TestCase):
    def test_fan_out_covers_every_lens_times_every_idea(self):
        ideas = [{"id": "p1", "title": "A"}, {"id": "p2", "title": "B"}]
        sends = graph.fan_out_dfv({"ideas": ideas, "problem_statement": "P", "dfv_scores": []})
        self.assertEqual(len(sends), len(graph.DFV_LENSES) * len(ideas))
        self.assertTrue(all(s.node == "score_one_dimension" for s in sends))
        lens_ids = {s.arg["lens"]["id"] for s in sends}
        self.assertEqual(lens_ids, {lens["id"] for lens in graph.DFV_LENSES})


class ScoreOneDimensionTests(unittest.TestCase):
    def _task(self):
        return {
            "lens": graph.DFV_LENSES[0], "problem_statement": "P",
            "idea": {"id": "p1", "persona_name": "林美", "title": "T", "summary": "S", "rationale": "R"},
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


class GenerateEvaluatorsTests(unittest.TestCase):
    def test_excludes_all_candidate_jobs_interviewee_names(self):
        llm_response = graph.json.dumps({
            "evaluators": [
                {"id": "e1", "name": "張三", "age": 30, "context": "c", "pain_points": [], "tone": "t"},
                {"id": "e2", "name": "李四", "age": 40, "context": "c", "pain_points": [], "tone": "t"},
                {"id": "e3", "name": "陳先生", "age": 32, "context": "c", "pain_points": [], "tone": "t"},  # 重複，該被排除
            ],
        })
        state = {
            "target_audience": "A",
            "candidate_jobs": [
                {"id": "job1", "interview_pool": [{"name": "陳先生"}]},
                {"id": "job2", "interview_pool": [{"name": "林小姐"}]},
            ],
        }
        with mock.patch.object(graph, "call_llm", return_value=llm_response), \
             mock.patch.object(graph, "emit_event"):
            result = graph.generate_evaluators(state)
        names = {e["name"] for e in result["evaluators"]}
        self.assertNotIn("陳先生", names)

    def test_falls_back_when_too_few_after_exclusion(self):
        llm_response = graph.json.dumps({"evaluators": []})
        fallback_pool = [
            {"id": "u1", "name": "陳先生"}, {"id": "u2", "name": "張三"}, {"id": "u3", "name": "李四"},
        ]
        state = {
            "target_audience": "A",
            "candidate_jobs": [{"id": "job1", "interview_pool": [{"name": "陳先生"}]}],
        }
        with mock.patch.object(graph, "call_llm", return_value=llm_response), \
             mock.patch.object(graph, "emit_event"), \
             mock.patch.object(graph, "load_users", return_value=fallback_pool):
            result = graph.generate_evaluators(state)
        self.assertTrue(result["used_fallback_evaluators"])
        self.assertNotIn("陳先生", {e["name"] for e in result["evaluators"]})


class DraftOneIdeaTests(unittest.TestCase):
    def _task(self):
        return {
            "persona": {"id": "p1", "name": "林美", "role": "產品", "background": "b", "focus": ["f"], "style": "s"},
            "problem_statement": "P", "hmw": "H", "target_audience": "A",
            "insights": [{"id": "i1", "text": "洞見1"}],
            "research_items": [],
        }

    def test_idea_gets_its_own_bmc_and_filters_invalid_insight_refs(self):
        mock_response = graph.json.dumps({
            "title": "T", "summary": "S", "rationale": "R",
            "insight_refs": ["i1", "not_a_real_id"], "sources": [],
            "bmc": _valid_bmc("自己想的"),
        })
        with mock.patch.object(graph, "call_llm", return_value=mock_response), \
             mock.patch.object(graph, "emit_event"):
            result = graph.draft_one_idea(self._task())
        idea = result["ideas"][0]
        self.assertEqual(idea["insight_refs"], ["i1"])
        self.assertEqual(idea["id"], "p1")
        self.assertEqual(idea["persona_name"], "林美")
        self.assertEqual(graph.assert_bmc_complete(idea), [])

    def test_missing_bmc_from_llm_still_produces_structurally_valid_default(self):
        mock_response = graph.json.dumps({
            "title": "T", "summary": "S", "rationale": "R",
            "insight_refs": ["i1"], "sources": [],
        })
        with mock.patch.object(graph, "call_llm", return_value=mock_response), \
             mock.patch.object(graph, "emit_event"):
            result = graph.draft_one_idea(self._task())
        idea = result["ideas"][0]
        self.assertIn("bmc", idea)
        self.assertEqual(set(idea["bmc"].keys()), set(graph.BMC_KEYS))


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
        self.assertEqual(graph.route_after_question({"pending_question": None}), "dfv_scoring")

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
            round_id="r1", topic="測試主題",
            candidate_jobs=[
                {"id": "job1", "job_statement": "情境一", "hypothesis_rationale": "理由",
                 "interview_pool": [{"id": "u1", "name": "陳先生", "age": 30, "context": "c", "pain_points": ["p"]}]},
                {"id": "job2", "job_statement": "情境二", "hypothesis_rationale": "理由二",
                 "interview_pool": [{"id": "u2", "name": "林小姐", "age": 28, "context": "c", "pain_points": []}]},
            ],
            used_fallback_candidate_jobs=False,
            job_evidence=[
                {"job_id": "job1", "job_statement": "情境一", "supported": True,
                 "evidence_summary": "證據充分", "insights": [{"id": "i1", "text": "洞見1"}]},
                {"job_id": "job2", "job_statement": "情境二", "supported": False,
                 "evidence_summary": "證據不足", "insights": []},
            ],
            survey_summary={
                "caveat": graph.SURVEY_METHOD_CAVEAT, "total_simulated_n": 64,
                "by_job": {
                    "job1": {"job_statement": "情境一", "high_distress_pct": 62.5,
                              "avg_intensity": 3.4, "sample_quotes": ["[分層A] 模擬引述"]},
                },
            },
            selected_job_id="job1",
            selected_job={"id": "job1", "job_statement": "情境一", "why_selected": "訪談證據明確支持"},
            target_audience="通勤族", problem_statement="情境一的未滿足需求", hmw="How Might We 幫助通勤族",
            five_forces={"新進入者威脅": "低"}, trend_analysis="趨勢文字",
            interview_transcript=[{"user_name": "陳先生", "round": 1, "question": "Q1", "answer": "A1", "candidate_job_id": "job1"}],
            insights=[{"id": "i1", "text": "洞見1"}],
            personas=[{"id": "p1", "name": "林美", "domain": "硬體工程"}, {"id": "p2", "name": "陳亞", "domain": "遊戲設計"}],
            ideas=[{"persona_name": "林美", "title": "T1", "summary": "S1", "rationale": "R1", "bmc": _valid_bmc("y")}],
            human_qa_log=[],
            dfv_scores=[
                {"idea_id": "p1", "lens_id": "desirability", "score": 8.0, "critique": "很想要"},
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
            "Discover", "Define", "五力分析", "人類提問記錄",
            "DFV 結構化評分", "收斂結果", "Prototype", "Baseline 對照",
            "最終評估者對照評分", "情境一", "情境二", "why_selected".replace("why_selected", "選定依據"),
        ):
            self.assertIn(marker, report)

    def test_report_shows_both_selected_and_rejected_candidate_jobs(self):
        report = graph.build_final_report_markdown(**self._base_kwargs())
        self.assertIn("雀屏中選", report)
        self.assertIn("未雀屏中選", report)

    def test_report_handles_empty_human_qa_log(self):
        report = graph.build_final_report_markdown(**self._base_kwargs(human_qa_log=[]))
        self.assertIn("本場沒有人類提問", report)

    def test_report_includes_final_verdict(self):
        report = graph.build_final_report_markdown(**self._base_kwargs())
        self.assertIn("這是評語。", report)


if __name__ == "__main__":
    unittest.main()
