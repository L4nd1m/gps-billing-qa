# Intent: GPS billable-distance QA (synthetic case)

> **All data in this project is 100 % synthetic.** Drivers (`DRV-01`…`DRV-05`,
> `T-01`, `T-02`), roads, the campaign zone and every ping are invented. The
> coordinates sit around a fictional origin in open ocean (35.0 N, 150.0 E).
> This is the suggested fictional case from the brief, not a live product.

## 1. What I set out to prove

The brief's test is "is the problem gone at the end". So the goal was not a QA
tool for its own sake. It was a closed loop where every claim can be re-run:

1. a dataset with **known** faults and an **independent** expected answer;
2. a legacy calculation that gets the distance wrong in realistic ways;
3. a tool that makes the error visible and shows **where and why**;
4. a fix that matches the expected answer per driver per day;
5. tests that **fail on the old behaviour and pass on the fix**, and are
   themselves checked by deliberate sabotage.

## 2. Scope (kept narrow on purpose)

| In scope | Out of scope |
|---|---|
| One fictional city grid with one curved road, one L-shaped zone | Real maps, real road matching |
| 5 drivers, one shift each, 2,350 pings, fixed seed `20260310` | Multi-day campaigns, invoices, pricing |
| Billable metres per driver per local day (UTC+9) | Money per km (no rate was given) |
| Python standard library only | Any third-party package, any paid tool |

## 3. Definitions and assumptions (labelled, not hidden)

- **Expected km = "ideal-ping km":** straight lines between the *genuine* pings
  (taken from the answer key), measured with haversine (mean Earth radius
  6,371,008.8 m), cut at the zone edge, split at 00:00 UTC+9 in proportion to
  time. This is the best any ping-based biller can do. It is **380.673 m below**
  the true road distance on this dataset, because straight lines cut corners
  (measured, see [`expected_km.csv`](data/expected_km.csv) vs
  [`truth_road.csv`](data/truth_road.csv)).
