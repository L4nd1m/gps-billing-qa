"""Rule tests: small SYNTHETIC scenarios per rule, plus exact-limit tests.

Against the corrected calculation (default):
    python -m unittest tests.test_rules -v
Against legacy (PowerShell):
    $env:GPSQA_CALC = "legacy"; python -m unittest tests.test_rules -v
Both, checking every test behaves as tagged:
    python tests/run_regression.py

Tags:
  @regression  must FAIL on legacy -- and with exactly the wrong result the
               test DECLARES for legacy (so it fails for the stated bug, not
               for some other reason) -- and PASS on fixed
  @guard       legacy already does this correctly: must PASS on both
  @fixed_only  checks a function legacy does not have: skipped on legacy

Every scenario checks billed metres per (driver, day) to 1e-6 m AND exactly
which pings were rejected and why.

Independence: pings are built with THIS file's own projection and distances
are checked with THIS file's own haversine (mean Earth radius 6,371,008.8 m);
nothing in the oracle comes from gpsqa. test_projection_pinned ties
gpsqa.world to the same hand-checkable numbers.

Legacy measures with a 6,371,000 m Earth radius (1.4 ppm short). Its metres
are rescaled to the mean radius in every test except
test_distance_uses_mean_earth_radius, so each test fails only for its own bug.
"""
import math
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gpsqa import fixed, legacy, world  # noqa: E402

KM_DELTA_M = 1e-6
LEGACY_TO_MEAN_RADIUS = 6371008.8 / 6371000.0
MIDDAY = datetime(2026, 3, 10, 3, 0, 0, tzinfo=timezone.utc)   # 12:00 UTC+9, same date in both
DAY, NEXT = "2026-03-10", "2026-03-11"

# ------------------------------------------------- the tests' own geometry
T_R = 6371008.8                     # mean Earth radius
T_LAT0, T_LON0 = 35.0, 150.0        # fictional origin (open ocean)
T_COS = math.cos(math.radians(T_LAT0))


def t_latlon(x, y):
    """Local metres -> degrees, written independently of gpsqa.world."""
    return T_LAT0 + y * 180.0 / (math.pi * T_R), T_LON0 + x * 180.0 / (math.pi * T_R * T_COS)


def t_x(lon):
    return (lon - T_LON0) * math.pi / 180.0 * T_R * T_COS


def hav(a, b):
    (la1, lo1), (la2, lo2) = a, b
    p1, p2 = math.radians(la1), math.radians(la2)
    h = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lo2 - lo1) / 2) ** 2)
    return 2 * T_R * math.asin(math.sqrt(h))


def pt(p):
    return float(p["lat"]), float(p["lon"])


def seg(p, q):
    return hav(pt(p), pt(q))


def path_m(pings):
    return sum(seg(a, b) for a, b in zip(pings, pings[1:]))


# ---------------------------------------------------------------- tags
def regression(fn):
    fn.legacy_should = "fail"
    return fn


def guard(fn):
    fn.legacy_should = "pass"
    return fn


def fixed_only(fn):
    fn.legacy_should = "skip"
    return fn


def calc_name():
    return os.environ.get("GPSQA_CALC", "fixed")


def run_calc(pings, normalise=True):
    rej = {}
    name = calc_name()
    if name == "legacy":
        km = legacy.legacy_billable(pings, rejections=rej)
        if normalise:
            km = {k: v * LEGACY_TO_MEAN_RADIUS for k, v in km.items()}
    elif name == "fixed":
        km = fixed.billable(pings, rejections=rej)
    else:
        raise ValueError(f"GPSQA_CALC must be 'fixed' or 'legacy', not {name!r}")
    return km, rej


# ----------------------------------------------------------- ping builders
def ts_str(t, base=MIDDAY):
    dt = base + timedelta(seconds=t)
    s = dt.isoformat(timespec="milliseconds" if dt.microsecond else "seconds")
    return s.replace("+00:00", "Z")


def ping(pid, t, lat, lon, drv="T-01", base=MIDDAY):
    return {"ping_id": pid, "driver_id": drv, "ts_utc": ts_str(t, base),
            "lat": repr(lat), "lon": repr(lon), "received_at_utc": ""}


def at(pid, t, y, x=1000.0, drv="T-01", base=MIDDAY):
    return ping(pid, t, *t_latlon(x, y), drv=drv, base=base)


