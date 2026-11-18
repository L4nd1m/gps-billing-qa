#!/usr/bin/env python3
"""QA checker for GPS billable distance. Works on SYNTHETIC data only.

Usage:
    python qa_check.py [--data data] [--before legacy] [--after fixed]
                       [--tolerance-m 1.0] [--report reports/qa_report.html]

What it does:
  1. Data checks on pings.csv (no answer key used): duplicates, arrival order,
     off-road (> 100 m), speed (> 150 km/h from the last accepted ping), zone
     boundary crossings, the 00:00 UTC+9 day cut.
  2. Scores every ping-level check against answer_key.csv (false positives /
     false negatives, with ping ids).
  3. Compares billing calculations with expected km per driver per local day:
     "before" = gpsqa.<before>, "after" = gpsqa.<after> if that module exists.
     A calculation module must expose billable(pings) -> {(driver, day): m}.
  4. Writes one self-contained HTML report (inline CSS + SVG, no scripts).

Exit codes:
  0  every check passed
  1  a detector disagrees with the answer key, the checker's own km disagree
     with expected km, or the calculation under test ("after" if present,
     otherwise "before") is off by more than the tolerance on any driver-day
  2  bad arguments or missing input files
"""
import argparse
import csv
import html
import importlib
import importlib.util
import math
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gpsqa import expected as E
from gpsqa import world as W

DETECTORS = [  # id, label, answer-key faults it should catch
    ("duplicate", "Duplicate (same driver + timestamp), first arrival kept",
     {"duplicate_exact", "duplicate_shifted"}),
    ("duplicate_exact", " of which: exact copy", {"duplicate_exact"}),
    ("duplicate_shifted", " of which: different coordinates", {"duplicate_shifted"}),
    ("arrived_out_of_order", "Arrived after a later-timestamped ping (same driver)",
     {"late", "out_of_order"}),
    ("off_road", f"More than {W.NOISE_OFFROAD_M:.0f} m from any road", {"noise_offroad"}),
    ("speed", f"Above {W.MAX_SPEED_KMH:.0f} km/h from the last accepted ping", {"jump_speed"}),
]
GENUINE_FAULTS = {"ok", "late", "out_of_order", "delayed_in_order"}


# --------------------------------------------------------------------------- load
def load(data):
    for name in ("pings.csv", "answer_key.csv"):
        if not (data / name).exists():
            raise FileNotFoundError(f"missing {data / name}; run python -m gpsqa.generate first")
    pings = list(csv.DictReader(open(data / "pings.csv", newline="")))
    key = {r["ping_id"]: r for r in csv.DictReader(open(data / "answer_key.csv", newline=""))}
    return pings, key


def ts(p):
    return W.parse_utc(p["ts_utc"]).timestamp()


def utc_date(t):
    return datetime.fromtimestamp(t, timezone.utc).date().isoformat()


def utc_cuts_between(t0, t1):
    d = datetime.fromtimestamp(t0, timezone.utc).date()
    out = []
    while True:
        d += timedelta(days=1)
        c = datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()
        if c >= t1:
            return out
        if c > t0:
            out.append(c)


