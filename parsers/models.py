from __future__ import annotations
from typing import List, Optional
from pydantic import BaseModel, Field


class ErrorEntry(BaseModel):
    type: str
    detail: str
    file_path: Optional[str] = None
    line_no: Optional[int] = None


class LogParseResult(BaseModel):
    step: str = ""
    error_type: str = "unknown"
    error_message: str = ""
    module: str = ""
    errors: List[ErrorEntry] = Field(default_factory=list)
    key_lines: List[str] = Field(default_factory=list)   # max 10 most diagnostic lines — only these go to LLM
    raw_line_count: int = 0
