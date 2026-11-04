#!/usr/bin/env python3
"""The Linux kernel for the maintainer corpus. Its review happens on mailing lists, not on GitHub.

  lore.py build [--list netdev] [--per-list 3]    find reviewed patch series and write their records

A patch series on lore.kernel.org becomes a pull request in the corpus's format:

  base     the series' `base-commit:` trailer, fetched from the GitHub mirror at depth 1
  head     made here by applying the series, with fixed committer details, so it is reproducible
  findings replies that quote the patch and say something after the quote, from people MAINTAINERS lists
  outcome  whether the next version of the series still has the quoted lines

Only series carrying `base-commit:` are usable. Others would need their base guessed, and a wrong guess
gives a reviewer the wrong code without any error. The series is kept under .cache/maintainer/lore/ so the
head can be rebuilt, since it exists nowhere upstream.
"""
from __future__ import annotations

import argparse
import dataclasses
import email
import email.policy
import email.utils
import gzip
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
import zlib
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import MAINTAINER_DIR, log, pr_key, run, sha256  # noqa: E402
from maintainers import MAX_FILES, MAX_LINES, MIN_LINES, MODEL_WRITTEN, NIT, PR_SKIP_TITLE, SEED, case_collisions, diff_summary, file_excerpt, git_lines, has_claim, is_substantive, locate, repo_meta, replace_records, rest  # noqa: E402

LORE = "https://lore.kernel.org"
LORE_DIR = MAINTAINER_DIR / "lore"
THREADS = LORE_DIR / "threads"
SERIES = LORE_DIR / "series"
NWO = "torvalds/linux"
FETCH_URL = f"https://github.com/{NWO}.git"
# Subsystems known for close review, across memory safety, concurrency, filesystems and networking.
LISTS = ("netdev", "bpf", "linux-mm", "linux-fsdevel", "linux-block", "io-uring", "rust-for-linux", "kvm", "linux-btrfs", "linux-xfs", "linux-crypto", "linux-pci")
# Bases have to reach Linus's tree to be fetchable, and a subsystem's commits arrive there months later.
WINDOWS = tuple((f"2026{m:02}{d:02}", f"2026{m:02}{d + 9:02}") for m in range(2, 8) for d in (1, 15))
USER_AGENT = "review-dataset-research/0.1"
SECONDS_BETWEEN_REQUESTS = 1.1

BOT_SENDER = re.compile(r"(lkp@intel\.com|syzbot|syzkaller|patchwork-bot|noreply|no-reply|robot|-bot@|bot@|sashiko|kernelci|0-day)", re.I)
TRAILER = re.compile(r"^\s*(reviewed-by|acked-by|tested-by|signed-off-by|nacked-by|suggested-by|reported-by|cc|link|pw-bot)\s*:", re.I)
SIGN_OFF = re.compile(r"^\s*(thanks|thank you|regards|best regards|kind regards|cheers|br|ciao|best)\b[,.! ]*\S{0,30}\s*$", re.I)
PATCH_SUBJECT = re.compile(r"^((?:\[[^\]]*\]\s*)+)(.*)$", re.S)
HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_last_request = [0.0]


# --- Parsing, no network and no git ---------------------------------------------------------------

@dataclasses.dataclass
class Mail:
    id: str
    in_reply_to: str
    subject: str
    name: str
    address: str
    date: str
    body: str


@dataclasses.dataclass
class Hunk:
    new_start: int
    lines: list[str]


def _msgid(value: str | None) -> str:
    return (value or "").strip().strip("<>").split(">")[0].strip()


def split_mbox(data: bytes) -> list[Mail]:
    """lore serves mboxrd: messages start at `From mboxrd@z`, and lines that look like that are escaped."""
    out: list[Mail] = []
    for chunk in re.split(rb"(?m)^From mboxrd@z [^\n]*\n", data):
        if not chunk.strip():
            continue
        chunk = re.sub(rb"(?m)^>(>*From )", rb"\1", chunk)
        message = email.message_from_bytes(chunk, policy=email.policy.default)
        try:
            part = message.get_body(preferencelist=("plain",))
            body = part.get_content() if part else ""
        except (KeyError, LookupError, ValueError):
            continue
        name, address = email.utils.parseaddr(str(message.get("from", "")))
        replies = message.get_all("in-reply-to") or [""]
        out.append(Mail(_msgid(str(message.get("message-id", ""))), _msgid(str(replies[-1])), " ".join(str(message.get("subject", "")).split()),
                        name, address.lower(), str(message.get("date", "")), body.replace("\r\n", "\n")))
    return out


