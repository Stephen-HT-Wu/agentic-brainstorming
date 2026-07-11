import tempfile
from pathlib import Path
from unittest import mock
import unittest

import graph


PERSONAS = [
    {"id": "a", "name": "A"}, {"id": "b", "name": "B"},
    {"id": "c", "name": "C"}, {"id": "d", "name": "D"},
]


class ScoringFanOutTests(unittest.TestCase):
    def test_fan_out_scoring_excludes_self_and_covers_all_pairs(self):
        final_proposals = {p["id"]: {"title": f"T-{p['id']}"} for p in PERSONAS}
        sends = graph.fan_out_scoring({"personas": PERSONAS, "final_proposals": final_proposals, "scores": []})
        # 4 raters x 3 targets each (不評自己) = 12
        self.assertEqual(len(sends), 12)
        for s in sends:
            self.assertNotEqual(s.arg["rater"]["id"], s.arg["target_persona_id"])

    def test_score_proposal_clamps_and_falls_back_on_bad_score(self):
        task = {"rater": PERSONAS[0], "target_persona_id": "b", "proposal": {"title": "T", "summary": "S", "bmc": {}}}
        with mock.patch.object(graph, "call_llm", return_value='{"score": 99, "reason": "x"}'), \
             mock.patch.object(graph, "emit_event"):
            result = graph.score_proposal(task)
        self.assertEqual(result["scores"][0]["score"], 10.0)  # clamp 到上限

        with mock.patch.object(graph, "call_llm", return_value='{"score": "not-a-number"}'), \
             mock.patch.object(graph, "emit_event"):
            result2 = graph.score_proposal(task)
        self.assertEqual(result2["scores"][0]["score"], 5.0)  # 保底中位數


class ScoreAggregateTests(unittest.TestCase):
    def test_compute_aggregates_mean_and_stdev(self):
        scores = [
            {"target_persona_id": "a", "score": 8.0},
            {"target_persona_id": "a", "score": 6.0},
            {"target_persona_id": "b", "score": 5.0},
            {"target_persona_id": "b", "score": 5.0},
        ]
        agg = graph.compute_score_aggregates(scores, ["a", "b", "c"])
        self.assertAlmostEqual(agg["a"]["mean"], 7.0)
        self.assertAlmostEqual(agg["a"]["stdev"], 1.0)  # 8,6 的母體標準差
        self.assertAlmostEqual(agg["b"]["mean"], 5.0)
        self.assertAlmostEqual(agg["b"]["stdev"], 0.0)  # 意見完全一致，分歧度 0
        self.assertEqual(agg["c"], {"mean": 0.0, "stdev": 0.0, "n": 0})  # 沒收到任何評分

    def test_select_top_k_ranks_by_mean_descending(self):
        aggregates = {
            "a": {"mean": 7.0, "stdev": 1.0, "n": 3},
            "b": {"mean": 9.0, "stdev": 0.5, "n": 3},
            "c": {"mean": 5.0, "stdev": 2.0, "n": 3},
            "d": {"mean": 8.0, "stdev": 0.0, "n": 3},
        }
        self.assertEqual(graph.select_top_k(aggregates, 2), ["b", "d"])
        self.assertEqual(graph.select_top_k(aggregates, 10), ["b", "d", "a", "c"])  # k 超過人數就全選


class LandingPageTests(unittest.TestCase):
    def test_ensure_fields_falls_back_to_proposal_and_bmc(self):
        proposal = {
            "title": "一個很長的標題超過二十個字用來測試截斷行為看看會怎樣",
            "summary": "摘要",
            "bmc": {"客群": "上班族", "價值主張": "省時間", "通路": "App"},
        }
        fixed = graph._ensure_landing_page_fields({}, proposal)
        self.assertTrue(fixed["headline"])
        self.assertLessEqual(len(fixed["headline"]), 20)
        self.assertTrue(fixed["features"])  # 從 BMC 補上
        self.assertEqual(fixed["cta_text"], "了解更多")

    def test_render_html_escapes_dangerous_content(self):
        data = {
            "headline": "<script>alert(1)</script>",
            "subheadline": "sub",
            "features": [{"title": "f1", "desc": "d1"}],
            "cta_text": "go",
            "concept_one_pager": "concept",
        }
        page = graph.render_landing_page_html(data, {})
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertTrue(page.startswith("<!doctype html>"))


class DiffProposalsTests(unittest.TestCase):
    def test_diff_is_empty_for_identical_proposals(self):
        p = {"title": "T", "summary": "S", "bmc": {"客群": "x"}}
        self.assertEqual(graph._diff_proposals(p, dict(p)), "")

    def test_diff_is_nonempty_when_title_changes(self):
        before = {"title": "舊標題", "summary": "S", "bmc": {"客群": "x"}}
        after = {"title": "新標題", "summary": "S", "bmc": {"客群": "x"}}
        diff = graph._diff_proposals(before, after)
        self.assertIn("舊標題", diff)
        self.assertIn("新標題", diff)


class PrototypeTestFanOutTests(unittest.TestCase):
    def test_fan_out_prototypes_one_send_per_top_k_item(self):
        items = [{"persona": PERSONAS[0], "proposal": {"title": "T1"}}, {"persona": PERSONAS[1], "proposal": {"title": "T2"}}]
        sends = graph.fan_out_prototypes({"top_k_items": items, "users": [], "round_id": "r1", "prototypes": []})
        self.assertEqual(len(sends), 2)
        self.assertTrue(all(s.node == "generate_prototype_and_test" for s in sends))


class GeneratePrototypeAndTestTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.patcher = mock.patch.object(graph, "PROTOTYPE_DIR", Path(self.tmpdir.name))
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmpdir.cleanup()

    def test_full_flow_writes_html_and_produces_nonzero_diff(self):
        persona = {"id": "a", "name": "A", "role": "R"}
        proposal = {
            "title": "原標題", "summary": "原摘要",
            "bmc": {k: "內容" for k in graph.BMC_KEYS},
            "self_score": 7.0,
        }
        users = [{"id": "u1", "name": "U1", "age": 30, "context": "c", "pain_points": [], "tone": "t"}]
        task = {"persona": persona, "proposal": proposal, "users": users, "round_id": "r1"}

        call_sequence = [
            '{"headline":"新標題H","subheadline":"副標","features":[{"title":"快","desc":"快速"}],'
            '"cta_text":"馬上用","concept_one_pager":"概念說明文字"}',
            "這個概念聽起來還不錯，但我擔心隱私問題。",
            '{"title":"修正後標題","summary":"修正後摘要","bmc":{' +
            ",".join(f'"{k}":"新內容"' for k in graph.BMC_KEYS) + '},'
            '"self_score":8,"score_reason":"r","revision_note":"回應U1的隱私疑慮，補充資料保護說明"}',
        ]
        with mock.patch.object(graph, "call_llm", side_effect=call_sequence), \
             mock.patch.object(graph, "emit_event"):
            result = graph.generate_prototype_and_test(task)

        proto = result["prototypes"][0]
        self.assertEqual(proto["persona_id"], "a")
        self.assertTrue(Path(proto["html_path"]).exists())
        html_content = Path(proto["html_path"]).read_text(encoding="utf-8")
        self.assertIn("新標題H", html_content)
        self.assertEqual(len(proto["reactions"]), 1)
        self.assertIn("隱私", proto["reactions"][0]["reaction"])
        self.assertGreater(proto["embedding_distance"], 0.0)
        self.assertIn("修正後標題", proto["diff_text"])
        self.assertEqual(proto["after"]["title"], "修正後標題")


class FacilitatorStillRoutesToMastersTests(unittest.TestCase):
    """回歸測試：確保 stage8 沿用 stage7 的路由目標，沒有不小心改回 END。"""

    def test_llm_end_decision_routes_to_run_masters(self):
        state = {
            "personas": PERSONAS[:1],
            "facilitator_log": [{"round": 1, "action": "present", "chosen_persona_id": "a", "reason": "x"}],
            "idea_pool_versions": [], "persona_results": [],
        }
        with mock.patch.object(graph, "call_llm", return_value='{"action":"end","reason":"done"}'), \
             mock.patch.object(graph, "emit_event"):
            cmd = graph.facilitator_decide(state)
        self.assertEqual(cmd.goto, "run_masters")


class RunCollectiveScoringAndPrototypeIntegrationTests(unittest.TestCase):
    """mock 掉巢狀子圖的 invoke，驗證 run_collective_scoring／run_prototype_test
    兩個父節點正確組裝輸入、正確從回傳值抽出 state 更新，不需要真的花錢。"""

    def test_run_collective_scoring_picks_top_k_from_mocked_scores(self):
        state = {
            "personas": PERSONAS,
            "idea_pool_versions": [
                {"persona_id": p["id"], "proposal_after": {"title": f"T-{p['id']}"}} for p in PERSONAS
            ],
        }

        def fake_scoring_invoke(payload):
            # b 分數最高、c 最低；a 的評分者之間刻意給不同分數製造真實分歧度
            base_map = {"a": 7.0, "b": 9.0, "c": 3.0, "d": 6.0}
            rater_order = [p["id"] for p in payload["personas"]]
            scores = []
            for r in payload["personas"]:
                for pid in payload["final_proposals"]:
                    if pid == r["id"]:
                        continue
                    jitter = 2.0 if (pid == "a" and rater_order.index(r["id"]) % 2 == 0) else 0.0
                    scores.append({
                        "rater_id": r["id"], "target_persona_id": pid,
                        "score": base_map[pid] + jitter,
                    })
            return {"scores": scores}

        with mock.patch.object(graph.scoring_panel_graph, "invoke", side_effect=fake_scoring_invoke), \
             mock.patch.object(graph, "emit_event"):
            result = graph.run_collective_scoring(state)

        self.assertEqual(set(result["top_k_ids"]), {"b", "d", "a"})  # top-3 by mean，c 被刷掉
        self.assertIn("c", result["score_aggregates"])
        self.assertGreater(result["score_aggregates"]["a"]["stdev"], 0)  # 有真實分歧度數字可看（除非剛好全一致）

    def test_run_prototype_test_builds_correct_top_k_items(self):
        state = {
            "personas": PERSONAS,
            "idea_pool_versions": [
                {"persona_id": p["id"], "proposal_after": {"title": f"T-{p['id']}"}} for p in PERSONAS
            ],
            "top_k_ids": ["b", "d"],
            "users": [],
            "round_id": "r1",
        }
        captured = {}

        def fake_invoke(payload):
            captured["payload"] = payload
            return {"prototypes": [{"persona_id": "b"}, {"persona_id": "d"}]}

        with mock.patch.object(graph.prototype_test_graph, "invoke", side_effect=fake_invoke):
            result = graph.run_prototype_test(state)

        ids_sent = {item["persona"]["id"] for item in captured["payload"]["top_k_items"]}
        self.assertEqual(ids_sent, {"b", "d"})
        self.assertEqual(len(result["prototypes"]), 2)


if __name__ == "__main__":
    unittest.main()
