#!/usr/bin/env python3
"""把 itinerary.json 渲染成独立 HTML 文件。零依赖（不用 jinja2）。

用法：
  python3 render_html.py --input work/itinerary.json --out outputs/trip.html

渲染内容：Leaflet 地图（有坐标才画；CDN 加载失败自动隐藏，卡片不受影响）、
逐日卡片（分辨率徽章 骨架/已填充/已核验）、claims 核验标注（✅❓❌⏳）、
预览模式横幅（meta.preview_mode）、本次变更块（change_log 最后一条）。
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
    "BACKTRACK": "回头路待确认",
}

FIDELITY_LABELS = {
    "skeleton": ("骨架", "fid-skeleton"),
    "filled": ("已填充", "fid-filled"),
    "verified": ("已核验", "fid-verified"),
}

VERDICT_ICONS = {
    "confirmed": ("✅", "已核实"),
    "user_confirmed": ("✅", "你已确认"),
    "unverified": ("❓", "待核实"),
    "contradicted": ("❌", "与近期来源冲突"),
    "stale": ("⏳", "信息可能过期，建议复检"),
}

DAY_COLORS = ["#2f6f63", "#a5702a", "#4a6fa5", "#a5432e", "#6b5b95", "#3d8361",
              "#b3541e", "#39648c", "#8c4f66", "#5c7a29", "#7a5b8c", "#2a7f8c"]


def esc(s):
    return html.escape(str(s)) if s is not None else ""


def place_line(place, label):
    if not place:
        return ""
    src = place.get("source", "unverified")
    src_badge = "" if src == "amap" else f'<span class="badge unverified">{"攻略原文" if src == "doc_claim" else "未核验"}</span>'
    pin_badge = '<span class="badge pinned">已钉</span>' if place.get("pinned") else ""
    elev = place.get("elevation_m")
    elev_txt = f' · 海拔 {elev:.0f}m' if elev is not None else ""
    return (f'<div class="place"><span class="place-label">{esc(label)}</span> '
            f'{esc(place.get("name", ""))}{src_badge}{pin_badge}{elev_txt}</div>')


def render_claims(claims, heading=None):
    if not claims:
        return ""
    items = ""
    for c in claims:
        icon, verdict_txt = VERDICT_ICONS.get(c.get("verdict", "unverified"), ("❓", "待核实"))
        ev_html = ""
        for ev in c.get("evidence", []) or []:
            meta_bits = [b for b in [ev.get("source"), ev.get("date")] if b]
            quote = ev.get("quote")
            ev_html += (f'<div class="evidence">来源：{esc(" · ".join(meta_bits)) or "未知"}'
                        + (f'<blockquote>{esc(quote)}</blockquote>' if quote else "") + "</div>")
        checked = f'<span class="claim-date">核验于 {esc(c["checked_at"])}</span>' if c.get("checked_at") else ""
        detail = f'<details><summary>证据</summary>{ev_html}</details>' if ev_html else ""
        items += (f'<li class="claim-item v-{esc(c.get("verdict", "unverified"))}">'
                  f'<span class="claim-icon">{icon}</span>'
                  f'<span class="claim-text">{esc(c.get("claim", ""))}</span>'
                  f'<span class="claim-verdict">{verdict_txt}</span>{checked}{detail}</li>')
    head = f'<div class="claims-head">{esc(heading)}</div>' if heading else ""
    return f'<div class="claims">{head}<ul>{items}</ul></div>'


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
        advisories = "".join(f'<span class="advisory">{esc(a)}</span>' for a in stop.get("advisories", []) or [])
        stops_html += f'<li>{place_line(stop, "途经")}{visit_txt}{advisories}</li>'

    warn_html = ""
    for w in day.get("warnings", []):
        code = w.get("code", "")
        level = w.get("level", "warning")
        label = WARNING_LABELS.get(code, code)
        warn_html += f'<li class="warn-item {level}"><span class="warn-code">{esc(label)}</span>{esc(w.get("detail", ""))}</li>'

    ov = day.get("overnight")
    ov_html = place_line(ov, "过夜") if ov else ""

    fid_label, fid_cls = FIDELITY_LABELS.get(day.get("fidelity", "skeleton"), FIDELITY_LABELS["skeleton"])

    return f'''
    <section class="day-card" id="day-{day.get("day_index", "?")}">
      <header class="day-head">
        <span class="day-idx">DAY {day.get("day_index", "?")}</span>
        <span class="day-date">{esc(day.get("date", ""))}</span>
        <span class="badge fidelity {fid_cls}">{fid_label}</span>
      </header>
      {segs_html}
      {'<ul class="stops">' + stops_html + '</ul>' if stops_html else ''}
      {ov_html}
      {render_claims(day.get("claims"))}
      {'<ul class="warnings">' + warn_html + '</ul>' if warn_html else ''}
    </section>'''


def render_change_block(itinerary):
    log = itinerary.get("change_log") or []
    if not log:
        return ""
    ch = log[-1]
    lines = ""
    for di, changes in (ch.get("day_changes") or {}).items():
        lines += f'<li><b>Day {esc(di)}</b>：{esc("；".join(changes))}</li>'
    deltas = []
    if ch.get("distance_km_delta"):
        deltas.append(f'里程 {"+" if ch["distance_km_delta"] > 0 else ""}{ch["distance_km_delta"]:.0f} km')
    if ch.get("duration_min_delta"):
        deltas.append(f'驾驶 {"+" if ch["duration_min_delta"] > 0 else ""}{ch["duration_min_delta"]:.0f} 分钟')
    if ch.get("warnings_delta"):
        deltas.append(f'警告 {"+" if ch["warnings_delta"] > 0 else ""}{ch["warnings_delta"]} 条')
    delta_txt = f'<div class="change-deltas">{esc(" · ".join(deltas))}</div>' if deltas else ""
    return f'''
    <div class="change-block">
      <div class="change-head">本次变更（{esc(ch.get("at", ""))}）：{esc(ch.get("summary", ""))}</div>
      {'<ul>' + lines + '</ul>' if lines else ''}
      {delta_txt}
    </div>'''


def collect_map_days(itinerary):
    """每天的坐标点序列（[lat,lng]，Leaflet 顺序），无坐标的天跳过。"""
    map_days = []
    for i, day in enumerate(itinerary.get("days", [])):
        points, seen = [], set()

        def push(place):
            ll = (place or {}).get("lnglat")
            if ll and len(ll) == 2:
                key = (round(ll[1], 5), round(ll[0], 5))
                if key not in seen:
                    seen.add(key)
                    points.append([ll[1], ll[0]])

        for seg in day.get("segments", []):
            push(seg.get("from"))
            push(seg.get("to"))
        for stop in day.get("stops", []):
            push(stop)
        push(day.get("overnight"))
        if not points:
            continue
        ov = day.get("overnight") or {}
        ov_ll = ov.get("lnglat")
        map_days.append({
            "idx": day.get("day_index"),
            "color": DAY_COLORS[i % len(DAY_COLORS)],
            "points": points,
            "overnight": {"name": ov.get("name", ""), "latlng": [ov_ll[1], ov_ll[0]]} if ov_ll else None,
        })
    return map_days


TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.css"
      onerror="document.documentElement.classList.add('no-map')">
<style>
  :root {{
    --bg:#f4f5f1; --surface:#ffffff; --ink:#1c2420; --ink-dim:#57614e;
    --line:#dfe2d8; --accent:#2f6f63; --warn:#a5702a; --warn-bg:#f5ead4;
    --err:#a5432e; --err-bg:#f6ddd4; --unverified-bg:#eee3c8; --unverified-ink:#7a5b18;
    --pin-bg:#dcebe6; --pin-ink:#2f6f63;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg:#14171a; --surface:#1b1f1e; --ink:#e6e9e2; --ink-dim:#a7b0a2;
      --line:#2c322c; --accent:#64a894; --warn:#d9a15d; --warn-bg:#33291a;
      --err:#d97a63; --err-bg:#332019; --unverified-bg:#3a331c; --unverified-ink:#e0c37a;
      --pin-bg:#1e3129; --pin-ink:#64a894;
    }}
  }}
  * {{ box-sizing:border-box; }}
  body {{
    margin:0; background:var(--bg); color:var(--ink);
    font-family:-apple-system,"PingFang SC",system-ui,sans-serif;
    font-variant-numeric:tabular-nums;
  }}
  .wrap {{ max-width:760px; margin:0 auto; padding:1.2rem 1.2rem 4rem; }}
  h1 {{ font-size:1.5rem; margin:0.8rem 0 0.3rem; }}
  .preview-banner {{
    position:sticky; top:0; z-index:2000; background:var(--err-bg); color:var(--err);
    padding:0.7rem 1rem; font-size:0.88rem; font-weight:600; border-bottom:1px solid var(--err);
  }}
  #map {{ height:380px; border-radius:8px; border:1px solid var(--line); margin:1rem 0; }}
  .no-map #map, #map:empty {{ display:none; }}
  .summary {{
    display:flex; flex-wrap:wrap; gap:0.5rem 1.5rem; margin:1rem 0 1.2rem;
    padding:1rem 1.2rem; background:var(--surface); border:1px solid var(--line); border-radius:8px;
    font-size:0.9rem; color:var(--ink-dim);
  }}
  .summary b {{ color:var(--ink); }}
  .change-block {{
    background:var(--surface); border:1px solid var(--accent); border-radius:8px;
    padding:0.9rem 1.2rem; margin-bottom:1.2rem; font-size:0.88rem;
  }}
  .change-head {{ font-weight:700; color:var(--accent); }}
  .change-block ul {{ margin:0.5rem 0 0; padding-left:1.2rem; }}
  .change-deltas {{ margin-top:0.4rem; color:var(--ink-dim); }}
  .day-card {{
    background:var(--surface); border:1px solid var(--line); border-radius:8px;
    padding:1.1rem 1.3rem; margin-bottom:1rem;
  }}
  .day-head {{ display:flex; align-items:baseline; gap:0.8em; margin-bottom:0.6rem; }}
  .day-idx {{ font-weight:700; color:var(--accent); }}
  .day-date {{ color:var(--ink-dim); font-size:0.85rem; }}
  .segment {{ margin:0.5rem 0; font-size:0.92rem; }}
  .segment-meta {{ color:var(--ink-dim); font-size:0.85rem; margin:0.15rem 0 0.15rem 0.2rem; }}
  .place {{ display:flex; gap:0.5em; align-items:center; flex-wrap:wrap; }}
  .place-label {{ color:var(--ink-dim); font-size:0.78rem; min-width:2.4em; }}
  .badge {{ font-size:0.68rem; padding:0.05em 0.5em; border-radius:3px; }}
  .badge.unverified {{ background:var(--unverified-bg); color:var(--unverified-ink); }}
  .badge.pinned {{ background:var(--pin-bg); color:var(--pin-ink); }}
  .badge.fidelity {{ margin-left:auto; font-weight:600; }}
  .fid-skeleton {{ background:var(--unverified-bg); color:var(--unverified-ink); }}
  .fid-filled {{ background:var(--pin-bg); color:var(--pin-ink); }}
  .fid-verified {{ background:var(--accent); color:var(--surface); }}
  ul.stops, ul.warnings {{ margin:0.6rem 0 0; padding-left:1.1rem; font-size:0.88rem; }}
  ul.stops li {{ margin:0.3rem 0; }}
  .advisory {{ font-size:0.78rem; color:var(--ink-dim); margin-left:0.5em; }}
  .warn-item {{ list-style:none; margin:0.3rem 0 0.3rem -1.1rem; padding:0.35rem 0.6rem; border-radius:5px; }}
  .warn-item.warning {{ background:var(--warn-bg); color:var(--warn); }}
  .warn-item.error {{ background:var(--err-bg); color:var(--err); }}
  .warn-code {{ font-weight:700; margin-right:0.5em; }}
  .claims {{ margin-top:0.7rem; font-size:0.87rem; }}
  .claims-head {{ font-weight:700; margin-bottom:0.3rem; }}
  .claims ul {{ margin:0; padding-left:0; }}
  .claim-item {{ list-style:none; padding:0.35rem 0.5rem; margin:0.25rem 0; border-radius:5px; background:var(--bg); }}
  .claim-icon {{ margin-right:0.4em; }}
  .claim-verdict {{ color:var(--ink-dim); font-size:0.8rem; margin-left:0.6em; }}
  .claim-date {{ color:var(--ink-dim); font-size:0.75rem; margin-left:0.6em; }}
  .claim-item.v-contradicted {{ background:var(--err-bg); }}
  .claim-item details {{ margin-top:0.3rem; font-size:0.82rem; color:var(--ink-dim); }}
  .evidence blockquote {{ margin:0.2rem 0 0.4rem 0.8rem; padding-left:0.6rem; border-left:2px solid var(--line); }}
  .disclaimer {{
    font-size:0.8rem; color:var(--ink-dim); background:var(--unverified-bg);
    padding:0.8rem 1rem; border-radius:6px; margin-bottom:1.5rem;
  }}
</style>
</head>
<body>
{preview_banner}
<div class="wrap">
  <h1>{title}</h1>
  {disclaimer}
  <div id="map"></div>
  <div class="summary">
    <span>总里程 <b>{total_km} km</b></span>
    <span>总驾驶 <b>{total_hours} 小时</b></span>
    <span>最高海拔 <b>{max_elev}</b></span>
    <span>警告 <b>{warning_count} 条</b></span>
    <span>Plan B <b>{has_plan_b}</b></span>
  </div>
  {change_block}
  {trip_claims}
  {days_html}
</div>
<script src="https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.min.js"
        onerror="document.documentElement.classList.add('no-map')"></script>
<script>
(function () {{
  var DAYS = {map_days_json};
  if (!window.L || !DAYS.length) {{
    document.documentElement.classList.add('no-map');
    return;
  }}
  var map = L.map('map');
  L.tileLayer('https://webrd0{{s}}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={{x}}&y={{y}}&z={{z}}', {{
    subdomains: ['1', '2', '3', '4'], maxZoom: 18, attribution: '© 高德地图'
  }}).addTo(map);
  var all = [];
  DAYS.forEach(function (d) {{
    d.points.forEach(function (p) {{ all.push(p); }});
    if (d.points.length > 1) {{
      L.polyline(d.points, {{ color: d.color, weight: 3, opacity: 0.85 }})
        .bindTooltip('DAY ' + d.idx).addTo(map)
        .on('click', function () {{
          var el = document.getElementById('day-' + d.idx);
          if (el) el.scrollIntoView({{ behavior: 'smooth' }});
        }});
    }}
    if (d.overnight) {{
      L.circleMarker(d.overnight.latlng, {{
        radius: 6, color: d.color, fillColor: d.color, fillOpacity: 0.9
      }}).bindTooltip('D' + d.idx + ' 过夜：' + d.overnight.name).addTo(map);
    }}
  }});
  if (all.length) map.fitBounds(all, {{ padding: [24, 24] }});
}})();
</script>
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

    meta = itinerary.get("meta", {})

    preview_banner = ""
    if meta.get("preview_mode"):
        preview_banner = ('<div class="preview-banner">⚠ 预览模式：路耗为估算值，'
                          '偏远路段可能偏差数小时，不可直接照此出行。配置高德后可生成可执行版。</div>')

    degraded = meta.get("degraded", [])
    disclaimer = ""
    if degraded:
        disclaimer = (
            f'<div class="disclaimer">⚠ 部分数据未核验（缺失依赖：{esc(", ".join(degraded))}），'
            f'出行前请自行确认坐标、路况与海拔信息。</div>'
        )

    days_html = "".join(render_day(d) for d in itinerary.get("days", []))

    out_html = TEMPLATE.format(
        title=esc(meta.get("title", "自驾行程")),
        preview_banner=preview_banner,
        disclaimer=disclaimer,
        total_km=summary["total_distance_km"],
        total_hours=summary["total_drive_hours"],
        max_elev=(f'{summary["max_elevation_m"]:.0f}m' if summary["max_elevation_m"] is not None else "未知"),
        warning_count=summary["warning_count"],
        has_plan_b="有" if summary["has_plan_b"] else "无",
        change_block=render_change_block(itinerary),
        trip_claims=render_claims(itinerary.get("claims"), "全程核验项"),
        days_html=days_html,
        map_days_json=json.dumps(collect_map_days(itinerary), ensure_ascii=False),
    )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(out_html, encoding="utf-8")
    print(json.dumps({"output": str(out_path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
