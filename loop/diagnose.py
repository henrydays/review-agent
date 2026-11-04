#!/usr/bin/env python3
"""Explain a judged run: which golden findings were missed, and at which stage they were lost.

Knowing a finding was missed is not enough to fix anything. A miss nobody ever reported calls for a
better lens. A miss some lens reported and the verifier refuted calls for a better verifier. So the raw
candidates of every stage get judged too, and each golden finding is traced through the pipeline.

  diagnose.py RUN_DIR [--model M]     writes RUN_DIR/diagnosis.json and diagnosis.md
"""
from __future__ import annotations

import argparse
import collections
import concurrent.futures as cf
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bench"))
from common import load_golden, load_manifest, log  # noqa: E402
from judge import Matcher, matcher_host, matcher_system_prompt, pct  # noqa: E402
from llm import DEFAULT_JUDGE_MODEL  # noqa: E402

DOCS = (".md", ".mdx", ".rst", ".adoc", ".txt")
CONFIG = (".yml", ".yaml", ".json", ".toml", ".ini", ".cfg", ".tf", ".hcl", ".gradle", ".xml", ".csproj", ".props", ".lock", ".tpl")


def file_kind(path: str) -> str:
    lowered = path.lower()
    name = lowered.rsplit("/", 1)[-1]
    if any(part in lowered for part in ("/test", "test/", "tests/", "__tests__", "spec/", ".test.", ".spec.", "_test.")) or name.startswith("test_"):
        return "test"
    if lowered.endswith(DOCS):
        return "docs"
    if lowered.endswith(CONFIG) or "dockerfile" in name or ".github/" in lowered:
        return "config"
    return "source"


def size_bucket(lines: int) -> str:
    for limit, label in ((200, "<=200"), (1000, "201-1000"), (5000, "1001-5000")):
        if lines <= limit:
            return label
    return ">5000"


def trace_pr(key: str, run_dir: Path, details: dict, matcher: Matcher, pool: cf.Executor) -> dict:
    """Where each golden TP was lost, plus what each lens contributed."""
    golden = load_golden(key)["findings"]
    artifacts = run_dir / "artifacts" / key
    read = lambda name: json.loads((artifacts / name).read_text()) if (artifacts / name).exists() else []  # noqa: E731
    raw = read("raw_candidates.json")
    final = read("final.json") or {"judged": []}
    judged = final.get("judged", [])
    raw_matches = matcher.match(raw, golden, pool) if raw else []
    judged_matches = matcher.match(judged, golden, pool) if judged else []
    final_covered = set(details.get("covered_golden", []))
    found_by_lens: dict[int, set[str]] = collections.defaultdict(set)
    for finding, matches in zip(raw, raw_matches):
        for g in matches:
            found_by_lens[g].add(finding.get("lens", "?"))
    in_judged: dict[int, list[dict]] = collections.defaultdict(list)
    for candidate, matches in zip(judged, judged_matches):
        for g in matches:
            in_judged[g].append(candidate)
    traces: list[dict] = []
    for index, g in enumerate(golden):
        if g["tp_fp"] != "tp":
            continue
        if index in final_covered:
            stage = "found"
        elif index not in found_by_lens:
            stage = "never_discovered"
        elif index not in in_judged:
            stage = "lost_in_merge"
        elif all(c.get("verdict") == "refuted" for c in in_judged[index]):
            stage = "refuted_by_verifier"
        else:
            stage = "lost_in_finalize"
        refuted = [c for c in in_judged.get(index, []) if c.get("verdict") == "refuted"]
        traces.append({"pr": key, "golden": index, "stage": stage, "file": g["file"], "start_line": g["start_line"], "end_line": g["end_line"],
                       "message": g["message"], "severity": g["severity"], "category": g["category"], "difficulty": g.get("difficulty"),
                       "context_required": g.get("context_required"), "scope": g.get("scope"), "file_kind": file_kind(g["file"]),
                       "lenses": sorted(found_by_lens.get(index, [])),
                       "refutation": refuted[0].get("verdict_reasoning", "")[:400] if refuted else None})
    # Golden FPs we matched: these are what hurts grounded precision.
    candidates = json.loads((run_dir / "findings" / f"{key}.json").read_text())["findings"]
    matched_fp = []
    for candidate_index, golden_index in details.get("matched_to_golden", {}).items():
        g = golden[int(golden_index)]
        if g["tp_fp"] != "tp":
            c = candidates[int(candidate_index)]
            matched_fp.append({"pr": key, "file": c["file"], "line": c["start_line"], "ours": c["message"], "why_false": g.get("tp_fp_justification", "")[:400], "severity": g["severity"]})
    # What each lens brought.
    lens_stats: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    tp_indices = {i for i, g in enumerate(golden) if g["tp_fp"] == "tp"}
    for finding, matches in zip(raw, raw_matches):
        lens = finding.get("lens", "?")
        lens_stats[lens]["raw"] += 1
        if any(g in tp_indices for g in matches):
            lens_stats[lens]["matched_tp"] += 1
        if any(g not in tp_indices for g in matches):
            lens_stats[lens]["matched_fp"] += 1
    for g, lenses in found_by_lens.items():
        if g in tp_indices:
            for lens in lenses:
                lens_stats[lens]["golden_covered"] += 1
            if len(lenses) == 1:
                lens_stats[next(iter(lenses))]["golden_unique"] += 1
    return {"traces": traces, "matched_fp": matched_fp, "lens": {k: dict(v) for k, v in lens_stats.items()},
            "stats": json.loads((artifacts / "stats.json").read_text()) if (artifacts / "stats.json").exists() else {}}


