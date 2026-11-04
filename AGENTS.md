# pr-review

A PR review agent (`skill/pr-review/`) and a loop that improves it against ReviewBench (`loop/`). Start with `README.md`. The direction is in `docs/ROADMAP.md`. Design reasons are in
`docs/ARCHITECTURE.md`, and how the loop works in `docs/LOOP.md`.

## Commands

```sh
python3 tests/test_scoring.py && python3 tests/test_pipeline.py && python3 tests/test_loop.py && python3 tests/test_maintainers.py   # offline, no model
python3 bench/preflight.py                                                                      # model reachable? reviewer isolated?
python3 tests/seeded_eval.py                                                                    # real model on fixtures written for this project
python3 bench/teacher.py run --who opus --select train_quick --name teach-opus                  # a stronger reviewer, to learn from
python3 loop/run_loop.py evaluate --candidate DIR --hypothesis "..."                            # test a change you wrote
python3 prompt/build_prompt.py                                                                  # after changing the skill's prompts
```

## Rules

- **Never read golden findings into anything that ships.** Nothing under `skill/` or `prompt/` may contain a repository name, path or identifier from the benchmark. The golden
  set stays in `.cache/`, which is gitignored.
- **Design from `train` only.** Do not read golden findings from `val` or `holdout` when deciding what to change. Run `holdout` only at milestones.
- **Reviewers stay read-only and off the web.** No shell, no web tools, no edits, working directory pinned to the checkout. A reviewer that can reach the answers makes every score meaningless.
- **Judge with Claude Sonnet 5.5 or better, never Sonnet 5.** `bench/judge.py` refuses anything outside `APPROVED_JUDGES`, and that includes DeepSeek. The leaderboard judges with Sonnet 5, so our numbers are not leaderboard scores, and comparing them with a leaderboard row carries an offset nobody has measured. Say which judge produced a number whenever you report one.
- **The maintainer corpus is not ReviewBench.** It is a second dataset (`docs/MAINTAINER_CORPUS.md`), labeled by Claude Sonnet 5.5 through Claude Code. Name the corpus, reviewer and judge with any number from it, and never gate on it alone: its grounded precision stays near 1 whatever the agent does. `maint_train` is the only part of it to read when deciding what to change.
- **Do not edit `skill/` while the loop is running.** It compares the skill against a snapshot, and treats a difference as the improver escaping its sandbox.
- **Scripts in the skill use the standard library only**, since they ship to other people's machines.
