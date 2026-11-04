#!/usr/bin/env python3
"""Run a version of the skill over benchmark PRs, then judge the result.

  run.py --select val_quick --name baseline [--model M] [--judge-model M] [--skill-dir DIR]
         [--prs-at-once 4] [--concurrency 12] [--round 1] [--no-judge] [--classify]

Writes runs/<name>/:
  findings/<pr key>.json     what the agent reports, in the ReviewBench format
  artifacts/<pr key>/        every intermediate: raw candidates, merged candidates, verdicts, stats
  results.json               scores
  results.details.json       per-finding matches

The checkout an agent runs in has no remote and no web tools, so it cannot read the upstream PR.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import CACHE, ROOT, load_manifest, log, resolve_keys
from dataset import PRS, fetch
from llm import DEFAULT_JUDGE_MODEL, DEFAULT_MODEL, over_budget, over_claude_budget, usage_limits

RUNS = ROOT / "runs"
TASK_CACHE = CACHE / "task-cache"
# Each run reviews its own clones, so runs can overlap, and the checkouts the classifier reads never hold a reviewer's scratch files.
CHECKOUTS = CACHE / "run-checkouts"
CLAUDE_CHECK_SECONDS = 600
ARTIFACTS = ("meta.json", "plan.json", "raw_candidates.json", "candidates.json", "final.json", "stats.json", "review.md")


def private_checkout(out: Path, key: str, head: str) -> Path:
    """Shares the object store, so it costs a working tree and no history."""
    target = CHECKOUTS / out.name / key / "repo"
    shutil.rmtree(target.parent, ignore_errors=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "--quiet", "--shared", "--no-checkout", str(PRS / key / "repo"), str(target)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(target), "remote", "remove", "origin"], check=False, capture_output=True)
    subprocess.run(["git", "-C", str(target), "checkout", "--quiet", "--detach", head], check=True, capture_output=True)
    return target


def review_one(key: str, out: Path, *, skill_dir: Path, model: str, concurrency: int, salt: str, deadline: float | None, use_cache: bool,
               host: str = "opencode", effort: str | None = None) -> dict:
    pr_dir = PRS / key
    pr = json.loads((pr_dir / "pr" / "pr.json").read_text())
    (out / "findings").mkdir(parents=True, exist_ok=True)
    findings_path = out / "findings" / f"{key}.json"
    repo = private_checkout(out, key, pr["head"])
    command = [sys.executable, str(skill_dir / "scripts" / "review.py"), "run", "--repo", str(repo),
               "--base", pr["base"], "--head", pr["head"], "--title", pr.get("title") or "", "--body-file", "-",
               "--diff-file", str((pr_dir / "pr" / "diff.patch").resolve()), "--profile", "exhaustive",
               "--nwo", pr["nwo"], "--pr-number", str(pr["pr_number"]), "--host", host, "--model", model,
               "--concurrency", str(concurrency), "--salt", salt, "--agent", "pr-review", "--out", str(findings_path), "--quiet"]
    body_file = out / "bodies" / f"{key}.txt"
    body_file.parent.mkdir(parents=True, exist_ok=True)
    body_file.write_text(pr.get("body") or "")
    command[command.index("--body-file") + 1] = str(body_file)
    if effort:
        command += ["--effort", effort]
    if deadline:
        command += ["--deadline", str(deadline)]
    if use_cache:
        command += ["--cache-dir", str(TASK_CACHE)]
    started = time.time()
    done = subprocess.run(command, capture_output=True, text=True)
    seconds = time.time() - started
    record = {"key": key, "exit": done.returncode, "seconds": round(seconds, 1)}
    try:
        record.update(json.loads(done.stdout.strip().splitlines()[-1]))
    except (IndexError, json.JSONDecodeError):
        record["stdout_tail"] = done.stdout[-400:]
    if done.returncode != 0 or not findings_path.exists():
        record["stderr_tail"] = done.stderr[-800:]
    work = repo / ".pr-review" / pr["head"][:10]
    target = out / "artifacts" / key
    target.mkdir(parents=True, exist_ok=True)
    for name in ARTIFACTS:
        if (work / name).exists():
            shutil.copy(work / name, target / name)
    if (work / "results").exists():
        results = target / "results"
        if results.exists():
            shutil.rmtree(results)
        shutil.copytree(work / "results", results)
    shutil.rmtree(repo.parent, ignore_errors=True)
    return record


FORBIDDEN_TOOLS = {"webfetch", "websearch", "fetch", "execute", "bash", "shell", "edit", "write", "notebookedit", "task", "agent", "web_search", "mcp_tool_call"}
# Codex's built-in server listing its resources, and finding none. Any other MCP call stays a violation.
EMPTY_MCP_LISTING = re.compile(r"""'server': 'codex', 'tool': 'list_mcp_resource(s|_templates)'.*\{"resource(s|Templates)":\[\]\}""")
OUTSIDE_COMMAND = re.compile(r"ReviewBench|/golden\b|review-agent/(?!\.cache/prs/)|\.\./\.\./\.\./|(?:^|[;&|(]\s*|-l?c\s+['\"]?)(curl|wget|gh)\s")


def _slug(path: Path | str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def _same_pr(path: str, key: str | None) -> bool:
    """A cached answer logged the path it ran in, which may be this PR's shared checkout or another run's clone of it. It is the same PR at the
    same commit, so reading there is reading the checkout. Any other PR's is not."""
    if not key:
        return False
    shared = str(PRS / key / "repo")
    return path == shared or path.startswith(shared + "/") or re.match(rf"{re.escape(str(CHECKOUTS))}/[^/]+/{re.escape(key)}/repo(/|$)", path) is not None


