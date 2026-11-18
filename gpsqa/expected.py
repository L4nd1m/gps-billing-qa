"""Expected billable distance ("ideal-ping km"), computed from the answer key.

Usage:  python -m gpsqa.expected --data data

Definition (agreed in step 3):
  * use only pings the answer key marks counts_for_expected = 1 (genuine);
  * order them by their own timestamp;
  * join consecutive genuine pings with straight lines (a replaced noise/jump
    ping is bridged from the previous to the next genuine ping); each line's
    length is the haversine distance between its two pings (step 7 decision);
    zone-edge and day-cut fractions are found in projected metres;
  * count only the part of each line inside the zone (cut at the zone edge);
  * split a line that crosses 00:00 UTC+9 in proportion to time;
  * a line spanning more than 60 s (R7, world.MAX_GAP_S) is not billed.

This module applies NO filters of its own: it trusts the answer key. Billing
code must never import it. Two independent methods are run and compared:
  exact     - cut each line at zone-edge crossings and day cuts
  sampled   - walk each line in <= 1 m steps and test every step's midpoint
"""
import argparse
import csv
import math
from collections import defaultdict
from pathlib import Path

from . import world as W


def load_genuine(data):
    key = {r["ping_id"]: r for r in csv.DictReader(open(data / "answer_key.csv", newline=""))}
    by_driver = defaultdict(list)
    for p in csv.DictReader(open(data / "pings.csv", newline="")):
        if key[p["ping_id"]]["counts_for_expected"] == "1":
            x, y = W.to_xy(float(p["lat"]), float(p["lon"]))
            by_driver[p["driver_id"]].append((W.parse_utc(p["ts_utc"]).timestamp(), x, y))
    for drv, pts in by_driver.items():
        pts.sort()
        ts = [p[0] for p in pts]
        assert len(ts) == len(set(ts)), f"{drv}: two genuine pings share a timestamp"
    return by_driver


def exact_segment(t0, p, t1, q, out):
    if t1 - t0 > W.MAX_GAP_S:
        return False, False
    breaks = {0.0, 1.0}
    zone_cuts = W.segment_edge_crossings(p, q)
    day_cuts = [(c - t0) / (t1 - t0) for c in W.day_cuts_between(t0, t1)]
    breaks.update(zone_cuts)
    breaks.update(day_cuts)
    length = W.chord_m(p, q)
    bs = sorted(breaks)
    for a, b in zip(bs, bs[1:]):
        mid = (a + b) / 2
        if W.point_in_polygon(p[0] + mid * (q[0] - p[0]), p[1] + mid * (q[1] - p[1])):
            out[W.local_date(t0 + mid * (t1 - t0))] += length * (b - a)
    return len(zone_cuts) > 0, len(day_cuts) > 0


def sampled_segment(t0, p, t1, q, out, step_m=1.0):
    if t1 - t0 > W.MAX_GAP_S:
        return
    length = W.chord_m(p, q)
    n = max(1, math.ceil(length / step_m))
    for k in range(n):
        mid = (k + 0.5) / n
        if W.point_in_polygon(p[0] + mid * (q[0] - p[0]), p[1] + mid * (q[1] - p[1])):
            out[W.local_date(t0 + mid * (t1 - t0))] += length / n


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", default="data")
    args = ap.parse_args()
    data = Path(args.data)

    genuine = load_genuine(data)
    truth = defaultdict(dict)
    for r in csv.DictReader(open(data / "truth_road.csv", newline="")):
        truth[r["driver_id"]][r["local_date"]] = float(r["true_road_in_zone_m"])

    rows = []
    stats = defaultdict(int)
    for drv in sorted(genuine):
        exact, sampled = defaultdict(float), defaultdict(float)
        pts = genuine[drv]
        for (t0, x0, y0), (t1, x1, y1) in zip(pts, pts[1:]):
            crosses_zone, crosses_day = exact_segment(t0, (x0, y0), t1, (x1, y1), exact)
            sampled_segment(t0, (x0, y0), t1, (x1, y1), sampled)
            stats["segments"] += 1
            stats["segments crossing zone edge"] += crosses_zone
            stats["segments crossing 00:00 UTC+9"] += crosses_day
            stats["bridged segments (dt > 10 s)"] += (t1 - t0) > W.PING_INTERVAL_S
        days = sorted({W.local_date(t) for t, _, _ in pts} | set(truth[drv]))
        for day in days:
            rows.append((drv, day, exact[day], sampled[day], truth[drv].get(day, 0.0)))

    with open(data / "expected_km.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["driver_id", "local_date", "expected_km", "expected_m",
                    "crosscheck_sampled_m", "true_road_m"])
        for drv, day, e, s, tr in rows:
            w.writerow([drv, day, f"{e / 1000:.4f}", f"{e:.3f}", f"{s:.3f}", f"{tr:.3f}"])

    for k, v in stats.items():
        print(f"{k}: {v}")
    print()
    print(f"{'driver':7} {'local_date':10} {'expected_m':>11} {'sampled_m':>11} "
          f"{'exact-samp':>10} {'true_road_m':>11} {'exp-true':>9}")
    tot = [0.0, 0.0, 0.0]
    for drv, day, e, s, tr in rows:
        print(f"{drv:7} {day:10} {e:11.3f} {s:11.3f} {e - s:10.3f} {tr:11.3f} {e - tr:9.3f}")
        tot = [tot[0] + e, tot[1] + s, tot[2] + tr]
    print(f"{'TOTAL':18} {tot[0]:11.3f} {tot[1]:11.3f} {tot[0] - tot[1]:10.3f} "
          f"{tot[2]:11.3f} {tot[0] - tot[2]:9.3f}")
    print(f"wrote {data / 'expected_km.csv'}")


if __name__ == "__main__":
    main()