# ---------------------------------------------------------------- 1. detectors
def detect(pings):
    """Ping-level checks. Uses NO answer key. Returns (flags, clean) where
    clean[driver] = timestamp-sorted [(t, x, y, ping_id)] that survive."""
    flags = {d: set() for d, _, _ in DETECTORS}
    by_driver = defaultdict(list)
    for p in pings:                                   # file order == arrival order
        by_driver[p["driver_id"]].append(p)

    clean = {}
    for drv, ps in by_driver.items():
        first, kept = {}, []
        for p in ps:                                  # duplicates
            f = first.get(p["ts_utc"])
            if f is not None:
                flags["duplicate"].add(p["ping_id"])
                same = (p["lat"], p["lon"]) == (f["lat"], f["lon"])
                flags["duplicate_exact" if same else "duplicate_shifted"].add(p["ping_id"])
                continue
            first[p["ts_utc"]] = p
            kept.append(p)

        latest = -math.inf                            # arrival order
        for p in kept:
            if ts(p) < latest:
                flags["arrived_out_of_order"].add(p["ping_id"])
            latest = max(latest, ts(p))

        pts = []                                      # off-road
        for p in kept:
            x, y = W.to_xy(float(p["lat"]), float(p["lon"]))
            if W.distance_to_road(x, y) > W.NOISE_OFFROAD_M:
                flags["off_road"].add(p["ping_id"])
                continue
            pts.append((ts(p), x, y, p["ping_id"]))
        pts.sort()

        accepted = []                                 # speed vs last ACCEPTED ping
        for q in pts:
            if accepted:
                a = accepted[-1]
                kmh = math.dist(a[1:3], q[1:3]) / (q[0] - a[0]) * 3.6
                if kmh > W.MAX_SPEED_KMH:
                    flags["speed"].add(q[3])
                    continue
            accepted.append(q)
        clean[drv] = accepted
    return flags, clean


def score(flags, clean, key):
    rows = []
    for det, label, faults in DETECTORS:
        truth = {pid for pid, k in key.items() if k["fault"] in faults}
        got = flags[det]
        rows.append(dict(id=det, label=label, flagged=len(got), truth=len(truth),
                         tp=len(got & truth), fp=sorted(got - truth), fn=sorted(truth - got)))
    kept = {q[3] for pts in clean.values() for q in pts}
    genuine = {pid for pid, k in key.items() if k["fault"] in GENUINE_FAULTS}
    rows.append(dict(id="kept_set", label="Pings kept after all checks = genuine pings in key",
                     flagged=len(kept), truth=len(genuine), tp=len(kept & genuine),
                     fp=sorted(kept - genuine), fn=sorted(genuine - kept)))
    return rows


# ------------------------------------------------- 2. boundary + midnight checks
def segment_checks(clean):
    """Walk the cleaned track. Returns findings and the checker's own
    in-zone metres per (driver, UTC+9 day), cut at zone edges and day cuts."""
    f = dict(segments=0, zone_cross=0, start_only_m=0.0, clipped_m=0.0, start_only_abs_err=0.0,
             day_cross=0, utc_vs_local_m=0.0, on_cut=[], gap_skipped=0)
    km = defaultdict(float)
    for drv, pts in clean.items():
        for d in {W.local_date(t) for t, *_ in pts}:
            km[(drv, d)] += 0.0
        for t, _, _, pid in pts:
            if datetime.fromtimestamp(t, W.LOCAL_TZ).strftime("%H:%M:%S") == "00:00:00":
                f["on_cut"].append(pid)
        for (t0, x0, y0, _), (t1, x1, y1, _) in zip(pts, pts[1:]):
            if t1 - t0 > W.MAX_GAP_S:                 # R7: not billed
                f["gap_skipped"] += 1
                continue
            p, q = (x0, y0), (x1, y1)
            length = W.chord_m(p, q)          # haversine, same as expected km
            zc = W.segment_edge_crossings(p, q)
            lc = W.day_cuts_between(t0, t1)
            cuts = lc + utc_cuts_between(t0, t1)
            bs = sorted({0.0, 1.0, *zc, *[(c - t0) / (t1 - t0) for c in cuts]})
            inside_m = 0.0
            for u, v in zip(bs, bs[1:]):
                mid = (u + v) / 2
                if W.point_in_polygon(x0 + mid * (x1 - x0), y0 + mid * (y1 - y0)):
                    m, tm = length * (v - u), t0 + mid * (t1 - t0)
                    km[(drv, W.local_date(tm))] += m
                    inside_m += m
                    if utc_date(tm) != W.local_date(tm):
                        f["utc_vs_local_m"] += m
            f["segments"] += 1
            if zc:
                start_only = length if W.point_in_polygon(x0, y0) else 0.0
                f["zone_cross"] += 1
                f["start_only_m"] += start_only
                f["clipped_m"] += inside_m
                f["start_only_abs_err"] += abs(start_only - inside_m)
            if lc:
                f["day_cross"] += 1
    return f, dict(km)


