from typing import List
from pydantic import BaseModel


class ValidationResult(BaseModel):
    commands_run: List[str] = []
    passed: bool = False
    output: str = ""
