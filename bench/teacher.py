#!/usr/bin/env python3
"""Run a strong model once over a PR, as a yardstick for the review agent and as something to learn from.

  teacher.py run --who opus --select train_quick --name teach-opus [--at-once 3] [--judge-model M]

Each teacher gets what one reviewer in the pipeline gets, on the whole diff: the same framing, the same output format,
the same read-only tools in a checkout of its own. It has no step limit, and it also says how it found each finding,
since that is what can be taught. Nothing here is part of the skill. The teacher's own words never go into it, only
general lessons a person draws from them, and `loop/guard.py` checks those for words tied to benchmark repositories.

Writes runs/<name>/findings/<key>.json in the ReviewBench format, and runs/<name>/traces/<key>.json with the answer,
the reasoning, and every tool call.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import CACHE, ROOT, log, resolve_keys
from dataset import PRS, fetch
from llm import DEFAULT_JUDGE_MODEL
from run import audit_calls

SKILL = ROOT / "skill" / "pr-review"
sys.path.insert(0, str(SKILL / "scripts"))
from hosts import make_host  # noqa: E402
from review import SKILL_DIR, _overview, extract_list, fill, load_config, normalize_finding, reference  # noqa: E402
from rpdiff import classify, make_partitions, parse_diff  # noqa: E402

WHO = {
    "opus": ("claude", "claude-opus-5-5", "max"),
    "astra": ("codex", "gpt-6-astra", "max"),
    "haiku1": ("claude", "claude-haiku-5-5", None),
    "haiku-medium": ("claude", "claude-haiku-5-5", "medium"),
    "haiku-high": ("claude", "claude-haiku-5-5", "high"),
    "haiku-max": ("claude", "claude-haiku-5-5", "max"),
}
TEACH_ROOT = CACHE / "teach"
HOW_FOUND = """
## Also say how you found each finding

Add one more field to every finding, `how_found`: two or three plain sentences on what first made you suspicious, what you
opened or searched for to check it, and what you compared it with. Then, after the `findings` list in the same JSON object, add
`method`: a short list of the concrete checks you actually ran on this change, in the order you ran them, each one general enough
to apply to a different repository. Report what you really did, not what a reviewer ought to do.
"""


def isolated_checkout(key: str, who: str) -> Path:
    """A clone of its own, so no other run's scratch files are in reach, and with no remote to read upstream from."""
    target = TEACH_ROOT / who / key / "repo"
    source = PRS / key / "repo"
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "--quiet", "--local", "--no-hardlinks", str(source), str(target)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(target), "remote", "remove", "origin"], check=False, capture_output=True)
    return target


def build_prompt(repo: Path, pr: dict, diff_text: str, work_rel: str) -> tuple[str, list[str]]:
    config = load_config(SKILL_DIR)
    files = parse_diff(diff_text)
    for file in files:
        classify(file)
    context = fill(reference("pr-context.md"), title=pr.get("title") or "(no title)", body=(pr.get("body") or "(no description)")[:6000], overview=_overview(files))
    part = make_partitions(files, 10**9, 1)[0]
    prompt = fill(reference("discovery.md"), pr_context=context, rules="", lens_title=config["lenses"]["general"]["title"], lens_body=reference("lenses/general.md"),
                  part_label="the whole pull request", diff=f"<diff>\n{part.rendered()}\n</diff>", context="(none)", hints="(none)", work=work_rel,
                  max_steps=60, output=reference("output-findings.md") + HOW_FOUND)
    return prompt, [f.path for f in files if not f.skip_reason]


def save_findings(out: Path, key: str, who: str, pr: dict, raw: list[dict], repo: Path, changed: list[str]) -> list[dict]:
    lengths: dict = {}
    findings = []
    for item in raw:
        normal = normalize_finding(item, repo, changed, lengths)
        if normal:
            findings.append({"file": normal["file"], "start_line": normal["start_line"], "end_line": normal["end_line"], "message": normal["message"], "producer": f"teacher:{who}"})
    (out / "findings").mkdir(parents=True, exist_ok=True)
    (out / "findings" / f"{key}.json").write_text(json.dumps({"pr": {"repo": f"https://github.com/{pr['nwo']}", "pr_number": pr["pr_number"], "base": pr["base"], "head": pr["head"]},
                                                              "agent": f"teacher-{who}", "findings": findings}, indent=1))
    return findings