def bracket(f, x0, limit, max_steps=10_000_000):
    """Adjacent floats (below, above) with f(below) <= limit < f(above); f increasing."""
    x, n = x0, 0
    if f(x) > limit:
        while f(x) > limit:
            x, n = math.nextafter(x, -math.inf), n + 1
            assert n < max_steps
        return x, math.nextafter(x, math.inf)
    while f(x) <= limit:
        x, n = math.nextafter(x, math.inf), n + 1
        assert n < max_steps
    return math.nextafter(x, -math.inf), x


def differences(km, rej, km_want, rej_want):
    keys = set(km) | set(km_want)
    bad = {k: (km.get(k, 0.0), km_want.get(k, 0.0)) for k in sorted(keys)
           if abs(km.get(k, 0.0) - km_want.get(k, 0.0)) > KM_DELTA_M}
    out = []
    if bad:
        out.append("metres (got, want): " + ", ".join(f"{k}: ({g:.6f}, {w:.6f})" for k, (g, w) in bad.items()))
    if rej != rej_want:
        out.append(f"rejections got {rej}, want {rej_want}")
    return out


class RuleTests(unittest.TestCase):
    maxDiff = None

    def check(self, pings, km_want, rej_want, legacy_km=None, legacy_rej=None, bug=None,
              normalise=True):
        km, rej = run_calc(pings, normalise)
        tag = getattr(getattr(self, self._testMethodName), "legacy_should", None)
        if calc_name() == "legacy" and tag == "fail":
            if legacy_km is None or legacy_rej is None or bug is None:
                self.fail("regression test must declare legacy_km, legacy_rej and bug")
            if not differences(legacy_km, legacy_rej, km_want, rej_want):
                self.fail("declared legacy result equals the correct result: not a regression test")
            off = differences(km, rej, legacy_km, legacy_rej)
            if off:
                self.fail("UNDECLARED legacy result: " + " | ".join(off))
            self.fail(f"DECLARED legacy failure ({bug}): "
                      + " | ".join(differences(km, rej, km_want, rej_want)))
        problems = differences(km, rej, km_want, rej_want)
        if problems:
            self.fail(" | ".join(problems))

    # ======================================================= projection
    @guard
    def test_projection_pinned_to_independent_values(self):
        """gpsqa.world must agree with this file's projection, and with two
        hand-checkable facts: 1 degree of latitude = R*pi/180 = 111,195.08 m;
        1 degree of longitude at 35N = that x cos(35 deg) = x 0.819152 = 91,085.68 m."""
        for x, y in [(0, 0), (1000, 2000), (3000, 3000), (2500, 500)]:
            wl, tl = world.to_latlon(x, y), t_latlon(x, y)
            self.assertAlmostEqual(wl[0], tl[0], delta=1e-12, msg=(x, y))
            self.assertAlmostEqual(wl[1], tl[1], delta=1e-12, msg=(x, y))
        deg_m = T_R * math.pi / 180.0
        self.assertAlmostEqual(world.to_xy(36.0, 150.0)[1], deg_m, delta=1e-6)
        self.assertAlmostEqual(world.to_xy(35.0, 151.0)[0], deg_m * T_COS, delta=1e-6)
        self.assertAlmostEqual(deg_m, 111195.08, delta=0.01)
        self.assertAlmostEqual(deg_m * T_COS, 91085.68, delta=0.01)

    @regression
    def test_distance_uses_mean_earth_radius(self):
        """1,000 m due north in 60 s (60 km/h, gap exactly 60 s): billed 1,000 m.
        Legacy measures with R = 6,371,000 m: 1,000 x 6,371,000 / 6,371,008.8."""
        pings = [at("A", 0, 600), at("B", 60, 1600)]
        want = seg(*pings)
        self.check(pings, {("T-01", DAY): want}, {},
                   legacy_km={("T-01", DAY): want / LEGACY_TO_MEAN_RADIUS}, legacy_rej={},
                   bug="legacy Earth radius 6,371,000 m", normalise=False)

    # ======================================================= R1 duplicates
    @guard
    def test_R1_duplicates_first_arrival_kept(self):
        """B2 exact copy of B, B3 a copy 10 m east; both arrive after B: 200 m."""
        a, b, c = at("A", 0, 1000), at("B", 10, 1100), at("C", 20, 1200)
        self.check([a, b, dict(b, ping_id="B2"), at("B3", 10, 1100, x=1010.0), c],
                   {("T-01", DAY): 200.0}, {"B2": "duplicate", "B3": "duplicate"})

    @guard
    def test_R1_shifted_copy_arriving_first_is_kept(self):
        """B3 (10 m east) arrives BEFORE the original B, so B3 is kept and B rejected."""
        a, b3, b, c = at("A", 0, 1000), at("B3", 10, 1100, x=1010.0), at("B", 10, 1100), at("C", 20, 1200)
        self.check([a, b3, b, c], {("T-01", DAY): path_m([a, b3, c])}, {"B": "duplicate"})

    # ======================================================= R2 ordering
    @regression
    def test_R2_late_ping_placed_by_its_timestamp(self):
        """C (t=20 s) arrives after D (t=30 s). Correct: 400 m.
        Legacy joins B->D->C->E in arrival order: 200 + 100 back + 200 = 600 m."""
        a, b, c, d, e = (at(n, 10 * i, 1000 + 100 * i) for i, n in enumerate("ABCDE"))
        self.check([a, b, d, c, e], {("T-01", DAY): 400.0}, {},
                   legacy_km={("T-01", DAY): 600.0}, legacy_rej={}, bug="bugs 1+2")

    @guard
    def test_drivers_interleaved_in_arrival_order(self):
        """Two drivers' pings alternate in the feed; each is billed on its own: 200 m each."""
        t1 = [at(f"P{i}", 10 * i, 1000 + 100 * i) for i in range(3)]
        t2 = [at(f"Q{i}", 10 * i, 1500 + 100 * i, drv="T-02") for i in range(3)]
        feed = [p for pair in zip(t1, t2) for p in pair]
        self.check(feed, {("T-01", DAY): 200.0, ("T-02", DAY): 200.0}, {})

    # ======================================================= R3 off-road
    @guard
    def test_R3_off_road_limit_1mm(self):
        """Probe 99.999 m from the road kept (T-01); 100.001 m ignored, line bridged A->B (T-02)."""
        keep = [at("A1", 0, 1600), at("P99.999", 10, 1700, x=1099.999), at("B1", 20, 1800)]
        drop = [at("A2", 0, 1600, drv="T-02"), at("P100.001", 10, 1700, x=1100.001, drv="T-02"),
                at("B2", 20, 1800, drv="T-02")]
        self.check(keep + drop, {("T-01", DAY): path_m(keep), ("T-02", DAY): 200.0},
                   {"P100.001": "off_road"})

    @guard
    def test_R3_off_road_limit_nearest_floats(self):
        """Adjacent longitudes whose distance to road x = 1000 (this file's own
        projection) is just <= 100 m and just > 100 m: first kept, second ignored."""
        lat, lon0 = t_latlon(1100.0, 1700.0)
        f = lambda lon: t_x(lon) - 1000.0
        below, above = bracket(f, lon0, 100.0)
        self.assertLess(f(above) - f(below), 1e-8, (f(below), f(above)))
        keep = [at("A1", 0, 1600), ping("Pbelow", 10, lat, below), at("B1", 20, 1800)]
        drop = [at("A2", 0, 1600, drv="T-02"), ping("Pabove", 10, lat, above, drv="T-02"),
                at("B2", 20, 1800, drv="T-02")]
        self.check(keep + drop, {("T-01", DAY): path_m(keep), ("T-02", DAY): 200.0},
                   {"Pabove": "off_road"})

    # ======================================================= R4 speed
    @regression
    def test_R4_jump_does_not_move_anchor(self):
        """J jumps 1,300 m in 10 s and is ignored; B->C 200 m in 20 s is billed: 400 m.
        Legacy moves its anchor to J, so C also looks too fast: 200 m, J and C rejected."""
        pings = [at("A", 0, 1000), at("B", 10, 1100), at("J", 20, 2400),
                 at("C", 30, 1300), at("D", 40, 1400)]
        self.check(pings, {("T-01", DAY): 400.0}, {"J": "too_fast"},
                   legacy_km={("T-01", DAY): 200.0}, legacy_rej={"J": "too_fast", "C": "too_fast"},
                   bug="bug 3")

    @guard
    def test_R4_speed_limit_1mm(self):
        """10 s after A: 416.666 m (149.9996 km/h) kept (T-01); 416.668 m (150.0004 km/h)
        ignored (T-02). The ignored ping is last, so bug 3 cannot change the metres."""
        exact = 150 / 3.6 * 10
        t1 = [at("A1", 0, 600), at("P-1mm", 10, 600 + exact - 0.001)]
        t2 = [at("A2", 0, 600, drv="T-02"), at("P+1mm", 10, 600 + exact + 0.001, drv="T-02")]
        self.check(t1 + t2, {("T-01", DAY): path_m(t1), ("T-02", DAY): 0.0}, {"P+1mm": "too_fast"})

    @regression
    def test_R4_speed_limit_nearest_floats(self):
        """Adjacent latitudes whose speed (this file's haversine, mean radius) is just
        <= 150 km/h and just > 150 km/h. Legacy's shorter radius reads 'just above'
        as about 149.9998 km/h and keeps it."""
        la, lo = t_latlon(1000.0, 600.0)
        f = lambda lat: hav((la, lo), (lat, lo)) / 10.0 * 3.6
        below, above = bracket(f, t_latlon(1000.0, 600 + 150 / 3.6 * 10)[0], 150.0)
        self.assertLess(f(above) - f(below), 1e-8, (f(below), f(above)))
        t1 = [at("A1", 0, 600), ping("Pbelow", 10, below, lo)]
        t2 = [at("A2", 0, 600, drv="T-02"), ping("Pabove", 10, above, lo, drv="T-02")]
        self.check(t1 + t2, {("T-01", DAY): path_m(t1), ("T-02", DAY): 0.0}, {"Pabove": "too_fast"},
                   legacy_km={("T-01", DAY): path_m(t1), ("T-02", DAY): path_m(t2)}, legacy_rej={},
                   bug="legacy Earth radius 6,371,000 m")

    # ======================================================= R5 zone
    @regression
    def test_R5_zone_east_west_edges(self):
        """North-south road x = 1000 crosses the zone edge at y = 500 (entry) and y = 2500 (exit).
        T-01: 50 + 100 + 100 = 250 m (legacy 0 + 100 + 100). T-02: 100 + 50 = 150 m (legacy 200)."""
        enter = [at(f"E{i}", 10 * i, 450 + 100 * i) for i in range(4)]
        leave = [at(f"X{i}", 10 * i, 2350 + 100 * i, drv="T-02") for i in range(3)]
        self.check(enter + leave, {("T-01", DAY): 250.0, ("T-02", DAY): 150.0}, {},
                   legacy_km={("T-01", DAY): 200.0, ("T-02", DAY): 200.0}, legacy_rej={},
                   bug="bug 4")

    @regression
    def test_R5_zone_north_south_edges(self):
        """East-west road y = 1000 crosses the zone edge at x = 500 (entry, T-01) and
        x = 2500 (exit, T-02), halfway along a line each time."""
        e = [at(f"E{i}", 10 * i, 1000, x=450 + 100 * i) for i in range(3)]
        x = [at(f"X{i}", 10 * i, 1000, x=2350 + 100 * i, drv="T-02") for i in range(3)]
        want = {("T-01", DAY): 0.5 * seg(e[0], e[1]) + seg(e[1], e[2]),
                ("T-02", DAY): seg(x[0], x[1]) + 0.5 * seg(x[1], x[2])}
        self.check(e + x, want, {},
                   legacy_km={("T-01", DAY): seg(e[1], e[2]), ("T-02", DAY): path_m(x)},
                   legacy_rej={}, bug="bug 4")

    @regression
    def test_R5_zone_inner_corner(self):
        """Diagonal road through the zone's inner corner (1500, 1500): the line
        (1450,1450)->(1550,1550) is inside only for its first half."""
        d = [at(f"D{i}", 10 * i, 1350 + 100 * i, x=1350 + 100 * i) for i in range(3)]
        self.check(d, {("T-01", DAY): seg(d[0], d[1]) + 0.5 * seg(d[1], d[2])}, {},
                   legacy_km={("T-01", DAY): path_m(d)}, legacy_rej={}, bug="bug 4")

    @guard
    def test_R5_ping_exactly_on_zone_edge(self):
        """Pings at y = 400, 500 (on the edge), 600: only 500->600 is inside: 100 m."""
        self.check([at(f"P{i}", 10 * i, 400 + 100 * i) for i in range(3)], {("T-01", DAY): 100.0}, {})

    # ======================================================= R6 day cut
    @regression
    def test_R6_day_cut_split_by_time(self):
        """14:59:45 ... 15:00:15 UTC; B->C crosses 00:00 UTC+9 halfway: 150 m per day.
        Legacy puts all 300 m on UTC date 03-10."""
        base = datetime(2026, 3, 10, 14, 59, 45, tzinfo=timezone.utc)
        pings = [at(n, 10 * i, 1000 + 100 * i, base=base) for i, n in enumerate("ABCD")]
        self.check(pings, {("T-01", DAY): 150.0, ("T-01", NEXT): 150.0}, {},
                   legacy_km={("T-01", DAY): 300.0}, legacy_rej={}, bug="bug 5")

    @regression
    def test_R6_ping_exactly_at_midnight(self):
        """B is at exactly 15:00:00 UTC = 00:00 UTC+9: A->B is all 03-10, B->C all 03-11."""
        base = datetime(2026, 3, 10, 14, 59, 50, tzinfo=timezone.utc)
        pings = [at(n, 10 * i, 1000 + 100 * i, base=base) for i, n in enumerate("ABC")]
        self.check(pings, {("T-01", DAY): 100.0, ("T-01", NEXT): 100.0}, {},
                   legacy_km={("T-01", DAY): 200.0}, legacy_rej={}, bug="bug 5")

    @regression
    def test_R5_R6_line_crosses_zone_edge_and_midnight(self):
        """14:59:55 y=430 -> 15:00:05 y=530 -> 15:00:15 y=630. First line: midnight at
        50 %, zone edge at 70 %: 30 m inside, all on 03-11. Second: 100 m on 03-11.
        Legacy: first line starts outside (0); second starts inside, UTC date 03-10."""
        base = datetime(2026, 3, 10, 14, 59, 55, tzinfo=timezone.utc)
        pings = [at(n, 10 * i, 430 + 100 * i, base=base) for i, n in enumerate("ABC")]
        self.check(pings, {("T-01", NEXT): 130.0}, {},
                   legacy_km={("T-01", DAY): 100.0}, legacy_rej={}, bug="bugs 4+5")

    # ======================================================= R7 gap
    @regression
    def test_R7_gap_limit_60s(self):
        """A->B exactly 60.000 s: billed. B->C 59.999 s: billed. C->D 60.001 s: not billed.
        300 + 300 + 0 = 600 m; legacy 900 m."""
        pings = [at("A", 0, 600), at("B", 60, 900), at("C", 119.999, 1200), at("D", 180, 1500)]
        self.check(pings, {("T-01", DAY): 600.0}, {},
                   legacy_km={("T-01", DAY): 900.0}, legacy_rej={}, bug="no gap rule")

    # ======================================================= R8 anchor
    @regression
    def test_R8_bad_first_ping(self):
        """J0, the first ping of the shift, is 1,400 m from A (10 s later). The next
        ping B agrees with A, so J0 is dropped as bad_anchor: A->D = 300 m.
        Without R8, J0 stays the anchor until C comes within 150 km/h: 1,300 m.
        Legacy also bills 300 m (its bug 3 moves the anchor) but rejects A instead."""
        pings = [at("J0", 0, 2400), at("A", 10, 1000), at("B", 20, 1100),
                 at("C", 30, 1200), at("D", 40, 1300)]
        self.check(pings, {("T-01", DAY): 300.0}, {"J0": "bad_anchor"},
                   legacy_km={("T-01", DAY): 300.0}, legacy_rej={"A": "too_fast"}, bug="bug 3")

    @regression
    def test_R8_bad_second_ping_keeps_good_anchor(self):
        """A is good, J (2nd ping) jumps 1,400 m. B does NOT agree with J, so J is
        dropped and A stays: A->B 200 m in 20 s, B->C 100 m = 300 m.
        Legacy anchors on J, then B looks too fast too: only C->... 100 m."""
        pings = [at("A", 0, 1000), at("J", 10, 2400), at("B", 20, 1200), at("C", 30, 1300)]
        self.check(pings, {("T-01", DAY): 300.0}, {"J": "too_fast"},
                   legacy_km={("T-01", DAY): 100.0}, legacy_rej={"J": "too_fast", "B": "too_fast"},
                   bug="bug 3")

    # ======================================================= predicates
    @fixed_only
    def test_limit_predicates_exact_values(self):
        """The comparisons fed exactly 100.0 m, 150.0 km/h, 60.0 s (allowed) and the next float above."""
        if calc_name() == "legacy":
            self.skipTest("legacy has no separate limit functions")
        up = lambda v: math.nextafter(v, math.inf)
        self.assertFalse(fixed.is_off_road(100.0))
        self.assertTrue(fixed.is_off_road(up(100.0)))
        self.assertFalse(fixed.is_too_fast(150.0))
        self.assertTrue(fixed.is_too_fast(up(150.0)))
        self.assertFalse(fixed.is_gap(60.0))
        self.assertTrue(fixed.is_gap(up(60.0)))


if __name__ == "__main__":
    unittest.main()
