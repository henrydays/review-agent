#!/usr/bin/env python3
"""Pins the scoring port to the rules in ReviewBench docs/METHODOLOGY.md sections 6.2.4,
7.1-7.3 and 7.6. Expected values here are worked out by hand from those rules."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bench"))
from judge import aggregate, build_matcher_prompt, parse_matcher_response, resolve_for_scoring, score_pr  # noqa: E402


def gold(tp_fp="tp", severity="low", category="correctness"):
    return {"file": "a.py", "start_line": 1, "end_line": 1, "message": "m", "tp_fp": tp_fp, "severity": severity, "category": category}


def corr(*matches):
    return [{"candidate_index": i, "matched_golden_indices": list(m)} for i, m in enumerate(matches)]


class ClaimRules(unittest.TestCase):
    def test_one_candidate_covers_two_goldens_but_claims_one(self):
        # Section 6.2.4: "if one candidate matches two golden TPs, both are covered for
        # recall, but the candidate contributes only one matched TP to precision".
        golden = [gold(), gold()]
        resolved = resolve_for_scoring(corr([0, 1]), 2)
        self.assertEqual(resolved["covered"], {0, 1})
        self.assertEqual(resolved["matched_to_golden"], {0: 0})
        m = score_pr(golden, 1, resolved, {})["overall"]
        self.assertEqual((m["golden_covered_count"], m["matched_count"], m["matched_tp_count"]), (2, 1, 1))

    def test_second_candidate_claims_the_next_unclaimed_golden(self):
        resolved = resolve_for_scoring(corr([0, 1], [0, 1], [0, 1]), 2)
        self.assertEqual(resolved["matched_to_golden"], {0: 0, 1: 1})
        self.assertEqual(resolved["unmatched"], {2})

    def test_duplicate_of_a_claimed_golden_is_unmatched_not_a_second_tp(self):
        golden = [gold()]
        resolved = resolve_for_scoring(corr([0], [0]), 1)
        self.assertEqual(resolved["matched_to_golden"], {0: 0})
        self.assertEqual(resolved["unmatched"], {1})
        m = score_pr(golden, 2, resolved, {})["overall"]
        # Grounded precision ignores the unmatched duplicate.
        self.assertEqual(m["grounded_precision"], 1.0)
        # Unclassified duplicate counts against augmented precision.
        self.assertEqual(m["augmented_precision"], 0.5)

    def test_claims_follow_golden_order_not_match_order(self):
        resolved = resolve_for_scoring(corr([2, 0]), 3)
        self.assertEqual(resolved["matched_to_golden"], {0: 0})

    def test_out_of_range_golden_indices_are_ignored(self):
        resolved = resolve_for_scoring(corr([5, -1, 0]), 1)
        self.assertEqual(resolved["covered"], {0})


class GroundedMetrics(unittest.TestCase):
    def test_unmatched_findings_do_not_affect_grounded_metrics(self):
        golden = [gold(), gold()]
        few = score_pr(golden, 1, resolve_for_scoring(corr([0]), 2), {})["overall"]
        many = score_pr(golden, 50, resolve_for_scoring(corr([0], *[[] for _ in range(49)]), 2), {})["overall"]
        self.assertEqual(few["grounded_precision"], many["grounded_precision"])
        self.assertEqual(few["grounded_recall"], many["grounded_recall"])
        self.assertLess(many["augmented_precision"], few["augmented_precision"])

    def test_matching_a_golden_fp_lowers_precision_only(self):
        golden = [gold(), gold("fp")]
        m = score_pr(golden, 2, resolve_for_scoring(corr([0], [1]), 2), {})["overall"]
        self.assertEqual(m["grounded_precision"], 0.5)
        self.assertEqual(m["grounded_recall"], 1.0)

    def test_no_golden_tps_gives_null_recall(self):
        m = score_pr([gold("fp")], 0, resolve_for_scoring([], 1), {})["overall"]
        self.assertIsNone(m["grounded_recall"])
        self.assertIsNone(m["grounded_precision"])

    def test_augmented_recall_counts_novel_tps_on_both_sides(self):
        # Section 7.2's worked example: 10 golden TPs, 5 covered, 2 novel TPs gives 7/12.
        golden = [gold() for _ in range(10)]
        matches = [[i] for i in range(5)] + [[], []]
        classes = {5: {"tp_fp": "tp", "severity": "low", "category": "correctness"},
                   6: {"tp_fp": "tp", "severity": "low", "category": "correctness"}}
        m = score_pr(golden, 7, resolve_for_scoring(corr(*matches), 10), classes)["overall"]
        self.assertAlmostEqual(m["augmented_recall"], 7 / 12)


class Stratification(unittest.TestCase):
    def setUp(self):
        self.golden = [gold(severity="high"), gold(severity="high"), gold("fp", severity="high"), gold(severity="low")]
        self.resolved = resolve_for_scoring(corr([0], [2], [3], []), 4)
        self.score = score_pr(self.golden, 4, self.resolved, {3: {"tp_fp": "tp", "severity": "high", "category": "testing"}})

    def test_high_stratum_precision_uses_only_candidates_matched_to_high_goldens(self):
        high = self.score["by_severity"]["high"]
        self.assertEqual(high["matched_count"], 2)
        self.assertEqual(high["matched_tp_count"], 1)
        self.assertEqual(high["grounded_precision"], 0.5)

    def test_high_stratum_recall_ignores_lower_severities(self):
        high = self.score["by_severity"]["high"]
        self.assertEqual(high["golden_tp_count"], 2)
        self.assertEqual(high["golden_covered_count"], 1)

    def test_unmatched_are_bucketed_by_their_classification(self):
        self.assertEqual(self.score["by_severity"]["high"]["unmatched_tp_count"], 1)
        self.assertEqual(self.score["by_severity"]["low"]["unmatched_tp_count"], 0)
        self.assertIn("testing", self.score["by_category"])


class Aggregation(unittest.TestCase):
    def test_micro_pools_counts_and_macro_averages_prs(self):
        one = score_pr([gold()], 1, resolve_for_scoring(corr([0]), 1), {})          # recall 1/1
        two = score_pr([gold() for _ in range(9)], 0, resolve_for_scoring([], 9), {})  # recall 0/9
        agg = aggregate({"a": one, "b": two})
        self.assertAlmostEqual(agg["micro"]["overall"]["grounded_recall"], 1 / 10)
        self.assertAlmostEqual(agg["macro"]["overall"]["grounded_recall"], 1 / 2)

    def test_macro_skips_prs_with_null_ratios(self):
        one = score_pr([gold()], 1, resolve_for_scoring(corr([0]), 1), {})
        none = score_pr([gold("fp")], 0, resolve_for_scoring([], 1), {})
        self.assertEqual(aggregate({"a": one, "b": none})["macro"]["overall"]["grounded_recall"], 1.0)


class MatcherProtocol(unittest.TestCase):
    def test_rendering_escapes_untrusted_text(self):
        f = {"file": 'a"b.py', "start_line": 1, "end_line": 2, "message": "x < y & z"}
        prompt = build_matcher_prompt([f], [f])
        self.assertIn('file="a&quot;b.py"', prompt)
        self.assertIn("x &lt; y &amp; z", prompt)
        self.assertIn('lines="1-2"', prompt)

    def test_response_must_cover_every_candidate(self):
        with self.assertRaises(ValueError):
            parse_matcher_response('{"candidateFindings":[{"index":0,"matchedGoldenIndices":[]}]}', 2, 1)

    def test_response_rejects_out_of_range_golden(self):
        with self.assertRaises(ValueError):
            parse_matcher_response('{"candidateFindings":[{"index":0,"matchedGoldenIndices":[3]}]}', 1, 1)

    def test_response_tolerates_surrounding_prose(self):
        text = 'Here it is: {"reasoning":"r","candidateFindings":[{"index":0,"matchedGoldenIndices":[0]}]} done'
        self.assertEqual(parse_matcher_response(text, 1, 1), [[0]])


class AugmentedEstimate(unittest.TestCase):
    def _metrics(self, candidates, matched_tp, covered, golden_tp):
        unmatched = candidates - matched_tp
        return {"overall": {"candidate_count": candidates, "matched_tp_count": matched_tp, "golden_covered_count": covered, "golden_tp_count": golden_tp, "unmatched_count": unmatched}}

    def test_pooled_rate_applied_per_pr(self):
        from judge import estimate_augmented
        results = {"a": self._metrics(10, 2, 2, 4), "b": self._metrics(5, 1, 1, 2)}
        sampled = {"a": {0: {"tp_fp": "tp"}, 1: {"tp_fp": "fp"}}, "b": {0: {"tp_fp": "tp"}, 1: {"tp_fp": "tp"}}}
        got = estimate_augmented(results, sampled)
        self.assertEqual(got["sampled"], 4)
        self.assertAlmostEqual(got["novel_tp_rate"], 0.75)
        # a: novel 6.0, precision 8/10, recall 8/10. b: novel 3.0, precision 4/5, recall 4/5.
        self.assertAlmostEqual(got["augmented_precision"], 0.8)
        self.assertAlmostEqual(got["augmented_recall"], 0.8)
        self.assertAlmostEqual(got["novel_yield"], 0.6)

    def test_no_sample_no_estimate(self):
        from judge import estimate_augmented
        self.assertIsNone(estimate_augmented({"a": self._metrics(1, 0, 0, 1)}, {"a": {}}))

    def test_sample_is_stable_when_other_findings_change(self):
        from judge import sample_order
        findings = [{"file": "a", "message": f"m{i}"} for i in range(40)]
        first = sample_order(findings, set(range(40)))[:5]
        fewer = sample_order(findings, set(range(40)) - {i for i in range(40) if i not in first and i % 2})[:5]
        self.assertEqual(first, fewer)


class JudgeRule(unittest.TestCase):
    """Sonnet 5.5 or better, and never Sonnet 5, whose name is one suffix away from the approved one."""

    def test_refuses_weaker_judges(self):
        from llm import require_judge
        for spec in ("claude-sonnet-5", "claude-sonnet-5#max", "claude-haiku-5-5", "opencode-go/deepseek-v4.1-flash"):
            with self.assertRaises(SystemExit, msg=spec):
                require_judge(spec)

    def test_accepts_approved_judges(self):
        from llm import DEFAULT_JUDGE_MODEL, require_judge
        for spec in ("claude-sonnet-5-5", "claude-opus-5-5#max", "gpt-6-astra#max", DEFAULT_JUDGE_MODEL):
            require_judge(spec)

    def test_hosts_refuse_too(self):
        from judge import classifier_host, matcher_host
        with self.assertRaises(SystemExit):
            matcher_host("claude-sonnet-5", "system")
        with self.assertRaises(SystemExit):
            classifier_host("claude-sonnet-5", "system", Path("."))


if __name__ == "__main__":
    unittest.main()