def parse_subject(subject: str) -> dict | None:
    """[PATCH net-next v2 3/5] title, as {version, index, total, title}, or None if it is not a patch."""
    if subject.lower().startswith(("re:", "aw:", "fwd:")):
        return None
    match = PATCH_SUBJECT.match(subject)
    if not match or "patch" not in match.group(1).lower():
        return None
    tags = match.group(1)
    version = re.search(r"\bv(\d+)\b", tags, re.I)
    position = re.search(r"\b(\d+)/(\d+)\b", tags)
    return {"version": int(version.group(1)) if version else 1, "index": int(position.group(1)) if position else 1,
            "total": int(position.group(2)) if position else 1, "title": match.group(2).strip()}


def series_versions(mails: list[Mail]) -> dict[int, dict]:
    """Complete series in a thread, by version. A thread normally holds one, though some contributors post a
    new version as a reply to the old one. A series with a patch missing cannot be applied."""
    versions: dict[int, dict] = {}
    for mail in mails:
        parsed = parse_subject(mail.subject)
        if not parsed:
            continue
        slot = versions.setdefault(parsed["version"], {"cover": None, "patches": {}, "total": parsed["total"], "version": parsed["version"], "address": mail.address})
        if mail.address != slot["address"]:
            continue  # a patch someone else posted in the thread, such as a suggested fix, is not part of the series
        if parsed["index"] == 0:
            slot["cover"] = mail
        elif "\ndiff --git " in "\n" + mail.body:
            slot["patches"][parsed["index"]] = mail
    return {v: s for v, s in versions.items() if s["patches"] and len(s["patches"]) == s["total"]}


def base_commit(slot: dict) -> str | None:
    bodies = ([slot["cover"].body] if slot["cover"] else []) + [m.body for _, m in sorted(slot["patches"].items())]
    for body in bodies:
        match = re.search(r"^base-commit:\s*([0-9a-f]{40})\s*$", body, re.M)
        if match:
            return match.group(1)
    return None


def split_patch(body: str) -> tuple[str, str]:
    """A patch mail's commit message, and its diff."""
    start = re.search(r"^diff --git ", body, re.M)
    head = body[: start.start()] if start else body
    message = head.split("\n---\n")[0] if "\n---\n" in head else head
    return message.strip(), body[start.start():] if start else ""


def parse_diff(diff: str) -> dict[str, list[Hunk]]:
    files: dict[str, list[Hunk]] = {}
    path = None
    for line in diff.split("\n"):
        if line.startswith("diff --git "):
            path = None
        elif line.startswith("+++ "):
            target = line[4:].strip()
            path = target[2:] if target.startswith("b/") else None
            if path:
                files.setdefault(path, [])
        elif path:
            match = HUNK_HEADER.match(line)
            if match:
                files[path].append(Hunk(int(match.group(3)), []))
            elif files[path] and line[:1] in ("+", "-", " ", ""):
                if line == "-- ":
                    path = None  # the signature that ends the mail
                else:
                    files[path][-1].lines.append(line)
    return files


def clean_comment(text: str) -> str:
    """The reviewer's own words: no signature, tags, or sign-off."""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.rstrip() == "--":
            lines = lines[:i]
            break
    lines = [line for line in lines if not TRAILER.match(line)]
    for i, line in enumerate(lines):
        if SIGN_OFF.match(line) and len(lines) - i <= 4:
            lines = lines[:i]
            break
    return "\n".join(lines).strip()


