import re

from incident_copilot.schemas.logs import StackFrame

PY_FRAME_RE = re.compile(r'File "([^"]+)", line (\d+), in ([A-Za-z0-9_<>]+)')
GENERIC_PATH_RE = re.compile(
    r"((?:[A-Za-z]:)?[A-Za-z0-9_\-./\\]+\.(?:tsx|jsx|py|ts|js|go|java))"
    r"(?::(\d+))?(?::\d+)?(?![\w.])"
)


def parse_stack_frames(text: str) -> list[StackFrame]:
    frames: list[StackFrame] = []
    for line in text.splitlines():
        py = PY_FRAME_RE.search(line)
        if py:
            frames.append(
                StackFrame(
                    file_path=py.group(1),
                    line_number=int(py.group(2)),
                    function_name=py.group(3),
                    raw=line,
                )
            )
            continue
        gp = GENERIC_PATH_RE.search(line)
        if gp:
            line_no = int(gp.group(2)) if gp.group(2) else None
            frames.append(StackFrame(file_path=gp.group(1), line_number=line_no, raw=line))
    return frames
