"""
render.py — builds one 400x300 1-bit frame for the Waveshare 4.2" V2 e-paper
panel, showing Japan's prefecture outlines, station activity, and the most
recent JMA-relayed earthquake event.

No browser, no HTML/JS involved — this draws directly with Pillow, which is
the right approach for a Pi Zero: SPI panels only accept a raw pixel buffer,
and Chromium-class rendering is impractical on that hardware anyway.
"""

import json
import math
import logging
import os
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger("seismic-epaper.render")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
FONT_PATH = os.path.join(BASE_DIR, "fonts", "NotoSansJP-Bold.ttf")
# Noto Sans JP, pre-instanced to a static bold weight and subset down to
# JIS X 0208 levels 1+2 (~6,600 glyphs) instead of shipping the full ~9.5MB
# variable font with every CJK ideograph. Two separate wins on a Pi Zero:
# no per-render variable-font interpolation, and a much smaller file to
# read off a slow SD card. See fonts/README.md for how this was built and
# how to regenerate it if you ever hit a genuinely missing glyph.

W, H = 400, 300                     # Waveshare 4.2" V2 native resolution
MAP_BOX = (4, 4, 396, 296)          # full canvas, small margin — no side column anymore
INFO_BOX = (4, 4, 180)               # x0, y0, x1 — height is computed from actual content now
BOUNDS = (30.7, 45.8, 128.6, 146.2)  # lat0, lat1, lon0, lon1 — same frame as the web version

HISTORY_LIMIT = 8
API_URL = "https://api.p2pquake.net/v2/history?codes=551&limit=%d" % HISTORY_LIMIT

# The live feed reports prefecture names in Japanese (熊本県, 広島県...);
# stations.json and labels.json key off the short English names used
# throughout this project (Kumamoto, Hiroshima...). Without this mapping,
# matching silently fails and nothing highlights, ever.
PREF_JA_TO_EN = {
    "北海道": "Hokkaido", "青森県": "Aomori", "岩手県": "Iwate", "宮城県": "Miyagi",
    "秋田県": "Akita", "山形県": "Yamagata", "福島県": "Fukushima", "茨城県": "Ibaraki",
    "栃木県": "Tochigi", "群馬県": "Gunma", "埼玉県": "Saitama", "千葉県": "Chiba",
    "東京都": "Tokyo", "神奈川県": "Kanagawa", "新潟県": "Niigata", "富山県": "Toyama",
    "石川県": "Ishikawa", "福井県": "Fukui", "山梨県": "Yamanashi", "長野県": "Nagano",
    "岐阜県": "Gifu", "静岡県": "Shizuoka", "愛知県": "Aichi", "三重県": "Mie",
    "滋賀県": "Shiga", "京都府": "Kyoto", "大阪府": "Osaka", "兵庫県": "Hyogo",
    "奈良県": "Nara", "和歌山県": "Wakayama", "鳥取県": "Tottori", "島根県": "Shimane",
    "岡山県": "Okayama", "広島県": "Hiroshima", "山口県": "Yamaguchi", "徳島県": "Tokushima",
    "香川県": "Kagawa", "愛媛県": "Ehime", "高知県": "Kochi", "福岡県": "Fukuoka",
    "佐賀県": "Saga", "長崎県": "Nagasaki", "熊本県": "Kumamoto", "大分県": "Oita",
    "宮崎県": "Miyazaki", "鹿児島県": "Kagoshima", "沖縄県": "Okinawa",
}

# Short Japanese names (no 県/府/都/道 suffix, except Hokkaido which has none
# to drop) for on-map labels — noticeably more compact than the English
# romanization, which is what makes labeling all 46 at once feasible here.
EN_TO_JA_SHORT = {
    "Hokkaido": "北海道", "Aomori": "青森", "Iwate": "岩手", "Miyagi": "宮城",
    "Akita": "秋田", "Yamagata": "山形", "Fukushima": "福島", "Ibaraki": "茨城",
    "Tochigi": "栃木", "Gunma": "群馬", "Saitama": "埼玉", "Chiba": "千葉",
    "Tokyo": "東京", "Kanagawa": "神奈川", "Niigata": "新潟", "Toyama": "富山",
    "Ishikawa": "石川", "Fukui": "福井", "Yamanashi": "山梨", "Nagano": "長野",
    "Gifu": "岐阜", "Shizuoka": "静岡", "Aichi": "愛知", "Mie": "三重",
    "Shiga": "滋賀", "Kyoto": "京都", "Osaka": "大阪", "Hyogo": "兵庫",
    "Nara": "奈良", "Wakayama": "和歌山", "Tottori": "鳥取", "Shimane": "島根",
    "Okayama": "岡山", "Hiroshima": "広島", "Yamaguchi": "山口", "Tokushima": "徳島",
    "Kagawa": "香川", "Ehime": "愛媛", "Kochi": "高知", "Fukuoka": "福岡",
    "Saga": "佐賀", "Nagasaki": "長崎", "Kumamoto": "熊本", "Oita": "大分",
    "Miyazaki": "宮崎", "Kagoshima": "鹿児島",
}


# ---------- data loading (all cached at module level — loaded once per
# process, not once per frame, since main.py calls build_frame() repeatedly
# for the life of the program) ----------

_cache = {}


def load_json(name):
    if name not in _cache:
        with open(os.path.join(DATA_DIR, name), encoding="utf-8") as f:
            _cache[name] = json.load(f)
    return _cache[name]