def _own_overflow(path: str, root: Path, key: str | None = None) -> bool:
    """Claude Code saves a tool result too large for the conversation, then has the model read it back. That file holds this session's
    own output, in a folder named after the checkout, so only that checkout's folder and only that kind of file qualify."""
    slugs = [re.escape(_slug(root))]
    if key:
        slugs += [re.escape(_slug(PRS / key / "repo")), f"{re.escape(_slug(CHECKOUTS))}-[^/]+-{re.escape(_slug(key))}-repo"]
    return re.fullmatch(rf".*/\.claude/projects/(?:{'|'.join(slugs)})/[0-9a-f-]+/tool-results/toolu_\w+\.txt", path) is not None


def audit_calls(calls: list[dict], checkout: Path, *, key: str | None = None) -> list[str]:
    """Violations in one task's tool calls: something a reviewer actually did that it should not be able
    to. A refused call is not a violation. Models do try tools they do not have, and OpenCode refuses them, which
    is the isolation working. Only `completed` counts. A folder named `golden` inside the reviewer's own
    checkout is fine, so the test is where a path points, not what it contains."""
    problems: list[str] = []
    root = checkout.resolve()
    for call in calls:
        if call.get("status") != "completed":
            continue
        tool = str(call.get("tool") or "").lower()
        if tool == "mcp_tool_call" and EMPTY_MCP_LISTING.search(str(call.get("input"))):
            continue
        if tool in FORBIDDEN_TOOLS:
            problems.append(f"ran {call['tool']}: {str(call.get('input'))[:120]}")
            continue
        # Codex reads through shell commands. Its sandbox blocks these, and this catches a sandbox that did not.
        if tool == "command_execution" and OUTSIDE_COMMAND.search(str(call.get("input"))):
            problems.append(f"command aimed outside its checkout: {str(call.get('input'))[:160]}")
            continue
        path = call.get("path")
        if isinstance(path, str) and path:
            target = Path(path) if Path(path).is_absolute() else root / path
            normal = Path(os.path.normpath(target))
            if normal != root and root not in normal.parents and not _same_pr(str(normal), key) and not _own_overflow(str(normal), root, key):
                problems.append(f"read outside its checkout: {path[:160]}")
    return problems


def refused_calls(out: Path) -> dict[str, int]:
    """Attempts OpenCode blocked, by tool. Worth seeing, since a burst of them means wasted steps."""
    counts: dict[str, int] = {}
    for path in (out / "artifacts").glob("*/results/*.json"):
        try:
            calls = json.loads(path.read_text()).get("tool_calls") or []
        except json.JSONDecodeError:
            continue
        for call in calls:
            if call.get("status") == "error":
                counts[call.get("tool", "?")] = counts.get(call.get("tool", "?"), 0) + 1
    return counts


