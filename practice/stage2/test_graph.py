import unittest

import graph
from langgraph.types import Send


class Stage2UnitTests(unittest.TestCase):
    def test_fan_out_produces_one_send_per_persona(self):
        personas = [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}]
        sends = graph.fan_out_personas({
            "topic": "t",
            "company": "c",
            "personas": personas,
            "persona_results": [],
        })
        self.assertEqual(len(sends), 2)
        self.assertTrue(all(isinstance(s, Send) for s in sends))
        self.assertTrue(all(s.node == "homework_worker" for s in sends))
        self.assertEqual([s.arg["persona"]["id"] for s in sends], ["a", "b"])
        self.assertTrue(all(s.arg["topic"] == "t" and s.arg["company"] == "c" for s in sends))

    def test_pairwise_diversity_higher_for_unrelated_topics(self):
        similar = [
            {"title": "A", "summary": "新聞短影音互動策略", "bmc": {}},
            {"title": "B", "summary": "新聞短影音的互動策略", "bmc": {}},
        ]
        different = [
            {"title": "A", "summary": "新聞短影音互動策略", "bmc": {}},
            {"title": "C", "summary": "量子電腦晶片供應鏈風險評估", "bmc": {}},
        ]
        d_similar = graph.pairwise_diversity(similar)["avg_distance"]
        d_different = graph.pairwise_diversity(different)["avg_distance"]
        self.assertLess(d_similar, d_different)

    def test_dedup_proposals_collapses_near_duplicates(self):
        proposals = [
            {"title": "A", "summary": "新聞短影音互動策略", "bmc": {}},
            {"title": "A2", "summary": "新聞短影音的互動策略", "bmc": {}},
            {"title": "B", "summary": "量子電腦晶片供應鏈風險評估", "bmc": {}},
        ]
        distinct = graph.dedup_proposals(proposals, threshold=0.5)
        self.assertEqual(len(distinct), 2)

    def test_role_cost_splits_interleaved_usage_log(self):
        """模擬平行執行時兩個 persona 的 usage_log 交錯寫入，_role_cost 仍要能正確拆分。"""
        graph.usage_log.clear()
        graph.usage_log.extend([
            {"node": "collect", "role": "persona:A", "model": graph.CHEAP_MODEL, "input": 100, "output": 50},
            {"node": "collect", "role": "persona:B", "model": graph.CHEAP_MODEL, "input": 200, "output": 50},
            {"node": "refine", "role": "persona:A", "model": graph.CHEAP_MODEL, "input": 100, "output": 50},
        ])
        cost_a = graph._role_cost("persona:A")
        cost_b = graph._role_cost("persona:B")
        self.assertGreater(cost_a, cost_b)
        self.assertAlmostEqual(cost_a + cost_b, graph.total_cost(), places=9)

    def test_bmc_requires_exact_nonempty_nine_fields(self):
        valid = {key: "內容" for key in graph.BMC_KEYS}
        self.assertEqual(graph.assert_bmc_complete({"bmc": valid}), [])
        invalid = dict(valid, 額外="不允許")
        self.assertTrue(graph.assert_bmc_complete({"bmc": invalid}))

    def test_search_result_filter_rejects_ads_and_empty_urls(self):
        self.assertFalse(graph.is_usable_search_result({"url": ""}))
        self.assertFalse(
            graph.is_usable_search_result({"url": "https://www.bing.com/aclick?x=1"})
        )
        self.assertTrue(
            graph.is_usable_search_result({"url": "https://example.com/research"})
        )


if __name__ == "__main__":
    unittest.main()
