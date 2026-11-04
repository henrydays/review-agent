# Host mechanics (verified 2026-11-03)

How the skill runs in OpenCode 2.2.0, Claude Code 2.1.317 and Codex 0.166. Evidence type on each claim:

- **V**: verified on this machine, from `--help` or a harmless command.
- **M**: verified against a fake model on localhost.
- **D**: documentation or tagged source.
- **U**: unverified.

## Portable layout

```
.agents/skills/pr-review/{SKILL.md,references/,scripts/}     # OpenCode, Codex
.claude/skills/pr-review -> ../../.agents/skills/pr-review   # Claude Code
```

OpenCode and Codex load through symlinks, and OpenCode dedupes the ID (V). Claude's symlink support is D.
The name `pr-review` avoids colliding with Claude Code's built-in `/code-review`.

Spec (agentskills.io): required `name` (1-64 chars of `a-z0-9-`, equal to the directory) and `description`
(up to 1024 chars). Relative paths from the skill root. Avoid host-specific `${CLAUDE_SKILL_DIR}`, `$ARGUMENTS`,
`context: fork`.

Invocation differs: Claude `/pr-review`, Codex `$pr-review`, OpenCode by asking in words ("load the pr-review
skill"), because `@id` stays literal in `opencode run` (M).

## OpenCode 2.2.0

Docs for this version live at `opencode.ai/v2/docs/<page>/`; `opencode.ai/docs` describes V1.

- **Install**: npm package `@opencode/cli`, or `curl -fsSL https://opencode.ai/v2/install | bash`. The older `opencode-ai` package and `opencode.ai/install` are OpenCode 1 only (V).
- **Skills**: project `.opencode/skills`, `.claude/skills`, `.agents/skills` (V). Global `~/.config/opencode/skills`,
  `~/.agents/skills` (D). The ID comes from the path.
- **Subagents**: `.opencode/agents/<name>.md`, body is the system prompt. `mode: subagent`, `model`, `steps`,
  `permissions` as a rule list. Tool and permission action are `subagent`. Parallel calls run in parallel, no cap in
  source (D). Depth `experimental.subagent_depth`, default 1.
- **Headless**: pipe the prompt, because argv with spaces arrives wrapped in literal quotes (M). `--standalone` costs
  20-30s per run, so loops use a private `opencode serve` with `OPENCODE_PASSWORD` plus `opencode run --server` (V).
- **Working directory: the `PWD` environment variable of the `opencode run` client decides it** (V, tested with a sentinel file
  in each of three directories). The client's real cwd and the server's cwd are both ignored while `PWD` is set, and a subprocess
  inherits `PWD` from the parent shell whatever `cwd=` says. Set it explicitly, or the agent works in the wrong directory
  without any error. Consequently, one server can serve several directories.
- **Events**: `step_start`, `text` (`part.text`), `tool_use` (`part.tool`, `part.state`), `step_finish` (`part.tokens`,
  `part.cost`), `error`. The last `step_finish` is often missing, so read totals from `GET /api/session/<id>` (M).
- **Pass `--title`**, or OpenCode makes an extra call to a different model to title the session (M).
- **Isolation**: `OPENCODE_CONFIG_CONTENT` beats project files. `OPENCODE_CONFIG_PROJECT_DISABLE=1` drops repo config,
  agents and project skills (V). `OPENCODE_DISABLE_CLAUDE_CODE=1` drops `.claude` (V). Both are needed when the checkout
  is untrusted.
- **Leak pin**: `"websearch": false` plus policies `{"action":"tool.use","resource":"webfetch:*","effect":"deny"}`. An
  agent's own `allow` beats a top-level deny, but a policy still blocks it (M). Deny `execute` too, since Code Mode exposes
  `fetch` (D). `shell` keeps network access, so reviewers do not get it at all.
- **Auth in a container**: `OPENCODE_API_KEY` activates `opencode` and `opencode-go`; `DEEPSEEK_API_KEY` activates
  `deepseek/*` (V, dummy keys).
- **Variants**: Go flash `low|high|max`; Go pro `high|max`; direct `none|low|high|max`. Written `model#variant` (V).

## OpenCode Go

Base `https://opencode.ai/inference/go`. Model IDs `deepseek-v4.1-flash`, `deepseek-v4-pro`.

| $/1M in/out/cache | Off-peak | Peak | Go monthly | Requests/month (Go) |
|---|---|---|---:|---:|
| Flash | .15/.60/.003 | .30/1.20/.006 | $100 | about 216,670 |
| Pro | .66/1.98/.022 | 1.32/3.96/.044 | $15 | about 5,200 |

Windows: 5h 20%, week 50%, month 100%. Peak: 01-04 and 06-10 UTC, Mon-Fri. Meters:
`opencode api GET /api/experimental/usage/limits` returns `.data.go.{rolling,weekly,monthly}`.

**DeepSeek on Go needs the workspace's Settings > Privacy > Regions set to Global** (V: otherwise HTTP 400
`This Go model requires Global regions`). Go lists DeepSeek as not trained on, 0-day retention, under an agreement through
Nov 30 (D). Free models return 403 outside OpenCode, and on this account the free DeepSeek route hit its limit after a few
calls (V).

## DeepSeek API

`deepseek-flash` (V4.1-Flash-1017), `deepseek-v4-pro`. Base `https://api.deepseek.com`. 1M context, 384K output, concurrency
2,500 and 500. Same prices as above (D).

## Claude Code

Skills `.claude/skills/<name>/SKILL.md`. Agents `.claude/agents/*.md` with `name`, `description`, `tools`, `model`, `maxTurns`;
20 concurrent, 3 levels (D). Headless `claude -p --output-format json`, reading `.result` and `.total_cost_usd`.

## Codex 0.166

Skills `.agents/skills` (V). `AGENTS.md` loads (V). Custom prompts deprecated (D). Agents in `$CODEX_HOME/agents/*.toml`.
Headless `codex exec --json --ephemeral --sandbox read-only`. Events `item.completed` (`agent_message.text`) and
`turn.completed.usage`.

**ReviewBench's advice does not disable web search on 0.166**: `tools.web_search=false` still sends the tool, while
`web_search="disabled"` removes it (M). `features.apps=false` parses, but its effect on connectors needs a signed-in run (U).

## The official judge

ReviewBench's judge uses the pi SDK. Its bundled `opencode-go` provider lacks `deepseek-v4.1-flash`. A `.judge/models.json` with
a custom provider can point it at Go's OpenAI endpoint (U, not executed). This project's `bench/judge.py` goes through OpenCode
instead, so no credential has to be handled. The leaderboard judges with Claude Sonnet 5, so DeepSeek-judged scores are not
directly comparable.