def audit_run(out: Path) -> list[str]:
    """What reviewers actually did, from the tool calls saved with each task. Benchmark docs warn that
    reviewers will look answers up when they can, so check the transcripts instead of assuming."""
    problems: list[str] = []
    for path in sorted((out / "artifacts").glob("*/results/*.json")):
        try:
            calls = json.loads(path.read_text()).get("tool_calls") or []
        except json.JSONDecodeError:
            continue
        key = path.parent.parent.name
        problems += [f"{key}/{path.stem}: {problem}" for problem in audit_calls(calls, CHECKOUTS / out.name / key / "repo", key=key)]
    return problems


def spend(records_dir: Path) -> dict:
    """Tokens from each task's saved numbers. Tasks killed at a deadline report nothing, so the real figure is
    higher. Compare with the usage meter, which `run` records before and after."""
    totals = {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0}
    cost = 0.0
    for path in (records_dir / "artifacts").glob("*/stats.json"):
        stats = json.loads(path.read_text())
        for field in totals:
            totals[field] += int(stats.get(field) or 0)
        cost += float(stats.get("cost") or 0)
    return {**totals, "cost": round(cost, 4)}


def save_record(out: Path, record: dict) -> None:
    """Written as each PR ends, so what a crash loses is the PRs in flight and not the timings of the ones before them."""
    path = out / "records" / f"{record['key']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(record, indent=1))
    temp.replace(path)


def check_resume(out: Path, resume: bool, settings: dict) -> dict[str, dict]:
    """The PRs a resumed run keeps. Reviewing them again would not reproduce them: answers replay from the cache, but tasks that were killed at a
    deadline never reached it, so they would run again with a new deadline, and the PR would score what it did not do in its measured time."""
    path = out / "run-settings.json"
    if not resume:
        path.write_text(json.dumps(settings, indent=1))
        shutil.rmtree(out / "records", ignore_errors=True)
        return {}
    if not path.exists():
        raise SystemExit(f"nothing to resume in {out}")
    before = json.loads(path.read_text())
    if before != settings:
        raise SystemExit(f"cannot resume {out.name} with different settings: was {before}, now {settings}")
    saved: dict[str, dict] = {}
    for file in sorted((out / "records").glob("*.json")):
        record = json.loads(file.read_text())
        if not record.get("exit") and not record.get("host_failure") and (out / "findings" / f"{record['key']}.json").exists():
            saved[record["key"]] = record
    log(f"resuming {out.name}: keeping {len(saved)} finished PRs")
    return saved


