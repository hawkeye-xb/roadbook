#!/usr/bin/env python3
"""把 itinerary.json 渲染成独立 HTML 文件。零依赖（不用 jinja2）。

用法：
  python3 render_html.py --input work/itinerary.json --out outputs/trip.html

布局：左侧逐日卡片栏 + 右侧全高地图（移动端上下堆叠）。
地图：真实道路几何 polyline（segments[].geometry，由 fetch_geometry.py 写入；
无几何的段回退为虚线直连）、分类标记（🏨 过夜 / ⭐ 途经 / ⛽ 补给）带弹窗、
风险日变色、点卡片高亮当天路线。
卡片：海拔链、风险徽章、真实/估算驾驶标注、分辨率徽章、claims 核验标注、
高德一键导航（当天途经点 + 终点一次带进高德 App）。
移动端：地图收窄到 3 成，日卡改成手风琴——收起只留 D/日期/路线一行，展开一天才看
导航按钮、途经点；建议与核验依据再收一层折叠。
"""
import argparse
import html
import json
import math
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

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

# 例外标注制：核验通过的不打标（沉默即合格），只有问题项才有图标和说明
PROBLEM_ICONS = {
    "unverified": ("❓", "待核实"),
    "contradicted": ("❌", "与近期来源冲突"),
    "stale": ("⏳", "可能过期，建议复检"),
}
OK_VERDICTS = {"confirmed", "user_confirmed"}

COLOR_ROUTE = "#16a34a"   # 统一绿色（近高德导航色），不按天染色
COLOR_ROUGH = "#8f1d1d"   # 风险日（error 级警告/季节性道路）
COLOR_SPECIAL = "#3b82f6" # 特殊路段（segment.special）

PREVIEW_MODE = False  # main() 里按 meta.preview_mode 设置
ALT_RELEVANT = True   # main() 里按行程数据判定：全程无 1500m+ 海拔（如东北/东南沿海）则隐藏海拔展示


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
    if not ALT_RELEVANT:
        return ""
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
    elev_txt = f' · 海拔 {elev:.0f}m' if elev is not None and ALT_RELEVANT else ""
    return (f'<div class="place"><span class="place-label">{esc(label)}</span> '
            f'{esc(place.get("name", ""))}{src_badge}{pin_badge}{elev_txt}</div>')


def render_claims(claims, heading=None):
    """例外标注制：问题项排前带图标；核验通过的以普通文字呈现（证据折叠可查），不打 ✅。"""
    if not claims:
        return ""
    items = ""
    ordered = sorted(claims, key=lambda c: c.get("verdict") in OK_VERDICTS)
    for c in ordered:
        verdict = c.get("verdict", "unverified")
        ev_html = ""
        for ev in c.get("evidence", []) or []:
            meta_bits = [b for b in [ev.get("source"), ev.get("date")] if b]
            quote = ev.get("quote")
            ev_html += (f'<div class="evidence">来源：{esc(" · ".join(meta_bits)) or "未知"}'
                        + (f'<blockquote>{esc(quote)}</blockquote>' if quote else "") + "</div>")
        detail = f'<details><summary>证据</summary>{ev_html}</details>' if ev_html else ""
        if verdict in OK_VERDICTS:
            items += (f'<li class="claim-item v-ok">'
                      f'<span class="claim-text">{esc(c.get("claim", ""))}</span>{detail}</li>')
        else:
            icon, verdict_txt = PROBLEM_ICONS.get(verdict, ("❓", "待核实"))
            checked = f'<span class="claim-date">核验于 {esc(c["checked_at"])}</span>' if c.get("checked_at") else ""
            items += (f'<li class="claim-item v-{esc(verdict)}">'
                      f'<span class="claim-icon">{icon}</span>'
                      f'<span class="claim-text">{esc(c.get("claim", ""))}</span>'
                      f'<span class="claim-verdict">{verdict_txt}</span>{checked}{detail}</li>')
    head = f'<div class="claims-head">{esc(heading)}</div>' if heading else ""
    return f'<div class="claims">{head}<ul>{items}</ul></div>'