def inline_comments(body: str) -> list[tuple[list[str], str]]:
    """(quoted lines just above, what the reviewer wrote) for each stretch of the reviewer's own text. Text
    before the first quote is a greeting. Deeper quoting is someone else's reply, not the patch."""
    out: list[tuple[list[str], str]] = []
    quote: list[str] = []
    text: list[str] = []
    fresh = True

    def flush() -> None:
        written = clean_comment("\n".join(text))
        if written and quote:
            out.append((list(quote), written))

    for line in body.split("\n"):
        if line.startswith(">"):
            if not fresh:
                flush()
                quote, text, fresh = [], [], True
            depth = len(line) - len(line.lstrip(">"))
            if depth == 1:
                stripped = line[1:]
                quote.append(stripped[1:] if stripped.startswith(" ") else stripped)
        elif line.strip() or text:
            if quote:
                fresh = False
                text.append(line)
    if text:
        flush()
    return out


def _diff_tail(quote: list[str], size: int) -> list[str]:
    """Up to `size` lines at the end of a quote that read as a diff. Those are right above what the reviewer
    wrote. A mail client or an editing reviewer may trim anything else."""
    lines = list(quote)
    while lines and not lines[-1].strip():
        lines.pop()
    tail: list[str] = []
    for line in reversed(lines):
        stripped = line.strip()
        if (line[:1] not in ("+", "-", " ") and stripped) or line.startswith(("+++ ", "--- ")) or re.fullmatch(r"\[?\.\.\.\]?|<snip>|\[snip\]", stripped):
            break
        tail.insert(0, line)
        if len(tail) == size:
            break
    while tail and not tail[0].strip():
        tail.pop(0)
    return tail


def _matches(tail: list[str], files: dict[str, list[Hunk]]) -> list[tuple[str, int]]:
    wanted = [t.rstrip() for t in tail]
    out = []
    for path, hunks in files.items():
        for hunk in hunks:
            rows = [r.rstrip() for r in hunk.lines]
            for i in range(len(rows) - len(wanted) + 1):
                if rows[i:i + len(wanted)] == wanted:
                    new_side = sum(1 for r in hunk.lines[:i + len(wanted)] if r[:1] in (" ", "+", ""))
                    out.append((path, max(1, hunk.new_start + new_side - 1)))
    return out


def anchor_in_diff(quote: list[str], files: dict[str, list[Hunk]]) -> dict | None:
    """Which file and line a quote is about. The same lines can appear in several places, and findings are only
    compared within a file, so guessing wrong makes a correct finding impossible to match. When the quote's file
    header and more of its lines cannot settle it, there is no anchor."""
    tail = _diff_tail(quote, 3)
    if not tail or not any(len(t[1:].strip()) >= 8 for t in tail):
        return None
    named = [m.group(1) for m in (re.match(r"\+\+\+ b/(\S+)", line) for line in quote) if m]
    scope = {named[-1]: files[named[-1]]} if named and named[-1] in files else files
    hits = _matches(tail, scope)
    if len(hits) > 1:
        tail = _diff_tail(quote, 10)
        hits = _matches(tail, scope)
    if len(hits) != 1:
        return None
    path, near = hits[0]
    return {"file": path, "near": near, "new_side": [t[1:] for t in tail if t[:1] != "-"], "last": tail[-1], "last_is_removal": tail[-1][:1] == "-"}


def maintainers_from(text: str) -> set[str]:
    """Addresses of everyone MAINTAINERS lists as a maintainer or designated reviewer."""
    out: set[str] = set()
    for line in text.split("\n"):
        if line[:2] in ("M:", "R:"):
            match = re.search(r"<([^>\s]+@[^>\s]+)>", line)
            if match:
                out.add(match.group(1).lower())
    return out


def anchor_survives(anchor: dict, next_files: dict[str, list[Hunk]]) -> bool | None:
    """Whether the next version of the patch still has the code the reviewer quoted. None when it cannot show that:
    a diff only displays unchanged lines near a change, so a quote ending on one says nothing, and neither does a
    next version that no longer touches the file, since the patch may have been dropped or split. The quoted lines
    are compared together, because one common line such as a return can reappear elsewhere after a rewrite."""
    hunks = next_files.get(anchor["file"])
    if not hunks or anchor["last"][:1] != "+":
        return None
    wanted = [line.rstrip() for line in anchor["new_side"]]
    for hunk in hunks:
        rows = [row[1:].rstrip() for row in hunk.lines if row[:1] != "-"]
        if any(rows[i:i + len(wanted)] == wanted for i in range(len(rows) - len(wanted) + 1)):
            return True
    return False


