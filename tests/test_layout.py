"""日卡结构：移动端手风琴依赖 day-mini（收起行）/ day-content（展开区）/ day-more（二级折叠）。

运行：python3 -m unittest discover -s tests
"""
import html as htmllib
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import render_html  # noqa: E402


def seg(a, b, km=100.0, mins=120):
    return {"from": {"name": a}, "to": {"name": b}, "distance_km": km, "duration_min": mins}


def mini_text(html):
    """只取收起行里的文字：路线名也会出现在标题里，整段搜会误判。"""
    m = re.search(r'class="day-mini">(.*?)</span>', html)
    return htmllib.unescape(m.group(1)) if m else ""


class DayCardShell(unittest.TestCase):
    def test_has_collapsed_summary_and_content_wrapper(self):
        html = render_html.render_day({"day_index": 1, "date": "2026-09-25",
                                       "segments": [seg("北京", "张家口", 188.7, 186)]})
        self.assertIn('class="day-content"', html)
        self.assertEqual(mini_text(html), "北京 → 张家口 · 189km")

    def test_city_day_falls_back_to_note_for_collapsed_line(self):
        html = render_html.render_day({"day_index": 4, "note": "老城深度游"})
        self.assertEqual(mini_text(html), "老城深度游")

    def test_tips_and_claims_folded_warnings_stay_visible(self):
        html = render_html.render_day({
            "day_index": 2,
            "tips": {"气温": "10-25℃"},
            "claims": [{"claim": "门票免费", "verdict": "confirmed"}],
            "warnings": [{"code": "SEASONAL_ROAD", "level": "warning", "detail": "冬季封路"}],
        })
        folded = html.split('class="day-more"')[1]
        self.assertIn("10-25℃", folded)
        self.assertIn("门票免费", folded)
        self.assertIn("v-ok", folded)          # 已核验项走安静分支，不再当问题项渲染
        self.assertNotIn("冬季封路", folded)   # 安全信息不进折叠
        self.assertIn("冬季封路", html.split('class="day-more"')[0])

    def test_claims_only_day_still_renders_fold(self):
        # 没有建议、只有核验记录的日子，折叠区不能丢
        html = render_html.render_day({"day_index": 3, "claims": [
            {"claim": "景区需预约", "verdict": "unverified"}]})
        folded = html.split('class="day-more')[1]
        self.assertIn("景区需预约", folded)
        self.assertIn("1 项待核实", folded)

    def test_problem_claims_are_counted_in_summary(self):
        html = render_html.render_day({"day_index": 5, "claims": [
            {"claim": "A", "verdict": "confirmed"},
            {"claim": "B", "verdict": "contradicted"}]})
        self.assertIn('class="day-more has-problems"', html)
        summary = re.search(r'<summary>(.*?)</summary>', html).group(1)
        self.assertEqual(summary, "1 项待核实")  # 精确值：子串断言对「11 项待核实」也会通过

    def test_more_fold_open_by_default(self):
        # 桌面端依赖这个 open：删掉它二级折叠在桌面就永远展开不了（summary 被隐藏）
        html = render_html.render_day({"day_index": 1, "claims": [
            {"claim": "A", "verdict": "confirmed"}]})
        self.assertRegex(html, r'<details class="day-more[^"]*" open>')

    def test_mobile_preview_banner_does_not_pin_layout_to_one_screen(self):
        # 同 specificity 的移动覆盖必须在基础规则之后；否则 sticky 地图滚动到中途就离屏
        mobile = render_html.TEMPLATE.split("@media (max-width: 900px)", 1)[1]
        self.assertIn(".preview-banner ~ .layout { height:auto; }", mobile)
        self.assertIn(".preview-banner ~ .layout #map { top:var(--preview-banner-height, 3rem); }", mobile)
        # 全屏选择器必须同时盖过横幅下的 sticky top，否则 100vh 底部会超出视口
        self.assertIn("body.map-full .layout #map { height:100vh; position:fixed; inset:0; }", mobile)
        self.assertIn("new ResizeObserver(syncPreviewOffset)", render_html.TEMPLATE)


if __name__ == "__main__":
    unittest.main()
