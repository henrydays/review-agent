#!/usr/bin/env python3
"""Does the choice of judge change the result? Runs several judges over the same findings and measures how far they agree.

  judge_study.py match    --runs a,b,c --judges claude-sonnet-5-5,claude-opus-5-5#max,gpt-6-astra#max --select KEYS
  judge_study.py classify --runs a,b,c --judges ... --select KEYS --per-run 20 --reference claude-sonnet-5-5
  judge_study.py report   --runs a,b,c --judges ... --select KEYS --out runs/judge-study/report.md
  judge_study.py adjudicate --runs RUN --details results-s10.details.json --judges ... --per-run 20

What matters is not only accuracy. A judge steers the loop, so three questions are asked of each one:
  - Does it agree with the others on individual decisions? Chance-corrected, since most pairs are obvious non-matches.
  - Does it rank the same reviewers in the same order? That is what a go or no-go on a change depends on.
  - Does it favor its own family? Claude reviewers against GPT reviewers, under Claude judges against a GPT judge.

Everything goes under runs/, which is gitignored, and only PRs in the `train` split are accepted.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import itertools
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, load_golden, load_splits, log, resolve_keys
from judge import aggregate, classify_findings, judge_candidates, load_candidates, resolve_for_scoring, sample_order

OUT = ROOT / "runs" / "judge-study"


def slug(spec: str) -> str:
    return spec.replace("#", "-")


def kappa(pairs: list[tuple[bool, bool]]) -> tuple[float | None, float | None]:
    """Agreement and Cohen's kappa."""
    if not pairs:
        return None, None
    n = len(pairs)
    observed = sum(a == b for a, b in pairs) / n
    a_yes = sum(a for a, _ in pairs) / n
    b_yes = sum(b for _, b in pairs) / n
    expected = a_yes * b_yes + (1 - a_yes) * (1 - b_yes)
    return observed, (observed - expected) / (1 - expected) if expected < 1 else None


def pick(args) -> list[str]:
    keys = resolve_keys(args.select)
    train = set(load_splits()["train"])
    outside = [k for k in keys if k not in train]
    if outside:
        raise SystemExit(f"refusing PRs outside the train split: {outside[:3]}")
    return keys


def candidates_of(run: str, keys: list[str]) -> dict[str, dict]:
    everything = load_candidates(ROOT / "runs" / run / "findings")
    return {k: everything[k] for k in keys if k in everything}


def cmd_match(args) -> int:
    keys = pick(args)
    (OUT / "match").mkdir(parents=True, exist_ok=True)
    for spec in args.judges.split(","):
        for run in args.runs.split(","):
            path = OUT / "match" / f"{run}__{slug(spec)}.json"
            if path.exists():
                continue
            judged = judge_candidates(candidates_of(run, keys), model=spec, concurrency=args.concurrency)
            path.write_text(json.dumps(judged, indent=1))
            usage = judged["summary"]["judge"]["usage"]
            log(f"{spec} on {run}: {judged['summary']['judge']['calls']} calls, ${usage['cost']:.2f}")
    return 0


def cmd_classify(args) -> int:
    """The same findings for every judge: those the reference judge's matcher left unmatched."""
    keys = pick(args)
    (OUT / "classify").mkdir(parents=True, exist_ok=True)
    sample: dict[str, dict[str, list[dict]]] = {}
    for run in args.runs.split(","):
        reference = json.loads((OUT / "match" / f"{run}__{slug(args.reference)}.json").read_text())["details"]
        candidates = candidates_of(run, keys)
        pool = []
        for key, entry in candidates.items():
            findings = entry["findings"]
            corr = [{"candidate_index": i, "matched_golden_indices": m} for i, m in enumerate(reference[key]["raw_matches"])]
            unmatched = resolve_for_scoring(corr, 10**6)["unmatched"]
            per_pr = sample_order(findings, unmatched)
            pool += [(rank, key, findings[i]) for rank, i in enumerate(per_pr)]
        pool.sort(key=lambda item: (item[0], item[1]))
        chosen = pool[: args.per_run]
        for _, key, finding in chosen:
            sample.setdefault(run, {}).setdefault(key, []).append(finding)
        log(f"{run}: {len(chosen)} findings from {len({k for _, k, _ in chosen})} PRs")
    (OUT / "classify" / "sample.json").write_text(json.dumps(sample, indent=1))
    for spec in args.judges.split(","):
        jobs = [(run, key, findings) for run, by_pr in sample.items() for key, findings in by_pr.items()]

        def work(job):
            run, key, findings = job
            got, used = classify_findings(key, findings, spec)
            return run, key, {str(i): got[i] for i in got}, used.cost

        out: dict = {}
        total = 0.0
        with cf.ThreadPoolExecutor(args.concurrency) as pool_:
            for run, key, got, cost in pool_.map(work, jobs):
                out.setdefault(run, {})[key] = got
                total += cost
        (OUT / "classify" / f"{slug(spec)}.json").write_text(json.dumps(out, indent=1))
        log(f"{spec}: classified, ${total:.2f}")
    return 0


