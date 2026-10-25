"""
Offline helper (run on a normal computer, not on the Pi; needs numpy + scipy).

Builds data/label_slots.json: every spot in the sea around Japan where a
label of up to 3 characters fits without touching the land. At runtime
render.py just picks the nearest free slots, so the Pi needs no numpy,
no scipy and no geometry maths beyond a few comparisons.

    python3 tools/make_label_slots.py
"""
import json
import os
import sys

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import render  # noqa: E402

KEEP = 3          # px of clear space between any label and the land
HALF_W, HALF_H = 16, 7   # half-size of the largest label box (3 CJK chars at 10px, plus padding)
MAX_OFFSHORE = 42        # don't offer slots further than this from the coast (keeps leaders short)
STEP = 4

W, H = render.W, render.H
gj = render.load_json("prefectures.geojson")
project = render.get_projector()
land = Image.new("1", (W, H), 0)
d = ImageDraw.Draw(land)
for f in gj["features"]:
    if f["properties"]["nam"] == "Okinawa Ken":
        continue
    g = f["geometry"]
    for poly in (g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]):
        d.polygon([project(lat, lon) for lon, lat in poly[0]], fill=1)

dist = ndimage.distance_transform_edt(~np.array(land, bool))
clear = ndimage.minimum_filter(dist, size=(2 * HALF_H + 1, 2 * HALF_W + 1), mode="constant", cval=0) > KEEP

# A rough centre line down the length of the country. Anything on its east/south
# side is the Pacific side (Pacific, Seto Inland Sea, East China Sea's eastern
# part); anything on its west/north side is the Sea of Japan side. Each slot is
# tagged so a prefecture's label can be kept on the sea that prefecture faces.
SPINE_LATLON = [(45.5, 143.0), (43.2, 142.8), (42.0, 141.8), (41.0, 140.8), (39.5, 140.8),
                (38.0, 140.3), (37.0, 139.5), (36.2, 138.2), (35.6, 137.2), (35.2, 136.3),
                (35.0, 135.3), (35.0, 134.3), (34.7, 133.0), (34.2, 131.8), (33.6, 131.0),
                (32.5, 130.7), (30.0, 130.5)]
spine = [project(la, lo) for la, lo in SPINE_LATLON]


def side_of(x, y):
    best = None
    for (x1, y1), (x2, y2) in zip(spine, spine[1:]):
        dx, dy = x2 - x1, y2 - y1
        t = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy)))
        d = (x - (x1 + t * dx)) ** 2 + (y - (y1 + t * dy)) ** 2
        if best is None or d < best[0]:
            best = (d, dx * (y - y1) - dy * (x - x1))
    return 1 if best[1] < 0 else -1       # 1 = Pacific side, -1 = Sea of Japan side


slots = []
for y in range(HALF_H, H - HALF_H + 1, STEP):
    for x in range(HALF_W, W - HALF_W + 1, STEP):
        if clear[y, x] and dist[y, x] <= MAX_OFFSHORE:
            slots.append([x, y, side_of(x, y)])

out = os.path.join(render.DATA_DIR, "label_slots.json")
with open(out, "w") as fh:
    json.dump(slots, fh, separators=(",", ":"))
print("wrote %d slots to %s (%d bytes)" % (len(slots), out, os.path.getsize(out)))
