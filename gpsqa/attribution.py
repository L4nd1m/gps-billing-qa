"""Root-cause attribution: which legacy bug causes how much error?

Usage:  python -m gpsqa.attribution --data data

Step 1  PROOF: replica with no fixes must equal gpsqa.legacy on every
        driver-day with a difference of exactly 0.0 (exit 1 otherwise).
Step 2  Run all 2^5 = 32 on/off combinations of fixes 1-5.
        error(S) = sum over driver-days of |calc - expected|  (metres)
Step 3  Report, per fix: removed alone, cumulative ladder (order 1,2,3,4,5),
        and Shapley share (average removal over all 120 orders; sums exactly
        to error(no fixes) - error(all fixes)).
Writes data/attribution_combos.csv and data/attribution_shares.csv.
"""
import argparse
import csv
import itertools
import math
import sys
from pathlib import Path

from . import legacy, replica
from qa_check import expected_by_day

NAMES = {1: "sort by timestamp", 2: "no abs(dt): drop stale ping",
         3: "speed check before anchor move", 4: "cut at zone edge",
         5: "day cut at 00:00 UTC+9"}


def l1(calc, expected):
    keys = set(calc) | set(expected)
    return sum(abs(calc.get(k, 0.0) - expected.get(k, 0.0)) for k in keys)


def signed(calc, expected):
    return sum(calc.values()) - sum(expected.values())


def main(argv=None):
    ap = argparse.ArgumentParser(description="Bug attribution for the legacy calculation")
    ap.add_argument("--data", default="data")
    args = ap.parse_args(argv)
    data = Path(args.data)
    pings = list(csv.DictReader(open(data / "pings.csv", newline="")))
    expected = expected_by_day(data)

    # ---- Step 1: exact reproduction proof
    leg = legacy.legacy_billable(pings)
    rep = replica.billable_with(pings, set())
    keys = sorted(set(leg) | set(rep))
    diffs = [(k, rep.get(k, 0.0) - leg.get(k, 0.0)) for k in keys]
    identical = sum(1 for k in keys if k in leg and k in rep and leg[k] == rep[k])
    max_abs = max(abs(d) for _, d in diffs)
    print("STEP 1  replica(no fixes) vs gpsqa.legacy")
    print(f"  driver-days: legacy {len(leg)}, replica {len(rep)}, same keys: {set(leg) == set(rep)}")
    for k, d in diffs:
        print(f"  {k[0]} {k[1]}  legacy {leg.get(k, 0.0):.6f}  replica {rep.get(k, 0.0):.6f}  diff {d!r}")
    print(f"  bit-identical values: {identical}/{len(keys)}; max |diff| = {max_abs!r} m")
    if set(leg) != set(rep) or max_abs != 0.0:
        print("  REPRODUCTION FAILED -- attribution would be meaningless. Stopping.")
        return 1
    print("  REPRODUCED EXACTLY (0.000 m)\n")

    # ---- Step 2: all combinations
    err, sgn, runs = {}, {}, {}
    for r in range(6):
        for combo in itertools.combinations(replica.ALL_FIXES, r):
            s = frozenset(combo)
            runs[s] = replica.billable_with(pings, s)
            err[s], sgn[s] = l1(runs[s], expected), signed(runs[s], expected)
    none, full = frozenset(), frozenset(replica.ALL_FIXES)
    print("STEP 2  all 32 combinations (error = sum |calc - expected| over driver-days)")
    print(f"  {'fixes on':16} {'abs error m':>13} {'total calc-exp m':>17}")
    for s in sorted(err, key=lambda s: (len(s), sorted(s))):
        label = ",".join(map(str, sorted(s))) or "none (legacy)"
        print(f"  {label:16} {err[s]:13.3f} {sgn[s]:+17.3f}")
    with open(data / "attribution_combos.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["fixes_on", "abs_error_m", "signed_total_error_m"])
        for s in sorted(err, key=lambda s: (len(s), sorted(s))):
            w.writerow(["+".join(map(str, sorted(s))) or "none", f"{err[s]:.3f}", f"{sgn[s]:.3f}"])

    # ---- Step 3: attribution
    n = len(replica.ALL_FIXES)
    shap = {}
    for i in replica.ALL_FIXES:
        others = [j for j in replica.ALL_FIXES if j != i]
        tot = 0.0
        for r in range(n):
            for combo in itertools.combinations(others, r):
                s = frozenset(combo)
                wgt = math.factorial(r) * math.factorial(n - r - 1) / math.factorial(n)
                tot += wgt * (err[s] - err[s | {i}])
        shap[i] = tot
    ladder, cur = {}, frozenset()
    for i in replica.ALL_FIXES:
        ladder[i] = err[cur] - err[cur | {i}]
        cur = cur | {i}
    alone = {i: err[none] - err[frozenset({i})] for i in replica.ALL_FIXES}

    total_removable = err[none] - err[full]
    print(f"\nSTEP 3  error with no fixes {err[none]:.3f} m; with all fixes {err[full]:.3f} m; "
          f"removed {total_removable:.3f} m")
    print(f"  {'fix':3} {'rule':32} {'alone':>11} {'ladder 1..5':>12} {'Shapley':>11} {'share':>7}")
    for i in replica.ALL_FIXES:
        print(f"  {i:3} {NAMES[i]:32} {alone[i]:11.3f} {ladder[i]:12.3f} {shap[i]:11.3f} "
              f"{shap[i] / total_removable:7.1%}")
    print(f"  {'':3} {'SUM':32} {sum(alone.values()):11.3f} {sum(ladder.values()):12.3f} "
          f"{sum(shap.values()):11.3f}")
    with open(data / "attribution_shares.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["fix", "rule", "removed_alone_m", "removed_ladder_m", "shapley_m", "shapley_share"])
        for i in replica.ALL_FIXES:
            w.writerow([i, NAMES[i], f"{alone[i]:.3f}", f"{ladder[i]:.3f}", f"{shap[i]:.3f}",
                        f"{shap[i] / total_removable:.4f}"])

    print("\nAll fixes on vs expected, per driver-day:")
    for k in sorted(set(runs[full]) | set(expected)):
        g, e = runs[full].get(k, 0.0), expected.get(k, 0.0)
        print(f"  {k[0]} {k[1]}  all-fixes {g:12.3f}  expected {e:12.3f}  diff {g - e:+.3f}")
    print(f"wrote {data / 'attribution_combos.csv'}, {data / 'attribution_shares.csv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
