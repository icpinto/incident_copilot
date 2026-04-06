from typing import List, Optional
from pydantic import BaseModel


class StackFrame(BaseModel):
    file_path: Optional[str] = None
    line_number: Optional[int] = None
    function_name: Optional[str] = None
    raw: str


class ParsedLog(BaseModel):
    services: List[str] = []
    error_lines: List[str] = []
    exception_types: List[str] = []
    stack_frames: List[StackFrame] = []
    keywords: List[str] = []
    tables_or_entities: List[str] = []
    raw_text: str
