"""Small labeled code-graph cases; fixtures contain source, never fabricated telemetry."""
from pathlib import Path

FIXTURE_ROOT = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "code_graph"
GOLDEN_CASES = [
    {
        "id": f"{language}-{name}", "language": language,
        "fixture": str(FIXTURE_ROOT / language), "actual_file": f"{file}.{extension}",
        "symbol": symbol, "expected_edges": edges,
        "line": ({"gateway": 1, "wrapper": 4, "api": 4, "duplicates": 5, "dynamic": 1} if language == "python"
                 else {"gateway": 1, "wrapper": 3, "api": 3, "duplicates": 4, "dynamic": 1})[file],
        "dynamic": name == "dynamic", "duplicate": name == "duplicate",
    }
    for language, extension in (("python", "py"), ("typescript", "ts"))
    for name, file, symbol, edges in (
        ("direct", "gateway", "charge", [("checkout", "charge")]),
        ("wrapper", "wrapper", "checkout", [("handle_request", "checkout"), ("checkout", "charge")]),
        ("entry", "api", "handle_request", [("handle_request", "checkout")]),
        ("duplicate", "duplicates", "retry", [("retry", "charge")]),
        ("dynamic", "dynamic", "invoke", []),
    )
]
