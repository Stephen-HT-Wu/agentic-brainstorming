import unittest

import graph


class Stage1UnitTests(unittest.TestCase):
    def test_chinese_near_duplicate_has_shared_features(self):
        distance = graph.embedding_distance(
            "新聞短影音互動策略", "新聞短影音的互動策略"
        )
        self.assertLess(distance, 0.35)

    def test_search_result_filter_rejects_ads_and_empty_urls(self):
        self.assertFalse(graph.is_usable_search_result({"url": ""}))
        self.assertFalse(
            graph.is_usable_search_result({"url": "https://www.bing.com/aclick?x=1"})
        )
        self.assertTrue(
            graph.is_usable_search_result({"url": "https://example.com/research"})
        )

    def test_bmc_requires_exact_nonempty_nine_fields(self):
        valid = {key: "內容" for key in graph.BMC_KEYS}
        self.assertEqual(graph.assert_bmc_complete({"bmc": valid}), [])
        invalid = dict(valid, 額外="不允許")
        self.assertTrue(graph.assert_bmc_complete({"bmc": invalid}))
        invalid = dict(valid)
        invalid[graph.BMC_KEYS[0]] = []
        self.assertTrue(graph.assert_bmc_complete({"bmc": invalid}))

    def test_repeated_node_invocations_do_not_share_usage(self):
        graph.usage_log.clear()

        def node(_state):
            graph.usage_log.append({
                "node": graph.current_node(),
                "invocation": graph.current_invocation(),
                "model": graph.CHEAP_MODEL,
                "input": 10,
                "output": 5,
            })
            return graph.current_invocation()

        wrapped = graph.instrument("refine", node)
        first = wrapped({})
        second = wrapped({})
        self.assertNotEqual(first, second)
        self.assertEqual(
            [e["invocation"] for e in graph.usage_log].count(first), 1
        )


if __name__ == "__main__":
    unittest.main()
