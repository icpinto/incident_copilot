"""Export human review templates and evaluate uncalibrated diagnosis scores."""
import argparse
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from incident_copilot.schemas.diagnosis import DiagnosisReport


class HypothesisReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hypothesis_id: str = Field(min_length=1)
    rank: int = Field(ge=1, le=3, strict=True)
    model_score: float | None = Field(ge=0, le=1, allow_inf_nan=False)
    causal_correct: bool | None = Field(strict=True)


class IncidentReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident_id: str = Field(min_length=1)
    repository_revision: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    diagnosis_status: Literal["provisional", "insufficient_evidence", "unknown"]
    evidence_sufficient: bool | None = Field(strict=True)
    dataset_split: Literal["development", "held_out", "unspecified"] = "unspecified"
    case_origin: Literal["verified", "synthetic", "unknown"] = "unknown"
    independently_adjudicated: bool = Field(default=False, strict=True)
    inference_mode: Literal["single_pass", "iterative", "unknown"] = "unknown"
    reasoning_mode: Literal["baseline", "structured", "structured_review"] | None = None
    evidence_fingerprint: str | None = None
    latency_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    unsupported_claims: int | None = Field(default=None, ge=0, strict=True)
    claim_count: int | None = Field(default=None, ge=0, strict=True)
    review_status: str | None = None
    draft_causal_correct: bool | None = Field(default=None, strict=True)
    hypotheses: list[HypothesisReview] = Field(max_length=3)

    @model_validator(mode="after")
    def coherent(self):
        if sorted(h.rank for h in self.hypotheses) != list(range(1, len(self.hypotheses) + 1)):
            raise ValueError("Ranks must be unique and consecutive starting at 1.")
        if len({h.hypothesis_id for h in self.hypotheses}) != len(self.hypotheses):
            raise ValueError("Hypothesis IDs must be unique within an incident.")
        if self.diagnosis_status == "provisional" and not self.hypotheses:
            raise ValueError("A provisional diagnosis requires a hypothesis.")
        if self.unsupported_claims is not None and self.claim_count is not None and self.unsupported_claims > self.claim_count:
            raise ValueError("Unsupported claims cannot exceed the total claim count.")
        return self


def review_template(report: DiagnosisReport, *, repository_revision: str,
                    model_version: str, prompt_version: str) -> IncidentReview:
    return IncidentReview(
        incident_id=report.incident_id, repository_revision=repository_revision,
        model_version=model_version, prompt_version=prompt_version,
        diagnosis_status=report.diagnosis_status, evidence_sufficient=None,
        reasoning_mode=report.reasoning.mode if report.reasoning else None,
        review_status=report.reasoning.review_status if report.reasoning else None,
        hypotheses=[HypothesisReview(
            hypothesis_id=f"{report.incident_id}:rank-{h.rank}", rank=h.rank,
            model_score=h.model_score, causal_correct=None,
        ) for h in report.hypotheses],
    )