# -------------------------------------------------------- 3. billing comparison
def expected_by_day(data):
    out = {}
    for drv, pts in E.load_genuine(data).items():
        tmp = defaultdict(float)
        for (t0, x0, y0), (t1, x1, y1) in zip(pts, pts[1:]):
            E.exact_segment(t0, (x0, y0), t1, (x1, y1), tmp)
        for d in {W.local_date(t) for t, _, _ in pts} | set(tmp):
            out[(drv, d)] = tmp.get(d, 0.0)
    return out


def run_calc(name, pings):
    if importlib.util.find_spec(f"gpsqa.{name}") is None:
        return None
    mod = importlib.import_module(f"gpsqa.{name}")
    if not hasattr(mod, "billable"):
        raise AttributeError(f"gpsqa.{name} has no billable(pings) function")
    return {k: float(v) for k, v in mod.billable(pings).items()}


def mismatches(expected, got, tol):
    keys = set(expected) | set(got)
    return sorted(k for k in keys if abs(got.get(k, 0.0) - expected.get(k, 0.0)) > tol)


# ------------------------------------------------------------------ 4. report
CSS = """
:root{--bg:#fbfaf7;--fg:#1d1d1b;--muted:#6b6a64;--line:#e2dfd6;--card:#ffffff;
--pass:#1f7a4d;--pass-bg:#e5f3ea;--fail:#b3261e;--fail-bg:#fbe7e5;
--s-exp:#8a8778;--s-before:#c4532d;--s-after:#2f6db5;--s-check:#7a5bb5}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#171716;--fg:#ecebe6;
--muted:#a4a297;--line:#34332f;--card:#201f1d;--pass:#6fcf97;--pass-bg:#1b3326;--fail:#ff8a80;
--fail-bg:#3a1f1d;--s-exp:#a9a698;--s-before:#e67a52;--s-after:#6ea3e6;--s-check:#a98ee0}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:980px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:1.6rem;margin:.2em 0}h2{font-size:1.15rem;margin:2em 0 .5em;
border-bottom:1px solid var(--line);padding-bottom:.3em}
.muted{color:var(--muted)}.badge{display:inline-block;padding:.15em .6em;border-radius:999px;
font-weight:600;font-size:.85em}.pass{color:var(--pass);background:var(--pass-bg)}
.fail{color:var(--fail);background:var(--fail-bg)}
.banner{padding:12px 16px;border-radius:10px;margin:16px 0;font-weight:600}
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{border-collapse:collapse;width:100%;font-size:.9rem;background:var(--card)}
th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
tr.total td{font-weight:600}code{font-size:.85em}
.ids{white-space:normal;font-family:ui-monospace,Consolas,monospace;font-size:.8rem;max-width:340px}
ul{padding-left:1.2em}.legend span{display:inline-flex;align-items:center;margin-right:14px}
.legend i{width:12px;height:12px;border-radius:2px;display:inline-block;margin-right:5px}
svg text{fill:var(--fg);font-size:11px}
"""


def fm(v):
    return f"{v:,.3f}"


def badge(ok):
    return f'<span class="badge {"pass" if ok else "fail"}">{"PASS" if ok else "FAIL"}</span>'


def ids(lst, limit=12):
    if not lst:
        return "—"
    s = ", ".join(lst[:limit])
    return html.escape(s + (f" … (+{len(lst) - limit})" if len(lst) > limit else ""))


