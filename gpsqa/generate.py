"""Generate a SYNTHETIC GPS ping dataset with injected faults + answer key.

Usage:  python -m gpsqa.generate --seed 20260310 --out data

Outputs (all 100% synthetic):
  pings.csv        what the billing system receives, in ARRIVAL order
  answer_key.csv   the fault label of every ping (never given to billing)
  truth_road.csv   true distance driven along the road inside the zone,
                   per driver per local day (from the route itself)
  README.md        labels the data as synthetic and records the seed
"""
import argparse
import bisect
import csv
import math
import random
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import world as W

SHIFT_DATE = "2026-03-10"          # fictional
GENUINE_PER_DRIVER = 450           # one genuine timestamp every 10 s
FAULT_COUNTS = {                   # per driver
    "noise_offroad": 12,
    "jump_speed": 6,
    "duplicate_exact": 10,
    "duplicate_shifted": 10,
    "late": 8,
    "out_of_order": 12,
}
# driver, route, shift start (UTC). 00:00 UTC+9 == 15:00 UTC.
DRIVERS = [
    ("DRV-01", "R2", "14:20:00"),   # crosses the cut; one ping lands EXACTLY on 15:00:00
    ("DRV-02", "R4", "14:50:07"),   # crosses the cut mid-segment; uses the curve
    ("DRV-03", "R3", "13:00:00"),   # before the cut only
    ("DRV-04", "R1", "15:10:00"),   # after the cut only; route never enters zone
    ("DRV-05", "R5", "14:05:04"),   # crosses the cut mid-segment
]


def routes():
    arc = W.arc_points()
    return {
        "R1": [(0, 0), (3000, 0), (3000, 3000), (0, 3000), (0, 0)],
        "R2": [(0, 1000), (3000, 1000), (3000, 2000), (0, 2000), (0, 1000)],
        "R3": [(1000, 0), (1000, 3000), (2000, 3000), (2000, 0), (1000, 0)],
        "R4": [(0, 0)] + arc + [(3000, 2000), (2000, 2000), (1000, 1000), (0, 0)],
        "R5": [(0, 1000), (1000, 1000), (1000, 2000), (2000, 2000), (2000, 1000),
               (3000, 1000), (3000, 0), (0, 0), (0, 1000)],
    }


class Polyline:
    def __init__(self, pts):
        self.pts = [(float(x), float(y)) for x, y in pts]
        self.cum = [0.0]
        for a, b in zip(self.pts, self.pts[1:]):
            self.cum.append(self.cum[-1] + math.dist(a, b))
        self.length = self.cum[-1]

    def at(self, s):
        s %= self.length
        i = min(bisect.bisect_right(self.cum, s) - 1, len(self.pts) - 2)
        a, b = self.pts[i], self.pts[i + 1]
        f = (s - self.cum[i]) / (self.cum[i + 1] - self.cum[i])
        return a[0] + f * (b[0] - a[0]), a[1] + f * (b[1] - a[1])


def check_routes_on_roads(rts):
    worst = 0.0
    for pts in rts.values():
        for a, b in zip(pts, pts[1:]):
            for k in range(101):
                f = k / 100
                worst = max(worst, W.distance_to_road(a[0] + f * (b[0] - a[0]),
                                                      a[1] + f * (b[1] - a[1])))
    assert worst < 1e-6, f"route leaves the road network by {worst} m"
    return worst


def kmh(d_m, dt_s):
    return d_m / dt_s * 3.6