def load_reviews(path: Path) -> list[IncidentReview]:
    return [IncidentReview.model_validate_json(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def evaluate_reviews(reviews: list[IncidentReview]) -> dict:
    """All metrics disclose denominators; unknown labels never become negatives."""
    if len({r.incident_id for r in reviews}) != len(reviews):
        raise ValueError("Duplicate incident IDs; evaluate each comparison arm in a separate file.")
    hypotheses = [h for r in reviews for h in r.hypotheses]
    scored = [h for h in hypotheses if h.model_score is not None and h.causal_correct is not None]
    provisional = [r for r in reviews if r.diagnosis_status == "provisional"]
    abstained = [r for r in reviews if r.diagnosis_status == "insufficient_evidence"]
    top = [next(h for h in r.hypotheses if h.rank == 1) for r in provisional]
    judged_top = [h for h in top if h.causal_correct is not None]
    judged_abstentions = [r for r in abstained if r.evidence_sufficient is not None]

    def rate(numerator, denominator, excluded_unknown):
        return {"value": numerator / denominator if denominator else None,
                "numerator": numerator, "denominator": denominator,
                "excluded_unknown": excluded_unknown}

    bins = []
    for index in range(10):
        members = [h for h in scored if min(int(h.model_score * 10), 9) == index]
        bins.append({
            "lower": index / 10, "upper": (index + 1) / 10, "count": len(members),
            "mean_model_score": sum(h.model_score for h in members) / len(members) if members else None,
            "fraction_correct": sum(h.causal_correct for h in members) / len(members) if members else None,
        })
    return {
        **causal_metrics(reviews),
        "score_basis": "uncalibrated model self-assessment; not a diagnosis probability",
        "counts": {
            "incidents": len(reviews), "hypotheses": len(hypotheses),
            "unknown_outcomes": len(reviews) - len(provisional) - len(abstained),
            "unknown_causal_labels": sum(h.causal_correct is None for h in hypotheses),
            "unknown_sufficiency_labels": sum(r.evidence_sufficient is None for r in reviews),
            "missing_model_scores": sum(h.model_score is None for h in hypotheses),
        },
        "brier_score": {
            "value": sum((h.model_score - int(h.causal_correct)) ** 2 for h in scored) / len(scored) if scored else None,
            "denominator": len(scored), "excluded": len(hypotheses) - len(scored),
        },
        "reliability_bins": bins,
        "non_abstention_coverage": rate(len(provisional), len(provisional) + len(abstained),
                                       len(reviews) - len(provisional) - len(abstained)),
        "top_ranked_accuracy": rate(sum(h.causal_correct for h in judged_top), len(judged_top),
                                    len(top) - len(judged_top)),
        "abstention_correctness": rate(sum(not r.evidence_sufficient for r in judged_abstentions),
                                       len(judged_abstentions), len(abstained) - len(judged_abstentions)),
    }


def diagnosis_success(review: IncidentReview) -> bool | None:
    if review.diagnosis_status == "insufficient_evidence":
        return False
    if review.diagnosis_status != "provisional":
        return None
    return next(h.causal_correct for h in review.hypotheses if h.rank == 1)


def proportion(numerator, denominator, excluded=0):
    # Wilson 95% interval, including well-defined intervals at zero and one.
    value = numerator / denominator if denominator else None
    interval = None
    if denominator:
        z = 1.959963984540054
        scale = 1 + z * z / denominator
        center = (value + z * z / (2 * denominator)) / scale
        radius = z * ((value * (1 - value) / denominator + z * z / (4 * denominator ** 2)) ** .5) / scale
        interval = [max(0, center - radius), min(1, center + radius)]
    return {"value": value, "numerator": numerator, "denominator": denominator,
            "excluded_unknown": excluded, "wilson_95_interval": interval}


def causal_metrics(reviews):
    sufficient = [r for r in reviews if r.evidence_sufficient is True]
    judged = [r for r in sufficient if diagnosis_success(r) is not None]
    insufficient = [r for r in reviews if r.evidence_sufficient is False and r.diagnosis_status != "unknown"]
    claims = [r for r in reviews if r.unsupported_claims is not None and r.claim_count is not None]
    changed = [r for r in reviews if r.draft_causal_correct is not None and diagnosis_success(r) is not None]
    return {
        "causal_diagnosis_success": proportion(sum(diagnosis_success(r) for r in judged), len(judged), len(reviews) - len(judged)),
        "false_causation_rate": proportion(sum(r.diagnosis_status == "provisional" for r in insufficient), len(insufficient), len(reviews) - len(insufficient)),
        "unsupported_claim_rate": proportion(sum(r.unsupported_claims for r in claims), sum(r.claim_count for r in claims), len(reviews) - len(claims)),
        "review_effect": {"improved_drafts": sum(not r.draft_causal_correct and diagnosis_success(r) for r in changed),
                          "damaged_drafts": sum(r.draft_causal_correct and not diagnosis_success(r) for r in changed),
                          "judged_pairs": len(changed), "excluded_unknown": len(reviews) - len(changed)},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("template", help="Export one unlabeled JSONL review record")
    source = export.add_mutually_exclusive_group(required=True)
    source.add_argument("--report", type=Path)
    source.add_argument("--benchmark", type=Path)
    export.add_argument("--inference-mode", choices=["single_pass", "iterative"])
    export.add_argument("--reasoning-mode", choices=["baseline", "structured", "structured_review"])
    for name in ("repository-revision", "model-version", "prompt-version"):
        export.add_argument(f"--{name}")
    evaluate = commands.add_parser("evaluate", help="Evaluate one reviewed JSONL dataset")
    evaluate.add_argument("--reviews", type=Path, required=True)
    for command in (export, evaluate):
        command.add_argument("--output", type=Path, required=True)
    compare = commands.add_parser("compare", help="Evaluate held-out promotion gates without changing settings")
    compare.add_argument("--baseline-reviews", type=Path, required=True)
    compare.add_argument("--structured-reviews", type=Path, required=True)
    compare.add_argument("--review-reviews", type=Path)
    compare.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "template":
        if args.benchmark:
            if not args.inference_mode or not args.reasoning_mode:
                parser.error("--benchmark requires --inference-mode and --reasoning-mode")
            arm = json.loads(args.benchmark.read_text(encoding="utf-8"))["reasoning_comparison"][args.inference_mode][args.reasoning_mode]
            report = DiagnosisReport.model_validate(arm["report"])
            result = review_template(report, repository_revision=arm["repository_revision"],
                                     model_version=arm["model_version"], prompt_version=arm["prompt_version"])
            result = result.model_copy(update={"inference_mode": args.inference_mode,
                "reasoning_mode": args.reasoning_mode, "evidence_fingerprint": arm["evidence_fingerprint"],
                "latency_seconds": arm["metrics"]["latency_seconds"]})
        else:
            if not all((args.repository_revision, args.model_version, args.prompt_version)):
                parser.error("--report requires --repository-revision, --model-version and --prompt-version")
            report = DiagnosisReport.model_validate_json(args.report.read_text(encoding="utf-8"))
            result = review_template(report, repository_revision=args.repository_revision,
                                     model_version=args.model_version, prompt_version=args.prompt_version)
        text = result.model_dump_json() + "\n"
    elif args.command == "compare":
        from incident_copilot.evaluation.reasoning_gates import compare_policies
        text = json.dumps(compare_policies(load_reviews(args.baseline_reviews), load_reviews(args.structured_reviews),
                          load_reviews(args.review_reviews) if args.review_reviews else None), indent=2) + "\n"
    else:
        text = json.dumps(evaluate_reviews(load_reviews(args.reviews)), indent=2) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
