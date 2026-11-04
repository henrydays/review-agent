#!/usr/bin/env python3
"""Optional differential check: the Python scoring port against ReviewBench's own TypeScript.

Runs 300 randomized PRs through both and compares every metric, stratum and aggregate. If this fails,
offline scores no longer mean what the leaderboard means.

This installs ReviewBench's npm dependencies and executes its code, so it is opt-in and not part of the
normal test run. tests/test_scoring.py covers the same rules with hand-derived cases, without running
anything external.

  python3 tests/optional_scoring_parity.py
"""
import json
import random
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bench"))
from common import RB_DIR, ensure_reviewbench  # noqa: E402
from judge import aggregate, resolve_for_scoring, score_pr  # noqa: E402

DRIVER = r"""
import { readFileSync, writeFileSync } from "node:fs";
import { resolveForScoring } from "%RB%/scripts/lib/match-types.ts";
import { scorePR, aggregate } from "%RB%/scripts/eval/scorer.ts";
const cases = JSON.parse(readFileSync(process.argv[2], "utf8"));
const results = [];
for (const c of cases) {
  const r = resolveForScoring(c.corr, c.golden.length);
  const classes = new Map(Object.entries(c.classes).map(([k, v]) => [Number(k), v]));
  results.push(scorePR({ pr_key: c.key, golden: c.golden, candidate_count: c.corr.length,
    matched: r.matchedToGolden, unmatched: r.unmatched, covered_golden: r.coveredGolden, unmatched_classifications: classes }));
}
const agg = aggregate(results);
writeFileSync(process.argv[3], JSON.stringify({ results, macro: agg.macro, micro: agg.micro }));
"""

KEYS = ("grounded_precision", "grounded_recall", "augmented_precision", "augmented_recall", "candidate_count",
        "golden_tp_count", "matched_count", "matched_tp_count", "unmatched_count", "unmatched_tp_count", "unmatched_fp_count", "golden_covered_count")


def make_case(rng: random.Random, i: int) -> dict:
    severities, categories = ["high", "medium", "low"], ["correctness", "security", "testing"]
    golden = [{"file": "a", "start_line": 1, "end_line": 1, "message": "m", "tp_fp": rng.choice(["tp", "tp", "fp"]),
               "severity": rng.choice(severities), "category": rng.choice(categories)} for _ in range(rng.randint(0, 9))]
    corr, classes = [], {}
    for ci in range(rng.randint(0, 14)):
        matches = rng.sample(range(len(golden)), k=min(len(golden), rng.choice([0, 0, 1, 1, 2]))) if golden else []
        corr.append({"candidate_index": ci, "matched_golden_indices": matches})
    for ci in resolve_for_scoring(corr, len(golden))["unmatched"]:
        if rng.random() < 0.7:
            classes[str(ci)] = {"tp_fp": rng.choice(["tp", "fp"]), "severity": rng.choice(severities), "category": rng.choice(categories)}
    return {"key": f"pr{i}", "golden": golden, "corr": corr, "classes": classes}


def compare(ours: dict, theirs: dict, where: str, problems: list[str]) -> None:
    for key in KEYS:
        a, b = ours[key], theirs[key]
        same = a is b if a is None or b is None else abs(a - b) < 1e-9
        if not same:
            problems.append(f"{where}.{key}: python={a} ts={b}")


def main() -> int:
    ensure_reviewbench()
    if not (RB_DIR / "node_modules" / ".bin" / "tsx").exists():
        subprocess.run(["npm", "ci", "--silent", "--no-audit", "--no-fund"], cwd=RB_DIR, check=True)
    rng = random.Random(42)
    cases = [make_case(rng, i) for i in range(300)]
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        (folder / "cases.json").write_text(json.dumps(cases))
        driver = RB_DIR / ".parity-driver.ts"
        driver.write_text(DRIVER.replace("%RB%", str(RB_DIR)))
        try:
            subprocess.run([str(RB_DIR / "node_modules/.bin/tsx"), str(driver), str(folder / "cases.json"), str(folder / "out.json")], cwd=RB_DIR, check=True)
        finally:
            driver.unlink(missing_ok=True)
        ts = json.loads((folder / "out.json").read_text())
    problems: list[str] = []
    per_pr = {}
    for case, theirs in zip(cases, ts["results"]):
        classes = {int(k): v for k, v in case["classes"].items()}
        ours = score_pr(case["golden"], len(case["corr"]), resolve_for_scoring(case["corr"], len(case["golden"])), classes)
        per_pr[case["key"]] = ours
        compare(ours["overall"], theirs["metrics"]["overall"], f"{case['key']}.overall", problems)
        for axis in ("by_severity", "by_category"):
            if set(ours[axis]) != set(theirs["metrics"][axis]):
                problems.append(f"{case['key']}.{axis} keys differ")
                continue
            for name in ours[axis]:
                compare(ours[axis][name], theirs["metrics"][axis][name], f"{case['key']}.{axis}.{name}", problems)
    summary = aggregate(per_pr)
    for average in ("micro", "macro"):
        compare(summary[average]["overall"], ts[average]["overall"], f"{average}.overall", problems)
        for axis in ("by_severity", "by_category"):
            for name in summary[average][axis]:
                if name in ts[average][axis]:
                    compare(summary[average][axis][name], ts[average][axis][name], f"{average}.{axis}.{name}", problems)
    if problems:
        print(f"FAIL: {len(problems)} differences")
        print("\n".join(problems[:25]))
        return 1
    print(f"ok: {len(cases)} randomized PRs agree on every metric, stratum and aggregate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