def chart(keys, series):
    """Horizontal grouped bars; series = [(name, css var, {key: m})]."""
    label_w, bar_w, bh, gap = 150, 560, 12, 10
    vmax = max([1.0] + [s[2].get(k, 0.0) for k in keys for s in series])
    group_h = len(series) * (bh + 2) + gap
    h = len(keys) * group_h + 10
    out = [f'<svg viewBox="0 0 {label_w + bar_w + 90} {h}" width="100%" role="img" '
           f'aria-label="Billable metres per driver and day">']
    for i, k in enumerate(keys):
        y0 = 5 + i * group_h
        out.append(f'<text x="0" y="{y0 + len(series) * (bh + 2) / 2 + 4}">'
                   f'{html.escape(k[0] + " " + k[1])}</text>')
        for j, (_, var, vals) in enumerate(series):
            v = vals.get(k, 0.0)
            w = bar_w * v / vmax
            y = y0 + j * (bh + 2)
            out.append(f'<rect x="{label_w}" y="{y}" width="{w:.1f}" height="{bh}" rx="2" '
                       f'fill="var({var})"/><text x="{label_w + w + 4:.1f}" y="{y + bh - 2}">'
                       f'{v:,.0f}</text>')
    out.append("</svg>")
    return "".join(out)


def billing_table(keys, expected, calcs, tol):
    head = "".join(f'<th class="n">{html.escape(n)} m</th><th class="n">{html.escape(n)} − exp</th>'
                   f"<th>{html.escape(n)}</th>" for n, _ in calcs)
    rows = []
    totals = defaultdict(lambda: defaultdict(float))
    for k in keys:
        e = expected.get(k, 0.0)
        totals[k[0]]["exp"] += e
        cells = ""
        for n, got in calcs:
            g = got.get(k, 0.0)
            totals[k[0]][n] += g
            cells += f'<td class="n">{fm(g)}</td><td class="n">{g - e:+,.3f}</td><td>{badge(abs(g - e) <= tol)}</td>'
        rows.append(f"<tr><td>{k[0]}</td><td>{k[1]}</td><td class='n'>{fm(e)}</td>{cells}</tr>")
    for drv in sorted(totals):
        e = totals[drv]["exp"]
        cells = "".join(f'<td class="n">{fm(totals[drv][n])}</td><td class="n">{totals[drv][n] - e:+,.3f}</td><td></td>'
                        for n, _ in calcs)
        rows.append(f"<tr class='total'><td>{drv}</td><td>all days</td><td class='n'>{fm(e)}</td>{cells}</tr>")
    return (f'<div class="scroll"><table><tr><th>Driver</th><th>Day</th><th class="n">expected m</th>{head}</tr>'
            + "".join(rows) + "</table></div>")


def write_report(path, ctx):
    s = ctx["score"]
    seg = ctx["seg"]
    det_rows = "".join(
        f"<tr><td>{html.escape(r['label'])}</td><td class='n'>{r['flagged']}</td><td class='n'>{r['truth']}</td>"
        f"<td class='n'>{r['tp']}</td><td class='n'>{len(r['fp'])}</td><td class='n'>{len(r['fn'])}</td>"
        f"<td>{badge(not r['fp'] and not r['fn'])}</td><td class='ids'>FP: {ids(r['fp'])}<br>FN: {ids(r['fn'])}</td></tr>"
        for r in s)
    keys = ctx["keys"]
    calcs = ctx["calcs"]
    series = [("expected", "--s-exp", ctx["expected"])]
    series += [(n, "--s-before" if i == 0 else "--s-after", g) for i, (n, g) in enumerate(calcs)]
    legend = "".join(f'<span><i style="background:var({v})"></i>{html.escape(n)}</span>' for n, v, _ in series)
    fail_items = "".join(f"<li>{html.escape(x)}</li>" for x in ctx["failures"]) or "<li>none</li>"
    after_note = ("" if ctx["after_present"] else
                  f"<p class='muted'>No <code>gpsqa/{html.escape(ctx['after_name'])}.py</code> yet, so there is no "
                  "“after” column. The calculation under test is “before”.</p>")
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>GPS Billing QA</title><style>{CSS}</style></head><body><main>
<p class="muted">SYNTHETIC DATA — fictional drivers, roads and zone. Generated by <code>qa_check.py</code>.</p>
<h1>GPS Billing QA</h1>
<div class="banner {'pass' if not ctx['failures'] else 'fail'}">Overall: {'PASS' if not ctx['failures'] else 'FAIL'}
 — exit code {0 if not ctx['failures'] else 1}</div>
