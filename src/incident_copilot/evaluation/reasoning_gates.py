"""Conservative pilot gates; missing labels or incomparable runs never pass."""

import math
import random
import statistics

from incident_copilot.evaluation.diagnosis_review import diagnosis_success, evaluate_reviews


def _percentile(values, quantile):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * quantile) - 1)] if ordered else None


def compare_pair(baseline, candidate, *, baseline_mode, candidate_mode, minimum_gain=0.05):
    left = {r.incident_id: r for r in baseline}
    right = {r.incident_id: r for r in candidate}
    if len(left) != len(baseline) or len(right) != len(candidate):
        raise ValueError("Duplicate incident IDs in comparison dataset.")
    matched = set(left) & set(right)
    comparable = bool(matched) and set(left) == set(right)
    pairs = [(left[key], right[key]) for key in sorted(matched)]
    for a, b in pairs:
        comparable &= bool(
            a.repository_revision == b.repository_revision
            and a.model_version == b.model_version
            and a.evidence_fingerprint
            and a.evidence_fingerprint == b.evidence_fingerprint
            and a.inference_mode != "unknown"
            and a.inference_mode == b.inference_mode
            and a.reasoning_mode == baseline_mode
            and b.reasoning_mode == candidate_mode
            and a.evidence_sufficient is not None
            and a.evidence_sufficient == b.evidence_sufficient
        )
    comparable &= len({r.model_version for r in baseline + candidate}) == 1
    comparable &= (
        len({r.prompt_version for r in baseline}) == 1
        and len({r.prompt_version for r in candidate}) == 1
    )
    comparable &= len({r.inference_mode for r in baseline + candidate}) == 1
    eligible = all(
        r.dataset_split == "held_out"
        and r.case_origin == "verified"
        and r.independently_adjudicated
        for r in baseline + candidate
    )
    sufficient = [(a, b) for a, b in pairs if a.evidence_sufficient is True]
    judged = [
        (a, b)
        for a, b in sufficient
        if diagnosis_success(a) is not None and diagnosis_success(b) is not None
    ]
    gains = [int(diagnosis_success(b)) - int(diagnosis_success(a)) for a, b in judged]
    gain = statistics.mean(gains) if gains else None
    interval = None
    if gains:
        rng = random.Random(0)
        estimates = [statistics.mean(rng.choices(gains, k=len(gains))) for _ in range(1000)]
        interval = [_percentile(estimates, 0.025), _percentile(estimates, 0.975)]
    lm, rm = evaluate_reviews(baseline), evaluate_reviews(candidate)
    complete_claims = all(
        r.unsupported_claims is not None and r.claim_count is not None for r in baseline + candidate
    )

    def no_worse(metric):
        a, b = lm[metric]["value"], rm[metric]["value"]
        return a is not None and b is not None and b <= a

    latency = all(r.latency_seconds is not None for r in baseline + candidate) and bool(pairs)
    mean_ratio = p95_ratio = None
    if latency:
        a, b = [r.latency_seconds for r in baseline], [r.latency_seconds for r in candidate]
        mean_ratio = statistics.mean(b) / statistics.mean(a) if statistics.mean(a) > 0 else None
        p95_ratio = (
            _percentile(b, 0.95) / _percentile(a, 0.95) if _percentile(a, 0.95) > 0 else None
        )
    checks = {
        "comparable_inputs": bool(comparable),
        "independent_verified_held_out": bool(eligible and pairs),
        "minimum_50_incidents": len(pairs) >= 50,
        "minimum_15_insufficient": sum(a.evidence_sufficient is False for a, _ in pairs) >= 15,
        "complete_assessments": all(r.diagnosis_status != "unknown" for r in baseline + candidate),
        "review_effect_labeled": candidate_mode != "structured_review"
        or (
            any(r.review_status == "completed" for r in candidate)
            and all(
                r.draft_causal_correct is not None
                for r in candidate
                if r.review_status == "completed"
            )
        ),
        "complete_causal_labels": len(judged) == len(sufficient) and bool(sufficient),
        "causal_success_gain": gain is not None
        and (gain >= minimum_gain - 1e-12 if minimum_gain else gain > 0),
        "unsupported_claims_no_worse": complete_claims and no_worse("unsupported_claim_rate"),
        "false_causation_no_worse": no_worse("false_causation_rate"),
        "mean_latency_within_1_5x": mean_ratio is not None and mean_ratio <= 1.5,
        "p95_latency_within_2x": p95_ratio is not None and p95_ratio <= 2,
    }
    return {
        "passes": all(checks.values()),
        "checks": checks,
        "matched_incidents": len(pairs),
        "excluded_unmatched": len(set(left) ^ set(right)),
        "causal_success_gain": gain,
        "paired_bootstrap_95_interval": interval,
        "mean_latency_ratio": mean_ratio,
        "p95_latency_ratio": p95_ratio,
        "baseline_metrics": lm,
        "candidate_metrics": rm,
    }


def compare_policies(baseline, structured, review=None):
    result = {
        "structured": compare_pair(
            baseline, structured, baseline_mode="baseline", candidate_mode="structured"
        ),
        "recommended_mode": "baseline",
        "settings_changed": False,
    }
    if result["structured"]["passes"]:
        result["recommended_mode"] = "structured"
    if review is not None:
        result["review_vs_baseline"] = compare_pair(
            baseline, review, baseline_mode="baseline", candidate_mode="structured_review"
        )
        result["review_vs_structured"] = compare_pair(
            structured,
            review,
            baseline_mode="structured",
            candidate_mode="structured_review",
            minimum_gain=0,
        )
        if result["review_vs_baseline"]["passes"] and result["review_vs_structured"]["passes"]:
            result["recommended_mode"] = "structured_review"
    return result
