# pr-review

A pull request review agent, packaged as a skill and a prompt that work in any coding agent, and a loop that improves it against
[ReviewBench](https://github.com/review-bench/ReviewBench), running Haiku 5.5 reviewers through Claude Code and learning from Opus 5.5 and GPT-6 Astra.

## What's here

| Path | What it is |
|---|---|
| `skill/pr-review/` | The agent, as an [Agent Skill](https://agentskills.io). Lens prompts, verifier, and standard-library Python scripts |
| `prompt/REVIEW_PROMPT.md` | Single-file version for agents with no skill support. Generated from the skill |
| `integrations/` | Installer and read-only worker agents for OpenCode, Claude Code and Codex |
| `docker/` | Image that satisfies the ReviewBench agent contract |
| `bench/` | Corpus splits, isolated PR checkouts, benchmark runner, and a judge using ReviewBench's prompts and rules. Also the [maintainer corpus](docs/MAINTAINER_CORPUS.md): review by the maintainers of high-stakes projects such as Linux and Kubernetes |
| `loop/` | The self-improvement loop |
| `tests/` | Offline tests, and seeded-bug PRs for integration testing with a real model |
| `docs/` | [Where this is going](docs/ROADMAP.md), [architecture](docs/ARCHITECTURE.md), [the loop](docs/LOOP.md), [research on the leaderboard](docs/research/leaderboard-agents.md), [host mechanics](docs/research/hosts.md) |

## Use it to review code

Install into a project, or for your whole account:

```sh
integrations/install.sh /path/to/project
integrations/install.sh --global
```

Then ask:

| Agent | How |
|---|---|
| OpenCode | `load the pr-review skill and review this branch against main` |
| Claude Code | `/pr-review review this branch against main` |
| Codex | `$pr-review review this branch against main` |
| Anything else | Paste `prompt/REVIEW_PROMPT.md`, then say what to review |

The agent checks the change against its description, reviews it from several angles, tries to disprove each finding against the code, and reports what survives.
It reads code and doesn't change it. The diff and the repository are treated as untrusted.

Or run it unattended:

```sh
python3 skill/pr-review/scripts/review.py run --repo . --base origin/main \
  --host opencode --model opencode-go/deepseek-v4.1-flash --profile balanced
```

Pick a profile by what you want back: `strict` for blockers only, `balanced` for everyday review, `exhaustive` for everything not disproved.

## How it works

Five stages, with scripts doing what they can do exactly and models doing what needs judgment. [Details and evidence](docs/ARCHITECTURE.md).

1. **Prepare.** Parse and partition the diff, skip generated files, collect short leads.
2. **Discover.** One read-only task per lens per partition. Reports everything with concrete grounds.
3. **Gap.** A script finds hunks nobody commented on, and a model looks at only those.
4. **Dedupe.** One call per file: would fixing one of these fix the other?
5. **Verify.** Try to disprove each finding. A refutation has to quote real code, and a script checks the quote.

## Improve it

```sh
python3 bench/preflight.py                            # is the model reachable?
python3 bench/dataset.py fetch train_quick,val_quick  # get the PRs
python3 loop/run_loop.py baseline --select val_quick  # score the current version
python3 loop/run_loop.py evaluate --candidate DIR --hypothesis "..."  # test a change you wrote
python3 loop/run_loop.py status                       # what happened
```

Each change is tested on repositories its author never saw. [How it stays honest](docs/LOOP.md).

## Benchmark it

Score any run offline:

```sh
python3 bench/run.py --select val --name my-run --host claude --model claude-haiku-5-5
```

This uses ReviewBench's own matcher and classifier prompts and its scoring rules, with Claude Sonnet 5.5 as the judge. **These numbers are for comparing versions of this agent. They
are not leaderboard scores**, since the leaderboard judges with Claude Sonnet 5 on all 219 PRs, three times. [How the judge was chosen](docs/JUDGES.md).

[Full runs so far, and how they compare with OhMyBug](docs/RESULTS.md). The best one so far, on an unaccepted candidate, doesn't match it yet: grounded P 82.7, R 57.1, and estimated augmented P 86.0, R 82.5, judged by Sonnet 5.5.

To appear on the leaderboard, build `docker/Dockerfile`, push the image, and register it at [review-bench.ai/submit](https://review-bench.ai/submit).
`docker/manifest.example.json` shows the shape of the registration.

## Tests

```sh
python3 tests/test_scoring.py                  # scoring rules, from hand-derived cases
python3 tests/test_pipeline.py                 # the pipeline, with a scripted model
python3 tests/test_loop.py                     # the guard and the gate
python3 tests/test_maintainers.py              # the maintainer corpus: filters, snapshots, splits
python3 tests/seeded_eval.py --host opencode   # real model, seeded bugs, injection canaries
python3 tests/optional_scoring_parity.py       # against ReviewBench's TypeScript; runs its npm dependencies
```

## Requirements

`python3` 3.10+ and `git`. For headless use, one of `opencode` 2.x, `claude` or `codex`.

The loop's defaults use Haiku 5.5 through the Claude Code CLI to review and Sonnet 5.5 to judge. The judge can be changed with `RA_JUDGE_MODEL`, but only to one in `APPROVED_JUDGES`.
