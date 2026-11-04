#!/usr/bin/env python3
"""Offline tests of the loop's safety checks: the guard and the gate."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "loop"))
import gate  # noqa: E402
import guard  # noqa: E402

SKILL = ROOT / "skill" / "pr-review"


def metric(precision, recall):
    return {"grounded_precision": precision, "grounded_recall": recall}


def pr(all_p=0.9, all_r=0.5, high_p=None, high_r=None):
    out = {"overall": metric(all_p, all_r), "by_severity": {}}
    if high_r is not None:
        out["by_severity"]["high"] = metric(high_p if high_p is not None else 1.0, high_r)
    return out


class Objective(unittest.TestCase):
    def test_macro_f1_averages_prs_then_combines(self):
        prs = [pr(1.0, 1.0), pr(1.0, 0.0)]
        self.assertAlmostEqual(gate.macro_f1(prs, "all"), 2 * 1.0 * 0.5 / 1.5)

    def test_every_pr_counts_the_same_regardless_of_size(self):
        self.assertAlmostEqual(gate.macro_f1([pr(1.0, 1.0)] * 3 + [pr(1.0, 0.0)] * 3, "all"), gate.macro_f1([pr(1.0, 1.0), pr(1.0, 0.0)], "all"))

    def test_objective_weights_high_and_all(self):
        one = pr(1.0, 0.5, 1.0, 1.0)
        expected = 0.3 * gate.macro_f1([one], "high") + 0.7 * gate.macro_f1([one], "all")
        self.assertAlmostEqual(gate.objective([one]), expected)

    def test_objective_without_high_uses_all(self):
        one = pr(1.0, 0.5)
        self.assertAlmostEqual(gate.objective([one]), gate.macro_f1([one], "all"))

    def test_null_ratios_are_skipped(self):
        self.assertEqual(gate.macro_f1([{"overall": metric(None, None), "by_severity": {}}, pr(1.0, 1.0)], "all"), 1.0)


class Compare(unittest.TestCase):
    def test_consistent_gain_is_accepted(self):
        champion = {f"p{i}": pr(0.9, 0.40 + 0.01 * i, 1.0, 0.5) for i in range(20)}
        candidate = {f"p{i}": pr(0.9, 0.50 + 0.01 * i, 1.0, 0.6) for i in range(20)}
        result = gate.compare(champion, candidate)
        self.assertGreater(result["delta"], 0)
        self.assertEqual(result["p_better"], 1.0)
        self.assertTrue(gate.decide(result, min_delta=0.005, min_p=0.8)[0])

    def test_gain_from_one_lucky_pr_is_not_accepted(self):
        champion = {f"p{i}": pr(0.9, 0.5, 1.0, 0.5) for i in range(12)}
        candidate = dict(champion)
        candidate["p0"] = pr(0.9, 1.0, 1.0, 1.0)
        candidate["p1"] = pr(0.9, 0.45, 1.0, 0.45)
        candidate["p2"] = pr(0.9, 0.45, 1.0, 0.45)
        result = gate.compare(champion, candidate)
        ok, reason = gate.decide(result, min_delta=0.005, min_p=0.8)
        self.assertFalse(ok, reason)

    def test_identical_runs_show_no_gain(self):
        same = {f"p{i}": pr(0.9, 0.5, 1.0, 0.5) for i in range(5)}
        result = gate.compare(same, same)
        self.assertEqual(result["delta"], 0)
        self.assertFalse(gate.decide(result, min_delta=0.005, min_p=0.8)[0])

    def test_only_prs_in_both_runs_count(self):
        self.assertEqual(gate.compare({"a": pr(), "b": pr()}, {"b": pr(), "c": pr()})["prs"], 1)

    def test_no_overlap_is_rejected(self):
        result = gate.compare({"a": pr()}, {"b": pr()})
        self.assertFalse(gate.decide(result, min_delta=0.0, min_p=0.0)[0])

    def test_cost_and_time_limits_veto_a_gain(self):
        good = {"prs": 10, "delta": 0.05, "p_better": 0.95}
        self.assertTrue(gate.decide(good, min_delta=0.005, min_p=0.8, cost_ratio=1.2, mean_seconds=300)[0])
        self.assertFalse(gate.decide(good, min_delta=0.005, min_p=0.8, cost_ratio=2.0)[0])
        self.assertFalse(gate.decide(good, min_delta=0.005, min_p=0.8, mean_seconds=500)[0])


    def test_a_real_saving_with_no_loss_is_accepted(self):
        neutral = {"prs": 10, "delta": 0.0, "p_better": 0.0}
        ok, reason = gate.decide(neutral, min_delta=0.005, min_p=0.8, cost_ratio=0.7)
        self.assertTrue(ok, reason)

    def test_a_saving_that_loses_score_is_rejected(self):
        self.assertFalse(gate.decide({"prs": 10, "delta": -0.02, "p_better": 0.1}, min_delta=0.005, min_p=0.8, cost_ratio=0.5)[0])

    def test_a_small_saving_is_not_enough(self):
        self.assertFalse(gate.decide({"prs": 10, "delta": 0.0, "p_better": 0.0}, min_delta=0.005, min_p=0.8, cost_ratio=0.95)[0])

    def test_time_limit_vetoes_even_a_saving(self):
        self.assertFalse(gate.decide({"prs": 10, "delta": 0.0, "p_better": 0.0}, min_delta=0.005, min_p=0.8, cost_ratio=0.7, mean_seconds=400)[0])

    def test_training_screen(self):
        self.assertTrue(gate.worth_validating({"delta": 0.01}, 1.0))
        self.assertTrue(gate.worth_validating({"delta": 0.0}, 0.7))
        self.assertFalse(gate.worth_validating({"delta": 0.0}, 1.0))
        self.assertFalse(gate.worth_validating({"delta": -0.05}, 0.5))


class Sandbox(unittest.TestCase):
    """The improver edits a copy. Nothing it does may reach the real skill."""

    def setUp(self):
        import run_loop
        self.run_loop = run_loop
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name).resolve()
        self.skill = self.root / "outer" / "skill" / "pr-review"
        shutil.copytree(SKILL, self.skill, ignore=shutil.ignore_patterns("__pycache__"))
        subprocess.run(["git", "init", "-q", str(self.root / "outer")], check=True)
        self.original = run_loop.SKILL
        run_loop.SKILL = self.skill

    def tearDown(self):
        self.run_loop.SKILL = self.original
        self.dir.cleanup()

    def test_sandbox_is_its_own_git_root_even_inside_another_repository(self):
        exp = self.root / "outer" / "loop" / "work" / "e1"
        candidate = self.run_loop.create_sandbox(exp)
        top = subprocess.run(["git", "-C", str(exp), "rev-parse", "--show-toplevel"], capture_output=True, text=True).stdout.strip()
        self.assertEqual(Path(top).resolve(), exp.resolve())
        self.assertTrue((candidate / "SKILL.md").exists())
        self.assertTrue((self.run_loop.champion_snapshot(exp) / "config.json").exists())

    def test_snapshot_is_out_of_the_improvers_reach(self):
        exp = self.root / "e0"
        self.run_loop.create_sandbox(exp)
        self.assertFalse(str(self.run_loop.champion_snapshot(exp).resolve()).startswith(str(exp.resolve()) + "/"))

    def test_an_edit_inside_the_sandbox_is_not_a_tamper(self):
        exp = self.root / "e2"
        candidate = self.run_loop.create_sandbox(exp)
        (candidate / "config.json").write_text("{}")
        self.assertEqual(self.run_loop.restore_if_tampered(self.skill, self.run_loop.champion_snapshot(exp)), [])

    def test_an_edit_to_the_real_skill_is_detected_and_undone(self):
        exp = self.root / "e3"
        self.run_loop.create_sandbox(exp)
        before = (self.skill / "config.json").read_text()
        (self.skill / "config.json").write_text("{}")
        (self.skill / "references" / "lenses" / "sneaky.md").write_text("x")
        (self.skill / "references" / "verify.md").unlink()
        changed = self.run_loop.restore_if_tampered(self.skill, self.run_loop.champion_snapshot(exp))
        self.assertEqual(changed, ["config.json", "references/lenses/sneaky.md", "references/verify.md"])
        self.assertEqual((self.skill / "config.json").read_text(), before)
        self.assertFalse((self.skill / "references" / "lenses" / "sneaky.md").exists())
        self.assertTrue((self.skill / "references" / "verify.md").exists())


class Audit(unittest.TestCase):
    """Scores from a run that could see the answers mean nothing."""

    def audit(self, calls):
        sys.path.insert(0, str(ROOT / "bench"))
        from run import audit_calls
        return audit_calls(calls, ROOT / ".cache" / "prs" / "pr1" / "repo")

    def read(self, path, status="completed"):
        return {"tool": "read", "status": status, "path": str(path), "input": f"{{'path': '{path}'}}"}

    def test_ordinary_reads_pass(self):
        self.assertEqual(self.audit([self.read("src/a.py"), {"tool": "grep", "input": "{'pattern': 'foo'}"}]), [])

    def test_absolute_path_inside_its_own_checkout_passes(self):
        self.assertEqual(self.audit([self.read(f"{ROOT}/.cache/prs/pr1/repo/src/a.py")]), [])

    def test_a_repo_folder_named_like_a_benchmark_path_passes(self):
        self.assertEqual(self.audit([self.read("testdata/golden/a.json"), self.read("src/runs/b.py"), self.read("docs/ReviewBench.md")]), [])

    def test_this_project_is_out_of_bounds(self):
        self.assertTrue(self.audit([self.read(ROOT / "tests" / "fixtures" / "seeded_pr.py")]))

    def test_climbing_out_fails(self):
        self.assertTrue(self.audit([self.read("../../golden/a.json")]))

    def test_dotdot_in_a_search_pattern_is_not_climbing(self):
        self.assertEqual(self.audit([{"tool": "glob", "status": "completed", "path": None, "input": "{'pattern': '**/bin/../lib/**'}"},
                                     {"tool": "grep", "status": "completed", "path": "src", "input": "{'pattern': \"from '../utils'\", 'path': 'src'}"}]), [])

    def test_a_parent_of_the_checkout_is_outside(self):
        self.assertTrue(self.audit([self.read(ROOT / ".cache" / "prs")]))

    def test_a_refused_read_is_not_a_leak(self):
        self.assertEqual(self.audit([self.read(ROOT / "bench" / "common.py", status="error")]), [])

    def test_web_tool_that_ran_fails(self):
        self.assertTrue(self.audit([{"tool": "webfetch", "status": "completed", "input": "{'url': 'https://github.com/x/y/pull/1'}"}]))

    def test_shell_that_ran_fails(self):
        self.assertTrue(self.audit([{"tool": "bash", "status": "completed", "input": "{'command': 'curl x'}"}]))

    def test_a_refused_shell_attempt_is_not_a_violation(self):
        self.assertEqual(self.audit([{"tool": "bash", "status": "error", "input": "{'command': 'git status'}"}]), [])

    def test_grep_patterns_are_not_paths(self):
        self.assertEqual(self.audit([{"tool": "grep", "status": "completed", "input": "{'pattern': '/api/v1/users'}"}]), [])


class Guard(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.candidate = Path(self.dir.name) / "skill"
        shutil.copytree(SKILL, self.candidate, ignore=shutil.ignore_patterns("__pycache__"))
        self.vocabulary = guard.build_vocabulary({
            "acme/rocket-engine": [{"file": "src/thrustController.py", "message": "thrustController overflows the fuelGauge buffer on launch and the timeout is too short"}],
            "beta/app": [{"file": "a.py", "message": "the timeout is too short and retries repeat"}],
            "gamma/app": [{"file": "b.py", "message": "the timeout is too short"}],
            "delta/app": [{"file": "c.py", "message": "the timeout is too short"}],
        })

    def tearDown(self):
        self.dir.cleanup()

    def lens(self, name="correctness"):
        return self.candidate / "references" / "lenses" / f"{name}.md"

    def check(self):
        return guard.check(SKILL, self.candidate, self.vocabulary)

    def test_no_change_is_rejected(self):
        self.assertFalse(self.check()["ok"])

    def test_general_improvement_passes(self):
        self.lens().write_text(self.lens().read_text() + "\n- A timeout that is too short for the job it guards.\n")
        result = self.check()
        self.assertTrue(result["ok"], result["problems"])
        self.assertEqual(result["changed"], ["references/lenses/correctness.md"])

    def test_corpus_identifier_is_rejected(self):
        self.lens().write_text(self.lens().read_text() + "\n- Check that thrustController handles fuelGauge overflow.\n")
        result = self.check()
        self.assertFalse(result["ok"])
        self.assertIn("thrustcontroller", " ".join(result["problems"]))

    def test_repository_name_is_rejected(self):
        self.lens().write_text(self.lens().read_text() + "\n- In the rocket engine, check launch.\n")
        result = self.check()
        self.assertFalse(result["ok"])

    def test_words_already_in_the_prompt_are_fine(self):
        original = self.lens().read_text()
        vocabulary = self.vocabulary | {"branch"}
        self.assertIn("branch", original)
        self.lens().write_text(original + "\n- A branch that returns early.\n")
        self.assertTrue(guard.check(SKILL, self.candidate, vocabulary)["ok"])

    def test_scripts_cannot_be_changed(self):
        script = self.candidate / "scripts" / "review.py"
        script.write_text(script.read_text() + "\n# changed\n")
        result = self.check()
        self.assertFalse(result["ok"])
        self.assertIn("scripts/review.py", " ".join(result["problems"]))

    def test_skill_description_cannot_be_changed(self):
        path = self.candidate / "SKILL.md"
        path.write_text(path.read_text() + "\nmore\n")
        self.assertFalse(self.check()["ok"])

    def test_lost_placeholder_is_rejected(self):
        path = self.candidate / "references" / "discovery.md"
        path.write_text(path.read_text().replace("{{diff}}", ""))
        self.assertIn("{{diff}}", " ".join(self.check()["problems"]))

    def test_lens_length_limit(self):
        self.lens().write_text(self.lens().read_text() + (" word" * 600))
        self.assertTrue(any("limit" in p for p in self.check()["problems"]))

    def test_total_growth_limit(self):
        for name in ("correctness", "contracts", "config", "intent", "reliability", "security", "tests", "docs", "maintainability", "ui", "general"):
            self.lens(name).write_text(self.lens(name).read_text() + (" extra" * 200))
        self.assertTrue(any("grew" in p for p in self.check()["problems"]))

    def config(self):
        return json.loads((self.candidate / "config.json").read_text())

    def write_config(self, config):
        (self.candidate / "config.json").write_text(json.dumps(config, indent=2))

    def test_valid_config_change_passes(self):
        config = self.config()
        config["lenses"]["general"]["passes"] = 3
        self.write_config(config)
        self.assertTrue(self.check()["ok"])

    def test_out_of_bounds_budget_is_rejected(self):
        config = self.config()
        config["budget"]["deadline_base"] = 3000
        self.write_config(config)
        self.assertIn("deadline_base", " ".join(self.check()["problems"]))

    def test_profiles_are_fixed(self):
        config = self.config()
        config["profiles"]["exhaustive"]["max_findings"] = 500
        self.write_config(config)
        self.assertIn("profiles", " ".join(self.check()["problems"]))

    def test_lens_without_prompt_is_rejected(self):
        config = self.config()
        config["lenses"]["ghost"] = {"title": "x", "applies": ["source"], "priority": 1}
        self.write_config(config)
        self.assertIn("ghost", " ".join(self.check()["problems"]))

    def test_disabling_every_lens_is_rejected(self):
        config = self.config()
        for lens in config["lenses"].values():
            lens["enabled"] = False
        self.write_config(config)
        self.assertTrue(any("disabled" in p for p in self.check()["problems"]))

    def test_broken_json_is_rejected(self):
        (self.candidate / "config.json").write_text("{oops")
        self.assertIn("does not parse", " ".join(self.check()["problems"]))


class Vocabulary(unittest.TestCase):
    def test_background_keeps_ordinary_words_usable_with_a_small_training_set(self):
        training = {"x/y": [{"file": "a.py", "message": "zanzibarWidget returns the wrong entries"}]}
        background = {f"o/r{i}": [{"file": "b.py", "message": "this returns the wrong entries"}] for i in range(5)}
        vocabulary = guard.build_vocabulary(training, background=background)
        self.assertIn("zanzibarwidget", vocabulary)
        self.assertNotIn("returns", vocabulary)
        self.assertNotIn("entries", vocabulary)

    def test_background_words_are_never_blocked_when_unseen_by_the_improver(self):
        vocabulary = guard.build_vocabulary({"x/y": [{"file": "a.py", "message": "plain words"}]}, background={"p/q": [{"file": "b.py", "message": "secretWidgetName"}]})
        self.assertNotIn("secretwidgetname", vocabulary)

    def test_words_across_many_repositories_stay_generic(self):
        by_repo = {f"o/r{i}": [{"file": "a.py", "message": "the timeout handling here"}] for i in range(5)}
        by_repo["x/y"] = [{"file": "a.py", "message": "zanzibarWidget breaks"}]
        vocabulary = guard.build_vocabulary(by_repo)
        self.assertNotIn("timeout", vocabulary)
        self.assertIn("zanzibarwidget", vocabulary)


class PrecisionHolds(unittest.TestCase):
    good = {"prs": 10, "delta": 0.05, "p_better": 0.95}

    def test_grounded_precision_drop_rejected(self):
        ok, reason = gate.decide(self.good, min_delta=0.005, min_p=0.8, grounded_precision_delta=-0.03)
        self.assertFalse(ok)
        self.assertIn("grounded precision", reason)

    def test_augmented_precision_drop_rejected(self):
        ok, reason = gate.decide(self.good, min_delta=0.005, min_p=0.8, augmented_precision_delta=-0.05)
        self.assertFalse(ok)
        self.assertIn("augmented precision", reason)

    def test_small_noise_allowed(self):
        self.assertTrue(gate.decide(self.good, min_delta=0.005, min_p=0.8, grounded_precision_delta=-0.01, augmented_precision_delta=-0.02)[0])

    def test_missing_estimates_do_not_block(self):
        self.assertTrue(gate.decide(self.good, min_delta=0.005, min_p=0.8, grounded_precision_delta=None, augmented_precision_delta=None)[0])


class PrecisionCounts(unittest.TestCase):
    level = {"prs": 10, "delta": 0.0, "p_better": 0.5}

    def test_significant_gain_with_no_grounded_loss_is_accepted(self):
        ok, reason = gate.decide(self.level, min_delta=0.005, min_p=0.8, augmented_precision_delta=0.10, novel_rate_z=2.0)
        self.assertTrue(ok, reason)

    def test_gain_that_could_be_chance_is_not(self):
        self.assertFalse(gate.decide(self.level, min_delta=0.005, min_p=0.8, augmented_precision_delta=0.10, novel_rate_z=0.5)[0])

    def test_small_gain_is_not(self):
        self.assertFalse(gate.decide(self.level, min_delta=0.005, min_p=0.8, augmented_precision_delta=0.01, novel_rate_z=3.0)[0])

    def test_grounded_loss_still_rejects(self):
        self.assertFalse(gate.decide({"prs": 10, "delta": -0.02, "p_better": 0.1}, min_delta=0.005, min_p=0.8, augmented_precision_delta=0.10, novel_rate_z=3.0)[0])

    def test_too_expensive_still_rejects(self):
        self.assertFalse(gate.decide(self.level, min_delta=0.005, min_p=0.8, augmented_precision_delta=0.10, novel_rate_z=3.0, cost_ratio=2.0)[0])

    def test_too_slow_still_rejects(self):
        self.assertFalse(gate.decide(self.level, min_delta=0.005, min_p=0.8, augmented_precision_delta=0.10, novel_rate_z=3.0, mean_seconds=500)[0])

    def test_z_uses_both_samples(self):
        self.assertGreater(gate.rate_gain_z(36, 47, 43, 47), 1.28)
        self.assertLess(gate.rate_gain_z(36, 47, 36, 47), 0.01)
        self.assertEqual(gate.rate_gain_z(0, 0, 3, 3), 0.0)


class OutsideProposals(unittest.TestCase):
    """A change written by the session driving the loop may restructure the skill. It still may not memorize answers."""

    def _trees(self, tmp, edit):
        base = Path(tmp) / "a"
        (base / "references").mkdir(parents=True)
        (base / "scripts").mkdir()
        (base / "config.json").write_text("{}")
        (base / "references" / "discovery.md").write_text("Review it.\n")
        (base / "scripts" / "review.py").write_text("print(1)\n")
        other = Path(tmp) / "b"
        shutil.copytree(base, other)
        edit(other)
        return base, other

    def test_scripts_may_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            base, other = self._trees(tmp, lambda d: (d / "scripts" / "review.py").write_text("print(2)\n"))
            self.assertFalse(guard.check(base, other, set())["ok"])
            self.assertTrue(guard.check(base, other, set(), allow_mechanics=True)["ok"])

    def test_leak_still_caught_in_prompts_and_scripts(self):
        for name in ("references/discovery.md", "scripts/review.py"):
            with tempfile.TemporaryDirectory() as tmp:
                base, other = self._trees(tmp, lambda d: (d / name).write_text("the frobnicator bug\n"))
                result = guard.check(base, other, {"frobnicator"}, allow_mechanics=True)
                self.assertFalse(result["ok"], name)
                self.assertIn("frobnicator", result["problems"][0])


class CandidateSalt(unittest.TestCase):
    """A fresh candidate must not lose the champion's cached scores, and the next round must find the new champion's."""

    def _args(self, salt=""):
        import argparse
        return argparse.Namespace(model="claude-haiku-5-5", judge_model="claude-sonnet-5-5", salt=salt)

    def test_salt_changes_only_the_salted_name(self):
        import run_loop
        plain = run_loop.run_name(SKILL, "fixture_train", self._args())
        salted = run_loop.run_name(SKILL, "fixture_train", self._args(), "fresh")
        self.assertEqual(salted, f"{plain}-fresh")
        self.assertEqual(run_loop.run_name(SKILL, "fixture_train", self._args("x")), plain)


class ClaudeMeter(unittest.TestCase):
    """A long run has to stop before the account's limit, since past it every session fails, including the one driving the run."""

    def _llm(self):
        sys.path.insert(0, str(ROOT / "bench"))
        import llm
        return llm

    def test_reads_the_fullest_window(self):
        event = {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "unifiedWindows": {"seven_day": {"utilization": 0.41}, "five_hour": {"utilization": 0.1}}}}
        stream = "\n".join(['{"type":"system"}', json.dumps(event), "not json", '{"type":"result"}'])
        self.assertAlmostEqual(self._llm().parse_claude_usage(stream), 41.0)

    def test_nothing_to_read(self):
        self.assertIsNone(self._llm().parse_claude_usage('{"type":"result"}'))
        self.assertIsNone(self._llm().parse_claude_usage('{"type":"rate_limit_event","rate_limit_info":{"status":"allowed"}}'))

    def test_limit(self):
        llm = self._llm()
        self.assertIn("86", llm.over_claude_budget(85, read=lambda: 86.0))
        self.assertIsNone(llm.over_claude_budget(85, read=lambda: 84.0))

    def test_unreadable_meter_does_not_stop_a_run(self):
        self.assertIsNone(self._llm().over_claude_budget(85, read=lambda: None))


if __name__ == "__main__":
    unittest.main()
