"""Deterministic leads for the reviewers. Cheap, language-agnostic, standard library only.

These are hints, never findings. Published results are consistent on this: raw static
analysis makes poor review comments (Semgrep is 5.5% true positive in the ReviewBench
golden set), and long context dumps can make models worse. So output is short and capped,
and a model must confirm any lead against the code before reporting it.

Each check is about something a model is bad at noticing and a machine is good at:
whether a removed name is still used, whether a new name is used at all, whether a changed
file still parses, whether a linked path exists.
"""
from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]

from rpdiff import FileDiff

_DEF_PATTERNS = [
    r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)",
    r"^\s*(?:export\s+|abstract\s+|public\s+|internal\s+|private\s+|sealed\s+|static\s+|final\s+|partial\s+|data\s+|open\s+)*class\s+([A-Za-z_]\w*)",
    r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\*?\s+([A-Za-z_$][\w$]*)",
    r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*(?:async\s*)?(?:\(|function\b|[A-Za-z_$][\w$]*\s*=>)",
    r"^\s*(?:export\s+)?(?:declare\s+)?(?:interface|type|enum)\s+([A-Za-z_$][\w$]*)",
    r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)",
    r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s+([A-Za-z_]\w*)",
    r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait)\s+([A-Za-z_]\w*)",
    r"^\s*(?:(?:public|private|protected|internal|static|final|override|virtual|abstract|async|sealed|synchronized)\s+)+[\w<>\[\],.?]+\s+([A-Za-z_]\w*)\s*\(",
    r"^\s*(?:(?:private|public|protected|internal|override|suspend|inline)\s+)*fun\s+(?:<[^>]+>\s*)?(?:[\w.]+\.)?([A-Za-z_]\w*)",
    r"^\s*(?:function\s+)?([A-Za-z_][\w-]*)\s*\(\)\s*\{",
    r"^\s*(?:public\s+|private\s+|protected\s+|static\s+)*function\s+([A-Za-z_]\w*)",
]
_DEFS = [re.compile(p) for p in _DEF_PATTERNS]
_ENV = re.compile(r"""(?:process\.env\.|os\.environ(?:\.get)?[\[(]\s*['"]|os\.getenv\(\s*['"]|getenv\(\s*['"]|System\.getenv\(\s*['"]|env::var\(\s*['"]|Environment\.GetEnvironmentVariable\(\s*['"]|ENV\[\s*['"])([A-Z][A-Z0-9_]{2,})""")
_MD_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
_LEFTOVER = re.compile(r"""\b(debugger\b|console\.log\(|pdb\.set_trace|breakpoint\(\)|System\.out\.print|fmt\.Print(?:ln|f)?\(|dd\(|var_dump\()|\.only\(|\b(?:fdescribe|fit|xit|xdescribe)\(|\b(?:TODO|FIXME|XXX|HACK)\b""")
_NOT_SYMBOLS = {"main", "init", "test", "setup", "teardown", "index", "default", "render", "constructor", "value", "name", "data", "self", "this", "type", "item", "items", "result", "error", "get", "set", "run", "start", "stop", "close", "open", "read", "write", "update", "create", "delete", "new", "from", "with", "when", "then", "else", "true", "false", "none", "null"}
_GREP_EXCLUDES = [":(exclude)*.lock", ":(exclude)*package-lock.json", ":(exclude)*.min.js", ":(exclude)*.map", ":(exclude)*/vendor/*", ":(exclude)*/node_modules/*", ":(exclude).pr-review/*"]


def _symbols(lines: list[str]) -> list[str]:
    out: list[str] = []
    for line in lines:
        for rx in _DEFS:
            match = rx.match(line)
            if match:
                name = match.group(1)
                if len(name) >= 4 and name.lower() not in _NOT_SYMBOLS:
                    out.append(name)
                break
    return list(dict.fromkeys(out))


