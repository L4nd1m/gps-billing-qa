"""Synthetic world model for the GPS billing QA project.

EVERYTHING HERE IS FICTIONAL. The origin is a point in open ocean, chosen only
so that cos(latitude) != 1 (a forgotten cos term would then show up). The roads
and the campaign zone are invented.

Geometry is done in local metres (x = east, y = north) through an
equirectangular projection around the origin. Pings are stored as lat/lon.
"""
import math
from datetime import date, datetime, timedelta, timezone

# --- Projection -------------------------------------------------------------
EARTH_RADIUS_M = 6371008.8
ORIGIN_LAT = 35.0   # FICTIONAL origin, open ocean
ORIGIN_LON = 150.0
_COS_LAT0 = math.cos(math.radians(ORIGIN_LAT))


def to_xy(lat, lon):
    """Degrees -> local metres."""
    x = EARTH_RADIUS_M * math.radians(lon - ORIGIN_LON) * _COS_LAT0
    y = EARTH_RADIUS_M * math.radians(lat - ORIGIN_LAT)
    return x, y


def to_latlon(x, y):
    """Local metres -> degrees (exact inverse of to_xy)."""
    lat = ORIGIN_LAT + math.degrees(y / EARTH_RADIUS_M)
    lon = ORIGIN_LON + math.degrees(x / (EARTH_RADIUS_M * _COS_LAT0))
    return lat, lon


def haversine_m(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def chord_m(p, q):
    """Length of the straight line between two local-metre points, measured with
    haversine on their lat/lon -- what a GPS biller measures. (Flat-world metres
    overstate east-west lengths north of the origin: measured -0.334 m per 3 km
    at y = 1000, -0.994 m per 3 km at y = 3000.)"""
    return haversine_m(*to_latlon(*p), *to_latlon(*q))


# --- Business constants from the brief --------------------------------------
NOISE_OFFROAD_M = 100.0       # more than 100 m off road = noise
MAX_SPEED_KMH = 150.0         # above 150 km/h = noise
PING_INTERVAL_S = 10
MAX_GAP_S = 60.0              # R7: a line spanning MORE than 60 s is not billed
LOCAL_TZ = timezone(timedelta(hours=9))   # report day cut at 00:00 UTC+9

# --- Roads (metres) ---------------------------------------------------------
GRID_M = 1000.0
GRID_BLOCKS = 3               # roads at x, y = 0, 1000, 2000, 3000
ARC_SEGMENTS = 24


def arc_points():
    """Curved road: quarter circle, centre (3000, 0), radius 1000,
    from (2000, 0) to (3000, 1000), as a polyline."""
    cx, cy, r = 3000.0, 0.0, 1000.0
    pts = []
    for i in range(ARC_SEGMENTS + 1):
        th = math.radians(90.0 * i / ARC_SEGMENTS)
        pts.append((cx - r * math.cos(th), cy + r * math.sin(th)))
    pts[0], pts[-1] = (2000.0, 0.0), (3000.0, 1000.0)   # snap ends exactly
    return pts


def _build_roads():
    roads = []
    for k in range(GRID_BLOCKS + 1):
        c = k * GRID_M
        for j in range(GRID_BLOCKS):
            roads.append(((c, j * GRID_M), (c, (j + 1) * GRID_M)))   # vertical
            roads.append(((j * GRID_M, c), ((j + 1) * GRID_M, c)))   # horizontal
    roads.append(((0.0, 0.0), (1000.0, 1000.0)))                     # diagonal
    roads.append(((1000.0, 1000.0), (2000.0, 2000.0)))
    arc = arc_points()
    roads.extend(zip(arc, arc[1:]))                                  # curve
    return roads


ROADS = _build_roads()

# --- Campaign zone: concave L-shape (metres) --------------------------------
ZONE = [(500.0, 500.0), (2500.0, 500.0), (2500.0, 1500.0),
        (1500.0, 1500.0), (1500.0, 2500.0), (500.0, 2500.0)]


# --- Geometry ---------------------------------------------------------------
def point_segment_distance(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    if l2 == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / l2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def distance_to_road(x, y):
    return min(point_segment_distance(x, y, a[0], a[1], b[0], b[1]) for a, b in ROADS)


def point_in_polygon(x, y, poly=ZONE):
    """Ray casting. Points exactly on an edge are ambiguous (measure zero)."""
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            if x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
                inside = not inside
    return inside


def segment_edge_crossings(p, q, poly=ZONE):
    """Sorted parameters t in (0, 1) where segment p->q crosses a polygon edge.
    Parallel/collinear edges are skipped (callers use midpoint tests)."""
    px, py = p
    rx, ry = q[0] - px, q[1] - py
    ts = []
    n = len(poly)
    for i in range(n):
        ax, ay = poly[i]
        bx, by = poly[(i + 1) % n]
        sx, sy = bx - ax, by - ay
        denom = rx * sy - ry * sx
        if denom == 0:
            continue
        t = ((ax - px) * sy - (ay - py) * sx) / denom
        u = ((ax - px) * ry - (ay - py) * rx) / denom
        if 0 < t < 1 and 0 <= u <= 1:
            ts.append(t)
    return sorted(ts)


# --- Time -------------------------------------------------------------------
def parse_utc(s):
    return datetime.fromisoformat(s)          # accepts trailing 'Z' (3.11+)


def fmt_utc(dt, ms=False):
    return dt.astimezone(timezone.utc).isoformat(
        timespec="milliseconds" if ms else "seconds").replace("+00:00", "Z")


def local_date(epoch_s):
    return datetime.fromtimestamp(epoch_s, LOCAL_TZ).date().isoformat()


def day_cuts_between(t0, t1):
    """Epoch seconds of every 00:00 UTC+9 strictly between t0 and t1."""
    d = datetime.fromtimestamp(t0, LOCAL_TZ).date()
    cuts = []
    while True:
        d = d + timedelta(days=1)
        c = datetime(d.year, d.month, d.day, tzinfo=LOCAL_TZ).timestamp()
        if c >= t1:
            return cuts
        if c > t0:
            cuts.append(c)