def next_version_excerpt(anchor: dict, next_files: dict[str, list[Hunk]], limit: int = 3000) -> str:
    near = sorted(next_files.get(anchor["file"], []), key=lambda h: abs(h.new_start - anchor["near"]))[:2]
    return "\n".join(f"@@ +{h.new_start} @@\n" + "\n".join(h.lines) for h in near)[:limit]


def parse_atom(text: str) -> list[dict]:
    rows = []
    for entry in text.split("<entry>")[1:]:
        title = re.search(r"<title[^>]*>(.*?)</title>", entry, re.S)
        link = re.search(r"<link\s[^>]*href=\"([^\"]+)\"", entry)
        if title and link:
            rows.append({"title": " ".join(html.unescape(title.group(1)).split()), "link": html.unescape(link.group(1))})
    return rows


# --- Git ----------------------------------------------------------------------------------------

def apply_series(repo: Path, base: str, patches: list[dict]) -> str:
    """Applies patches in a throwaway index, one commit each, without a work tree. Committer details are fixed
    so the same series always gives the same head."""
    head = base
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index"), "GIT_COMMITTER_NAME": "lore import", "GIT_COMMITTER_EMAIL": "lore-import@localhost"}
        git = ["git", "-C", str(repo)]
        run([*git, "read-tree", base], env=env)
        for patch in patches:
            run([*git, "apply", "--cached", "--whitespace=nowarn", "-"], input=patch["diff"].encode("utf-8", "replace"), text=False, env=env)
            tree = run([*git, "write-tree"], env=env).stdout.strip()
            stamp = {"GIT_AUTHOR_NAME": patch["name"] or "unknown", "GIT_AUTHOR_EMAIL": patch["address"] or "unknown@localhost", "GIT_AUTHOR_DATE": patch["date"], "GIT_COMMITTER_DATE": patch["date"]}
            head = run([*git, "commit-tree", tree, "-p", head, "-m", patch["message"] or patch["title"]], env={**env, **stamp}).stdout.strip()
    run([*git, "update-ref", f"refs/keep/{head}", head])
    return head


def rebuild_head(repo: Path, entry: dict) -> None:
    patches = json.loads((MAINTAINER_DIR / entry["series"]).read_text())
    head = apply_series(repo, entry["base"], patches)
    if head != entry["head"]:
        raise RuntimeError(f"rebuilding {entry['series']} gave {head}, expected {entry['head']}")


# --- Network ----------------------------------------------------------------------------------------

def get(url: str, *, attempts: int = 4) -> bytes | None:
    """Politely, one at a time, and from disk when possible: this is a public service run by volunteers.

    None means the page does not exist. Anything else that goes wrong raises, because a service that cannot be
    reached must not look like a list with no patches on it. Through curl, since a Python installed from
    python.org cannot verify certificates until its own bundle is set up."""
    path = THREADS / (hashlib.sha256(url.encode()).hexdigest()[:24] + ".bin")
    if path.exists():
        return path.read_bytes()
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(f".{os.getpid()}.tmp")
    problem = ""
    for attempt in range(attempts):
        wait = _last_request[0] + SECONDS_BETWEEN_REQUESTS - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_request[0] = time.time()
        done = subprocess.run(["curl", "-sS", "-L", "-m", "60", "-A", USER_AGENT, "-o", str(partial), "-w", "%{http_code}", url], capture_output=True, text=True)
        status = done.stdout.strip()
        if done.returncode == 0 and status == "200":
            partial.replace(path)
            return path.read_bytes()
        partial.unlink(missing_ok=True)
        if done.returncode == 0 and status == "404":
            return None
        problem = f"HTTP {status}" if done.returncode == 0 else done.stderr.strip()[:200]
        time.sleep(5 * 2 ** attempt)
    raise RuntimeError(f"could not reach {url}: {problem}")


