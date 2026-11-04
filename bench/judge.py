#!/usr/bin/env python3
"""Score findings the way ReviewBench does, with Claude Sonnet 5.5 or better as the judge.

Reused from the pinned ReviewBench clone: the matcher and classifier prompts, read as
text. Ported from its TypeScript: the finding rendering and the claim and scoring rules
(tests/test_scoring.py pins them to the methodology's rules). What differs from the
official judge is the model and the transport: the leaderboard is judged by Claude
Sonnet 5, which this project does not use, so offline numbers are a proxy. Compare runs
with each other, and do not read them as leaderboard scores.

Grounded metrics need only the matcher, since unmatched findings do not affect them. The
costlier classifier runs only with --classify, for the augmented metrics.

  judge.py --candidate DIR --output results.json [--classify] [--model M#effort] [--concurrency N]
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import re
import sys
import threading
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import CACHE, RB_DIR, ensure_reviewbench, load_golden, load_manifest, log, pr_key, sha256
from llm import DEFAULT_JUDGE_MODEL, ClaudeHost, LockedClaudeHost, SystemPromptCodexHost, Usage, require_judge, split_spec

CHUNK_SIZE = 10
JUDGE_CACHE = CACHE / "judge-cache"
_cache_lock = threading.Lock()


# --- Prompts: read from the pinned ReviewBench sources, never restated here ----------

def _template_literal(path: Path, name: str) -> str:
    source = path.read_text()
    match = re.search(rf"export const {name}\s*=\s*`(.*?)`;", source, re.S)
    if not match:
        raise RuntimeError(f"{name} not found in {path}")
    # The only template-literal escapes these prompts use.
    return match.group(1).replace("\\`", "`").replace("\\$", "$")


def _data_instruction(path: Path) -> str:
    source = path.read_text()
    match = re.search(r"export const FINDINGS_DATA_INSTRUCTION\s*=\s*((?:\s*\".*?\"\s*\+?)+);", source, re.S)
    if not match:
        raise RuntimeError("FINDINGS_DATA_INSTRUCTION not found")
    parts = re.findall(r"\"((?:[^\"\\]|\\.)*)\"", match.group(1))
    return "".join(json.loads(f'"{part}"') for part in parts)


def matcher_system_prompt() -> str:
    ensure_reviewbench()
    return (_template_literal(RB_DIR / "scripts/eval/matcher.ts", "MATCHER_SYSTEM_PROMPT")
            + _data_instruction(RB_DIR / "scripts/eval/prompt-format.ts"))


def classifier_system_prompt() -> str:
    ensure_reviewbench()
    return (_template_literal(RB_DIR / "scripts/classifier/prompts.ts", "CLASSIFIER_SYSTEM_PROMPT")
            + _data_instruction(RB_DIR / "scripts/eval/prompt-format.ts"))


# --- Rendering: scripts/eval/prompt-format.ts --------------------------------------

def escape_text(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;")


def escape_attr(text: str) -> str:
    return escape_text(text).replace('"', "&quot;")


def render_matcher_finding(role: str, finding: dict, index: int) -> str:
    return (f'<{role} index="{index}" file="{escape_attr(finding["file"])}" '
            f'lines="{finding["start_line"]}-{finding["end_line"]}">{escape_text(finding["message"])}</{role}>')


def build_matcher_prompt(candidates: list[dict], goldens: list[dict]) -> str:
    cand = "\n".join(render_matcher_finding("candidate", f, i) for i, f in enumerate(candidates))
    gold = "\n".join(render_matcher_finding("golden", f, i) for i, f in enumerate(goldens))
    return f"## Candidate findings\n{cand}\n\n## Golden findings\n{gold}"


# --- Scoring: scripts/lib/match-types.ts and scripts/eval/scorer.ts ----------------

def resolve_for_scoring(correspondences: list[dict], golden_count: int) -> dict:
    """Each candidate, in order, claims its first unclaimed golden match (in golden
    order) for precision. Every raw match covers its golden finding for recall."""
    covered: set[int] = set()
    claimed: dict[int, int] = {}
    matched_to_golden: dict[int, int] = {}
    seen: set[int] = set()
    for corr in correspondences:
        ci = corr["candidate_index"]
        if ci in seen:
            continue
        seen.add(ci)
        valid = sorted({g for g in corr["matched_golden_indices"] if isinstance(g, int) and 0 <= g < golden_count})
        covered.update(valid)
        for gi in valid:
            if gi not in claimed:
                claimed[gi] = ci
                matched_to_golden[ci] = gi
                break
    candidates = {c["candidate_index"] for c in correspondences}
    return {"matched_to_golden": matched_to_golden, "unmatched": candidates - set(matched_to_golden), "covered": covered}


def compute_metrics(golden_tp: set[int], matched: dict[int, int], covered: set[int],
                    unmatched_tp: int, unmatched_fp: int, candidate_count: int) -> dict:
    tp_count = len(golden_tp)
    matched_tp = sum(1 for g in matched.values() if g in golden_tp)
    matched_count = len(matched)
    covered_tp = sum(1 for g in covered if g in golden_tp)
    denom = tp_count + unmatched_tp
    return {
        "grounded_precision": matched_tp / matched_count if matched_count else None,
        "grounded_recall": covered_tp / tp_count if tp_count else None,
        "augmented_precision": (matched_tp + unmatched_tp) / candidate_count if candidate_count else None,
        "augmented_recall": (covered_tp + unmatched_tp) / denom if denom else None,
        "candidate_count": candidate_count,
        "golden_tp_count": tp_count,
        "matched_count": matched_count,
        "matched_tp_count": matched_tp,
        "unmatched_count": unmatched_tp + unmatched_fp,
        "unmatched_tp_count": unmatched_tp,
        "unmatched_fp_count": unmatched_fp,
        "golden_covered_count": covered_tp,
    }


def _stratum(golden: list[dict], resolved: dict, classes: dict[int, dict], axis: str, value: str) -> dict:
    tp = {i for i, g in enumerate(golden) if g[axis] == value and g["tp_fp"] == "tp"}
    matched = {c: g for c, g in resolved["matched_to_golden"].items() if golden[g][axis] == value}
    covered = {g for g in resolved["covered"] if golden[g][axis] == value}
    u_tp = sum(1 for c in classes.values() if c.get(axis) == value and c["tp_fp"] == "tp")
    u_fp = sum(1 for c in classes.values() if c.get(axis) == value and c["tp_fp"] != "tp")
    return compute_metrics(tp, matched, covered, u_tp, u_fp, len(matched) + u_tp + u_fp)


def score_pr(golden: list[dict], candidate_count: int, resolved: dict, classifications: dict[int, dict]) -> dict:
    # An unmatched finding with no classification counts as an unclassified FP.
    classes = {i: classifications.get(i, {"tp_fp": "fp", "severity": "unclassified", "category": "unclassified"})
               for i in resolved["unmatched"]}
    tp = {i for i, g in enumerate(golden) if g["tp_fp"] == "tp"}
    u_tp = sum(1 for c in classes.values() if c["tp_fp"] == "tp")
    u_fp = len(classes) - u_tp
    overall = compute_metrics(tp, resolved["matched_to_golden"], resolved["covered"], u_tp, u_fp, candidate_count)
    severities = {g["severity"] for g in golden} | {c["severity"] for c in classes.values()}
    categories = {g["category"] for g in golden} | {c["category"] for c in classes.values()}
    return {
        "overall": overall,
        "by_severity": {s: _stratum(golden, resolved, classes, "severity", s) for s in sorted(severities)},
        "by_category": {c: _stratum(golden, resolved, classes, "category", c) for c in sorted(categories)},
    }


COUNTS = ("candidate_count", "golden_tp_count", "matched_count", "matched_tp_count", "unmatched_count",
          "unmatched_tp_count", "unmatched_fp_count", "golden_covered_count")
RATIOS = ("grounded_precision", "grounded_recall", "augmented_precision", "augmented_recall")


def f1(p: float | None, r: float | None) -> float | None:
    if p is None or r is None:
        return None
    return 2 * p * r / (p + r) if p + r else 0.0


def micro(sets: list[dict]) -> dict:
    total = {k: sum(m[k] for m in sets) for k in COUNTS}
    mt, m, g, c = total["matched_tp_count"], total["matched_count"], total["golden_tp_count"], total["golden_covered_count"]
    ut, cc = total["unmatched_tp_count"], total["candidate_count"]
    out = dict(total)
    out["grounded_precision"] = mt / m if m else None
    out["grounded_recall"] = c / g if g else None
    out["augmented_precision"] = (mt + ut) / cc if cc else None
    out["augmented_recall"] = (c + ut) / (g + ut) if g + ut else None
    out["grounded_f1"] = f1(out["grounded_precision"], out["grounded_recall"])
    return out


def macro(sets: list[dict]) -> dict:
    out: dict = {k: sum(m[k] for m in sets) for k in COUNTS}
    for ratio in RATIOS:
        values = [m[ratio] for m in sets if m[ratio] is not None]
        out[ratio] = sum(values) / len(values) if values else None
    out["grounded_f1"] = f1(out["grounded_precision"], out["grounded_recall"])
    return out


def aggregate(results: dict[str, dict]) -> dict:
    prs = list(results.values())

    def strat(fn):
        out = {"overall": fn([r["overall"] for r in prs])}
        for axis in ("by_severity", "by_category"):
            names = {name for r in prs for name in r[axis]}
            out[axis] = {name: fn([r[axis][name] for r in prs if name in r[axis]]) for name in sorted(names)}
        return out

    return {"pr_count": len(prs), "micro": strat(micro), "macro": strat(macro)}


# --- Matching ---------------------------------------------------------------------

def _extract_json(text: str):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", text)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                return None
    return None


def parse_matcher_response(text: str, candidate_count: int, golden_count: int) -> list[list[int]]:
    parsed = _extract_json(text)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("candidateFindings"), list):
        raise ValueError("matcher returned invalid JSON")
    out: list[list[int] | None] = [None] * candidate_count
    for entry in parsed["candidateFindings"]:
        index = entry.get("index") if isinstance(entry, dict) else None
        golds = entry.get("matchedGoldenIndices") if isinstance(entry, dict) else None
        if not isinstance(index, int) or not 0 <= index < candidate_count or out[index] is not None or not isinstance(golds, list):
            raise ValueError("matcher returned invalid candidate entries")
        if not all(isinstance(g, int) and 0 <= g < golden_count for g in golds):
            raise ValueError("matcher returned invalid golden indices")
        out[index] = golds
    if any(v is None for v in out):
        raise ValueError("matcher omitted candidates")
    return out  # type: ignore[return-value]


class Matcher:
    """Compares one chunk at a time and caches by content, so re-judging an unchanged
    finding across loop iterations costs nothing and cannot drift."""

    def __init__(self, server, model: str, system_prompt: str):
        self.server = server
        self.model = model
        self.system_hash = sha256(system_prompt)
        self.usage = Usage()
        self.calls = 0
        self.cache_hits = 0
        self._lock = threading.Lock()

    def match_chunk(self, candidates: list[dict], goldens: list[dict]) -> list[list[int]]:
        prompt = build_matcher_prompt(candidates, goldens)
        key = sha256(f"{self.model}\n{self.system_hash}\n{prompt}")
        path = JUDGE_CACHE / key[:2] / f"{key}.json"
        if path.exists():
            with self._lock:
                self.cache_hits += 1
            return json.loads(path.read_text())
        error: Exception | None = None
        for _ in range(3):
            # A judge at max effort can think for many minutes over one chunk.
            reply = self.server.ask(prompt, timeout=1200 if self.model.endswith("#max") else 400)
            with self._lock:
                self.calls += 1
                self.usage.add(reply.usage)
            if reply.error:
                error = RuntimeError(reply.error)
                continue
            try:
                result = parse_matcher_response(reply.text, len(candidates), len(goldens))
            except ValueError as exc:
                error = exc
                continue
            with _cache_lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(result))
            return result
        raise RuntimeError(f"matcher failed: {error}")

    def match(self, candidates: list[dict], goldens: list[dict], pool: cf.Executor) -> list[list[int]]:
        """Same pairing as ReviewBench: same file only, cross-product of 10-finding chunks."""
        by_file_c: dict[str, list[int]] = {}
        by_file_g: dict[str, list[int]] = {}
        for i, f in enumerate(candidates):
            by_file_c.setdefault(f["file"], []).append(i)
        for i, f in enumerate(goldens):
            by_file_g.setdefault(f["file"], []).append(i)
        jobs = []
        for file, c_idx in by_file_c.items():
            g_idx = by_file_g.get(file)
            if not g_idx:
                continue
            for a in range(0, len(c_idx), CHUNK_SIZE):
                for b in range(0, len(g_idx), CHUNK_SIZE):
                    cc, gg = c_idx[a:a + CHUNK_SIZE], g_idx[b:b + CHUNK_SIZE]
                    jobs.append((cc, gg, pool.submit(self.match_chunk, [candidates[i] for i in cc], [goldens[i] for i in gg])))
        raw: list[list[int]] = [[] for _ in candidates]
        for cc, gg, future in jobs:
            for local, golds in enumerate(future.result()):
                for g in golds:
                    if gg[g] not in raw[cc[local]]:
                        raw[cc[local]].append(gg[g])
        return raw


# --- Classifier (only for augmented metrics) ---------------------------------------

def matcher_host(spec: str, system: str):
    """A judge with no tools. It compares two sets of text, so it has no reason to read anything."""
    require_judge(spec)
    model, effort = split_spec(spec)
    return SystemPromptCodexHost(None, model, system, effort=effort) if model.startswith("gpt-") else LockedClaudeHost(model, system, effort=effort)


def classifier_host(spec: str, system: str, checkout: Path):
    """A judge with read-only tools, in the PR's checkout, because a finding has to be checked against the code."""
    require_judge(spec)
    model, effort = split_spec(spec)
    return SystemPromptCodexHost(checkout, model, system, effort=effort) if model.startswith("gpt-") else ClaudeHost(checkout, model, effort=effort, system_prompt=system)


