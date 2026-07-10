import unittest
from unittest import mock

import graph


class Stage4UnitTests(unittest.TestCase):
    def test_pad_to_three_truncates_and_pads(self):
        self.assertEqual(len(graph._pad_to_three(["a", "b", "c", "d"], "x")), 3)
        padded = graph._pad_to_three(["a"], "洞見")
        self.assertEqual(len(padded), 3)
        self.assertEqual(padded[0], "a")
        self.assertTrue(padded[1].startswith("洞見"))
        self.assertEqual(graph._pad_to_three(None, "x"), [f"x #{i}（系統保底，模型未提供足夠項目）" for i in (1, 2, 3)])

    def test_ensure_review_shape_always_has_exact_3_3_3(self):
        reviewer = {"id": "r1", "name": "審閱者"}
        data = {"agreements": ["a1", "a2"], "disagreements": [], "insights": ["i1", "i2", "i3", "i4"]}
        review = graph._ensure_review_shape(data, reviewer, "發表者")
        self.assertEqual(len(review["agreements"]), 3)
        self.assertEqual(len(review["disagreements"]), 3)
        self.assertEqual(len(review["insights"]), 3)
        self.assertEqual(review["reviewer_id"], "r1")
        self.assertEqual(review["presenter_name"], "發表者")

    def test_fan_out_reviewers_excludes_presenter(self):
        state = {
            "presenter_id": "p1",
            "presenter_name": "P1",
            "proposal": {"title": "T"},
            "reviewers": [{"id": "p2"}, {"id": "p3"}],
            "reviews": [],
            "revised_proposal": {},
        }
        sends = graph.fan_out_reviewers(state)
        self.assertEqual(len(sends), 2)
        self.assertTrue(all(s.node == "give_feedback" for s in sends))
        self.assertEqual([s.arg["reviewer"]["id"] for s in sends], ["p2", "p3"])
        self.assertTrue(all(s.arg["presenter_name"] == "P1" for s in sends))

    def test_ensure_revision_fields_preserves_pov_hmw_and_filters_bad_ids(self):
        original = {
            "pov": "原始 POV", "hmw": "原始 HMW",
            "insight_refs": ["i1"], "hmw_response": "原本的回應", "self_score": 8.0,
            "sources": [{"title": "s"}],
        }
        revised = {"addressed_reviewer_ids": ["bogus"], "hmw_response": "", "revision_note": ""}
        reviews = [
            {"reviewer_id": "r1", "reviewer_name": "審閱者一"},
            {"reviewer_id": "r2", "reviewer_name": "審閱者二"},
        ]
        fixed = graph._ensure_revision_fields(revised, original, reviews)
        self.assertEqual(fixed["pov"], "原始 POV")
        self.assertEqual(fixed["hmw"], "原始 HMW")
        self.assertEqual(fixed["insight_refs"], ["i1"])
        self.assertEqual(fixed["addressed_reviewer_ids"], ["r1"])  # 壞 id 被換成保底的第一個合法 id
        self.assertEqual(fixed["hmw_response"], "原本的回應")  # 空字串沿用原值
        self.assertTrue(fixed["revision_note"])  # 保底文字非空

    def test_ensure_revision_fields_keeps_valid_reviewer_refs(self):
        original = {"pov": "p", "hmw": "h", "insight_refs": [], "self_score": 7}
        revised = {"addressed_reviewer_ids": ["r2", "bogus"], "revision_note": "回應了 r2 的異議"}
        reviews = [{"reviewer_id": "r1", "reviewer_name": "一"}, {"reviewer_id": "r2", "reviewer_name": "二"}]
        fixed = graph._ensure_revision_fields(revised, original, reviews)
        self.assertEqual(fixed["addressed_reviewer_ids"], ["r2"])
        self.assertEqual(fixed["revision_note"], "回應了 r2 的異議")

    def test_ensure_revision_fields_resolves_reviewer_names_to_ids(self):
        """真實跑測踩過的情況：模型在 addressed_reviewer_ids 裡混填中文姓名
        而非 id，嚴格 id 比對會把它們全部濾掉——這裡驗證姓名也能正確解析。"""
        original = {"pov": "p", "hmw": "h", "insight_refs": [], "self_score": 7}
        reviews = [
            {"reviewer_id": "mei", "reviewer_name": "林美華"},
            {"reviewer_id": "alex", "reviewer_name": "陳建宏"},
            {"reviewer_id": "victor", "reviewer_name": "王承翰"},
        ]
        revised = {"addressed_reviewer_ids": ["林美華", "陳建宏", "王承翰"], "revision_note": "回應三人"}
        fixed = graph._ensure_revision_fields(revised, original, reviews)
        self.assertEqual(fixed["addressed_reviewer_ids"], ["mei", "alex", "victor"])

    def test_ensure_revision_fields_dedupes_mixed_id_and_name_for_same_reviewer(self):
        original = {"pov": "p", "hmw": "h", "insight_refs": [], "self_score": 7}
        reviews = [{"reviewer_id": "alex", "reviewer_name": "陳建宏"}]
        revised = {"addressed_reviewer_ids": ["alex", "陳建宏"], "revision_note": "x"}
        fixed = graph._ensure_revision_fields(revised, original, reviews)
        self.assertEqual(fixed["addressed_reviewer_ids"], ["alex"])

    def test_run_presentation_rounds_produces_one_version_per_presenter(self):
        """monkeypatch review_round_graph.invoke，驗證固定順序逐一發表、
        idea_pool_versions/final_proposals 都齊全，不需要真的打 API。"""
        personas = [
            {"id": "a", "name": "A"},
            {"id": "b", "name": "B"},
            {"id": "c", "name": "C"},
        ]
        persona_results = [
            {"persona": {"id": p["id"], "name": p["name"]}, "proposal": {"title": f"提案-{p['id']}", "bmc": {}}}
            for p in personas
        ]

        def fake_invoke(payload):
            reviewers = payload["reviewers"]
            reviews = [
                {
                    "reviewer_id": r["id"], "reviewer_name": r["name"],
                    "presenter_name": payload["presenter_name"],
                    "agreements": ["a1", "a2", "a3"],
                    "disagreements": ["d1", "d2", "d3"],
                    "insights": ["i1", "i2", "i3"],
                    "hmw_addressed": True, "hmw_addressed_reason": "ok",
                }
                for r in reviewers
            ]
            revised = dict(payload["proposal"])
            revised["title"] = payload["proposal"]["title"] + "-revised"
            revised["addressed_reviewer_ids"] = [reviewers[0]["id"]] if reviewers else []
            revised["revision_note"] = "調整了商業模式"
            return {"reviews": reviews, "revised_proposal": revised}

        with mock.patch.object(graph, "review_round_graph") as mock_graph, \
             mock.patch.object(graph, "emit_event"):
            mock_graph.invoke.side_effect = fake_invoke
            result = graph.run_presentation_rounds({
                "personas": personas,
                "persona_results": persona_results,
            })

        self.assertEqual(len(result["idea_pool_versions"]), 3)
        self.assertEqual(len(result["review_log"]), 3 * 2)  # 每人被另外 2 人審閱
        self.assertEqual(set(result["final_proposals"].keys()), {"a", "b", "c"})
        for pid, proposal in result["final_proposals"].items():
            self.assertTrue(proposal["title"].endswith("-revised"))

    def test_bmc_requires_exact_nonempty_nine_fields(self):
        valid = {key: "內容" for key in graph.BMC_KEYS}
        self.assertEqual(graph.assert_bmc_complete({"bmc": valid}), [])
        invalid = dict(valid, 額外="不允許")
        self.assertTrue(graph.assert_bmc_complete({"bmc": invalid}))

    def test_extract_json_object_handles_non_dict_json(self):
        self.assertEqual(graph.extract_json_object("[1, 2, 3]"), {})
        self.assertEqual(graph.extract_json_object('{"pov": "p"}'), {"pov": "p"})


if __name__ == "__main__":
    unittest.main()
