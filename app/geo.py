"""Web Mercator projection for the aerial basemap.

The basemap rasters are axis-aligned in EPSG:3857, so lat/lng ↔ image pixel is a
pure affine map. This is the Python side of it; `static/js/map.js` has the same
formulas (the browser does the per-render projection). Keep the two in step.

"World space" for a location with a basemap is **level-0 image pixels**: (0,0) at
the raster's top-left, (width_px, height_px) at its bottom-right. Device
positions are stored as lat/lng and projected here or in the browser, so
replacing the imagery doesn't move any device.

No dependencies — this must stay importable in the offline SQLite dev setup.
"""

import logging
import math

R = 6378137.0   # Web Mercator sphere radius, as used by EPSG:3857

log = logging.getLogger(__name__)


def merc_x(lng: float) -> float:
    return math.radians(lng) * R


def merc_y(lat: float) -> float:
    # Clamped short of the poles: tan() blows up at ±90°, and a bad lat should
    # yield a far-off-map pixel rather than a ValueError.
    lat = max(-89.9, min(89.9, lat))
    return R * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


def inv_merc_x(x: float) -> float:
    return math.degrees(x / R)


def inv_merc_y(y: float) -> float:
    return math.degrees(2 * math.atan(math.exp(y / R)) - math.pi / 2)


def to_px(georef: dict, lat: float, lng: float) -> tuple[float, float]:
    """lat/lng → level-0 image pixel (x right, y DOWN — SVG's convention, which is
    why y is measured from ymax)."""
    b = georef['epsg3857_bbox']
    res_x = (b['xmax'] - b['xmin']) / georef['width_px']
    res_y = (b['ymax'] - b['ymin']) / georef['height_px']
    return ((merc_x(lng) - b['xmin']) / res_x,
            (b['ymax'] - merc_y(lat)) / res_y)


def from_px(georef: dict, x: float, y: float) -> tuple[float, float]:
    """Level-0 image pixel → (lat, lng). Inverse of to_px."""
    b = georef['epsg3857_bbox']
    res_x = (b['xmax'] - b['xmin']) / georef['width_px']
    res_y = (b['ymax'] - b['ymin']) / georef['height_px']
    return (inv_merc_y(b['ymax'] - y * res_y),
            inv_merc_x(b['xmin'] + x * res_x))


def view_px(georef: dict, view: dict) -> dict:
    """A lat/lng view window → its pixel rect {x0, y0, x1, y1} (x0/y0 = top-left)."""
    x0, y0 = to_px(georef, view['north'], view['west'])
    x1, y1 = to_px(georef, view['south'], view['east'])
    return {'x0': min(x0, x1), 'y0': min(y0, y1),
            'x1': max(x0, x1), 'y1': max(y0, y1)}


def valid_view(view) -> bool:
    """True if `view` is a usable lat/lng window (all four bounds present, numeric,
    in range, and non-degenerate)."""
    if not isinstance(view, dict):
        return False
    try:
        s, w, n, e = (float(view[k]) for k in ('south', 'west', 'north', 'east'))
    except (KeyError, TypeError, ValueError):
        return False
    return (-90 <= s < n <= 90) and (-180 <= w < e <= 180)


def fit_layout_to_view(positions: dict, georef: dict, view: dict,
                       inset: float = 0.06) -> dict:
    """One-time seed: map an existing logical x/y layout onto the basemap's view
    window so every device starts somewhere plausible on the photo instead of at
    (0,0) or in one pile.

    positions: {ip: {'x', 'y'}} in the old arbitrary world space.
    Returns {ip: (lat, lng)}.

    Scale is UNIFORM (the smaller of the two axis ratios) so the layout keeps its
    proportions — a stretched layout would be harder to recognise while dragging
    devices onto their real buildings. The result is centered in the window with
    `inset` of it left as margin.
    """
    if not positions:
        return {}

    xs = [p['x'] for p in positions.values()]
    ys = [p['y'] for p in positions.values()]
    src_w = max(xs) - min(xs)
    src_h = max(ys) - min(ys)
    src_cx = (max(xs) + min(xs)) / 2
    src_cy = (max(ys) + min(ys)) / 2

    r = view_px(georef, view)
    dst_w = (r['x1'] - r['x0']) * (1 - 2 * inset)
    dst_h = (r['y1'] - r['y0']) * (1 - 2 * inset)
    dst_cx = (r['x1'] + r['x0']) / 2
    dst_cy = (r['y1'] + r['y0']) / 2

    # A degenerate source axis (all devices on one line, or a single device) would
    # divide by zero; fall back to 1:1 on that axis.
    sx = dst_w / src_w if src_w > 0 else 1.0
    sy = dst_h / src_h if src_h > 0 else 1.0
    scale = min(sx, sy)

    out = {}
    for ip, p in positions.items():
        px = dst_cx + (p['x'] - src_cx) * scale
        py = dst_cy + (p['y'] - src_cy) * scale
        out[ip] = from_px(georef, px, py)
    return out
