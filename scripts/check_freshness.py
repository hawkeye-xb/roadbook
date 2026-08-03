#!/usr/bin/env python3
"""claims 时效检查：超过类别时效窗口的 confirmed/user_confirmed 降级为 stale。零依赖。

用法：
  python3 check_freshness.py --input work/itinerary.json [--as-of 2026-08-03]

时效窗口与 checklists/categories.md 保持一致；改窗口先改那边再同步这里。
输出：stale 清单（day/type/claim），供只对 stale 项重新检索，不全量重跑。
verdict 为 unverified/contradicted 的不动——它们本来就待处理。
"""
import argparse
import json
import sys
from datetime import date, datetime

WINDOW_DAYS = {
    "gate_access": 365,
    "shuttle_transfer": 365,
    "reservation": 180,
    "permit": 180,
    "seasonal_road": 7,
    "phenology": 30,
    "refuel": 180,
    "altitude": 730,
    "other": 180,
}

DOWNGRADABLE = {"confirmed", "user_confirmed"}


def parse_date(s):
    return datetime.strptime(s[:10], "%Y-%m-%d").date()


def scan(claims, as_of, ctx, stale_out):
    for c in claims or []:
        if c.get("verdict") not in DOWNGRADABLE:
            continue
        checked = c.get("checked_at")
        window = WINDOW_DAYS.get(c.get("type", "other"), 180)
        expired = True if not checked else (as_of - parse_date(checked)).days > window
        if expired:
            c["prior_verdict"] = c["verdict"]
            c["verdict"] = "stale"
            stale_out.append({"where": ctx, "type": c.get("type"), "claim": c.get("claim"),
                              "checked_at": checked, "window_days": window})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--as-of", default=None, help="默认今天")
    args = ap.parse_args()

    as_of = parse_date(args.as_of) if args.as_of else date.today()
    with open(args.input, "r", encoding="utf-8") as f:
        itinerary = json.load(f)

    stale = []
    scan(itinerary.get("claims"), as_of, "trip", stale)
    for day in itinerary.get("days", []):
        scan(day.get("claims"), as_of, f"day{day.get('day_index')}", stale)

    with open(args.input, "w", encoding="utf-8") as f:
        json.dump(itinerary, f, ensure_ascii=False, indent=2)

    print(json.dumps({"as_of": str(as_of), "stale_count": len(stale), "stale": stale},
                     ensure_ascii=False, indent=2))
    sys.exit(0)


if __name__ == "__main__":
    main()