def _grep(repo: Path, symbol: str, limit: int = 8) -> list[str]:
    try:
        done = subprocess.run(["git", "-C", str(repo), "grep", "-n", "-w", "-F", "-I", "--", symbol, ".", *_GREP_EXCLUDES],
                              capture_output=True, text=True, errors="replace", timeout=20)
    except (subprocess.TimeoutExpired, OSError):
        return []
    return done.stdout.splitlines()[:limit] if done.returncode == 0 else []


def _show(repo: Path, rev: str, path: str) -> str | None:
    done = subprocess.run(["git", "-C", str(repo), "show", f"{rev}:{path}"], capture_output=True, text=True, errors="replace")
    return done.stdout if done.returncode == 0 else None


# --- Parse checks --------------------------------------------------------------------

def _bash_n(source: str) -> str | None:
    if not shutil.which("bash"):
        return None
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=True) as handle:
        handle.write(source)
        handle.flush()
        done = subprocess.run(["bash", "-n", handle.name], capture_output=True, text=True)
    if done.returncode == 0:
        return None
    return done.stderr.replace(handle.name, "script").strip().splitlines()[0][:200] if done.stderr.strip() else "syntax error"


def _workflow_run_blocks(text: str) -> list[tuple[int, str]]:
    """`run: |` blocks of a GitHub Actions workflow, found by indentation, with no YAML dependency."""
    blocks: list[tuple[int, str]] = []
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        match = re.match(r"^(\s*)(?:-\s+)?run:\s*[|>][-+]?\s*$", lines[i])
        if not match:
            i += 1
            continue
        indent = len(match.group(1))
        start = i + 1
        i += 1
        body: list[str] = []
        while i < len(lines) and (not lines[i].strip() or len(lines[i]) - len(lines[i].lstrip()) > indent):
            body.append(lines[i])
            i += 1
        blocks.append((start + 1, "\n".join(body)))
    return blocks


def _parse_problem(path: str, text: str) -> str | None:
    name = path.lower()
    try:
        if name.endswith(".py"):
            ast.parse(text)
        elif name.endswith(".json") and not re.search(r"(tsconfig|jsconfig|devcontainer|\.vscode/|biome|\.jsonc)", name):
            json.loads(text)
        elif name.endswith(".toml") and tomllib is not None:
            tomllib.loads(text)
        elif name.endswith((".xml", ".csproj", ".props", ".targets", ".fsproj")):
            ET.fromstring(text)
        elif name.endswith((".sh", ".bash")):
            return _bash_n(text)
        elif re.search(r"(^|/)\.github/workflows/[^/]+\.ya?ml$", name):
            for line_no, block in _workflow_run_blocks(text):
                problem = _bash_n(re.sub(r"\$\{\{.*?\}\}", "x", block))
                if problem:
                    return f"shell in the `run:` block at line {line_no}: {problem}"
    except SyntaxError as exc:
        return f"line {exc.lineno}: {exc.msg}"
    except json.JSONDecodeError as exc:
        return f"line {exc.lineno}: {exc.msg}"
    except ET.ParseError as exc:
        return str(exc)[:200]
    except Exception as exc:  # tomllib.TOMLDecodeError and anything else a parser raises
        return str(exc)[:200]
    return None


# --- Collection ----------------------------------------------------------------------

