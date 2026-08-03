#!/usr/bin/env python3
"""对比两版 itinerary.json，产出"本次变更"摘要并追加进新版的 change_log。零依赖。

用法（微调前先 cp work/itinerary.json work/itinerary.prev.json）：
  python3 diff_itinerary.py --old work/itinerary.prev.json --new work/itinerary.json [--summary "换了第6晚过夜点"]

对比粒度：过夜点、途经点集合、当日里程/时长、警告数。输出人话描述，
渲染层读取 change_log 最后一条画"本次变更"块。
"""
import argparse
import json
from datetime import datetime


def load(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def day_totals(day):
    dist = sum(s.get("distance_km") or 0 for s in day.get("segments", []))
    dur = sum(s.get("duration_min") or 0 for s in day.get("segments", []))
    return dist, dur


def describe_day_change(old_day, new_day):
    changes = []
    if old_day is None:
        return ["新增的一天"]
    if new_day is None:
        return ["删除的一天"]
    ov_old = (old_day.get("overnight") or {}).get("name")
    ov_new = (new_day.get("overnight") or {}).get("name")
    if ov_old != ov_new:
        changes.append(f"过夜点 {ov_old or '（无）'} → {ov_new or '（无）'}")
    stops_old = [s.get("name") for s in old_day.get("stops", [])]
    stops_new = [s.get("name") for s in new_day.get("stops", [])]
    added = [s for s in stops_new if s not in stops_old]
    removed = [s for s in stops_old if s not in stops_new]
    if added:
        changes.append("新增途经点：" + "、".join(added))
    if removed:
        changes.append("移除途经点：" + "、".join(removed))
    (do, uo), (dn, un) = day_totals(old_day), day_totals(new_day)
    if abs(dn - do) >= 1:
        changes.append(f"当日里程 {do:.0f}→{dn:.0f}km")
    if abs(un - uo) >= 1:
        changes.append(f"当日驾驶 {uo:.0f}→{un:.0f} 分钟")
    return changes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", required=True)
    ap.add_argument("--new", required=True)
    ap.add_argument("--summary", default=None, help="一句话变更说明（缺省自动生成）")
    args = ap.parse_args()

    old, new = load(args.old), load(args.new)
    old_days = {d["day_index"]: d for d in old.get("days", [])}
    new_days = {d["day_index"]: d for d in new.get("days", [])}

    affected, day_changes = [], {}
    for di in sorted(set(old_days) | set(new_days)):
        changes = describe_day_change(old_days.get(di), new_days.get(di))
        if changes:
            affected.append(di)
            day_changes[str(di)] = changes

    def trip_totals(it):
        dist = dur = 0.0
        warn = 0
        for d in it.get("days", []):
            dd, du = day_totals(d)
            dist, dur = dist + dd, dur + du
            warn += len(d.get("warnings", []))
        return dist, dur, warn

    (od, ou, ow), (nd, nu, nw) = trip_totals(old), trip_totals(new)
    entry = {
        "at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "summary": args.summary or (f"调整了第 {'、'.join(map(str, affected))} 天" if affected else "无实质变更"),
        "affected_days": affected,
        "day_changes": day_changes,
        "distance_km_delta": round(nd - od, 1),
        "duration_min_delta": round(nu - ou, 0),
        "warnings_delta": nw - ow,
    }
    new.setdefault("change_log", []).append(entry)
    with open(args.new, "w", encoding="utf-8") as f:
        json.dump(new, f, ensure_ascii=False, indent=2)

    print(json.dumps(entry, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
