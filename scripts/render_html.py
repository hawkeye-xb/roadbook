#!/usr/bin/env python3
"""把 itinerary.json 渲染成独立 HTML 文件。零依赖（不用 jinja2）。

用法：
  python3 render_html.py --input work/itinerary.json --out outputs/trip.html
"""
import argparse
import html
import json
import subprocess
import sys
from pathlib import Path

WARNING_LABELS = {
    "UNVERIFIED_POI": "坐标未核验",
    "MISSING_DATA": "数据缺失",
    "LONG_DRIVE": "驾驶偏长",
    "VERY_LONG_DRIVE": "驾驶过长",
    "HIGH_ALTITUDE_SLEEP": "过夜海拔偏高",
    "RAPID_ASCENT": "海拔爬升过快",
    "FUEL_GAP": "补给间隔风险",
}


def esc(s):
    return html.escape(str(s)) if s is not None else ""


def place_line(place, label):
    if not place:
        return ""
    src = place.get("source", "unverified")
    src_badge = "" if src == "amap" else '<span class="badge unverified">未核验</span>'
    elev = place.get("elevation_m")
    elev_txt = f' · 海拔 {elev:.0f}m' if elev is not None else ""
    return f'<div class="place"><span class="place-label">{esc(label)}</span> {esc(place.get("name",""))}{src_badge}{elev_txt}</div>'


def render_day(day):
    segs_html = ""
    for seg in day.get("segments", []):
        dist = seg.get("distance_km")
        dur = seg.get("duration_min")
        dist_txt = f'{dist:.0f} km' if dist is not None else "?"
        dur_txt = f'{dur/60:.1f} 小时' if dur is not None else "?"
        segs_html += f'''
        <div class="segment">
          {place_line(seg.get("from"), "起")}
          <div class="segment-meta">→ {dist_txt} · {dur_txt}</div>
          {place_line(seg.get("to"), "终")}
        </div>'''

    stops_html = ""
    for stop in day.get("stops", []):
        visit = stop.get("visit_min")
        visit_txt = f'（游玩约 {visit} 分钟）' if visit else ""
        stops_html += f'<li>{place_line(stop, "途经")}{visit_txt}</li>'

    warn_html = ""
    for w in day.get("warnings", []):
        code = w.get("code", "")
        level = w.get("level", "warning")
        label = WARNING_LABELS.get(code, code)
        warn_html += f'<li class="warn-item {level}"><span class="warn-code">{esc(label)}</span>{esc(w.get("detail",""))}</li>'

    ov = day.get("overnight")
    ov_html = place_line(ov, "过夜") if ov else ""

    return f'''
    <section class="day-card">
      <header class="day-head">
        <span class="day-idx">DAY {day.get("day_index","?")}</span>
        <span class="day-date">{esc(day.get("date",""))}</span>
      </header>
      {segs_html}
      {'<ul class="stops">' + stops_html + '</ul>' if stops_html else ''}
      {ov_html}
      {'<ul class="warnings">' + warn_html + '</ul>' if warn_html else ''}
    </section>'''


TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{
    --bg:#f4f5f1; --surface:#ffffff; --ink:#1c2420; --ink-dim:#57614e;
    --line:#dfe2d8; --accent:#2f6f63; --warn:#a5702a; --warn-bg:#f5ead4;
    --err:#a5432e; --err-bg:#f6ddd4; --unverified-bg:#eee3c8; --unverified-ink:#7a5b18;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg:#14171a; --surface:#1b1f1e; --ink:#e6e9e2; --ink-dim:#a7b0a2;
      --line:#2c322c; --accent:#64a894; --warn:#d9a15d; --warn-bg:#33291a;
      --err:#d97a63; --err-bg:#332019; --unverified-bg:#3a331c; --unverified-ink:#e0c37a;
    }}
  }}
  * {{ box-sizing:border-box; }}
  body {{
    margin:0; background:var(--bg); color:var(--ink);
    font-family:-apple-system,"PingFang SC",system-ui,sans-serif;
    font-variant-numeric:tabular-nums;
  }}
  .wrap {{ max-width:760px; margin:0 auto; padding:2.5rem 1.2rem 4rem; }}
  h1 {{ font-size:1.5rem; margin:0 0 0.3rem; }}
  .summary {{
    display:flex; flex-wrap:wrap; gap:0.5rem 1.5rem; margin:1rem 0 2rem;
    padding:1rem 1.2rem; background:var(--surface); border:1px solid var(--line); border-radius:8px;
    font-size:0.9rem; color:var(--ink-dim);
  }}
  .summary b {{ color:var(--ink); }}
  .day-card {{
    background:var(--surface); border:1px solid var(--line); border-radius:8px;
    padding:1.1rem 1.3rem; margin-bottom:1rem;
  }}
  .day-head {{ display:flex; align-items:baseline; gap:0.8em; margin-bottom:0.6rem; }}
  .day-idx {{ font-weight:700; color:var(--accent); }}
  .day-date {{ color:var(--ink-dim); font-size:0.85rem; }}
  .segment {{ margin:0.5rem 0; font-size:0.92rem; }}
  .segment-meta {{ color:var(--ink-dim); font-size:0.85rem; margin:0.15rem 0 0.15rem 0.2rem; }}
  .place {{ display:flex; gap:0.5em; align-items:center; }}
  .place-label {{ color:var(--ink-dim); font-size:0.78rem; min-width:2.4em; }}
  .badge.unverified {{
    font-size:0.68rem; background:var(--unverified-bg); color:var(--unverified-ink);
    padding:0.05em 0.5em; border-radius:3px;
  }}
  ul.stops, ul.warnings {{ margin:0.6rem 0 0; padding-left:1.1rem; font-size:0.88rem; }}
  ul.stops li {{ margin:0.3rem 0; }}
  .warn-item {{ list-style:none; margin:0.3rem 0 0.3rem -1.1rem; padding:0.35rem 0.6rem; border-radius:5px; }}
  .warn-item.warning {{ background:var(--warn-bg); color:var(--warn); }}
  .warn-item.error {{ background:var(--err-bg); color:var(--err); }}
  .warn-code {{ font-weight:700; margin-right:0.5em; }}
  .disclaimer {{
    font-size:0.8rem; color:var(--ink-dim); background:var(--unverified-bg);
    padding:0.8rem 1rem; border-radius:6px; margin-bottom:1.5rem;
  }}
</style>
</head>
<body>
<div class="wrap">
  <h1>{title}</h1>
  {disclaimer}
  <div class="summary">
    <span>总里程 <b>{total_km} km</b></span>
    <span>总驾驶 <b>{total_hours} 小时</b></span>
    <span>最高海拔 <b>{max_elev}</b></span>
    <span>警告 <b>{warning_count} 条</b></span>
    <span>Plan B <b>{has_plan_b}</b></span>
  </div>
  {days_html}
</div>
</body>
</html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    with open(args.input, "r", encoding="utf-8") as f:
        itinerary = json.load(f)

    script_dir = Path(__file__).parent
    summary_raw = subprocess.run(
        [sys.executable, str(script_dir / "summarize.py"), "--input", args.input],
        capture_output=True, text=True, check=True
    ).stdout
    summary = json.loads(summary_raw)

    degraded = itinerary.get("meta", {}).get("degraded", [])
    disclaimer = ""
    if degraded:
        disclaimer = (
            f'<div class="disclaimer">⚠ 部分数据未核验（缺失依赖：{esc(", ".join(degraded))}），'
            f'出行前请自行确认坐标、路况与海拔信息。</div>'
        )

    days_html = "".join(render_day(d) for d in itinerary.get("days", []))

    out_html = TEMPLATE.format(
        title=esc(itinerary.get("meta", {}).get("title", "自驾行程")),
        disclaimer=disclaimer,
        total_km=summary["total_distance_km"],
        total_hours=summary["total_drive_hours"],
        max_elev=(f'{summary["max_elevation_m"]:.0f}m' if summary["max_elevation_m"] is not None else "未知"),
        warning_count=summary["warning_count"],
        has_plan_b="有" if summary["has_plan_b"] else "无",
        days_html=days_html,
    )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(out_html, encoding="utf-8")
    print(json.dumps({"output": str(out_path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
