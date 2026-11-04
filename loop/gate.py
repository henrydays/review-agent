"""Decide whether a candidate beats the champion, on evidence that accounts for noise.

The same prompt can score differently twice, and a score on a few dozen PRs moves by several points
from which PRs happen to be in the sample. So the comparison is paired by PR and resampled: only a gain
that shows up across most resamples counts. Pure functions, no model involved.
"""
from __future__ import annotations

import random

# The row being chased is the unfiltered one, grounded F1 over all severities. High severity keeps some weight, since it is
# what a reader of a review cares about most and the site's default view, but its few findings are too noisy to lead.
# Macro-averaged, as ReviewBench's own aggregate is, so every PR counts the same.
WEIGHTS = {"all": 0.7, "high": 0.3}


def _ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


def _f1(precision: float | None, recall: float | None) -> float | None:
    if precision is None or recall is None:
        return None
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def macro_f1(per_pr: list[dict], stratum: str) -> float | None:
    """F1 of the macro-averaged grounded precision and recall over the PRs given."""
    def metric(pr: dict) -> dict | None:
        return pr["overall"] if stratum == "all" else pr["by_severity"].get(stratum)

    sets = [m for m in (metric(pr) for pr in per_pr) if m]
    precision = _mean([m["grounded_precision"] for m in sets if m["grounded_precision"] is not None])
    recall = _mean([m["grounded_recall"] for m in sets if m["grounded_recall"] is not None])
    return _f1(precision, recall)


def objective(per_pr: list[dict]) -> float:
    """Missing strata are left out and the rest renormalized, so a small sample with no high-severity
    finding at all still gets a score."""
    total = weight_sum = 0.0
    for stratum, weight in WEIGHTS.items():
        value = macro_f1(per_pr, stratum)
        if value is not None:
            total += weight * value
            weight_sum += weight
    return total / weight_sum if weight_sum else 0.0


def compare(champion: dict[str, dict], candidate: dict[str, dict], *, resamples: int = 2000, seed: int = 7) -> dict:
    """Paired bootstrap of the objective over the PRs both runs scored."""
    keys = sorted(set(champion) & set(candidate))
    if not keys:
        return {"prs": 0, "champion": 0.0, "candidate": 0.0, "delta": 0.0, "p_better": 0.0, "low": 0.0, "high": 0.0}
    base = objective([champion[k] for k in keys])
    new = objective([candidate[k] for k in keys])
    rng = random.Random(seed)
    deltas: list[float] = []
    for _ in range(resamples):
        sample = [rng.choice(keys) for _ in keys]
        deltas.append(objective([candidate[k] for k in sample]) - objective([champion[k] for k in sample]))
    deltas.sort()
    return {
        "prs": len(keys),
        "champion": round(base, 4),
        "candidate": round(new, 4),
        "delta": round(new - base, 4),
        "p_better": round(sum(d > 0 for d in deltas) / resamples, 3),
        "low": round(deltas[int(0.05 * resamples)], 4),
        "high": round(deltas[int(0.95 * resamples)], 4),
    }


# A change that costs much less and loses essentially nothing is worth keeping: the budget it frees
# pays for the next idea. Removing a lens that adds nothing is the typical example.
CHEAPER_RATIO = 0.85
CHEAPER_TOLERANCE = 0.003

# The maintainer corpus's golden findings are human comments, few of them labeled false, and grounded metrics ignore
# unmatched findings. So its grounded precision sits near 1 whatever the agent does, its objective only measures
# recall, and reporting more always looks better. These limits make that gain impossible to buy with volume. The
# ratio is against the first champion measured on these PRs, since against the current one small steps would add up.
MAX_VOLUME_RATIO = 1.25
REVIEWBENCH_TOLERANCE = 0.003


def comments_per_pr(results: dict[str, dict], keys: list[str]) -> float | None:
    counts = [results[k]["overall"]["candidate_count"] for k in keys if k in results]
    return sum(counts) / len(counts) if counts else None


def training_problem(train_keys: list[str], val_keys: list[str], held_out: list[str]) -> str | None:
    """The improver reads what it trains on, so a training set that overlaps validation or holdout shows it the answers."""
    overlap = sorted(set(train_keys) & (set(val_keys) | set(held_out)))
    return f"the training PRs include {len(overlap)} PRs from validation or holdout, starting with {overlap[0]}" if overlap else None


def validation_problem(keys: list[str], maintainer_keys: list[str]) -> str | None:
    """Why this set of PRs cannot decide what ships, if it cannot."""
    if keys and set(keys) <= set(maintainer_keys):
        return ("validating on the maintainer corpus alone rewards reporting more, since its grounded precision is close to 1 whatever "
                "the agent does. Add ReviewBench PRs, for example --val val_quick,maint_val_quick")
    return None


def is_cheaper_without_loss(comparison: dict, cost_ratio: float | None) -> bool:
    return cost_ratio is not None and cost_ratio <= CHEAPER_RATIO and comparison["delta"] >= -CHEAPER_TOLERANCE