def review(key: str, who: str, out: Path) -> dict:
    host_name, model, effort = WHO[who]
    pr_dir = PRS / key
    pr = json.loads((pr_dir / "pr" / "pr.json").read_text())
    trace_path = out / "traces" / f"{key}.json"
    if trace_path.exists():
        saved = json.loads(trace_path.read_text())
        if saved.get("ok"):
            return saved["record"]
        # These sessions are slow and costly. One refused by an audit rule since corrected is still a good answer.
        if not saved["record"].get("error") and saved.get("raw") and not audit_calls(saved.get("tool_calls") or [], pr_dir / "repo"):
            changed = [f.path for f in parse_diff((pr_dir / "pr" / "diff.patch").read_text(errors="replace"))]
            findings = save_findings(out, key, who, pr, saved["raw"], pr_dir / "repo", changed)
            saved.update(ok=True, record={**saved["record"], "findings": len(findings), "audit": []})
            trace_path.write_text(json.dumps(saved, indent=1))
            log(f"  recovered {key} from its saved trace")
            return saved["record"]
    repo = isolated_checkout(key, who)
    head, base = pr["head"], pr["base"]
    done = subprocess.run(["git", "-C", str(repo), "merge-base", base, head], capture_output=True, text=True)
    merge_base = done.stdout.strip() or base
    diff_text = (pr_dir / "pr" / "diff.patch").read_text(errors="replace")
    work_rel = ".pr-review/teach"
    for file in parse_diff(diff_text):
        if file.status == "added":
            continue
        shown = subprocess.run(["git", "-C", str(repo), "show", f"{merge_base}:{file.old_path}"], capture_output=True)
        if shown.returncode == 0 and len(shown.stdout) < 1_000_000:
            target = repo / work_rel / "base" / file.path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(shown.stdout)
    exclude = repo / ".git" / "info" / "exclude"
    exclude.write_text((exclude.read_text() if exclude.exists() else "") + "\n.pr-review/\n")
    prompt, changed = build_prompt(repo, pr, diff_text, work_rel)
    started = time.time()
    with make_host(host_name, repo, model, max_steps=60, effort=effort) as host:
        reply = host.ask(prompt, timeout=3000, attempts=2)
    seconds = round(time.time() - started, 1)
    raw = extract_list(reply.text, "findings") or []
    problems = audit_calls(reply.tool_calls, repo)
    ok = not reply.error and not problems and bool(raw or reply.text)
    findings = save_findings(out, key, who, pr, raw, repo, changed) if ok else []
    record = {"key": key, "who": who, "findings": len(findings), "seconds": seconds, "cost": reply.usage.cost, "input": reply.usage.input, "output": reply.usage.output,
              "cache_read": reply.usage.cache_read, "tools": reply.tools, "error": reply.error, "audit": problems}
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    trace_path.write_text(json.dumps({"ok": ok, "record": record, "raw": raw, "text": reply.text, "thinking": reply.thinking, "tool_calls": reply.tool_calls}, indent=1))
    shutil.rmtree(TEACH_ROOT / who / key, ignore_errors=True)
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("cmd", choices=["run"])
    parser.add_argument("--who", required=True, choices=sorted(WHO))
    parser.add_argument("--select", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--at-once", type=int, default=3)
    parser.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL)
    parser.add_argument("--classify", action="store_true")
    parser.add_argument("--classify-sample", type=int, default=0)
    parser.add_argument("--no-judge", action="store_true")
    args = parser.parse_args()
    keys = resolve_keys(args.select)
    ready, errors = fetch(keys)
    if errors:
        log(f"could not fetch: {errors}")
    out = ROOT / "runs" / args.name
    out.mkdir(parents=True, exist_ok=True)
    records = []

    def work(key: str) -> None:
        record = review(key, args.who, out)
        records.append(record)
        log(f"  [{len(records)}/{len(ready)}] {key}: {record['findings']} findings, {record['seconds']}s, ${record.get('cost', 0):.2f}, tools {record['tools']}"
            + (f" ERROR {record['error']}" if record.get("error") else "") + (f" AUDIT {record['audit'][:2]}" if record.get("audit") else ""))

    with cf.ThreadPoolExecutor(args.at_once) as pool:
        list(pool.map(work, ready))
    (out / "run.json").write_text(json.dumps({"name": args.name, "who": WHO[args.who], "keys": ready, "records": records}, indent=1))
    print(f"{args.who}: {len(records)} PRs, ${sum(r.get('cost', 0) for r in records):.2f}, mean {sum(r['seconds'] for r in records) / max(1, len(records)):.0f}s")
    bad = [r for r in records if r.get("audit")]
    if bad:
        (out / "AUDIT_FAILED").write_text(json.dumps(bad, indent=1))
        raise SystemExit(f"audit failed on {len(bad)} PRs, see {out / 'AUDIT_FAILED'}")
    if args.no_judge or not (out / "findings").exists():
        return 0
    from judge import headline, judge_candidates, load_candidates
    judged = judge_candidates(load_candidates(out / "findings"), model=args.judge_model, classify=args.classify, classify_sample=args.classify_sample)
    (out / "results.json").write_text(json.dumps(judged["summary"], indent=1))
    (out / "results.details.json").write_text(json.dumps(judged["details"], indent=1))
    print(f"judge: {args.judge_model}")
    print(headline(judged["summary"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