def load_fonts():
    if "fonts" not in _cache:
        _cache["fonts"] = {
            "h1": ImageFont.truetype(FONT_PATH, 17),
            "body": ImageFont.truetype(FONT_PATH, 14),
            "small": ImageFont.truetype(FONT_PATH, 11),
            "tiny": ImageFont.truetype(FONT_PATH, 10),
            "micro": ImageFont.truetype(FONT_PATH, 9),
        }
    return _cache["fonts"]


def get_projector():
    if "projector" not in _cache:
        _cache["projector"] = make_projector(BOUNDS, MAP_BOX)
    return _cache["projector"]


# ---------- projection ----------

def make_projector(bounds, box):
    lat0, lat1, lon0, lon1 = bounds
    x0, y0, x1, y1 = box
    lat_mid = (lat0 + lat1) / 2
    cosf = math.cos(math.radians(lat_mid))
    world_w = (lon1 - lon0) * cosf
    world_h = lat1 - lat0
    avail_w, avail_h = x1 - x0, y1 - y0
    scale = min(avail_w / world_w, avail_h / world_h)
    off_x = x0 + (avail_w - world_w * scale) / 2
    off_y = y0 + (avail_h - world_h * scale) / 2

    def project(lat, lon):
        px = off_x + (lon - lon0) * cosf * scale
        py = off_y + (lat1 - lat) * scale
        return px, py

    return project


# ---------- drawing ----------

def draw_prefectures(draw, geojson, project):
    for feature in geojson["features"]:
        if feature["properties"]["nam"] == "Okinawa Ken":
            continue  # out of frame, same as the web version
        geom = feature["geometry"]
        polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
        for poly in polys:
            for ring in poly:
                pts = [project(lat, lon) for lon, lat in ring]
                draw.line(pts + [pts[0]], fill=0, width=1)


def draw_stations(draw, stations, project):
    cs = 2  # every station gets a small fixed-size X, marking its location only
    for name, pref, lat, lon in stations:
        x, y = project(lat, lon)
        draw.line([(x - cs, y - cs), (x + cs, y + cs)], fill=0)
        draw.line([(x - cs, y + cs), (x + cs, y - cs)], fill=0)


def ripple_count_from_scale(code):
    return max(1, math.ceil(code / 10))  # shindo 1 -> 1 ring, shindo 7 -> 7 rings


def draw_shindo_ripples(draw, project, epicenter_lat, epicenter_lon, max_scale, active_points):
    """One ring per shindo point, like ripples from where the quake actually
    happened — not one circle per station. The outer ring's radius reflects
    how far the felt area actually reached: the farthest affected
    prefecture from the epicenter, not a fixed size."""
    ex, ey = project(epicenter_lat, epicenter_lon)
    outer_r = 10.0
    for lat, lon in active_points:
        px, py = project(lat, lon)
        outer_r = max(outer_r, math.hypot(px - ex, py - ey))

    n = ripple_count_from_scale(max_scale)
    for i in range(1, n + 1):
        r = outer_r * i / n
        draw.ellipse([ex - r, ey - r, ex + r, ey + r], outline=0, width=1)
    draw.ellipse([ex - 2, ey - 2, ex + 2, ey + 2], fill=0)  # epicenter itself


LEFT_COL_X = 66          # left column: text ends here, grows further left into the margin
RIGHT_COL_X = W - 66     # right column: text starts here, grows further right into the margin
LEFT_COL_Y_BOTTOM = 292          # left column's bottom bound; its top depends on the info box's actual height
RIGHT_COL_Y_RANGE = (8, 260)    # right column now stops short, handing its bottom off to the row below
BOTTOM_ROW_Y = 286              # a third margin: the empty strip along the very bottom edge
BOTTOM_ROW_X_RANGE = (100, 328)  # between the two columns, clear of both


def pack_1d(natural_positions, y0, y1, min_gap):
    """Start from each label's actual (clamped) position, sorted, and only
    push apart the ones that would otherwise overlap — by the minimum
    needed, not by stretching everything across the full range. Two nearby
    prefectures stay visually near each other; a genuinely crowded cluster
    still spreads out because it has to."""
    n = len(natural_positions)
    if n == 0:
        return []
    pos = [min(max(p, y0), y1) for p in natural_positions]

    # forward pass: push later items down if they're too close to the one before
    for i in range(1, n):
        if pos[i] - pos[i - 1] < min_gap:
            pos[i] = pos[i - 1] + min_gap

    # if that pushed the last item past the bottom, pull the whole chain
    # back up from the end instead, so it still respects the bounds
    if pos[-1] > y1:
        pos[-1] = y1
        for i in range(n - 2, -1, -1):
            if pos[i + 1] - pos[i] < min_gap:
                pos[i] = pos[i + 1] - min_gap

    return pos


# ---------- coastal label placement ----------
# Labels sit in the sea right next to the coast, each on the free spot nearest
# its prefecture, instead of in straight columns at the screen edges.
# data/label_slots.json (built offline by tools/make_label_slots.py) lists
# every spot where a 3-character label fits without touching land, so the
# Pi only has to pick from a list and run a few cheap overlap checks.

LABEL_FONT = "tiny"      # 10px
LABEL_H = 12             # label box height incl. padding
LABEL_GAP = 2            # minimum clear space between label boxes
LEADER_COLOR = 0


def _rects_overlap(a, b, gap=0):
    return not (a[2] + gap < b[0] or b[2] + gap < a[0] or a[3] + gap < b[1] or b[3] + gap < a[1])


