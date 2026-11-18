# Directive: GPS billable distance — problem found, explained, fixed, locked in

> **100 % synthetic data and a fictional case** (the brief's suggested case, not a
> live product). Every number below was produced by running the linked code;
> the saved console outputs are linked next to each claim.
> Assumptions and the AI log are in [`intent.md`](intent.md).

## Result in one line

The legacy calculation billed **93,442.007 m** against **73,650.670 m** expected
(**+19,791.337 m**, +26.9 %) and put every metre on the wrong day after
midnight. The fixed calculation matches expected on **all 8 driver-days**
(max difference below 0.000001 m). `qa_check` exits **0**, and **22 tests**
fail on the old behaviour or guard what it already did right, and pass on the fix.

## Links

| What | Link |
|---|---|
| **QA tool** (checker + report, exit 1 on failure) | [`qa_check.py`](qa_check.py) |
| **HTML report** (self-contained, no scripts; download and open, or serve via Pages) | [`reports/qa_report.html`](reports/qa_report.html) |
| QA tool console output | [`reports/qa_check_output.txt`](reports/qa_check_output.txt) |
| **Synthetic data**: pings (input, arrival order) | [`data/pings.csv`](data/pings.csv) |
| Answer key (fault of every ping) | [`data/answer_key.csv`](data/answer_key.csv) |
| Expected km per driver per day | [`data/expected_km.csv`](data/expected_km.csv) |
| True road km (for the corner-cutting gap) | [`data/truth_road.csv`](data/truth_road.csv) |
| Data label and seed | [`data/README.md`](data/README.md) |
| Generator / world model / expected-km calculator | [`gpsqa/generate.py`](gpsqa/generate.py), [`gpsqa/world.py`](gpsqa/world.py), [`gpsqa/expected.py`](gpsqa/expected.py) |
| **Legacy (wrong) calculation**, bugs commented | [`gpsqa/legacy.py`](gpsqa/legacy.py) · output [`legacy_km.csv`](data/legacy_km.csv) · [`console`](reports/legacy_vs_expected_output.txt) |
| **Root cause**: exact replica + 32-combination attribution | [`gpsqa/replica.py`](gpsqa/replica.py), [`gpsqa/attribution.py`](gpsqa/attribution.py) · [`console`](reports/attribution_output.txt) · [`combos`](data/attribution_combos.csv) · [`shares`](data/attribution_shares.csv) |
| **Fix**: corrected calculation, rules R1–R8 switchable | [`gpsqa/fixed.py`](gpsqa/fixed.py) · output [`fixed_km.csv`](data/fixed_km.csv) · [`console`](reports/fixed_vs_expected_output.txt) |
| **Regression tests** | [`tests/test_rules.py`](tests/test_rules.py) · runner [`tests/run_regression.py`](tests/run_regression.py) · [`results`](reports/regression_tests_output.txt) |
| Sabotage (mutation) check of the tests | [`tests/sabotage.py`](tests/sabotage.py) · [`results`](reports/sabotage_output.txt) |

## 1. Problems found, ranked by impact on advertiser and driver

Error = sum over driver-days of |legacy − expected|; total **70,141.099 m**.
"Share" is each bug's Shapley share of the error removed (average over all
orders of fixing, so shares add up exactly). Source: [`attribution_output.txt`](reports/attribution_output.txt).

| Rank | Problem | Share | Removed if fixed alone | Who is hurt |
|---|---|---|---|---|
| 1 | **Day cut at UTC midnight** instead of 00:00 UTC+9 (bug 5) | 70.3 % | 50,349.762 m | Both: daily reports are wrong. Totals are unchanged, so no net money moves, but every km after midnight lands on the previous day |
| 2 | **Pings processed in arrival order** (bug 1) | 23.2 % | 20,091.027 m | Advertiser: late pings create back-and-forth lines that are billed |
| 3 | **`abs()` on the time step** hides backward lines (bug 2) | 23.3 % | 20,481.094 m | Advertiser (same mechanism as rank 2; once pings are sorted it adds nothing) |
| 4 | **Zone tested at the start point only** (bug 4) | −16.6 % | −35,622.911 m (worse alone) | Both directions: lines leaving the zone are billed in full, lines entering are not billed at all. Alone it *increases* the error because it was hiding part of bugs 1+2 |
| 5 | **Anchor moved to a rejected too-fast ping** (bug 3) | −0.2 % | −3,616.149 m (worse alone) | Driver: the good line after a GPS jump is dropped too |

Found later, while testing the fix: a **bad first ping of a shift** becomes the
speed anchor (billed 1,300 m instead of 300 m in a test scenario) → rule R8.

**Fix first: ranks 2+3 (ordering).** Fixing it alone moves the net error from
+19,791.337 m (advertisers overcharged) to −3,469.639 m. The day-cut bug is
bigger per day but moves no money in the totals.
**[CONFIRM: your choice of what to fix first]**

### How each problem was found
- **Answer key + data checks:** `qa_check` detectors (duplicates, arrival order,
  off-road, speed) scored against the answer key: 0 false positives and
  0 false negatives on all checks ([`qa_check_output.txt`](reports/qa_check_output.txt)).
- **Per driver per day comparison:** legacy vs expected showed +6,472 to
  +18,339 m too much on 03-10 and 0 m on 03-11 ([`legacy_vs_expected_output.txt`](reports/legacy_vs_expected_output.txt)).