def gen_driver(rng, driver_id, route_pts, start_hms):
    poly = Polyline(route_pts)
    speed = rng.uniform(30.0, 60.0) / 3.6           # m/s, constant per driver
    s0 = rng.uniform(0.0, poly.length)
    start = datetime.fromisoformat(f"{SHIFT_DATE}T{start_hms}+00:00")
    n = GENUINE_PER_DRIVER
    true = [(start + timedelta(seconds=10 * i), poly.at(s0 + speed * 10 * i))
            for i in range(n)]

    # Choose fault positions. First/last two pings stay clean; replaced pings
    # (noise/jump) are never adjacent, so each has genuine neighbours.
    cand = list(range(2, n - 2))
    rng.shuffle(cand)
    replaced = {}
    for fault in ("noise_offroad", "jump_speed"):
        need = FAULT_COUNTS[fault]
        for i in cand:
            if need == 0:
                break
            if i in replaced or i - 1 in replaced or i + 1 in replaced:
                continue
            replaced[i] = fault
            need -= 1
        assert need == 0
    rest = [i for i in cand if i not in replaced]
    picks, k = {}, 0
    for fault in ("duplicate_exact", "duplicate_shifted", "late", "out_of_order"):
        for i in rest[k:k + FAULT_COUNTS[fault]]:
            picks[i] = fault
        k += FAULT_COUNTS[fault]

    rows = []
    for i, (ts, (x, y)) in enumerate(true):
        recv = ts + timedelta(seconds=rng.uniform(0.5, 3.0))
        row = dict(driver_id=driver_id, ts=ts, x=x, y=y, recv=recv, fault="ok",
                   counts=1, key=(driver_id, i), source_key=None, note="")
        f = replaced.get(i) or picks.get(i)
        prev_xy, next_xy = true[i - 1][1], true[i + 1][1] if i + 1 < n else None

        if f == "noise_offroad":
            for _ in range(10000):
                ang, mag = rng.uniform(0, 2 * math.pi), rng.uniform(120.0, 250.0)
                nx, ny = x + mag * math.cos(ang), y + mag * math.sin(ang)
                d = W.distance_to_road(nx, ny)
                sp = max(kmh(math.dist((nx, ny), prev_xy), 10),
                         kmh(math.dist((nx, ny), next_xy), 10))
                if d > 110.0 and sp < 140.0:     # off-road only, NOT a speed fault
                    break
            else:
                raise RuntimeError("could not place noise ping")
            row.update(x=nx, y=ny, fault=f, counts=0)
        elif f == "jump_speed":
            for _ in range(10000):
                (ax, ay), (bx, by) = rng.choice(W.ROADS)
                u = rng.random()
                jx, jy = ax + u * (bx - ax), ay + u * (by - ay)
                if min(math.dist((jx, jy), prev_xy), math.dist((jx, jy), next_xy)) > 500.0:
                    break                          # on a road, but > 180 km/h away
            else:
                raise RuntimeError("could not place jump ping")
            row.update(x=jx, y=jy, fault=f, counts=0)
        elif f == "late":
            row.update(recv=ts + timedelta(seconds=rng.uniform(300, 1800)), fault=f)
        elif f == "out_of_order":
            # arrives after the NEXT ping (next ping arrives at ts+10.5..13 s)
            row.update(recv=ts + timedelta(seconds=rng.uniform(14, 19)), fault=f)
        rows.append(row)

        if f in ("duplicate_exact", "duplicate_shifted"):
            row["fault"] = "ok"                    # the original stays genuine
            dup = dict(row, fault=f, counts=0, key=None, source_key=row["key"],
                       recv=row["recv"] + timedelta(seconds=rng.uniform(1, 60)))
            if f == "duplicate_shifted":
                ang, mag = rng.uniform(0, 2 * math.pi), rng.uniform(3.0, 15.0)
                dup.update(x=x + mag * math.cos(ang), y=y + mag * math.sin(ang))
            rows.append(dup)

    # True distance along the road inside the zone, between the first and the
    # last genuine timestamp, integrated in 0.1 s steps (midpoint rule).
    truth = defaultdict(float)
    t_first = start.timestamp()
    step = 0.1
    for k2 in range(round(10 * (n - 1) / step)):
        tm = t_first + (k2 + 0.5) * step
        px, py = poly.at(s0 + speed * (tm - t_first))
        if W.point_in_polygon(px, py):
            truth[W.local_date(tm)] += speed * step
    return rows, truth, speed * 3.6