def _seg_hits_rect(p, q, rect):
    """Exact test: does the segment p-q touch rect? (Liang-Barsky clipping.)"""
    x0, y0 = p
    x1, y1 = q
    if (max(x0, x1) < rect[0] or min(x0, x1) > rect[2]
            or max(y0, y1) < rect[1] or min(y0, y1) > rect[3]):
        return False                                   # bounding boxes don't even meet
    dx, dy = x1 - x0, y1 - y0
    t0, t1 = 0.0, 1.0
    for pk, qk in ((-dx, x0 - rect[0]), (dx, rect[2] - x0), (-dy, y0 - rect[1]), (dy, rect[3] - y0)):
        if pk == 0:
            if qk < 0:
                return False
        else:
            t = qk / pk
            if pk < 0:
                if t > t1:
                    return False
                t0 = max(t0, t)
            else:
                if t < t0:
                    return False
                t1 = min(t1, t)
    return True


def _ccw(a, b, c):
    return (c[1] - a[1]) * (b[0] - a[0]) - (b[1] - a[1]) * (c[0] - a[0])


def _segs_cross(p1, p2, p3, p4):
    d1, d2 = _ccw(p3, p4, p1), _ccw(p3, p4, p2)
    d3, d4 = _ccw(p1, p2, p3), _ccw(p1, p2, p4)
    return (d1 > 0) != (d2 > 0) and (d3 > 0) != (d4 > 0)


