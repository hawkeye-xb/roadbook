"""输出编码回归：内联 JSON 与 Leaflet HTML 不能把行程文本当成代码。"""
import html as htmllib
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import render_html  # noqa: E402


PAYLOAD = '</script><img src=x onerror=window.__roadbookXss=1>&  '


class ScriptJson(unittest.TestCase):
    def test_script_json_escapes_html_and_js_line_separators(self):
        value = {"name": PAYLOAD}
        encoded = render_html.script_json(value)
        self.assertNotIn("<", encoded)
        self.assertNotIn(">", encoded)
        self.assertNotIn("&", encoded)
        self.assertNotIn(" ", encoded)
        self.assertNotIn(" ", encoded)
        self.assertEqual(json.loads(encoded), value)


class MapHtmlEscaping(unittest.TestCase):
    def test_dynamic_tooltip_popup_and_weather_are_escaped(self):
        itinerary = {"days": [{
            "day_index": 1,
            "segments": [{
                "from": {"name": "A", "lnglat": [116.0, 40.0]},
                "to": {"name": "B", "lnglat": [117.0, 41.0]},
                "special": PAYLOAD,
                "refuel_points": [{"name": PAYLOAD, "lnglat": [116.5, 40.5]}],
            }],
            "stops": [{
                "name": PAYLOAD, "lnglat": [116.2, 40.2],
                "advisories": [PAYLOAD], "weather": PAYLOAD,
            }],
            "overnight": {"name": PAYLOAD, "lnglat": [117.0, 41.0], "weather": PAYLOAD},
        }]}
        day = render_html.collect_map_days(itinerary)[0]
        self.assertEqual(htmllib.unescape(day["segs"][0]["sp"]), PAYLOAD)
        self.assertNotIn(PAYLOAD, day["segs"][0]["sp"])
        for marker in day["markers"]:
            self.assertEqual(htmllib.unescape(marker["name"]), PAYLOAD)
            self.assertNotIn("<", marker["name"])
            if marker["t"] == "scenic":
                self.assertIn(PAYLOAD, htmllib.unescape(marker["desc"]))
            if marker.get("wx"):
                self.assertEqual(htmllib.unescape(marker["wx"]), PAYLOAD)
            for field in ("name", "desc", "wx"):
                value = marker.get(field)
                if value:
                    self.assertNotIn("</script>", value)
                    self.assertNotIn("<img", value)


if __name__ == "__main__":
    unittest.main()
