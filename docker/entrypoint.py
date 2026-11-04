#!/usr/bin/env python3
"""ReviewBench adapter: read one pull request from the contract's mounts, write one findings file.

See AGENT_CONTRACT.md in ReviewBench. Three rules shape this file:

- One PR that never succeeds fails the whole run, so a partial review is still written and accepted.
- But an empty result caused by a broken key or blocked model must not count as a clean review. That
  exits non-zero, so the harness retries and the problem stays visible.
- `pr.head` and `pr.pr_number` must match the environment exactly.

Credentials stay in the environment, and nothing here prints them.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SKILL = Path(os.environ.get("PR_REVIEW_SKILL", "/agent/skill/pr-review"))
REQUIRED = ("RB_NWO", "RB_PR_NUMBER", "RB_BASE", "RB_HEAD", "RB_OUT")


def read_pr(env: dict) -> dict:
    try:
        return json.loads(Path(env.get("RB_PR_JSON", "/work/pr/pr.json")).read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def write_empty(env: dict, agent: str) -> None:
    Path(env["RB_OUT"]).write_text(json.dumps({
        "pr": {"repo": f"https://github.com/{env['RB_NWO']}", "pr_number": int(env["RB_PR_NUMBER"]), "base": env["RB_BASE"], "head": env["RB_HEAD"]},
        "agent": agent, "findings": []}))


def provider_config(env: dict, model: str) -> tuple[str, str | None]:
    """A registered model API URL becomes a custom provider, so one registration can point the agent at
    any OpenAI-compatible gateway. Built-in providers need nothing here."""
    base_url = env.get("RB_MODEL_BASE_URL", "").strip()
    if not base_url or model.split("/")[0] in ("opencode", "opencode-go", "deepseek"):
        return model, None
    model_id = model.split("/", 1)[-1]
    provider = {"providers": {"gateway": {"env": ["RB_MODEL_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY"],
                                          "package": "@opencode/ai/providers/openai-compatible",
                                          "settings": {"baseURL": base_url}, "models": {model_id: {"modelID": model_id}}}}}
    return f"gateway/{model_id}", json.dumps(provider)


def main() -> int:
    env = dict(os.environ)
    missing = [name for name in REQUIRED if not env.get(name)]
    if missing:
        print(f"agent: missing {', '.join(missing)}", file=sys.stderr)
        return 2
    agent = env.get("RB_AGENT", "pr-review")
    model = env.get("RB_CONFIG_MODEL", "deepseek/deepseek-flash")
    profile = env.get("RB_CONFIG_PROFILE", "exhaustive")
    deadline = env.get("RB_CONFIG_DEADLINE", "")
    concurrency = env.get("RB_CONFIG_CONCURRENCY", "24")
    model, provider = provider_config(env, model)
    pr = read_pr(env)
    body_file = Path("/tmp/pr-body.txt")
    body_file.write_text(pr.get("body") or "")
    # The benchmark reads these labels from the agent's output, to confirm each one had an effect.
    print(f"model={env.get('RB_CONFIG_MODEL', model)} profile={profile} deadline={deadline or 'auto'} concurrency={concurrency}", file=sys.stderr)

    repo = env.get("RB_REPO", "/work/repo")
    command = [sys.executable, str(SKILL / "scripts" / "review.py"), "run", "--repo", repo, "--base", env["RB_BASE"], "--head", env["RB_HEAD"],
               "--title", pr.get("title") or "", "--body-file", str(body_file), "--profile", profile, "--nwo", env["RB_NWO"],
               "--pr-number", env["RB_PR_NUMBER"], "--host", "opencode", "--model", model, "--concurrency", concurrency,
               "--agent", agent, "--out", env["RB_OUT"], "--quiet"]
    if env.get("RB_DIFF") and Path(env["RB_DIFF"]).exists():
        command += ["--diff-file", env["RB_DIFF"]]
    if deadline:
        command += ["--deadline", deadline]
    run_env = dict(env)
    if provider:
        run_env["PR_REVIEW_OPENCODE_PROVIDERS"] = provider
    done = subprocess.run(command, env=run_env)
    if done.returncode == 3:
        print("agent: the model host failed and no findings were produced; failing this attempt rather than reporting a clean review", file=sys.stderr)
        return 1
    if done.returncode != 0:
        print(f"agent: review exited with {done.returncode}", file=sys.stderr)
        if not Path(env["RB_OUT"]).exists():
            return 1
    # Final check against the contract: an invalid file would fail the PR anyway, with less explanation.
    try:
        data = json.loads(Path(env["RB_OUT"]).read_text())
        if data["pr"]["head"] != env["RB_HEAD"] or int(data["pr"]["pr_number"]) != int(env["RB_PR_NUMBER"]):
            raise ValueError("wrong head or PR number")
        for finding in data["findings"]:
            if finding["file"].startswith(("./", "/")):
                raise ValueError(f"bad path {finding['file']}")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"agent: findings file failed validation: {exc}", file=sys.stderr)
        return 1
    print(f"agent: {len(data['findings'])} finding(s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
