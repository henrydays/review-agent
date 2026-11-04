#!/usr/bin/env python3
"""The self-improvement loop: propose a change to the skill, test it on repositories the proposer never
saw, keep it only if it helps.

  run_loop.py evaluate --candidate DIR --hypothesis "..."   test a change written by someone else, usually the session driving this loop
  run_loop.py iterate [--rounds 5] [--train train_quick] [--val val_quick]
  run_loop.py baseline [--select val]      score the current skill
  run_loop.py holdout [--select maint_holdout]  final estimate on PRs the loop never touched; use rarely
  run_loop.py status                       current skill, its scores, usage meters, history

One round:

  1. Score the current skill (the champion) on the training and validation PRs.
  2. Trace each missed golden finding through the pipeline (diagnose.py).
  3. An improver agent edits a copy of the skill, given the diagnosis and past experiments.
  4. Guard: reject edits outside the prompts and knobs, memorized answers, prompt bloat (guard.py).
  5. Evaluate on the training PRs. Only re-runs tasks whose prompts changed, thanks to the task cache.
  6. If it helped there, evaluate on the validation PRs, from different repositories.
  7. Gate: accept only a gain that survives paired resampling and stays within budget (gate.py).

Every experiment lands in loop/ledger.jsonl, whatever the outcome. That file is the loop's memory.
"""
from __future__ import annotations

import argparse
import filecmp
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bench"))
sys.path.insert(0, str(ROOT / "loop"))
from common import load_golden, load_manifest, load_splits, log, maintainer_manifest, resolve_keys, reviewbench_manifest, sha256  # noqa: E402
from diagnose import diagnose  # noqa: E402
from gate import comments_per_pr, compare, decide, macro_f1, objective, precision_delta, rate_gain_z, training_problem, validation_problem, worth_validating  # noqa: E402
from guard import build_vocabulary, check  # noqa: E402
from judge import headline, judge_candidates, load_candidates  # noqa: E402
from llm import DEFAULT_JUDGE_MODEL, OpencodeHost, opencode_config, over_budget, usage_limits  # noqa: E402
from run import RUNS, run as run_reviews  # noqa: E402

SKILL = ROOT / "skill" / "pr-review"
WORK = ROOT / "loop" / "work"
LEDGER = ROOT / "loop" / "ledger.jsonl"
IMPROVER_PROMPT = ROOT / "loop" / "improver.md"
DEFAULT_IMPROVER_MODEL = "opencode-go/deepseek-v4-pro"
# The reviewers. The judge's default is in bench/llm.py.
DEFAULT_HOST = "claude"
DEFAULT_REVIEW_MODEL = "claude-haiku-5-5"