def search(list_name: str, query: str) -> list[dict]:
    data = get(f"{LORE}/{list_name}/?q={urllib.parse.quote(query)}&x=A")
    return parse_atom(data.decode(errors="replace")) if data else []


def thread(link: str) -> list[Mail]:
    data = get(link.rstrip("/") + "/t.mbox.gz")
    if not data:
        return []
    try:
        return split_mbox(gzip.decompress(data))
    except (OSError, zlib.error):
        return []


def on_github(sha: str) -> bool:
    """`rest` raises on anything but a clear no, so that a rate limit cannot read as a missing commit."""
    return rest(f"repos/{NWO}/commits/{sha}?per_page=1") is not None


def next_version_files(list_name: str, title: str, version: int) -> dict[str, list[Hunk]] | None:
    """The diff of the series' next version, found by its title. None when no later version can be found, as
    when the series was renamed or this was the last version."""
    words = re.sub(r"[\"\\]", " ", title)[:90].strip()
    best: tuple[int, str] | None = None
    for row in search(list_name, f's:"{words}"'):
        parsed = parse_subject(row["title"])
        if parsed and parsed["title"] == title and parsed["version"] > version and (best is None or parsed["version"] < best[0]):
            best = (parsed["version"], row["link"])
    if not best:
        return None
    versions = series_versions(thread(best[1]))
    slot = versions.get(best[0])
    if not slot:
        return None
    merged: dict[str, list[Hunk]] = {}
    for _, mail in sorted(slot["patches"].items()):
        for path, hunks in parse_diff(split_patch(mail.body)[1]).items():
            merged.setdefault(path, []).extend(hunks)
    return merged


# --- Building records ---------------------------------------------------------------------------------

def review_replies(mails: list[Mail], slot: dict, maintainers: set[str]) -> list[tuple[Mail, Mail]]:
    """(patch, reply) for replies a maintainer wrote directly to one of the series' patches."""
    patches = {m.id: m for m in slot["patches"].values()}
    out = []
    for mail in mails:
        patch = patches.get(mail.in_reply_to)
        own_words = "\n".join(line for line in mail.body.split("\n") if not line.startswith(">"))
        if not patch or BOT_SENDER.search(mail.address + " " + mail.name) or MODEL_WRITTEN.search(own_words):
            continue
        if mail.address != patch.address and mail.address in maintainers:
            out.append((patch, mail))
    return out


def quoted_comments(replies: list[tuple[Mail, Mail]], next_files: dict[str, list[Hunk]]) -> list[dict]:
    """Each claim a reply makes under a quote of the patch, with whether the next version still has that line."""
    rows = []
    for patch, reply in replies:
        files = parse_diff(split_patch(patch.body)[1])
        for quote, written in inline_comments(reply.body):
            anchor = anchor_in_diff(quote, files) if has_claim(written) else None
            if anchor and anchor["new_side"]:
                rows.append({"patch": patch, "reply": reply, "quote": quote, "written": written, "anchor": anchor, "survives": anchor_survives(anchor, next_files)})
    return rows


