"""Checks on a proposed change to the skill, run before it costs anything to evaluate.

The golden findings are public and the improver reads some of them, so the failure to prevent is a
prompt that memorizes answers: it would score well here and be worthless on any other PR. The gate
catches most of this, because validation PRs come from different repositories. This guard catches it
earlier and more directly, by looking for corpus-specific words in what the improver added.

Pure functions, no model involved.
"""
from __future__ import annotations

import collections
import difflib
import json
import re
from pathlib import Path

# The improver may change prompts and tuning knobs. It may not change the mechanics: a bug there
# could fake a better score, for example by breaking the file the judge reads.
ALLOWED = re.compile(r"^(references/[\w./-]+\.md|config\.json)$")
MAX_LENS_WORDS = 450
MAX_TOTAL_GROWTH = 0.25
CONFIG_BOUNDS = {
    ("partition", "target_chars"): (8000, 80000),
    ("partition", "max_partitions"): (4, 40),
    ("budget", "max_discovery_tasks"): (10, 150),
    ("budget", "max_steps"): (3, 16),
    ("budget", "min_task_seconds"): (30, 150),
    ("budget", "deadline_base"): (120, 300),
    ("budget", "deadline_per_1k_lines"): (0, 40),
    ("budget", "deadline_max"): (300, 900),
    ("gap", "max_tasks"): (0, 24),
    ("gap", "margin"): (0, 20),
    ("gap", "min_changed_lines"): (1, 20),
    ("merge", "near_lines"): (0, 20),
    ("merge", "near_jaccard"): (0.2, 0.9),
    ("merge", "far_jaccard"): (0.5, 1.0),
    ("dedupe", "max_per_task"): (5, 60),
    ("verify", "batch_size"): (1, 12),
    ("verify", "max_candidates"): (20, 300),
}
_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_.$-]{3,}")


def tokens(text: str) -> set[str]:
    return {t.strip(".-_$").lower() for t in _TOKEN.findall(text)} - {""}


def _words_of(finding: dict) -> set[str]:
    return tokens(finding.get("message", "")) | tokens(finding.get("file", "").replace("/", " "))


def build_vocabulary(golden_by_repo: dict[str, list[dict]], *, generic_repos: int = 4, background: dict[str, list[dict]] | None = None) -> set[str]:
    """Words the improver saw that are tied to a few repositories: paths, identifiers, product names.
    A word used across at least `generic_repos` repositories is ordinary vocabulary ("timeout",
    "pagination") and stays usable. `background` is the wider corpus, used only to judge which words are
    ordinary. A small training set would otherwise make everyday words look specific."""
    seen_in: dict[str, set[str]] = collections.defaultdict(set)
    for repo, findings in {**(background or {}), **golden_by_repo}.items():
        for finding in findings:
            for word in _words_of(finding):
                seen_in[word].add(repo)
    specific: set[str] = set()
    for repo, findings in golden_by_repo.items():
        specific |= {t for t in tokens(repo.replace("/", " ").replace("_", " ").replace("-", " ")) if len(t) >= 4}
        for finding in findings:
            specific |= {word for word in _words_of(finding) if len(seen_in[word]) < generic_repos}
    return specific


def read_tree(skill: Path) -> dict[str, str]:
    return {str(p.relative_to(skill)): p.read_text(errors="replace") for p in sorted(skill.rglob("*"))
            if p.is_file() and "__pycache__" not in p.parts}


def added_text(before: str, after: str) -> str:
    out = []
    for line in difflib.ndiff(before.splitlines(), after.splitlines()):
        if line.startswith("+ "):
            out.append(line[2:])
    return "\n".join(out)


def word_count(tree: dict[str, str]) -> int:
    return sum(len(text.split()) for path, text in tree.items() if path.endswith(".md"))


def check(champion: Path, candidate: Path, vocabulary: set[str], *, allow_mechanics: bool = False) -> dict:
    """Returns {"ok": bool, "problems": [...], "changed": [...]}. `allow_mechanics` is for a change a person or the session driving
    the loop wrote: it may touch the scripts and restructure the prompts, and the checks for memorized answers still apply."""
    old, new = read_tree(champion), read_tree(candidate)
    changed = sorted(p for p in set(old) | set(new) if old.get(p) != new.get(p))
    problems: list[str] = []
    if not changed:
        problems.append("nothing changed")
    for path in changed:
        if allow_mechanics:
            break
        if not ALLOWED.match(path):
            problems.append(f"{path}: the improver may only change references/*.md and config.json")
        if path not in new and path != "config.json" and path.startswith("references/lenses/"):
            problems.append(f"{path}: to retire a lens, disable it in config.json instead of deleting the file")
    try:
        config = json.loads(new.get("config.json", "{}"))
    except json.JSONDecodeError as exc:
        problems.append(f"config.json does not parse: {exc}")
        config = None
    if config is not None and not allow_mechanics:
        old_config = json.loads(old["config.json"])
        for (section, key), (low, high) in CONFIG_BOUNDS.items():
            value = config.get(section, {}).get(key)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not low <= value <= high:
                problems.append(f"config.json: {section}.{key} = {value!r} is outside {low}..{high}")
        if set(config) != set(old_config):
            problems.append("config.json: top-level sections were added or removed")
        if config.get("profiles") != old_config.get("profiles"):
            problems.append("config.json: profiles are fixed, since they define what each mode reports")
        for lens_id, lens in config.get("lenses", {}).items():
            if f"references/lenses/{lens_id}.md" not in new:
                problems.append(f"config.json: lens {lens_id} has no prompt file")
            if not set(lens.get("applies", [])) <= {"source", "test", "config", "docs", "ui"}:
                problems.append(f"config.json: lens {lens_id} has an unknown file kind")
        if not any(lens.get("enabled", True) for lens in config.get("lenses", {}).values()):
            problems.append("config.json: every lens is disabled")
    for path, placeholders in () if allow_mechanics else (("references/discovery.md", ("{{pr_context}}", "{{diff}}", "{{hints}}", "{{output}}", "{{lens_body}}", "{{max_steps}}")),
                               ("references/verify.md", ("{{pr_context}}", "{{claims}}", "{{max_steps}}")),
                               ("references/dedupe.md", ("{{comments}}", "{{file}}"))):
        for placeholder in placeholders:
            if placeholder not in new.get(path, ""):
                problems.append(f"{path}: lost the {placeholder} placeholder")
    for path in [] if allow_mechanics else changed:
        if path.startswith("references/lenses/") and path in new and len(new[path].split()) > MAX_LENS_WORDS:
            problems.append(f"{path}: {len(new[path].split())} words, over the {MAX_LENS_WORDS} limit. Longer prompts have been reported to hurt; cut before adding")
    if word_count(new) > word_count(old) * (1 + MAX_TOTAL_GROWTH) and not allow_mechanics:
        problems.append(f"prompts grew from {word_count(old)} to {word_count(new)} words in one step")
    for path in changed:
        if path not in new or not (path.endswith(".md") or (allow_mechanics and path.endswith((".py", ".json")))):
            continue
        existing = set().union(*(tokens(text) for text in old.values()))
        leaked = sorted((tokens(added_text(old.get(path, ""), new[path])) & vocabulary) - existing)
        if leaked:
            problems.append(f"{path}: added words tied to specific repositories in the benchmark: {', '.join(leaked[:12])}")
    return {"ok": not problems, "problems": problems, "changed": changed}
