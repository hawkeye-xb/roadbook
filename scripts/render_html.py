#!/usr/bin/env python3
"""把 itinerary.json 渲染成独立 HTML 文件。零依赖（不用 jinja2）。

用法：
  python3 render_html.py --input work/itinerary.json --out outputs/trip.html

布局：左侧逐日卡片栏 + 右侧全高地图（移动端上下堆叠）。
地图：真实道路几何 polyline（segments[].geometry，由 fetch_geometry.py 写入；
无几何的段回退为虚线直连）、分类标记（🏨 过夜 / ⭐ 途经 / ⛽ 补给）带弹窗、
风险日变色、点卡片高亮当天路线。
卡片：海拔链、风险徽章、真实/估算驾驶标注、分辨率徽章、claims 核验标注。
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
    "DOC_CLAIM_GAP": "原文数字偏差",
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

COLOR_ROUTE = "#16a34a"   # 统一绿色（近高德导航色），不按天染色
COLOR_ROUGH = "#8f1d1d"   # 风险日（error 级警告/季节性道路）
COLOR_SPECIAL = "#3b82f6" # 特殊路段（segment.special）

PREVIEW_MODE = False  # main() 里按 meta.preview_mode 设置


def esc(s):
    return html.escape(str(s)) if s is not None else ""


def day_risk(day):
    """低/中/高：error 级警告→高；warning（除预览聚合的坐标项）或季节性道路 claim→中。"""
    warns = [w for w in day.get("warnings", []) if w.get("code") != "UNVERIFIED_POI"]
    if any(w.get("level") == "error" for w in warns):
        return "high"
    if warns or any(c.get("type") == "seasonal_road" for c in day.get("claims", []) or []):
        return "mid"
    return "low"


def elevation_chain(day):
    """起点→途经→过夜的海拔链，如 800m → 1900m → 800m。缺数据的点跳过。"""
    elevs = []
    segs = day.get("segments", [])
    if segs:
        e = (segs[0].get("from") or {}).get("elevation_m")
        if e is not None:
            elevs.append(e)
    for stop in day.get("stops", []):
        if stop.get("elevation_m") is not None:
            elevs.append(stop["elevation_m"])
    ov = day.get("overnight") or {}
    if ov.get("elevation_m") is not None:
        elevs.append(ov["elevation_m"])
    if len(elevs) < 2:
        return ""
    return " → ".join(f"{e:.0f}m" for e in elevs)


def place_line(place, label):
    if not place:
        return ""
    src = place.get("source", "unverified")
    if src == "amap":
        src_badge = ""
    elif src == "doc_claim":
        src_badge = '<span class="badge unverified">攻略原文</span>'
    else:
        src_badge = "" if PREVIEW_MODE else '<span class="badge unverified">未核验</span>'
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


RISK_LABELS = {"low": ("低", "risk-low"), "mid": ("中", "risk-mid"), "high": ("高", "risk-high")}


def render_day(day):
    di = day.get("day_index", "?")
    total_km = sum(s.get("distance_km") or 0 for s in day.get("segments", []))
    total_min = sum(s.get("duration_min") or 0 for s in day.get("segments", []))
    all_amap = all((s.get("from") or {}).get("source") == "amap" and (s.get("to") or {}).get("source") == "amap"
                   for s in day.get("segments", [])) and day.get("segments")
    drive_label = "真实驾驶" if all_amap and not PREVIEW_MODE else "估算驾驶"
    drive_html = (f'<div class="drive-line"><span class="drive-label">{drive_label}</span>'
                  f'<b>{total_km:.1f}km / {total_min/60:.1f}h</b></div>') if total_min else ""

    # doc 模式：原文声称 vs 实测对照（有 claimed 就展示，差超 25% 标红）
    cl_km = sum((s.get("claimed") or {}).get("distance_km") or 0 for s in day.get("segments", []))
    cl_min = sum((s.get("claimed") or {}).get("duration_min") or 0 for s in day.get("segments", []))
    diff_html = ""
    if cl_km and total_km:
        big = abs(total_km - cl_km) / cl_km > 0.25 or (cl_min and total_min and abs(total_min - cl_min) / cl_min > 0.25)
        cl_txt = f"{cl_km:.0f}km" + (f" / {cl_min/60:.1f}h" if cl_min else "")
        ms_txt = f"{total_km:.0f}km" + (f" / {total_min/60:.1f}h" if total_min else "")
        diff_html = (f'<div class="claim-diff{" big" if big else ""}">📋 原文称 {cl_txt}，实测 {ms_txt}</div>')

    chain = elevation_chain(day)
    chain_html = f'<div class="elev-chain">海拔 {esc(chain)}</div>' if chain else ""

    route_names = []
    for seg in day.get("segments", []):
        for p in (seg.get("from"), seg.get("to")):
            n = (p or {}).get("name")
            if n and (not route_names or route_names[-1] != n):
                route_names.append(n)
    title_route = " → ".join(route_names) if route_names else ""

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
    note_html = f'<div class="day-note">{esc(day.get("note"))}</div>' if day.get("note") else ""

    # 信息密度分级：温度/穿衣/拍摄/美食这类建议折叠，顶层不堆内容
    tips = day.get("tips") or {}
    tips_html = ""
    if tips:
        sections = "".join(
            f'<div class="detail-section"><div class="detail-label">{esc(k)}</div>'
            f'<div class="detail-content">{esc(v)}</div></div>'
            for k, v in tips.items())
        tips_html = f'<details class="day-detail"><summary>当日建议（{len(tips)} 项）</summary>{sections}</details>'

    fid_label, fid_cls = FIDELITY_LABELS.get(day.get("fidelity", "skeleton"), FIDELITY_LABELS["skeleton"])
    risk_label, risk_cls = RISK_LABELS[day_risk(day)]

    return f'''
    <section class="day-card" id="day-{di}" data-day="{di}">
      <header class="day-head">
        <span class="day-idx">D{di}</span>
        <span class="day-date">{esc(day.get("date", ""))}</span>
        <span class="badge risk {risk_cls}">{risk_label}</span>
        <span class="badge fidelity {fid_cls}">{fid_label}</span>
      </header>
      <div class="day-route-title">{esc(title_route)}</div>
      {chain_html}
      {drive_html}
      {diff_html}
      {note_html}
      {tips_html}
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


def to_latlng(lnglat):
    return [lnglat[1], lnglat[0]]


def collect_map_days(itinerary):
    """每天：分段几何（真实/直连）、分类标记。geometry 存 [lng,lat]，此处翻成 Leaflet 的 [lat,lng]。"""
    map_days = []
    for day in itinerary.get("days", []):
        risk = day_risk(day)
        color = COLOR_ROUGH if risk == "high" else COLOR_ROUTE
        segs_js, markers = [], []

        for seg in day.get("segments", []):
            geom = seg.get("geometry")
            o = (seg.get("from") or {}).get("lnglat")
            d = (seg.get("to") or {}).get("lnglat")
            special = seg.get("special")
            if geom and len(geom) > 1:
                segs_js.append({"pts": [to_latlng(p) for p in geom], "real": True, "sp": special})
            elif o and d:
                segs_js.append({"pts": [to_latlng(o), to_latlng(d)], "real": False, "sp": special})
            for rp in seg.get("refuel_points", []) or []:
                if rp.get("lnglat"):
                    markers.append({"t": "rest", "ll": to_latlng(rp["lnglat"]),
                                    "name": rp.get("name", ""), "desc": "补给点"})

        for stop in day.get("stops", []):
            if stop.get("lnglat"):
                desc_bits = [f"D{day.get('day_index')} 途经"]
                if stop.get("visit_min"):
                    desc_bits.append(f"游玩约 {stop['visit_min']} 分钟")
                if stop.get("elevation_m") is not None:
                    desc_bits.append(f"海拔 {stop['elevation_m']:.0f}m")
                for a in stop.get("advisories", []) or []:
                    desc_bits.append(str(a))
                markers.append({"t": "scenic", "ll": to_latlng(stop["lnglat"]),
                                "name": stop.get("name", ""), "desc": " · ".join(desc_bits),
                                "wx": stop.get("weather")})

        ov = day.get("overnight") or {}
        if ov.get("lnglat"):
            desc = f"D{day.get('day_index')} 过夜" + (f" · 海拔 {ov['elevation_m']:.0f}m" if ov.get("elevation_m") is not None else "")
            markers.append({"t": "hotel", "ll": to_latlng(ov["lnglat"]),
                            "name": ov.get("name", ""), "desc": desc, "wx": ov.get("weather")})

        if not segs_js and not markers:
            continue
        map_days.append({"idx": day.get("day_index"), "color": color, "risk": risk,
                         "segs": segs_js, "markers": markers})
    return map_days


TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%%TITLE%%</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.css"
      onerror="document.documentElement.classList.add('no-map')">
<style>
  :root {
    --bg:#f4f5f1; --surface:#ffffff; --ink:#1c2420; --ink-dim:#57614e;
    --line:#dfe2d8; --accent:#2f6f63; --warn:#a5702a; --warn-bg:#f5ead4;
    --err:#a5432e; --err-bg:#f6ddd4; --unverified-bg:#eee3c8; --unverified-ink:#7a5b18;
    --pin-bg:#dcebe6; --pin-ink:#2f6f63; --chain-bg:#e4efe9; --chain-ink:#1f5c4d;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg:#14171a; --surface:#1b1f1e; --ink:#e6e9e2; --ink-dim:#a7b0a2;
      --line:#2c322c; --accent:#64a894; --warn:#d9a15d; --warn-bg:#33291a;
      --err:#d97a63; --err-bg:#332019; --unverified-bg:#3a331c; --unverified-ink:#e0c37a;
      --pin-bg:#1e3129; --pin-ink:#64a894; --chain-bg:#1e3129; --chain-ink:#8cc7b2;
    }
  }
  * { box-sizing:border-box; }
  body {
    margin:0; background:var(--bg); color:var(--ink);
    font-family:-apple-system,"PingFang SC",system-ui,sans-serif;
    font-variant-numeric:tabular-nums;
  }
  .preview-banner {
    position:sticky; top:0; z-index:2000; background:var(--err-bg); color:var(--err);
    padding:0.7rem 1rem; font-size:0.88rem; font-weight:600; border-bottom:1px solid var(--err);
  }
  .layout { display:flex; height:100vh; }
  .preview-banner ~ .layout { height:calc(100vh - 3rem); }
  #sidebar { width:460px; overflow-y:auto; padding:1rem 1rem 4rem; flex-shrink:0; }
  #map { flex:1; height:100%; }
  .no-map #map { display:none; }
  @media (max-width: 900px) {
    .layout { flex-direction:column; height:auto; }
    #map { order:-1; height:45vh; width:100%; flex:none; position:sticky; top:0; z-index:1500; }
    #sidebar { width:100%; }
  }
  h1 { font-size:1.25rem; margin:0.4rem 0 0.6rem; }
  .summary {
    display:flex; flex-wrap:wrap; gap:0.4rem 1.2rem; margin:0 0 1rem;
    padding:0.8rem 1rem; background:var(--surface); border:1px solid var(--line); border-radius:8px;
    font-size:0.85rem; color:var(--ink-dim);
  }
  .summary b { color:var(--ink); }
  .change-block {
    background:var(--surface); border:1px solid var(--accent); border-radius:8px;
    padding:0.9rem 1.2rem; margin-bottom:1rem; font-size:0.85rem;
  }
  .change-head { font-weight:700; color:var(--accent); }
  .change-block ul { margin:0.5rem 0 0; padding-left:1.2rem; }
  .change-deltas { margin-top:0.4rem; color:var(--ink-dim); }
  .day-card {
    background:var(--surface); border:1px solid var(--line); border-radius:10px;
    padding:0.9rem 1.1rem; margin-bottom:0.8rem; cursor:pointer;
    transition:border-color 0.15s, box-shadow 0.15s;
  }
  .day-card.active { border-color:var(--accent); box-shadow:0 0 0 1px var(--accent); }
  .day-head { display:flex; align-items:baseline; gap:0.6em; }
  .day-idx { font-weight:700; color:var(--accent); font-size:1.02rem; }
  .day-date { color:var(--ink-dim); font-size:0.82rem; }
  .day-route-title { font-weight:600; font-size:0.92rem; margin:0.35rem 0 0.2rem; }
  .elev-chain {
    display:inline-block; background:var(--chain-bg); color:var(--chain-ink);
    font-size:0.78rem; padding:0.1em 0.6em; border-radius:4px; margin:0.2rem 0;
  }
  .drive-line { display:flex; justify-content:space-between; align-items:baseline;
                font-size:0.9rem; margin:0.25rem 0; }
  .drive-label { color:var(--ink-dim); font-size:0.8rem; }
  .day-note { font-size:0.83rem; color:var(--ink-dim); margin:0.3rem 0; }
  .claim-diff { font-size:0.82rem; color:var(--ink-dim); margin:0.2rem 0;
                padding:0.25rem 0.5rem; background:var(--bg); border-radius:4px; }
  .claim-diff.big { background:var(--err-bg); color:var(--err); font-weight:600; }
  .segment { margin:0.5rem 0; font-size:0.9rem; }
  .place { display:flex; gap:0.5em; align-items:center; flex-wrap:wrap; }
  .place-label { color:var(--ink-dim); font-size:0.76rem; min-width:2.4em; }
  .badge { font-size:0.68rem; padding:0.05em 0.5em; border-radius:3px; }
  .badge.unverified { background:var(--unverified-bg); color:var(--unverified-ink); }
  .badge.pinned { background:var(--pin-bg); color:var(--pin-ink); }
  .badge.fidelity { margin-left:auto; font-weight:600; }
  .badge.risk { font-weight:700; }
  .risk-low { background:var(--chain-bg); color:var(--chain-ink); }
  .risk-mid { background:var(--warn-bg); color:var(--warn); }
  .risk-high { background:var(--err-bg); color:var(--err); }
  .fid-skeleton { background:var(--unverified-bg); color:var(--unverified-ink); }
  .fid-filled { background:var(--pin-bg); color:var(--pin-ink); }
  .fid-verified { background:var(--accent); color:var(--surface); }
  ul.stops, ul.warnings { margin:0.5rem 0 0; padding-left:1.1rem; font-size:0.86rem; }
  ul.stops li { margin:0.28rem 0; }
  .advisory { font-size:0.78rem; color:var(--ink-dim); margin-left:0.5em; }
  .warn-item { list-style:none; margin:0.3rem 0 0.3rem -1.1rem; padding:0.35rem 0.6rem; border-radius:5px; }
  .warn-item.warning { background:var(--warn-bg); color:var(--warn); }
  .warn-item.error { background:var(--err-bg); color:var(--err); }
  .warn-code { font-weight:700; margin-right:0.5em; }
  .claims { margin-top:0.6rem; font-size:0.85rem; }
  .claims-head { font-weight:700; margin-bottom:0.3rem; }
  .claims ul { margin:0; padding-left:0; }
  .claim-item { list-style:none; padding:0.35rem 0.5rem; margin:0.25rem 0; border-radius:5px; background:var(--bg); }
  .claim-icon { margin-right:0.4em; }
  .claim-verdict { color:var(--ink-dim); font-size:0.8rem; margin-left:0.6em; }
  .claim-date { color:var(--ink-dim); font-size:0.75rem; margin-left:0.6em; }
  .claim-item.v-contradicted { background:var(--err-bg); }
  .claim-item details { margin-top:0.3rem; font-size:0.82rem; color:var(--ink-dim); }
  .evidence blockquote { margin:0.2rem 0 0.4rem 0.8rem; padding-left:0.6rem; border-left:2px solid var(--line); }
  .disclaimer {
    font-size:0.8rem; color:var(--ink-dim); background:var(--unverified-bg);
    padding:0.8rem 1rem; border-radius:6px; margin-bottom:1rem;
  }
  .poi-icon { font-size:18px; line-height:1; text-shadow:0 0 3px #fff, 0 0 3px #fff; }
  .day-detail { margin:0.4rem 0; font-size:0.85rem; }
  .day-detail summary { color:var(--ink-dim); cursor:pointer; }
  .detail-section { margin:0.5rem 0 0 0.2rem; }
  .detail-label { font-size:0.72rem; font-weight:700; letter-spacing:0.08em; color:var(--ink-dim); }
  .detail-content { margin-top:0.15rem; line-height:1.5; }
  .popup-wx { margin-top:0.4rem; padding:0.3rem 0.5rem; background:#0d2137; color:#cfe3f5; border-radius:4px; font-size:0.82rem; }
  .popup-slot { margin-top:0.3rem; padding:0.25rem 0.5rem; border:1px dashed #aaa; border-radius:4px; color:#888; font-size:0.78rem; }
</style>
</head>
<body>
%%PREVIEW_BANNER%%
<div class="layout">
<div id="sidebar">
  <h1>%%TITLE%%</h1>
  %%DISCLAIMER%%
  <div class="summary">
    <span>总里程 <b>%%TOTAL_KM%% km</b></span>
    <span>总驾驶 <b>%%TOTAL_HOURS%% 小时</b></span>
    <span>最高海拔 <b>%%MAX_ELEV%%</b></span>
    <span>警告 <b>%%WARNING_COUNT%% 条</b></span>
    <span>Plan B <b>%%HAS_PLAN_B%%</b></span>
  </div>
  %%CHANGE_BLOCK%%
  %%TRIP_CLAIMS%%
  %%DAYS_HTML%%
</div>
<div id="map"></div>
</div>
<script src="https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.min.js"
        onerror="document.documentElement.classList.add('no-map')"></script>
<script>
(function () {
  var DAYS = %%MAP_DAYS_JSON%%;
  if (!window.L || !DAYS.length) {
    document.documentElement.classList.add('no-map');
    return;
  }
  var ICONS = { hotel: '🏨', scenic: '⭐', rest: '⛽' };
  var map = L.map('map');
  L.tileLayer('https://webrd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}', {
    subdomains: ['1', '2', '3', '4'], maxZoom: 18, attribution: '© 高德地图'
  }).addTo(map);

  var all = [], layers = {}, selected = null;

  DAYS.forEach(function (d) {
    var group = L.layerGroup().addTo(map);
    d.segs.forEach(function (s) {
      s.pts.forEach(function (p) { all.push(p); });
      var base = s.sp ? '#3b82f6' : d.color;
      var line = L.polyline(s.pts, {
        color: base, weight: 4, opacity: 0.85,
        dashArray: s.real ? null : '6 8'
      }).bindTooltip('D' + d.idx + (s.sp ? ' · 📍' + s.sp : '') + (s.real ? '' : '（直连示意，非真实路线）'))
        .on('click', function () { selectDay(d.idx, true); })
        .addTo(group);
      line._base = base;
    });
    d.markers.forEach(function (m) {
      var html = '<b>' + m.name + '</b><br>' + m.desc;
      if (m.wx) html += '<div class="popup-wx">🌤 ' + m.wx + '</div>';
      html += '<div class="popup-slot">📸 照片 — 待添加</div><div class="popup-slot">📝 笔记 — 待补充</div>';
      L.marker(m.ll, {
        icon: L.divIcon({ className: 'poi-icon', html: ICONS[m.t] || '📍', iconSize: [20, 20], iconAnchor: [10, 10] })
      }).bindPopup(html).addTo(group);
      all.push(m.ll);
    });
    layers[d.idx] = { group: group, color: d.color };
  });

  function setDim(idx, state) {
    // state: 'normal' | 'dim' | 'active' —— 三态配色：常态绿 / 未选中淡化 / 选中加粗
    var l = layers[idx];
    if (!l) return;
    l.group.eachLayer(function (layer) {
      if (layer.setStyle) {
        if (state === 'dim') layer.setStyle({ opacity: 0.2, weight: 3 });
        else if (state === 'active') layer.setStyle({ color: layer._base, opacity: 0.95, weight: 6 });
        else layer.setStyle({ color: layer._base, opacity: 0.85, weight: 4 });
      }
      if (layer.setOpacity) layer.setOpacity(state === 'dim' ? 0.35 : 1);
    });
  }

  function selectDay(idx, scrollCard) {
    if (selected === idx) {  // 再点一次取消选中
      Object.keys(layers).forEach(function (k) { setDim(k, 'normal'); });
      document.querySelectorAll('.day-card.active').forEach(function (el) { el.classList.remove('active'); });
      selected = null;
      return;
    }
    selected = idx;
    Object.keys(layers).forEach(function (k) { setDim(k, String(k) !== String(idx) ? 'dim' : 'active'); });
    document.querySelectorAll('.day-card.active').forEach(function (el) { el.classList.remove('active'); });
    var card = document.getElementById('day-' + idx);
    if (card) {
      card.classList.add('active');
      if (scrollCard) card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
    var pts = [];
    var d = DAYS.find(function (x) { return String(x.idx) === String(idx); });
    if (d) {
      d.segs.forEach(function (s) { pts = pts.concat(s.pts); });
      d.markers.forEach(function (m) { pts.push(m.ll); });
      if (pts.length) map.fitBounds(pts, { padding: [40, 40] });
    }
  }

  document.querySelectorAll('.day-card').forEach(function (card) {
    card.addEventListener('click', function (e) {
      if (e.target.closest('details, a')) return;
      selectDay(card.getAttribute('data-day'), false);
    });
  });

  if (all.length) map.fitBounds(all, { padding: [24, 24] });
})();
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

    global PREVIEW_MODE
    PREVIEW_MODE = bool(meta.get("preview_mode"))

    preview_banner = ""
    if PREVIEW_MODE:
        preview_banner = ('<div class="preview-banner">⚠ 预览模式：路耗为估算值，'
                          '偏远路段可能偏差数小时，不可直接照此出行。配置高德后可生成可执行版。</div>')

    degraded = meta.get("degraded", [])
    disclaimer = ""
    if degraded:
        disclaimer = (
            f'<div class="disclaimer">⚠ 部分数据未核验（缺失依赖：{esc(", ".join(degraded))}），'
            f'出行前请自行确认坐标、路况与海拔信息。</div>'
        )

    replacements = {
        "%%TITLE%%": esc(meta.get("title", "自驾行程")),
        "%%PREVIEW_BANNER%%": preview_banner,
        "%%DISCLAIMER%%": disclaimer,
        "%%TOTAL_KM%%": str(summary["total_distance_km"]),
        "%%TOTAL_HOURS%%": str(summary["total_drive_hours"]),
        "%%MAX_ELEV%%": (f'{summary["max_elevation_m"]:.0f}m' if summary["max_elevation_m"] is not None else "未知"),
        "%%WARNING_COUNT%%": str(summary["warning_count"]),
        "%%HAS_PLAN_B%%": "有" if summary["has_plan_b"] else "无",
        "%%CHANGE_BLOCK%%": render_change_block(itinerary),
        "%%TRIP_CLAIMS%%": render_claims(itinerary.get("claims"), "全程核验项"),
        "%%DAYS_HTML%%": "".join(render_day(d) for d in itinerary.get("days", [])),
        "%%MAP_DAYS_JSON%%": json.dumps(collect_map_days(itinerary), ensure_ascii=False, separators=(",", ":")),
    }
    out_html = TEMPLATE
    for k, v in replacements.items():
        out_html = out_html.replace(k, v)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(out_html, encoding="utf-8")
    print(json.dumps({"output": str(out_path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