<p>Input: <code>{html.escape(str(ctx['data']))}</code> — {ctx['n_pings']:,} pings, {ctx['n_drivers']} drivers.
Calculation under test: <code>gpsqa.{html.escape(ctx['under_test'])}</code>. Tolerance: {ctx['tol']} m per driver-day.</p>
<h3>Why it failed</h3><ul>{fail_items}</ul>

<h2>1. Data checks vs answer key</h2>
<p class="muted">Detectors read only <code>pings.csv</code>. The answer key is used only to score them.
FP = flagged but not a fault in the key. FN = fault in the key but not flagged.</p>
<div class="scroll"><table><tr><th>Check</th><th class="n">flagged</th><th class="n">in key</th>
<th class="n">TP</th><th class="n">FP</th><th class="n">FN</th><th></th><th>ping ids</th></tr>{det_rows}</table></div>

<h2>2. Zone boundary and day cut (on the cleaned track)</h2>
<div class="scroll"><table>
<tr><td>Segments checked</td><td class="n">{seg['segments']:,}</td></tr>
<tr><td>Segments not billed: gap over {W.MAX_GAP_S:.0f} s (R7)</td><td class="n">{seg['gap_skipped']:,}</td></tr>
<tr><td>Segments crossing the zone edge</td><td class="n">{seg['zone_cross']:,}</td></tr>
<tr><td> in-zone metres on those, cut at the edge (correct)</td><td class="n">{fm(seg['clipped_m'])}</td></tr>
<tr><td> metres on those if zone is tested at the start point only</td><td class="n">{fm(seg['start_only_m'])}</td></tr>
<tr><td> absolute error of start-point-only, summed</td><td class="n">{fm(seg['start_only_abs_err'])}</td></tr>
<tr><td>Segments crossing 00:00 UTC+9 (15:00 UTC)</td><td class="n">{seg['day_cross']}</td></tr>
<tr><td>Pings exactly at 00:00:00 UTC+9</td><td class="n">{len(seg['on_cut'])} ({ids(seg['on_cut'])})</td></tr>
<tr><td>In-zone metres whose UTC date differs from the UTC+9 date</td><td class="n">{fm(seg['utc_vs_local_m'])}</td></tr>
</table></div>
<p>Checker's own km (cleaned track, cut at edge and day) vs expected km:
{badge(not ctx['checker_mismatch'])} {len(ctx['checker_mismatch'])} driver-days off by more than {ctx['tol']} m.</p>

<h2>3. Billable metres per driver per day</h2>
<p class="muted">Expected = ideal-ping km from the answer key, by UTC+9 day.
A calculation that labels days by UTC date shows up here as metres on the wrong day.</p>
{after_note}
{billing_table(keys, ctx['expected'], calcs, ctx['tol'])}
<p class="legend">{legend}</p>
<div class="scroll">{chart(keys, series)}</div>

<h2>4. Assumptions and limits</h2><ul>
<li>Duplicates: the first copy to arrive is kept; the others are flagged.</li>
<li>Speed is measured against the last accepted ping. If the very first ping of a driver were bad, every later ping could be rejected; this dataset keeps the first two pings clean, so that case is not exercised.</li>
<li>Off-road = distance to the nearest road of the whole network, not only the driver's own route.</li>
<li>Expected km joins genuine pings with straight lines, so it is below true road distance at corners (accepted definition).</li>
<li>The answer key exists only for synthetic data; on real data only the billing comparison against a trusted reference could run.</li>
</ul></main></body></html>"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(doc, encoding="utf-8")