def classify_findings(key: str, findings: list[dict], model: str) -> tuple[dict[int, dict], Usage]:
    """One session per finding, in the PR checkout, with read-only tools."""
    pr_dir = CACHE / "prs" / key
    entry = load_manifest()[key]
    diff = (pr_dir / "pr" / "diff.patch").read_text(errors="replace")
    if len(diff) > 100_000:
        diff = diff[:100_000] + "\n\n... [diff truncated, use tools to read full files] ..."
    context = (f"## PR Context\n\n**Repository:** {entry['nwo']}\n**SHA:** {entry['head']}\n**Title:** {entry['title']}\n\n"
               f"**Description:**\n{entry.get('body') or '(no description)'}\n\n## Diff\n\n```diff\n{diff}\n```\n\n---\n\n")
    system = classifier_system_prompt()
    usage = Usage()
    out: dict[int, dict] = {}

    def one(item):
        index, finding = item
        cache_key = sha256(f"{model}\n{sha256(system)}\n{key}\n{finding['file']}\n{finding['start_line']}\n{finding['end_line']}\n{finding['message']}")
        path = JUDGE_CACHE / "classify" / f"{cache_key}.json"
        if path.exists():
            return index, json.loads(path.read_text()), Usage()
        body = (f"<finding>\n<file>{escape_text(finding['file'])}</file>\n<lines>{finding['start_line']}-{finding['end_line']}</lines>\n"
                f"<message>{escape_text(finding['message'])}</message>\n</finding>\n\n---\n\n"
                'Classify this finding. Use tools to verify the claim if needed. End your response with EXACTLY a JSON object: '
                '{"tp_fp": "...", "tp_fp_justification": "...", "severity": "...", "severity_justification": "...", '
                '"category": "...", "category_justification": "...", "scope": "...", "difficulty": "...", "context_required": "..."}')
        reply = server.ask(context + body, timeout=1500 if model.endswith("#max") else 600)
        if reply.error:
            return index, None, reply.usage
        match = None
        for match in re.finditer(r"\{[^{}]*\"tp_fp\"[^{}]*\}", reply.text):
            pass
        if not match:
            return index, None, reply.usage
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return index, None, reply.usage
        if parsed.get("tp_fp") not in ("tp", "fp") or not parsed.get("severity") or not parsed.get("category"):
            return index, None, reply.usage
        # Whether the judge looked at the code at all matters as much as what it decided.
        parsed["judge_tools"] = sum(reply.tools.values())
        parsed["judge_cost"] = reply.usage.cost
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(parsed))
        return index, parsed, reply.usage

    with classifier_host(model, system, pr_dir / "repo") as server:
        with cf.ThreadPoolExecutor(4) as pool:
            for index, parsed, used in pool.map(one, list(enumerate(findings))):
                usage.add(used)
                if parsed:
                    out[index] = parsed
    return out, usage