def render_trip_claims_block(claims, day_claims_exist=False):
    """全程核验项：次要信息，折叠放到日卡之后，问题数进标题。
    整本路书一条 claim 都没有 = 检索核验被跳过了——必须显式警示，不能静默。"""
    if not claims and not day_claims_exist:
        return ('<div class="disclaimer">⚠ 本路书未附检索核验记录：门禁动线、预约规则、'
                '季节封路、花期等信息未经查证，出发前请自行核实。</div>')
    if not claims:
        return ""
    problems = [c for c in claims if c.get("verdict") not in OK_VERDICTS]
    label = f'出发前核对清单：{len(problems)} 项需关注 / 共 {len(claims)} 项' if problems \
        else f'出发前核对清单：全部核验通过（{len(claims)} 项，点开可查证据）'
    return (f'<details class="trip-claims"{" open" if problems else ""}>'
            f'<summary>📋 {esc(label)}</summary>{render_claims(claims)}</details>')


RISK_LABELS = {"low": ("低", "risk-low"), "mid": ("中", "risk-mid"), "high": ("高", "risk-high")}


def usable_place(p):
    """坐标成对、为有限数字、在经纬度范围内才算可用。
    [None, None]、NaN/Infinity、越界值都会拼出坏链接（dlat=None / dlon=nan），高德点了没反应。"""
    if not isinstance(p, dict):
        return False
    ll = p.get("lnglat")
    if not (isinstance(ll, (list, tuple)) and len(ll) == 2):
        return False
    lng, lat = ll
    if not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in ll):
        return False
    return (math.isfinite(lng) and math.isfinite(lat)
            and -180 <= lng <= 180 and -90 <= lat <= 90)


def place_km(a, b):
    """两点直线距离（公里）。用来判断「景点是不是已经被路段端点覆盖」，不追求精度。"""
    lng1, lat1 = a
    lng2, lat2 = b
    dx = (lng2 - lng1) * 111.32 * math.cos(math.radians((lat1 + lat2) / 2))
    dy = (lat2 - lat1) * 110.57
    return math.hypot(dx, dy)


NEAR_KM = 2.0  # 途经点离某个路段端点这么近，就当作同一处，不再单独加进导航


def day_nav_points(day):
    """当天一键导航的点序：路段终点是主干，当天景点插在终点前。
    起点不放（由 day_origin 决定）；无路段的日子退回 stops + 过夜点。
    景点离某个路段端点不到 NEAR_KM 就不再重复加（景区本身就是路段终点的日子）。
    末点坐标不可用时整条返回空：宁可不出按钮，也不能让终点悄悄变成倒数第二个点。"""
    segs = day.get("segments") or []
    stops = day.get("stops") or []
    if segs:
        ends = [s.get("to") for s in segs]
        if ends and not usable_place(ends[-1]):
            ov = day.get("overnight")
            if usable_place(ov) and isinstance(ends[-1], dict) and ov.get("name") == ends[-1].get("name"):
                ends = ends[:-1] + [ov]  # 末点缺坐标但过夜点同名可用：终点落在过夜点上
            else:
                return []
        seq = [p for p in ends if usable_place(p)]
        extras = [s for s in stops if usable_place(s)
                  and all(place_km(s["lnglat"], p["lnglat"]) > NEAR_KM for p in seq)]
        cands = seq[:-1] + extras + seq[-1:]
    else:
        if not any(usable_place(s) for s in stops):
            return []  # 休息日：只有过夜点，没有可导航的行程
        cands = list(stops) + [day.get("overnight")]
    pts = []
    for p in cands:
        if not usable_place(p):
            continue
        cur = {"name": p.get("name", ""), "lnglat": list(p["lnglat"])}
        if pts and pts[-1]["lnglat"] == cur["lnglat"]:  # 按坐标判重：同名不同点是两个地方
            if cur["name"]:  # 同坐标连着出现时，用后一个的名字（终点名更贴近实际）
                pts[-1]["name"] = cur["name"]
            continue
        pts.append(cur)
    return pts


