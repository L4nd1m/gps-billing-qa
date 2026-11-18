"""Run tests/test_rules.py against legacy AND fixed and check every outcome.

    python tests/run_regression.py

Required:
  fixed   every test passes
  legacy  @regression tests FAIL with the result they DECLARE for legacy
          (message starts "DECLARED legacy failure"); @guard tests PASS;
          @fixed_only tests SKIP
Exit 0 if all outcomes are as required, 1 otherwise.
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class Collect(unittest.TestResult):
    def __init__(self):
        super().__init__()
        self.out = {}

    def _name(self, t):
        return t._testMethodName

    def addSuccess(self, t):
        self.out[self._name(t)] = ("pass", "")

    def addFailure(self, t, err):
        self.out[self._name(t)] = ("fail", str(err[1]).splitlines()[0][:260])

    def addError(self, t, err):
        self.out[self._name(t)] = ("error", f"{err[0].__name__}: {err[1]}"[:150])

    def addSkip(self, t, reason):
        self.out[self._name(t)] = ("skip", reason)


def run(calc):
    os.environ["GPSQA_CALC"] = calc
    suite = unittest.defaultTestLoader.loadTestsFromName("tests.test_rules")
    res = Collect()
    tags = {}                       # read tags BEFORE running: run() empties the suite

    def walk(s):
        for t in s:
            if isinstance(t, unittest.TestSuite):
                walk(t)
            else:
                tags[t._testMethodName] = getattr(getattr(t, t._testMethodName), "legacy_should", "?")
    walk(suite)
    suite.run(res)
    return res.out, tags


def main():
    fixed_out, tags = run("fixed")
    legacy_out, _ = run("legacy")
    ok_all = True
    print(f"{'test':48} {'tag':10} {'legacy':7} {'fixed':6} ok")
    for name in sorted(tags):
        want_legacy = tags[name]
        lg, fx = legacy_out[name][0], fixed_out[name][0]
        ok = fx == "pass" and lg == want_legacy
        if want_legacy == "fail":      # must fail for the declared reason
            ok = ok and legacy_out[name][1].startswith("DECLARED legacy failure")
        ok_all &= ok
        tag = {"fail": "regression", "pass": "guard", "skip": "fixed_only"}.get(want_legacy, "?")
        print(f"{name:48} {tag:10} {lg:7} {fx:6} {'OK' if ok else 'UNEXPECTED'}")
        for label, out in (("legacy", legacy_out[name]), ("fixed", fixed_out[name])):
            if out[0] in ("fail", "error"):
                print(f"    {label}: {out[1]}")
    print("RESULT:", "PASS" if ok_all else "FAIL")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