# --- Driver -----------------------------------------------------------------------

def load_candidates(path: Path) -> dict[str, dict]:
    files = [path] if path.is_file() else sorted(path.rglob("*.json"))
    by_head = {e["head"]: k for k, e in load_manifest().items()}
    out: dict[str, dict] = {}
    for file in files:
        try:
            data = json.loads(file.read_text())
        except json.JSONDecodeError:
            log(f"skipping malformed {file}")
            continue
        if not isinstance(data, dict) or "pr" not in data or "findings" not in data:
            continue
        key = by_head.get(data["pr"].get("head")) or pr_key(data["pr"])
        slot = out.setdefault(key, {"pr": data["pr"], "findings": []})
        slot["findings"].extend(data["findings"])
    return out


def sample_order(findings: list[dict], indices: set[int] | list[int]) -> list[int]:
    """Stable under change: a finding keeps its place however many others come and go, so two versions of the agent share most of
    their sample, the judge's cache replays those answers, and the difference between the two estimates is not sampling noise."""
    return sorted(indices, key=lambda i: sha256(f"{findings[i]['file']}\n{findings[i]['message']}"))


def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if not n:
        return (0.0, 1.0)
    p = successes / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    margin = z * ((p * (1 - p) + z * z / (4 * n)) / n) ** 0.5 / (1 + z * z / n)
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def estimate_augmented(results: dict[str, dict], sampled: dict[str, dict]) -> dict | None:
    """Augmented metrics from a sample of the unmatched findings. Classifying them all costs a session each, too much to do
    every round. Applies the pooled rate of true findings to each PR's unmatched count, then averages over PRs."""
    labels = [v["tp_fp"] == "tp" for pr in sampled.values() for v in pr.values()]
    if not labels:
        return None
    rate = sum(labels) / len(labels)
    rows = []
    for metrics in results.values():
        m = metrics["overall"]
        novel = rate * m["unmatched_count"]
        count = m["candidate_count"]
        rows.append({"p": (m["matched_tp_count"] + novel) / count if count else None,
                     "r": (m["golden_covered_count"] + novel) / (m["golden_tp_count"] + novel) if m["golden_tp_count"] + novel else None,
                     "y": novel / count if count else None})

    def mean(field: str) -> float | None:
        values = [row[field] for row in rows if row[field] is not None]
        return sum(values) / len(values) if values else None

    precision, recall = mean("p"), mean("r")
    low, high = wilson(sum(labels), len(labels))
    return {"sampled": len(labels), "novel_tp_rate": rate, "novel_tp_rate_ci95": [low, high], "augmented_precision": precision, "augmented_recall": recall,
            "augmented_f1": f1(precision, recall), "novel_yield": mean("y")}


