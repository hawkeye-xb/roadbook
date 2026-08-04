#!/usr/bin/env python3
"""为 itinerary.json 的每个路段取真实道路几何（高德驾车 API polyline），抽稀后写入
segments[].geometry。零依赖（urllib）。

用法：
  python3 fetch_geometry.py --input work/itinerary.json [--key KEY] [--force]

设计约束：
- 几何数据量大（每段几百上千个坐标点），**绝不经过模型上下文**——本脚本直接
  REST 取数、抽稀、落盘，模型只看本脚本 stdout 的统计摘要。
- key 解析顺序：--key > 环境变量 AMAP_MAPS_API_KEY > ~/.claude.json 里
  amap-maps MCP 配置的 env。
- 已有 geometry 的段跳过（--force 重取）。
- 顺带用 API 返回校正该段 distance_km/duration_min（与 MCP 取数同源，正常无差异；
  有差异说明之前的数是估的）。
- geometry 存 [lng, lat] 数组（与 schema 的 lnglat 字段顺序一致；渲染层负责翻转给 Leaflet）。
"""
import argparse
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request

API = "https://restapi.amap.com/v3/direction/driving"


def resolve_key(cli_key):
    if cli_key:
        return cli_key
    if os.environ.get("AMAP_MAPS_API_KEY"):
        return os.environ["AMAP_MAPS_API_KEY"]
    cfg_path = os.path.expanduser("~/.claude.json")
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg.get("mcpServers", {}).get("amap-maps", {}).get("env", {}).get("AMAP_MAPS_API_KEY")
    except Exception:
        return None


def perpendicular_dist(pt, a, b):
    if a == b:
        return math.hypot(pt[0] - a[0], pt[1] - a[1])
    t = ((pt[0] - a[0]) * (b[0] - a[0]) + (pt[1] - a[1]) * (b[1] - a[1])) / \
        ((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2)
    t = max(0.0, min(1.0, t))
    px, py = a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])
    return math.hypot(pt[0] - px, pt[1] - py)


def rdp(points, epsilon):
    """Douglas-Peucker 抽稀。epsilon 单位是度（0.0004 ≈ 40m）。"""
    if len(points) < 3:
        return points
    dmax, idx = 0.0, 0
    for i in range(1, len(points) - 1):
        d = perpendicular_dist(points[i], points[0], points[-1])
        if d > dmax:
            dmax, idx = d, i
    if dmax > epsilon:
        left = rdp(points[:idx + 1], epsilon)
        right = rdp(points[idx:], epsilon)
        return left[:-1] + right
    return [points[0], points[-1]]


def fetch_route(key, origin, dest, retries=4):
    params = urllib.parse.urlencode({
        "key": key,
        "origin": f"{origin[0]:.6f},{origin[1]:.6f}",
        "destination": f"{dest[0]:.6f},{dest[1]:.6f}",
        "extensions": "all",
        "strategy": "0",
    })
    data = None
    for attempt in range(retries):
        with urllib.request.urlopen(f"{API}?{params}", timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        # 免费版 QPS 限制：退避重试
        if data.get("info", "").startswith("CUQPS"):
            time.sleep(1.5 * (attempt + 1))
            continue
        break
    if data.get("status") != "1" or not data.get("route", {}).get("paths"):
        return None, data.get("info", "unknown error")
    path = data["route"]["paths"][0]
    pts = []
    for step in path.get("steps", []):
        for pair in step.get("polyline", "").split(";"):
            if "," in pair:
                lng, lat = pair.split(",")
                pts.append([float(lng), float(lat)])
    return {
        "points": pts,
        "distance_km": float(path["distance"]) / 1000.0,
        "duration_min": float(path["duration"]) / 60.0,
    }, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--key", default=None)
    ap.add_argument("--force", action="store_true", help="已有 geometry 的段也重取")
    ap.add_argument("--epsilon", type=float, default=0.0004, help="抽稀容差（度），默认约 40m")
    args = ap.parse_args()

    key = resolve_key(args.key)
    if not key:
        print(json.dumps({"error": "找不到高德 key（--key / AMAP_MAPS_API_KEY / ~/.claude.json 均无）"},
                         ensure_ascii=False))
        sys.exit(2)

    with open(args.input, "r", encoding="utf-8") as f:
        itinerary = json.load(f)

    done = skipped = failed = 0
    raw_pts_total = kept_pts_total = 0
    failures = []
    for day in itinerary.get("days", []):
        for seg in day.get("segments", []):
            if seg.get("geometry") and not args.force:
                skipped += 1
                continue
            o = (seg.get("from") or {}).get("lnglat")
            d = (seg.get("to") or {}).get("lnglat")
            if not o or not d:
                skipped += 1
                continue
            route, err = fetch_route(key, o, d)
            if err:
                failed += 1
                failures.append({"day": day.get("day_index"),
                                 "seg": f'{seg.get("from", {}).get("name")}→{seg.get("to", {}).get("name")}',
                                 "error": err})
                continue
            simplified = rdp(route["points"], args.epsilon)
            seg["geometry"] = [[round(p[0], 5), round(p[1], 5)] for p in simplified]
            seg["distance_km"] = round(route["distance_km"], 1)
            seg["duration_min"] = round(route["duration_min"])
            raw_pts_total += len(route["points"])
            kept_pts_total += len(simplified)
            done += 1
            time.sleep(0.4)  # 个人版驾车规划约 3 QPS，保守放慢

    with open(args.input, "w", encoding="utf-8") as f:
        json.dump(itinerary, f, ensure_ascii=False, indent=2)

    print(json.dumps({
        "fetched": done, "skipped": skipped, "failed": failed,
        "points_raw": raw_pts_total, "points_kept": kept_pts_total,
        "failures": failures,
    }, ensure_ascii=False, indent=2))
    sys.exit(1 if failed and not done else 0)


if __name__ == "__main__":
    main()
