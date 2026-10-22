from pathlib import Path

import pytest

from incident_copilot.correlation.candidate_ranker import rank_candidates
from incident_copilot.correlation.locations import select_stack_location
from incident_copilot.correlation.scorer import score_file
from incident_copilot.indexing.repo_indexer import index_repository
from incident_copilot.schemas.index import FileRecord, SymbolRecord
from incident_copilot.schemas.logs import ParsedLog, StackFrame

SOURCE = """def unrelated():
    return 'unrelated'

class Gateway:
    def charge(self):
        raise ValueError('payment failed')
"""


@pytest.mark.parametrize(
    "trace_path",
    [
        "/app/services/payments/handlers.py",
        "/usr/src/app/services/payments/handlers.py",
        r"C:\container\services\payments\handlers.py",
        "services/payments/handlers.py",
    ],
)
def test_trace_localizes_correct_file_and_enclosing_method(tmp_path, trace_path):
    root = tmp_path / "repo"
    # Insert the wrong service first, with identical symbols and basename.
    for service in ("shipping", "payments"):
        path = root / "services" / service / "handlers.py"
        path.parent.mkdir(parents=True)
        path.write_text(SOURCE, encoding="utf-8")
    index = tmp_path / "index.db"
    index_repository(root, None, index)

    candidates, _ = rank_candidates(
        root,
        index,
        f'File "{trace_path}", line 6, in charge\nValueError: payment failed',
    )

    top = candidates.items[0]
    assert top.file_path == "services/payments/handlers.py"
    assert top.score > candidates.items[1].score
    assert top.symbol_name == "charge"
    assert (top.line_start, top.line_end) == (5, 6)
    assert "raise ValueError" in top.snippet
    assert "unrelated" not in top.snippet
    assert any("exact path match" in reason for reason in top.reasons)


def test_module_level_failure_does_not_point_to_first_function(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "main.py").write_text(
        "def unrelated():\n    pass\n\nraise RuntimeError('startup')\n",
        encoding="utf-8",
    )
    index = tmp_path / "index.db"
    index_repository(root, None, index)
    candidates, _ = rank_candidates(
        root,
        index,
        'File "/app/main.py", line 4, in <module>\nRuntimeError: startup',
    )
    top = candidates.items[0]
    assert top.symbol_name is None
    assert top.line_start <= 4 <= top.line_end
    assert "raise RuntimeError" in top.snippet


def test_lexical_candidate_does_not_invent_a_symbol(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "payment.py").write_text(SOURCE, encoding="utf-8")
    index = tmp_path / "index.db"
    index_repository(root, None, index)
    candidates, _ = rank_candidates(root, index, "ValueError: payment failed")
    assert candidates.items[0].symbol_name is None
    assert candidates.items[0].line_start is None


def test_repeated_frame_does_not_inflate_score():
    record = FileRecord(file_path="services/api/main.py")
    frame = StackFrame(file_path="/app/services/api/main.py", raw="frame")
    once = score_file(record, ParsedLog(stack_frames=[frame], raw_text=""))
    repeated = score_file(record, ParsedLog(stack_frames=[frame] * 10, raw_text=""))
    assert once == repeated


@pytest.mark.parametrize("path", ["domain.py", "main.py.bak", "main.pyc"])
def test_stack_filename_requires_whole_components(path):
    parsed = ParsedLog(stack_frames=[StackFrame(file_path="main.py", raw="frame")], raw_text="")
    assert score_file(FileRecord(file_path=path), parsed) == (0, [])


def test_partial_container_path_outranks_basename():
    parsed = ParsedLog(
        stack_frames=[StackFrame(file_path="/app/src/main.py", raw="frame")],
        raw_text="",
    )
    matching = score_file(FileRecord(file_path="services/api/src/main.py"), parsed)[0]
    basename = score_file(FileRecord(file_path="services/worker/main.py"), parsed)[0]
    assert matching > basename > 0


def test_host_absolute_path_resolves_repository_root_file(tmp_path):
    parsed = ParsedLog(
        stack_frames=[StackFrame(file_path=str(tmp_path / "main.py"), raw="frame")],
        raw_text="",
    )
    root_file = score_file(FileRecord(file_path="main.py"), parsed, tmp_path)[0]
    other_file = score_file(FileRecord(file_path="services/worker/main.py"), parsed, tmp_path)[0]
    assert root_file > other_file


def test_function_name_from_unrelated_file_is_not_evidence():
    record = FileRecord(
        file_path="worker.py",
        symbols=[
            SymbolRecord(
                name="run",
                kind="function",
                line_start=1,
                line_end=3,
                file_path="worker.py",
            )
        ],
    )
    parsed = ParsedLog(
        stack_frames=[
            StackFrame(
                file_path="api.py",
                function_name="run",
                raw="frame",
            )
        ],
        raw_text="",
    )
    assert score_file(record, parsed) == (0, [])


def test_innermost_function_wins_and_named_fallback_handles_missing_line():
    record = FileRecord(
        file_path="main.py",
        language="python",
        symbols=[
            SymbolRecord(
                name="outer", kind="function", line_start=1, line_end=10, file_path="main.py"
            ),
            SymbolRecord(
                name="inner", kind="function", line_start=4, line_end=6, file_path="main.py"
            ),
        ],
    )
    frame = StackFrame(file_path="/app/main.py", line_number=5, raw="frame")
    _, symbol = select_stack_location(record, [frame], Path("/repo"))
    assert symbol.name == "inner"
    frame = StackFrame(file_path="/app/main.py", function_name="inner", raw="frame")
    _, symbol = select_stack_location(record, [frame], Path("/repo"))
    assert symbol.name == "inner"


def test_duplicate_method_names_are_resolved_by_line():
    record = FileRecord(
        file_path="main.py",
        language="python",
        symbols=[
            SymbolRecord(
                name="run", kind="function", line_start=2, line_end=4, file_path="main.py"
            ),
            SymbolRecord(
                name="run", kind="function", line_start=8, line_end=10, file_path="main.py"
            ),
        ],
    )
    frame = StackFrame(file_path="/app/main.py", line_number=9, function_name="run", raw="frame")
    _, symbol = select_stack_location(record, [frame], Path("/repo"))
    assert symbol.line_start == 8
