#!/usr/bin/env python3
"""What each discovery session is worth, measured on runs that already finished. No new reviews, only cached judging.

  lens_value.py RUN [RUN ...] [--out runs/lens-value.md]

Removing a session can only lose a finding that no remaining session reported, so a policy can be replayed against a finished run:
a golden finding stays covered if the run covered it and some kept session reported it. This is an estimate. A real run also changes
how much time verification gets, and a session's output varies from one sample to the next, so treat a policy that looks good here as
a candidate to evaluate, not as a result.

Only PRs in the `train` split are accepted, since this reads golden findings.
"""
from __future__ import annotations

import argparse
import collections
import concurrent.futures as cf
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bench"))
from common import load_golden, load_splits, log  # noqa: E402
from judge import Matcher, matcher_host, matcher_system_prompt  # noqa: E402
from llm import DEFAULT_JUDGE_MODEL  # noqa: E402

TASK_ID = re.compile(r"d_([a-z]+)_(\d+)_(\d+)$")
LENS_ORDER = {"quick": 0, "general": 1, "intent": 2, "drift": 3, "failure": 4, "logic": 4, "config": 6, "ui": 7}


def load_pr(run_dir: Path, key: str, details: dict, matcher: Matcher, pool: cf.Executor) -> dict:
    artifacts = run_dir / "artifacts" / key
    golden = load_golden(key)["findings"]
    raw = json.loads((artifacts / "raw_candidates.json").read_text())
    matches = matcher.match(raw, golden, pool) if raw else []
    reported_by: dict[int, set[str]] = collections.defaultdict(set)
    for finding, found in zip(raw, matches):
        for g in found:
            reported_by[g].add(finding.get("task", "?"))
    tasks = {}
    verify_cost = 0.0
    for path in sorted((artifacts / "results").glob("*.json")):
        record = json.loads(path.read_text())
        cost = (record.get("usage") or {}).get("cost") or 0.0
        parsed = TASK_ID.match(path.stem)
        if parsed:
            tasks[path.stem] = {"lens": parsed[1], "part": int(parsed[2]), "pass": int(parsed[3]), "cost": cost, "seconds": record.get("seconds") or 0.0,
                                "tools": sum((record.get("tools") or {}).values()), "failed": bool(record.get("error")) and not record.get("partial"),
                                "raw": sum(1 for f in raw if f.get("task") == path.stem)}
        elif path.stem.startswith("v_"):
            verify_cost += cost
    final = json.loads((artifacts / "final.json").read_text())
    meta = json.loads((artifacts / "meta.json").read_text())
    tp = {i for i, g in enumerate(golden) if g["tp_fp"] == "tp"}
    high = {i for i in tp if golden[i]["severity"] == "high"}
    return {"key": key, "tasks": tasks, "reported_by": reported_by, "covered": set(details.get("covered_golden", [])) & tp, "tp": tp, "high": high,
            "verify_cost": verify_cost, "judged": final.get("judged", []), "partitions": meta.get("partitions"), "lines": meta.get("lines_changed", 0)}


def plan_order(task_id: str, task: dict) -> tuple:
    """The order the skill plans sessions in: every lens's first pass before anyone's second."""
    return (task["pass"], LENS_ORDER.get(task["lens"], 9), task["part"], task_id)