def _leader_start(rect, target):
    """Where the leader line leaves the label box, on the side facing the target."""
    cx, cy = (rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2
    dx, dy = target[0] - cx, target[1] - cy
    hw, hh = (rect[2] - rect[0]) / 2, (rect[3] - rect[1]) / 2
    if dx == 0 and dy == 0:
        return (cx, cy)
    t = min(hw / abs(dx) if dx else 1e9, hh / abs(dy) if dy else 1e9)
    return (cx + dx * t, cy + dy * t)


def _nearest_slots(p, n=700):
    """The n label slots closest to a prefecture's anchor point, nearest first.
    Cached forever: there are only 46 anchors, so sorting happens at most 46
    times in the life of the process, never per earthquake."""
    cache = _cache.setdefault("near_slots", {})
    key = (round(p[0], 1), round(p[1], 1))
    if key not in cache:
        slots = load_json("label_slots.json")
        order = sorted(slots, key=lambda s: (s[0] - p[0]) ** 2 + (s[1] - p[1]) ** 2)
        cache[key] = [(s[0], s[1], ((s[0] - p[0]) ** 2 + (s[1] - p[1]) ** 2) ** 0.5, s[2])
                      for s in order[:n]]
    return cache[key]


# Which sea each prefecture faces: 1 = Pacific side (Pacific, Seto Inland Sea),
# -1 = Sea of Japan side, 0 = both / either. Nearest-coast alone is ambiguous
# for wide or landlocked prefectures (Fukushima is about as far from the Sea of
# Japan as from the Pacific), so the label is kept on the sea people think of
# the prefecture as facing. A slot on the other side is allowed, but costs 3x.
SEA_SIDE = {
    "Hokkaido": 0, "Aomori": 0, "Iwate": 1, "Miyagi": 1, "Akita": -1, "Yamagata": -1,
    "Fukushima": 1, "Ibaraki": 1, "Tochigi": 1, "Gunma": -1, "Saitama": 1, "Chiba": 1,
    "Tokyo": 1, "Kanagawa": 1, "Niigata": -1, "Toyama": -1, "Ishikawa": -1, "Fukui": -1,
    "Yamanashi": 1, "Nagano": -1, "Gifu": 1, "Shizuoka": 1, "Aichi": 1, "Mie": 1,
    "Shiga": -1, "Kyoto": -1, "Osaka": 0, "Hyogo": 0, "Nara": 1, "Wakayama": 1,
    "Tottori": -1, "Shimane": -1, "Okayama": 0, "Hiroshima": 0, "Yamaguchi": 0,
    "Tokushima": 1, "Kagawa": 0, "Ehime": 0, "Kochi": 1, "Fukuoka": -1, "Saga": -1,
    "Nagasaki": -1, "Kumamoto": -1, "Oita": 1, "Miyazaki": 1, "Kagoshima": 0,
}
WRONG_SEA_FACTOR = 3.0
CROSSING_PENALTY = 40.0


def _slot_candidates(it):
    """Candidate slots as (cost, x, y, dist), cheapest first. Cost is the
    leader length, multiplied for slots on the sea the prefecture doesn't face."""
    slots = _nearest_slots(it["p"])
    side = it.get("side", 0)
    if not side:
        return [(d, x, y, d) for x, y, d, _s in slots]
    out = [(d * (1.0 if sd == side else WRONG_SEA_FACTOR), x, y, d) for x, y, d, sd in slots]
    out.sort()
    return out


def _place_in_order(items, font, blocked):
    """Greedy: each label takes the cheapest free sea spot near its prefecture."""
    placed = []
    for it in items:
        px, py = it["p"]
        half_w = int(font.getlength(it["text"]) / 2) + 2
        half_h = LABEL_H // 2
        best, best_cost = None, 1e18
        for cost0, sx, sy, _d in _slot_candidates(it):
            if cost0 >= best_cost:
                break                               # sorted: nothing later can win
            rect = (sx - half_w, sy - half_h, sx + half_w, sy + half_h)
            if any(_rects_overlap(rect, b) for b in blocked):
                continue
            if any(_rects_overlap(rect, o["rect"], LABEL_GAP) for o in placed):
                continue
            start = _leader_start(rect, (px, py))
            if any(_seg_hits_rect(start, (px, py), b) for b in blocked):
                continue
            if any(_seg_hits_rect(start, (px, py), o["rect"]) for o in placed):
                continue
            if any(_seg_hits_rect(o["start"], o["p"], rect) for o in placed):
                continue
            cost = cost0 + CROSSING_PENALTY * sum(
                _segs_cross(start, (px, py), o["start"], o["p"]) for o in placed)
            if cost < best_cost:
                best, best_cost = (rect, (sx, sy), start), cost
        if best is None:
            return None
        placed.append({"text": it["text"], "rect": best[0], "center": best[1],
                       "start": best[2], "p": (px, py), "half_w": half_w})
    return placed


def _hard_ok(placed, blocked):
    """No overlaps, nothing on the info box / badge, no leader running through a label."""
    for i, a in enumerate(placed):
        if any(_rects_overlap(a["rect"], b) for b in blocked):
            return False
        if any(_seg_hits_rect(a["start"], a["p"], b) for b in blocked):
            return False
        for b in placed[i + 1:]:
            if _rects_overlap(a["rect"], b["rect"], LABEL_GAP):
                return False
            if _seg_hits_rect(a["start"], a["p"], b["rect"]) or _seg_hits_rect(b["start"], b["p"], a["rect"]):
                return False
    return True


def _hard_ok_pair(placed, i, j, blocked):
    """Same checks as _hard_ok, but only for the two labels that just moved."""
    for k in (i, j):
        a = placed[k]
        if any(_rects_overlap(a["rect"], b) for b in blocked):
            return False
        if any(_seg_hits_rect(a["start"], a["p"], b) for b in blocked):
            return False
        for m, b in enumerate(placed):
            if m == k or (k == j and m == i):
                continue
            if _rects_overlap(a["rect"], b["rect"], LABEL_GAP):
                return False
            if _seg_hits_rect(a["start"], a["p"], b["rect"]) or _seg_hits_rect(b["start"], b["p"], a["rect"]):
                return False
    return True


def _n_cross(placed):
    n = 0
    for i, a in enumerate(placed):
        for b in placed[i + 1:]:
            if _segs_cross(a["start"], a["p"], b["start"], b["p"]):
                n += 1
    return n


def _refit(o, center):
    hw, hh = o["half_w"], LABEL_H // 2
    rect = (center[0] - hw, center[1] - hh, center[0] + hw, center[1] + hh)
    return dict(o, center=center, rect=rect, start=_leader_start(rect, o["p"]))


def _leader_len(o):
    return ((o["start"][0] - o["p"][0]) ** 2 + (o["start"][1] - o["p"][1]) ** 2) ** 0.5


def _swap_pass(placed, blocked, wants_swap, max_growth):
    """Try swapping label positions for pairs where wants_swap(a, b) is true.
    A swap is kept only if the layout stays valid, has no more crossings, and
    the two leader lines together grow by at most max_growth px — so a swap
    can never send a label to the far side of the country."""
    placed = list(placed)
    current = _n_cross(placed)
    for i in range(len(placed)):
        for j in range(i + 1, len(placed)):
            a, b = placed[i], placed[j]
            if not wants_swap(a, b):
                continue
            trial = list(placed)
            trial[i], trial[j] = _refit(a, b["center"]), _refit(b, a["center"])
            if _leader_len(trial[i]) + _leader_len(trial[j]) > _leader_len(a) + _leader_len(b) + max_growth:
                continue
            if _hard_ok_pair(trial, i, j, blocked):
                n = _n_cross(trial)
                if n <= current:
                    placed, current = trial, n
    return placed


def _cross(a, b):
    return _segs_cross(a["start"], a["p"], b["start"], b["p"])


def _out_of_order(a, b):
    """Neighbouring labels should keep their prefectures' relative positions:
    if two prefectures are mostly side by side, the left one's label should be
    on the left; if mostly one above the other, the northern one's label goes
    above. Only labels close to each other are compared."""
    (ax, ay), (bx, by) = a["center"], b["center"]
    if abs(ax - bx) > 50 or abs(ay - by) > 50:
        return False
    dxl, dyl = ax - bx, ay - by
    dxa, dya = a["p"][0] - b["p"][0], a["p"][1] - b["p"][1]
    axes = [(dxl, dxa), (dyl, dya)] if abs(dxa) >= abs(dya) else [(dyl, dya), (dxl, dxa)]
    for lab, anc in axes:
        if abs(lab) > 3 and abs(anc) > 2:
            return lab * anc < 0
    return False


def _tidy(placed, blocked):
    """First swap crossing leaders apart (shortens the total length too), then
    put stacked labels in geographic order."""
    for _ in range(4):
        before = _n_cross(placed)
        placed = _swap_pass(placed, blocked, _cross, -0.01)   # must get shorter
        if before == 0 or _n_cross(placed) == before:
            break
    for _ in range(3):
        before = [o["center"] for o in placed]
        placed = _swap_pass(placed, blocked, _out_of_order, 45)
        if before == [o["center"] for o in placed]:
            break
    return placed


_PLACEMENT_CACHE_SIZE = 8


def place_labels_coastal(items, font, blocked):
    """items: [{"text", "p", "side"}]. Returns a placement list, or None if the
    labels can't all be placed (the caller then falls back to the columns).

    Each label takes the free sea spot nearest its prefecture. The first
    handling order is almost always crossing-free and is used straight away;
    further orders are only tried when it isn't. The result is cached, so the
    same event redrawn (offline badge appearing, recovery, maintenance) costs
    nothing."""
    key = (tuple(sorted((it["text"], it["p"]) for it in items)), tuple(blocked))
    cache = _cache.setdefault("placements", {})
    if key in cache:
        return cache[key]

    near = lambda it: _slot_candidates(it)[0][0]
    orders = (
        lambda: sorted(items, key=near),                    # coastal prefectures claim their spots first
        lambda: sorted(items, key=lambda it: it["p"][1]),   # north to south
        lambda: sorted(items, key=lambda it: -near(it)),    # inland ones first
    )
    best, best_score = None, None
    for make_order in orders:
        placed = _place_in_order(make_order(), font, blocked)
        if placed is None:
            continue
        placed = _tidy(placed, blocked)
        length = sum(_leader_len(o) for o in placed)
        score = (_n_cross(placed), length)
        if best_score is None or score < best_score:
            best, best_score = placed, score
        if score[0] == 0:
            break                                            # crossing-free: good enough, stop here
    if len(cache) >= _PLACEMENT_CACHE_SIZE:
        cache.pop(next(iter(cache)))
    cache[key] = best
    return best


def draw_prefecture_labels(draw, fonts, labels, project, active_prefs, left_y_range,
                           right_y_range=RIGHT_COL_Y_RANGE, bottom_x_range=BOTTOM_ROW_X_RANGE,
                           blocked=()):
    items = []
    for name, clat, clon, _llat, _llon in labels:
        if name in active_prefs:
            items.append({"text": EN_TO_JA_SHORT.get(name, name), "p": project(clat, clon),
                          "side": SEA_SIDE.get(name, 0)})
    if not items:
        return
    font = fonts[LABEL_FONT]
    placed = place_labels_coastal(items, font, list(blocked))
    if placed is None:
        log.warning("coastal label placement failed, using columns")
        return draw_prefecture_labels_columns(draw, fonts, labels, project, active_prefs,
                                              left_y_range, right_y_range, bottom_x_range)
    for o in placed:
        draw.line([o["start"], o["p"]], fill=LEADER_COLOR, width=1)
    for o in placed:                                  # white backing hides ripple lines under the text
        draw.rectangle(o["rect"], fill=1)
        draw.text(o["center"], o["text"], font=font, fill=0, anchor="mm")


def draw_prefecture_labels_columns(draw, fonts, labels, project, active_prefs, left_y_range,
                           right_y_range=RIGHT_COL_Y_RANGE, bottom_x_range=BOTTOM_ROW_X_RANGE):
    """Only the prefecture(s) the current event actually affects get a
    label — same as the original design, just with the Japanese short name
    (北海道, 熊本, ...) instead of the English romanization, since Japanese
    takes less space. Still placed in the margins (below the status box,
    the right side, and along the bottom) rather than hugging the
    coastline, since that's what avoided landing on top of the map."""
    font = fonts["micro"]
    pts = []
    for name, clat, clon, _llat, _llon in labels:
        if name not in active_prefs:
            continue
        p = project(clat, clon)
        pts.append({"text": EN_TO_JA_SHORT.get(name, name), "p": p})

    left = sorted([it for it in pts if it["p"][0] < W / 2], key=lambda it: it["p"][1])
    right = sorted([it for it in pts if it["p"][0] >= W / 2], key=lambda it: it["p"][1])

    # The left column's height depends on how tall the info box ended up
    # (a short recent-log gives it more room) — a plain geographic split
    # can still leave more in the left column than fits. Rebalance by
    # capacity: move the left entries sitting closest to the midline (the
    # weakest "left" calls) over to the right column.
    min_spacing = 10
    left_capacity = max(1, int((left_y_range[1] - left_y_range[0]) / min_spacing))
    if len(left) > left_capacity:
        left.sort(key=lambda it: -it["p"][0])  # closest to the midline first
        overflow = left[:len(left) - left_capacity]
        left = left[len(left) - left_capacity:]
        right = sorted(right + overflow, key=lambda it: it["p"][1])
    left.sort(key=lambda it: it["p"][1])

    # The right column is still the most crowded of the two. Peel its
    # southernmost entries off into a third pool along the bottom edge —
    # they're already near the bottom of the map, so this is a short,
    # natural-looking leader line rather than a long detour.
    bottom_capacity = 8
    right.sort(key=lambda it: it["p"][1])
    bottom = right[-bottom_capacity:] if len(right) > bottom_capacity else []
    right = right[:-bottom_capacity] if bottom else right
    bottom.sort(key=lambda it: it["p"][0])

    def place(items, col_x, y_range, side):
        y0, y1 = y_range
        positions = pack_1d([it["p"][1] for it in items], y0, y1, min_gap=13)
        for it, ly in zip(items, positions):
            elbow_x = col_x + 14 if side == "left" else col_x - 14
            p = it["p"]
            draw.line([(col_x, ly), (elbow_x, ly), p], fill=0, width=1)
            anchor = "rm" if side == "left" else "lm"
            draw.text((col_x, ly), it["text"], font=font, fill=0, anchor=anchor)

    place(left, LEFT_COL_X, left_y_range, "left")
    place(right, RIGHT_COL_X, right_y_range, "right")

    x0, x1 = bottom_x_range
    positions = pack_1d([it["p"][0] for it in bottom], x0, x1, min_gap=32)
    for it, lx in zip(bottom, positions):
        p = it["p"]
        elbow_y = BOTTOM_ROW_Y - 10
        draw.line([(lx, BOTTOM_ROW_Y), (lx, elbow_y), p], fill=0, width=1)
        draw.text((lx, BOTTOM_ROW_Y), it["text"], font=font, fill=0, anchor="ma")


def format_time(t):
    """JMA's origin time is only ever precise to the minute — every real
    timestamp this feed has ever returned ends in ':00'. Displaying
    seconds that never carry information just costs width for nothing."""
    if not t:
        return t
    return t.rsplit(":", 1)[0]


def wrap_cjk(text, max_chars):
    return [text[i:i + max_chars] for i in range(0, len(text), max_chars)] or [text]


def scale_label(code):
    table = [(70, "7"), (60, "6+"), (55, "6-"), (50, "5+"), (45, "5-"),
             (40, "4"), (30, "3"), (20, "2"), (10, "1")]
    for th, lab in table:
        if code >= th:
            return lab
    return "0"


def compute_info_box_height(events, pad=6):
    """How tall the info box actually needs to be for this event — no
    reason to reserve fixed space for a 3rd recent-log line when there are
    only one or two to show."""
    if not events:
        return pad * 2 + 16

    latest = events[0]
    region = latest["earthquake"]["hypocenter"]["name"] or "Unknown"
    n_region_lines = len(wrap_cjk(region, 11)[:2])
    n_recent = len(events[1:3])

    h = pad  # top padding
    h += n_region_lines * 18
    h += 16   # mag/shindo/depth line
    h += 15   # time line
    if n_recent:
        h += 4    # gap before divider
        h += n_recent * 13
    h += pad  # bottom padding
    return h


def draw_info_box(draw, fonts, events, box):
    x0, y0, x1 = box
    y1 = y0 + compute_info_box_height(events)
    # clear whatever map content sits under the box, then frame it, so the
    # status readout is always legible regardless of what's underneath
    draw.rectangle([x0, y0, x1, y1], fill=1, outline=None)
    draw.rectangle([x0, y0, x1, y1], outline=0, width=1)

    pad = 6
    x, y = x0 + pad, y0 + pad
    right = x1 - pad

    if not events:
        draw.text((x, y), "No data", font=fonts["body"], fill=0)
        return y1

    latest = events[0]
    eq = latest["earthquake"]
    region = eq["hypocenter"]["name"] or "Unknown"
    for line in wrap_cjk(region, 11)[:2]:
        draw.text((x, y), line, font=fonts["body"], fill=0)
        y += 18

    mag = eq["hypocenter"].get("magnitude")
    depth = eq["hypocenter"].get("depth")
    scale = scale_label(eq.get("maxScale", 0))
    mag_s = f"M{mag:.1f}" if mag is not None else "M—"
    depth_s = f"{depth}km" if depth is not None else "—"
    draw.text((x, y), f"{mag_s}  Shindo {scale}  {depth_s}", font=fonts["small"], fill=0)
    y += 16

    draw.text((x, y), format_time(eq.get("time", "")), font=fonts["tiny"], fill=0)
    y += 15

    recent = events[1:3]
    if recent:
        draw.line([(x, y), (right, y)], fill=0, width=1)
        y += 4
        for ev in recent:
            e = ev["earthquake"]
            name = (e["hypocenter"]["name"] or "?")[:10]
            s = scale_label(e.get("maxScale", 0))
            draw.text((x, y), f"{name} S{s}", font=fonts["tiny"], fill=0)
            y += 13

    return y1


def warm_up():
    """Do every one-time cost now (fonts, map, label slots, nearest-slot lists for
    all 47 prefectures) so the first real event — which may be a big one touching
    many prefectures — is drawn at full speed instead of paying for them then."""
    build_frame([])
    project = get_projector()
    for name, clat, clon, _llat, _llon in load_json("labels.json"):
        _nearest_slots(project(clat, clon))


def get_label_centroid():
    if "label_centroid" not in _cache:
        labels = load_json("labels.json")
        _cache["label_centroid"] = {name: (clat, clon) for name, clat, clon, _l, _o in labels}
    return _cache["label_centroid"]


def get_base_map():
    """Prefecture outlines and station markers never change between frames —
    render them once and reuse a copy, instead of redrawing several thousand
    line segments from the geojson on every single event."""
    if "base_map" not in _cache:
        img = Image.new("1", (W, H), 1)
        draw = ImageDraw.Draw(img)
        project = get_projector()
        draw_prefectures(draw, load_json("prefectures.geojson"), project)
        draw_stations(draw, load_json("stations.json"), project)
        _cache["base_map"] = img
    return _cache["base_map"].copy()


# ---------- top-level build ----------

def build_frame(events, net=None):
    """events: list from the p2pquake /history?codes=551 response, newest first."""
    img = get_base_map()
    draw = ImageDraw.Draw(img)
    fonts = load_fonts()

    labels = load_json("labels.json")
    project = get_projector()

    label_centroid = get_label_centroid()

    active_prefs = set()
    lat = lon = None
    max_scale = 0
    if events:
        latest = events[0]
        eq = latest["earthquake"]
        lat, lon = eq["hypocenter"].get("latitude"), eq["hypocenter"].get("longitude")
        max_scale = eq.get("maxScale", 0) or 0
        for pt in latest.get("points") or []:
            pref_ja = pt.get("pref")
            if not pref_ja:
                continue
            pref_en = PREF_JA_TO_EN.get(pref_ja)
            if not pref_en:
                continue  # unrecognized pref string — skip rather than mismatch silently
            active_prefs.add(pref_en)

    # The info box is only as tall as it needs to be (see
    # compute_info_box_height) — the left label column starts right below
    # wherever that actually ends up, reclaiming whatever space a short
    # recent-log freed rather than leaving it blank.
    info_box_bottom = INFO_BOX[1] + compute_info_box_height(events)
    left_y_range = (info_box_bottom + 8, LEFT_COL_Y_BOTTOM)

    right_range, bottom_range = RIGHT_COL_Y_RANGE, BOTTOM_ROW_X_RANGE
    net_box = None
    if net is not None:
        net_box = compute_net_box(net)
        right_range = (RIGHT_COL_Y_RANGE[0], min(RIGHT_COL_Y_RANGE[1], net_box[1] - 8))
        bottom_range = (BOTTOM_ROW_X_RANGE[0], min(BOTTOM_ROW_X_RANGE[1], net_box[0] - 22))
    # Ripples first, so the labels (which carry a white backing) stay readable on top of them.
    if lat is not None and lon is not None and max_scale > 0:
        active_points = [label_centroid[p] for p in active_prefs if p in label_centroid]
        draw_shindo_ripples(draw, project, lat, lon, max_scale, active_points)

    blocked = [(INFO_BOX[0] - 3, INFO_BOX[1] - 3, INFO_BOX[2] + 3, info_box_bottom + 3)]
    if net_box is not None:
        blocked.append((net_box[0] - 3, net_box[1] - 3, net_box[2] + 3, net_box[3] + 3))
    draw_prefecture_labels(draw, fonts, labels, project, active_prefs, left_y_range,
                           right_range, bottom_range, blocked)

    draw_info_box(draw, fonts, events, INFO_BOX)
    if net_box is not None:
        draw_net_box(draw, fonts, net, net_box)

    return img


# ---------- offline badge (bottom-right corner of the normal screen) ----------

NET_BOX_W = 122
NET_MAX_ROWS = 3


def _sorted_saved(net):
    """Connected first, then in range, then the rest (original order kept within each group)."""
    active = net.get("active_ssid") if net.get("wifi_connected") else None
    return sorted(net.get("saved") or [],
                  key=lambda sv: 0 if sv[0] == active else (1 if sv[1] else 2))


def compute_net_box(net):
    """(x0, y0, x1, y1) of the offline badge, bottom-right corner."""
    rows = min(len(net.get("saved") or []), NET_MAX_ROWS) or 1
    more = 1 if len(net.get("saved") or []) > NET_MAX_ROWS else 0
    h = 4 + 13 + (rows + more) * 12 + 2
    return (W - 4 - NET_BOX_W, H - 4 - h, W - 4, H - 4)


def draw_net_box(draw, fonts, net, box):
    x0, y0, x1, y1 = box
    draw.rectangle([x0, y0, x1, y1], fill=1, outline=0)
    ft = fonts["tiny"]
    title = "NO INTERNET" if net.get("wifi_connected") else "NO NETWORK"
    draw.text((x0 + 5, y0 + 3), title, font=fonts["small"], fill=0)
    y = y0 + 17
    saved = _sorted_saved(net)
    if not saved:
        msg = "(unavailable)" if net.get("error") else "(no saved networks)"
        draw.text((x0 + 5, y), msg, font=ft, fill=0)
    for ssid, visible in saved[:NET_MAX_ROWS]:
        active = net.get("wifi_connected") and ssid == net.get("active_ssid")
        mx, my = x0 + 5, y + 3
        if active or visible:
            draw.rectangle([mx, my, mx + 5, my + 5], fill=0)
        else:
            draw.rectangle([mx, my, mx + 5, my + 5], fill=1, outline=0)
        draw.text((x0 + 15, y), _fit(ft, safe_text(ssid), NET_BOX_W - 20), font=ft, fill=0)
        y += 12
    if len(saved) > NET_MAX_ROWS:
        draw.text((x0 + 15, y), "+%d more" % (len(saved) - NET_MAX_ROWS), font=ft, fill=0)
        y += 12


# ---------- offline screen (fallback when there is no event to show) ----------
# Shown when the Pi has had no working connection for a while. Its job is to
# answer, from across the room and without SSH: am I connected, to what, and
# which saved networks does this Pi know about.

_QUOTE_FIXES = {"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
                "\u2013": "-", "\u2014": "-", "\u00a0": " "}


def get_glyph_set():
    if "glyphs" not in _cache:
        try:
            with open(os.path.join(DATA_DIR, "font_charset.txt"), encoding="utf-8") as f:
                _cache["glyphs"] = frozenset(f.read())
        except OSError:
            _cache["glyphs"] = None
    return _cache["glyphs"]


def safe_text(text):
    """Network names are arbitrary user text. The font is a subset, so swap
    typographic quotes for plain ones and any glyph still missing for '?'
    rather than drawing an empty box."""
    text = "".join(_QUOTE_FIXES.get(c, c) for c in (text or ""))
    glyphs = get_glyph_set()
    if glyphs is None:
        return text
    return "".join(c if (c in glyphs or c == " ") else "?" for c in text)


def _fit(font, text, max_w):
    if font.getlength(text) <= max_w:
        return text
    while text and font.getlength(text + "...") > max_w:
        text = text[:-1]
    return text + "..."


def build_offline_frame(info, last_event=None):
    img = Image.new("1", (W, H), 1)
    d = ImageDraw.Draw(img)
    f = load_fonts()
    M = 10

    d.text((M, 8), "NO NETWORK", font=f["h1"], fill=0)
    y = 32
    if info.get("wifi_connected"):
        ssid = safe_text(info.get("active_ssid") or "?")
        d.text((M, y), _fit(f["small"], "Wi-Fi: %s  %s" % (ssid, info.get("ip", "")), W - 2 * M),
               font=f["small"], fill=0)
        d.text((M, y + 14), "Connected, but no internet access", font=f["small"], fill=0)
    else:
        st = info.get("wifi_state", "")
        head = "Wi-Fi: connecting..." if st in ("connecting", "connecting (getting IP configuration)") \
            else "Wi-Fi: not connected"
        d.text((M, y), head, font=f["small"], fill=0)
        d.text((M, y + 14), "Looking for a saved network", font=f["small"], fill=0)
    d.text((M, y + 28), safe_text("SSH: %s.local" % info.get("hostname", "")),
           font=f["small"], fill=0)

    y = 82
    d.line([(M, y), (W - M, y)], fill=0)
    d.text((M, y + 4), "SAVED NETWORKS", font=f["tiny"], fill=0)
    y += 20

    footer_top = H - 38 if last_event else H - 6
    step = 14
    saved = _sorted_saved(info)
    max_rows = max(1, (footer_top - y) // step)
    if not saved:
        msg = "(unavailable: %s)" % info["error"] if info.get("error") else "(none found)"
        d.text((M, y), msg, font=f["small"], fill=0)
    else:
        shown = saved if len(saved) <= max_rows else saved[:max_rows - 1]
        for ssid, visible in shown:
            active = info.get("wifi_connected") and ssid == info.get("active_ssid")
            status = "connected" if active else ("in range" if visible else "not seen")
            sw = f["small"].getlength(status)
            d.text((W - M, y), status, font=f["small"], fill=0, anchor="ra")
            d.text((M, y), _fit(f["small"], safe_text(ssid), W - 2 * M - sw - 10),
                   font=f["small"], fill=0)
            y += step
        if len(shown) < len(saved):
            d.text((M, y), "+%d more" % (len(saved) - len(shown)), font=f["small"], fill=0)

    if last_event:
        d.line([(M, footer_top), (W - M, footer_top)], fill=0)
        eq = last_event
        name = (eq.get("hypocenter") or {}).get("name") or "?"
        mag = (eq.get("hypocenter") or {}).get("magnitude")
        d.text((M, footer_top + 3), "Last event (not live):", font=f["tiny"], fill=0)
        line = "%s  M%s  %s" % (name, mag if mag is not None and mag >= 0 else "-",
                                format_time(eq.get("time", "")))
        d.text((M, footer_top + 15), _fit(f["small"], line, W - 2 * M), font=f["small"], fill=0)
    return img


def _error_frame(events):
    """Last resort when an event can't be drawn at all: still tell the person
    that something happened, with whatever fields can be read safely."""
    img = Image.new("1", (W, H), 1)
    d = ImageDraw.Draw(img)
    f = load_fonts()
    d.text((10, 10), "NEW EARTHQUAKE REPORT", font=f["h1"], fill=0)
    d.text((10, 36), "Details could not be displayed.", font=f["small"], fill=0)
    try:
        eq = events[0]["earthquake"]
        d.text((10, 64), safe_text(str((eq.get("hypocenter") or {}).get("name") or "")), font=f["body"], fill=0)
        d.text((10, 86), safe_text(str(eq.get("time") or "")), font=f["small"], fill=0)
        d.text((10, 104), "Max intensity: %s" % scale_label(int(eq.get("maxScale") or 0)), font=f["small"], fill=0)
    except Exception:
        pass
    d.text((10, 130), "Check jma.go.jp or NHK for the latest information.", font=f["small"], fill=0)
    return img


def build_frame_safe(events, net=None):
    """build_frame with a safety net, so a malformed or unexpected event can never
    leave the panel showing stale information during an emergency:
    full frame -> frame without per-prefecture detail -> plain text notice."""
    try:
        return build_frame(events, net)
    except Exception:
        log.exception("build_frame failed; retrying without per-prefecture detail")
    try:
        slim = [dict(e, points=[]) for e in events if isinstance(e, dict)]
        return build_frame(slim, net)
    except Exception:
        log.exception("simplified frame failed too; showing text notice")
    return _error_frame(events)


def _make_session():
    """A pooled, retrying session — reused across the process's lifetime so
    we're not paying a fresh TCP+TLS handshake (real CPU cost on this
    hardware) for every single fetch, and so transient network blips (a
    dropped packet, a momentary DNS hiccup) resolve themselves without
    every caller needing its own retry logic."""
    session = requests.Session()
    retry = Retry(
        total=3, connect=3, read=3,
        backoff_factor=1.5,  # ~1.5s, 3s, 6s between attempts
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


_session = _make_session()


def fetch_events():
    resp = _session.get(API_URL, timeout=15)
    resp.raise_for_status()
    return resp.json()


API_URL_LATEST = "https://api.p2pquake.net/v2/history?codes=551&limit=1"


def fetch_latest_id():
    """Cheap change-detector for the fallback poll: one event instead of
    eight, so a quiet minute costs a fraction of the bandwidth, TLS and
    JSON-parsing time on a Pi Zero. Returns None if the feed is empty."""
    resp = _session.get(API_URL_LATEST, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return data[0].get("id") if data else None


if __name__ == "__main__":
    # standalone test: renders a PNG you can inspect without any e-paper hardware
    evs = fetch_events()
    frame = build_frame(evs)
    frame.save(os.path.join(BASE_DIR, "preview.png"))
    print("wrote preview.png")