def day_origin(day):
    """当天行程的起点：第一段的 from；城内日（无路段）退回第一个有坐标的途经点。"""
    segs = day.get("segments") or []
    if segs:
        p = segs[0].get("from")
        return {"name": p.get("name", ""), "lnglat": list(p["lnglat"])} if usable_place(p) else None
    for s in day.get("stops") or []:
        if usable_place(s):
            return {"name": s.get("name", ""), "lnglat": list(s["lnglat"])}
    return None


def amap_nav_urls(points, origin=None):
    """高德地图 App 路线规划唤起链接（Android amapuri / iOS iosamap，参数同构）。
    多途经点只有 App URI 支持（Web URI 限 1 个）。坐标来自高德即 GCJ-02，dev=0 不再偏移。
    origin 为空时起点留空 = 高德按「我的位置」规划。"""
    if not points:
        return None

    def name(p):  # "|" 是途经点分隔符，名字里出现会把列表切错
        return quote(str(p["name"]).replace("|", " "), safe="")

    *vias, dest = points
    slat = slon = sname = ""
    if origin and origin.get("lnglat"):
        slat, slon = origin["lnglat"][1], origin["lnglat"][0]
        sname = name(origin)
    q = (f"sourceApplication=roadbook&sid=&slat={slat}&slon={slon}&sname={sname}"
         f"&did=&dlat={dest['lnglat'][1]}&dlon={dest['lnglat'][0]}&dname={name(dest)}&dev=0&t=0")
    if vias:
        q += (f"&vian={len(vias)}"
              f"&vialons={'|'.join(str(p['lnglat'][0]) for p in vias)}"
              f"&vialats={'|'.join(str(p['lnglat'][1]) for p in vias)}"
              f"&vianames={'|'.join(name(p) for p in vias)}")
    return {"android": "amapuri://route/plan/?" + q, "ios": "iosamap://path?" + q}


