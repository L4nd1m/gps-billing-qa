"""Switchable re-implementation of the legacy calculation, for root-cause work.

billable_with(pings, fixes) -> {(driver_id, day): metres}

`fixes` is a set of rule numbers to switch ON. With fixes = set() this must
reproduce gpsqa.legacy EXACTLY (proved by gpsqa.attribution). It is written
from scratch: nothing is imported from legacy.py. Shared with legacy: only the
road map (world.distance_to_road) and the zone config (world.ZONE).

  fix 1  process pings in timestamp order (stable sort, so the first-arrived
         copy of a duplicate still wins)                 -- removes BUG 1
  fix 2  no abs(): a ping older than the anchor is dropped, anchor unchanged
                                                         -- removes BUG 2
  fix 3  speed check BEFORE moving the anchor; a too-fast ping is dropped and
         the anchor stays on the last accepted ping      -- removes BUG 3
  fix 4  cut each segment at the zone edge; bill only the inside part
                                                         -- removes BUG 4
  fix 5  day = 00:00 UTC+9; a segment crossing the cut is split by time
                                                         -- removes BUG 5
"""
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from . import world as W

R_M = 6371000.0
MAX_KMH = 150.0
OFFROAD_M = 100.0
UTC9 = timezone(timedelta(hours=9))
ZONE_DEG = [W.to_latlon(x, y) for x, y in W.ZONE]          # (lat, lon) vertices
ALL_FIXES = (1, 2, 3, 4, 5)


def hav(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * R_M * math.asin(math.sqrt(a))


def inside(lat, lon):
    hit = False
    for i in range(len(ZONE_DEG)):
        la1, lo1 = ZONE_DEG[i]
        la2, lo2 = ZONE_DEG[(i + 1) % len(ZONE_DEG)]
        if (la1 > lat) != (la2 > lat) and lon < lo1 + (lat - la1) * (lo2 - lo1) / (la2 - la1):
            hit = not hit
    return hit


def edge_params(a, b):
    """Parameters u in (0,1) where a->b ((lat, lon) degrees) crosses a zone edge.
    The projection is linear, so these equal the fractions along the true line."""
    (ay, ax), (by, bx) = a, b
    rx, ry = bx - ax, by - ay
    out = []
    for i in range(len(ZONE_DEG)):
        (py, px), (qy, qx) = ZONE_DEG[i], ZONE_DEG[(i + 1) % len(ZONE_DEG)]
        sx, sy = qx - px, qy - py
        den = rx * sy - ry * sx
        if den == 0:
            continue
        u = ((px - ax) * sy - (py - ay) * sx) / den
        w = ((px - ax) * ry - (py - ay) * rx) / den
        if 0 < u < 1 and 0 <= w <= 1:
            out.append(u)
    return out


def local_cut_params(t0, t1):
    """Fractions of a->b (time t0 -> t1, either direction) at 00:00 UTC+9."""
    lo, hi = min(t0, t1), max(t0, t1)
    d = datetime.fromtimestamp(lo, UTC9).date()
    out = []
    while True:
        d += timedelta(days=1)
        c = datetime(d.year, d.month, d.day, tzinfo=UTC9).timestamp()
        if c >= hi:
            return out
        if c > lo:
            out.append((c - t0) / (t1 - t0))


def billable_with(pings, fixes=frozenset()):
    fixes = set(fixes)
    stream = sorted(pings, key=lambda p: p["ts_utc"]) if 1 in fixes else pings
    totals = defaultdict(float)
    seen, anchor = set(), {}
    for p in stream:
        drv, ts = p["driver_id"], p["ts_utc"]
        if (drv, ts) in seen:
            continue
        seen.add((drv, ts))
        lat, lon = float(p["lat"]), float(p["lon"])
        if W.distance_to_road(*W.to_xy(lat, lon)) > OFFROAD_M:
            continue
        if drv not in anchor:
            anchor[drv] = p
            continue
        a = anchor[drv]
        t_a = datetime.fromisoformat(a["ts_utc"]).timestamp()
        t_p = datetime.fromisoformat(ts).timestamp()
        raw_dt = t_p - t_a
        if 2 in fixes and raw_dt < 0:
            continue                                   # stale ping: drop, keep anchor
        dt = abs(raw_dt)
        if dt == 0:
            continue
        a_lat, a_lon = float(a["lat"]), float(a["lon"])
        d = hav(a_lat, a_lon, lat, lon)
        speed = d / dt * 3.6
        if 3 in fixes:
            if speed > MAX_KMH:
                continue                               # drop ping, keep anchor
            anchor[drv] = p
        else:
            anchor[drv] = p
            if speed > MAX_KMH:
                continue

        if 4 not in fixes and 5 not in fixes:          # exact legacy arithmetic
            if inside(a_lat, a_lon):
                totals[(drv, a["ts_utc"][:10])] += d
            continue

        cuts = {0.0, 1.0}
        if 4 in fixes:
            cuts.update(edge_params((a_lat, a_lon), (lat, lon)))
        if 5 in fixes:
            cuts.update(local_cut_params(t_a, t_p))
        cuts = sorted(cuts)
        start_in = inside(a_lat, a_lon)
        for u, v in zip(cuts, cuts[1:]):
            m = (u + v) / 2
            if 4 in fixes:
                if not inside(a_lat + m * (lat - a_lat), a_lon + m * (lon - a_lon)):
                    continue
            elif not start_in:
                continue
            if 5 in fixes:
                day = datetime.fromtimestamp(t_a + m * (t_p - t_a), UTC9).date().isoformat()
            else:
                day = a["ts_utc"][:10]
            totals[(drv, day)] += d * (v - u)
    return dict(totals)