def collect(repo: Path, base: str, files: list[FileDiff], *, max_symbols: int = 60, max_per_file: int = 6) -> dict[str, list[str]]:
    """Leads per changed file. Bounded in count and size, and it must never fail a review."""
    hints: dict[str, list[str]] = {}

    def add(path: str, text: str) -> None:
        bucket = hints.setdefault(path, [])
        if len(bucket) < max_per_file and text not in bucket:
            bucket.append(text)

    reviewable = [f for f in files if f.skip_reason is None]
    added_defs: dict[str, str] = {}
    removed_defs: dict[str, str] = {}
    for file in reviewable:
        added = [l.text for h in file.hunks for l in h.lines if l.tag == "+"]
        removed = [l.text for h in file.hunks for l in h.lines if l.tag == "-"]
        for symbol in _symbols(added):
            added_defs.setdefault(symbol, file.path)
        for symbol in _symbols(removed):
            removed_defs.setdefault(symbol, file.path)

    budget = max_symbols
    # 1. A removed definition that is not redefined anywhere but is still used.
    for symbol, path in removed_defs.items():
        if budget <= 0:
            break
        if symbol in added_defs:
            continue
        budget -= 1
        hits = _grep(repo, symbol, 5)
        if hits:
            sites = ", ".join(":".join(h.split(":")[:2]) for h in hits[:3])
            add(path, f"`{symbol}` was removed from this file but the name still appears at head: {sites}. Check for a dangling reference, or confirm it moved.")

    # 2. A new definition nothing refers to (feature not wired in, or dead code).
    for symbol, path in added_defs.items():
        if budget <= 0:
            break
        if symbol in removed_defs or "test" in path.lower():
            continue
        budget -= 1
        hits = [h for h in _grep(repo, symbol, 4)]
        if len(hits) == 1:
            add(path, f"`{symbol}` is new and has no other reference in the repository. Check it is wired in, or is meant as public API.")

    # 3. A changed file that no longer parses, where it parsed before.
    for file in reviewable:
        if file.status == "deleted":
            continue
        target = repo / file.path
        try:
            if not target.is_file() or target.stat().st_size > 800_000:
                continue
            text = target.read_text(errors="replace")
        except OSError:
            continue
        problem = _parse_problem(file.path, text)
        if problem:
            before = None if file.status == "added" else _show(repo, base, file.old_path)
            if before is None or _parse_problem(file.path, before) is None:
                add(file.path, f"Does not parse at head, and did before this PR: {problem}")

    # 4. Environment variables newly read, with nothing else mentioning them.
    seen_env: set[str] = set()
    for file in reviewable:
        for hunk in file.hunks:
            for line in hunk.lines:
                if line.tag != "+":
                    continue
                for name in _ENV.findall(line.text):
                    if name in seen_env:
                        continue
                    seen_env.add(name)
                    if len(_grep(repo, name, 3)) <= 1:
                        add(file.path, f"Reads environment variable `{name}` at line {line.new_no}, which nothing else in the repository sets or documents.")

    # 5. Relative links in changed docs that point nowhere.
    for file in reviewable:
        if "docs" not in file.kinds or file.status == "deleted":
            continue
        for hunk in file.hunks:
            for line in hunk.lines:
                if line.tag != "+":
                    continue
                for target in _MD_LINK.findall(line.text):
                    if re.match(r"^[a-z][a-z0-9+.-]*:|^#|^/|^<", target, re.I):
                        continue
                    clean = target.split("#")[0].split("?")[0]
                    if clean and not (repo / Path(file.path).parent / clean).exists():
                        add(file.path, f"Line {line.new_no} links to `{clean}`, which does not exist relative to this file.")

    # 6. Leftovers on added lines.
    for file in reviewable:
        if "docs" in file.kinds:
            continue
        for hunk in file.hunks:
            for line in hunk.lines:
                if line.tag == "+" and _LEFTOVER.search(line.text):
                    add(file.path, f"Line {line.new_no} looks like a debug leftover or an open TODO: `{line.text.strip()[:90]}`")
                    break
    return hints


def mentions(repo: Path, files: list[FileDiff], *, max_symbols: int = 25, per_symbol: int = 4) -> dict[str, list[str]]:
    """Where the names defined in changed lines are used outside their file."""
    out: dict[str, list[str]] = {}
    remaining = max_symbols
    for file in files:
        if file.skip_reason or remaining <= 0:
            continue
        changed = [l.text for h in file.hunks for l in h.lines if l.tag in "+-"]
        for symbol in _symbols(changed)[:6]:
            if remaining <= 0:
                break
            remaining -= 1
            sites = [":".join(h.split(":")[:2]) for h in _grep(repo, symbol, per_symbol + 6) if not h.startswith(file.path + ":")]
            if sites:
                out.setdefault(file.path, []).append(f"`{symbol}` used in: " + ", ".join(sites[:per_symbol]))
    return out
