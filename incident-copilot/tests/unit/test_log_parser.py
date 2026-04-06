from incident_copilot.correlation.log_parser import parse_logs


def test_parse_logs_extracts_exception():
    parsed = parse_logs('api | Traceback\nFile "/app/main.py", line 10, in run\nValueError: boom')
    assert "ValueError" in parsed.exception_types
    assert parsed.services == ["api"]
