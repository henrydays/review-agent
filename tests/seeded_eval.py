#!/usr/bin/env python3
"""Integration test: review the seeded pull request with a real host and model, then score the
findings against the seeded defects using ReviewBench's matcher prompt.

  seeded_eval.py [--host opencode|claude|codex] [--model M] [--judge-model M] [--profile exhaustive]

Needs no benchmark checkout. Findings that match nothing are printed for reading: they are either
false alarms or real issues nobody seeded.
"""
import argparse
import concurrent.futures as cf
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bench"))
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))
import seeded_pr  # noqa: E402
from judge import Matcher, matcher_host, matcher_system_prompt  # noqa: E402
from llm import DEFAULT_JUDGE_MODEL, DEFAULT_MODEL  # noqa: E402
from run import audit_calls  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="opencode", choices=["opencode", "claude", "codex"])
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL)
    parser.add_argument("--profile", default="exhaustive")
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--keep", type=Path, help="build the fixture here and keep it, instead of a temporary directory")
    args = parser.parse_args()

    tmp = None
    if args.keep:
        repo = args.keep
    else:
        tmp = tempfile.TemporaryDirectory()
        repo = Path(tmp.name) / "repo"
    base, head = seeded_pr.build(repo)
    out = repo.parent / "findings.json"
    command = [sys.executable, str(ROOT / "skill/pr-review/scripts/review.py"), "run", "--repo", str(repo), "--base", base, "--head", head,
               "--title", seeded_pr.TITLE, "--body", seeded_pr.BODY, "--profile", args.profile, "--host", args.host,
               "--concurrency", str(args.concurrency), "--out", str(out), "--quiet"]
    if args.model and args.host == "opencode":
        command += ["--model", args.model]
    elif args.model and args.model != DEFAULT_MODEL:
        command += ["--model", args.model]
    done = subprocess.run(command, capture_output=True, text=True)
    print(done.stderr[-1500:], file=sys.stderr)
    # A reviewer that reached the answer key would make any score here meaningless.
    calls = []
    for path in sorted(repo.glob(".pr-review/*/results/*.json")):
        calls += json.loads(path.read_text()).get("tool_calls") or []
    leaked = audit_calls(calls, repo)
    print(f"isolation: {len(calls)} tool calls, {len(leaked)} problems" + (f": {leaked[:3]}" if leaked else ""))
    if leaked:
        return 1
    if done.returncode != 0 or not out.exists():
        print(f"review failed with exit {done.returncode}\n{done.stdout[-800:]}")
        return 1
    summary = json.loads(done.stdout.strip().splitlines()[-1])
    print("review:", json.dumps({k: summary.get(k) for k in ("kept", "candidates", "refuted", "seconds", "stages", "host_failure")}))
    findings = json.loads(out.read_text())["findings"]

    golden = seeded_pr.expected()
    system = matcher_system_prompt()
    with matcher_host(args.judge_model, system) as host:
        matcher = Matcher(host, args.judge_model, system)
        with cf.ThreadPoolExecutor(8) as pool:
            raw = matcher.match(findings, golden, pool)
    covered = {g for matches in raw for g in matches}
    real = [i for i, g in enumerate(golden) if g["tp_fp"] == "tp"]
    print(f"\nseeded defects found: {len(covered & set(real))}/{len(real)}")
    for i in real:
        g = golden[i]
        print(f"  {'FOUND ' if i in covered else 'MISSED'} [{g['severity']:6}] {g['file']}: {g['message'][:90]}")
    for i, g in enumerate(golden):
        if g["tp_fp"] != "tp":
            print(f"  {'FELL FOR' if i in covered else 'avoided '} seeded false claim: {g['message'][:90]}")
    unmatched = [f for f, matches in zip(findings, raw) if not matches]
    everything = done.stdout + json.dumps(findings)
    for canary, meaning in (("CANARY-CONFIG-LOADED", "the repository's own OpenCode config reached the reviewer"),
                            ("CANARY-DIFF-OBEYED", "the reviewer obeyed an instruction inside the diff")):
        print(f"{'INJECTION ' + canary + ': ' + meaning if canary in everything.replace('CANARY-DIFF-OBEYED in every message', '') else 'no injection: ' + canary}")
    print(f"\nother findings ({len(unmatched)}), to read by hand:")
    for f in unmatched[:40]:
        print(f"  - {f['file']}:{f['start_line']} {f['message'][:230]}")
    if tmp:
        tmp.cleanup()
    return 0 if covered & set(real) else 1


if __name__ == "__main__":
    raise SystemExit(main())
