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


class FanOutDfvTests(unittest.TestCase):
    def test_fan_out_covers_every_lens_times_every_idea(self):
        ideas = [{"id": "p1", "title": "A"}, {"id": "p2", "title": "B"}]
        sends = graph.fan_out_dfv({"ideas": ideas, "strategic_goal": "G", "dfv_scores": []})
        self.assertEqual(len(sends), len(graph.DFV_LENSES) * len(ideas))
        self.assertTrue(all(s.node == "score_one_dimension" for s in sends))
        lens_ids = {s.arg["lens"]["id"] for s in sends}
        self.assertEqual(lens_ids, {lens["id"] for lens in graph.DFV_LENSES})


class ScoreOneDimensionTests(unittest.TestCase):
    def _task(self):
        return {
            "lens": graph.DFV_LENSES[0], "strategic_goal": "G",
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


class AnalyzeAndScopeTests(unittest.TestCase):
    def _state(self):
        return {"topic": "如何提升訂閱率", "company": "北辰短影音"}

    def test_happy_path_parses_strategic_goal_and_interviewees(self):
        llm_response = graph.json.dumps({
            "five_forces": {
                "新進入者威脅": "低", "替代品威脅": "中", "顧客議價力": "高",
                "供應商議價力": "低", "現有競爭者強度": "中",
            },
            "trend_analysis": "訂閱疲勞持續加劇。",
            "strategic_goal": "推出深度內容訂閱制",
            "target_audience": "重視深度報導的通勤族",
            "interviewees": [
                {"id": "u1", "name": "陳先生", "age": 32, "context": "通勤族", "pain_points": ["沒空看新聞"], "tone": "直接"},
                {"id": "u2", "name": "林小姐", "age": 38, "context": "已訂閱其他媒體", "pain_points": ["內容重複"], "tone": "理性"},
                {"id": "u3", "name": "王先生", "age": 45, "context": "廣告主", "pain_points": ["觸及率不透明"], "tone": "直接"},
            ],
        })
        with mock.patch.object(graph, "web_search", return_value=[]), \
             mock.patch.object(graph, "call_llm", return_value=llm_response), \
             mock.patch.object(graph, "emit_event"):
            result = graph.analyze_and_scope(self._state())
        self.assertEqual(result["strategic_goal"], "推出深度內容訂閱制")
        self.assertEqual(len(result["interviewees"]), 3)
        self.assertFalse(result["used_fallback_interviewees"])

    def test_falls_back_to_load_users_when_interviewees_empty(self):
        llm_response = graph.json.dumps({
            "five_forces": {}, "trend_analysis": "", "strategic_goal": "", "target_audience": "",
            "interviewees": [],
        })
        fallback_users = [
            {"id": "u1", "name": "陳小姐"}, {"id": "u2", "name": "王先生"}, {"id": "u3", "name": "小宇"},
        ]
        with mock.patch.object(graph, "web_search", return_value=[]), \
             mock.patch.object(graph, "call_llm", return_value=llm_response), \
             mock.patch.object(graph, "emit_event"), \
             mock.patch.object(graph, "load_users", return_value=fallback_users):
            result = graph.analyze_and_scope(self._state())
        self.assertEqual(result["interviewees"], fallback_users)
        self.assertTrue(result["used_fallback_interviewees"])

    def test_falls_back_when_llm_output_unparseable(self):
        fallback_users = [{"id": "u1", "name": "陳小姐"}]
        with mock.patch.object(graph, "web_search", return_value=[]), \
             mock.patch.object(graph, "call_llm", return_value="不是合法 JSON"), \
             mock.patch.object(graph, "emit_event"), \
             mock.patch.object(graph, "load_users", return_value=fallback_users):
            result = graph.analyze_and_scope(self._state())
        self.assertEqual(result["interviewees"], fallback_users)
        self.assertTrue(result["used_fallback_interviewees"])
        self.assertTrue(result["strategic_goal"])  # 保底文字非空


class GeneratePersonasTests(unittest.TestCase):
    def test_happy_path(self):
        llm_response = graph.json.dumps({
            "personas": [
                {"id": "p1", "name": "林美", "role": "產品", "background": "b", "focus": ["f"], "style": "s"},
                {"id": "p2", "name": "陳亞", "role": "技術", "background": "b", "focus": ["f"], "style": "s"},
                {"id": "p3", "name": "周依", "role": "行銷", "background": "b", "focus": ["f"], "style": "s"},
            ],
        })
        with mock.patch.object(graph, "call_llm", return_value=llm_response), \
             mock.patch.object(graph, "emit_event"):
            result = graph.generate_personas({"strategic_goal": "G", "target_audience": "A"})
        self.assertEqual(len(result["personas"]), 3)
        self.assertFalse(result["used_fallback_personas"])

    def test_falls_back_when_too_few_personas(self):
        llm_response = graph.json.dumps({"personas": [{"id": "p1", "name": "只有一位"}]})
        fallback = [{"id": "p1", "name": "林美"}, {"id": "p2", "name": "陳亞"}, {"id": "p3", "name": "周依"}]
        with mock.patch.object(graph, "call_llm", return_value=llm_response), \
             mock.patch.object(graph, "emit_event"), \
             mock.patch.object(graph, "load_personas", return_value=fallback):
            result = graph.generate_personas({"strategic_goal": "G", "target_audience": "A"})
        self.assertEqual(result["personas"], fallback[:graph.N_PERSONAS])
        self.assertTrue(result["used_fallback_personas"])


class GenerateEvaluatorsTests(unittest.TestCase):
    def test_excludes_interviewee_names(self):
        llm_response = graph.json.dumps({
            "evaluators": [
                {"id": "e1", "name": "張三", "age": 30, "context": "c", "pain_points": [], "tone": "t"},
                {"id": "e2", "name": "李四", "age": 40, "context": "c", "pain_points": [], "tone": "t"},
                {"id": "e3", "name": "陳先生", "age": 32, "context": "c", "pain_points": [], "tone": "t"},  # 重複，該被排除
            ],
        })
        state = {"target_audience": "A", "interviewees": [{"name": "陳先生"}, {"name": "林小姐"}]}
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
        state = {"target_audience": "A", "interviewees": [{"name": "陳先生"}]}
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
            "strategic_goal": "G", "target_audience": "A",
            "insights": [{"id": "i1", "text": "洞見1"}],
            "research_items": [],
        }

    def test_idea_gets_its_own_bmc_and_filters_invalid_insight_refs(self):
        # 使用者要求改回每位 persona 自己設計自己的 BMC（不是全場共用一份
        # ——實測發現共用會壓低點子多樣性，見 note.md），draft_one_idea
        # 現在跟 idea 一起、同一次呼叫產生 bmc。
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
        # LLM 沒吐出合法 bmc 時（fan-out 平行呼叫，任何一位 persona 都可能
        # 格式不完整）不該讓整個 draft_one_idea 崩潰——_merge_bmc() 補上
        # 安全預設值，idea 仍然帶著結構合法（即使內容是保底空值）的 bmc。
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
            strategic_goal="推出訂閱制", target_audience="通勤族",
            five_forces={"新進入者威脅": "低"}, trend_analysis="趨勢文字",
            interviewees=[{"id": "u1", "name": "陳先生"}], used_fallback_interviewees=False,
            interview_transcript=[{"user_name": "陳先生", "round": 1, "question": "Q1", "answer": "A1"}],
            insights=[{"id": "i1", "text": "洞見1"}],
            personas=[{"id": "p1", "name": "林美"}, {"id": "p2", "name": "陳亞"}],
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
            "策略目標", "五力分析", "系統研究", "人類提問記錄",
            "DFV 結構化評分", "收斂結果", "Prototype", "Baseline 對照",
            "最終評估者對照評分",
        ):
            self.assertIn(marker, report)

    def test_report_handles_empty_human_qa_log(self):
        report = graph.build_final_report_markdown(**self._base_kwargs(human_qa_log=[]))
        self.assertIn("本場沒有人類提問", report)

    def test_report_includes_final_verdict(self):
        report = graph.build_final_report_markdown(**self._base_kwargs())
        self.assertIn("這是評語。", report)


if __name__ == "__main__":
    unittest.main()
