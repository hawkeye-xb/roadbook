#!/usr/bin/env python3
"""对 itinerary.json 跑合理性校验，产出 warnings/errors。零依赖。

用法：
  python3 validate_route.py --input work/itinerary.json [--request work/trip_request.json]

规则（v0，共 7 条，均为可程序化判断，不含尽调/搜索类）：
  UNVERIFIED_POI    warning  地点 source != "amap"
  MISSING_DATA      warning  该有 elevation_m 却是 null
  LONG_DRIVE        warning  单日总驾驶 > cap
  VERY_LONG_DRIVE   error    单日总驾驶 > cap * 1.25
  HIGH_ALTITUDE_SLEEP warning 过夜海拔超阈值
  RAPID_ASCENT      warning  过夜海拔较前一晚升幅 > 800m 且当前 > 2500m
  FUEL_GAP          warning/error 相邻补给点间隔超过续航比例

规则只读 itinerary.json 本身；warnings 数组由本脚本独占写入并原地保存。
"""
import argparse
import json
import sys


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def driver_multiplier(profile):
    # experienced 司机对长驾驶时长的容忍度略高，不写进 SKILL.md，只在这里生效
    return {"novice": 0.85, "normal": 1.0, "experienced": 1.15}.get(profile, 1.0)


def check_day(day, prev_overnight_elev, cap_min, alt_cap_m, range_km, energy):
    warnings = []
    errors = []

    def add(level, code, detail):
        entry = {"day": day["day_index"], "code": code, "level": level, "detail": detail}
        (errors if level == "error" else warnings).append(entry)

    # UNVERIFIED_POI
    def check_place(place, ctx):
        if not place:
            return
        if place.get("source") != "amap":
            add("warning", "UNVERIFIED_POI", f"{ctx} “{place.get('name', '?')}” 坐标未经高德核验")

    for seg in day.get("segments", []):
        check_place(seg.get("from"), "路段起点")
        check_place(seg.get("to"), "路段终点")
    for stop in day.get("stops", []):
        check_place(stop, "途经点")
    check_place(day.get("overnight"), "过夜点")

    # MISSING_DATA (elevation)
    ov = day.get("overnight") or {}
    if ov.get("lnglat") and ov.get("elevation_m") is None:
        add("warning", "MISSING_DATA", "过夜点缺海拔数据（elevation API 可能失败或未跑 enrich）")

    # LONG_DRIVE / VERY_LONG_DRIVE
    total_drive = sum(seg.get("duration_min") or 0 for seg in day.get("segments", []))
    if total_drive > 0:
        if total_drive > cap_min * 1.25:
            add("error", "VERY_LONG_DRIVE", f"单日驾驶 {total_drive:.0f} 分钟，远超上限 {cap_min:.0f} 分钟")
        elif total_drive > cap_min:
            add("warning", "LONG_DRIVE", f"单日驾驶 {total_drive:.0f} 分钟，超过上限 {cap_min:.0f} 分钟")

    # HIGH_ALTITUDE_SLEEP
    ov_elev = ov.get("elevation_m")
    if ov_elev is not None and alt_cap_m is not None and ov_elev > alt_cap_m:
        add("warning", "HIGH_ALTITUDE_SLEEP", f"过夜海拔 {ov_elev:.0f}m 超过阈值 {alt_cap_m:.0f}m")

    # RAPID_ASCENT
    if ov_elev is not None and prev_overnight_elev is not None:
        rise = ov_elev - prev_overnight_elev
        if rise > 800 and ov_elev > 2500:
            add("warning", "RAPID_ASCENT", f"过夜海拔较前一晚上升 {rise:.0f}m（{prev_overnight_elev:.0f}→{ov_elev:.0f}m）")

    # FUEL_GAP：以每段 distance_km 累计，比对相邻补给点/无补给点则整段视为一个间隔
    if range_km:
        warn_ratio, err_ratio = (0.4, 0.6) if energy == "ev" else (0.5, 0.8)
        cursor_km = 0.0
        last_refuel_km = 0.0
        for seg in day.get("segments", []):
            dist = seg.get("distance_km") or 0
            refuels = seg.get("refuel_points", []) or []
            if not refuels:
                cursor_km += dist
                continue
            for rp in sorted(refuels, key=lambda r: r.get("km_from_start", 0)):
                point_km = cursor_km + (rp.get("km_from_start") or 0)
                gap = point_km - last_refuel_km
                if gap > range_km * err_ratio:
                    add("error", "FUEL_GAP", f"补给间隔 {gap:.0f}km，超过续航 {range_km:.0f}km 的 {err_ratio*100:.0f}%")
                elif gap > range_km * warn_ratio:
                    add("warning", "FUEL_GAP", f"补给间隔 {gap:.0f}km，超过续航 {range_km:.0f}km 的 {warn_ratio*100:.0f}%")
                last_refuel_km = point_km
            cursor_km += dist
        # 末段到本日结束（若无补给点覆盖到终点）
        final_gap = cursor_km - last_refuel_km
        if final_gap > range_km * err_ratio:
            add("error", "FUEL_GAP", f"当日最后一段到收尾间隔 {final_gap:.0f}km，超过续航 {range_km:.0f}km 的 {err_ratio*100:.0f}%")
        elif final_gap > range_km * warn_ratio:
            add("warning", "FUEL_GAP", f"当日最后一段到收尾间隔 {final_gap:.0f}km，超过续航 {range_km:.0f}km 的 {warn_ratio*100:.0f}%")

    return warnings, errors


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--request", default=None, help="trip_request.json，用于读取阈值")
    args = ap.parse_args()

    itinerary = load_json(args.input)
    trip_request = load_json(args.request) if args.request else {}

    cap_min = trip_request.get("daily_drive_cap_min", 480)
    cap_min = cap_min * driver_multiplier(trip_request.get("driver_profile", "normal"))
    alt_cap_m = trip_request.get("overnight_altitude_cap_m")
    if alt_cap_m is None:
        constraints = trip_request.get("travelers", {}).get("constraints", [])
        alt_cap_m = 3000 if any("高反" in c for c in constraints) else 3500
    range_km = trip_request.get("vehicle", {}).get("range_km")
    energy = trip_request.get("vehicle", {}).get("energy", "fuel")

    all_warnings, all_errors = [], []
    prev_ov_elev = None
    for day in itinerary.get("days", []):
        w, e = check_day(day, prev_ov_elev, cap_min, alt_cap_m, range_km, energy)
        day["warnings"] = w + e  # error 和 warning 都呈现在 warnings 里，级别在 code 语义里区分
        all_warnings.extend(w)
        all_errors.extend(e)
        ov = day.get("overnight") or {}
        if ov.get("elevation_m") is not None:
            prev_ov_elev = ov["elevation_m"]

    with open(args.input, "w", encoding="utf-8") as f:
        json.dump(itinerary, f, ensure_ascii=False, indent=2)

    print(json.dumps({
        "errors": len(all_errors),
        "warnings": all_warnings + all_errors
    }, ensure_ascii=False))

    sys.exit(1 if all_errors else 0)


if __name__ == "__main__":
    main()