def write_outputs(out, rows, truths, speeds, seed):
    out.mkdir(parents=True, exist_ok=True)
    rows.sort(key=lambda r: (r["recv"], r["driver_id"], r["ts"]))

    # Label by OBSERVED effect, not intent: a ping meant to be late/out_of_order
    # that did not actually arrive after a later-timestamped ping of the same
    # driver (e.g. because its successor was itself late) is "delayed_in_order".
    # Duplicate copies are ignored here, as a first-arrival dedup would.
    latest = {}
    for r in rows:
        if r["fault"].startswith("duplicate_"):
            continue
        drv = r["driver_id"]
        overtaken = r["ts"] < latest.get(drv, r["ts"])
        if r["fault"] in ("late", "out_of_order") and not overtaken:
            r["fault"] = "delayed_in_order"
        latest[drv] = max(latest.get(drv, r["ts"]), r["ts"])
    pid = {}
    for n, r in enumerate(rows, 1):
        r["ping_id"] = f"P{n:05d}"
        r["lat"], r["lon"] = (round(v, 7) for v in W.to_latlon(r["x"], r["y"]))
        if r["key"] is not None:
            pid[r["key"]] = r["ping_id"]

    with open(out / "pings.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ping_id", "driver_id", "ts_utc", "lat", "lon", "received_at_utc"])
        for r in rows:
            w.writerow([r["ping_id"], r["driver_id"], W.fmt_utc(r["ts"]),
                        f"{r['lat']:.7f}", f"{r['lon']:.7f}", W.fmt_utc(r["recv"], ms=True)])

    with open(out / "answer_key.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ping_id", "driver_id", "ts_utc", "fault",
                    "counts_for_expected", "duplicate_of"])
        for r in rows:
            w.writerow([r["ping_id"], r["driver_id"], W.fmt_utc(r["ts"]), r["fault"],
                        r["counts"], pid[r["source_key"]] if r["source_key"] else ""])

    with open(out / "truth_road.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["driver_id", "local_date", "true_road_in_zone_m"])
        for drv, by_day in truths.items():
            for day in sorted(by_day):
                w.writerow([drv, day, f"{by_day[day]:.3f}"])

    (out / "README.md").write_text(
        "# SYNTHETIC DATA - NOT REAL\n\n"
        "Every ping, driver id, road and zone in this folder is invented by\n"
        f"`python -m gpsqa.generate --seed {seed}`. The coordinates sit around a\n"
        "FICTIONAL origin in open ocean (35.0N, 150.0E). No real person, vehicle,\n"
        "place or company is represented.\n\n"
        "- `pings.csv`: input for billing, in arrival order\n"
        "- `answer_key.csv`: fault label per ping (do not feed to billing)\n"
        "- `truth_road.csv`: true road distance in zone per driver/local day\n"
        "- `expected_km.csv`: written by `python -m gpsqa.expected`\n\n"
        "Driver speeds (km/h, constant per driver): "
        + ", ".join(f"{d}={s:.2f}" for d, s in speeds.items()) + "\n",
        encoding="utf-8")


def verify(out):
    """Re-read the written files and measure that each fault is what it claims."""
    pings = {r["ping_id"]: r for r in csv.DictReader(open(out / "pings.csv", newline=""))}
    key = list(csv.DictReader(open(out / "answer_key.csv", newline="")))
    print(f"rows in pings.csv: {len(pings)}")
    print("fault counts:", dict(sorted(Counter(k["fault"] for k in key).items())))
    print("rows per driver:", dict(sorted(Counter(k["driver_id"] for k in key).items())))

    def xy(pid):
        p = pings[pid]
        return W.to_xy(float(p["lat"]), float(p["lon"]))

    def t(pid):
        return W.parse_utc(pings[pid]["ts_utc"]).timestamp()

    genuine = defaultdict(list)
    for k in key:
        if k["counts_for_expected"] == "1":
            genuine[k["driver_id"]].append((t(k["ping_id"]), k["ping_id"]))
    for g in genuine.values():
        g.sort()

    m = defaultdict(list)
    for k in key:
        pid, f = k["ping_id"], k["fault"]
        x, y = xy(pid)
        if f in ("ok", "late", "out_of_order", "delayed_in_order"):
            m["genuine dist_to_road"].append(W.distance_to_road(x, y))
        elif f in ("noise_offroad", "jump_speed"):
            g = genuine[k["driver_id"]]
            i = bisect.bisect_left(g, (t(pid), ""))
            sp = [kmh(math.dist((x, y), xy(g[j][1])), abs(t(pid) - g[j][0])) for j in (i - 1, i)]
            m[f + " dist_to_road"].append(W.distance_to_road(x, y))
            m[f + " implied_kmh"].extend(sp)
        elif f == "duplicate_shifted":
            m["duplicate_shifted offset"].append(math.dist((x, y), xy(k["duplicate_of"])))
        elif f == "duplicate_exact":
            m["duplicate_exact offset"].append(math.dist((x, y), xy(k["duplicate_of"])))
    for g in genuine.values():
        for (t0, a), (t1, b) in zip(g, g[1:]):
            m["genuine consecutive implied_kmh"].append(kmh(math.dist(xy(a), xy(b)), t1 - t0))
    for name in sorted(m):
        v = m[name]
        print(f"  {name:36s} n={len(v):4d} min={min(v):9.3f} max={max(v):9.3f}")

    # arrival-order inversions: ping arrives after a later-timestamped ping
    inv = Counter()
    seen = defaultdict(float)
    by_pid = {k["ping_id"]: k for k in key}
    for pid in pings:                         # file order == arrival order
        k = by_pid[pid]
        if t(pid) < seen[k["driver_id"]]:
            inv[k["fault"]] += 1
        seen[k["driver_id"]] = max(seen[k["driver_id"]], t(pid))
    print("pings arriving after a later-timestamped ping, by fault:", dict(sorted(inv.items())))
    late_cross = sum(
        1 for k in key if k["fault"] == "late"
        and W.local_date(t(k["ping_id"])) != W.local_date(
            W.parse_utc(pings[k["ping_id"]]["received_at_utc"]).timestamp()))
    print("late pings that arrive on a later local day than their timestamp:", late_cross)
    on_cut = [k["ping_id"] for k in key
              if W.parse_utc(k["ts_utc"]).astimezone(W.LOCAL_TZ).strftime("%H:%M:%S") == "00:00:00"]
    print("pings timestamped exactly at 00:00:00 UTC+9:", on_cut)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seed", type=int, default=20260310)
    ap.add_argument("--out", default="data")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    rts = routes()
    print(f"routes on road network: max deviation {check_routes_on_roads(rts):.2e} m")

    rows, truths, speeds = [], {}, {}
    for drv, rname, start in DRIVERS:
        r, truth, sp = gen_driver(rng, drv, rts[rname], start)
        rows += r
        truths[drv], speeds[drv] = truth, sp
    out = Path(args.out)
    write_outputs(out, rows, truths, speeds, args.seed)

    # projection checks
    worst = max(math.dist((r["x"], r["y"]), W.to_xy(r["lat"], r["lon"])) for r in rows)
    print(f"projection round-trip after 7-decimal rounding: max error {worst:.4f} m")
    for a, b in [((0, 0), (3000, 0)), ((0, 0), (0, 3000)), ((0, 0), (3000, 3000))]:
        hv = W.haversine_m(*W.to_latlon(*a), *W.to_latlon(*b))
        print(f"  {a}->{b}: planar {math.dist(a, b):.4f} m, haversine {hv:.4f} m, "
              f"diff {hv - math.dist(a, b):+.4f} m")
    print("driver speeds km/h:", {d: round(s, 2) for d, s in speeds.items()})
    verify(out)
    print(f"wrote {out / 'pings.csv'}, answer_key.csv, truth_road.csv, README.md")


if __name__ == "__main__":
    main()
