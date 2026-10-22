import pytest

from incident_copilot.correlation.log_parser import parse_logs


def test_parse_logs_extracts_exception():
    parsed = parse_logs('api | Traceback\nFile "/app/main.py", line 10, in run\nValueError: boom')
    assert "ValueError" in parsed.exception_types
    assert parsed.services == ["api"]


@pytest.mark.parametrize(
    "path",
    [
        "/app/src/view.tsx",
        r"C:\app\src\view.tsx",
        "/app/src/view.jsx",
    ],
)
def test_javascript_frame_preserves_extension_path_and_line(path):
    parsed = parse_logs(f"at render ({path}:42:7)")
    assert len(parsed.stack_frames) == 1
    assert parsed.stack_frames[0].file_path == path
    assert parsed.stack_frames[0].line_number == 42