# ---------------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(description="QA checker for GPS billable distance (synthetic data).")
    ap.add_argument("--data", default="data")
    ap.add_argument("--before", default="legacy")
    ap.add_argument("--after", default="fixed")
    ap.add_argument("--tolerance-m", type=float, default=1.0)
    ap.add_argument("--report", default="reports/qa_report.html")
    args = ap.parse_args(argv)
    data, tol = Path(args.data), args.tolerance_m

    try:
        pings, key = load(data)
        before = run_calc(args.before, pings)
        if before is None:
            raise FileNotFoundError(f"gpsqa/{args.before}.py not found")
        after = run_calc(args.after, pings)
    except (FileNotFoundError, AttributeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    flags, clean = detect(pings)
    sc = score(flags, clean, key)
    seg, checker_km = segment_checks(clean)
    expected = expected_by_day(data)

    failures = []
    for r in sc:
        if r["fp"] or r["fn"]:
            failures.append(f"detector '{r['id']}': {len(r['fp'])} false positives, {len(r['fn'])} false negatives")
    checker_mismatch = mismatches(expected, checker_km, tol)
    if checker_mismatch:
        failures.append(f"checker km differ from expected on {len(checker_mismatch)} driver-days")
    calcs = [(f"before ({args.before})", before)]
    under_test, got = args.before, before
    if after is not None:
        calcs.append((f"after ({args.after})", after))
        under_test, got = args.after, after
    bad = mismatches(expected, got, tol)
    for k in bad:
        failures.append(f"gpsqa.{under_test} {k[0]} {k[1]}: {got.get(k, 0.0):,.3f} m, "
                        f"expected {expected.get(k, 0.0):,.3f} m ({got.get(k, 0.0) - expected.get(k, 0.0):+,.3f})")

    keys = sorted(set(expected) | {k for _, g in calcs for k in g})
    write_report(Path(args.report), dict(
        score=sc, seg=seg, keys=keys, calcs=calcs, expected=expected, failures=failures,
        checker_mismatch=checker_mismatch, data=data, tol=tol, n_pings=len(pings),
        n_drivers=len({p["driver_id"] for p in pings}), under_test=under_test,
        after_present=after is not None, after_name=args.after))

    # console summary
    print(f"{'check':24} {'flagged':>7} {'in key':>6} {'TP':>5} {'FP':>4} {'FN':>4}")
    for r in sc:
        print(f"{r['id']:24} {r['flagged']:7} {r['truth']:6} {r['tp']:5} {len(r['fp']):4} {len(r['fn']):4}")
    print(f"\nsegments {seg['segments']}, gaps over {W.MAX_GAP_S:.0f} s skipped {seg['gap_skipped']}, "
          f"zone-edge crossings {seg['zone_cross']}, "
          f"day-cut crossings {seg['day_cross']}, pings on cut {seg['on_cut']}")
    print(f"start-point-only zone test, abs error on crossing segments: {seg['start_only_abs_err']:,.3f} m")
    print(f"in-zone metres whose UTC date != UTC+9 date: {seg['utc_vs_local_m']:,.3f} m")
    print(f"checker km vs expected: {len(checker_mismatch)} driver-days off by > {tol} m")
    print(f"\n{'driver':7} {'day':10} {'expected':>11} " + " ".join(f"{n:>22}" for n, _ in calcs))
    for k in keys:
        e = expected.get(k, 0.0)
        print(f"{k[0]:7} {k[1]:10} {e:11.3f} "
              + " ".join(f"{g.get(k, 0.0):11.3f} ({g.get(k, 0.0) - e:+9.3f})" for _, g in calcs))
    print(f"\nreport: {args.report}")
    print("RESULT:", "PASS" if not failures else "FAIL")
    for f_ in failures:
        print("  -", f_)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
