"""Model access for the benchmark tools. The transport is the skill's own, so the judge and the
reviewer reach models the same way and neither handles credentials."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skill" / "pr-review" / "scripts"))
from hosts import ClaudeHost, CodexHost, OpencodeHost, Reply, Usage, locked_agent, opencode_config  # noqa: E402,F401

# Both default to DeepSeek through OpenCode. Change one value to change the model everywhere.
DEFAULT_MODEL = os.environ.get("RA_MODEL", "opencode-go/deepseek-v4.1-flash")
DEFAULT_JUDGE_MODEL = os.environ.get("RA_JUDGE_MODEL", "claude-sonnet-5-5")
# Findings are judged by Sonnet 5.5 or better, and by nothing else. A weaker judge moves every score, and a loop steering by
# it steers toward what that judge can be talked into. Written `model` or `model#effort`.
APPROVED_JUDGES = ("claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-1", "gpt-6-astra")


def split_spec(spec: str) -> tuple[str, str | None]:
    model, _, effort = spec.partition("#")
    return model, effort or None


def require_judge(spec: str) -> None:
    if split_spec(spec)[0] not in APPROVED_JUDGES:
        raise SystemExit(f"{spec} may not judge. Use one of {', '.join(APPROVED_JUDGES)}. Reviews already finished are kept; judge them with bench/judge.py")


class LockedClaudeHost(ClaudeHost):
    """Claude Code as a plain model call: one system prompt, no tools, one subprocess per question.

    Claude Code reads instruction files from its working directory, the parents of that directory and the
    user's home. Running in this repository would hand a labeler our notes about golden sets, and running with
    user settings would add whatever else the user keeps there. So it runs in an empty directory outside
    this repository, and takes settings only from that directory, which has none."""

    def __init__(self, model: str, system_prompt: str, *, effort: str | None = None):
        self._effort = effort
        self._scratch = tempfile.TemporaryDirectory(prefix="locked-claude-")
        scratch = Path(self._scratch.name).resolve()
        (scratch / "cwd").mkdir()
        self._system_file = scratch / "system-prompt.txt"
        self._system_file.write_text(system_prompt)
        super().__init__(scratch / "cwd", model)

    def __exit__(self, *exc) -> None:
        self._scratch.cleanup()

    def command(self) -> list[str]:
        return ["claude", "-p", "--output-format", "json", "--tools", "", "--strict-mcp-config", "--setting-sources", "project", "--no-session-persistence",
                "--permission-mode", "dontAsk", "--model", self.model, "--system-prompt-file", str(self._system_file)] + (["--effort", self._effort] if self._effort else [])


class SystemPromptCodexHost(CodexHost):
    """Codex has no system prompt to replace, so the instructions lead the prompt. `locked` runs it in an empty directory,
    for questions that need no code. Its sandbox has no network and reads only its working directory, see CodexHost."""

    def __init__(self, cwd: str | Path | None, model: str, system_prompt: str, *, effort: str | None = None):
        self._scratch = tempfile.TemporaryDirectory(prefix="locked-codex-") if cwd is None else None
        super().__init__(Path(self._scratch.name).resolve() if self._scratch else cwd, model, effort=effort)
        self._system_prompt = system_prompt

    def __exit__(self, *exc) -> None:
        if self._scratch:
            self._scratch.cleanup()

    def ask(self, prompt: str, **kw) -> Reply:
        return super().ask(f"{self._system_prompt}\n\n---\n\n{prompt}", **kw)


def usage_limits() -> dict | None:
    """OpenCode Go meters, as percent used in the 5-hour, weekly and monthly windows."""
    try:
        done = subprocess.run(["opencode", "api", "GET", "/api/experimental/usage/limits"], capture_output=True, text=True, timeout=30)
        data = json.loads(done.stdout)
        return (data.get("data") or data).get("go")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def parse_claude_usage(stream: str) -> float | None:
    """The fullest window a Claude Code session reports, as a percent. Only events with a utilization count."""
    fullest = None
    for line in stream.splitlines():
        if '"rate_limit_event"' not in line:
            continue
        try:
            info = json.loads(line).get("rate_limit_info") or {}
        except ValueError:
            continue
        for window in (info.get("unifiedWindows") or {}).values():
            value = window.get("utilization") if isinstance(window, dict) else None
            if isinstance(value, (int, float)):
                fullest = max(fullest or 0.0, value * 100)
    return fullest


def claude_usage(timeout: float = 300) -> float | None:
    """The account's usage, read from a one-word session, since Claude Code has no command for it. None if it could not be read."""
    cache = Path(__file__).resolve().parent.parent / ".cache"
    cache.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=cache) as cwd:
        command = ["claude", "-p", "--output-format", "stream-json", "--verbose", "--setting-sources", "", "--strict-mcp-config", "--no-session-persistence",
                   "--permission-mode", "dontAsk", "--tools", "", "--model", "claude-haiku-5-5", "--system-prompt", "Reply with OK."]
        try:
            # Ends when the answer arrives. Waiting for the process to exit takes minutes while a run is going.
            stdout, _, _ = ClaudeHost(cwd)._session(command, "OK?", timeout)
        except (OSError, subprocess.TimeoutExpired):
            return None
    return parse_claude_usage(stdout)


def over_claude_budget(max_percent: float, read=claude_usage) -> str | None:
    percent = read()
    return f"Claude usage is at {percent:.0f}%" if percent is not None and percent >= max_percent else None


def over_budget(max_percent: float) -> str | None:
    meters = usage_limits()
    if not meters:
        return None
    for window in ("rolling", "weekly", "monthly"):
        meter = meters.get(window) or {}
        if meter.get("limited") or (meter.get("percent") or 0) >= max_percent:
            return f"{window} usage is at {meter.get('percent')}%"
    return None