def build_series(list_name: str, link: str, repo: Path, maintainers: set[str]) -> tuple[dict | None, str]:
    """Cheap questions first. Fetching a base takes up to a minute for a tree this size, and reading the next
    version takes a second, so a thread that shows no change costs almost nothing to reject."""
    from dataset import fetch_commits
    mails = thread(link)
    versions = series_versions(mails)
    if not versions:
        return None, "no complete series in the thread"
    # The version with the most replies is the review-time snapshot, as for GitHub.
    slot = sorted(versions.values(), key=lambda s: (-len(review_replies(mails, s, maintainers)), s["version"]))[0]
    replies = review_replies(mails, slot, maintainers)
    if not replies:
        return None, "no reply from a maintainer"
    base = base_commit(slot)
    if not base:
        return None, "no base-commit"
    root = slot["cover"] or slot["patches"][min(slot["patches"])]
    title = (parse_subject(root.subject) or {"title": root.subject})["title"]
    if PR_SKIP_TITLE.match(title):
        return None, "a revert or similar"
    next_files = next_version_files(list_name, title, slot["version"])
    if next_files is None:
        return None, "no later version found"
    # The same bar as a pull request: two real points, at least one of which changed the code.
    claims = quoted_comments(replies, next_files)
    solid = [c for c in claims if is_substantive(c["written"])]
    if not any(c["survives"] is False for c in solid):
        return None, f"nothing shown to have changed: {len(claims)} comments"
    if len(solid) < 2:
        return None, "only one substantive comment"
    if not on_github(base):
        return None, "base is not in the mirror"
    try:
        fetch_commits(repo, FETCH_URL, [base])
    except subprocess.CalledProcessError as exc:
        return None, f"could not fetch: {(exc.stderr or '')[:120]}"
    patches: list[dict] = []
    for _, mail in sorted(slot["patches"].items()):
        message, diff = split_patch(mail.body)
        parsed = parse_subject(mail.subject) or {"title": mail.subject}
        date = email.utils.parsedate_to_datetime(mail.date).isoformat() if mail.date else "1970-01-01T00:00:00+00:00"
        patches.append({"title": parsed["title"], "message": f"{parsed['title']}\n\n{message}".strip(), "diff": diff, "name": mail.name, "address": mail.address, "date": date})
    try:
        head = apply_series(repo, base, patches)
    except subprocess.CalledProcessError:
        return None, "the series does not apply to its base"
    added, removed, changed = diff_summary(repo, base, head)
    if not MIN_LINES <= added + removed <= MAX_LINES or len(changed) > MAX_FILES or not changed:
        return None, "out of scope"
    if case_collisions(repo, head, changed):
        return None, "a changed file collides by case"
    by_parent: dict[str, list[Mail]] = {}
    for mail in mails:
        by_parent.setdefault(mail.in_reply_to, []).append(mail)
    file_cache: dict[str, list[str] | None] = {}
    comments: list[dict] = []
    for claim in claims:
        anchor, patch, reply, written, quote, survives = claim["anchor"], claim["patch"], claim["reply"], claim["written"], claim["quote"], claim["survives"]
        if anchor["file"] not in changed:
            continue
        lines = file_cache.setdefault(anchor["file"], git_lines(repo, head, anchor["file"]))
        # A later patch in the series can rewrite the line; such a comment does not describe this snapshot.
        end = locate(lines, anchor["new_side"], anchor["near"]) if lines is not None else None
        if end is None:
            continue
        start = max(1, end - max(0, len(anchor["new_side"]) - 1))
        comments.append({
            "file": anchor["file"], "start_line": start, "end_line": end, "alignment": "patch_quote", "body": written, "author": reply.name, "role": "listed as a maintainer or reviewer in the kernel's MAINTAINERS file",
            "created_at": reply.date, "url": f"{LORE}/r/{urllib.parse.quote(reply.id)}", "hunk": "\n".join(quote)[-2500:],
            "replies": [{"by_pr_author": m.address == patch.address, "author": m.name, "body": "\n".join(l for l in m.body.split("\n") if not l.startswith(">"))[:1200]} for m in by_parent.get(reply.id, []) if not BOT_SENDER.search(m.address + " " + m.name)],
            "addressed": survives is False, "followup": next_version_excerpt(anchor, next_files) if survives is False else "", "followup_known": survives is not None, "nit": bool(NIT.match(written)),
            "excerpt": file_excerpt(lines, start, end),
        })
    if len(comments) < 2 or not any(c["addressed"] for c in comments):
        return None, f"too little survived alignment: {len(comments)} comments"
    number = zlib.crc32(root.id.encode())
    series_path = SERIES / f"{number}.json"
    series_path.parent.mkdir(parents=True, exist_ok=True)
    series_path.write_text(json.dumps(patches, indent=1))
    meta = repo_meta(NWO)
    cover_text = split_patch(root.body)[0] if not slot["cover"] else root.body.split("\nbase-commit:")[0]
    record = {
        "source": "lore", "repo": f"https://github.com/{NWO}", "pr_number": number, "pr_url": f"{LORE}/{list_name}/{urllib.parse.quote(root.id)}/", "nwo": NWO, "base": base, "head": head, "final_head": None,
        "title": title, "body": cover_text[:6000], "language": "Rust" if sum(c.endswith(".rs") for c in changed) * 2 > len(changed) else "C", "repo_size_kb": meta["diskUsage"], "stars": meta["stargazerCount"],
        "lines_added": added, "lines_removed": removed, "files_changed": len(changed), "domain": "kernel", "pr_author": root.name, "fetch_url": FETCH_URL, "series": str(series_path.relative_to(MAINTAINER_DIR)),
        "list": list_name, "version": slot["version"], "comments": comments,
    }
    record["key"] = pr_key(record)
    return record, "kept"


