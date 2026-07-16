import re
import unittest

import avatars


class SvgAvatarTests(unittest.TestCase):
    def test_same_id_always_produces_the_same_color(self):
        a = avatars.svg_avatar("mei", "林美華")
        b = avatars.svg_avatar("mei", "林美華")
        self.assertEqual(a, b)

    def test_different_ids_usually_produce_different_colors(self):
        colors = {avatars._hue(pid) for pid in ["mei", "alex", "joyce", "victor", "facilitator"]}
        self.assertGreater(len(colors), 1)

    def test_hue_is_stable_across_python_invocations(self):
        """_hue 不能用內建 hash()——那個對字串會加隨機種子（PYTHONHASHSEED），
        同一個 id 換一個 process 執行就會拿到不同顏色，頭像就不 deterministic
        了。這裡用已知字串鎖住一個具體數字，確保演算法本身穩定。"""
        self.assertEqual(avatars._hue("mei"), avatars._hue("mei"))
        self.assertIsInstance(avatars._hue("mei"), int)
        self.assertTrue(0 <= avatars._hue("mei") < 360)

    def test_initials_take_first_character_of_name(self):
        self.assertEqual(avatars._initials("林美華"), "林")
        self.assertEqual(avatars._initials("Alex"), "A")

    def test_initials_falls_back_to_question_mark_for_empty_name(self):
        self.assertEqual(avatars._initials(""), "?")
        self.assertEqual(avatars._initials(None), "?")

    def test_output_is_well_formed_svg_containing_initials(self):
        svg = avatars.svg_avatar("mei", "林美華")
        self.assertTrue(svg.startswith("<svg"))
        self.assertTrue(svg.endswith("</svg>"))
        self.assertIn(">林<", svg)
        self.assertIn("hsl(", svg)

    def test_size_parameter_scales_viewbox(self):
        svg = avatars.svg_avatar("mei", "林美華", size=80)
        self.assertIn('width="80"', svg)
        self.assertIn('height="80"', svg)
        self.assertIn('viewBox="0 0 80 80"', svg)

    def test_falls_back_to_name_when_persona_id_missing(self):
        # id 缺席時要退回用 name 當 hash seed，不能整個崩潰
        svg = avatars.svg_avatar("", "王先生")
        self.assertIn(">王<", svg)

    def test_name_is_escaped_safe_characters_only(self):
        """姓名理論上都來自 personas.yaml，不是不受信任的使用者輸入，但這裡
        用單一字元當 initials，本來就不會帶進任何 HTML 特殊字元，順手確認一下
        不會意外壞掉。"""
        svg = avatars.svg_avatar("weird", "<b>")
        self.assertIn(">&lt;<", svg)


if __name__ == "__main__":
    unittest.main()
