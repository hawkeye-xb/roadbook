#!/usr/bin/env python3
"""对 itinerary.json 跑合理性校验，产出 warnings/errors。零依赖。

用法：
  python3 validate_route.py --input work/itinerary.json [--request work/trip_request.json]

规则（共 8 条，均为可程序化判断，不含尽调/搜索类）：
  UNVERIFIED_POI    warning  地点 source != "amap"
  MISSING_DATA      warning  该有 elevation_m 却是 null
  LONG_DRIVE        warning  单日总驾驶 > cap
  VERY_LONG_DRIVE   error    单日总驾驶 > cap * 1.25
  HIGH_ALTITUDE_SLEEP warning 过夜海拔超阈值
  RAPID_ASCENT      warning  过夜海拔较前一晚升幅 > 800m 且当前 > 2500m
  FUEL_GAP          warning/error 相邻补给点间隔超过续航比例
  BACKTRACK         warning  重走已走过的路段/重返已住过的过夜点（allow_backtrack=false 时生效；
                             命中不代表一定错——可能是封路绕行，必须把走法问题抛给用户决定）
  DOC_CLAIM_GAP     warning  攻略原文声称的当日里程/时长与实测差 >25%（doc 模式核心产出，
                             渲染层据此展示"原文称 X 实测 Y"对照）

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


def place_key(place):
    """地点身份：poi_id > 坐标（约 1km 粒度）> 名字。用于回头路比对。"""
    if not place:
        return None
    if place.get("poi_id"):
        return "poi:" + place["poi_id"]
    ll = place.get("lnglat")
    if ll and len(ll) == 2:
        return f"ll:{round(ll[0], 2)},{round(ll[1], 2)}"
    return "name:" + (place.get("name") or "?")


def check_backtrack(days, route_style):
    """跨天追踪：路段反向重走 + 过夜点重返。返回 [(day_index, detail)]。

    不报的正常玩法：
    - 同日原路往返（基地放射式一日游：住 A 去景点再回 A）
    - 连续天同一过夜点（连住）
    - loop 末日回出发地 / out_and_back 的整体返程
    """
    hits = []
    seen_edges = {}      # (from_key, to_key) -> day_index 首次出现
    seen_overnights = {} # place_key -> day_index 首次过夜
    last_day_index = days[-1]["day_index"] if days else None
    prev_overnight_key = None
    for day in days:
        di = day["day_index"]
        for seg in day.get("segments", []):
            fk, tk = place_key(seg.get("from")), place_key(seg.get("to"))
            if not fk or not tk or fk == tk:
                continue
            if (tk, fk) in seen_edges:
                first = seen_edges[(tk, fk)]
                # 同日反向重走 = 当日往返，正常；out_and_back 模式返程也是预期
                if first != di and route_style != "out_and_back":
                    hits.append((di, f"“{seg.get('from', {}).get('name', '?')}→{seg.get('to', {}).get('name', '?')}”反向重走了第 {first} 天的路段，需与用户确认走法（可能是封路绕行或应改环线）"))
            seen_edges.setdefault((fk, tk), di)
        ok = place_key(day.get("overnight"))
        if ok:
            if ok == prev_overnight_key:
                pass  # 连住，正常
            elif ok in seen_overnights:
                first = seen_overnights[ok]
                # 末日回到出发地过夜是环线收尾，不报
                if not (route_style == "loop" and di == last_day_index):
                    hits.append((di, f"过夜点“{day.get('overnight', {}).get('name', '?')}”与第 {first} 天重复，中间去了别处又折返，需与用户确认"))
            seen_overnights.setdefault(ok, di)
            prev_overnight_key = ok
    return hits


def check_day(day, prev_overnight_elev, cap_min, alt_cap_m, range_km, energy, alt_relevant=True):
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

    # MISSING_DATA (elevation)——低地行程（altitude_relevant=false）海拔本就不采集，不算缺数据
    ov = day.get("overnight") or {}
    if alt_relevant and ov.get("lnglat") and ov.get("elevation_m") is None:
        add("warning", "MISSING_DATA", "过夜点缺海拔数据（elevation API 可能失败或未跑 enrich）")

    # DOC_CLAIM_GAP：攻略原文数字 vs 实测（只在段有 claimed 且实测已回填时比）
    cl_km = sum((s.get("claimed") or {}).get("distance_km") or 0 for s in day.get("segments", []))
    cl_min = sum((s.get("claimed") or {}).get("duration_min") or 0 for s in day.get("segments", []))
    ms_km = sum(s.get("distance_km") or 0 for s in day.get("segments", []) if s.get("claimed"))
    ms_min = sum(s.get("duration_min") or 0 for s in day.get("segments", []) if s.get("claimed"))
    if cl_km and ms_km and abs(ms_km - cl_km) / cl_km > 0.25:
        add("warning", "DOC_CLAIM_GAP", f"原文称约 {cl_km:.0f}km，实测 {ms_km:.0f}km（差 {abs(ms_km-cl_km)/cl_km*100:.0f}%）")
    elif cl_min and ms_min and abs(ms_min - cl_min) / cl_min > 0.25:
        add("warning", "DOC_CLAIM_GAP", f"原文称约 {cl_min/60:.1f}h，实测 {ms_min/60:.1f}h（差 {abs(ms_min-cl_min)/cl_min*100:.0f}%）")

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
    alt_relevant = trip_request.get("altitude_relevant", True)

    all_warnings, all_errors = [], []
    prev_ov_elev = None
    for day in itinerary.get("days", []):
        w, e = check_day(day, prev_ov_elev, cap_min, alt_cap_m, range_km, energy, alt_relevant)
        day["warnings"] = w + e  # error 和 warning 都呈现在 warnings 里，级别在 code 语义里区分
        all_warnings.extend(w)
        all_errors.extend(e)
        ov = day.get("overnight") or {}
        if ov.get("elevation_m") is not None:
            prev_ov_elev = ov["elevation_m"]

    # 预览模式：顶部横幅已全局声明"全部未核验"，逐地点的 UNVERIFIED_POI 聚合为每天一条
    if itinerary.get("meta", {}).get("preview_mode"):
        for day in itinerary.get("days", []):
            unv = [w for w in day.get("warnings", []) if w["code"] == "UNVERIFIED_POI"]
            if len(unv) > 1:
                day["warnings"] = [w for w in day["warnings"] if w["code"] != "UNVERIFIED_POI"]
                day["warnings"].append({"day": day["day_index"], "code": "UNVERIFIED_POI", "level": "warning",
                                        "detail": f"本日 {len(unv)} 个地点坐标未经核验（预览模式，配置高德后自动消除）"})
        agg = {w["day"] for w in all_warnings if w["code"] == "UNVERIFIED_POI"}
        all_warnings = [w for w in all_warnings if w["code"] != "UNVERIFIED_POI"]
        for day in itinerary.get("days", []):
            if day["day_index"] in agg:
                all_warnings.extend(w for w in day["warnings"] if w["code"] == "UNVERIFIED_POI")

    # BACKTRACK：跨天规则，在逐日检查之后统一跑
    if not trip_request.get("allow_backtrack", False):
        route_style = trip_request.get("route_style", "loop")
        days = itinerary.get("days", [])
        by_index = {d["day_index"]: d for d in days}
        for di, detail in check_backtrack(days, route_style):
            entry = {"day": di, "code": "BACKTRACK", "level": "warning", "detail": detail}
            by_index[di].setdefault("warnings", []).append(entry)
            all_warnings.append(entry)

    with open(args.input, "w", encoding="utf-8") as f:
        json.dump(itinerary, f, ensure_ascii=False, indent=2)

    print(json.dumps({
        "errors": len(all_errors),
        "warnings": all_warnings + all_errors
    }, ensure_ascii=False))

    sys.exit(1 if all_errors else 0)


if __name__ == "__main__":
    main()
