"""日卡结构：移动端手风琴依赖 day-mini（收起行）/ day-content（展开区）/ day-more（二级折叠）。

运行：python3 -m unittest discover -s tests
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import render_html  # noqa: E402


def seg(a, b, km=100.0, mins=120):
    return {"from": {"name": a}, "to": {"name": b}, "distance_km": km, "duration_min": mins}


class DayCardShell(unittest.TestCase):
    def test_has_collapsed_summary_and_content_wrapper(self):
        html = render_html.render_day({"day_index": 1, "date": "2026-09-25",
                                       "segments": [seg("北京", "张家口", 188.7, 186)]})
        self.assertIn('class="day-mini"', html)
        self.assertIn('class="day-content"', html)
        self.assertIn("北京 → 张家口", html)

    def test_city_day_falls_back_to_note_for_collapsed_line(self):
        html = render_html.render_day({"day_index": 4, "note": "老城深度游"})
        self.assertIn("老城深度游", html.split("day-mini")[1].split("</span>")[0])

    def test_tips_and_claims_folded_warnings_stay_visible(self):
        html = render_html.render_day({
            "day_index": 2,
            "tips": {"气温": "10-25℃"},
            "claims": [{"claim": "门票免费", "verdict": "verified"}],
            "warnings": [{"code": "SEASONAL_ROAD", "level": "warning", "detail": "冬季封路"}],
        })
        folded = html.split('class="day-more"')[1]
        self.assertIn("10-25℃", folded)
        self.assertIn("门票免费", folded)
        self.assertNotIn("冬季封路", folded)   # 安全信息不进折叠
        self.assertIn("冬季封路", html.split('class="day-more"')[0])


if __name__ == "__main__":
    unittest.main()