def judge_candidates(candidates: dict[str, dict], *, model: str, concurrency: int = 12, classify: bool = False,
                     exclude_producers: tuple[str, ...] = (), classify_sample: int = 0) -> dict:
    """`classify_sample` classifies that many unmatched findings per PR and reports the result as an estimate, in
    `augmented_estimate`. The per-PR augmented numbers are only meaningful with `classify`."""
    require_judge(model)
    system = matcher_system_prompt()
    results: dict[str, dict] = {}
    details: dict[str, dict] = {}
    samples: dict[str, dict] = {}
    classifier_usage = Usage()
    lock = threading.Lock()
    with matcher_host(model, system) as server:
        matcher = Matcher(server, model, system)
        with cf.ThreadPoolExecutor(concurrency) as chunk_pool:

            def judge_one(key: str) -> None:
                golden = [g for g in load_golden(key)["findings"]
                          if not (exclude_producers and g["producer"].startswith(exclude_producers))]
                findings = candidates[key]["findings"]
                raw = matcher.match(findings, golden, chunk_pool)
                corr = [{"candidate_index": i, "matched_golden_indices": m} for i, m in enumerate(raw)]
                resolved = resolve_for_scoring(corr, len(golden))
                classes: dict[int, dict] = {}
                used = Usage()
                if classify and resolved["unmatched"]:
                    order = sorted(resolved["unmatched"])
                    got, used = classify_findings(key, [findings[i] for i in order], model)
                    classes = {order[i]: v for i, v in got.items()}
                sample: dict[int, dict] = {}
                if classify_sample and not classify and resolved["unmatched"]:
                    order = sample_order(findings, resolved["unmatched"])[:classify_sample]
                    got, used = classify_findings(key, [findings[i] for i in order], model)
                    sample = {order[i]: v for i, v in got.items()}
                score = score_pr(golden, len(findings), resolved, classes)
                with lock:
                    classifier_usage.add(used)
                    results[key] = score
                    samples[key] = sample
                    details[key] = {
                        "classify_sample": {str(i): sample[i] for i in sample},
                        "raw_matches": raw,
                        "matched_to_golden": {str(c): g for c, g in resolved["matched_to_golden"].items()},
                        "covered_golden": sorted(resolved["covered"]),
                        "classifications": {str(i): classes[i] for i in classes},
                    }
                m = score["overall"]
                log(f"  {key}: {len(findings)}c, {m['golden_covered_count']}/{m['golden_tp_count']} TP covered, "
                    f"matched {m['matched_tp_count']}/{m['matched_count']}")

            with cf.ThreadPoolExecutor(min(8, max(1, len(candidates)))) as pr_pool:
                for future in [pr_pool.submit(judge_one, key) for key in sorted(candidates)]:
                    future.result()
    summary = aggregate(results)
    summary["judge"] = {"model": model, "calls": matcher.calls, "cache_hits": matcher.cache_hits,
                        "usage": vars(matcher.usage), "classifier_usage": vars(classifier_usage), "classified": classify}
    summary["augmented_estimate"] = estimate_augmented(results, samples) if classify_sample and not classify else None
    summary["per_pr"] = results
    return {"summary": summary, "details": details}


