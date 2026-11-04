"""Shared helpers for the benchmark tooling: corpus access and paths.

The golden set is never copied into this repository. It lives in a pinned clone
of ReviewBench under .cache/ so nothing the review agent ships can contain it.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = Path(os.environ.get("RA_CACHE", ROOT / ".cache"))
RB_DIR = CACHE / "ReviewBench"
RB_URL = "https://github.com/review-bench/ReviewBench.git"
# Pinned so every score in the ledger is against the same corpus and judge prompts.
RB_REV = "228f2a8d662c9c1c12d25757e23c52c9a6170d5a"
SPLITS_FILE = ROOT / "bench" / "splits.json"
# A second corpus, of review by the maintainers of high-stakes projects. Separate from ReviewBench, whose
# PRs and splits it never changes. Its golden findings stay under .cache/, the same as ReviewBench's.
MAINTAINER_DIR = CACHE / "maintainer"
MAINTAINER_SPLITS_FILE = ROOT / "bench" / "maintainer_splits.json"
MAINTAINER_PREFIX = "maint_"


def log(*args) -> None:
    print(*args, file=sys.stderr, flush=True)


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    kw.setdefault("check", True)
    kw.setdefault("text", True)
    kw.setdefault("capture_output", True)
    return subprocess.run(cmd, **kw)


def ensure_reviewbench() -> Path:
    if not (RB_DIR / ".git").exists():
        CACHE.mkdir(parents=True, exist_ok=True)
        log(f"cloning ReviewBench into {RB_DIR}")
        run(["git", "clone", "--quiet", RB_URL, str(RB_DIR)])
    current = run(["git", "-C", str(RB_DIR), "rev-parse", "HEAD"]).stdout.strip()
    if current != RB_REV:
        run(["git", "-C", str(RB_DIR), "fetch", "--quiet", "origin"], check=False)
        run(["git", "-C", str(RB_DIR), "checkout", "--quiet", "--detach", RB_REV])
    return RB_DIR


def pr_key(entry: dict) -> str:
    repo = entry["repo"].replace("https://github.com/", "").replace("/", "_")
    return f"{repo}_{entry['pr_number']}-{entry['head'][:8]}"


FIXTURE_DIR = CACHE / "fixtures"


def fixture_manifest() -> dict[str, dict]:
    path = FIXTURE_DIR / "manifest.json"
    return json.loads(path.read_text()) if path.exists() else {}


def reviewbench_manifest() -> dict[str, dict]:
    """ReviewBench's PRs alone. Its splits are computed from this, so that nothing added to the local
    dataset can reshuffle them and move a holdout PR into training."""
    ensure_reviewbench()
    entries = json.loads((RB_DIR / "corpus" / "manifest.json").read_text())
    return {pr_key(e): e for e in entries}


def maintainer_manifest() -> dict[str, dict]:
    path = MAINTAINER_DIR / "manifest.json"
    return json.loads(path.read_text()) if path.exists() else {}


def load_manifest() -> dict[str, dict]:
    return {**reviewbench_manifest(), **maintainer_manifest(), **fixture_manifest()}


def load_official_test_keys() -> list[str]:
    ensure_reviewbench()
    entries = json.loads((RB_DIR / "corpus" / "test" / "test.json").read_text())
    return [pr_key(e) for e in entries]


def load_golden(key: str) -> dict:
    for folder in (FIXTURE_DIR / "golden", MAINTAINER_DIR / "golden"):
        path = folder / f"{key}.json"
        if path.exists():
            return json.loads(path.read_text())
    return json.loads((RB_DIR / "golden" / f"{key}.json").read_text())


def load_splits() -> dict[str, list[str]]:
    # fixture_train and fixture_val exercise the loop on content written for this project.
    splits = {**json.loads(SPLITS_FILE.read_text()), "fixture_train": ["fixtures_ledger_1"], "fixture_val": ["fixtures_notifier_2"]}
    if MAINTAINER_SPLITS_FILE.exists():
        splits.update(json.loads(MAINTAINER_SPLITS_FILE.read_text()))
    return splits


def resolve_keys(selector: str) -> list[str]:
    """A split name, comma-separated split names or PR keys, or `all`. `all` still means ReviewBench's
    219 PRs, since that is what a leaderboard-shaped run is; maint_all selects the maintainer corpus."""
    manifest = load_manifest()
    if selector == "all":
        return sorted(reviewbench_manifest())
    splits = load_splits()
    keys: list[str] = []
    for part in selector.split(","):
        part = part.strip()
        if part in splits:
            if part.startswith(MAINTAINER_PREFIX) and not set(splits[part]) <= set(manifest):
                # Its golden findings are gitignored, so a fresh clone has the split but not the corpus.
                raise SystemExit(f"{part} is not on this machine. Build it as docs/MAINTAINER_CORPUS.md describes")
            keys.extend(splits[part])
        elif part.startswith("fixtures_"):
            keys.append(part)
        elif part in manifest:
            keys.append(part)
        else:
            matches = [k for k in manifest if k.startswith(part)]
            if not matches:
                raise SystemExit(f"unknown split or PR key: {part}")
            keys.extend(matches)
    return list(dict.fromkeys(keys))


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()
