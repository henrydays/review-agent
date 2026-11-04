#!/usr/bin/env python3
"""Corpus management: repo-disjoint splits and isolated per-PR checkouts.

  dataset.py splits            regenerate bench/splits.json (deterministic)
  dataset.py stats [selector]  golden-set stats per split
  dataset.py fetch <selector>  fetch repositories and materialize PR checkouts
  dataset.py fixtures          build the seeded fixture PRs as a miniature corpus

A materialized PR mirrors what the benchmark mounts:

  .cache/prs/<key>/repo             checkout at head; only base and head are reachable by ref
  .cache/prs/<key>/pr/diff.patch    three-dot diff, base...head
  .cache/prs/<key>/pr/pr.json       {repo, pr_number, base, head, nwo, title, body}

Isolation matters: the repository cache of a project can hold commits from other
corpus PRs, some of which may contain the fix for an issue under review. Each
checkout has no remote and only refs for its own base and head, so ordinary git
commands cannot see those commits, and with no promisor remote a blob lookup can
never reach the network.
"""
from __future__ import annotations

import argparse
import collections
import concurrent.futures as cf
import json
import random
import shutil
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import CACHE, FIXTURE_DIR, ROOT, SPLITS_FILE, load_golden, load_manifest, load_official_test_keys, log, resolve_keys, reviewbench_manifest, run

REPOS = CACHE / "repos"
PRS = CACHE / "prs"
MIRROR_ORG = "review-bench"
SEED = 20261103


def size_bucket(entry: dict) -> int:
    lines = entry["lines_added"] + entry["lines_removed"]
    for i, limit in enumerate((200, 1000, 5000)):
        if lines <= limit:
            return i
    return 3