def _table(counter: collections.Counter, found: collections.Counter) -> str:
    rows = []
    for label, total in counter.most_common():
        rows.append(f"| {label} | {found[label]}/{total} | {pct(found[label] / total if total else None)} |")
    return "| Group | Found | Recall |\n|---|---:|---:|\n" + "\n".join(rows)


def diagnose(run_dir: Path, model: str, max_examples: int = 40) -> dict:
    results = json.loads((run_dir / "results.json").read_text())
    details = json.loads((run_dir / "results.details.json").read_text())
    system = matcher_system_prompt()
    per_pr: dict[str, dict] = {}
    with matcher_host(model, system) as host:
        matcher = Matcher(host, model, system)
        with cf.ThreadPoolExecutor(12) as pool:
            for key in sorted(details):
                per_pr[key] = trace_pr(key, run_dir, details[key], matcher, pool)
                log(f"  traced {key}")
    traces = [t for pr in per_pr.values() for t in pr["traces"]]
    matched_fp = [m for pr in per_pr.values() for m in pr["matched_fp"]]
    manifest = load_manifest()
    lens_total: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for pr in per_pr.values():
        for lens, stats in pr["lens"].items():
            lens_total[lens].update(stats)
    summary = {
        "run": run_dir.name,
        "objective_inputs": {avg: {"overall": results[avg]["overall"], "high": results[avg]["by_severity"].get("high")} for avg in ("micro", "macro")},
        "stage_counts": collections.Counter(t["stage"] for t in traces),
        "lens": {k: dict(v) for k, v in lens_total.items()},
        "traces": traces,
        "matched_fp": matched_fp,
    }

    def group_table(field: str, rows: list[dict] = traces) -> str:
        total, found = collections.Counter(), collections.Counter()
        for t in rows:
            label = t[field]
            total[label] += 1
            if t["stage"] == "found":
                found[label] += 1
        return _table(total, found)

    for t in traces:
        pr = manifest.get(t["pr"], {})
        t["pr_size"] = size_bucket(int(pr.get("lines_added", 0)) + int(pr.get("lines_removed", 0)))
        t["language"] = pr.get("language") or "?"
    (run_dir / "diagnosis.json").write_text(json.dumps(summary, indent=1))

    lines = [f"# Diagnosis of run `{run_dir.name}`", ""]
    macro, micro = results["macro"], results["micro"]
    high = macro["by_severity"].get("high") or {}
    lines += [f"Macro grounded, all severities: F1 {pct(macro['overall']['grounded_f1'])}, precision {pct(macro['overall']['grounded_precision'])}, recall {pct(macro['overall']['grounded_recall'])}.",
              f"Macro grounded, high severity: F1 {pct(high.get('grounded_f1'))}, precision {pct(high.get('grounded_precision'))}, recall {pct(high.get('grounded_recall'))}.",
              f"Comments per PR: {micro['overall']['candidate_count'] / max(1, results['pr_count']):.1f}. Golden findings matched that are labeled false: {len(matched_fp)}.", ""]
    lines += ["## Where golden true positives were lost", "",
              "| Stage | Count | What it means |", "|---|---:|---|"]
    meaning = {"found": "reported in the final output", "never_discovered": "no lens reported it. Lens prompts, partitioning or attention are the problem",
               "lost_in_merge": "a lens reported it, but it disappeared in dedupe. Merging too aggressively",
               "refuted_by_verifier": "reported, then wrongly refuted. The verifier is too harsh",
               "lost_in_finalize": "survived verification but was cut by the cap or the profile, or its final wording stopped matching"}
    stage_counts = summary["stage_counts"]
    for stage in ("found", "never_discovered", "lost_in_merge", "refuted_by_verifier", "lost_in_finalize"):
        lines.append(f"| {stage} | {stage_counts.get(stage, 0)} | {meaning[stage]} |")
    for title, field in (("severity", "severity"), ("category", "category"), ("kind of file", "file_kind"), ("context needed", "context_required"), ("difficulty", "difficulty"), ("PR size", "pr_size"), ("language", "language")):
        lines += ["", f"## Recall by {title}", "", group_table(field)]
    lines += ["", "## What each lens contributed", "", "`golden_unique` is how many golden true positives only this lens reported. `matched_fp` is how many of its reports match a golden finding labeled false.", "",
              "| Lens | Raw reports | Matched TP | Golden covered | Golden unique | Matched FP |", "|---|---:|---:|---:|---:|---:|"]
    for lens, stats in sorted(lens_total.items(), key=lambda item: -item[1]["golden_unique"]):
        lines.append(f"| {lens} | {stats['raw']} | {stats['matched_tp']} | {stats['golden_covered']} | {stats['golden_unique']} | {stats['matched_fp']} |")
    order = {"high": 0, "medium": 1, "low": 2}
    missed = sorted((t for t in traces if t["stage"] == "never_discovered"), key=lambda t: (order.get(t["severity"], 3), t["pr"]))
    lines += ["", f"## Findings no lens reported ({len(missed)}, most severe first, up to {max_examples})", "",
              "Look for classes of problem here, not individual cases. A prompt that describes one of these cases will not help on other repositories."]
    for t in missed[:max_examples]:
        lines.append(f"- [{t['severity']}/{t['category']}/{t['file_kind']}/{t['context_required']}] {t['file']}:{t['start_line']}: {t['message'][:420]}")
    refuted = [t for t in traces if t["stage"] == "refuted_by_verifier"]
    lines += ["", f"## Real findings the verifier refuted ({len(refuted)})"]
    for t in sorted(refuted, key=lambda t: order.get(t["severity"], 3))[:15]:
        lines.append(f"- [{t['severity']}] {t['file']}:{t['start_line']}: {t['message'][:300]}\n  Verifier said: {t['refutation']}")
    lines += ["", f"## Golden findings labeled false that we matched ({len(matched_fp)})", "", "These are the only thing that lowers grounded precision."]
    for m in sorted(matched_fp, key=lambda m: order.get(m["severity"], 3))[:15]:
        lines.append(f"- {m['file']}:{m['line']}: we said: {m['ours'][:260]}\n  Why that kind of claim was judged false: {m['why_false'][:260]}")
    text = "\n".join(lines) + "\n"
    (run_dir / "diagnosis.md").write_text(text)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--model", default=DEFAULT_JUDGE_MODEL)
    args = parser.parse_args()
    summary = diagnose(args.run_dir, args.model)
    print(json.dumps({"stages": summary["stage_counts"], "lenses": list(summary["lens"])}))
    print(f"wrote {args.run_dir / 'diagnosis.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