- **Off-road** = more than 100 m from *any* road of the network (not only the
  driver's own route). Exactly 100 m is allowed ("more than").
- **Speed** = above 150 km/h measured from the **last accepted** ping. Exactly
  150 km/h is allowed ("above").
- **Duplicates** (same driver + timestamp): the copy that **arrived first** is kept.
- **Gap rule (R7):** a line spanning more than 60 s is not billed (my choice of
  threshold; the brief names gaps but gives no number). Exactly 60 s is billed.
- **Bad first ping (R8):** an anchor that no accepted line touches yet is
  "unconfirmed"; if the next ping disagrees with it, a one-ping look-ahead
  decides which of the two is wrong.
- **Who pays:** I assume advertisers pay per billable km and drivers are paid
  per billable km. Overcounting hurts the advertiser; undercounting hurts the driver.
- **Day cut:** 00:00 UTC+9 = 15:00 UTC of the previous calendar day.

## 4. Working rules I gave the AI

1. Before writing code, propose options with pros and cons and wait for my decision.
2. Never invent numbers or test results: run them and show the output.
3. Python standard library only.
4. 100 % synthetic data.
5. At the end of each step, list what I should check with my own eyes.

## 5. What existed before

Nothing. The folder was empty at the start. Everything below was built for this
Quest. An unrelated first draft (`SPEC.md`, a spec for a different idea) was
written in the same session; it is **not** part of the submission and is not
published in this repository.

## 6. AI log: what AI produced, what was checked, changed or rejected

AI tool used: Claude (Claude Code desktop app). The AI wrote all code and drafted
both documents. The decisions were mine, picked from options the AI laid out.

| Step | AI produced | My decision (from the options offered) | What was checked, changed or rejected |
|---|---|---|---|
| Ambiguities | 7 billing ambiguities, 2 options each with money impact | — | Basis for every later rule |
| World model | 2 options (flat metres / lat-lon with metres inside) | Lat/lon, metres inside; expected = ideal-ping km; noise *replaces* a genuine ping; cut at zone edge, split by time | Projection round-trip measured: max 0.0071 m |
| Dataset | [`generate.py`](gpsqa/generate.py) + self-verification | — | First run had **0** lines crossing midnight (pings landed exactly on 15:00:00). **Changed:** two shift starts offset by seconds. Gap between expected and true road km traced to corner-cutting, not a bug |
| Legacy | [`legacy.py`](gpsqa/legacy.py) with 5 commented bugs | Haversine; off-road and dedup done *correctly* (keeps the bug list to 5); plain code, no on/off flags | Trace log proved each bug fires on the data |
| QA tool | [`qa_check.py`](qa_check.py) + HTML report | Pluggable "after"; fail on billing mismatch or detector error; one arrival-order check; tolerance 1 m | One detector "miss" (P01120) traced to a **wrong answer-key label**: the generator labelled intent, not effect. Fixed later by relabelling |
| Root cause | [`replica.py`](gpsqa/replica.py), [`attribution.py`](gpsqa/attribution.py) | All 32 on/off combinations + Shapley; error = sum of abs error per driver-day; fix 2 drops a stale ping; own zone geometry | **Proved** the replica equals legacy bit-for-bit (difference 0.0) before any blame. With all fixes on, 1.3 m remained: traced to flat-world vs haversine. **AI error caught:** my earlier "0.0000 m on axis-aligned roads" had only been measured at y = 0 |
| Fix | [`fixed.py`](gpsqa/fixed.py) rules R1–R6 | Brief + decisions so far; haversine in fix *and* expected; separate module | Matches expected on all 8 driver-days; switching each rule off showed R1 had **no effect** on the data (masked by R4) |
| Tests | [`test_rules.py`](tests/test_rules.py), [`run_regression.py`](tests/run_regression.py) | Add R7 (60 s); keep "guard" tests where legacy is already right; exact limits via limit functions + nearest-float bracketing; rejection log in both | **AI's proposed approach rejected by measurement:** exactly 100.000 m / 150.000 km/h cannot be built through lat/lon (nearest neighbours 99.9999999977 / 100.0000000003 m) |
| Review | Sabotage harness ([`sabotage.py`](tests/sabotage.py)) | — | **5 of 24 mutants survived** the first suite (e.g. ignoring north-south zone edges, a forgotten cos(latitude)); a bad first ping billed 1,300 m instead of 300 m; qa_check was permanently red |
| Close gaps | R8, independent test oracle, 1e-6 m tolerance, declared legacy failures, P01120 relabelled | Confirm anchor + look-ahead; relabel by effect; own projection + pinned values; 1e-6 m | Now 22 tests, **25 of 25 mutants caught**. **AI error caught by its own new test:** I first wrote 1° of longitude at 35 N as 91,085.66 m; correct is 91,085.68 m. **AI miscount corrected:** I reported 26 and 27 mutants; the harness has 24 and 25 |

Options I **rejected** when offered: an epsilon tolerance hidden in the billing
rules; forcing legacy to fail by combining bugs (misleading root cause);
dropping tests that pass on legacy; per-bug on/off flags inside the legacy code.

### Checks with my own eyes, as recommended by the AI at the end of each step

Working rule 5 made the AI end every step with a list of things to check by
eye. Below is every item, step by step. Where a value has changed since that
step, the current value is noted in *italics*.
**[FILL IN: tick the boxes for the checks you actually did]**

**Setup and first idea (unrelated to the GPS case; that draft, `SPEC.md`, is not published)**
- [ ] The project folder was empty at the start.
- [ ] `python --version` prints Python 3.12.10.
- [ ] The first options (what to test, which test runner) matched what I wanted.
- [ ] Each rule in section 3 of `SPEC.md` is clear enough that two people would write the same test for it.
- [ ] Worked examples E1 and E4 recomputed by hand: 84 and 74.
- [ ] In E5, all three reasons are listed in the required order.
- [ ] The draft decisions D1–D7 reviewed.
- [ ] Section 6 rules out real names, emails, companies or places.

**Billing ambiguities**
- [ ] 150 km/h × 10 s = 150,000 m ÷ 3,600 s × 10 s ≈ 416.7 m.
- [ ] 00:00 at UTC+9 is 15:00 UTC on the previous calendar day.
- [ ] Three points on paper: removing the middle one never makes the path longer.
- [ ] Re-read the brief: "more than 100 metres" and "above 150 km/h", so exactly 100 m and 150 km/h are kept.
- [ ] Checked the assumption that drivers are paid per km (the brief only says advertisers pay per km).

**World model**
- [ ] Sketched the grid and the L-shaped zone, and found a route that leaves and re-enters the zone.
- [ ] Hand example: (500, 101) against road y = 0 is 101 m, so it is noise.
- [ ] Decided whether the data must look like GPS (option A vs B).
- [ ] Chose the origin (a mid-latitude origin shows a forgotten cos(latitude) bug).
- [ ] Confirmed a curved road is in scope, so straight-line under-counting can appear.

**Synthetic dataset**
- [ ] [`pings.csv`](data/pings.csv): lat/lon near 35.0 / 150.0, timestamps end in `Z`, rows in `received_at_utc` order.
- [ ] A `duplicate_shifted` row in [`answer_key.csv`](data/answer_key.csv): its `duplicate_of` ping has the same timestamp and slightly different coordinates.
- [ ] DRV-01 and DRV-03 in [`expected_km.csv`](data/expected_km.csv) were exactly 9,000 / 7,000 / 26,000 m at that step and could be reproduced by hand. *(Now 8,998.681 / 6,998.901 / 26,000.000 m, after expected km switched to haversine.)*
- [ ] DRV-04 is 0 m, because its route never enters the zone.
- [ ] P01536 is at 15:00:00 UTC, exactly 00:00 UTC+9.
- [ ] Re-running both commands gives the same output (fixed seed).

**Legacy calculation**
- [ ] Read the five `BUG n` comments in [`legacy.py`](gpsqa/legacy.py): each is a plausible real mistake.
- [ ] `anchor[drv] = p` comes before the speed check (bug 3).
- [ ] [`legacy_km.csv`](data/legacy_km.csv) has no rows for 2026-03-11.
- [ ] DRV-03 never crosses midnight but is still 6,472 m too high: caused by bugs 1, 2 and 4, not bug 5.
- [ ] Re-running gives 93,442.007 m again.

**QA tool**
- [ ] [`qa_report.html`](reports/qa_report.html): the banner and the "Why it failed" list match the console. *(Now: PASS.)*
- [ ] P01120 and P01536 `received_at_utc` compared: P01120 really arrived in order.
- [ ] Report section 3: all legacy metres on 2026-03-10, none on 2026-03-11 (bug 5).
- [ ] Report section 2: start-point-only figure vs cut-at-edge figure (bug 4's raw effect).
- [ ] Re-running `python qa_check.py` gives the same table and exit code. *(Exit code 1 then; 0 now.)*

**Exact reproduction and attribution**
- [ ] The step 1 output prints `diff 0.0` at full precision, not rounded.
- [ ] Rows `1` and `1,2` in [`attribution_combos.csv`](data/attribution_combos.csv) are identical (fix 2 adds nothing once sorted).
- [ ] The Shapley column adds up to the total removed. *(70,136.550 m at that step; 70,140.997 m now.)*
- [ ] In [`replica.py`](gpsqa/replica.py), with fix 3 on, the anchor moves only after the speed check passes.
- [ ] The DRV-01 residuals (−1.332 / −1.109 m) are explained by the measured per-road gap (−0.334 / −0.664 m per 3 km).

**Corrected calculation**
- [ ] [`fixed_km.csv`](data/fixed_km.csv): `diff_m` is ±0.000000 on every row.
- [ ] In [`fixed.py`](gpsqa/fixed.py), R4 compares against the last kept ping, not the previous raw ping.
- [ ] The report shows "before (legacy)" and "after (fixed)" columns, every "after" PASS.
- [ ] Switching R1 off gives 0.000 m on the dataset, which is why R1 needs its own test.
- [ ] `python -m gpsqa.fixed --data data` exits with code 0.

**Rule tests (first version)**
- [ ] Read each test docstring in [`test_rules.py`](tests/test_rules.py) and redid the arithmetic, e.g. 50 + 100 + 100 = 250 m in the zone test.
- [ ] In the jump test, legacy rejects both `J` and `C`; the fix rejects only `J`.
- [ ] Ran the tests against legacy and saw the regression tests fail.
- [ ] `is_off_road`, `is_too_fast` and `is_gap` all use `>`.
- [ ] The only changes in `legacy.py` are rejection-log lines, no calculation changes.

**Sabotage review**
- [ ] The zone-nudge results (−0.010000000222 vs +0.009999999902 m) sat right on the old 0.01 m tolerance.
- [ ] The zone test then used only x = 1000, so no north-south zone edge was ever crossed.
- [ ] Bad first ping traced by hand: J0 (y = 2400) to C (y = 1200) over 30 s is exactly 144 km/h, under 150.
- [ ] The speed-boundary test then used `fixed.haversine`, the function under test.
- [ ] `qa_check` exited 1 on P01120 alone, which would have hidden any new failure.

**Closing the gaps**
- [ ] Read a few "DECLARED legacy failure" lines in [`regression_tests_output.txt`](reports/regression_tests_output.txt), e.g. `R8_bad_first_ping`: legacy rejects A instead of J0.
- [ ] Inner-corner test by hand: (1450,1450)→(1550,1550) meets the corner (1500,1500) exactly halfway.
- [ ] Read the R8 block in [`fixed.py`](gpsqa/fixed.py): "confirmed" means what I intended.
- [ ] The report banner now says PASS, exit code 0.
- [ ] Checksums unchanged after relabelling P01120: `pings.csv` `C004E143…`, `truth_road.csv` `61DFAB14…`.

**These documents**
- [ ] The before/after table in [`directive.md`](directive.md) matches [`qa_check_output.txt`](reports/qa_check_output.txt) line by line.
- [ ] The ranking shares match [`attribution_shares.csv`](data/attribution_shares.csv) (0.7026, 0.2315, 0.2334, −0.1658, −0.0017).
- [ ] Each "My decision" in the AI log above matches what I actually chose.
- [ ] Opened several links in each document.
- [ ] "Still not solved" in `directive.md` misses nothing important.

## 7. Effort

**6 hours over 2 days.**

Steps covered: ambiguity analysis → world model → synthetic data and answer key
→ legacy calculation → QA tool and report → exact legacy reproduction and
per-bug attribution → corrected calculation → rule tests → sabotage review →
closing the test gaps → these documents.
