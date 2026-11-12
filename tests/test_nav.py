"""一键导航：当天途经点提取 + 高德 App 唤起链接。

运行：python3 -m unittest discover -s tests
"""
import sys
import unittest
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import render_html  # noqa: E402


def P(name, lng, lat):
    return {"name": name, "lnglat": [lng, lat], "source": "amap"}


def seg(a, b):
    return {"from": a, "to": b}


BJ, ZJK, WLCB, HHHT = P("北京", 116.4, 39.9), P("张家口", 114.9, 40.8), P("乌兰察布", 113.1, 41.0), P("呼和浩特", 111.7, 40.8)


def params(url):
    """iosamap://path?... / amapuri://route/plan/?... 的查询参数（值已解码）。"""
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query, keep_blank_values=True).items()}


class DayNavPoints(unittest.TestCase):
    def test_segment_chain_excludes_start(self):
        # 起点交给高德用"我的位置"，只取各段终点：途经点在前、终点在最后
        day = {"segments": [seg(BJ, ZJK), seg(ZJK, WLCB), seg(WLCB, HHHT)]}
        self.assertEqual([p["name"] for p in render_html.day_nav_points(day)], ["张家口", "乌兰察布", "呼和浩特"])

    def test_no_segments_falls_back_to_stops_then_overnight(self):
        day = {"segments": [], "stops": [P("大召寺", 111.66, 40.80), {"name": "无坐标点"}],
               "overnight": HHHT}
        self.assertEqual([p["name"] for p in render_html.day_nav_points(day)], ["大召寺", "呼和浩特"])

    def test_points_without_coordinates_are_skipped(self):
        day = {"segments": [seg(BJ, {"name": "张家口"}), seg({"name": "张家口"}, WLCB)]}
        self.assertEqual([p["name"] for p in render_html.day_nav_points(day)], ["乌兰察布"])

    def test_consecutive_duplicates_collapse(self):
        day = {"segments": [], "stops": [HHHT], "overnight": HHHT}
        self.assertEqual(len(render_html.day_nav_points(day)), 1)

    def test_nothing_to_navigate(self):
        self.assertEqual(render_html.day_nav_points({"segments": [], "stops": []}), [])


class AmapNavUrls(unittest.TestCase):
    def test_none_without_points(self):
        self.assertIsNone(render_html.amap_nav_urls([]))

    def test_destination_and_waypoints(self):
        urls = render_html.amap_nav_urls([ZJK, WLCB, HHHT])
        self.assertTrue(urls["android"].startswith("amapuri://route/plan/?"))
        self.assertTrue(urls["ios"].startswith("iosamap://path?"))
        for url in urls.values():
            q = params(url)
            self.assertEqual(q["dname"], "呼和浩特")
            self.assertEqual((q["dlat"], q["dlon"]), ("40.8", "111.7"))
            self.assertEqual(q["vian"], "2")
            self.assertEqual(q["vianames"], "张家口|乌兰察布")
            self.assertEqual(q["vialons"], "114.9|113.1")
            self.assertEqual(q["vialats"], "40.8|41.0")
            self.assertEqual(q["t"], "0")    # 驾车
            self.assertEqual(q["dev"], "0")  # 高德坐标已是 GCJ-02，不再偏移
            self.assertEqual(q["slat"], "")  # 起点留空 = 当前位置
            self.assertEqual(q["sourceApplication"], "roadbook")

    def test_single_point_has_no_waypoints(self):
        q = params(render_html.amap_nav_urls([ZJK])["ios"])
        self.assertEqual(q["dname"], "张家口")
        self.assertNotIn("vian", q)

    def test_separator_in_name_does_not_break_waypoint_list(self):
        q = params(render_html.amap_nav_urls([P("A|B", 1, 2), ZJK])["android"])
        self.assertEqual(q["vianames"].split("|"), ["A B"])

    def test_names_are_percent_encoded(self):
        url = render_html.amap_nav_urls([P("五塔寺 & 博物院", 1, 2)])["ios"]
        self.assertNotIn(" ", url)
        self.assertIn("dname=" + "%E4%BA%94", url)
        self.assertEqual(params(url)["dname"], "五塔寺 & 博物院")

    def test_origin_supplies_start_point(self):
        # 翻看不是今天的日子时用行程起点，而不是手机当前位置
        q = params(render_html.amap_nav_urls([ZJK, WLCB], origin=BJ)["android"])
        self.assertEqual((q["slat"], q["slon"], q["sname"]), ("39.9", "116.4", "北京"))
        self.assertNotIn("北京", q["vianames"].split("|"))  # 起点不进途经点


class DayOrigin(unittest.TestCase):
    def test_first_segment_start(self):
        day = {"segments": [seg(BJ, ZJK), seg(ZJK, WLCB)]}
        self.assertEqual(render_html.day_origin(day)["name"], "北京")

    def test_city_day_falls_back_to_first_stop(self):
        day = {"segments": [], "stops": [P("大召寺", 111.66, 40.8), HHHT]}
        self.assertEqual(render_html.day_origin(day)["name"], "大召寺")

    def test_no_origin_without_coordinates(self):
        self.assertIsNone(render_html.day_origin({"segments": [], "stops": [{"name": "无坐标"}]}))


class RenderDayNavButton(unittest.TestCase):
    def test_button_present_with_both_platform_links(self):
        html = render_html.render_day({"day_index": 1, "segments": [seg(BJ, ZJK)]})
        self.assertIn('class="nav-btn"', html)
        self.assertIn('data-android="amapuri://', html)
        self.assertIn('data-ios="iosamap://', html)

    def test_no_button_when_nothing_to_navigate(self):
        html = render_html.render_day({"day_index": 8, "segments": [], "stops": []})
        self.assertNotIn("nav-btn", html)

    def test_fixed_start_variant_and_date_emitted(self):
        html = render_html.render_day({"day_index": 1, "date": "2026-09-25",
                                       "segments": [seg(BJ, ZJK)]})
        self.assertIn("data-android-fixed=", html)
        self.assertIn('data-date="2026-09-25"', html)

    def test_city_day_has_no_fixed_variant(self):
        # 起点就是第一个要去的点：没有「从起点出发」这回事
        html = render_html.render_day({"day_index": 4, "date": "2026-09-28", "segments": [],
                                       "stops": [P("大召寺", 111.66, 40.8)], "overnight": HHHT})
        self.assertIn('class="nav-btn"', html)
        self.assertNotIn("-fixed=", html)


if __name__ == "__main__":
    unittest.main()