def pct(v) -> str:
    return "  n/a " if v is None else f"{100 * v:5.1f}%"


def headline(summary: dict) -> str:
    rows = []
    for avg in ("micro", "macro"):
        strata = [("all", summary[avg]["overall"])] + [(s, summary[avg]["by_severity"].get(s)) for s in ("high", "medium", "low")]
        for label, m in strata:
            if m:
                rows.append(f"{avg:5} {label:6} F1 {pct(m['grounded_f1'])}  P {pct(m['grounded_precision'])}  "
                            f"R {pct(m['grounded_recall'])}  covered {m['golden_covered_count']}/{m['golden_tp_count']}")
    overall = summary["micro"]["overall"]
    line = (f"comments/PR {overall['candidate_count'] / max(1, summary['pr_count']):.1f}  matched {overall['matched_count']}  "
            f"matched-FP {overall['matched_count'] - overall['matched_tp_count']}")
    if summary.get("judge", {}).get("classified"):
        line += f"  augmented P {pct(overall['augmented_precision'])} R {pct(overall['augmented_recall'])}"
    estimate = summary.get("augmented_estimate")
    if estimate:
        low, high = estimate["novel_tp_rate_ci95"]
        line += (f"\nestimated from {estimate['sampled']} classified: augmented P {pct(estimate['augmented_precision'])} R {pct(estimate['augmented_recall'])} F1 {pct(estimate['augmented_f1'])}, "
                 f"novel yield {pct(estimate['novel_yield'])}, unmatched true {pct(estimate['novel_tp_rate'])} ({100 * low:.0f}-{100 * high:.0f}%)")
    return "\n".join(rows + [line])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--model", default=DEFAULT_JUDGE_MODEL)
    parser.add_argument("--concurrency", type=int, default=12)
    parser.add_argument("--classify", action="store_true", help="also classify unmatched findings (augmented metrics)")
    parser.add_argument("--classify-sample", type=int, default=0, help="classify this many unmatched findings per PR, and estimate augmented metrics from them")
    parser.add_argument("--exclude-producer", action="append", default=[], help="leave-producer-out: drop golden findings with this producer prefix")
    args = parser.parse_args()
    candidates = load_candidates(args.candidate)
    if not candidates:
        raise SystemExit(f"no findings files under {args.candidate}")
    out = judge_candidates(candidates, model=args.model, concurrency=args.concurrency, classify=args.classify,
                           exclude_producers=tuple(args.exclude_producer), classify_sample=args.classify_sample)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(out["summary"], indent=1))
    args.output.with_suffix(".details.json").write_text(json.dumps(out["details"], indent=1))
    print(headline(out["summary"]))
    judge = out["summary"]["judge"]
    print(f"judge {args.model}: {judge['calls']} calls, {judge['cache_hits']} cached")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
