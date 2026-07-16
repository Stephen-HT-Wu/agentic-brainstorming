import unittest
from unittest import mock

import company_research as cr


class TextStripperTests(unittest.TestCase):
    def test_strips_tags_and_skips_script_style(self):
        html = "<html><head><style>.x{color:red}</style></head>" \
               "<body><h1>標題</h1><p>內文一</p><script>alert(1)</script></body></html>"
        stripper = cr._TextStripper()
        stripper.feed(html)
        text = " ".join(stripper.chunks)
        self.assertIn("標題", text)
        self.assertIn("內文一", text)
        self.assertNotIn("alert", text)
        self.assertNotIn("color:red", text)


class FetchUrlTextTests(unittest.TestCase):
    def test_uses_tavily_when_api_key_present(self):
        with mock.patch.dict(cr.os.environ, {"TAVILY_API_KEY": "k"}), \
             mock.patch.object(cr, "_tavily_extract", return_value="抓到的內容") as mock_tavily, \
             mock.patch.object(cr, "_fetch_url_stdlib") as mock_stdlib:
            text = cr.fetch_url_text("https://example.com")
        self.assertEqual(text, "抓到的內容")
        mock_tavily.assert_called_once()
        mock_stdlib.assert_not_called()

    def test_falls_back_to_stdlib_when_tavily_empty(self):
        with mock.patch.dict(cr.os.environ, {"TAVILY_API_KEY": "k"}), \
             mock.patch.object(cr, "_tavily_extract", return_value=""), \
             mock.patch.object(cr, "_fetch_url_stdlib", return_value="stdlib 內容") as mock_stdlib:
            text = cr.fetch_url_text("https://example.com")
        self.assertEqual(text, "stdlib 內容")
        mock_stdlib.assert_called_once()

    def test_no_api_key_goes_straight_to_stdlib(self):
        with mock.patch.dict(cr.os.environ, {}, clear=True), \
             mock.patch.object(cr, "_fetch_url_stdlib", return_value="stdlib 內容") as mock_stdlib:
            text = cr.fetch_url_text("https://example.com")
        self.assertEqual(text, "stdlib 內容")
        mock_stdlib.assert_called_once()


class ResearchCompanyTests(unittest.TestCase):
    def test_uses_fetched_url_text_when_available(self):
        with mock.patch.object(cr, "fetch_url_text", return_value="公司網頁內容"), \
             mock.patch.object(cr.sg, "call_llm", return_value="整理後的公司描述") as mock_llm:
            result = cr.research_company("某公司", ["https://example.com"])
        self.assertEqual(result["markdown"], "整理後的公司描述")
        self.assertTrue(result["sources"][0]["used"])
        self.assertEqual(result["sources"][0]["method"], "fetch")
        user_prompt = mock_llm.call_args[0][2]
        self.assertIn("公司網頁內容", user_prompt)

    def test_falls_back_to_web_search_when_fetch_fails(self):
        hits = [{"title": "某公司官網", "url": "https://example.com", "snippet": "服務內容摘要"}]
        with mock.patch.object(cr, "fetch_url_text", return_value=""), \
             mock.patch.object(cr.sg, "web_search", return_value=hits), \
             mock.patch.object(cr.sg, "is_usable_search_result", return_value=True), \
             mock.patch.object(cr.sg, "call_llm", return_value="整理後的公司描述") as mock_llm:
            result = cr.research_company("某公司", ["https://example.com"])
        self.assertEqual(result["sources"][0]["method"], "search_fallback")
        self.assertTrue(result["sources"][0]["used"])
        user_prompt = mock_llm.call_args[0][2]
        self.assertIn("服務內容摘要", user_prompt)

    def test_no_urls_uses_web_search_directly(self):
        hits = [{"title": "某公司", "url": "https://example.com", "snippet": "官網描述"}]
        with mock.patch.object(cr.sg, "web_search", return_value=hits) as mock_search, \
             mock.patch.object(cr.sg, "is_usable_search_result", return_value=True), \
             mock.patch.object(cr.sg, "call_llm", return_value="整理後的公司描述"):
            result = cr.research_company("某公司", [])
        mock_search.assert_called_once()
        self.assertEqual(result["sources"][0]["method"], "search")

    def test_no_materials_still_produces_result_without_crashing(self):
        with mock.patch.object(cr, "fetch_url_text", return_value=""), \
             mock.patch.object(cr.sg, "web_search", return_value=[]), \
             mock.patch.object(cr.sg, "call_llm", return_value="推測性描述") as mock_llm:
            result = cr.research_company("某公司", ["https://dead-link.example"])
        self.assertEqual(result["markdown"], "推測性描述")
        user_prompt = mock_llm.call_args[0][2]
        self.assertIn("沒有抓到任何素材", user_prompt)


class WriteCompanyProfileTests(unittest.TestCase):
    def test_actually_writes_file_with_expected_name(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(cr.sg, "PRACTICE_DIR", Path(tmp)):
                path = cr.write_company_profile("acme", "# Acme\n測試內容")
            self.assertEqual(path.name, "acme-company.md")
            self.assertEqual(path.read_text(encoding="utf-8"), "# Acme\n測試內容")


if __name__ == "__main__":
    unittest.main()
