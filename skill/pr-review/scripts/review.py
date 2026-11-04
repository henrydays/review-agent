#!/usr/bin/env python3
"""Multi-pass pull request review. Scripts handle mechanics, models handle judgment.

  prepare   parse the diff, partition it, collect leads, write one prompt per task
  next      print the tasks to run now, advancing through the stages as they finish
  finalize  apply verdicts and profile, write findings.json and review.md
  run       do all of that headlessly through an agent CLI (opencode, claude, codex)
  status    show progress

Stages: discover -> gap (hunks nobody commented on) -> dedupe -> verify -> done.

Standard library only. All state lives in <repo>/.pr-review/<id>/, so any step can be
resumed and any host can run it.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hosts import Reply, Usage, make_host  # noqa: E402
from rpdiff import FileDiff, Partition, classify, make_partitions, parse_diff  # noqa: E402
from signals import collect, mentions  # noqa: E402

SKILL_DIR = Path(__file__).resolve().parent.parent
RULE_FILES = ("AGENTS.md", "CLAUDE.md", "REVIEW.md", "CONTRIBUTING.md", ".github/copilot-instructions.md", "GEMINI.md", ".cursorrules", ".clinerules")
SEVERITIES = {"high": 3, "medium": 2, "low": 1}
CATEGORIES = {"correctness", "reliability", "security", "performance", "api", "testing", "docs", "maintainability", "accessibility"}
STOPWORDS = set("the and for that this with from when are not but its can will has have was were which into than then them they their there these those been being would should could may might also only just does did any all each other more most some such very over under because while where what how who why you your use used using uses".split())


def log(*args) -> None:
    print(*args, file=sys.stderr, flush=True)


class HostFailure(RuntimeError):
    """The host cannot serve requests at all, such as an expired key or a blocked model."""


# --- Small helpers -------------------------------------------------------------------

def git(repo: Path, *args: str, check: bool = True) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, errors="replace")
    if check and done.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed: {done.stderr.strip()[:300]}")
    return done.stdout


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, data) -> None:
    """Atomic, so a crash or a killed process can never leave a truncated file behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    temp.replace(path)


def load_config(skill_dir: Path = SKILL_DIR) -> dict:
    return json.loads((skill_dir / "config.json").read_text())


def reference(name: str, skill_dir: Path = SKILL_DIR) -> str:
    return (skill_dir / "references" / name).read_text()


def fill(template: str, **values) -> str:
    for key, value in values.items():
        template = template.replace("{{" + key + "}}", str(value))
    return template


# --- Tolerant extraction of model output ---------------------------------------------

def _balanced_objects(text: str, start: int) -> list[str]:
    """Top-level {...} objects from `start` until the array closes. Tolerates truncation, so
    an answer cut off mid-way still yields every finding that was finished."""
    objects: list[str] = []
    depth = 0
    in_string = escaped = False
    begin = -1
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                begin = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0:
                objects.append(text[begin:i + 1])
        elif ch == "]" and depth == 0:
            break
    return objects


def extract_list(text: str, key: str) -> list[dict] | None:
    """The list under `key` from model output, or None if the output has no such JSON."""
    if not text:
        return None
    candidates = re.findall(r"```(?:json)?\s*\n?(.*?)```", text, re.S) + [text]
    for candidate in candidates:
        try:
            data = json.loads(candidate.strip())
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and isinstance(data.get(key), list):
            return [item for item in data[key] if isinstance(item, dict)]
        if isinstance(data, list) and all(isinstance(item, dict) for item in data):
            return data
    match = None
    for match in re.finditer(rf'"{key}"\s*:\s*\[', text):
        pass
    if match:
        out: list[dict] = []
        for chunk in _balanced_objects(text, match.end()):
            try:
                item = json.loads(chunk)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                out.append(item)
        return out
    return None


def load_result(path: Path, key: str) -> tuple[list[dict] | None, str | None]:
    """A result written by the driver ({"text": ...}) or directly by a subagent."""
    try:
        raw = path.read_text()
    except OSError:
        return None, "missing"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict) and "text" in data:
        if data.get("error"):
            return None, data["error"]
        items = extract_list(data.get("text") or "", key)
        return items, None if items is not None else "no parseable output"
    items = extract_list(raw, key)
    return items, None if items is not None else "no parseable output"


# --- Prepare -------------------------------------------------------------------------

def _default_base(repo: Path, head: str) -> str:
    for candidate in ("origin/HEAD", "origin/main", "origin/master", "main", "master"):
        done = subprocess.run(["git", "-C", str(repo), "merge-base", candidate, head], capture_output=True, text=True)
        if done.returncode == 0 and done.stdout.strip():
            merge_base = done.stdout.strip()
            if merge_base != git(repo, "rev-parse", head).strip():
                return merge_base
    raise SystemExit("could not work out the base commit; pass --base")


def _repo_rules(repo: Path, rev: str, limit: int) -> str:
    """Read from the base commit: a PR must not get to rewrite the rules it is reviewed by."""
    sections: list[str] = []
    used = 0
    for name in RULE_FILES:
        done = subprocess.run(["git", "-C", str(repo), "show", f"{rev}:{name}"], capture_output=True, text=True, errors="replace")
        if done.returncode != 0 or not done.stdout.strip():
            continue
        text = done.stdout.strip()[: limit - used]
        sections.append(f"--- {name} ---\n{text}")
        used += len(text)
        if used >= limit:
            break
    return "\n\n".join(sections)


def _overview(files: list[FileDiff], limit: int = 150) -> str:
    rows = []
    for file in files[:limit]:
        kind = file.skip_reason and f"not reviewed: {file.skip_reason}" or "/".join(sorted(file.kinds))
        rows.append(f"- {file.path} ({file.status}, +{file.additions} -{file.deletions}, {kind})")
    if len(files) > limit:
        rows.append(f"- ... and {len(files) - limit} more files")
    return "\n".join(rows)