def build_list(list_name: str, repo: Path, maintainers: set[str], per_list: int, max_threads: int) -> dict:
    """An error is treated as a possible outage: unless the list still reached its quota, the build keeps its
    previous records and reports failure."""
    reasons: dict[str, int] = {}
    kept: list[dict] = []
    tried = 0
    seen: set[str] = set()
    titles: set[str] = set()  # each version of a series has its own thread, and one series should count once
    for start, end in WINDOWS:
        if len(kept) >= per_list or tried >= max_threads:
            break
        rows = [r for r in search(list_name, f's:PATCH AND b:"base-commit" AND d:{start}..{end}') if parse_subject(r["title"]) and r["link"] not in seen]
        # Fixed by hash, so this does not favour whichever threads the search lists first.
        for row in sorted(rows, key=lambda r: sha256(f"{SEED}:{r['link']}")):
            if len(kept) >= per_list or tried >= max_threads:
                break
            parsed = parse_subject(row["title"])
            if parsed["index"] > 1 or row["link"] in seen or parsed["title"] in titles:
                continue
            seen.add(row["link"])
            tried += 1
            try:
                record, reason = build_series(list_name, row["link"], repo, maintainers)
            except Exception as exc:  # keep going: one odd thread should not stop a list
                record, reason = None, f"error: {type(exc).__name__}: {str(exc)[:100]}"
            reason = reason.split(":")[0]
            reasons[reason] = reasons.get(reason, 0) + 1
            if record:
                titles.add(parsed["title"])
                kept.append(record)
                log(f"  {list_name}: {record['title'][:70]} ({len(record['comments'])} comments)")
    failed = bool(reasons.get("error")) and len(kept) < per_list
    if failed:
        log(f"{list_name}: FAILED with {reasons['error']} errors, keeping its previous records")
    else:
        replace_records(NWO.replace("/", "_"), kept, applies=lambda r: r.get("list") == list_name)
    log(f"{list_name}: kept {len(kept)} of {tried} threads {reasons}")
    return {"list": list_name, "kept": len(kept), "tried": tried, "reasons": reasons, "failed": failed}


def current_maintainers() -> set[str]:
    """Read through the API. Fetching the current tree for this file would put every later fix in the object store
    that reviewers' checkouts share."""
    done = subprocess.run(["gh", "api", "-H", "Accept: application/vnd.github.raw", f"repos/{NWO}/contents/MAINTAINERS"], capture_output=True, text=True, check=True)
    return maintainers_from(done.stdout)


def cmd_build(args) -> int:
    from dataset import repo_cache
    repo = repo_cache(NWO)
    maintainers = current_maintainers()
    log(f"{len(maintainers)} addresses in MAINTAINERS")
    repo_meta(NWO)
    rows = [build_list(name, repo, maintainers, args.per_list, args.max_threads) for name in (args.list or LISTS)]
    failed = [r["list"] for r in rows if r["failed"]]
    print(json.dumps({"kept": sum(r["kept"] for r in rows), "tried": sum(r["tried"] for r in rows), "failed": failed}))
    (LORE_DIR / "build_log.json").write_text(json.dumps(rows, indent=1))
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("build")
    p.add_argument("--list", action="append", default=[])
    p.add_argument("--per-list", type=int, default=3)
    p.add_argument("--max-threads", type=int, default=60)
    p.set_defaults(func=cmd_build)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
