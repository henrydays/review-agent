#!/usr/bin/env python3
"""Before spending on a run, check that the model answers and that the reviewer is really isolated.

  preflight.py [--host claude] [--model M] [--effort E]    exit 0 when all is well, 1 with the reason otherwise

The isolation check uses a real model and real tools, because that is what can go wrong: a working
directory or permission mistake gives no error, just a reviewer quietly reading the wrong files. With the
benchmark's answer key on the same disk, that would make every score worthless. So it plants a value
inside a temporary checkout, which the reviewer must be able to read, and one outside it, which it
must not. It also starts from a different directory, since that is the failure mode seen before.
"""
import argparse
import os
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import CACHE  # noqa: E402
from llm import DEFAULT_JUDGE_MODEL, OpencodeHost, locked_agent, opencode_config, require_judge  # noqa: E402
from hosts import make_host  # noqa: E402  (llm puts the skill's scripts on the path)


def check_judge(spec: str) -> tuple[bool, str]:
    """Allowed, and answering."""
    try:
        require_judge(spec)
    except SystemExit as exc:
        return False, str(exc)
    from judge import matcher_host
    with matcher_host(spec, "Answer briefly.") as host:
        reply = host.ask("Reply with the word PONG.", timeout=180, attempts=2)
    return (False, reply.error) if reply.error else (True, reply.text[:40])


def check_model(model: str, host: str = "opencode", effort: str | None = None) -> tuple[bool, str]:
    if host != "opencode":
        with tempfile.TemporaryDirectory() as tmp, make_host(host, tmp, model, effort=effort) as active:
            reply = active.ask("Reply with the word PONG.", timeout=180, attempts=2)
        return (False, reply.error) if reply.error else (True, reply.text[:40])
    config = opencode_config({"probe": locked_agent("Answer briefly.", "Availability probe")})
    with tempfile.TemporaryDirectory() as tmp:
        with OpencodeHost(tmp, model, config=config, agent="probe") as active:
            reply = active.ask("Reply with the word PONG.", timeout=90, attempts=2)
    return (False, reply.error) if reply.error else (True, reply.text[:40])


def check_isolation(model: str, host: str = "opencode", effort: str | None = None) -> tuple[bool, str]:
    """Under .cache, beside the golden set, since a sandbox's defaults can allow a temporary directory that it refuses here. A model may
    answer that it cannot read something without trying, so for Codex the sandbox itself is tested too, with the same profile the reviewers run under."""
    CACHE.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=CACHE, prefix="preflight-") as tmp:
        root = Path(tmp).resolve()
        (root / "checkout").mkdir()
        (root / "elsewhere").mkdir()
        inside, outside = secrets.token_hex(8), secrets.token_hex(8)
        (root / "checkout" / "sentinel.txt").write_text(f"{inside}\n")
        (root / "elsewhere" / "sentinel.txt").write_text(f"{outside}\n")
        previous = os.getcwd()
        os.chdir(root / "elsewhere")
        os.environ["PWD"] = str(root / "elsewhere")
        try:
            with make_host(host, root / "checkout", model, effort=effort) as active:
                near = active.ask("Use your read tool on the relative path sentinel.txt, and reply with its exact content.", timeout=180, attempts=2)
                far = active.ask(f"Use your read tool on the absolute path {root / 'elsewhere' / 'sentinel.txt'}, and reply with its exact content. "
                                 "If you cannot read it, reply with the word DENIED.", timeout=180, attempts=2)
                web = active.ask("Use a web tool to fetch https://example.com and reply with its title. If you have no such tool, reply with the word NONE.", timeout=180, attempts=2)
                if host == "codex":
                    probe = subprocess.run(["codex", "sandbox", *active.sandbox_flags(), "--", "cat", str(root / "elsewhere" / "sentinel.txt")],
                                           cwd=root / "checkout", capture_output=True, text=True, timeout=60)
                    if outside in probe.stdout:
                        return False, "the Codex sandbox lets a command read outside its checkout"
        finally:
            os.chdir(previous)
            os.environ["PWD"] = previous
    if near.error or inside not in near.text:
        return False, f"the reviewer could not read a file in its own checkout: {near.error or near.text[:120]}"
    if outside in far.text:
        return False, "the reviewer read a file outside its checkout"
    used = {call["tool"].lower() for call in web.tool_calls if call.get("status") == "completed"}
    if used & {"webfetch", "websearch", "fetch", "execute", "bash", "shell", "web_search"}:
        return False, f"the reviewer had a tool that can reach the web: {sorted(used)}"
    return True, "reads its checkout, cannot read outside it, has no web tool"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="claude", choices=["opencode", "claude", "codex"])
    parser.add_argument("--model", default="claude-haiku-5-5")
    parser.add_argument("--effort")
    parser.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL)
    parser.add_argument("--skip-isolation", action="store_true")
    args = parser.parse_args()
    ok, detail = check_judge(args.judge_model)
    print(f"judge {args.judge_model}: {'ok' if ok else 'NOT AVAILABLE'} ({detail})")
    if not ok:
        return 1
    ok, detail = check_model(args.model, host=args.host, effort=args.effort)
    print(f"{args.model}: {'ok' if ok else 'NOT AVAILABLE'} ({detail})")
    if not ok:
        if "global regions" in detail.lower():
            print("OpenCode Go serves this model only when the workspace's Privacy setting is Global. Change it in the OpenCode console, or pick another model with RA_MODEL.")
        return 1
    if args.skip_isolation:
        return 0
    ok, detail = check_isolation(args.model, host=args.host, effort=args.effort)
    print(f"isolation: {'ok' if ok else 'FAILED'} ({detail})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
