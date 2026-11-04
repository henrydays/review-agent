"""Parse a unified diff, classify the files, and split them into review partitions.

Standard library only: this file ships inside the skill and has to run on any host.
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass, field

MAX_LINE_CHARS = 320


@dataclass
class Line:
    tag: str  # " ", "+" or "-"
    new_no: int | None
    text: str


@dataclass
class Hunk:
    new_start: int
    new_len: int
    header: str
    lines: list[Line] = field(default_factory=list)

    def rendered(self) -> str:
        out = [f"@@ {self.header}".rstrip()] if self.header else ["@@"]
        for line in self.lines:
            number = f"{line.new_no:>6}" if line.new_no is not None else " " * 6
            text = line.text
            if len(text) > MAX_LINE_CHARS:
                text = text[:MAX_LINE_CHARS] + " ...[line truncated]"
            out.append(f"{number} {line.tag} {text}")
        return "\n".join(out)

    def changed_new_lines(self) -> list[int]:
        return [line.new_no for line in self.lines if line.tag == "+" and line.new_no is not None]


@dataclass
class FileDiff:
    path: str
    old_path: str
    status: str = "modified"  # added, modified, deleted, renamed, binary
    hunks: list[Hunk] = field(default_factory=list)
    additions: int = 0
    deletions: int = 0
    kinds: set[str] = field(default_factory=set)
    skip_reason: str | None = None


def _unquote(path: str) -> str:
    """Git C-quotes paths with unusual bytes, including every non-ASCII name."""
    path = path.strip()
    if not (path.startswith('"') and path.endswith('"')):
        return path
    body = path[1:-1]
    raw = bytearray()
    i = 0
    escapes = {"n": 10, "t": 9, "r": 13, "\\": 92, '"': 34, "a": 7, "b": 8, "f": 12, "v": 11}
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            if nxt in escapes:
                raw.append(escapes[nxt])
                i += 2
                continue
            if re.fullmatch(r"[0-7]{3}", body[i + 1:i + 4]):
                raw.append(int(body[i + 1:i + 4], 8))
                i += 4
                continue
        raw.extend(ch.encode())
        i += 1
    return raw.decode(errors="replace")


def _strip_prefix(path: str) -> str:
    path = _unquote(path)
    return path[2:] if path[:2] in ("a/", "b/") else path


_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$")
_GIT_HEADER = re.compile(r'^diff --git (?:"?a/)(.*?)"? (?:"?b/)(.*?)"?$')


def parse_diff(text: str) -> list[FileDiff]:
    files: list[FileDiff] = []
    current: FileDiff | None = None
    hunk: Hunk | None = None
    new_no = 0
    remaining_old = remaining_new = 0
    for raw in text.split("\n"):
        if raw.endswith("\r"):
            raw = raw[:-1]
        if hunk is not None and (remaining_old > 0 or remaining_new > 0):
            tag = raw[:1]
            if tag == "+":
                hunk.lines.append(Line("+", new_no, raw[1:]))
                new_no += 1
                remaining_new -= 1
                current.additions += 1  # type: ignore[union-attr]
                continue
            if tag == "-":
                hunk.lines.append(Line("-", None, raw[1:]))
                remaining_old -= 1
                current.deletions += 1  # type: ignore[union-attr]
                continue
            if tag == " " or raw == "":
                hunk.lines.append(Line(" ", new_no, raw[1:]))
                new_no += 1
                remaining_old -= 1
                remaining_new -= 1
                continue
            if tag == "\\":
                continue
        if raw.startswith("diff --git "):
            match = _GIT_HEADER.match(raw)
            old, new = (match.group(1), match.group(2)) if match else ("", "")
            current = FileDiff(path=_unquote(new), old_path=_unquote(old))
            files.append(current)
            hunk = None
            continue
        if current is None:
            continue
        if raw.startswith("new file mode"):
            current.status = "added"
        elif raw.startswith("deleted file mode"):
            current.status = "deleted"
        elif raw.startswith("rename from "):
            current.old_path = _unquote(raw[len("rename from "):])
            current.status = "renamed"
        elif raw.startswith("rename to "):
            current.path = _unquote(raw[len("rename to "):])
            current.status = "renamed"
        elif raw.startswith("Binary files ") or raw.startswith("GIT binary patch"):
            current.status = "binary"
        elif raw.startswith("--- ") and hunk is None:
            name = raw[4:].split("\t")[0]
            if name != "/dev/null":
                current.old_path = _strip_prefix(name)
        elif raw.startswith("+++ ") and hunk is None:
            name = raw[4:].split("\t")[0]
            if name != "/dev/null":
                current.path = _strip_prefix(name)
        elif raw.startswith("@@ "):
            match = _HUNK.match(raw)
            if not match:
                continue
            old_len = int(match.group(2)) if match.group(2) is not None else 1
            new_len = int(match.group(4)) if match.group(4) is not None else 1
            new_no = int(match.group(3))
            hunk = Hunk(new_start=new_no, new_len=new_len, header=match.group(5))
            current.hunks.append(hunk)
            remaining_old, remaining_new = old_len, new_len
    return files


# --- Classification ------------------------------------------------------------------

# Almost no accepted finding lands in these (measured on the training split), and their
# diffs are long enough to crowd out the code that matters.
_SKIP = [
    ("lockfile", r"(^|/)(package-lock\.json|npm-shrinkwrap\.json|yarn\.lock|pnpm-lock\.yaml|cargo\.lock|poetry\.lock|pipfile\.lock"
                 r"|go\.sum|composer\.lock|gemfile\.lock|uv\.lock|bun\.lockb?|packages\.lock\.json|flake\.lock|podfile\.lock)$"),
    ("minified or sourcemap", r"\.(min\.(js|css|mjs)|map)$"),
    ("build output", r"(^|/)(dist|build|out|coverage|\.next|__pycache__)/"),
    ("vendored", r"(^|/)(vendor|vendors|node_modules|third_party|thirdparty|bower_components)/"),
    ("generated", r"(_pb2(_grpc)?\.py|\.pb\.(go|cc|h)|\.g\.(dart|cs)|\.generated\.|_generated\.|\.designer\.cs|(^|/)generated/|\.gen\.(go|ts))"),
    ("snapshot", r"(\.snap$|(^|/)__snapshots__/)"),
    ("binary or media", r"\.(png|jpe?g|gif|webp|ico|icns|bmp|tiff?|woff2?|ttf|otf|eot|pdf|zip|gz|tgz|bz2|xz|7z|jar|war|mp[34]|mov|avi|wav|ogg|wasm|exe|dll|so|dylib|class|pyc|bin|dat|pkl|onnx|pt|h5)$"),
    ("data file", r"\.(csv|tsv|parquet|feather|arrow|ndjson|jsonl)$"),
]
_SKIP_COMPILED = [(name, re.compile(rx)) for name, rx in _SKIP]
_TEST = re.compile(r"(^|/)(tests?|__tests__|testing|spec|specs|e2e|fixtures?|testdata)(/|$)|(\.|_|-)(test|tests|spec)\.[a-z0-9]+$|(^|/)test_[^/]+$|tests?\.cs$|test\.(java|kt|go|swift)$")
_DOCS = re.compile(r"\.(md|mdx|rst|adoc|txt)$|(^|/)(docs?|documentation)/|(^|/)(readme|changelog|license|contributing)[^/]*$")
_CONFIG = re.compile(r"\.(ya?ml|json|jsonc|toml|ini|cfg|conf|env|tf|tfvars|hcl|gradle|kts|xml|props|targets|csproj|sln|fsproj|plist|lock|nix|bzl|bazel|cmake|sbt|gemspec|editorconfig|tpl)$"
                     r"|(^|/)(dockerfile[^/]*|makefile|gnumakefile|jenkinsfile|rakefile|procfile|cmakelists\.txt|\.[a-z]*ignore|\.[a-z]*rc|build|workspace|module\.bazel)$"
                     r"|(^|/)\.github/|(^|/)\.gitlab-ci|(^|/)(helm|charts?|k8s|kubernetes|kustomize|terraform|deploy|deployment|infra|ansible)/")
_UI = re.compile(r"\.(tsx|jsx|vue|svelte|html?|css|scss|sass|less|razor|cshtml|xaml|astro|hbs|erb)$")


def classify(file: FileDiff) -> None:
    path = file.path.lower()
    if file.status == "binary":
        file.skip_reason = "binary"
    else:
        for name, rx in _SKIP_COMPILED:
            if rx.search(path):
                file.skip_reason = name
                break
    if not file.hunks and file.skip_reason is None:
        file.skip_reason = "no textual change"
    kinds: set[str] = set()
    if _TEST.search(path):
        kinds.add("test")
    elif _DOCS.search(path):
        kinds.add("docs")
    elif _CONFIG.search(path):
        kinds.add("config")
    else:
        kinds.add("source")
    if _UI.search(path):
        kinds.add("ui")
    file.kinds = kinds


# --- Partitions ----------------------------------------------------------------------

@dataclass
class PartitionItem:
    file: FileDiff
    hunks: list[Hunk]
    part_of_file: int = 1
    parts_of_file: int = 1

    def rendered(self) -> str:
        change = {"added": "new file", "deleted": "deleted", "renamed": f"renamed from {self.file.old_path}"}.get(self.file.status, "modified")
        suffix = f", section {self.part_of_file} of {self.parts_of_file}" if self.parts_of_file > 1 else ""
        title = f"=== {self.file.path} ({change}, +{self.file.additions} -{self.file.deletions}{suffix}) ==="
        return title + "\n" + "\n".join(h.rendered() for h in self.hunks)

    def size(self) -> int:
        return sum(len(h.rendered()) + 1 for h in self.hunks) + len(self.file.path) + 50


@dataclass
class Partition:
    index: int
    items: list[PartitionItem] = field(default_factory=list)

    def kinds(self) -> set[str]:
        out: set[str] = set()
        for item in self.items:
            out |= item.file.kinds
        return out

    def files(self) -> list[str]:
        return list(dict.fromkeys(item.file.path for item in self.items))

    def rendered(self, seed: int = 0) -> str:
        """Later passes see the files in a different order. Attention drops off through a
        long prompt, so an order change moves that blind spot to different code."""
        items = list(self.items)
        if seed:
            random.Random(seed).shuffle(items)
        return "\n\n".join(item.rendered() for item in items)

    def size(self) -> int:
        return sum(item.size() for item in self.items)


def _affinity_key(path: str) -> tuple:
    """Directory first, then stem, so a test sits next to the code it tests."""
    parts = path.split("/")
    name = parts[-1]
    stem = re.sub(r"(\.|_|-)?(test|tests|spec)(?=\.)|^test_", "", name.lower()).split(".")[0]
    top = "/".join(parts[:2]) if len(parts) > 2 else parts[0] if len(parts) > 1 else ""
    return (top, stem, path)


def _split_file(file: FileDiff, limit: int) -> list[PartitionItem]:
    groups: list[list[Hunk]] = [[]]
    used = 0
    for hunk in file.hunks:
        size = len(hunk.rendered()) + 1
        if groups[-1] and used + size > limit:
            groups.append([])
            used = 0
        groups[-1].append(hunk)
        used += size
    return [PartitionItem(file, group, i + 1, len(groups)) for i, group in enumerate(groups)]


def _pack(reviewable: list[FileDiff], target: int) -> list[Partition]:
    partitions: list[Partition] = []
    current = Partition(1)
    used = 0
    for file in reviewable:
        for item in _split_file(file, target):
            size = item.size()
            if current.items and used + size > target:
                partitions.append(current)
                current = Partition(len(partitions) + 1)
                used = 0
            current.items.append(item)
            used += size
    if current.items:
        partitions.append(current)
    return partitions


def make_partitions(files: list[FileDiff], target_chars: int, max_partitions: int) -> list[Partition]:
    """Greedy packing in affinity order. A larger PR gets larger partitions rather than lost
    files: nothing reviewable is ever dropped, because the largest PRs hold many of the most
    serious findings."""
    reviewable = sorted((f for f in files if f.skip_reason is None), key=lambda f: _affinity_key(f.path))
    total = sum(sum(len(h.rendered()) + 1 for h in f.hunks) + 60 for f in reviewable)
    target = max(target_chars, total // max(1, max_partitions) + 1)
    partitions = _pack(reviewable, target)
    while len(partitions) > max_partitions:
        target = int(target * 1.15) + 1
        partitions = _pack(reviewable, target)
    return partitions
