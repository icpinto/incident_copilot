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
    hypotheses: list[HypothesisReview] = Field(max_length=3)

    @model_validator(mode="after")
    def coherent(self):
        if sorted(h.rank for h in self.hypotheses) != list(range(1, len(self.hypotheses) + 1)):
            raise ValueError("Ranks must be unique and consecutive starting at 1.")
        if len({h.hypothesis_id for h in self.hypotheses}) != len(self.hypotheses):
            raise ValueError("Hypothesis IDs must be unique within an incident.")
        if self.diagnosis_status == "provisional" and not self.hypotheses:
            raise ValueError("A provisional diagnosis requires a hypothesis.")
        return self


def review_template(report: DiagnosisReport, *, repository_revision: str,
                    model_version: str, prompt_version: str) -> IncidentReview:
    return IncidentReview(
        incident_id=report.incident_id, repository_revision=repository_revision,
        model_version=model_version, prompt_version=prompt_version,
        diagnosis_status=report.diagnosis_status, evidence_sufficient=None,
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("template", help="Export one unlabeled JSONL review record")
    export.add_argument("--report", type=Path, required=True)
    for name in ("repository-revision", "model-version", "prompt-version"):
        export.add_argument(f"--{name}", required=True)
    evaluate = commands.add_parser("evaluate", help="Evaluate one reviewed JSONL dataset")
    evaluate.add_argument("--reviews", type=Path, required=True)
    for command in (export, evaluate):
        command.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "template":
        report = DiagnosisReport.model_validate_json(args.report.read_text(encoding="utf-8"))
        result = review_template(report, repository_revision=args.repository_revision,
                                 model_version=args.model_version, prompt_version=args.prompt_version)
        text = result.model_dump_json() + "\n"
    else:
        text = json.dumps(evaluate_reviews(load_reviews(args.reviews)), indent=2) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
