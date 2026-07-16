#!/usr/bin/env python3
"""批量给 itinerary.json 里的 stops/overnight 写回海拔。零依赖，仅用标准库。

用法：
  python3 enrich.py --input work/itinerary.json

行为：
  收集所有带 lnglat 且 elevation_m 缺失的 place 对象，
  批量调用 Open-Elevation（免费，无需 key）填回 elevation_m。
  原地修改 --input 指向的文件。stdout 只输出一行回执 JSON。
"""
import argparse
import json
import sys
import urllib.request
import urllib.error

ELEVATION_API = "https://api.open-elevation.com/api/v1/lookup"
BATCH_SIZE = 50


def collect_targets(itinerary):
    """返回 [(place_dict, lat, lng), ...]，用于回写 elevation_m。"""
    targets = []

    def visit(place):
        if not isinstance(place, dict):
            return
        lnglat = place.get("lnglat")
        if lnglat and len(lnglat) == 2 and place.get("elevation_m") is None:
            lng, lat = lnglat
            targets.append((place, lat, lng))

    for day in itinerary.get("days", []):
        for stop in day.get("stops", []):
            visit(stop)
        visit(day.get("overnight"))
    return targets


def fetch_elevations(points):
    """points: [(lat, lng), ...] -> [elevation or None, ...]（保序）"""
    results = [None] * len(points)
    for start in range(0, len(points), BATCH_SIZE):
        chunk = points[start:start + BATCH_SIZE]
        payload = json.dumps({
            "locations": [{"latitude": lat, "longitude": lng} for lat, lng in chunk]
        }).encode("utf-8")
        req = urllib.request.Request(
            ELEVATION_API, data=payload,
            headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            for i, item in enumerate(data.get("results", [])):
                results[start + i] = item.get("elevation")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError):
            # 该批次整体失败，保留 None，交由 validate_route.py 产 MISSING_DATA
            continue
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    args = ap.parse_args()

    with open(args.input, "r", encoding="utf-8") as f:
        itinerary = json.load(f)

    targets = collect_targets(itinerary)
    if not targets:
        print(json.dumps({"enriched": 0, "failed": 0, "skipped_no_coord": 0}))
        return

    elevations = fetch_elevations([(lat, lng) for _, lat, lng in targets])

    enriched, failed = 0, 0
    for (place, _, _), elev in zip(targets, elevations):
        if elev is not None:
            place["elevation_m"] = round(elev, 1)
            enriched += 1
        else:
            failed += 1

    if "meta" in itinerary and failed and "elevation" not in itinerary["meta"].get("degraded", []):
        itinerary["meta"].setdefault("degraded", [])
        if failed == len(targets):
            itinerary["meta"]["degraded"].append("elevation")

    with open(args.input, "w", encoding="utf-8") as f:
        json.dump(itinerary, f, ensure_ascii=False, indent=2)

    print(json.dumps({"enriched": enriched, "failed": failed, "skipped_no_coord": 0}))


if __name__ == "__main__":
    sys.exit(main())
