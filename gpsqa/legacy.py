"""LEGACY billable-distance calculation -- DELIBERATELY BUGGY.

This imitates code a real system might have inherited. It is the "reference
calculation that gets the distance wrong" required by the brief. Do NOT fix
bugs here; the corrected implementation will live in a separate module.

Known bugs (each marked "BUG n" at the line where it happens):
  BUG 1  pings processed in ARRIVAL order, not timestamp order
  BUG 2  abs() on the time difference hides backwards (out-of-order) segments
  BUG 3  a too-fast segment is dropped, but the anchor still moves to the bad ping
  BUG 4  zone tested only at the segment's START point; whole segment counted
  BUG 5  report day = UTC date of the start ping, not 00:00 UTC+9

Done correctly on purpose (to keep the bug list to the five above):
  * duplicates (same driver + timestamp): first arrival kept
  * off-road noise (> 100 m from any road): ping skipped, anchor NOT moved

Usage:  python -m gpsqa.legacy --data data
"""
import argparse
import csv
import math
from collections import defaultdict
from pathlib import Path

from . import world as W   # used ONLY as the "road map service" and zone config

EARTH_RADIUS_M = 6371000.0          # legacy's own constant (world.py uses 6371008.8)
MAX_SPEED_KMH = 150.0
OFFROAD_LIMIT_M = 100.0

# Campaign zone as the legacy config stores it: lat/lon vertices.
ZONE_LATLON = [W.to_latlon(x, y) for x, y in W.ZONE]


def _haversine_m(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def _in_zone(lat, lon):
    """Ray casting directly in degrees (lon = x, lat = y)."""
    inside = False
    n = len(ZONE_LATLON)
    for i in range(n):
        y1, x1 = ZONE_LATLON[i]
        y2, x2 = ZONE_LATLON[(i + 1) % n]
        if (y1 > lat) != (y2 > lat):
            if lon < x1 + (lat - y1) * (x2 - x1) / (y2 - y1):
                inside = not inside
    return inside


def _offroad(lat, lon):
    return W.distance_to_road(*W.to_xy(lat, lon)) > OFFROAD_LIMIT_M


def legacy_billable(pings, trace=None, rejections=None):
    """pings: list of dicts from pings.csv, IN FILE (= arrival) ORDER.
    Returns {(driver_id, day): metres}. Debug logs (they never change results):
      trace       list -> one dict per evaluated segment
      rejections  dict -> ping_id: reason, for every ping whose segment was
                  dropped (duplicate, off_road, zero_dt, too_fast)"""
    rej = rejections if rejections is not None else {}
    totals = defaultdict(float)
    seen = set()
    anchor = {}                                   # driver -> last accepted ping

    # BUG 1: iterate in the order pings ARRIVED. There is no sort by ts_utc,
    # so late and out-of-order pings are joined to whatever came just before
    # them in the feed, not to their true neighbours in time.
    for p in pings:
        drv, ts = p["driver_id"], p["ts_utc"]
        if (drv, ts) in seen:                     # dedup: correct, first arrival wins
            rej[p["ping_id"]] = "duplicate"
            continue
        seen.add((drv, ts))
        lat, lon = float(p["lat"]), float(p["lon"])
        if _offroad(lat, lon):                    # off-road: correct, anchor kept
            rej[p["ping_id"]] = "off_road"
            continue
        if drv not in anchor:
            anchor[drv] = p
            continue

        a = anchor[drv]
        raw_dt = (W.parse_utc(ts) - W.parse_utc(a["ts_utc"])).total_seconds()
        # BUG 2: abs() turns a negative time step (this ping is OLDER than the
        # anchor) into a positive one. The segment then looks normal and is
        # billed, and the vehicle is billed for driving back to an old position
        # and then forward again.
        dt = abs(raw_dt)
        if dt == 0:
            rej[p["ping_id"]] = "zero_dt"
            continue
        a_lat, a_lon = float(a["lat"]), float(a["lon"])
        d = _haversine_m(a_lat, a_lon, lat, lon)
        speed = d / dt * 3.6

        # BUG 3: the anchor moves BEFORE the speed check. When this ping is a
        # GPS jump, the segment is dropped, but the jump becomes the anchor, so
        # the next (good) segment also looks too fast and is dropped too.
        # Real distance driven during both intervals is lost.
        anchor[drv] = p
        counted = 0.0
        if speed > MAX_SPEED_KMH:
            rej[p["ping_id"]] = "too_fast"        # logged; anchor already moved (BUG 3)
        if speed <= MAX_SPEED_KMH:
            # BUG 4: zone tested at the START point only. A segment that starts
            # inside and ends outside is billed in full; one that starts outside
            # and ends inside is not billed at all.
            if _in_zone(a_lat, a_lon):
                # BUG 5: the day is the UTC calendar date of the start ping
                # (first 10 chars of ts). The report should cut at 00:00 UTC+9,
                # i.e. 15:00 UTC, so 9 hours of driving land on the wrong day.
                day = a["ts_utc"][:10]
                totals[(drv, day)] += d
                counted = d
        if trace is not None:
            trace.append(dict(driver_id=drv, from_id=a["ping_id"], to_id=p["ping_id"],
                              raw_dt=raw_dt, dt=dt, dist_m=d, speed_kmh=speed,
                              start_in=_in_zone(a_lat, a_lon), end_in=_in_zone(lat, lon),
                              utc_day=a["ts_utc"][:10],
                              local_day=W.local_date(W.parse_utc(a["ts_utc"]).timestamp()),
                              counted_m=counted))
    return dict(totals)


# Common interface used by qa_check.py: billable(pings) -> {(driver, day): metres}
billable = legacy_billable


def main():
    ap = argparse.ArgumentParser(description="Legacy (buggy) billable distance")
    ap.add_argument("--data", default="data")
    args = ap.parse_args()
    data = Path(args.data)
    pings = list(csv.DictReader(open(data / "pings.csv", newline="")))
    totals = legacy_billable(pings)

    with open(data / "legacy_km.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["driver_id", "day_as_legacy_reports_it", "legacy_km", "legacy_m"])
        for (drv, day), m in sorted(totals.items()):
            w.writerow([drv, day, f"{m / 1000:.4f}", f"{m:.3f}"])

    expected = defaultdict(dict)
    for r in csv.DictReader(open(data / "expected_km.csv", newline="")):
        expected[r["driver_id"]][r["local_date"]] = float(r["expected_m"])

    print("Per driver per day. Legacy labels days by UTC date; expected by UTC+9 date.")
    print(f"{'driver':7} {'day':10} {'legacy_m':>11} {'expected_m':>11} {'legacy-exp':>11}")
    drivers = sorted(set(expected) | {d for d, _ in totals})
    gl = ge = 0.0
    for drv in drivers:
        days = sorted(set(expected[drv]) | {day for d, day in totals if d == drv})
        tl = te = 0.0
        for day in days:
            lm, em = totals.get((drv, day), 0.0), expected[drv].get(day, 0.0)
            tl, te = tl + lm, te + em
            print(f"{drv:7} {day:10} {lm:11.3f} {em:11.3f} {lm - em:+11.3f}")
        print(f"{drv:7} {'TOTAL':10} {tl:11.3f} {te:11.3f} {tl - te:+11.3f}")
        gl, ge = gl + tl, ge + te
    print(f"{'ALL':7} {'TOTAL':10} {gl:11.3f} {ge:11.3f} {gl - ge:+11.3f}")
    print(f"wrote {data / 'legacy_km.csv'}")


if __name__ == "__main__":
    main()