def run(keys: list[str], name: str, *, skill_dir: Path, model: str, prs_at_once: int, concurrency: int, salt: str = "",
        deadline: float | None = None, use_cache: bool = True, max_usage: float = 90, host: str = "opencode", effort: str | None = None, resume: bool = False) -> tuple[Path, list[dict]]:
    out = RUNS / name
    out.mkdir(parents=True, exist_ok=True)
    (out / "AUDIT_FAILED").unlink(missing_ok=True)
    ready, errors = fetch(keys)
    if errors:
        log(f"could not fetch: {errors}")
    records: list[dict] = []
    started = time.time()
    saved = check_resume(out, resume, {"skill_dir": str(skill_dir), "model": model, "salt": salt, "host": host, "effort": effort})
    # The meters are OpenCode Go's, so they say nothing about a run on another host.
    meters = host == "opencode"
    meter_before = usage_limits() if meters else None
    stopped: list[str] = []
    meter = {"problem": None}
    finished = threading.Event()

    def watch_claude() -> None:
        # Past the limit every session fails, including whoever is driving the run. A reading can take minutes on a busy machine, so no PR waits for it.
        while True:
            meter["problem"] = over_claude_budget(max_usage) or meter["problem"]
            if finished.wait(CLAUDE_CHECK_SECONDS):
                return

    if host == "claude":
        threading.Thread(target=watch_claude, daemon=True).start()

    def work(key: str) -> dict | None:
        if key in saved:
            records.append(saved[key])
            return saved[key]
        # One run has moved the 5-hour meter by more than half, so check before each PR, not just once.
        problem = over_budget(max_usage) if meters else meter["problem"]
        if problem:
            stopped.append(key)
            log(f"  not starting {key}: {problem}, limit {max_usage}%")
            return None
        try:
            record = review_one(key, out, skill_dir=skill_dir, model=model, concurrency=concurrency, salt=salt, deadline=deadline, use_cache=use_cache, host=host, effort=effort)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            # A bad checkout loses this PR, not the hours of work around it.
            record = {"key": key, "exit": 1, "seconds": 0, "stderr_tail": repr(error)[-800:]}
        log(f"  [{len(records) + 1}/{len(ready)}] {key}: {record.get('kept', '?')} findings from {record.get('candidates', '?')} candidates "
            f"in {record['seconds']}s" + (f" EXIT {record['exit']}" if record["exit"] else "") + (f" HOST FAILURE {record.get('host_failure')}" if record.get("host_failure") else ""))
        records.append(record)
        save_record(out, record)
        return record

    with cf.ThreadPoolExecutor(prs_at_once) as pool:
        list(pool.map(work, ready))
    finished.set()
    audit = audit_run(out)
    (out / "run.json").write_text(json.dumps({"name": name, "host": host, "model": model, "effort": effort, "skill_dir": str(skill_dir), "salt": salt, "keys": ready,
                                              "seconds": round(time.time() - started, 1), "audit_problems": audit, "refused_calls": refused_calls(out),
                                              "tokens": spend(out), "meter_before": meter_before, "meter_after": usage_limits() if meters else None, "not_started": stopped,
                                              "records": records}, indent=1))
    if audit:
        # Scores from a run that could see the answers mean nothing, so nothing downstream may use them.
        (out / "AUDIT_FAILED").write_text("\n".join(audit) + "\n")
        raise SystemExit(f"audit failed, {len(audit)} problems, see {out / 'AUDIT_FAILED'}. First: {audit[0]}")
    return out, records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--select", required=True, help="split name, comma-separated splits, or PR key prefixes")
    parser.add_argument("--name", required=True)
    parser.add_argument("--host", default="opencode", choices=["opencode", "claude", "codex"])
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--effort", help="reasoning effort, passed to the host")
    parser.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL)
    parser.add_argument("--skill-dir", type=Path, default=ROOT / "skill" / "pr-review")
    parser.add_argument("--prs-at-once", type=int, default=4)
    parser.add_argument("--concurrency", type=int, default=12)
    parser.add_argument("--salt", default="", help="a different salt gets fresh model samples despite the cache")
    parser.add_argument("--deadline", type=float)
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--resume", action="store_true", help="keep the PRs this run name has finished, and review only the rest. Refuses different settings")
    parser.add_argument("--max-usage", type=float, default=90, help="stop starting PRs when the host's usage meter, OpenCode Go or Claude, reaches this percent")
    parser.add_argument("--no-judge", action="store_true")
    parser.add_argument("--classify", action="store_true")
    parser.add_argument("--classify-sample", type=int, default=0, help="classify this many unmatched findings per PR, and estimate augmented metrics from them")
    args = parser.parse_args()
    keys = resolve_keys(args.select)
    out, records = run(keys, args.name, skill_dir=args.skill_dir.resolve(), model=args.model, prs_at_once=args.prs_at_once,
                       concurrency=args.concurrency, salt=args.salt, deadline=args.deadline, use_cache=not args.no_cache, max_usage=args.max_usage, host=args.host, effort=args.effort, resume=args.resume)
    report = json.loads((out / "run.json").read_text())
    print(f"tokens: {report['tokens']}")
    print(f"usage meter: {report['meter_before']} to {report['meter_after']}".replace("'limited': False, ", "")[:400])
    if report["not_started"]:
        print(f"NOT STARTED, usage limit reached: {len(report['not_started'])} PRs. Scores below cover only what ran.")
    if report["refused_calls"]:
        print(f"refused tool attempts: {report['refused_calls']}")
    failures = [r for r in records if r["exit"] or r.get("host_failure")]
    seconds = [r["seconds"] for r in records] or [0]
    print(f"{len(records)} PRs, {len(failures)} with failures, mean {sum(seconds) / len(seconds):.0f}s, max {max(seconds):.0f}s per PR")
    if failures:
        print(json.dumps([{k: r.get(k) for k in ("key", "exit", "host_failure", "stderr_tail")} for r in failures[:4]], indent=1))
    if args.no_judge:
        return 0
    from judge import headline, judge_candidates, load_candidates
    judged = judge_candidates(load_candidates(out / "findings"), model=args.judge_model, classify=args.classify, classify_sample=args.classify_sample)
    (out / "results.json").write_text(json.dumps(judged["summary"], indent=1))
    (out / "results.details.json").write_text(json.dumps(judged["details"], indent=1))
    print(headline(judged["summary"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
