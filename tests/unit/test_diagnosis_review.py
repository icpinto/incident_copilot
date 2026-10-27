from pathlib import Path

import pytest
from pydantic import ValidationError

from incident_copilot.evaluation.diagnosis_review import (
    IncidentReview,
    evaluate_reviews,
    load_reviews,
    main,
    review_template,
)
from incident_copilot.reporting.report_builder import build_report

FIXTURE = Path(__file__).parents[1] / "fixtures" / "diagnosis_review" / "synthetic.jsonl"


def test_metrics_use_causal_labels_and_disclose_unknowns():
    reviews = load_reviews(FIXTURE)
    metrics = evaluate_reviews(reviews)
    assert metrics["brier_score"]["value"] == pytest.approx((.04 + .04 + .81) / 3)
    assert metrics["brier_score"]["denominator"] == 3
    assert metrics["brier_score"]["excluded"] == 1
    assert metrics["non_abstention_coverage"] == {
        "value": .5, "numerator": 2, "denominator": 4, "excluded_unknown": 1}
    assert metrics["top_ranked_accuracy"] == {
        "value": .5, "numerator": 1, "denominator": 2, "excluded_unknown": 0}
    assert metrics["abstention_correctness"] == {
        "value": 1, "numerator": 1, "denominator": 1, "excluded_unknown": 1}
    assert metrics["counts"]["unknown_causal_labels"] == 1
    assert metrics["counts"]["unknown_sufficiency_labels"] == 2
    assert metrics["reliability_bins"][8] == {
        "lower": .8, "upper": .9, "count": 1, "mean_model_score": .8, "fraction_correct": 1}
    assert metrics["reliability_bins"][5]["count"] == 0


def test_empty_dataset_has_no_invented_zero_accuracy():
    metrics = evaluate_reviews([])
    assert metrics["brier_score"]["value"] is None
    assert metrics["non_abstention_coverage"]["value"] is None
    assert metrics["top_ranked_accuracy"]["value"] is None
    assert metrics["abstention_correctness"]["value"] is None
    assert all(b["count"] == 0 and b["fraction_correct"] is None for b in metrics["reliability_bins"])


def test_bin_boundaries_and_missing_score_are_not_mislabeled():
    review = load_reviews(FIXTURE)[0]
    review.hypotheses[0].model_score = 1
    review.hypotheses[1].model_score = None
    result = evaluate_reviews([review])
    assert result["brier_score"] == {"value": 0, "denominator": 1, "excluded": 1}
    assert result["reliability_bins"][9]["count"] == 1
    assert result["counts"]["missing_model_scores"] == 1
    review.hypotheses[0].model_score = 0
    assert evaluate_reviews([review])["reliability_bins"][0]["count"] == 1


def test_review_rejects_duplicate_incidents_and_malformed_labels():
    review = load_reviews(FIXTURE)[0]
    with pytest.raises(ValueError, match="Duplicate incident IDs"):
        evaluate_reviews([review, review])
    data = review.model_dump()
    data["hypotheses"][0]["causal_correct"] = "true"
    with pytest.raises(ValidationError):
        IncidentReview.model_validate(data)


def test_review_template_preserves_model_score_and_unknown_labels():
    from incident_copilot.llm.structured_output import report_hypothesis

    report = build_report({"incident_id": "review-me", "diagnosis_status": "provisional",
        "hypotheses": [report_hypothesis({"rank": 1, "title": "Mechanism", "explanation": "Evidence",
            "evidence": [], "confidence": .8, "next_checks": []})]})
    template = review_template(report, repository_revision="abc", model_version="model-v1", prompt_version="prompt-v2")
    assert template.evidence_sufficient is None
    assert template.hypotheses[0].model_score == .8
    assert template.hypotheses[0].causal_correct is None
    assert template.hypotheses[0].hypothesis_id == "review-me:rank-1"
    assert report.hypotheses[0].confidence is None


def test_review_cli_exports_and_evaluates(tmp_path, monkeypatch):
    report_path, template_path, metrics_path = [tmp_path / name for name in ("report.json", "review.jsonl", "metrics.json")]
    report_path.write_text(build_report({"incident_id": "empty", "diagnosis_status": "insufficient_evidence"}).model_dump_json())
    monkeypatch.setattr("sys.argv", ["review", "template", "--report", str(report_path), "--output", str(template_path),
        "--repository-revision", "abc", "--model-version", "model-v1", "--prompt-version", "prompt-v2"])
    main()
    assert len(load_reviews(template_path)) == 1
    monkeypatch.setattr("sys.argv", ["review", "evaluate", "--reviews", str(template_path), "--output", str(metrics_path)])
    main()
    import json
    assert json.loads(metrics_path.read_text())["abstention_correctness"]["denominator"] == 0