def replay(prs: list[dict], keep) -> dict:
    """`keep(pr, task_id, task) -> bool`."""
    recalls, high_recalls, covered, high_covered, tp, high_tp = [], [], 0, 0, 0, 0
    full_cost = kept_cost = full_sessions = kept_sessions = 0.0
    kept_candidates = all_candidates = 0
    longest = []
    for pr in prs:
        kept = {t for t, task in pr["tasks"].items() if keep(pr, t, task)}
        got = {g for g in pr["covered"] if pr["reported_by"].get(g, set()) & kept}
        if pr["tp"]:
            recalls.append(len(got) / len(pr["tp"]))
            tp += len(pr["tp"])
            covered += len(got)
        if pr["high"]:
            high_recalls.append(len(got & pr["high"]) / len(pr["high"]))
            high_tp += len(pr["high"])
            high_covered += len(got & pr["high"])
        discovery = sum(task["cost"] for task in pr["tasks"].values())
        share = sum(pr["tasks"][t]["cost"] for t in kept) / discovery if discovery else 1.0
        lenses = {pr["tasks"][t]["lens"] for t in kept}
        survivors = sum(1 for c in pr["judged"] if set(c.get("lenses") or [c.get("lens")]) & lenses)
        all_candidates += len(pr["judged"])
        kept_candidates += survivors
        # Verification runs on what discovery reports, so it shrinks with the candidates.
        verify = pr["verify_cost"] * (survivors / len(pr["judged"]) if pr["judged"] else 1.0)
        full_cost += discovery + pr["verify_cost"]
        kept_cost += discovery * share + verify
        full_sessions += len(pr["tasks"])
        kept_sessions += len(kept)
        longest.append(max((pr["tasks"][t]["seconds"] for t in kept), default=0.0))
    return {"recall": sum(recalls) / len(recalls), "high_recall": sum(high_recalls) / len(high_recalls) if high_recalls else None, "covered": covered, "tp": tp,
            "high_covered": high_covered, "high_tp": high_tp, "cost_ratio": kept_cost / full_cost if full_cost else 1.0, "sessions": kept_sessions / len(prs),
            "candidates_ratio": kept_candidates / all_candidates if all_candidates else 1.0, "discover_seconds": sum(longest) / len(longest)}


def cap(limit: int):
    def keep(pr, task_id, task) -> bool:
        ranked = sorted(pr["tasks"], key=lambda t: plan_order(t, pr["tasks"][t]))
        return task_id in ranked[:limit]
    return keep


def policies(prs: list[dict]) -> dict:
    lenses = sorted({task["lens"] for pr in prs for task in pr["tasks"].values()}, key=lambda l: LENS_ORDER.get(l, 9))
    table: dict = {"everything": lambda pr, t, task: True}
    for lens in lenses:
        table[f"without {lens}"] = lambda pr, t, task, lens=lens: task["lens"] != lens
    table["general once"] = lambda pr, t, task: task["pass"] == 1
    table["general once, without quick"] = lambda pr, t, task: task["pass"] == 1 and task["lens"] != "quick"
    table["only general"] = lambda pr, t, task: task["lens"] == "general"
    table["general + quick"] = lambda pr, t, task: task["lens"] in ("general", "quick")
    table["general + drift + failure"] = lambda pr, t, task: task["lens"] in ("general", "drift", "failure")
    # A split PR runs each lens on each part, which is where the sessions go.
    table["large PRs: general only"] = lambda pr, t, task: (pr["partitions"] or 1) <= 1 or task["lens"] == "general"
    table["large PRs: general + quick"] = lambda pr, t, task: (pr["partitions"] or 1) <= 1 or task["lens"] in ("general", "quick")
    table["large PRs: general + failure"] = lambda pr, t, task: (pr["partitions"] or 1) <= 1 or task["lens"] in ("general", "failure")
    for limit in (5, 6, 8, 10, 12, 16):
        table[f"at most {limit} sessions"] = cap(limit)
    # Combinations, since sessions overlap and savings do not add up.
    core = ("general", "drift", "failure", "intent")
    table["core: general drift failure intent"] = lambda pr, t, task: task["lens"] in core
    table["core, general once"] = lambda pr, t, task: task["lens"] in core and task["pass"] == 1
    table["core, at most 16"] = lambda pr, t, task: task["lens"] in core and _ranked(pr, core, t, 16)
    table["core, at most 12"] = lambda pr, t, task: task["lens"] in core and _ranked(pr, core, t, 12)
    table["core, general once, at most 12"] = lambda pr, t, task: task["lens"] in core and task["pass"] == 1 and _ranked(pr, core, t, 12, passes=1)
    table["general drift intent"] = lambda pr, t, task: task["lens"] in ("general", "drift", "intent")
    table["general drift intent, general once"] = lambda pr, t, task: task["lens"] in ("general", "drift", "intent") and task["pass"] == 1
    table["general drift failure"] = lambda pr, t, task: task["lens"] in ("general", "drift", "failure")
    table["general drift"] = lambda pr, t, task: task["lens"] in ("general", "drift")
    table["general drift, general once"] = lambda pr, t, task: task["lens"] in ("general", "drift") and task["pass"] == 1
    table["general once + drift + failure + intent + quick"] = lambda pr, t, task: task["lens"] in (*core, "quick") and task["pass"] == 1
    # A session slower than the discovery limit is killed, and a killed session reports almost nothing.
    for limit in (150, 180, 200, 220, 240, 260):
        table[f"sessions done within {limit}s"] = lambda pr, t, task, limit=limit: task["seconds"] <= limit and not task["failed"]
    # Which sessions a split PR can do without, keeping the first pass of general everywhere.
    table["split PRs: no failure"] = lambda pr, t, task: (pr["partitions"] or 1) <= 1 or task["lens"] != "failure"
    table["split PRs: no drift"] = lambda pr, t, task: (pr["partitions"] or 1) <= 1 or task["lens"] != "drift"
    table["no failure anywhere"] = lambda pr, t, task: task["lens"] != "failure"
    return table