def render_nav_button(day):
    """两套链接：*_fixed 是当天行程起点版本，点击时按日期选（见页面脚本 nav）。
    当天开这一天就用当前位置；翻看其它日子用起点，避免从「人在哪」规划出一条不是书里的路。"""
    points = day_nav_points(day)
    urls = amap_nav_urls(points)
    if not urls:
        return ""
    origin = day_origin(day)
    if origin and points and origin["lnglat"] == points[0]["lnglat"]:
        origin = None  # 起点本身就是要去的第一个点（城内日），没有固定起点的意义
    fixed_attrs = ""
    if origin:
        fixed = amap_nav_urls(points, origin)
        fixed_attrs = (f' data-android-fixed="{esc(fixed["android"])}"'
                       f' data-ios-fixed="{esc(fixed["ios"])}"')
    return (f'<a class="nav-btn" href="#" data-android="{esc(urls["android"])}" data-ios="{esc(urls["ios"])}"'
            f'{fixed_attrs} data-date="{esc(day.get("date", ""))}">'
            f'🧭 高德一键导航</a>')


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

    # 第二层折叠：建议、核验依据这类「行前才细读」的内容不上顶层；
    # 风险警告不进折叠——安全信息必须撑开就能看见
    tips = day.get("tips") or {}
    tips_html = "".join(
        f'<div class="detail-section"><div class="detail-label">{esc(k)}</div>'
        f'<div class="detail-content">{esc(v)}</div></div>'
        for k, v in tips.items())
    claims = day.get("claims") or []
    claims_html = render_claims(claims)
    problems = sum(1 for c in claims if c.get("verdict") not in OK_VERDICTS)
    more_bits = ([f"{len(tips)} 项建议"] if tips else [])
    if claims_html:
        more_bits.append(f"{problems} 项待核实" if problems else "核验依据")
    more_html = (f'<details class="day-more{" has-problems" if problems else ""}" open>'
                 f'<summary>{" · ".join(more_bits)}</summary>{tips_html}{claims_html}</details>'
                 ) if more_bits else ""
    mini = " · ".join(x for x in [title_route or day.get("note") or "",
                                 (f"{total_km:.0f}km" if total_km else "")] if x)

    # 例外标注制：已核验/低风险是常态不打标，只有"还没到位"和"有风险"才出徽章
    fid = day.get("fidelity", "skeleton")
    fid_badge = ""
    if fid != "verified":
        fid_label, fid_cls = FIDELITY_LABELS.get(fid, FIDELITY_LABELS["skeleton"])
        fid_badge = f'<span class="badge fidelity {fid_cls}">{fid_label}</span>'
    risk = day_risk(day)
    risk_badge = ""
    if risk != "low":
        risk_label, risk_cls = RISK_LABELS[risk]
        risk_badge = f'<span class="badge risk {risk_cls}">{risk_label}</span>'

    return f'''
    <section class="day-card" id="day-{di}" data-day="{di}">
      <header class="day-head">
        <span class="day-idx">D{di}</span>
        <span class="day-date">{esc(day.get("date", ""))}</span>
        <span class="day-mini">{esc(mini)}</span>
        {risk_badge}
        {fid_badge}
        <span class="day-chev" aria-hidden="true">›</span>
      </header>
      <div class="day-content">
        <div class="day-route-title">{esc(title_route)}</div>
        {chain_html}
        {drive_html}
        {render_nav_button(day)}
        {diff_html}
        {'<ul class="warnings">' + warn_html + '</ul>' if warn_html else ''}
        {note_html}
        {'<ul class="stops">' + stops_html + '</ul>' if stops_html else ''}
        {ov_html}
        {more_html}
      </div>
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
                if stop.get("elevation_m") is not None and ALT_RELEVANT:
                    desc_bits.append(f"海拔 {stop['elevation_m']:.0f}m")
                for a in stop.get("advisories", []) or []:
                    desc_bits.append(str(a))
                markers.append({"t": "scenic", "ll": to_latlng(stop["lnglat"]),
                                "name": stop.get("name", ""), "desc": " · ".join(desc_bits),
                                "wx": stop.get("weather")})

        ov = day.get("overnight") or {}
        if ov.get("lnglat"):
            desc = f"D{day.get('day_index')} 过夜" + (f" · 海拔 {ov['elevation_m']:.0f}m" if ov.get("elevation_m") is not None and ALT_RELEVANT else "")
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
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
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
  .no-map .view-toggle { display:none !important; }
  .view-toggle { display:none; }
  .trip-claims { margin:1rem 0 2rem; font-size:0.87rem; }
  .trip-claims > summary {
    cursor:pointer; padding:0.7rem 1rem; background:var(--surface);
    border:1px solid var(--line); border-radius:8px; font-weight:600;
  }
  .claim-item.v-ok { color:var(--ink-dim); }
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
  .day-mini, .day-chev { display:none; }
  .day-more { margin:0.5rem 0 0.2rem; font-size:0.85rem; }
  .day-more > summary { display:none; }  /* 桌面端二级折叠常展开、不提供收起，summary 只在手机显示 */
  .day-more.has-problems > summary { color:var(--warn); font-weight:600; }
  .day-route-title { font-weight:600; font-size:0.92rem; margin:0.35rem 0 0.2rem; }
  .elev-chain {
    display:inline-block; background:var(--chain-bg); color:var(--chain-ink);
    font-size:0.78rem; padding:0.1em 0.6em; border-radius:4px; margin:0.2rem 0;
  }
  .drive-line { display:flex; justify-content:space-between; align-items:baseline;
                font-size:0.9rem; margin:0.25rem 0; }
  .drive-label { color:var(--ink-dim); font-size:0.8rem; }
  .nav-btn {
    display:flex; align-items:center; justify-content:center; min-height:44px; margin:0.5rem 0;
    background:var(--accent); color:#fff; border-radius:8px; font-size:0.92rem; font-weight:600;
    text-decoration:none;
  }
  .nav-btn:active { opacity:0.8; }
  .nav-hint { font-size:0.8rem; color:var(--warn); background:var(--warn-bg);
              padding:0.4rem 0.6rem; border-radius:6px; margin:-0.2rem 0 0.5rem; }
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
  .map-overview-btn {
    background:#fff; color:#333; border:2px solid rgba(0,0,0,0.2); border-radius:4px;
    padding:0.3rem 0.6rem; font-size:0.82rem; font-weight:600; cursor:pointer;
  }
  .map-overview-btn:hover { background:#f4f4f4; }
  .detail-section { margin:0.5rem 0 0 0.2rem; }
  .detail-label { font-size:0.72rem; font-weight:700; letter-spacing:0.08em; color:var(--ink-dim); }
  .detail-content { margin-top:0.15rem; line-height:1.5; }
  .popup-wx { margin-top:0.4rem; padding:0.3rem 0.5rem; background:#0d2137; color:#cfe3f5; border-radius:4px; font-size:0.82rem; }
  .popup-slot { margin-top:0.3rem; padding:0.25rem 0.5rem; border:1px dashed #aaa; border-radius:4px; color:#888; font-size:0.78rem; }

  /* 移动端规则必须排在全部基础规则之后：同优先级下先写的会被后写的覆盖 */
  @media (max-width: 900px) {
    .layout { flex-direction:column; height:auto; }
    /* 地图收窄到 3 成，把高度让给行程；看路线用「看大图」切全屏 */
    #map { order:-1; height:30vh; width:100%; flex:none; position:sticky; top:0; z-index:1500; }
    #sidebar { width:100%; padding-bottom:calc(5rem + env(safe-area-inset-bottom)); }
    /* 日卡手风琴：收起时就是一行 D/日期/路线/里程，展开一天才看内容（见脚本 accordion） */
    .day-card { padding:0.7rem 0.9rem; scroll-margin-top:calc(30vh + 0.6rem); }
    .day-head { cursor:pointer; align-items:center; min-height:44px; }
    .day-card .day-mini {
      display:block; flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis;
      white-space:nowrap; color:var(--ink-dim); font-size:0.8rem;
    }
    .day-card.open .day-mini { display:none; }
    .day-card .day-chev {
      display:inline-block; color:var(--ink-dim); font-size:1.15rem; line-height:1;
      transition:transform 0.15s;
    }
    .day-card.open .day-chev { transform:rotate(90deg); }
    .badge.fidelity { margin-left:0; }
    /* 收起只由 JS 加 .open 控制；脚本没跑起来（禁 JS/离线）时正文必须照常可读 */
    html.js .day-card .day-content { display:none; }
    html.js .day-card.open .day-content { display:block; }
    .day-more > summary {
      display:flex; align-items:center; min-height:44px;
      color:var(--ink-dim); cursor:pointer; font-size:0.82rem;
    }
    .day-more.has-problems > summary { color:var(--warn); }
    .summary { font-size:0.8rem; gap:0.3rem 0.9rem; padding:0.6rem 0.8rem; margin-bottom:0.7rem; }
    h1 { font-size:1.1rem; margin:0.2rem 0 0.5rem; }
    .view-toggle {
      display:block; position:fixed; bottom:calc(1.1rem + env(safe-area-inset-bottom)); left:50%; transform:translateX(-50%);
      z-index:3000; background:var(--accent); color:#fff; border:none; border-radius:999px;
      padding:0.55rem 1.4rem; font-size:0.92rem; font-weight:600;
      box-shadow:0 3px 10px rgba(0,0,0,0.35); cursor:pointer;
    }
    body.map-full #map { height:100vh; position:fixed; inset:0; }
    body.map-full #sidebar { display:none; }
  }
  @media (prefers-color-scheme: dark) {
    /* 暗色的 accent 是浅绿，白字对比度不足；导航按钮与看大图用深一档底色 */
    .nav-btn, .view-toggle { background:#2f6f63; color:#fff; }
  }
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
%%ELEV_SPAN%%    <span>警告 <b>%%WARNING_COUNT%% 条</b></span>
    <span>Plan B <b>%%HAS_PLAN_B%%</b></span>
  </div>
  %%CHANGE_BLOCK%%
  %%DAYS_HTML%%
  %%TRIP_CLAIMS%%
</div>
<div id="map"></div>
</div>
<noscript><style>#map{display:none!important}.view-toggle{display:none!important}
  .day-mini,.day-chev{display:none!important}</style></noscript>
<button class="view-toggle" id="viewToggle">🗺 看大图</button>
<script>
// 手风琴与日期归一化：不依赖地图脚本。离线、CDN 被墙、禁 JS 也要能读。
(function () {
  var mq = window.matchMedia('(max-width: 900px)');
  var cards = document.querySelectorAll('.day-card');
  function isMobile() { return mq.matches; }
  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function todayStr() {
    var t = new Date();
    return t.getFullYear() + '-' + pad(t.getMonth() + 1) + '-' + pad(t.getDate());
  }
  function dayDate(s) {  // 「2026-09-28」「2026-09-28 周一」都归一化到日期本身
    var m = /(\\d{4})-(\\d{2})-(\\d{2})/.exec(s || '');
    return m ? m[0] : '';
  }
  var RB = window.RB = {
    isMobile: isMobile, todayStr: todayStr, dayDate: dayDate,
    mapSelect: null, mapOverview: null, syncing: false
  };

  function openDay(idx, scrollCard) {
    document.querySelectorAll('.day-card').forEach(function (c) {
      var on = String(c.getAttribute('data-day')) === String(idx);
      c.classList.toggle('open', on);
      c.querySelector('.day-head').setAttribute('aria-expanded', on ? 'true' : 'false');
      var more = c.querySelector('.day-more');
      if (more && !on) more.removeAttribute('open');
    });
    if (RB.mapSelect && !RB.syncing) {
      RB.syncing = true;
      RB.mapSelect(idx);
      RB.syncing = false;
    }
    if (scrollCard) {
      var card = document.getElementById('day-' + idx);
      if (card) card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
  }
  RB.openDay = openDay;

  document.querySelectorAll('.day-card .day-head').forEach(function (head) {
    head.addEventListener('click', function () {
      if (!isMobile()) return;
      var card = head.closest('.day-card');
      if (card.classList.contains('open')) {
        card.classList.remove('open');
        head.setAttribute('aria-expanded', 'false');
        if (RB.mapOverview) RB.mapOverview();
      } else {
        openDay(card.getAttribute('data-day'), false);
      }
    });
    head.addEventListener('keydown', function (e) {
      if (!isMobile() || (e.key !== 'Enter' && e.key !== ' ')) return;
      e.preventDefault();
      head.click();
    });
  });

  // 桌面端二级折叠常展开、summary 只在手机出现；手风琴的可访问性属性同样只在手机挂
  function syncMode() {
    document.querySelectorAll('.day-more').forEach(function (d) {
      if (isMobile()) d.removeAttribute('open');
      else d.setAttribute('open', '');
    });
    cards.forEach(function (c) {
      var head = c.querySelector('.day-head');
      if (isMobile()) {
        head.setAttribute('role', 'button');
        head.setAttribute('tabindex', '0');
        head.setAttribute('aria-expanded', c.classList.contains('open') ? 'true' : 'false');
      } else {
        head.removeAttribute('role');
        head.removeAttribute('tabindex');
        head.removeAttribute('aria-expanded');
      }
    });
  }
  RB.syncMode = syncMode;
  syncMode();
  var lastMobile = mq.matches;

  // 断点切换后手机端要有卡是展开的：旋转屏幕、拉窄窗口后不能变成「全部收起且点不开」
  function ensureOpenCard() {
    if (document.querySelector('.day-card.open')) return;
    var picked = document.querySelector('.day-card.active');
    if (!picked) {
      var todayS = todayStr();
      cards.forEach(function (c) {
        var d = c.querySelector('.day-date');
        if (d && dayDate(d.textContent) === todayS) picked = c;
      });
    }
    if (!picked) picked = cards[0];
    if (picked) openDay(picked.getAttribute('data-day'), false);
  }

  function onViewportChange() {
    if (mq.matches === lastMobile) return;  // 手机滚动收地址栏也会触发 resize，不能每次都重排
    lastMobile = mq.matches;
    syncMode();
    if (mq.matches) ensureOpenCard();
  }
  if (mq.addEventListener) mq.addEventListener('change', onViewportChange);
  else if (mq.addListener) mq.addListener(onViewportChange);
  window.addEventListener('resize', onViewportChange);  // 部分环境不派发 media change

  if (isMobile()) {  // 默认展开今天；不在行程期就展开第一天
    var target = null;
    cards.forEach(function (c) {
      var d = c.querySelector('.day-date');
      if (d && dayDate(d.textContent) === todayStr()) target = c;
    });
    if (!target) target = cards[0];
    if (target) openDay(target.getAttribute('data-day'), false);
  }
  // 全部逻辑绑定成功后才交给 CSS 收起正文：脚本中途报错时页面保持完整可读
  document.documentElement.classList.add('js');
})();
</script>
<script src="https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.min.js"
        onerror="document.documentElement.classList.add('no-map')"></script>
<script>
(function () {
  var DAYS = %%MAP_DAYS_JSON%%;
  if (!window.L || !DAYS.length) {
    document.documentElement.classList.add('no-map');
    return;
  }
  try {
    initMap();
  } catch (err) {
    document.documentElement.classList.add('no-map');  // 初始化挂了就退回纯列表，别留空白地图和死按钮
  }

  function initMap() {
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

  function overview() {  // 回到全览：清除选中并恢复全程视野
    Object.keys(layers).forEach(function (k) { setDim(k, 'normal'); });
    document.querySelectorAll('.day-card.active').forEach(function (el) { el.classList.remove('active'); });
    selected = null;
    if (all.length) map.fitBounds(all, { padding: [24, 24] });
  }

  function selectDay(idx, scrollCard, fromCard) {
    if (!fromCard && window.RB && window.RB.isMobile()) {  // 手机上「选中」= 展开那张卡
      window.RB.openDay(idx, scrollCard);  // 已选中的那天被收起后，再点折线也要能重新展开
      return;
    }
    if (selected === idx) {  // 再点一次取消选中 → 回全览；从卡片展开进来时选中已就位，不回全览
      if (fromCard) return;
      overview();
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
      if (window.RB && window.RB.isMobile()) return;  // 手机上点卡 = 展开/收起，交给手风琴脚本
      if (e.target.closest('details, a')) return;
      selectDay(card.getAttribute('data-day'), false);
    });
  });

  // 地图左上角常驻"全览"按钮：任何状态下一键回到全程视野
  var OverviewCtl = L.Control.extend({
    options: { position: 'topleft' },
    onAdd: function () {
      var btn = L.DomUtil.create('button', 'map-overview-btn');
      btn.type = 'button';
      btn.innerHTML = '⤢ 全览';
      L.DomEvent.disableClickPropagation(btn);
      btn.addEventListener('click', overview);
      return btn;
    }
  });
  map.addControl(new OverviewCtl());

  if (all.length) map.fitBounds(all, { padding: [24, 24] });

  // 供手风琴脚本回调：手机上展开哪张卡，地图就跟到那天
  if (window.RB) {
    window.RB.mapSelect = function (idx) { selectDay(idx, false, true); };
    window.RB.mapOverview = overview;
    var openCard = document.querySelector('.day-card.open');
    if (openCard && window.RB.isMobile()) {
      selectDay(openCard.getAttribute('data-day'), false, true);
    }
  }

  // 移动端：地图全屏 ↔ 行程列表 切换
  var tbtn = document.getElementById('viewToggle');
  if (tbtn) {
    tbtn.addEventListener('click', function () {
      var full = document.body.classList.toggle('map-full');
      tbtn.textContent = full ? '📋 看行程' : '🗺 看大图';
      setTimeout(function () { map.invalidateSize(); if (all.length && full) map.fitBounds(all, { padding: [24, 24] }); }, 80);
    });
  }
  }
})();

// 一键导航：按平台唤起高德 App（独立于地图脚本，地图加载失败也能用）
(function () {
  var ua = navigator.userAgent;
  var iOSLike = /iPhone|iPad|iPod/i.test(ua) || (/Macintosh/i.test(ua) && navigator.maxTouchPoints > 1);
  var platform = /MicroMessenger/i.test(ua) ? 'wechat'
    : iOSLike ? 'ios'
    : /Android|HarmonyOS|OpenHarmony/i.test(ua) ? 'android' : 'desktop';
  var HINTS = {
    wechat: '微信里打不开高德：点右上角 ··· 选「在浏览器打开」，再点导航',
    desktop: '用手机打开本页，点这里即可在高德 App 里导航',
    missing: '没有唤起高德地图？请确认手机已安装高德地图 App'
  };
  function hint(btn, key) {
    var el = btn.nextElementSibling;
    if (!el || !el.classList.contains('nav-hint')) {
      el = document.createElement('div');
      el.className = 'nav-hint';
      btn.parentNode.insertBefore(el, btn.nextSibling);
    }
    el.textContent = HINTS[key];
  }
  // 页面常跨夜挂着，日期在点击时现算，不能加载时算一次
  function todayStr() {
    var t = new Date(), pad = function (n) { return (n < 10 ? '0' : '') + n; };
    return t.getFullYear() + '-' + pad(t.getMonth() + 1) + '-' + pad(t.getDate());
  }
  function normDate(s) {
    var m = /(\\d{4})-(\\d{2})-(\\d{2})/.exec(s || '');
    return m ? m[0] : '';
  }
  function markLabels() {
    document.querySelectorAll('.nav-btn').forEach(function (btn) {
      var d = normDate(btn.getAttribute('data-date'));
      btn.textContent = (d && d === todayStr()) ? '🧭 高德导航今天' : '🧭 高德一键导航';
    });
  }
  markLabels();
  // 页面常在后台挂着，回到前台时重算「今天」，避免跨夜后文案过期
  document.addEventListener('visibilitychange', function () { if (!document.hidden) markLabels(); });

  document.querySelectorAll('.nav-btn').forEach(function (btn) {
    var date = normDate(btn.getAttribute('data-date'));
    btn.addEventListener('click', function (e) {
      e.preventDefault();
      e.stopPropagation();
      if (platform === 'wechat' || platform === 'desktop') return hint(btn, platform);
      // 当天/无日期：从当前位置出发；翻看其它日子：按行程起点，避免算出书外的路线
      var fixed = btn.getAttribute('data-' + platform + '-fixed');
      var useFixed = fixed && (!date || date !== todayStr());
      location.href = useFixed ? fixed : btn.getAttribute('data-' + platform);
      // 唤起成功页面会进后台；2 秒后仍在前台 = 多半没装高德
      setTimeout(function () { if (!document.hidden) hint(btn, 'missing'); }, 2000);
    });
  });
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

    global PREVIEW_MODE, ALT_RELEVANT
    PREVIEW_MODE = bool(meta.get("preview_mode"))
    # 全程无 1500m+ 海拔（东北/东南沿海等）→ 海拔信息无意义，整体隐藏
    all_elevs = [p.get("elevation_m") for d in itinerary.get("days", [])
                 for p in ([d.get("overnight")] + d.get("stops", [])) if p and p.get("elevation_m") is not None]
    ALT_RELEVANT = bool(all_elevs) and max(all_elevs) >= 1500

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
        "%%ELEV_SPAN%%": (f'    <span>最高海拔 <b>{summary["max_elevation_m"]:.0f}m</b></span>\n'
                          if ALT_RELEVANT and summary["max_elevation_m"] is not None else ""),
        "%%WARNING_COUNT%%": str(summary["warning_count"]),
        "%%HAS_PLAN_B%%": "有" if summary["has_plan_b"] else "无",
        "%%CHANGE_BLOCK%%": render_change_block(itinerary),
        "%%TRIP_CLAIMS%%": render_trip_claims_block(
            itinerary.get("claims"),
            day_claims_exist=any(d.get("claims") for d in itinerary.get("days", []))),
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
