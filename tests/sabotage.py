"""Sabotage (mutation) harness. Usage: python tests/sabotage.py

 Patches gpsqa.fixed / gpsqa.world IN MEMORY, runs
tests/test_rules.py against each mutant, restores, reports which tests caught it.
No project file is modified."""
import math, os, sys, unittest
from datetime import timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["GPSQA_CALC"] = "fixed"
from gpsqa import fixed as F, world as W

ORIG_BILLABLE_WITH = F.billable_with


def run_suite():
    suite = unittest.defaultTestLoader.loadTestsFromName("tests.test_rules")
    res = unittest.TestResult()
    suite.run(res)
    return sorted(t._testMethodName.replace("test_", "") for t, _ in res.failures + res.errors), res.testsRun


def mutate(label, patches, after=None):
    """patches: list of (module, attr, new_value)."""
    saved = [(m, a, getattr(m, a)) for m, a, _ in patches]
    for m, a, v in patches:
        setattr(m, a, v)
    if after:
        after()
    try:
        caught, n = run_suite()
    finally:
        for m, a, v in saved:
            setattr(m, a, v)
        F.ZONE_DEG = [W.to_latlon(x, y) for x, y in W.ZONE]
    status = f"CAUGHT by {len(caught)}" if caught else "SURVIVED"
    print(f"  {label:52} {status:12} {', '.join(caught)}")
    return bool(caught)


def without(rule):
    return lambda pings, rejections=None: ORIG_BILLABLE_WITH(
        pings, [r for r in F.ALL_RULES if r != rule], rejections)


baseline, n = run_suite()
print(f"baseline: {n} tests, failures: {baseline or 'none'}\n")

print("A. Thresholds nudged by 0.01 (in their own unit)")
mutate("OFFROAD_M 100 -> 99.99 m", [(F, "OFFROAD_M", 99.99)])
mutate("OFFROAD_M 100 -> 100.01 m", [(F, "OFFROAD_M", 100.01)])
mutate("MAX_KMH 150 -> 149.99 km/h", [(F, "MAX_KMH", 149.99)])
mutate("MAX_KMH 150 -> 150.01 km/h", [(F, "MAX_KMH", 150.01)])
mutate("MAX_GAP_S 60 -> 59.99 s", [(F, "MAX_GAP_S", 59.99)])
mutate("MAX_GAP_S 60 -> 60.01 s", [(F, "MAX_GAP_S", 60.01)])
mutate("day cut UTC+9 -> UTC+8.99 h", [(F, "TZ_REPORT", timezone(timedelta(hours=8.99)))])
mutate("day cut UTC+9 -> UTC+9.01 h", [(F, "TZ_REPORT", timezone(timedelta(hours=9.01)))])
mutate("day cut shifted by 0.01 s", [(F, "TZ_REPORT", timezone(timedelta(hours=9, seconds=0.01)))])


def shift_zone(dy):
    def apply():
        F.ZONE_DEG = [W.to_latlon(x, y + dy) for x, y in W.ZONE]
    return apply


mutate("zone moved north by 0.01 m", [], after=shift_zone(0.01))
mutate("zone moved south by 0.01 m", [], after=shift_zone(-0.01))
mutate("EARTH_R_M 6371008.8 -> 6371008.81 (+0.01 m)", [(F, "EARTH_R_M", 6371008.81)])

print("\nB. Each rule switched off")
for r in F.ALL_RULES:
    mutate(f"{r} off ({F.RULE_NAMES[r]})", [(F, "billable", without(r))])

print("\nC. Reviewer blind-spot mutants")


def planar(lat1, lon1, lat2, lon2):
    return math.dist(W.to_xy(lat1, lon1), W.to_xy(lat2, lon2))


mutate("flat-world distance instead of haversine", [(F, "haversine", planar)])

orig_cross = F.zone_crossings


def no_vertical_edges(a, b):
    (ay, ax), (by, bx) = a, b
    out = []
    n = len(F.ZONE_DEG)
    for i in range(n):
        (py, px), (qy, qx) = F.ZONE_DEG[i], F.ZONE_DEG[(i + 1) % n]
        if px == qx:
            continue                       # BUG: ignore north-south zone edges
        ex, ey = qx - px, qy - py
        dx, dy = bx - ax, by - ay
        den = dx * ey - dy * ex
        if den == 0:
            continue
        u = ((px - ax) * ey - (py - ay) * ex) / den
        v = ((px - ax) * dy - (py - ay) * dx) / den
        if 0 < u < 1 and 0 <= v <= 1:
            out.append(u)
    return out


mutate("zone crossings ignore north-south edges", [(F, "zone_crossings", no_vertical_edges)])


def forgot_cos():
    W._COS_LAT0 = 1.0
    F.ZONE_DEG = [W.to_latlon(x, y) for x, y in W.ZONE]


mutate("projection forgets cos(latitude) (shared by tests)", [(W, "_COS_LAT0", W._COS_LAT0)], after=forgot_cos)


def keep_last_arrival(pings, rejections=None):
    # BUG: dedup keeps the LAST arrival instead of the first
    return ORIG_BILLABLE_WITH(list(reversed(pings)), F.ALL_RULES, rejections)


mutate("dedup keeps last arrival (feed reversed)", [(F, "billable", keep_last_arrival)])


def day_by_start(pings, rejections=None):
    # BUG: whole line goes to the day of its start ping (no time split)
    orig = F.day_cuts
    F.day_cuts = lambda t0, t1, tz: []
    try:
        return ORIG_BILLABLE_WITH(pings, F.ALL_RULES, rejections)
    finally:
        F.day_cuts = orig


mutate("day: whole line to start ping's day (no split)", [(F, "billable", day_by_start)])

