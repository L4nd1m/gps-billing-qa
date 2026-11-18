"""CORRECTED billable-distance calculation. Rules R1-R6, each switchable.

    billable(pings)                  -> all rules ON  (used by qa_check as "after")
    billable_with(pings, rules)      -> only the rules in `rules` ON
    result: {(driver_id, day): metres}

Rules (from the brief plus the decisions recorded during this project):
  R1 dedup     same driver + same timestamp counts once; first ARRIVAL kept.
               OFF: every copy is processed.
  R2 sort      process each driver's pings by their own timestamp
               (stable sort, so R1 still keeps the first arrival).
               OFF: arrival order; time steps may be negative (|dt| is used).
  R3 offroad   a ping > 100 m from every road is ignored; the line is bridged
               from the previous to the next accepted ping.   OFF: no check.
  R4 speed     a ping implying > 150 km/h from the last ACCEPTED ping is
               ignored; the anchor does not move. dt = 0 counts as infinite.
               OFF: no check.
  R5 zone      bill only the part of each line inside the campaign zone,
               cut at the zone edge.   OFF: bill every line in full.
  R6 day       report day = calendar day in UTC+9; a line crossing 00:00
               UTC+9 is split in proportion to time.
               OFF: calendar day in UTC (still split by time).
  R7 gap       a line spanning MORE than 60 s is not billed (exactly 60 s is
               billed); its end ping is kept and becomes the anchor.
               OFF: every line is billed whatever its duration.
  R8 anchor    (needs R4) the speed anchor is "unconfirmed" until an accepted
               line touches it -- in practice the first ping of a shift, or a
               ping that just replaced a bad anchor. If a ping is too fast vs
               an UNCONFIRMED anchor, look at the next ping: if it agrees with
               the new ping, the anchor is dropped as "bad_anchor" and the new
               ping becomes the anchor; otherwise the new ping is dropped.
               OFF: the first ping is always trusted (a bad first ping makes
               every later good ping look too fast until one comes close).

Optional log: billable_with(..., rejections={}) fills ping_id -> reason for
every ping ignored by R1-R4/R8 ("duplicate", "off_road", "too_fast",
"bad_anchor"). R7 rejects no ping (only the line is unbilled).

Distance = haversine between ping coordinates, R = 6,371,008.8 m (mean Earth
radius). Nothing is imported from legacy.py, replica.py or expected.py; the
only shared pieces are the road map (world.distance_to_road) and the zone
config (world.ZONE), as a real system would load them.

Usage:  python -m gpsqa.fixed --data data      (prints the comparison)
"""
import argparse
import csv
import math
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import world as W

ALL_RULES = ("R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8")
RULE_NAMES = {"R1": "dedup (first arrival)", "R2": "sort by timestamp",
              "R3": "ignore > 100 m off road", "R4": "ignore > 150 km/h vs last accepted",
              "R5": "bill only inside zone (cut at edge)", "R6": "day cut 00:00 UTC+9",
              "R7": "no billing across gaps > 60 s",
              "R8": "verify unconfirmed anchor (look-ahead)"}
EARTH_R_M = 6371008.8
OFFROAD_M = 100.0
MAX_KMH = 150.0
MAX_GAP_S = 60.0
TZ_REPORT = timezone(timedelta(hours=9))
ZONE_DEG = [W.to_latlon(x, y) for x, y in W.ZONE]     # campaign config, (lat, lon)


# ---------------------------------------------------- limit comparisons
# The brief says "more than 100 m" and "above 150 km/h": the limit itself is
# allowed. Same for the gap: exactly 60 s is billed. No hidden tolerance.
def is_off_road(distance_m):
    return distance_m > OFFROAD_M


def is_too_fast(kmh):
    return kmh > MAX_KMH


def is_gap(seconds):
    return seconds > MAX_GAP_S