- **Exact reproduction before blame:** a from-scratch replica with each bug
  switchable reproduces legacy **bit-for-bit on 4/4 driver-days (difference 0.0)**.
  All 32 on/off combinations were then run to attribute the error
  ([`attribution_combos.csv`](data/attribution_combos.csv)).

## 2. The fix (rules in [`gpsqa/fixed.py`](gpsqa/fixed.py))

| Rule | Meaning |
|---|---|
| R1 | Same driver + timestamp counts once; first arrival kept |
| R2 | Process pings by their own timestamp |
| R3 | Ignore a ping more than 100 m from every road; join previous → next |
| R4 | Ignore a ping above 150 km/h from the last **accepted** ping; anchor does not move |
| R5 | Bill only the part of each line inside the zone, cut at the edge |
| R6 | Day = 00:00 UTC+9; a line crossing midnight is split in proportion to time |
| R7 | Do not bill a line spanning more than 60 s |
| R8 | An unconfirmed anchor (first ping) is checked with a one-ping look-ahead |

Each rule switched **off** on the dataset ([`fixed_vs_expected_output.txt`](reports/fixed_vs_expected_output.txt)):
R5 off 208,584.022 m error · R4 off 56,195.104 m · R6 off 50,349.762 m (net 0) ·
R2 off 3,660.606 m · R3 off 3,129.960 m · R1, R7, R8 off 0.000 m.
R1, R7 and R8 change nothing on *this* dataset; the tests below prove they work.
R2 off is small only because R7 drops the long back-and-forth lines (measured:
59,920.616 m with R7 also off).

## 3. Before and after, per driver per day

Source: [`qa_check_output.txt`](reports/qa_check_output.txt). Legacy labels days
by UTC date, so all of its metres sit on 03-10.

| Driver | Day (UTC+9) | Expected m | Before (legacy) m | After (fixed) m |
|---|---|---|---|---|
| DRV-01 | 2026-03-10 | 8,998.681 | 20,111.099 | 8,998.681 |
| DRV-01 | 2026-03-11 | 6,998.901 | 0.000 | 6,998.901 |
| DRV-02 | 2026-03-10 | 2,417.052 | 20,756.196 | 2,417.052 |
| DRV-02 | 2026-03-11 | 14,972.468 | 0.000 | 14,972.468 |
| DRV-03 | 2026-03-10 | 26,000.000 | 32,472.443 | 26,000.000 |
| DRV-04 | 2026-03-11 | 0.000 | 0.000 | 0.000 |
| DRV-05 | 2026-03-10 | 11,060.056 | 20,102.269 | 11,060.056 |
| DRV-05 | 2026-03-11 | 3,203.512 | 0.000 | 3,203.512 |
| **All** | | **73,650.670** | **93,442.007** | **73,650.670** |

## 4. Locked in

- **22 tests** ([`test_rules.py`](tests/test_rules.py)), each checking billed
  metres to 1e-6 m **and** exactly which pings were rejected and why.
  13 regression tests must fail on legacy **with the exact wrong result they
  declare**, so they fail for the stated bug and not some other reason.
  8 guard tests cover what legacy already did right and must pass on both.
  1 test checks the limit functions directly and runs on the fix only.
  Runner result: **PASS** ([`regression_tests_output.txt`](reports/regression_tests_output.txt)).
- **Exact limits:** 100 m, 150 km/h and 60 s are tested at ±1 mm / ±1 ms, at
  the nearest representable coordinates either side of each limit, and
  exactly at each limit through the limit functions.
- **Tests checked by sabotage:** each threshold nudged by ±0.01 in its unit,
  each rule R1–R8 switched off, plus 5 targeted bugs. **25 of 25 mutants
  caught** ([`sabotage_output.txt`](reports/sabotage_output.txt)).
- **Independent oracle:** tests build pings with their own projection and
  haversine; one test pins the production projection to hand-checkable values.

## 5. Still not solved

- **Late pings after the report is closed:** 5 late pings arrive on a later
  local day than their timestamp. Whether closed days are reissued is a
  business decision; not implemented.
- **Gap rule loses real distance:** a real tunnel over 60 s is not billed
  (driver underpaid). The 60 s threshold is a choice, not a fact.
- **Ideal-ping km is below true road km:** 380.673 m on this dataset, because
  straight lines cut corners. No ping-based method can recover it.
- **R8 looks one ping ahead only.** Two bad pings in a row at the start of a shift are untested.
- **Off-road uses the whole road network** and the same road map as the fix;
  matching each driver to their own route is not attempted.
- **Answer key exists only for synthetic data.** On real data only the
  comparison with a trusted reference could run.
- Some sabotage is caught by **one test only**: R2 off, R7 off, R8 off, and
  "whole line to the start ping's day".
- **Not reviewed by anyone else.**

## 6. How to run (Python 3.11+, standard library only)

```bash
python -m gpsqa.generate --seed 20260310 --out data   # synthetic data + answer key
python -m gpsqa.expected --data data                  # expected km
python -m gpsqa.legacy --data data                    # legacy vs expected
python -m gpsqa.attribution --data data               # reproduction proof + root cause
python -m gpsqa.fixed --data data                     # fix vs expected
python qa_check.py                                    # checks + report, exit 0/1
python tests/run_regression.py                        # tests vs legacy and fix
python tests/sabotage.py                              # do the tests catch sabotage?
```

## Before submitting [FILL IN]

- [ ] Push the folder to a host (it is not a git repo yet) and open **every** link above from a logged-out browser.
- [ ] Tick the eye-checks you actually did in [`intent.md`](intent.md); confirm the "fix first" choice above.
- [ ] Record the Loom video.