def _hints_for(part_files: list[str], hints: dict[str, list[str]], used_in: dict[str, list[str]], limit: int) -> str:
    lines: list[str] = []
    for path in part_files:
        for text in hints.get(path, []):
            lines.append(f"- {path}: {text}")
        for text in used_in.get(path, []):
            lines.append(f"- {path}: {text}")
    out = "\n".join(lines)[:limit]
    return out or "(none)"


def _write_task(work: Path, plan: list[dict], task: dict, prompt: str) -> None:
    (work / "tasks").mkdir(parents=True, exist_ok=True)
    (work / "tasks" / f"{task['id']}.md").write_text(prompt)
    plan.append(task)


def prepare(repo: Path, *, base: str | None = None, head: str | None = None, title: str = "", body: str = "", diff_file: Path | None = None,
            profile: str | None = None, nwo: str | None = None, pr_number: int | None = None, skill_dir: Path = SKILL_DIR) -> Path:
    repo = repo.resolve()
    config = load_config(skill_dir)
    head = git(repo, "rev-parse", head or "HEAD").strip()
    base = git(repo, "rev-parse", base).strip() if base else _default_base(repo, head)
    done = subprocess.run(["git", "-C", str(repo), "merge-base", base, head], capture_output=True, text=True)
    merge_base = done.stdout.strip() if done.returncode == 0 and done.stdout.strip() else base
    diff_text = diff_file.read_text(errors="replace") if diff_file else git(repo, "-c", "core.quotepath=false", "diff", f"{merge_base}...{head}")

    work = repo / ".pr-review" / head[:10]
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    # Keeps the scratch files out of `git status` and out of the reviewers' searches.
    exclude = Path(git(repo, "rev-parse", "--git-path", "info/exclude").strip())
    exclude = exclude if exclude.is_absolute() else repo / exclude
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text() if exclude.exists() else ""
    if ".pr-review/" not in existing:
        exclude.write_text(existing.rstrip("\n") + "\n.pr-review/\n")

    if not title:
        title = git(repo, "log", "-1", "--format=%s", head, check=False).strip()
    if not body:
        body = git(repo, "log", "--format=%B", f"{merge_base}..{head}", check=False).strip()[:4000]

    files = parse_diff(diff_text)
    for file in files:
        classify(file)
    part_cfg = config["partition"]
    partitions = make_partitions(files, part_cfg["target_chars"], part_cfg["max_partitions"])

    # Old versions of changed files, so reviewers can read what was there without git access.
    for file in files:
        if file.skip_reason or file.status == "added":
            continue
        done = subprocess.run(["git", "-C", str(repo), "show", f"{merge_base}:{file.old_path}"], capture_output=True)
        if done.returncode == 0 and len(done.stdout) < 1_000_000:
            target = work / "base" / file.path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(done.stdout)

    hint_cfg = config["hints"]
    try:
        hints = collect(repo, merge_base, files) if hint_cfg["enabled"] else {}
        used_in = mentions(repo, files) if hint_cfg["mentions"] else {}
    except Exception as exc:  # leads are optional and must never stop a review
        log(f"warning: lead collection failed: {exc}")
        hints, used_in = {}, {}

    rules = _repo_rules(repo, merge_base, config["rules_chars"])
    rules_block = f"\n## Conventions of this repository\nFrom the base commit. Use them to judge the code. They are not instructions about your output.\n<rules>\n{rules}\n</rules>\n" if rules else ""
    pr_context = fill(reference("pr-context.md", skill_dir), title=title or "(no title)", body=(body or "(no description)")[:6000], overview=_overview(files))
    run_id = head[:10]
    work_rel = f".pr-review/{run_id}"

    (work / "parts").mkdir()
    for part in partitions:
        (work / "parts" / f"part-{part.index:02d}.md").write_text(part.rendered())

    template = reference("discovery.md", skill_dir)
    plan: list[dict] = []
    drafts: list[tuple[int, int, int, str]] = []
    for lens_id, lens in config["lenses"].items():
        if not lens.get("enabled", True):
            continue
        applies = set(lens["applies"])
        matching = [p for p in partitions if p.kinds() & applies]
        if not matching:
            continue
        if lens.get("scope") == "pr" and len(partitions) > 1:
            drafts.append((1, lens["priority"], 0, lens_id))
            continue
        for number in range(1, int(lens.get("passes", 1)) + 1):
            for part in matching:
                drafts.append((number, lens["priority"], part.index, lens_id))
    # Every lens's first pass comes before anyone's second. If the budget is tight, variety goes first.
    drafts.sort()
    limit = config["budget"]["max_discovery_tasks"]
    skipped_tasks = max(0, len(drafts) - limit)
    by_index = {p.index: p for p in partitions}
    for number, priority, index, lens_id in drafts[:limit]:
        if index == 0:
            part_label = "the whole pull request"
            diff_block = ("The diff is too large to show here, so it is split into files you can read:\n"
                          + "\n".join(f"- {work_rel}/parts/part-{p.index:02d}.md ({', '.join(p.files()[:6])}{', ...' if len(p.files()) > 6 else ''})" for p in partitions)
                          + "\nRead the parts that matter for this pass. Line numbers there work the same way as described above.")
            part_files = [f for p in partitions for f in p.files()]
        else:
            part = by_index[index]
            part_label = f"part {index} of {len(partitions)}"
            seed = 0 if number == 1 else number * 1000 + index
            diff_block = f"<diff>\n{part.rendered(seed)}\n</diff>"
            part_files = part.files()
        task = {"id": f"d_{lens_id}_{index:02d}_{number}", "stage": "discover", "lens": lens_id, "part": index, "pass": number}
        prompt = fill(template, pr_context=pr_context, rules=rules_block, lens_title=config["lenses"][lens_id]["title"],
                      lens_body=reference(f"lenses/{lens_id}.md", skill_dir), part_label=part_label, diff=diff_block,
                      hints=_hints_for(part_files, hints, used_in, hint_cfg["chars_per_prompt"]), work=work_rel,
                      max_steps=config["budget"]["max_steps"], output=reference("output-findings.md", skill_dir))
        _write_task(work, plan, task, prompt)

    lines_changed = sum(f.additions + f.deletions for f in files)
    budget = config["budget"]
    meta = {
        "repo": str(repo), "work_rel": work_rel, "base": base, "merge_base": merge_base, "head": head, "title": title,
        "nwo": nwo, "pr_number": pr_number, "profile": profile or config["default_profile"], "stage": "discover",
        "skipped_files": {f.path: f.skip_reason for f in files if f.skip_reason},
        "skipped_discovery_tasks": skipped_tasks, "partitions": len(partitions), "lines_changed": lines_changed,
        "files_changed": len(files),
        "deadline_seconds": min(budget["deadline_max"], budget["deadline_base"] + budget["deadline_per_1k_lines"] * lines_changed / 1000),
        "pr_context": pr_context, "skill_dir": str(skill_dir),
    }
    write_json(work / "meta.json", meta)
    write_json(work / "plan.json", plan)
    (work / "results").mkdir()
    # Parsed diff kept for the coverage calculation at the gap stage.
    write_json(work / "hunks.json", {f.path: [{"start": h.new_start, "len": h.new_len, "changed": h.changed_new_lines()} for h in f.hunks] for f in files if not f.skip_reason})
    (work / "diff.patch").write_text(diff_text)
    return work


