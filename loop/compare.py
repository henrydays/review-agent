#!/usr/bin/env python3
"""Compare runs on the same training PRs, and write out what the stronger reviewers found that the weaker one did not.

  compare.py --student base-haiku --others teach-haiku1,teach-opus,teach-astra --out runs/compare.md

The report quotes golden findings, so it stays under runs/, which is gitignored. It reads them only for PRs in the `train`
split and refuses anything else: design decisions must not see validation or holdout answers. Read it for classes of
problem and ways of working. A prompt that describes one of these cases will not help on another repository.
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bench"))
from common import load_golden, load_splits  # noqa: E402

RUNS = ROOT / "runs"


def pct(value) -> str:
    return "  n/a" if value is None else f"{100 * value:5.1f}"


def load_run(name: str) -> dict:
    run = RUNS / name
    results = json.loads((run / "results.json").read_text())
    details = json.loads((run / "results.details.json").read_text())
    findings = {p.stem: json.loads(p.read_text())["findings"] for p in (run / "findings").glob("*.json")}
    traces = {p.stem: json.loads(p.read_text()) for p in (run / "traces").glob("*.json")} if (run / "traces").exists() else {}
    return {"name": name, "results": results, "details": details, "findings": findings, "traces": traces, "per_pr": results["per_pr"], "judge": results["judge"]["model"]}


def how_found(run: dict, key: str, message: str) -> str:
    trace = run["traces"].get(key)
    if not trace:
        return ""
    wanted = re.sub(r"\s+", " ", message).strip()[:120]
    for item in trace.get("raw", []):
        if re.sub(r"\s+", " ", str(item.get("message") or "")).strip()[:120] == wanted:
            return str(item.get("how_found") or "")
    return ""


def means(run: dict, keys: list[str]) -> dict:
    rows = [run["per_pr"][k] for k in keys if k in run["per_pr"]]
    overall = [r["overall"] for r in rows]

    def mean(field: str, sets) -> float | None:
        values = [m[field] for m in sets if m and m.get(field) is not None]
        return sum(values) / len(values) if values else None

    high = [r["by_severity"].get("high") for r in rows]
    return {"prs": len(rows), "P": mean("grounded_precision", overall), "R": mean("grounded_recall", overall), "highR": mean("grounded_recall", high),
            "comments": sum(m["candidate_count"] for m in overall) / max(1, len(overall)), "covered": sum(m["golden_covered_count"] for m in overall), "tp": sum(m["golden_tp_count"] for m in overall)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--student", required=True)
    parser.add_argument("--others", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    train = set(load_splits()["train"])
    student = load_run(args.student)
    others = [load_run(name) for name in args.others.split(",") if name]
    keys = sorted(set(student["per_pr"]) & set.intersection(*(set(o["per_pr"]) for o in others)) if others else set(student["per_pr"]))
    outside = [k for k in keys if k not in train]
    if outside:
        raise SystemExit(f"refusing to read golden findings outside the train split: {outside[:3]}")
    runs = [student] + others
    lines = [f"# {args.student} against {args.others}", "", f"Judges: {sorted({r['judge'] for r in runs})}. On {len(keys)} training PRs. Macro means, grounded.", "",
             "| Run | P | R | High R | Comments/PR | Golden TPs covered |", "|---|---:|---:|---:|---:|---:|"]
    for run in runs:
        m = means(run, keys)
        lines.append(f"| {run['name']} | {pct(m['P'])} | {pct(m['R'])} | {pct(m['highR'])} | {m['comments']:.1f} | {m['covered']}/{m['tp']} |")
    lines += ["", "## By PR: recall", "", "| PR | TPs | " + " | ".join(r["name"] for r in runs) + " |", "|---|---:|" + "---:|" * len(runs)]
    for key in keys:
        cells = [f"{100 * (r['per_pr'][key]['overall']['grounded_recall'] or 0):.0f}% ({r['per_pr'][key]['overall']['candidate_count']}c)" for r in runs]
        lines.append(f"| {key[:44]} | {student['per_pr'][key]['overall']['golden_tp_count']} | " + " | ".join(cells) + " |")
    missed_counts: collections.Counter = collections.Counter()
    groups: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    lines += ["", "## Golden true positives the student missed and another run covered", ""]
    for key in keys:
        golden = load_golden(key)["findings"]
        covered = {run["name"]: set(run["details"][key]["covered_golden"]) for run in runs}
        rows = []
        for index, g in enumerate(golden):
            if g["tp_fp"] != "tp":
                continue
            for field in ("severity", "category", "context_required", "difficulty", "scope"):
                groups[field][(g.get(field), "total")] += 1
                if index in covered[student["name"]]:
                    groups[field][(g.get(field), "student")] += 1
            if index in covered[student["name"]]:
                continue
            who = [o for o in others if index in covered[o["name"]]]
            missed_counts["student missed, a teacher covered" if who else "nobody covered"] += 1
            if not who:
                rows.append(f"- NOBODY [{g['severity']}/{g['category']}/{g.get('context_required')}] `{g['file']}:{g['start_line']}` {g['message'][:300]}")
                continue
            rows.append(f"- by {','.join(o['name'] for o in who)} [{g['severity']}/{g['category']}/{g.get('context_required')}] `{g['file']}:{g['start_line']}` {g['message'][:300]}")
            for other in who:
                for finding, matches in zip(other["findings"][key], other["details"][key]["raw_matches"]):
                    if index in matches:
                        note = how_found(other, key, finding["message"])
                        rows.append(f"    - {other['name']} said: {finding['message'][:300]}" + (f"\n      how: {note[:400]}" if note else ""))
                        break
        if rows:
            lines += [f"### {key}", ""] + rows + [""]
    lines += ["## Student's recall by golden attribute", ""]
    for field, counter in groups.items():
        names = sorted({str(name) for name, kind in counter if kind == "total"})
        cells = []
        for name in names:
            original = next(n for n, kind in counter if kind == "total" and str(n) == name)
            total, got = counter[(original, "total")], counter[(original, "student")]
            cells.append(f"{name} {got}/{total}")
        lines.append(f"- {field}: " + ", ".join(cells))
    lines += ["", f"Counts: {dict(missed_counts)}", "", "## How each teacher worked"]
    for other in others:
        calls = collections.Counter()
        for trace in other["traces"].values():
            for tool, count in (trace.get("record", {}).get("tools") or {}).items():
                calls[tool] += count
        records = [t.get("record", {}) for t in other["traces"].values()]
        if records:
            lines += ["", f"### {other['name']}", f"Tools over {len(records)} PRs: {dict(calls)}. Mean {sum(r.get('seconds', 0) for r in records) / len(records):.0f}s, cost ${sum(r.get('cost', 0) for r in records):.2f}."]
        for key, trace in sorted(other["traces"].items()):
            match = re.search(r'"method"\s*:\s*\[(.*?)\]', trace.get("text") or "", re.S)
            if match:
                lines += [f"- {key[:44]} method: {re.sub(r'\s+', ' ', match.group(1))[:900]}"]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(lines) + "\n")
    print(f"wrote {args.out}")
    print("\n".join(lines[:4 + len(runs) + 2]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