def _ranked(pr: dict, lenses: tuple, task_id: str, limit: int, passes: int = 9) -> bool:
    chosen = [t for t, task in pr["tasks"].items() if task["lens"] in lenses and task["pass"] <= passes]
    return task_id in sorted(chosen, key=lambda t: plan_order(t, pr["tasks"][t]))[:limit]


def _pct(value) -> str:
    return "n/a" if value is None else f"{value * 100:5.1f}"


def report(name: str, prs: list[dict]) -> str:
    lines = [f"## {name} ({len(prs)} PRs)", "", "| Policy | Golden covered | Macro recall | High covered | Sessions/PR | Cost | Candidates | Longest session |", "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for label, keep in policies(prs).items():
        r = replay(prs, keep)
        lines.append(f"| {label} | {r['covered']}/{r['tp']} | {_pct(r['recall'])} | {r['high_covered']}/{r['high_tp']} | {r['sessions']:.1f} | {_pct(r['cost_ratio'])}% | {_pct(r['candidates_ratio'])}% | {r['discover_seconds']:.0f}s |")
    lines += ["", "| Lens | Sessions | Cost | Failed | Raw findings | Golden covered | Only this lens | Cost per golden |", "|---|---:|---:|---:|---:|---:|---:|---:|"]
    stats: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    total_cost = 0.0
    for pr in prs:
        by_lens: dict[int, set[str]] = {}
        for g in pr["covered"]:
            by_lens[g] = {pr["tasks"][t]["lens"] for t in pr["reported_by"].get(g, set()) if t in pr["tasks"]}
        for task in pr["tasks"].values():
            row = stats[task["lens"]]
            row["sessions"] += 1
            row["cost"] += task["cost"]
            row["failed"] += task["failed"]
            row["raw"] += task["raw"]
            total_cost += task["cost"]
        for found in by_lens.values():
            for lens in found:
                stats[lens]["golden"] += 1
            if len(found) == 1:
                stats[next(iter(found))]["unique"] += 1
    for lens, row in sorted(stats.items(), key=lambda item: -item[1]["cost"]):
        per = row["cost"] / row["golden"] if row["golden"] else 0
        lines.append(f"| {lens} | {row['sessions']} | ${row['cost']:.2f} ({row['cost'] / total_cost * 100:.0f}%) | {row['failed']} | {row['raw']} | {row['golden']} | {row['unique']} | ${per:.3f} |")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("runs", nargs="+", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--model", default=DEFAULT_JUDGE_MODEL)
    args = parser.parse_args()
    train = set(load_splits()["train"])
    system = matcher_system_prompt()
    sections = []
    with matcher_host(args.model, system) as host:
        matcher = Matcher(host, args.model, system)
        with cf.ThreadPoolExecutor(4) as pool:
            for run_dir in args.runs:
                details = json.loads((run_dir / "results.details.json").read_text())
                outside = sorted(set(details) - train)
                if outside:
                    raise SystemExit(f"{run_dir.name} includes {len(outside)} PRs outside the training split, starting with {outside[0]}")
                prs = []
                for key in sorted(details):
                    prs.append(load_pr(run_dir, key, details[key], matcher, pool))
                    log(f"  read {key}")
                sections.append(report(run_dir.name, prs))
    text = "\n\n".join(sections) + "\n"
    if args.out:
        args.out.write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
