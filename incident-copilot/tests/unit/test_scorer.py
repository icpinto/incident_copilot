from incident_copilot.correlation.scorer import score_file
from incident_copilot.schemas.index import FileRecord
from incident_copilot.schemas.logs import ParsedLog, StackFrame


def test_score_file_stacktrace_match():
    record = FileRecord(file_path="services/api/main.py", symbols=[], imports=[], env_refs=[], routes=[], service_hints=["api"])
    parsed = ParsedLog(
        services=["api"],
        error_lines=[],
        exception_types=["ValueError"],
        stack_frames=[StackFrame(file_path="main.py", line_number=10, function_name=None, raw="x")],
        keywords=["main"],
        tables_or_entities=[],
        raw_text="",
    )
    score, reasons = score_file(record, parsed)
    assert score > 0
    assert reasons