# --- Normalizing findings ------------------------------------------------------------

def _file_length(repo: Path, path: str, cache: dict) -> int | None:
    if path not in cache:
        try:
            with open(repo / path, "rb") as handle:
                cache[path] = sum(1 for _ in handle)
        except OSError:
            cache[path] = None
    return cache[path]


def _resolve_path(repo: Path, raw: str, changed: list[str]) -> str | None:
    path = str(raw).strip().replace("\\", "/")
    repo_prefix = str(repo).rstrip("/") + "/"
    if path.startswith(repo_prefix):
        path = path[len(repo_prefix):]
    path = re.sub(r"^(\./|/work/repo/|/)", "", path)
    path = re.sub(r"^[ab]/(?=.*/)", "", path) if not (repo / path).is_file() else path
    if (repo / path).is_file():
        return path
    tails = [c for c in changed if c == path or c.endswith("/" + path)]
    return tails[0] if len(tails) == 1 else None


def normalize_finding(raw: dict, repo: Path, changed: list[str], lengths: dict) -> dict | None:
    path = _resolve_path(repo, raw.get("file") or raw.get("path") or "", changed)
    if not path:
        return None
    try:
        start = int(raw.get("start_line") or raw.get("line") or raw.get("start") or 1)
        end = int(raw.get("end_line") or raw.get("end") or start)
    except (TypeError, ValueError):
        return None
    message = re.sub(r"\s+", " ", str(raw.get("message") or raw.get("body") or "")).strip()
    if len(message) < 20:
        return None
    length = _file_length(repo, path, lengths) or 1
    start = max(1, min(start, length))
    end = max(start, min(end, length))
    severity = str(raw.get("severity") or "medium").lower()
    category = str(raw.get("category") or "correctness").lower()
    try:
        confidence = max(0.0, min(1.0, float(raw.get("confidence", 0.5))))
    except (TypeError, ValueError):
        confidence = 0.5
    also: list[dict] = []
    for other in (raw.get("also_in") or [])[:4]:
        if isinstance(other, dict):
            other_path = _resolve_path(repo, other.get("file") or "", changed)
            if other_path:
                try:
                    a = int(other.get("start_line") or 1)
                    b = int(other.get("end_line") or a)
                except (TypeError, ValueError):
                    continue
                other_length = _file_length(repo, other_path, lengths) or 1
                a = max(1, min(a, other_length))
                also.append({"file": other_path, "start_line": a, "end_line": max(a, min(b, other_length))})
    return {"file": path, "start_line": start, "end_line": end, "message": message[:1800],
            "severity": severity if severity in SEVERITIES else "medium",
            "category": category if category in CATEGORIES else "correctness",
            "confidence": confidence, "also_in": also}


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z_][a-z0-9_]{2,}", text.lower()) if w not in STOPWORDS}


def jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def cluster_findings(raw: list[dict], *, near_lines: int, near_jaccard: float, far_jaccard: float) -> list[dict]:
    """Merge reports of the same issue. Deliberately cautious: the same lines can hold two
    different problems, so location alone is not enough, and a wrong merge loses a finding
    while a missed merge only leaves a duplicate."""
    parent = list(range(len(raw)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    by_file: dict[str, list[int]] = {}
    for i, f in enumerate(raw):
        by_file.setdefault(f["file"], []).append(i)
    words = [_words(f["message"]) for f in raw]
    for members in by_file.values():
        for x_pos, x in enumerate(members):
            for y in members[x_pos + 1:]:
                fx, fy = raw[x], raw[y]
                near = fx["start_line"] - near_lines <= fy["end_line"] and fy["start_line"] - near_lines <= fx["end_line"]
                sim = jaccard(words[x], words[y])
                if (near and sim >= near_jaccard) or sim >= far_jaccard:
                    parent[find(x)] = find(y)
    groups: dict[int, list[int]] = {}
    for i in range(len(raw)):
        groups.setdefault(find(i), []).append(i)
    merged: list[dict] = []
    for members in groups.values():
        items = [raw[i] for i in members]
        best = max(items, key=lambda f: (f["confidence"], SEVERITIES[f["severity"]], len(f["message"])))
        also: dict[str, dict] = {}
        for item in items:
            for other in item.get("also_in", []):
                also.setdefault(other["file"], other)
        merged.append({**best,
                       "severity": max((f["severity"] for f in items), key=SEVERITIES.get),
                       "confidence": max(f["confidence"] for f in items),
                       "also_in": list(also.values()),
                       "lenses": sorted({f.get("lens", "?") for f in items}),
                       "reports": len(items)})
    merged.sort(key=lambda f: (f["file"], f["start_line"]))
    for i, f in enumerate(merged, 1):
        f["id"] = f"c{i:03d}"
    return merged


# --- Stage transitions ---------------------------------------------------------------

def collect_raw_findings(work: Path, repo: Path, plan: list[dict]) -> list[dict]:
    hunks = read_json(work / "hunks.json", {})
    changed = list(hunks)
    lengths: dict = {}
    raw: list[dict] = []
    for task in plan:
        if task["stage"] not in ("discover", "gap"):
            continue
        items, _error = load_result(work / "results" / f"{task['id']}.json", "findings")
        for item in items or []:
            finding = normalize_finding(item, repo, changed, lengths)
            if finding:
                finding["lens"] = task["lens"]
                finding["task"] = task["id"]
                raw.append(finding)
    return raw


def plan_gap_tasks(work: Path, repo: Path, meta: dict, plan: list[dict], config: dict, skill_dir: Path) -> int:
    """A reviewer pass over the hunks nobody has said anything about. Coverage is something a
    script can measure exactly, and those hunks are where attention is known to run out."""
    gap = config["gap"]
    if not gap.get("enabled", True):
        return 0
    raw = collect_raw_findings(work, repo, plan)
    covered: dict[str, list[tuple[int, int]]] = {}
    for f in raw:
        covered.setdefault(f["file"], []).append((f["start_line"] - gap["margin"], f["end_line"] + gap["margin"]))
    files = parse_diff((work / "diff.patch").read_text(errors="replace"))
    for file in files:
        classify(file)
    uncovered: list[FileDiff] = []
    for file in files:
        if file.skip_reason:
            continue
        keep = []
        for hunk in file.hunks:
            changed = hunk.changed_new_lines()
            if len(changed) < gap["min_changed_lines"]:
                continue
            spans = covered.get(file.path, [])
            if not any(a <= line <= b for line in changed for a, b in spans):
                keep.append(hunk)
        if keep:
            clone = FileDiff(path=file.path, old_path=file.old_path, status=file.status, hunks=keep,
                             additions=file.additions, deletions=file.deletions, kinds=file.kinds)
            uncovered.append(clone)
    if not uncovered:
        return 0
    partitions = make_partitions(uncovered, config["partition"]["target_chars"], gap["max_tasks"])
    template = reference("discovery.md", skill_dir)
    for part in partitions:
        task = {"id": f"g_gap_{part.index:02d}_1", "stage": "gap", "lens": "gap", "part": part.index, "pass": 1}
        prompt = fill(template, pr_context=meta["pr_context"], rules="", lens_title=config["gap"]["title"],
                      lens_body=reference("lenses/gap.md", skill_dir), part_label=f"gap section {part.index} of {len(partitions)}",
                      diff=f"<diff>\n{part.rendered()}\n</diff>", hints="(none)", work=meta["work_rel"],
                      max_steps=config["budget"]["max_steps"], output=reference("output-findings.md", skill_dir))
        _write_task(work, plan, task, prompt)
    return len(partitions)


def _snippet(repo: Path, finding: dict, context: int = 12, limit: int = 70) -> str:
    try:
        lines = (repo / finding["file"]).read_text(errors="replace").split("\n")
    except OSError:
        return "(could not read file)"
    start = max(1, finding["start_line"] - context)
    end = min(len(lines), min(finding["end_line"] + context, start + limit))
    return "\n".join(f"{n:>6}  {lines[n - 1][:240]}" for n in range(start, end + 1))


def build_candidates(work: Path, repo: Path, plan: list[dict], config: dict) -> list[dict]:
    raw = collect_raw_findings(work, repo, plan)
    write_json(work / "raw_candidates.json", raw)
    merge = config["merge"]
    candidates = cluster_findings(raw, near_lines=merge["near_lines"], near_jaccard=merge["near_jaccard"], far_jaccard=merge["far_jaccard"])
    write_json(work / "candidates.json", candidates)
    return candidates


def plan_dedupe_tasks(work: Path, repo: Path, meta: dict, plan: list[dict], config: dict, skill_dir: Path) -> int:
    """Word overlap merges only the obvious duplicates. Whether two differently worded comments
    are one issue takes judgment, so a model decides, one file at a time."""
    candidates = build_candidates(work, repo, plan, config)
    dedupe = config["dedupe"]
    if not dedupe.get("enabled", True):
        return 0
    by_file: dict[str, list[dict]] = {}
    for candidate in candidates:
        by_file.setdefault(candidate["file"], []).append(candidate)
    template = reference("dedupe.md", skill_dir)
    index = 0
    for file, members in by_file.items():
        if len(members) < dedupe["min_candidates"]:
            continue
        members.sort(key=lambda c: c["start_line"])
        for start in range(0, len(members), dedupe["max_per_task"]):
            chunk = members[start:start + dedupe["max_per_task"]]
            if len(chunk) < 2:
                continue
            index += 1
            comments = "\n".join(f'<comment id="{c["id"]}" lines="{c["start_line"]}-{c["end_line"]}">{c["message"]}</comment>' for c in chunk)
            task = {"id": f"m_{index:03d}", "stage": "dedupe", "lens": "dedupe", "part": 0, "pass": 1, "claims": [c["id"] for c in chunk]}
            _write_task(work, plan, task, fill(template, file=file, comments=comments))
    return index


def merge_groups(candidates: list[dict], groups: list[list[str]]) -> list[dict]:
    """Keep the best-supported wording of each group. Everything else about the group is pooled."""
    by_id = {c["id"]: c for c in candidates}
    absorbed: set[str] = set()
    for ids in groups:
        members = [by_id[i] for i in dict.fromkeys(ids) if i in by_id and i not in absorbed]
        members = [m for m in members if m["file"] == members[0]["file"]] if members else []
        if len(members) < 2:
            continue
        best = max(members, key=lambda c: (c["reports"], c["confidence"], len(c["message"])))
        also: dict[str, dict] = {}
        for member in members:
            for other in member.get("also_in", []):
                also.setdefault(other["file"], other)
        best["severity"] = max((m["severity"] for m in members), key=SEVERITIES.get)
        best["confidence"] = max(m["confidence"] for m in members)
        best["reports"] = sum(m["reports"] for m in members)
        best["lenses"] = sorted({lens for m in members for lens in m.get("lenses", [])})
        best["also_in"] = list(also.values())
        absorbed.update(m["id"] for m in members if m is not best)
    return [c for c in candidates if c["id"] not in absorbed]


def plan_verify_tasks(work: Path, repo: Path, meta: dict, plan: list[dict], config: dict, skill_dir: Path) -> int:
    candidates = read_json(work / "candidates.json")
    if candidates is None:
        candidates = build_candidates(work, repo, plan, config)
    groups: list[list[str]] = []
    for task in plan:
        if task["stage"] == "dedupe":
            items, _error = load_result(work / "results" / f"{task['id']}.json", "groups")
            allowed = set(task["claims"])
            groups += [[i for i in item.get("ids", []) if i in allowed] for item in items or [] if isinstance(item.get("ids"), list)]
    if groups:
        candidates = merge_groups(candidates, groups)
        write_json(work / "candidates.json", candidates)
    verify = config["verify"]
    if not verify.get("enabled", True) or not candidates:
        return 0
    ranked = sorted(candidates, key=lambda f: (-f["reports"], -SEVERITIES[f["severity"]], -f["confidence"]))
    chosen = ranked[: verify["max_candidates"]]
    by_file: dict[str, list[dict]] = {}
    for candidate in chosen:
        by_file.setdefault(candidate["file"], []).append(candidate)
    batches: list[list[dict]] = []
    size = verify["batch_size"]
    for members in by_file.values():
        members.sort(key=lambda f: f["start_line"])
        for i in range(0, len(members), size):
            batches.append(members[i:i + size])
    template = reference("verify.md", skill_dir)
    for index, batch in enumerate(batches, 1):
        claims = "\n\n".join(
            f'<claim id="{c["id"]}" file="{c["file"]}" lines="{c["start_line"]}-{c["end_line"]}">\n{c["message"]}\n</claim>\n'
            f'Code at head:\n```\n{_snippet(repo, c)}\n```' for c in batch)
        task = {"id": f"v_{index:03d}", "stage": "verify", "lens": "verify", "part": 0, "pass": 1, "claims": [c["id"] for c in batch]}
        _write_task(work, plan, task, fill(template, pr_context=meta["pr_context"], work=meta["work_rel"], claims=claims, max_steps=config["budget"]["max_steps"]))
    return len(batches)


STAGES = ("discover", "gap", "dedupe", "verify", "done")


def advance(work: Path) -> str:
    """Move to the next stage once every task of the current one has a result."""
    meta = read_json(work / "meta.json")
    plan = read_json(work / "plan.json")
    repo = Path(meta["repo"])
    skill_dir = Path(meta.get("skill_dir") or SKILL_DIR)
    config = load_config(skill_dir)
    planners = {"gap": plan_gap_tasks, "dedupe": plan_dedupe_tasks, "verify": plan_verify_tasks}
    stage = meta["stage"]
    while stage != "done":
        stage = STAGES[STAGES.index(stage) + 1]
        if stage == "done" or planners[stage](work, repo, meta, plan, config, skill_dir):
            break
    meta["stage"] = stage
    write_json(work / "meta.json", meta)
    write_json(work / "plan.json", plan)
    return stage


def pending_tasks(work: Path) -> list[dict]:
    meta = read_json(work / "meta.json")
    plan = read_json(work / "plan.json")
    return [t for t in plan if t["stage"] == meta["stage"] and not (work / "results" / f"{t['id']}.json").exists()]


def next_pending(work: Path) -> tuple[str, list[dict]]:
    while True:
        meta = read_json(work / "meta.json")
        if meta["stage"] == "done":
            return "done", []
        pending = pending_tasks(work)
        if pending:
            return meta["stage"], pending
        advance(work)


# --- Finalize ------------------------------------------------------------------------

def _quote_found(repo: Path, work: Path, path: str | None, quote: str | None) -> bool:
    """A verifier may drop a finding only if the code it cites as counter-evidence exists.
    This catches the expensive failure: a verifier inventing a guard that isn't there."""
    if not path or not quote:
        return False
    needle = re.sub(r"\s+", " ", str(quote)).strip()
    if len(needle) < 4:
        return False
    clean = str(path).strip().replace("\\", "/").lstrip("./")
    for root in (repo, work / "base"):
        try:
            text = (root / clean).read_text(errors="replace")
        except OSError:
            continue
        if needle in re.sub(r"\s+", " ", text):
            return True
    return False


def apply_verdicts(work: Path, repo: Path, candidates: list[dict], plan: list[dict]) -> list[dict]:
    verdicts: dict[str, dict] = {}
    for task in plan:
        if task["stage"] != "verify":
            continue
        items, _error = load_result(work / "results" / f"{task['id']}.json", "verdicts")
        for item in items or []:
            if item.get("id") in task["claims"]:
                verdicts[item["id"]] = item
    out = []
    for candidate in candidates:
        result = dict(candidate)
        verdict = verdicts.get(candidate["id"])
        if not verdict:
            result["verdict"] = "unverified"
        else:
            value = str(verdict.get("verdict") or "uncertain").lower()
            if value == "refuted" and not _quote_found(repo, work, verdict.get("evidence_file"), verdict.get("evidence_quote")):
                value = "uncertain"
                result["verdict_note"] = "refutation discarded: its evidence quote was not found in the code"
            result["verdict"] = value if value in ("confirmed", "refuted", "uncertain") else "uncertain"
            result["verdict_reasoning"] = str(verdict.get("reasoning") or "")[:600]
            rewritten = re.sub(r"\s+", " ", str(verdict.get("message") or "")).strip()
            if value == "confirmed" and len(rewritten) >= 40:
                result["message"] = rewritten[:1800]
            for field in ("start_line", "end_line"):
                try:
                    if verdict.get(field):
                        result[field] = int(verdict[field])
                except (TypeError, ValueError):
                    pass
            if result["end_line"] < result["start_line"]:
                result["end_line"] = result["start_line"]
        out.append(result)
    return out


def finalize(work: Path, *, profile: str | None = None, agent: str = "pr-review", out: Path | None = None) -> dict:
    meta = read_json(work / "meta.json")
    plan = read_json(work / "plan.json")
    repo = Path(meta["repo"])
    config = load_config(Path(meta.get("skill_dir") or SKILL_DIR))
    profile_name = profile or meta.get("profile") or config["default_profile"]
    rules = config["profiles"][profile_name]
    if not (work / "candidates.json").exists():  # the review stopped early, by deadline or host failure
        build_candidates(work, repo, plan, config)
    judged = apply_verdicts(work, repo, read_json(work / "candidates.json", []), plan)
    kept = [c for c in judged if c["verdict"] not in rules["drop_verdicts"] and c["confidence"] >= rules["min_confidence"]
            and SEVERITIES[c["severity"]] >= SEVERITIES[rules["min_severity"]]]
    kept.sort(key=lambda c: (-SEVERITIES[c["severity"]], -c["reports"], -c["confidence"]))
    kept = kept[: rules["max_findings"]]
    findings: list[dict] = []
    for c in kept:
        findings.append({"file": c["file"], "start_line": c["start_line"], "end_line": c["end_line"], "message": c["message"],
                         "severity": c["severity"], "category": c["category"], "confidence": c["confidence"], "verdict": c["verdict"],
                         "lenses": c.get("lenses", [])})
    if rules.get("expand_also_in"):
        # A reader looks where the code is, so a defect spanning files gets a comment in each. Only
        # files the message itself names: otherwise models list anything vaguely related.
        for c in kept:
            for other in c.get("also_in", []):
                if other["file"] == c["file"] or Path(other["file"]).name not in c["message"]:
                    continue
                note = f" (Same issue as {c['file']}:{c['start_line']}.)"
                findings.append({"file": other["file"], "start_line": other["start_line"], "end_line": other["end_line"],
                                 "message": c["message"] + note, "severity": c["severity"], "category": c["category"],
                                 "confidence": c["confidence"], "verdict": c["verdict"], "lenses": c.get("lenses", [])})
    nwo = meta.get("nwo")
    repo_url = f"https://github.com/{nwo}" if nwo else git(repo, "remote", "get-url", "origin", check=False).strip() or str(repo)
    pr_number = meta.get("pr_number") or 0
    contract = {
        "pr": {"repo": repo_url, "pr_number": pr_number, "base": meta["base"], "head": meta["head"]},
        "agent": agent,
        "findings": [{"file": f["file"], "start_line": f["start_line"], "end_line": f["end_line"], "message": f["message"], "producer": agent} for f in findings],
    }
    stats = read_json(work / "stats.json", {})
    if stats:
        contract["usage"] = {"prompt_tokens": stats.get("input", 0), "completion_tokens": stats.get("output", 0),
                             "cached_tokens": stats.get("cache_read", 0), "time_in_ms": int(stats.get("seconds", 0) * 1000),
                             "cost_usd": stats.get("cost", 0.0)}
    target = out or work / "findings.json"
    write_json(target, contract)
    write_json(work / "final.json", {"profile": profile_name, "judged": judged, "kept": findings})
    (work / "review.md").write_text(render_review(meta, findings, judged, profile_name))
    return {"path": str(target), "kept": len(findings), "candidates": len(judged),
            "refuted": sum(c["verdict"] == "refuted" for c in judged), "work": str(work)}


def render_review(meta: dict, findings: list[dict], judged: list[dict], profile: str) -> str:
    lines = [f"# Review: {meta.get('title') or meta['head'][:10]}", ""]
    if not findings:
        lines.append("No issues found.")
    for severity in ("high", "medium", "low"):
        group = [f for f in findings if f["severity"] == severity]
        if not group:
            continue
        lines += [f"## {severity.capitalize()} ({len(group)})", ""]
        for f in group:
            where = f"{f['file']}:{f['start_line']}" + (f"-{f['end_line']}" if f["end_line"] != f["start_line"] else "")
            lines.append(f"- **`{where}`** ({f['category']}{', not fully verified' if f['verdict'] != 'confirmed' else ''}) {f['message']}")
        lines.append("")
    lines.append(f"---\n{len(findings)} findings from {len(judged)} distinct candidates, profile `{profile}`, "
                 f"{meta.get('partitions', 0)} diff parts, {sum(c['verdict'] == 'refuted' for c in judged)} disproved against the code.")
    if meta.get("skipped_files"):
        lines.append(f"Not reviewed: {len(meta['skipped_files'])} generated, vendored or binary files.")
    return "\n".join(lines) + "\n"


# --- Headless driver -----------------------------------------------------------------

STAGE_KEYS = {"verify": "verdicts", "dedupe": "groups"}


def _cache_path(cache_dir: Path, prompt: str, model: str, salt: str) -> Path:
    key = hashlib.sha256(f"{model}\n{salt}\n{prompt}".encode()).hexdigest()
    return cache_dir / key[:2] / f"{key}.json"


def run_tasks(work: Path, tasks: list[dict], host, *, concurrency: int, until: float, task_timeout: float,
              key: str, cache_dir: Path | None, salt: str, totals: Usage, model: str, min_seconds: float = 15) -> dict:
    """Run tasks in parallel up to a deadline. A failed task only loses its own coverage;
    only a failure of the host itself (auth, quota, blocked model) stops the batch. A task is not started
    without time to finish: killing one loses its answer and every token it already spent."""
    counts = {"ok": 0, "cached": 0, "failed": 0, "skipped": 0}
    fatal: list[str] = []

    def one(task: dict) -> None:
        result_path = work / "results" / f"{task['id']}.json"
        prompt = (work / "tasks" / f"{task['id']}.md").read_text()
        cache = _cache_path(cache_dir, prompt, model, salt) if cache_dir else None
        if cache and cache.exists():
            write_json(result_path, read_json(cache))
            counts["cached"] += 1
            return
        reply = Reply(error="not started")
        retries = 2
        for attempt in range(retries):
            remaining = until - time.time()
            if fatal or remaining < min_seconds:
                reason = fatal[0] if fatal else "out of time" + (f", after: {reply.error}" if attempt and reply.error else "")
                write_json(result_path, {"text": "", "error": reason, "task": task["id"]})
                counts["skipped"] += 1
                return
            unparseable = attempt > 0 and not reply.error
            ask = prompt + "\n\nYour previous answer had no parseable JSON. Reply with only the JSON object described above." if unparseable else prompt
            reply = host.ask(ask, timeout=min(task_timeout, remaining))
            totals.add(reply.usage)
            if reply.fatal:
                fatal.append(reply.error or "host failure")
                break
            if not reply.error and extract_list(reply.text, key) is not None:
                break
        record = {"text": reply.text, "error": reply.error, "task": task["id"], "seconds": round(reply.seconds, 1), "api_seconds": round(reply.api_seconds, 1), "throttled": reply.throttled[:3], "tools": reply.tools,
                  "tool_calls": reply.tool_calls, "usage": vars(reply.usage)}
        ok = not reply.error and extract_list(reply.text, key) is not None
        if not ok and not reply.error:
            record["error"] = "no parseable output"
        write_json(result_path, record)
        counts["ok" if ok else "failed"] += 1
        if ok and cache:
            write_json(cache, record)

    with cf.ThreadPoolExecutor(max(1, concurrency)) as pool:
        for future in [pool.submit(one, t) for t in tasks]:
            future.result()
    if fatal:
        raise HostFailure(fatal[0])
    return counts


def drive(work: Path, host_name: str, model: str | None, *, concurrency: int = 24, deadline: float | None = None,
          task_timeout: float = 300, cache_dir: Path | None = None, salt: str = "", host=None, effort: str | None = None) -> dict:
    meta = read_json(work / "meta.json")
    repo = Path(meta["repo"])
    config = load_config(Path(meta.get("skill_dir") or SKILL_DIR))
    budget_seconds = deadline if deadline is not None else meta["deadline_seconds"]
    started = time.time()
    shares = config["budget"]["stage_share"]
    totals = Usage()
    summary: dict[str, dict] = {}
    context = host or make_host(host_name, repo, model, max_steps=config["budget"]["max_steps"], effort=effort)
    cache_model = f"{model or host_name}#{effort}" if effort else (model or host_name)
    failure: str | None = None
    with context as active:
        while True:
            stage, pending = next_pending(work)
            if stage == "done":
                break
            until = started + budget_seconds * shares.get(stage, 0.9)
            key = STAGE_KEYS.get(stage, "findings")
            log(f"  {stage}: {len(pending)} tasks, {max(0, int(until - time.time()))}s budget")
            try:
                # Merging is quick, so only the stages that explore need the full margin.
                floor = 25 if stage == "dedupe" else config["budget"].get("min_task_seconds", 15)
                counts = run_tasks(work, pending, active, concurrency=concurrency, until=until, task_timeout=task_timeout,
                                   key=key, cache_dir=cache_dir, salt=salt, totals=totals, model=cache_model, min_seconds=floor)
            except HostFailure as exc:
                failure = str(exc)
                log(f"  host failure: {failure}")
                break
            log(f"  {stage}: {counts}")
            summary[stage] = counts
    # If no discovery task produced an answer, no review happened, however the tasks ended: errors, or
    # retries using up the deadline against a dead endpoint. That must not pass for a clean review. One
    # task getting through is enough to count the review as real, and a PR with nothing to review has no
    # discovery tasks at all, so it is not caught here.
    discover = summary.get("discover")
    if discover and not failure and not (discover["ok"] or discover["cached"]):
        plan = read_json(work / "plan.json")
        errors = [read_json(work / "results" / f"{t['id']}.json", {}).get("error") for t in plan if t["stage"] == "discover"]
        failure = f"no discovery task succeeded: {next((e for e in errors if e), 'unknown error')}"
    stats = {**vars(totals), "seconds": round(time.time() - started, 1), "stages": summary, "model": model, "effort": effort, "host": host_name, "host_failure": failure}
    write_json(work / "stats.json", stats)
    return stats


# --- CLI -----------------------------------------------------------------------------

def _find_work(args) -> Path:
    if getattr(args, "work", None):
        return Path(args.work).resolve()
    root = Path(args.repo or ".").resolve() / ".pr-review"
    runs = sorted((p for p in root.glob("*") if (p / "meta.json").exists()), key=lambda p: p.stat().st_mtime)
    if not runs:
        raise SystemExit("no prepared review found; run `prepare` first")
    return runs[-1]


def _body(args) -> str:
    if getattr(args, "body_file", None):
        return Path(args.body_file).read_text(errors="replace")
    return getattr(args, "body", "") or ""


def cmd_prepare(args) -> int:
    work = prepare(Path(args.repo), base=args.base, head=args.head, title=args.title or "", body=_body(args),
                   diff_file=Path(args.diff_file) if args.diff_file else None, profile=args.profile, nwo=args.nwo, pr_number=args.pr_number)
    meta = read_json(work / "meta.json")
    plan = read_json(work / "plan.json")
    print(f"work directory: {work}")
    print(f"{meta['files_changed']} files, {meta['lines_changed']} changed lines, {meta['partitions']} diff parts, {len(plan)} discovery tasks")
    print(f"next: python3 {Path(__file__).resolve()} next --work {work}")
    return 0


def cmd_next(args) -> int:
    work = _find_work(args)
    stage, pending = next_pending(work)
    if stage == "done":
        print(f"All tasks are finished. Run: python3 {Path(__file__).resolve()} finalize --work {work}")
        return 0
    batch = pending[: args.limit]
    meta = read_json(work / "meta.json")
    print(f"stage: {stage}. {len(pending)} tasks pending. Run these {len(batch)} now, in parallel if you can:\n")
    for task in batch:
        rel = meta["work_rel"]
        print(f"{task['id']}\n  prompt: {rel}/tasks/{task['id']}.md\n  result: {rel}/results/{task['id']}.json")
    print(f"\nFor each task: read the prompt file, do what it says, and write the JSON it asks for to the result file. "
          f"Then run `next` again:\npython3 {Path(__file__).resolve()} next --work {work}")
    return 0


def cmd_status(args) -> int:
    work = _find_work(args)
    meta = read_json(work / "meta.json")
    plan = read_json(work / "plan.json")
    done = sum((work / "results" / f"{t['id']}.json").exists() for t in plan)
    print(json.dumps({"work": str(work), "stage": meta["stage"], "tasks": len(plan), "done": done, "stats": read_json(work / "stats.json", {})}, indent=1))
    return 0


def cmd_finalize(args) -> int:
    work = _find_work(args)
    result = finalize(work, profile=args.profile, agent=args.agent, out=Path(args.out) if args.out else None)
    print(json.dumps(result))
    print((work / "review.md").read_text())
    return 0


def cmd_run(args) -> int:
    work = prepare(Path(args.repo), base=args.base, head=args.head, title=args.title or "", body=_body(args),
                   diff_file=Path(args.diff_file) if args.diff_file else None, profile=args.profile, nwo=args.nwo, pr_number=args.pr_number)
    stats = drive(work, args.host, args.model, concurrency=args.concurrency, deadline=args.deadline, task_timeout=args.task_timeout,
                  cache_dir=Path(args.cache_dir) if args.cache_dir else None, salt=args.salt, effort=args.effort)
    result = finalize(work, profile=args.profile, agent=args.agent, out=Path(args.out) if args.out else None)
    if not args.quiet:
        print((work / "review.md").read_text())
    print(json.dumps({**result, "seconds": stats["seconds"], "stages": stats["stages"], "host_failure": stats["host_failure"]}))
    # An empty review caused by a broken host must not look like a clean one.
    return 3 if stats["host_failure"] and result["candidates"] == 0 else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    def target(p):
        p.add_argument("--repo", default=".")
        p.add_argument("--base", help="base commit or branch; defaults to the merge base with the default branch")
        p.add_argument("--head", help="defaults to HEAD")
        p.add_argument("--title")
        p.add_argument("--body")
        p.add_argument("--body-file")
        p.add_argument("--diff-file", help="use this diff instead of computing it")
        p.add_argument("--profile", choices=["exhaustive", "balanced", "strict"])
        p.add_argument("--nwo", help="owner/repo, used in findings.json")
        p.add_argument("--pr-number", type=int)

    p = sub.add_parser("prepare")
    target(p)
    p.set_defaults(func=cmd_prepare)

    p = sub.add_parser("next")
    p.add_argument("--work")
    p.add_argument("--repo")
    p.add_argument("--limit", type=int, default=8)
    p.set_defaults(func=cmd_next)

    p = sub.add_parser("status")
    p.add_argument("--work")
    p.add_argument("--repo")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("finalize")
    p.add_argument("--work")
    p.add_argument("--repo")
    p.add_argument("--profile", choices=["exhaustive", "balanced", "strict"])
    p.add_argument("--agent", default=os.environ.get("RB_AGENT", "pr-review"))
    p.add_argument("--out")
    p.set_defaults(func=cmd_finalize)

    p = sub.add_parser("run")
    target(p)
    p.add_argument("--host", default="opencode", choices=["opencode", "claude", "codex"])
    p.add_argument("--model", default=os.environ.get("PR_REVIEW_MODEL"))
    p.add_argument("--effort", help="reasoning effort, passed to the host: low, medium, high, xhigh, max")
    p.add_argument("--concurrency", type=int, default=24, help="tasks at once; start them all, since each spends most of its time thinking")
    p.add_argument("--deadline", type=float, help="total seconds; defaults to a budget scaled by PR size")
    p.add_argument("--task-timeout", type=float, default=300)
    p.add_argument("--cache-dir", help="reuse answers to identical prompts")
    p.add_argument("--salt", default="", help="change to get fresh model samples despite the cache")
    p.add_argument("--agent", default=os.environ.get("RB_AGENT", "pr-review"))
    p.add_argument("--out")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_run)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