def fingerprint(skill: Path) -> str:
    """Changes when anything a review depends on changes."""
    digest = hashlib.sha256()
    for path in sorted(skill.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            digest.update(str(path.relative_to(skill)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:10]


def ledger_records() -> list[dict]:
    if not LEDGER.exists():
        return []
    return [json.loads(line) for line in LEDGER.read_text().splitlines() if line.strip()]


def record(entry: dict) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def augmented_precision_of(run_dir: Path) -> float | None:
    estimate = json.loads((run_dir / "results.json").read_text()).get("augmented_estimate")
    return estimate["augmented_precision"] if estimate else None


def augmented_change(before_run: Path, after_run: Path) -> tuple[float | None, float | None]:
    """The change in estimated augmented precision, and how unlikely it is to be chance, from the classified samples."""
    before = json.loads((before_run / "results.json").read_text()).get("augmented_estimate")
    after = json.loads((after_run / "results.json").read_text()).get("augmented_estimate")
    if not before or not after:
        return None, None
    z = rate_gain_z(round(before["novel_tp_rate"] * before["sampled"]), before["sampled"], round(after["novel_tp_rate"] * after["sampled"]), after["sampled"])
    return after["augmented_precision"] - before["augmented_precision"], z


def run_cost(run_dir: Path) -> dict:
    """Tokens and an estimate of real wall-clock time, using the numbers saved with each task, so
    tasks answered from the cache still count at what they originally cost. Time is estimated per stage
    as its slowest task, since all tasks of a stage run at once."""
    tokens = usd = 0.0
    seconds: list[float] = []
    for artifacts in sorted((run_dir / "artifacts").glob("*")):
        stage_max: dict[str, float] = {}
        for path in (artifacts / "results").glob("*.json"):
            try:
                result = json.loads(path.read_text())
            except json.JSONDecodeError:
                continue
            usage = result.get("usage") or {}
            tokens += float(usage.get("input") or 0) + float(usage.get("output") or 0) + float(usage.get("reasoning") or 0) + 0.1 * float(usage.get("cache_read") or 0)
            usd += float(usage.get("cost") or 0)
            stage = path.stem[0]
            stage_max[stage] = max(stage_max.get(stage, 0.0), float(result.get("seconds") or 0))
        seconds.append(sum(stage_max.values()))
    return {"tokens": tokens, "usd": usd, "usd_per_pr": usd / len(seconds) if seconds else 0.0, "mean_seconds": sum(seconds) / len(seconds) if seconds else 0.0, "max_seconds": max(seconds, default=0.0)}


def cost_ratio_between(champion_run: Path, candidate_run: Path) -> float | None:
    """In dollars when both runs report them, since a token of output costs far more than a token of cached input."""
    champion, candidate = run_cost(champion_run), run_cost(candidate_run)
    if champion["usd"] and candidate["usd"]:
        return candidate["usd"] / champion["usd"]
    return candidate["tokens"] / champion["tokens"] if champion["tokens"] else None


def corpus_fingerprint(selector: str) -> str:
    """Two runs can only be compared if they were judged on the same golden findings. ReviewBench's are pinned, but
    this corpus can be relabeled, and a champion reused from before that would be compared with a candidate from after."""
    mine = maintainer_manifest()
    keys = sorted(k for k in resolve_keys(selector) if k in mine)
    return "-g" + sha256(json.dumps([load_golden(k) for k in keys], sort_keys=True))[:8] if keys else ""


def run_name(skill: Path, selector: str, args, salt: str = "") -> str:
    # The judge's name is part of it: a score is only comparable with another from the same judge.
    return (f"{selector.replace(',', '+')}-{fingerprint(skill)}-{args.model.split('/')[-1]}-j-{args.judge_model.split('/')[-1].replace('#', '-')}"
            + corpus_fingerprint(selector) + (f"-{salt}" if salt else ""))


def ensure_run(skill: Path, selector: str, args, salt: str | None = None) -> Path:
    """Reviewed and judged once per version of the skill, split and salt."""
    salt = args.salt if salt is None else salt
    name = run_name(skill, selector, args, salt)
    out = RUNS / name
    if (out / "results.json").exists():
        log(f"reusing {name}")
        return out
    keys = resolve_keys(selector)
    log(f"reviewing {len(keys)} PRs as {name}")
    _, records = run_reviews(keys, name, skill_dir=skill.resolve(), model=args.model, prs_at_once=args.prs_at_once,
                             concurrency=args.concurrency, salt=salt, host=args.host, effort=args.effort)
    failed = [r for r in records if r.get("host_failure")]
    if failed:
        raise SystemExit(f"the model host failed: {failed[0]['host_failure']}")
    judged = judge_candidates(load_candidates(out / "findings"), model=args.judge_model, classify_sample=args.classify_sample)
    (out / "results.json").write_text(json.dumps(judged["summary"], indent=1))
    (out / "results.details.json").write_text(json.dumps(judged["details"], indent=1))
    log(headline(judged["summary"]))
    return out


def per_pr(run_dir: Path) -> dict[str, dict]:
    return json.loads((run_dir / "results.json").read_text())["per_pr"]


def scores(run_dir: Path) -> dict:
    prs = list(per_pr(run_dir).values())
    return {"objective": round(objective(prs), 4), "f1_high": macro_f1(prs, "high"), "f1_all": macro_f1(prs, "all")}


def history_markdown(limit: int = 30) -> str:
    lines = ["# Past experiments", ""]
    records = ledger_records()[-limit:]
    if not records:
        lines.append("None yet. This is the first.")
    for entry in records:
        lines.append(f"## {entry['id']}: {entry['outcome']}")
        lines.append(f"- Hypothesis: {entry.get('hypothesis', '(none)')}")
        lines.append(f"- Changes: {entry.get('changes', '(none)')}")
        if entry.get("train"):
            lines.append(f"- Training PRs: {entry['train']['champion']} to {entry['train']['candidate']} (delta {entry['train']['delta']:+})")
        if entry.get("val"):
            lines.append(f"- Validation PRs: {entry['val']['champion']} to {entry['val']['candidate']} (delta {entry['val']['delta']:+}, better in {entry['val']['p_better']:.0%} of resamples)")
        lines.append(f"- Decision: {entry.get('reason', '')}")
        lines.append("")
    return "\n".join(lines) + "\n"


def champion_snapshot(exp_dir: Path) -> Path:
    return exp_dir.parent / f"{exp_dir.name}.champion"


def create_sandbox(exp_dir: Path) -> Path:
    """A copy of the skill for the improver to edit. It has to be its own git repository: OpenCode
    treats the enclosing git root as the project and resolves relative paths against it, so without
    this an edit to `skill/pr-review/config.json` lands on the real champion."""
    exp_dir.mkdir(parents=True)
    candidate = exp_dir / "skill" / "pr-review"
    shutil.copytree(SKILL, candidate, ignore=shutil.ignore_patterns("__pycache__"))
    # Outside the sandbox, where the improver cannot mistake it for the copy to edit.
    shutil.copytree(SKILL, champion_snapshot(exp_dir), ignore=shutil.ignore_patterns("__pycache__"))
    subprocess.run(["git", "init", "-q", str(exp_dir)], check=True)
    top = subprocess.run(["git", "-C", str(exp_dir), "rev-parse", "--show-toplevel"], check=True, capture_output=True, text=True).stdout.strip()
    if Path(top).resolve() != exp_dir.resolve():
        raise RuntimeError(f"the sandbox is not its own git root: {top}")
    return candidate


def restore_if_tampered(skill: Path, snapshot: Path) -> list[str]:
    """If anything reached the real skill, put it back and name what changed. Without this check a
    leak would silently score the champion against itself, and the bug would look like a result."""
    def differences(comparison: filecmp.dircmp, prefix: str = "") -> list[str]:
        out = [prefix + name for name in comparison.left_only + comparison.right_only + comparison.diff_files
               if name != "__pycache__"]
        for name, sub in comparison.subdirs.items():
            if name != "__pycache__":
                out += differences(sub, f"{prefix}{name}/")
        return out

    filecmp.clear_cache()
    changed = differences(filecmp.dircmp(snapshot, skill))
    if changed:
        shutil.rmtree(skill)
        shutil.copytree(snapshot, skill)
    return sorted(changed)


def propose(exp_dir: Path, args) -> dict:
    """The improver works on a copy of the skill, with no shell and no web."""
    permission = {"*": "deny", "read": "allow", "grep": "allow", "glob": "allow", "list": "allow", "edit": "allow", "write": "allow"}
    agents = {"improver": {"description": "Improves the review prompts", "mode": "primary", "prompt": IMPROVER_PROMPT.read_text(),
                           "permission": permission, "steps": args.improver_steps}}
    prompt = "Read diagnosis.md and history.md, then make one focused improvement to the review agent in skill/pr-review/, as your instructions describe."
    with OpencodeHost(exp_dir, args.improver_model, config=opencode_config(agents), agent="improver") as host:
        reply = host.ask(prompt, timeout=args.improver_timeout, attempts=2)
    if reply.error:
        raise RuntimeError(f"improver failed: {reply.error}")
    match = None
    for match in re.finditer(r"\{[^{}]*\"hypothesis\"[^{}]*\}", reply.text):
        pass
    try:
        parsed = json.loads(match.group(0)) if match else {}
    except json.JSONDecodeError:
        parsed = {}
    return {"hypothesis": parsed.get("hypothesis") or reply.text[:400], "evidence": parsed.get("evidence", ""), "changes": parsed.get("changes", ""),
            "tools": reply.tools, "usage": vars(reply.usage)}


def training_vocabulary(selector: str) -> set[str]:
    manifest = load_manifest()
    keys = resolve_keys(selector)
    # A word counts as ordinary by how many repositories use it, so a run that never asked for the maintainer corpus
    # must not have that corpus change which words it may use.
    if not set(keys) & set(maintainer_manifest()):
        manifest = {k: e for k, e in manifest.items() if k in reviewbench_manifest()}
    by_repo: dict[str, list[dict]] = {}
    for key in keys:
        by_repo.setdefault(manifest[key]["nwo"], []).extend(load_golden(key)["findings"])
    background: dict[str, list[dict]] = {}
    for key, entry in manifest.items():
        background.setdefault(entry["nwo"], []).extend(load_golden(key)["findings"])
    return build_vocabulary(by_repo, background=background)


def accept(candidate: Path, entry: dict, args) -> None:
    for path in sorted(SKILL.rglob("*"), reverse=True):
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(SKILL)
        if not (candidate / relative).exists():
            path.unlink() if path.is_file() else path.rmdir()
    for path in candidate.rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts:
            target = SKILL / path.relative_to(candidate)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(path, target)
    subprocess.run([sys.executable, str(ROOT / "prompt" / "build_prompt.py")], check=True, capture_output=True)
    if args.commit:
        message = f"loop: {entry['id']}\n\n{entry.get('hypothesis', '')}\n\nValidation delta {entry['val']['delta']:+}, better in {entry['val']['p_better']:.0%} of resamples."
        subprocess.run(["git", "-C", str(ROOT), "add", "skill", "prompt", "loop/ledger.jsonl"], check=True)
        subprocess.run(["git", "-C", str(ROOT), "commit", "-qm", message], check=True)


def corpus_guards(champion: dict[str, dict], candidate: dict[str, dict]) -> dict:
    """When validation mixes corpora, how each moved. Empty for ReviewBench alone, the default."""
    maintainer = set(maintainer_manifest())
    keys = sorted(set(champion) & set(candidate))
    theirs = [k for k in keys if k in maintainer]
    if not theirs:
        return {}
    ours = [k for k in keys if k not in maintainer]
    baseline = next((r["maintainer_baseline_comments"] for r in ledger_records() if r.get("maintainer_baseline_comments")), None) or comments_per_pr(champion, theirs)
    after = comments_per_pr(candidate, theirs)
    return {"maintainer_baseline_comments": baseline, "maintainer_volume_ratio": after / baseline if baseline else None,
            "val_maintainer": compare({k: champion[k] for k in theirs}, {k: candidate[k] for k in theirs}),
            "val_reviewbench": compare({k: champion[k] for k in ours}, {k: candidate[k] for k in ours}) if ours else None}


def one_round(args) -> str:
    champion_id = fingerprint(SKILL)
    log(f"\n=== round: champion {champion_id} ===")
    train_run = ensure_run(SKILL, args.train, args)
    val_run = ensure_run(SKILL, args.val, args)
    if not (train_run / "diagnosis.md").exists():
        log("tracing where findings were lost")
        diagnose(train_run, args.judge_model)
    exp_id = time.strftime("%Y%m%d-%H%M%S")
    exp_dir = WORK / exp_id
    candidate = create_sandbox(exp_dir)
    shutil.copy(train_run / "diagnosis.md", exp_dir / "diagnosis.md")
    (exp_dir / "history.md").write_text(history_markdown())
    entry: dict = {"id": exp_id, "champion": champion_id, "train_set": args.train, "val_set": args.val, "model": args.model, "judge_model": args.judge_model,
                   "improver_model": args.improver_model, "champion_scores": {"train": scores(train_run), "val": scores(val_run)}}
    log("asking the improver for one change")
    failure = None
    try:
        entry.update(propose(exp_dir, args))
    except RuntimeError as exc:
        failure = str(exc)
    escaped = restore_if_tampered(SKILL, champion_snapshot(exp_dir))
    if escaped:
        entry.update(outcome="sandbox_escape", reason=f"the real skill changed during the proposal, and was restored: {escaped}")
        record(entry)
        raise SystemExit(f"stopping: {entry['reason']}")
    if failure:
        entry.update(outcome="improver_failed", reason=failure)
        record(entry)
        return entry["outcome"]
    result = check(SKILL, candidate, training_vocabulary(args.train))
    entry["changed_files"] = result["changed"]
    log(f"proposal: {entry['hypothesis'][:300]}\nchanged: {result['changed']}")
    if not result["ok"]:
        entry.update(outcome="rejected_by_guard", reason="; ".join(result["problems"])[:800])
        record(entry)
        log(f"rejected by guard: {entry['reason']}")
        return entry["outcome"]
    return test_candidate(candidate, entry, train_run, val_run, args)


def test_candidate(candidate: Path, entry: dict, train_run: Path, val_run: Path, args) -> str:
    """Steps 5 to 7: training PRs, validation PRs, gate. Records the outcome, and accepts on a pass."""
    entry["candidate"] = fingerprint(candidate)
    # Tasks replayed from the cache take no time, which flatters any change to how the time budget is split. A salt of its own
    # makes the candidate run every task for real, and the champion's scores stay cached.
    salt = getattr(args, "candidate_salt", "") or args.salt
    if salt != args.salt:
        entry["candidate_salt"] = salt
    candidate_train = ensure_run(candidate, args.train, args, salt)
    entry["train"] = compare(per_pr(train_run), per_pr(candidate_train))
    log(f"training PRs: {entry['train']}")
    train_cost = cost_ratio_between(train_run, candidate_train)
    entry["train_cost_ratio"] = train_cost
    entry["train_augmented_delta"], entry["train_novel_rate_z"] = augmented_change(train_run, candidate_train)
    # This filter only saves money. On 16 PRs, two samples of the same version can differ by more than a real change, so a change backed by a
    # controlled test may go on to validation regardless. What decides is `decide`, on validation PRs, either way.
    entry["validated_anyway"] = bool(getattr(args, "validate_anyway", False))
    if not entry["validated_anyway"] and not worth_validating(entry["train"], train_cost, entry["train_augmented_delta"], entry["train_novel_rate_z"]):
        entry.update(outcome="rejected_on_train", reason=f"no gain even on the PRs it was written for (delta {entry['train']['delta']:+}, cost x{train_cost or 0:.2f})")
        record(entry)
        log(entry["reason"])
        return entry["outcome"]
    candidate_val = ensure_run(candidate, args.val, args, salt)
    entry["val"] = compare(per_pr(val_run), per_pr(candidate_val))
    costs = {"champion": run_cost(val_run), "candidate": run_cost(candidate_val)}
    entry["cost"] = costs
    cost_ratio = cost_ratio_between(val_run, candidate_val)
    guards = corpus_guards(per_pr(val_run), per_pr(candidate_val))
    entry.update(guards)
    entry["grounded_precision_delta"] = precision_delta(per_pr(val_run), per_pr(candidate_val))
    entry["augmented_precision_delta"], entry["novel_rate_z"] = augmented_change(val_run, candidate_val)
    ok, reason = decide(entry["val"], min_delta=args.min_delta, min_p=args.min_p, cost_ratio=cost_ratio, max_cost_ratio=args.max_cost_ratio,
                        mean_seconds=costs["candidate"]["mean_seconds"], max_mean_seconds=args.max_mean_seconds,
                        volume_ratio=guards.get("maintainer_volume_ratio"), reviewbench_delta=(guards.get("val_reviewbench") or {}).get("delta"),
                        grounded_precision_delta=entry["grounded_precision_delta"], augmented_precision_delta=entry["augmented_precision_delta"], novel_rate_z=entry["novel_rate_z"])
    # Every ReviewBench PR can fail to fetch or score even though the selection named some.
    scored = sorted(set(per_pr(val_run)) & set(per_pr(candidate_val)))
    problem = validation_problem(scored, [k for k in scored if k in maintainer_manifest()])
    if problem:
        ok, reason = False, problem
    entry.update(outcome="accepted" if ok else "rejected_on_val", reason=reason)
    log(f"validation PRs: {entry['val']}\n{entry['outcome']}: {reason}")
    record(entry)
    if ok:
        accept(candidate, entry, args)
        # The next round looks up the new champion's scores by the plain name.
        for selector, run in ((args.train, candidate_train), (args.val, candidate_val)):
            plain = RUNS / run_name(candidate, selector, args, args.salt)
            if plain != run and not plain.exists():
                plain.symlink_to(run.name)
    return entry["outcome"]


def preflight(args) -> None:
    from preflight import check_isolation, check_judge, check_model
    ok, detail = check_judge(args.judge_model)
    if not ok:
        raise SystemExit(f"{args.judge_model} cannot judge: {detail}")
    ok, detail = check_model(args.model, host=args.host)
    if not ok:
        raise SystemExit(f"{args.model} is not available: {detail}")
    improver = getattr(args, "improver_model", None)
    if improver and args.cmd == "iterate":
        ok, detail = check_model(improver)
        if not ok:
            raise SystemExit(f"{improver} is not available: {detail}")
    ok, detail = check_isolation(args.model, host=args.host)
    if not ok:
        raise SystemExit(f"isolation check failed: {detail}")
    log(f"preflight: {detail}")


def cmd_iterate(args) -> int:
    keys = resolve_keys(args.val)
    splits = load_splits()
    held_out = [k for name in ("val", "holdout", "maint_val", "maint_holdout") for k in splits.get(name, [])]
    problem = validation_problem(keys, [k for k in keys if k in maintainer_manifest()]) or training_problem(resolve_keys(args.train), keys, held_out)
    if problem:
        raise SystemExit(problem)
    preflight(args)
    outcomes = []
    for number in range(args.rounds):
        problem = over_budget(args.max_usage)
        if problem:
            print(f"stopping: {problem}, limit {args.max_usage}%")
            break
        outcomes.append(one_round(args))
        log(f"finished round {number + 1} of {args.rounds}: {outcomes[-1]}")
    print(json.dumps({"rounds": len(outcomes), "accepted": outcomes.count("accepted"), "outcomes": outcomes}))
    return 0


def cmd_evaluate(args) -> int:
    """For a change written outside this script. The session driving the loop plays the improver, so it reads the diagnosis and the
    teachers' traces itself and hands over a directory. Everything after the proposal is the same as in `iterate`."""
    candidate = args.candidate.resolve()
    keys = resolve_keys(args.val)
    splits = load_splits()
    held_out = [k for name in ("val", "holdout", "maint_val", "maint_holdout") for k in splits.get(name, [])]
    problem = validation_problem(keys, [k for k in keys if k in maintainer_manifest()]) or training_problem(resolve_keys(args.train), keys, held_out)
    if problem:
        raise SystemExit(problem)
    preflight(args)
    champion_id = fingerprint(SKILL)
    train_run = ensure_run(SKILL, args.train, args)
    val_run = ensure_run(SKILL, args.val, args)
    entry: dict = {"id": time.strftime("%Y%m%d-%H%M%S"), "champion": champion_id, "train_set": args.train, "val_set": args.val, "model": args.model, "judge_model": args.judge_model,
                   "improver_model": args.improver, "hypothesis": args.hypothesis, "champion_scores": {"train": scores(train_run), "val": scores(val_run)}}
    # Mechanics are allowed to change here, unlike with an automated improver. The leak check still applies.
    result = check(SKILL, candidate, training_vocabulary(args.train), allow_mechanics=True)
    entry["changed_files"] = result["changed"]
    if not result["ok"]:
        entry.update(outcome="rejected_by_guard", reason="; ".join(result["problems"])[:800])
        record(entry)
        print(json.dumps({"outcome": entry["outcome"], "reason": entry["reason"]}))
        return 1
    outcome = test_candidate(candidate, entry, train_run, val_run, args)
    print(json.dumps({key: entry.get(key) for key in ("outcome", "reason", "train", "train_augmented_delta", "train_novel_rate_z", "val", "grounded_precision_delta", "augmented_precision_delta", "novel_rate_z", "cost")}, indent=1))
    return 0 if outcome == "accepted" else 2


def cmd_baseline(args) -> int:
    preflight(args)
    run_dir = ensure_run(SKILL, args.select, args)
    print(headline(json.loads((run_dir / "results.json").read_text())))
    print(json.dumps({**scores(run_dir), **run_cost(run_dir), "run": run_dir.name}))
    return 0


def cmd_holdout(args) -> int:
    preflight(args)
    done = [r for r in ledger_records() if r.get("outcome") == "holdout" and r.get("select", "holdout") == args.select]
    if done and not args.force:
        print(f"{args.select} was last used at {done[-1]['id']}. Each look at it makes it less of a holdout. Pass --force if this is a milestone.")
        return 1
    run_dir = ensure_run(SKILL, args.select, args)
    summary = scores(run_dir)
    record({"id": time.strftime("%Y%m%d-%H%M%S"), "outcome": "holdout", "select": args.select, "champion": fingerprint(SKILL), "scores": summary, "model": args.model, "judge_model": args.judge_model})
    print(headline(json.loads((run_dir / "results.json").read_text())))
    print(json.dumps(summary))
    return 0


def cmd_status(args) -> int:
    records = ledger_records()
    counts: dict[str, int] = {}
    for entry in records:
        counts[entry["outcome"]] = counts.get(entry["outcome"], 0) + 1
    print(json.dumps({"champion": fingerprint(SKILL), "experiments": counts, "usage": usage_limits()}, indent=1))
    print(history_markdown(limit=args.last))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--host", default=DEFAULT_HOST, choices=["opencode", "claude", "codex"])
        p.add_argument("--model", default=DEFAULT_REVIEW_MODEL)
        p.add_argument("--effort", help="reasoning effort for the reviewers")
        p.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL)
        p.add_argument("--classify-sample", type=int, default=4, help="unmatched findings per PR to classify, for the augmented estimate")
        # One PR in a benchmark run has its whole container. Too few workers here looks like a slow agent, and tasks get cut at the deadline.
        p.add_argument("--prs-at-once", type=int, default=2)
        p.add_argument("--concurrency", type=int, default=40)
        p.add_argument("--salt", default="")

    def gating(p):
        p.add_argument("--train", default="train_quick")
        p.add_argument("--val", default="val_quick")
        p.add_argument("--min-delta", type=float, default=0.005, help="smallest objective gain worth keeping")
        p.add_argument("--min-p", type=float, default=0.8, help="share of resamples that must show a gain")
        p.add_argument("--max-cost-ratio", type=float, default=1.4)
        p.add_argument("--max-mean-seconds", type=float, default=330, help="finals allow about 6.4 minutes per PR on average")
        p.add_argument("--commit", action="store_true", help="commit each accepted change")

    p = sub.add_parser("iterate")
    common(p)
    gating(p)
    p.add_argument("--rounds", type=int, default=3)
    p.add_argument("--improver-model", default=DEFAULT_IMPROVER_MODEL)
    p.add_argument("--improver-steps", type=int, default=40)
    p.add_argument("--improver-timeout", type=float, default=900)
    p.add_argument("--max-usage", type=float, default=80, help="stop when an OpenCode Go usage meter reaches this percent")
    p.set_defaults(func=cmd_iterate)

    p = sub.add_parser("evaluate")
    common(p)
    gating(p)
    p.add_argument("--candidate", type=Path, required=True, help="a copy of skill/pr-review with the change in it")
    p.add_argument("--hypothesis", required=True, help="what the change should do, and why")
    p.add_argument("--improver", default="claude-opus-5-5", help="who wrote the change, for the ledger")
    p.add_argument("--candidate-salt", default="", help="run the candidate fresh, with the champion's scores still cached. Use it whenever the change touches the time budget")
    p.add_argument("--validate-anyway", action="store_true", help="go on to the validation PRs whatever the training PRs show. It skips a filter that saves money, not the decision")
    p.set_defaults(func=cmd_evaluate)

    p = sub.add_parser("baseline")
    common(p)
    p.add_argument("--select", default="val_quick")
    p.set_defaults(func=cmd_baseline)

    p = sub.add_parser("holdout")
    common(p)
    p.add_argument("--select", default="holdout", choices=("holdout", "maint_holdout"))
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_holdout)

    p = sub.add_parser("status")
    p.add_argument("--last", type=int, default=10)
    p.set_defaults(func=cmd_status)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