# ------------------------------------------------------------------ geometry
def haversine(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    h = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * EARTH_R_M * math.asin(math.sqrt(h))


def in_zone(lat, lon):
    inside = False
    n = len(ZONE_DEG)
    for i in range(n):
        la1, lo1 = ZONE_DEG[i]
        la2, lo2 = ZONE_DEG[(i + 1) % n]
        if (la1 > lat) != (la2 > lat):
            if lon < lo1 + (lat - la1) * (lo2 - lo1) / (la2 - la1):
                inside = not inside
    return inside


def zone_crossings(a, b):
    """Fractions u in (0,1) where line a->b ((lat, lon)) crosses a zone edge.
    The map projection is linear, so fractions in degrees equal true fractions."""
    (ay, ax), (by, bx) = a, b
    dx, dy = bx - ax, by - ay
    out = []
    n = len(ZONE_DEG)
    for i in range(n):
        (py, px), (qy, qx) = ZONE_DEG[i], ZONE_DEG[(i + 1) % n]
        ex, ey = qx - px, qy - py
        den = dx * ey - dy * ex
        if den == 0:
            continue
        u = ((px - ax) * ey - (py - ay) * ex) / den
        v = ((px - ax) * dy - (py - ay) * dx) / den
        if 0 < u < 1 and 0 <= v <= 1:
            out.append(u)
    return out


def day_cuts(t0, t1, tz):
    """Fractions of the time span t0 -> t1 (either direction) at local midnight."""
    lo, hi = min(t0, t1), max(t0, t1)
    d = datetime.fromtimestamp(lo, tz).date()
    out = []
    while True:
        d += timedelta(days=1)
        c = datetime(d.year, d.month, d.day, tzinfo=tz).timestamp()
        if c >= hi:
            return out
        if c > lo:
            out.append((c - t0) / (t1 - t0))


# --------------------------------------------------------------- pipeline
def _accepted_track(pings_of_driver, rules, rej):
    """Apply R1-R4. Returns the list of (t, lat, lon) that will be joined."""
    ps = pings_of_driver
    if "R1" in rules:
        seen, kept = set(), []
        for p in ps:                                   # arrival order
            if p["ts_utc"] not in seen:
                seen.add(p["ts_utc"])
                kept.append(p)
            else:
                rej[p["ping_id"]] = "duplicate"
        ps = kept
    if "R2" in rules:
        ps = sorted(ps, key=lambda p: datetime.fromisoformat(p["ts_utc"]).timestamp())
    cand = []
    for p in ps:
        lat, lon = float(p["lat"]), float(p["lon"])
        if "R3" in rules and is_off_road(W.distance_to_road(*W.to_xy(lat, lon))):
            rej[p["ping_id"]] = "off_road"
            continue
        cand.append((datetime.fromisoformat(p["ts_utc"]).timestamp(), lat, lon, p["ping_id"]))

    def too_fast(a, b):
        dt = abs(b[0] - a[0])
        return is_too_fast(math.inf if dt == 0 else haversine(a[1], a[2], b[1], b[2]) / dt * 3.6)

    track = []                     # accepted (t, lat, lon, ping_id)
    confirmed = False              # does an accepted line touch the anchor track[-1]?
    for i, q in enumerate(cand):
        if not track or "R4" not in rules or not too_fast(track[-1], q):
            confirmed = bool(track)
            track.append(q)
            continue
        # q is too fast vs the anchor. R8: if the anchor is unconfirmed (only
        # possible while it is the sole accepted ping), the anchor may be the
        # bad one: look ahead one ping and keep whichever of the two it agrees with.
        nxt = cand[i + 1] if i + 1 < len(cand) else None
        if "R8" in rules and not confirmed and nxt is not None and not too_fast(q, nxt):
            rej[track.pop()[3]] = "bad_anchor"
            track.append(q)        # new, still unconfirmed, anchor
            continue
        rej[q[3]] = "too_fast"
    return [(t, lat, lon) for t, lat, lon, _ in track]


def billable_with(pings, rules=ALL_RULES, rejections=None):
    rules = set(rules)
    unknown = rules - set(ALL_RULES)
    if unknown:
        raise ValueError(f"unknown rules: {sorted(unknown)}")
    tz = TZ_REPORT if "R6" in rules else timezone.utc
    by_driver = defaultdict(list)
    for p in pings:                                    # keeps arrival order
        by_driver[p["driver_id"]].append(p)

    rej = rejections if rejections is not None else {}
    totals = defaultdict(float)
    for drv, ps in by_driver.items():
        track = _accepted_track(ps, rules, rej)
        for t, _, _ in track:                          # report every active day
            totals[(drv, datetime.fromtimestamp(t, tz).date().isoformat())] += 0.0
        for (t0, la0, lo0), (t1, la1, lo1) in zip(track, track[1:]):
            if t1 == t0:
                continue                               # only possible with R1 off
            if "R7" in rules and is_gap(abs(t1 - t0)):
                continue                               # gap: line not billed
            length = haversine(la0, lo0, la1, lo1)
            cuts = {0.0, 1.0, *day_cuts(t0, t1, tz)}
            if "R5" in rules:
                cuts.update(zone_crossings((la0, lo0), (la1, lo1)))
            cuts = sorted(cuts)
            for u, v in zip(cuts, cuts[1:]):
                m = (u + v) / 2
                if "R5" in rules and not in_zone(la0 + m * (la1 - la0), lo0 + m * (lo1 - lo0)):
                    continue
                day = datetime.fromtimestamp(t0 + m * (t1 - t0), tz).date().isoformat()
                totals[(drv, day)] += length * (v - u)
    return dict(totals)


def billable(pings, rejections=None):
    return billable_with(pings, ALL_RULES, rejections)


# -------------------------------------------------------------- comparison
def main(argv=None):
    ap = argparse.ArgumentParser(description="Corrected billable distance vs expected km")
    ap.add_argument("--data", default="data")
    ap.add_argument("--tolerance-m", type=float, default=1.0)
    args = ap.parse_args(argv)
    data = Path(args.data)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from qa_check import expected_by_day                # reference only, for the comparison

    pings = list(csv.DictReader(open(data / "pings.csv", newline="")))
    expected = expected_by_day(data)
    got = billable(pings)

    print("All rules ON vs expected km (UTC+9 days)")
    print(f"{'driver':7} {'day':10} {'fixed_m':>12} {'expected_m':>12} {'diff_m':>10}  result")
    worst = 0.0
    keys = sorted(set(got) | set(expected))
    rows = []
    for k in keys:
        g, e = got.get(k, 0.0), expected.get(k, 0.0)
        worst = max(worst, abs(g - e))
        ok = abs(g - e) <= args.tolerance_m
        rows.append((k, g, e, ok))
        print(f"{k[0]:7} {k[1]:10} {g:12.3f} {e:12.3f} {g - e:+10.6f}  {'PASS' if ok else 'FAIL'}")
    print(f"max |diff| = {worst:.6f} m  (tolerance {args.tolerance_m} m)")

    with open(data / "fixed_km.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["driver_id", "local_date", "fixed_km", "fixed_m", "expected_m", "diff_m"])
        for k, g, e, _ in rows:
            w.writerow([k[0], k[1], f"{g / 1000:.4f}", f"{g:.3f}", f"{e:.3f}", f"{g - e:.6f}"])

    def l1(calc):
        return sum(abs(calc.get(k, 0.0) - expected.get(k, 0.0)) for k in set(calc) | set(expected))

    print("\nEach rule switched OFF alone (others ON):")
    print(f"  {'rule off':4} {'meaning':36} {'abs error m':>12} {'total calc-exp m':>17}")
    print(f"  {'none':4} {'all rules on':36} {l1(got):12.3f} {sum(got.values()) - sum(expected.values()):+17.3f}")
    for r in ALL_RULES:
        res = billable_with(pings, [x for x in ALL_RULES if x != r])
        print(f"  {r:4} {RULE_NAMES[r]:36} {l1(res):12.3f} "
              f"{sum(res.values()) - sum(expected.values()):+17.3f}")
    print(f"wrote {data / 'fixed_km.csv'}")
    return 0 if all(ok for *_, ok in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