def make_splits() -> dict[str, list[str]]:
    """Splits are disjoint by repository, so a heuristic learned from a project's
    PRs in `train` can never be credited on the same project's PRs in `val`.

    val      the official 25-PR test set, so offline numbers line up with portal test runs
    train    what the improver may read
    holdout  never shown to the improver; only for the final estimate
    *_quick  stratified subsets for fast iterations

    Reads ReviewBench's manifest only. The shuffle below depends on every repository in its input, so a PR
    from anywhere else would silently move others between train and holdout.
    """
    manifest = reviewbench_manifest()
    val = load_official_test_keys()
    val_repos = {manifest[k]["nwo"] for k in val}
    groups: dict[str, list[str]] = collections.defaultdict(list)
    for key, entry in manifest.items():
        if entry["nwo"] not in val_repos:
            groups[entry["nwo"]].append(key)
    # PRs from a val repository that are not themselves in val belong to neither
    # train nor holdout: they are excluded to keep the splits repo-disjoint.
    val_siblings = sorted(k for k, e in manifest.items() if e["nwo"] in val_repos and k not in val)

    rng = random.Random(SEED)
    repos = sorted(groups)
    # Sort by size so each stratum is split evenly, shuffling within it.
    by_bucket: dict[int, list[str]] = collections.defaultdict(list)
    for repo in repos:
        bucket = max(size_bucket(manifest[k]) for k in groups[repo])
        by_bucket[bucket].append(repo)
    train: list[str] = []
    holdout: list[str] = []
    for bucket in sorted(by_bucket):
        members = by_bucket[bucket]
        rng.shuffle(members)
        cut = round(len(members) * 0.6)
        for repo in members[:cut]:
            train.extend(groups[repo])
        for repo in members[cut:]:
            holdout.extend(groups[repo])

    def quick(keys: list[str], n: int) -> list[str]:
        """Stratified by size, favouring PRs with high-severity golden TPs, skipping
        the very largest so one iteration stays affordable."""
        pool = [k for k in keys if size_bucket(manifest[k]) < 3]
        tp_high = {k: sum(f["tp_fp"] == "tp" and f["severity"] == "high" for f in load_golden(k)["findings"]) for k in pool}
        ordered: list[str] = []
        local = random.Random(SEED)
        buckets = {b: sorted((k for k in pool if size_bucket(manifest[k]) == b), key=lambda k: (-tp_high[k], k)) for b in range(3)}
        for b in buckets:
            head = buckets[b][: len(buckets[b]) // 2]
            tail = buckets[b][len(buckets[b]) // 2 :]
            local.shuffle(tail)
            buckets[b] = head + tail
        while len(ordered) < n and any(buckets.values()):
            for b in (1, 0, 2):
                if buckets[b] and len(ordered) < n:
                    ordered.append(buckets[b].pop(0))
        return sorted(ordered)

    return {
        "val": sorted(val),
        "train": sorted(train),
        "holdout": sorted(holdout),
        "val_siblings_unused": val_siblings,
        "train_quick": quick(train, 16),
        "val_quick": quick(val, 12),
    }


def golden_stats(keys: list[str]) -> dict:
    manifest = load_manifest()
    counts: collections.Counter = collections.Counter()
    for key in keys:
        for f in load_golden(key)["findings"]:
            counts["findings"] += 1
            if f["tp_fp"] == "tp":
                counts["tp"] += 1
                counts[f"tp_{f['severity']}"] += 1
            else:
                counts["fp"] += 1
                counts[f"fp_{f['severity']}"] += 1
    lines = sum(manifest[k]["lines_added"] + manifest[k]["lines_removed"] for k in keys)
    return {"prs": len(keys), "lines": lines, **counts}


def repo_cache(nwo: str) -> Path:
    repo = REPOS / nwo.replace("/", "_")
    if not (repo / "HEAD").exists():
        repo.mkdir(parents=True, exist_ok=True)
        run(["git", "init", "--quiet", "--bare", str(repo)])
    return repo


def has_commit(repo: Path, sha: str) -> bool:
    return run(["git", "-C", str(repo), "cat-file", "-e", f"{sha}^{{commit}}"], check=False).returncode == 0


def fetch_commits(repo: Path, url: str, shas: list[str]) -> None:
    """Exactly these commits, with no history. Large projects hold gigabytes of history that a review does not
    read, and fetching two snapshots means the commits that fixed what reviewers found never reach the disk.

    Each commit gets a ref in this cache, for two reasons: without refs git has nothing to tell the server it
    already holds, and every fetch downloads a whole snapshot again, and unreferenced objects could be garbage
    collected. A checkout that borrows these objects through alternates does not see these refs."""
    missing = [s for s in sorted(set(shas)) if not has_commit(repo, s)]
    if not missing:
        return
    run(["git", "-C", str(repo), "-c", "lfs.fetchexclude=*", "-c", "gc.auto=0", "-c", "maintenance.auto=false", "fetch", "--quiet", "--no-tags", "--depth=1", url, *missing],
        timeout=3600, env=_env())
    for sha in missing:
        run(["git", "-C", str(repo), "update-ref", f"refs/keep/{sha}", sha])


def fetch_repo(entries: list[dict]) -> None:
    """Fetch every commit needed for one repository: from its review-bench mirror for ReviewBench PRs, and
    from upstream for the maintainer corpus."""
    nwo = entries[0]["nwo"]
    repo = repo_cache(nwo)
    if entries[0].get("fetch_url"):
        shas = {e["base"] for e in entries} | {e["head"] for e in entries if not e.get("series")}
        fetch_commits(repo, entries[0]["fetch_url"], sorted(shas))
        for entry in entries:
            if entry.get("series") and not has_commit(repo, entry["head"]):
                from lore import rebuild_head
                rebuild_head(repo, entry)
        return
    shas = sorted({e["base"] for e in entries} | {e["head"] for e in entries})
    missing = [s for s in shas if not has_commit(repo, s)]
    if not missing:
        return
    url = f"https://github.com/{MIRROR_ORG}/{nwo.replace('/', '_')}.git"
    run(["git", "-C", str(repo), "-c", "lfs.fetchexclude=*", "fetch", "--quiet", "--no-tags", url, *missing],
        timeout=3600, env=_env())


def _env() -> dict:
    import os
    env = dict(os.environ)
    env["GIT_LFS_SKIP_SMUDGE"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def materialize(key: str, entry: dict, force: bool = False) -> Path:
    pr_dir = PRS / key
    marker = pr_dir / ".ready"
    meta = {k: entry.get(k) for k in ("repo", "pr_number", "base", "head", "nwo", "title", "body")}
    meta["pr_number"] = int(meta["pr_number"])
    if marker.exists() and not force:
        if entry.get("shallow"):
            # This corpus can be rebuilt with a corrected description for the same commit.
            (pr_dir / "pr" / "pr.json").write_text(json.dumps(meta, indent=2))
        return pr_dir
    if pr_dir.exists():
        shutil.rmtree(pr_dir)
    cache = REPOS / entry["nwo"].replace("/", "_")
    repo = pr_dir / "repo"
    (pr_dir / "pr").mkdir(parents=True)
    run(["git", "init", "--quiet", str(repo)])
    # Share objects with the cache instead of copying, exposing only these two commits by ref.
    (repo / ".git" / "objects" / "info" / "alternates").write_text(str((cache / "objects").resolve()) + "\n")
    if (cache / "shallow").exists():
        # Without this git goes looking for parents that were never fetched, and fails. Only this PR's commits are
        # listed: the cache's file names the snapshots of every other PR from the project, some of which are later.
        own = set((cache / "shallow").read_text().split()) & {entry["base"], entry["head"]}
        (repo / ".git" / "shallow").write_text("".join(f"{sha}\n" for sha in sorted(own)))
    git = ["git", "-C", str(repo)]
    run([*git, "update-ref", "refs/rb/base", entry["base"]])
    run([*git, "update-ref", "refs/rb/head", entry["head"]])
    run([*git, "-c", "advice.detachedHead=false", "checkout", "--quiet", "--force", "--detach", entry["head"]], env=_env())
    # A shallow entry has no history to find a merge base in, but its base already is the merge base.
    span = [entry["base"], entry["head"]] if entry.get("shallow") else [f"{entry['base']}...{entry['head']}"]
    diff = run([*git, "diff", *span], text=False).stdout
    (pr_dir / "pr" / "diff.patch").write_bytes(diff)
    (pr_dir / "pr" / "pr.json").write_text(json.dumps(meta, indent=2))
    marker.write_text("ok\n")
    return pr_dir


def build_fixtures() -> list[str]:
    """Materialize the seeded PRs exactly as the benchmark's PRs are laid out."""
    sys.path.insert(0, str(ROOT / "tests" / "fixtures"))
    import seeded_pr
    manifest: dict[str, dict] = {}
    (FIXTURE_DIR / "golden").mkdir(parents=True, exist_ok=True)
    for name, fixture in seeded_pr.FIXTURES.items():
        key = f"fixtures_{name}_{fixture['number']}"
        pr_dir = PRS / key
        if pr_dir.exists():
            shutil.rmtree(pr_dir)
        (pr_dir / "pr").mkdir(parents=True)
        base, head = seeded_pr.build(pr_dir / "repo", name)
        entry = {"repo": f"https://github.com/{fixture['nwo']}", "pr_number": fixture["number"], "base": base, "head": head, "nwo": fixture["nwo"],
                 "title": fixture["title"], "body": fixture["body"], "language": fixture["language"], "fixture": True}
        git = ["git", "-C", str(pr_dir / "repo")]
        diff = run([*git, "diff", f"{base}...{head}"], text=False).stdout
        (pr_dir / "pr" / "diff.patch").write_bytes(diff)
        added = removed = 0
        for line in diff.decode(errors="replace").splitlines():
            added += line.startswith("+") and not line.startswith("+++")
            removed += line.startswith("-") and not line.startswith("---")
        entry.update(lines_added=added, lines_removed=removed)
        (pr_dir / "pr" / "pr.json").write_text(json.dumps({k: entry[k] for k in ("repo", "pr_number", "base", "head", "nwo", "title", "body")}, indent=2))
        (pr_dir / ".ready").write_text("ok\n")
        manifest[key] = entry
        golden = {"pr_key": key, "pr": {k: entry[k] for k in ("repo", "pr_number", "base", "head")}, "findings": seeded_pr.expected(name)}
        (FIXTURE_DIR / "golden" / f"{key}.json").write_text(json.dumps(golden, indent=1))
    (FIXTURE_DIR / "manifest.json").write_text(json.dumps(manifest, indent=1))
    return sorted(manifest)


def fetch(keys: list[str], workers: int = 6) -> tuple[list[str], dict[str, str]]:
    if any(key.startswith("fixtures_") for key in keys):
        build_fixtures()
    manifest = load_manifest()
    by_repo: dict[str, list[dict]] = collections.defaultdict(list)
    for key in keys:
        if not manifest[key].get("fixture"):
            by_repo[manifest[key]["nwo"]].append(manifest[key])
    errors: dict[str, str] = {}

    def work(nwo: str) -> None:
        try:
            fetch_repo(by_repo[nwo])
            log(f"  fetched {nwo}")
        except Exception as exc:  # keep going: one failing mirror should not stop the others
            errors[nwo] = str(getattr(exc, "stderr", exc))[:300]
            log(f"  FAILED {nwo}: {errors[nwo]}")

    with cf.ThreadPoolExecutor(workers) as pool:
        list(pool.map(work, sorted(by_repo)))
    ready: list[str] = []
    for key in keys:
        if manifest[key]["nwo"] in errors:
            continue
        if not manifest[key].get("fixture"):
            materialize(key, manifest[key])
        ready.append(key)
    return ready, errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("splits")
    stats = sub.add_parser("stats")
    stats.add_argument("selector", nargs="?")
    fetch_p = sub.add_parser("fetch")
    fetch_p.add_argument("selector")
    fetch_p.add_argument("--workers", type=int, default=6)
    sub.add_parser("fixtures")
    args = parser.parse_args()

    if args.cmd == "splits":
        splits = make_splits()
        SPLITS_FILE.write_text(json.dumps(splits, indent=1) + "\n")
        for name, keys in splits.items():
            print(name, json.dumps(golden_stats(keys)))
    elif args.cmd == "stats":
        for name in ([args.selector] if args.selector else sorted(json.loads(SPLITS_FILE.read_text()))):
            print(name, json.dumps(golden_stats(resolve_keys(name))))
    elif args.cmd == "fixtures":
        print("\n".join(build_fixtures()))
    elif args.cmd == "fetch":
        ready, errors = fetch(resolve_keys(args.selector), args.workers)
        print(f"ready {len(ready)}, repositories failed {len(errors)}")
        if errors:
            print(json.dumps(errors, indent=1))
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
