#!/usr/bin/env python3
"""从 itinerary.json 求和统计，模型不做口算。零依赖。

用法：
  python3 summarize.py --input work/itinerary.json
"""
import argparse
import json


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    args = ap.parse_args()

    with open(args.input, "r", encoding="utf-8") as f:
        itinerary = json.load(f)

    total_km = 0.0
    total_min = 0.0
    max_elev = None
    warning_count = 0

    for day in itinerary.get("days", []):
        for seg in day.get("segments", []):
            total_km += seg.get("distance_km") or 0
            total_min += seg.get("duration_min") or 0
        ov = day.get("overnight") or {}
        if ov.get("elevation_m") is not None:
            max_elev = ov["elevation_m"] if max_elev is None else max(max_elev, ov["elevation_m"])
        for stop in day.get("stops", []):
            if stop.get("elevation_m") is not None:
                max_elev = stop["elevation_m"] if max_elev is None else max(max_elev, stop["elevation_m"])
        warning_count += len(day.get("warnings", []))

    print(json.dumps({
        "total_distance_km": round(total_km, 1),
        "total_drive_min": round(total_min),
        "total_drive_hours": round(total_min / 60, 1),
        "max_elevation_m": max_elev,
        "warning_count": warning_count,
        "has_plan_b": bool(itinerary.get("plan_b")),
        "degraded": itinerary.get("meta", {}).get("degraded", [])
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