def cmd_adjudicate(args) -> int:
    """Does a stronger judge overturn the working judge? Agreement on a sample that is mostly true findings says little, because a judge that
    says true every time scores well on it. So this takes a balanced set: as many findings the working judge called false as it called true."""
    run_dir = ROOT / "runs" / args.runs
    details = json.loads((run_dir / args.details).read_text())
    train = set(load_splits()["train"])
    outside = [k for k in details if k not in train]
    if outside:
        raise SystemExit(f"refusing PRs outside the train split: {outside[:3]}")
    pools: dict[bool, list[dict]] = {True: [], False: []}
    for key, entry in details.items():
        findings = json.loads((run_dir / "findings" / f"{key}.json").read_text())["findings"]
        for index, verdict in entry.get("classify_sample", {}).items():
            pools[verdict["tp_fp"] == "tp"].append({"key": key, "finding": findings[int(index)], "working": verdict["tp_fp"], "why": verdict.get("tp_fp_justification")})
    for label in pools:
        pools[label].sort(key=lambda item: sample_order([item["finding"]], [0]) and (item["key"], item["finding"]["file"], item["finding"]["message"]))
    chosen = pools[True][:: max(1, len(pools[True]) // args.per_run)][: args.per_run] + pools[False][:: max(1, len(pools[False]) // args.per_run)][: args.per_run]
    (OUT / "adjudicate").mkdir(parents=True, exist_ok=True)
    by_pr: dict[str, list[int]] = {}
    for position, item in enumerate(chosen):
        by_pr.setdefault(item["key"], []).append(position)
    for spec in args.judges.split(","):
        def work(pair):
            key, positions = pair
            got, _ = classify_findings(key, [chosen[p]["finding"] for p in positions], spec)
            return {positions[i]: v for i, v in got.items()}

        with cf.ThreadPoolExecutor(args.concurrency) as pool_:
            for result in pool_.map(work, list(by_pr.items())):
                for position, verdict in result.items():
                    chosen[position][spec] = verdict
        answered = [item for item in chosen if spec in item]
        log(f"{spec}: answered {len(answered)} of {len(chosen)}, ${sum(item[spec].get('judge_cost') or 0 for item in answered):.2f}")
    (OUT / "adjudicate" / f"{args.runs}.json").write_text(json.dumps(chosen, indent=1))
    lines = [f"# Stronger judges on {len(chosen)} findings the working judge had labeled ({args.runs})", ""]
    for spec in args.judges.split(","):
        answered = [item for item in chosen if spec in item]
        for working in ("tp", "fp"):
            group = [item for item in answered if item["working"] == working]
            kept = sum(item[spec]["tp_fp"] == working for item in group)
            lines.append(f"- {spec}: of {len(group)} the working judge called {working}, agrees on {kept}, overturns {len(group) - kept}")
    print("\n".join(lines))
    return 0


def _pairs_for(details: dict, candidates: list[dict], golden: list[dict]) -> dict[tuple[int, int], bool]:
    out = {}
    for c, matches in enumerate(details["raw_matches"]):
        for g, gold in enumerate(golden):
            if gold["file"] == candidates[c]["file"]:
                out[(c, g)] = g in matches
    return out


def cmd_report(args) -> int:
    keys = pick(args)
    runs = args.runs.split(",")
    judges = args.judges.split(",")
    lines = [f"# Judge study on {len(keys)} training PRs", ""]
    loaded = {(r, j): json.loads(p.read_text()) for r in runs for j in judges if (p := OUT / "match" / f"{r}__{slug(j)}.json").exists()}
    lines += ["## Matcher: grounded scores of each reviewer under each judge", "", "| Reviewer | Judge | P | R | F1 | Comments/PR | Cost |", "|---|---|---:|---:|---:|---:|---:|"]
    for run in runs:
        for judge in judges:
            data = loaded.get((run, judge))
            if not data:
                continue
            macro = data["summary"]["macro"]["overall"]
            usage = data["summary"]["judge"]["usage"]
            lines.append(f"| {run} | {judge} | {100 * (macro['grounded_precision'] or 0):.1f} | {100 * (macro['grounded_recall'] or 0):.1f} | {100 * (macro['grounded_f1'] or 0):.1f} | "
                         f"{macro['candidate_count'] / max(1, data['summary']['pr_count']):.1f} | ${usage['cost']:.2f} |")
    lines += ["", "## Matcher: agreement between judges", "", "Pair level is each (finding, golden finding in the same file). Golden level is whether a golden true positive counts as covered.", "",
              "| Judges | Pairs | Agreement | Kappa | Golden TPs | Agreement | Kappa | Decisions flipped |", "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for a, b in itertools.combinations(judges, 2):
        pairs: list[tuple[bool, bool]] = []
        golden_pairs: list[tuple[bool, bool]] = []
        for run in runs:
            if (run, a) not in loaded or (run, b) not in loaded:
                continue
            cands = candidates_of(run, keys)
            for key in cands:
                golden = load_golden(key)["findings"]
                da, db = loaded[(run, a)]["details"][key], loaded[(run, b)]["details"][key]
                pa, pb = _pairs_for(da, cands[key]["findings"], golden), _pairs_for(db, cands[key]["findings"], golden)
                pairs += [(pa[x], pb[x]) for x in pa]
                for g, gold in enumerate(golden):
                    if gold["tp_fp"] == "tp":
                        golden_pairs.append((g in da["covered_golden"], g in db["covered_golden"]))
        agree, kap = kappa(pairs)
        g_agree, g_kap = kappa(golden_pairs)
        if agree is None:
            continue
        flips = sum(x != y for x, y in golden_pairs)
        lines.append(f"| {a} / {b} | {len(pairs)} | {100 * agree:.1f}% | {kap if kap is None else round(kap, 3)} | {len(golden_pairs)} | {100 * g_agree:.1f}% | {g_kap if g_kap is None else round(g_kap, 3)} | {flips} |")
    lines += ["", "## Matcher: does each judge rank the reviewers the same way?", ""]
    for judge in judges:
        ranked = sorted((r for r in runs if (r, judge) in loaded), key=lambda r: -(loaded[(r, judge)]["summary"]["macro"]["overall"]["grounded_recall"] or 0))
        lines.append(f"- {judge}: " + " > ".join(f"{r} {100 * (loaded[(r, judge)]['summary']['macro']['overall']['grounded_recall'] or 0):.1f}" for r in ranked))
    classify_dir = OUT / "classify"
    if (classify_dir / "sample.json").exists():
        sample = json.loads((classify_dir / "sample.json").read_text())
        verdicts = {j: json.loads(p.read_text()) for j in judges if (p := classify_dir / f"{slug(j)}.json").exists()}
        lines += ["", "## Classifier: share of unmatched findings judged true", "", "| Reviewer | " + " | ".join(verdicts) + " |", "|---|" + "---:|" * len(verdicts)]
        for run, by_pr in sample.items():
            cells = []
            for judge, table in verdicts.items():
                labels = [v["tp_fp"] == "tp" for key in by_pr for v in table.get(run, {}).get(key, {}).values()]
                cells.append(f"{100 * sum(labels) / max(1, len(labels)):.0f}% of {len(labels)}")
            lines.append(f"| {run} | " + " | ".join(cells) + " |")
        lines += ["", "| Judges | Findings | Agreement | Kappa |", "|---|---:|---:|---:|"]
        disagreements = []
        for a, b in itertools.combinations(verdicts, 2):
            pairs = []
            for run, by_pr in sample.items():
                for key, findings in by_pr.items():
                    for index, finding in enumerate(findings):
                        va, vb = verdicts[a].get(run, {}).get(key, {}).get(str(index)), verdicts[b].get(run, {}).get(key, {}).get(str(index))
                        if va and vb:
                            pairs.append((va["tp_fp"] == "tp", vb["tp_fp"] == "tp"))
                            if va["tp_fp"] != vb["tp_fp"]:
                                disagreements.append({"judges": [a, b], "run": run, "pr": key, "file": finding["file"], "line": finding["start_line"], "message": finding["message"],
                                                      a: {"tp_fp": va["tp_fp"], "why": va.get("tp_fp_justification")}, b: {"tp_fp": vb["tp_fp"], "why": vb.get("tp_fp_justification")}})
            agree, kap = kappa(pairs)
            if agree is not None:
                lines.append(f"| {a} / {b} | {len(pairs)} | {100 * agree:.1f}% | {kap if kap is None else round(kap, 3)} |")
        (OUT / "disagreements.json").write_text(json.dumps(disagreements, indent=1))
        lines += ["", f"{len(disagreements)} disagreements saved to runs/judge-study/disagreements.json, for reading against the code."]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("cmd", choices=["match", "classify", "report", "adjudicate"])
    parser.add_argument("--runs", required=True)
    parser.add_argument("--judges", required=True)
    parser.add_argument("--select", default="")
    parser.add_argument("--details", default="results.details.json", help="adjudicate: the details file holding the working judge's sample")
    parser.add_argument("--per-run", type=int, default=20)
    parser.add_argument("--reference", default="claude-sonnet-5-5")
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--out", type=Path, default=OUT / "report.md")
    args = parser.parse_args()
    if args.cmd != "adjudicate" and not args.select:
        raise SystemExit("--select is required")
    return {"match": cmd_match, "classify": cmd_classify, "report": cmd_report, "adjudicate": cmd_adjudicate}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