def worth_validating(train_comparison: dict, cost_ratio: float | None, augmented_delta: float | None = None, rate_z: float | None = None) -> bool:
    """Spend on the validation PRs only if the training PRs show a gain, a saving with no loss, or better precision with no loss."""
    return train_comparison["delta"] > 0 or is_cheaper_without_loss(train_comparison, cost_ratio) or is_more_precise_without_loss(train_comparison, augmented_delta, rate_z)


# Volume buys grounded recall, and grounded precision barely notices, so F1 alone would let a change trade quality for it. These
# hold precision in place: what is reported has to stay true, on the golden set and on what the classifier judges.
MAX_PRECISION_DROP = 0.02
MAX_AUGMENTED_DROP = 0.03


# A gain in what the classifier accepts is worth keeping if the grounded score holds, since that gain is the point of the work. Samples are small,
# so it also has to be unlikely to be chance.
MIN_AUGMENTED_GAIN = 0.03
MIN_RATE_Z = 1.28


def rate_gain_z(before_true: int, before_n: int, after_true: int, after_n: int) -> float:
    """One-sided two-proportion z. 1.28 is about 90% confidence that the second rate is higher."""
    if not before_n or not after_n:
        return 0.0
    pooled = (before_true + after_true) / (before_n + after_n)
    spread = (pooled * (1 - pooled) * (1 / before_n + 1 / after_n)) ** 0.5
    return (after_true / after_n - before_true / before_n) / spread if spread else 0.0


def is_more_precise_without_loss(comparison: dict, augmented_delta: float | None, rate_z: float | None) -> bool:
    return (augmented_delta is not None and rate_z is not None and augmented_delta >= MIN_AUGMENTED_GAIN and rate_z >= MIN_RATE_Z
            and comparison["delta"] >= -CHEAPER_TOLERANCE)


def macro_precision(per_pr: list[dict]) -> float | None:
    return _mean([pr["overall"]["grounded_precision"] for pr in per_pr if pr["overall"]["grounded_precision"] is not None])


def precision_delta(champion: dict[str, dict], candidate: dict[str, dict]) -> float | None:
    keys = sorted(set(champion) & set(candidate))
    before, after = macro_precision([champion[k] for k in keys]), macro_precision([candidate[k] for k in keys])
    return after - before if before is not None and after is not None else None


def decide(comparison: dict, *, min_delta: float, min_p: float, cost_ratio: float | None = None, max_cost_ratio: float = 1.4,
           mean_seconds: float | None = None, max_mean_seconds: float = 330, volume_ratio: float | None = None, max_volume_ratio: float = MAX_VOLUME_RATIO,
           reviewbench_delta: float | None = None, grounded_precision_delta: float | None = None, augmented_precision_delta: float | None = None,
           novel_rate_z: float | None = None) -> tuple[bool, str]:
    """Accept only a real gain, or a real saving, that stays within the benchmark's time limit and does not lower precision.
    `volume_ratio` and `reviewbench_delta` apply when the PRs compared include the maintainer corpus."""
    if comparison["prs"] == 0:
        return False, "no PRs in common"
    if grounded_precision_delta is not None and grounded_precision_delta < -MAX_PRECISION_DROP:
        return False, f"grounded precision falls by {-grounded_precision_delta:.3f}, limit {MAX_PRECISION_DROP}"
    if augmented_precision_delta is not None and augmented_precision_delta < -MAX_AUGMENTED_DROP:
        return False, f"estimated augmented precision falls by {-augmented_precision_delta:.3f}, limit {MAX_AUGMENTED_DROP}"
    if mean_seconds is not None and mean_seconds > max_mean_seconds:
        return False, f"takes {mean_seconds:.0f}s per PR on average, limit {max_mean_seconds}s"
    if volume_ratio is not None and volume_ratio > max_volume_ratio:
        return False, f"reports {volume_ratio:.2f}x the comments of the first version measured on maintainer PRs, limit {max_volume_ratio}x, where more comments cost nothing in the score"
    if reviewbench_delta is not None and reviewbench_delta < -REVIEWBENCH_TOLERANCE:
        return False, f"loses {reviewbench_delta:+.4f} on ReviewBench's PRs, whatever it gains on the maintainer corpus"
    if is_more_precise_without_loss(comparison, augmented_precision_delta, novel_rate_z) and (cost_ratio is None or cost_ratio <= max_cost_ratio):
        return True, f"estimated augmented precision {augmented_precision_delta:+.3f} (z {novel_rate_z:.2f}) with no grounded loss (delta {comparison['delta']:+.4f})"
    if is_cheaper_without_loss(comparison, cost_ratio) and comparison["delta"] < min_delta:
        return True, f"costs x{cost_ratio:.2f} with no meaningful loss (delta {comparison['delta']:+.4f})"
    if comparison["delta"] < min_delta:
        return False, f"gain {comparison['delta']:+.4f} is below the {min_delta} minimum"
    if comparison["p_better"] < min_p:
        return False, f"only {comparison['p_better']:.0%} of resamples improve, needs {min_p:.0%}"
    if cost_ratio is not None and cost_ratio > max_cost_ratio:
        return False, f"costs {cost_ratio:.2f}x the champion, limit {max_cost_ratio}x"
    return True, f"gain {comparison['delta']:+.4f} in {comparison['p_better']:.0%} of resamples"
